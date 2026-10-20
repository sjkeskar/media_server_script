"""Magnet link tests: parsing, .magnet drop files, the --add-magnet command,
the metadata wait and timeout, and the Windows link handler (against a
scratch registry key, never the real magnet: handler)."""

import base64
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_phase1 import FakeFile, build_service, drop, make_torrent
from torrentfiler import config as cfgmod
from torrentfiler import db, magnet

PASSED = []
FAILED = []

HEX = "c9e15763f722f23e98a29decdfae341b98d53056"
B32 = base64.b32encode(bytes.fromhex(HEX)).decode()


def check(label, condition, detail=""):
    (PASSED if condition else FAILED).append(label)
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}{(' -- ' + detail) if detail else ''}")


def link(infohash=HEX, name="Some.Show.S01E01.1080p"):
    dn = f"&dn={name.replace(' ', '+')}" if name else ""
    return f"magnet:?xt=urn:btih:{infohash}{dn}&tr=udp%3A%2F%2Ftracker.example%3A1337"


def rejects(uri):
    try:
        magnet.parse(uri)
        return False
    except magnet.MagnetError:
        return True


def test_parse(work: Path):
    print("\nparsing")
    m = magnet.parse(link(HEX.upper()))
    check("hex infohash normalised to lower case", m.infohash == HEX)
    check("base32 infohash converted to hex", magnet.parse(link(B32)).infohash == HEX)
    check("display name decoded",
          magnet.parse(link(name="A Film (2024)")).display_name == "A Film (2024)")
    check("no display name falls back to hash",
          magnet.parse(link(name=None)).name == f"magnet {HEX[:8]}")
    check("rejects non-magnet", rejects("https://example.com/file.torrent"))
    check("rejects missing infohash", rejects("magnet:?dn=nothing"))
    check("rejects malformed infohash", rejects(link("xyz123")))
    check("rejects v2-only magnet",
          rejects("magnet:?xt=urn:btmh:1220" + "ab" * 32 + "&dn=v2"))

    f = work / "a.magnet"
    f.write_text("\ufeff\n# from a forum post\n  " + link() + "  \n", encoding="utf-8")
    check("reads link from a file with BOM, blank and comment lines",
          magnet.read(f).infohash == HEX)

    target = magnet.write_drop_file(link(name='Bad:Name/With*Chars?'), work / "drop")
    check("drop file gets a safe name", target.name == f"Bad_Name_With_Chars [{HEX[:8]}].magnet",
          target.name)
    check("drop file round-trips", magnet.read(target).infohash == HEX)
    check("no temporary file left behind", not list((work / "drop").glob("*.tmp")))


def test_cli(work: Path):
    print("\n--add-magnet")
    from torrentfiler.__main__ import main

    root = work / "cli"
    toml = root / "config.toml"
    root.mkdir(parents=True)
    toml.write_text(f"""
[paths]
watch_root    = '{root / "watch"}'
temp_download = '{root / "tmp"}'
library_root  = '{root / "lib"}'
database      = '{root / "x.db"}'
[transmission]
username = "u"
password = "p"
""", encoding="utf-8")
    errors = []
    magnet.show_error = errors.append  # no message boxes during tests

    check("valid link exits 0", main(["--config", str(toml), "--add-magnet", link()]) == 0)
    check("written into inbox/", len(list((root / "watch/inbox").glob("*.magnet"))) == 1)
    main(["--config", str(toml), "--add-magnet", link(name="Other"), "--force"])
    check("--force writes into force/", len(list((root / "watch/force").glob("*.magnet"))) == 1)
    check("invalid link exits 1 with an error shown",
          main(["--config", str(toml), "--add-magnet", "magnet:?dn=x"]) == 1 and errors)


def test_service(work: Path):
    print("\nservice")
    svc, cfg = build_service(work / "svc")

    drop(cfg, cfgmod.INBOX, "show.magnet", (link() + "\n").encode())
    svc.tick()
    row = db.get(svc.conn, HEX)
    check("magnet accepted and downloading", row and row["state"] == db.DOWNLOADING,
          row["state"] if row else "no record")
    check("name taken from dn", row["name"] == "Some.Show.S01E01.1080p")
    check("size unknown until metadata", row["size_bytes"] is None)
    check("claimed as processing/<hash>.magnet",
          Path(row["source_file"]).name == f"{HEX}.magnet")
    check("metadata clock started", row["stalled_since"] is not None)

    torrent = svc.client.torrents[row["transmission_id"]]
    torrent.metadata_percent_complete = 0.0
    svc.tick()
    check("waits while metadata is missing", db.get(svc.conn, HEX)["state"] == db.DOWNLOADING)

    # Metadata arrives: real name, size and file list.
    files = {"Some.Show.S01E01.1080p/episode.mkv": b"e" * 70, "Some.Show.S01E01.1080p/e.srt": b"s"}
    for rel, payload in files.items():
        p = cfg.paths.temp_download / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(payload)
    torrent.files = [FakeFile(rel, len(b)) for rel, b in files.items()]
    torrent.metadata_percent_complete = 1.0
    torrent.name = "Some.Show.S01E01.1080p"
    torrent.total_size = 71
    svc.tick()
    row = db.get(svc.conn, HEX)
    check("metadata fills size and file count",
          row["size_bytes"] == 71 and row["file_count"] == 2 and row["stalled_since"] is None)

    torrent.percent_done = 1.0
    svc.tick()
    check("magnet download is filed like any other", db.get(svc.conn, HEX)["state"] == db.FILED)
    check("its media reached the library",
          (cfg.paths.library_root / "Some.Show.S01E01.1080p/episode.mkv").is_file())

    # Same content as a .torrent later: deduplicated by infohash.
    raw, ih = make_torrent("Dup.Check")
    drop(cfg, cfgmod.INBOX, "dup.magnet", (link(ih, "Dup.Check") + "\n").encode())
    svc.tick()
    drop(cfg, cfgmod.INBOX, "dup.torrent", raw)
    svc.tick()
    check(".torrent of a known magnet skipped as duplicate",
          len(list(cfg.paths.folder(cfgmod.SKIPPED).glob("dup*.torrent"))) == 1)

    drop(cfg, cfgmod.INBOX, "broken.magnet", b"not a link at all\n")
    svc.tick()
    failed = cfg.paths.folder(cfgmod.FAILED)
    check("unusable .magnet quarantined with a note",
          (failed / "broken.magnet").is_file()
          and "magnet link" in (failed / "broken.magnet.error.txt").read_text())
    svc.conn.close()


