"""Category rules: classification, per-category filing (copy or move), seeding
limits, the stall rule, crash recovery mid-move, config, schema upgrade and
--refile. Real files in a temp folder, fake Transmission."""

import dataclasses
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_phase1 import FakeFile, build_service, drop, make_torrent
from torrentfiler import config as cfgmod
from torrentfiler import db
from torrentfiler.filer import TorrentFile, classify, is_wanted

PASSED = []
FAILED = []


def check(label, condition, detail=""):
    (PASSED if condition else FAILED).append(label)
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}{(' -- ' + detail) if detail else ''}")


def stage(cfg, files):
    out = []
    for rel, payload in files.items():
        path = cfg.paths.temp_download / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        out.append(FakeFile(rel, len(payload)))
    return out


def download(svc, cfg, name, files):
    raw, ih = make_torrent(name)
    svc.client.next_files = stage(cfg, files)
    drop(cfg, cfgmod.INBOX, f"{name}.torrent", raw)
    svc.tick()
    row = db.get(svc.conn, ih)
    svc.client.torrents[row["transmission_id"]].percent_done = 1.0
    svc.tick()
    return ih, row["transmission_id"]


def state(svc, ih):
    return db.get(svc.conn, ih)["state"]


def ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


def tf(*names):
    return [TorrentFile(n, 1, 1) for n in names]


def test_classify():
    print("\nclassification")
    check("any video -> video", classify(tf("x/a.mkv", "x/a.flac")) == "video")
    check("sample-only video is not video", classify(tf("x/sample.mkv", "x/a.txt")) == "other")
    check("audio, no video -> music", classify(tf("Album/01.flac", "Album/cover.jpg")) == "music")
    check("disk image -> iso", classify(tf("ubuntu.iso")) == "iso")
    check("iso with checksums -> iso", classify(tf("d/fedora.iso", "d/SHA256SUM")) == "iso")
    check("anything else -> other", classify(tf("book.epub", "setup.exe")) == "other")

    music = ["A/01 Song.flac", "A/02.mp3", "A/cover.jpg", "A/Front.png", "A/album.cue",
             "A/01 Song.lrc", "A/AlbumArtSmall.jpg"]
    junk = ["A/rip.log", "A/info.nfo", "A/Scans/page01.jpg", "A/sample.flac"]
    check("music keeps audio, art, lyrics, cue",
          all(is_wanted(p, "music") for p in music), str([p for p in music
                                                         if not is_wanted(p, "music")]))
    check("music skips logs, nfo, scans, samples",
          not any(is_wanted(p, "music") for p in junk))
    check("iso and other keep every file",
          is_wanted("x/readme.txt", "iso") and is_wanted("x/SHA256SUM", "other"))


def test_music(work):
    print("\nmusic: copy, seed 3 hours")
    svc, cfg = build_service(work / "music")
    music_root = cfg.root_for("music")
    ih, tid = download(svc, cfg, "Album", {
        "Artist - Album/01 Track.flac": b"f" * 300,
        "Artist - Album/cover.jpg": b"c",
        "Artist - Album/rip.log": b"l",
    })
    row = db.get(svc.conn, ih)
    check("filed as music", row["state"] == db.FILED and row["category"] == "music",
          f"{row['state']} {row['category']}")
    check("copied into jellyfin-music",
          music_root.name == "jellyfin-music"
          and (music_root / "Artist - Album/01 Track.flac").read_bytes() == b"f" * 300
          and (music_root / "Artist - Album/cover.jpg").is_file())
    check("rip log not copied", not (music_root / "Artist - Album/rip.log").exists())
    check("nothing put in the video library",
          not (cfg.paths.library_root / "Artist - Album").exists())

    t = svc.client.torrents[tid]
    t.done_date = ago(hours=2.9)
    svc.tick()
    check("2.9 hours: still seeding", state(svc, ih) == db.FILED)
    t.done_date = ago(hours=3.1)
    svc.tick()
    check("3 hours: seeding ended, removed with its temp copy",
          state(svc, ih) == db.PURGED and (tid, True) in svc.client.removed)
    check("music copy kept", (music_root / "Artist - Album/01 Track.flac").is_file())
    svc.conn.close()


def test_iso(work):
    print("\niso: move, no seeding")
    svc, cfg = build_service(work / "iso")
    dl_root = cfg.root_for("iso")
    ih, tid = download(svc, cfg, "Distro", {"distro-26.04.iso": b"i" * 500})
    row = db.get(svc.conn, ih)
    check("moved and done in one go", row["state"] == db.PURGED and row["category"] == "iso",
          row["state"])
    check("removed from Transmission without deleting data",
          (tid, False) in svc.client.removed and tid not in svc.client.torrents)
    check("single file wrapped in its own folder under download",
          dl_root.name == "download"
          and (dl_root / "distro-26.04/distro-26.04.iso").read_bytes() == b"i" * 500)
    check("no duplicate left in the temp folder",
          not (cfg.paths.temp_download / "distro-26.04.iso").exists())
    check(".torrent filed under done/",
          Path(row["source_file"]).parent.name == cfgmod.DONE)

    ih2, _ = download(svc, cfg, "Fedora", {"Fedora-44/Fedora-44.iso": b"F" * 400,
                                           "Fedora-44/CHECKSUM": b"sum"})
    check("multi-file iso moved with every file",
          (dl_root / "Fedora-44/Fedora-44.iso").is_file()
          and (dl_root / "Fedora-44/CHECKSUM").is_file())
    check("its emptied temp folder removed", not (cfg.paths.temp_download / "Fedora-44").exists())

    # A different file already at the destination: stop BEFORE removing the
    # torrent, so nothing is half-done.
    (dl_root / "Clash").mkdir(parents=True)
    (dl_root / "Clash/Clash.iso").write_bytes(b"precious")
    ih3, tid3 = download(svc, cfg, "Clash", {"Clash/Clash.iso": b"n" * 50})
    check("conflict -> review, torrent still in Transmission",
          state(svc, ih3) == db.NEEDS_REVIEW and tid3 in svc.client.torrents)
    check("existing file untouched", (dl_root / "Clash/Clash.iso").read_bytes() == b"precious")

    # Downloaded file deleted by hand while Transmission still says complete.
    raw5, ih5 = make_torrent("Deleted")
    svc.client.next_files = stage(cfg, {"Deleted.iso": b"d" * 30})
    drop(cfg, cfgmod.INBOX, "Deleted.torrent", raw5)
    svc.tick()
    (cfg.paths.temp_download / "Deleted.iso").unlink()
    tid5 = db.get(svc.conn, ih5)["transmission_id"]
    svc.client.torrents[tid5].percent_done = 1.0
    svc.tick()
    row5 = db.get(svc.conn, ih5)
    check("file deleted outside torrentfiler -> review at once, no retry loop",
          row5["state"] == db.NEEDS_REVIEW and row5["attempts"] == 0
          and "missing from the download folder" in row5["last_error"], row5["state"])
    check("and the torrent is left in Transmission", tid5 in svc.client.torrents)

    # Crash after Transmission let go but before the move happened.
    raw, ih4 = make_torrent("Crashy")
    svc.client.next_files = stage(cfg, {"Crashy.iso": b"z" * 70})
    drop(cfg, cfgmod.INBOX, "Crashy.torrent", raw)
    svc.tick()
    tid4 = db.get(svc.conn, ih4)["transmission_id"]
    src = cfg.paths.temp_download / "Crashy.iso"
    dst = dl_root / "Crashy/Crashy.iso"
    db.update(svc.conn, ih4, state=db.FILING, category="iso")
    db.record_file(svc.conn, ih4, str(src), str(dst), verified=False)
    del svc.client.torrents[tid4]
    svc.tick()
    check("interrupted move finished after restart",
          state(svc, ih4) == db.PURGED and dst.read_bytes() == b"z" * 70 and not src.exists(),
          state(svc, ih4))
    svc.conn.close()