def test_timeout(work: Path):
    print("\nmetadata timeout")
    svc, cfg = build_service(work / "timeout")
    dead = "ab" * 20
    drop(cfg, cfgmod.INBOX, "dead.magnet", (link(dead, "Dead.Link") + "\n").encode())
    svc.tick()
    row = db.get(svc.conn, dead)
    tid = row["transmission_id"]
    svc.client.torrents[tid].metadata_percent_complete = 0.0

    db.update(svc.conn, dead, stalled_since=(
        datetime.now(timezone.utc) - timedelta(minutes=29)).isoformat())
    svc.tick()
    check("29 minutes without metadata keeps waiting",
          db.get(svc.conn, dead)["state"] == db.DOWNLOADING)

    db.update(svc.conn, dead, stalled_since=(
        datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat())
    svc.tick()
    row = db.get(svc.conn, dead)
    check("31 minutes without metadata fails it", row["state"] == db.FAILED)
    check("removed from Transmission", (tid, True) in svc.client.removed)
    check("slot freed", db.count_active(svc.conn) == 0)
    check("quarantined with an explanation",
          "no metadata" in next(cfg.paths.folder(cfgmod.FAILED).glob("*.error.txt")).read_text())

    drop(cfg, cfgmod.INBOX, "dead-again.magnet", (link(dead, "Dead.Link") + "\n").encode())
    svc.tick()
    row = db.get(svc.conn, dead)
    check("re-dropping a timed-out magnet retries it",
          row["state"] == db.DOWNLOADING and row["stalled_since"] is not None)
    svc.conn.close()


def test_recovery_and_client(work: Path):
    print("\nrecovery and client")
    svc, cfg = build_service(work / "rec")
    ih = "cd" * 20
    claimed = cfg.paths.folder(cfgmod.PROCESSING) / f"{ih}.magnet"
    claimed.write_text(link(ih) + "\n", encoding="utf-8")
    db.insert(svc.conn, infohash=ih, name="x", source_file=str(claimed), size_bytes=None,
              file_count=None, force_download=False, force_dup=False)
    svc._recover()
    check("claimed .magnet recovered as queued", db.get(svc.conn, ih)["state"] == db.QUEUED)
    svc.conn.close()

    from torrentfiler.client import TransmissionClient

    class StubRpc:
        def add_torrent(self, payload, **kwargs):
            self.payload = payload
            return type("T", (), {"id": 7})()

        def start_torrent(self, ids):
            pass

    real = TransmissionClient(cfg.transmission)
    real._client = StubRpc()
    real.add(claimed, download_dir=cfg.paths.temp_download, peer_limit=20, download_limit_kbps=0)
    check("client sends the link itself to Transmission",
          real._client.payload == link(ih))


def test_handler():
    print("\nWindows link handler (scratch registry key)")
    import winreg

    scratch = r"Software\torrentfiler-test"
    magnet._ROOT = scratch
    keys = magnet._keys()
    hkcu = winreg.HKEY_CURRENT_USER

    def value(path, name=""):
        try:
            with winreg.OpenKey(hkcu, path) as key:
                return winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return None

    try:
        with winreg.CreateKey(hkcu, keys["classic_cmd"]) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, '"C:\\uTorrent.exe" "%1"')

        cmd = magnet.handler_command(Path(r"C:\venv\pythonw.exe"),
                                     Path(r"Z:\t\magnet_handler.pyw"), Path(r"Z:\t\config.toml"))
        check("handler command passes the link through",
              cmd.endswith('--add-magnet "%1"') and "magnet_handler.pyw" in cmd)
        previous = magnet.register_handler(cmd)
        check("register returns the handler it replaced", previous == '"C:\\uTorrent.exe" "%1"')
        check("register installs the command", magnet.current_handler() == cmd)
        check("registered as a named application for magnet:",
              value(keys["progid"] + r"\shell\open\command") == cmd
              and value(keys["progid"] + r"\Application", "ApplicationName") == "torrentfiler"
              and value(keys["app"] + r"\Capabilities\URLAssociations", "magnet") == magnet.PROGID
              and value(keys["regapps"], "torrentfiler") == keys["app"] + r"\Capabilities")
        magnet.register_handler(cmd)  # registering twice must not lose the backup
        check("unregister restores the original",
              magnet.unregister_handler() == '"C:\\uTorrent.exe" "%1"'
              and magnet.current_handler() == '"C:\\uTorrent.exe" "%1"')
        check("unregister removes the application registration",
              value(keys["progid"]) is None and value(keys["app"] + r"\Capabilities",
                                                      "ApplicationName") is None
              and value(keys["regapps"], "torrentfiler") is None)
    finally:
        magnet._delete_tree(hkcu, scratch)
        magnet._ROOT = "Software"


def main():
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        test_parse(work)
        test_cli(work)
        test_service(work)
        test_timeout(work)
        test_recovery_and_client(work)
        test_handler()

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