def test_other(work):
    print("\nother: copy everything, seed 1 day or ratio 0.5")
    svc, cfg = build_service(work / "other")
    dl_root = cfg.root_for("other")
    ih, tid = download(svc, cfg, "Book", {"Book/book.epub": b"e" * 90, "Book/readme.txt": b"r"})
    check("filed as other with every file copied",
          db.get(svc.conn, ih)["category"] == "other"
          and (dl_root / "Book/book.epub").is_file() and (dl_root / "Book/readme.txt").is_file())
    check("temp copy kept for seeding", (cfg.paths.temp_download / "Book/book.epub").is_file())
    t = svc.client.torrents[tid]
    t.ratio = 0.49
    svc.tick()
    check("ratio 0.49: still seeding", state(svc, ih) == db.FILED)
    t.ratio = 0.5
    svc.tick()
    check("ratio 0.5: done", state(svc, ih) == db.PURGED)

    ih2, tid2 = download(svc, cfg, "Game", {"Game/setup.exe": b"g" * 40})
    svc.client.torrents[tid2].done_date = ago(hours=24.1)
    svc.tick()
    check("1 day: done", state(svc, ih2) == db.PURGED)
    svc.conn.close()


def test_stall(work):
    print("\nstall rule: no uploads for 30% of the seed time")
    svc, cfg = build_service(work / "stall")
    ih, tid = download(svc, cfg, "Movie", {"Movie/Movie.mkv": b"m" * 80})
    t = svc.client.torrents[tid]
    t.uploaded_ever = 1000
    db.update(svc.conn, ih, seed_mark=1000, seed_mark_at=ago(hours=71).isoformat())
    svc.tick()
    check("video: 71h without uploads keeps seeding", state(svc, ih) == db.FILED)

    t.uploaded_ever = 5000
    db.update(svc.conn, ih, seed_mark_at=ago(hours=100).isoformat())
    svc.tick()
    row = db.get(svc.conn, ih)
    check("an upload restarts the clock",
          row["state"] == db.FILED and row["seed_mark"] == 5000
          and datetime.fromisoformat(row["seed_mark_at"]) > ago(minutes=1))

    db.update(svc.conn, ih, seed_mark_at=ago(hours=72.5).isoformat())
    svc.tick()
    check("video: 72h (30% of 10 days) without uploads ends seeding",
          state(svc, ih) == db.PURGED and (tid, True) in svc.client.removed)

    ih2, tid2 = download(svc, cfg, "Album", {"Album/01.flac": b"a" * 30})
    svc.client.torrents[tid2].uploaded_ever = 0
    db.update(svc.conn, ih2, seed_mark=0, seed_mark_at=ago(minutes=55).isoformat())
    svc.tick()
    check("music: 54 minutes (30% of 3h) without uploads ends seeding",
          state(svc, ih2) == db.PURGED)

    off = dataclasses.replace(cfg, filing=dataclasses.replace(cfg.filing, stall_fraction=0))
    svc.cfg = off
    ih3, tid3 = download(svc, cfg, "Show", {"Show/e1.mkv": b"s" * 30})
    db.update(svc.conn, ih3, seed_mark=0, seed_mark_at=ago(hours=200).isoformat())
    svc.tick()
    check("stall_fraction = 0 turns the rule off", state(svc, ih3) == db.FILED)
    svc.conn.close()


def test_config(work):
    print("\nconfig")
    root = work / "cfg"
    root.mkdir()
    base = f"""
[paths]
watch_root    = '{root / "w"}'
temp_download = '{root / "t"}'
library_root  = 'Z:\\jellyfin'
database      = '{root / "x.db"}'
[transmission]
username = "u"
password = "p"
"""

    def load(extra):
        (root / "c.toml").write_text(base + extra, encoding="utf-8")
        return cfgmod.load(root / "c.toml")

    c = load("")
    check("defaults: music 3h, iso move, other 0.5 / 24h, stall 30%",
          c.filing.music.seed_hours == 3 and c.filing.iso.mode == "move"
          and c.filing.other.seed_ratio == 0.5 and c.filing.other.seed_hours == 24
          and c.filing.stall_fraction == 0.3)
    check("default folders beside the library",
          str(c.root_for("music")) == r"Z:\jellyfin-music"
          and str(c.root_for("iso")) == r"Z:\download"
          and str(c.root_for("video")) == r"Z:\jellyfin")
    c = load("[filing]\nseed_ratio = 2.0\nseed_days = 5\n")
    check("old seed_ratio / seed_days still set the video policy",
          c.filing.video.seed_ratio == 2.0 and c.filing.video.seed_hours == 120)
    c = load("[filing.music]\nroot = 'D:\\Music'\nseed_hours = 6\n")
    check("per-category override", str(c.root_for("music")) == r"D:\Music"
          and c.filing.music.seed_hours == 6)
    for extra, label in (("[filing.iso]\nseed_hours = 2\n", "a move can't seed"),
                         ("[filing.other]\nmode = \"link\"\n", "unknown mode refused"),
                         ("[filing]\nstall_fraction = 1.5\n", "stall_fraction out of range")):
        try:
            load(extra)
            check(label, False)
        except cfgmod.ConfigError:
            check(label, True)


def test_upgrade_and_refile(work):
    print("\nschema upgrade and --refile")
    old = work / "old.db"
    conn = sqlite3.connect(old)
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (1);
        CREATE TABLE torrents (infohash TEXT PRIMARY KEY, name TEXT NOT NULL,
            source_file TEXT NOT NULL, state TEXT NOT NULL,
            force_download INTEGER NOT NULL DEFAULT 0, force_dup INTEGER NOT NULL DEFAULT 0,
            size_bytes INTEGER, file_count INTEGER, transmission_id INTEGER, temp_path TEXT,
            target_path TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            stalled_since TEXT, retry_after TEXT, added_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, filed_at TEXT);
        INSERT INTO torrents (infohash, name, source_file, state, added_at, updated_at)
            VALUES ('aa', 'Old.Row', 'x', 'filed', '2026-01-01', '2026-01-01');
    """)
    conn.commit()
    conn.close()
    conn = db.connect(old)
    row = db.get(conn, "aa")
    check("version-1 database upgraded in place, rows kept",
          row["name"] == "Old.Row" and "category" in row.keys() and "seed_mark_at" in row.keys()
          and conn.execute("SELECT version FROM schema_version").fetchone()[0] == 2)
    conn.close()

    from torrentfiler.__main__ import main

    svc, cfg = build_service(work / "refile")
    raw, ih = make_torrent("Reviewed.Distro")
    saved = cfg.paths.folder(cfgmod.FAILED) / f"{ih}.torrent"
    saved.write_bytes(raw)
    (saved.parent / (saved.name + ".error.txt")).write_text("NEEDS REVIEW: x")
    db.insert(svc.conn, infohash=ih, name="Reviewed.Distro", source_file=str(saved),
              size_bytes=1, file_count=1, force_download=False, force_dup=False,
              state=db.NEEDS_REVIEW)
    toml = work / "refile" / "c.toml"
    toml.write_text(f"""
[paths]
watch_root    = '{cfg.paths.watch_root}'
temp_download = '{cfg.paths.temp_download}'
library_root  = '{cfg.paths.library_root}'
database      = '{cfg.paths.database}'
[transmission]
username = "u"
password = "p"
""", encoding="utf-8")
    check("--refile with no match is an error",
          main(["--config", str(toml), "--refile", "nothing-like-this"]) == 1)
    check("--refile by name re-queues into force/",
          main(["--config", str(toml), "--refile", "distro"]) == 0
          and (cfg.paths.folder(cfgmod.FORCE) / saved.name).is_file() and not saved.exists()
          and not (saved.parent / (saved.name + ".error.txt")).exists())
    svc.client.next_files = stage(cfg, {"reviewed.iso": b"r" * 20})
    svc.tick()
    svc.client.torrents[db.get(svc.conn, ih)["transmission_id"]].percent_done = 1.0
    svc.tick()
    check("re-queued torrent goes through the new rules", state(svc, ih) == db.PURGED
          and (cfg.root_for("iso") / "reviewed/reviewed.iso").is_file(), state(svc, ih))
    svc.conn.close()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        test_classify()
        test_music(work)
        test_iso(work)
        test_other(work)
        test_stall(work)
        test_config(work)
        test_upgrade_and_refile(work)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
