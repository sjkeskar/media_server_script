"""REST API tests: a real server on a random local port, temporary folders
and a real database. No Transmission needed."""

import dataclasses
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_phase1 import build_service, make_torrent
from torrentfiler import audit, db, devices
from torrentfiler import config as cfgmod
from torrentfiler.api import ApiServer

PASSED = []
FAILED = []
HEX = "c9e15763f722f23e98a29decdfae341b98d53056"
LINK = f"magnet:?xt=urn:btih:{HEX}&dn=Some.Show.S01E01"


def check(label, condition, detail=""):
    (PASSED if condition else FAILED).append(label)
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}{(' -- ' + detail) if detail else ''}")


class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def call(self, method, path, body=None, token=None, ctype=None):
        req = urllib.request.Request(self.base + path, data=body, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                status, raw, kind = r.status, r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            status, raw, kind = e.code, e.read(), e.headers.get("Content-Type", "")
        except (ConnectionError, urllib.error.URLError) as e:
            return None, str(e)
        return status, (json.loads(raw) if "json" in kind else raw.decode())

    def magnet(self, link, token, query=""):
        return self.call("POST", "/api/magnet" + query, json.dumps({"link": link}).encode(),
                         token, "application/json")

    def torrent(self, data, token, query=""):
        return self.call("POST", "/api/torrent" + query, data, token, "application/x-bittorrent")


def start(cfg):
    server = ApiServer(cfg, ("127.0.0.1", 0))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, Client(server.server_address[1])


def files(cfg, folder, pattern="*"):
    return [p for p in cfg.paths.folder(folder).glob(pattern) if p.is_file()]


def audit_lines(cfg):
    return list(audit.entries(cfg.audit_log))


def test_api(work: Path):
    svc, base_cfg = build_service(work / "api")
    cfg = dataclasses.replace(base_cfg, api=dataclasses.replace(
        base_cfg.api, devices_file=work / "api" / "devices.json",
        audit_log=work / "api" / "audit.log"))
    phone = devices.add(cfg.devices_file, "phone-sj")
    tab = devices.add(cfg.devices_file, "tab-livingroom")
    server, c = start(cfg)

    print("\nbasics")
    check("health needs no token", c.call("GET", "/api/health") == (200, {"ok": True}))
    status, page = c.call("GET", "/")
    check("add page served", status == 200 and "Add torrent" in page)
    check("unknown path is 404", c.call("GET", "/nope")[0] == 404)

    print("\nauthentication")
    status, body = c.magnet(LINK, None)
    check("missing token -> 401", status == 401 and body["result"] == "unauthorized")
    status, body = c.magnet(LINK, "not-a-real-token")
    check("wrong token -> 401", status == 401 and body["error"] == "unknown token")
    check("nothing written without a valid token", not files(cfg, cfgmod.INBOX))
    last = audit_lines(cfg)[-1]
    check("failed auth audited with IP and no device",
          last["result"] == "unauthorized" and last["device"] is None
          and last["ip"] == "127.0.0.1")

    print("\nmagnets")
    status, body = c.magnet(LINK, phone)
    check("valid magnet queued", status == 200 and body["result"] == "queued", str(body))
    check("response names the device", body.get("device") == "phone-sj")
    check(".magnet written into inbox/", len(files(cfg, cfgmod.INBOX, "*.magnet")) == 1)

    other = "magnet:?xt=urn:btih:" + "ab" * 20 + "&dn=Shared.Text"
    status, text = c.call("POST", "/api/magnet?format=text",
                          f"Look at this {other} cool".encode(), tab, "text/plain")
    check("plain text with words around the link, text reply",
          status == 200 and text.strip() == "Queued: Shared.Text", repr(text))

    c.magnet("magnet:?xt=urn:btih:" + "cd" * 20 + "&dn=Forced.Q", phone, "?force=1")
    c.call("POST", "/api/magnet", json.dumps({"link": "magnet:?xt=urn:btih:" + "ef" * 20,
                                              "force": True}).encode(), phone,
           "application/json")
    check("?force=1 and JSON force both go to force/",
          len(files(cfg, cfgmod.FORCE, "*.magnet")) == 2)

    status, body = c.magnet("magnet:?dn=no-hash", phone)
    check("invalid magnet -> 400, explained", status == 400 and body["result"] == "rejected"
          and "infohash" in body["error"])

    hostile = "magnet:?xt=urn:btih:" + "12" * 20 + "&dn=..%5C..%5C..%5Cevil%2Fx"
    c.magnet(hostile, phone)
    names = [p.name for p in cfg.paths.watch_root.rglob("*evil*")]
    check("hostile name can't escape inbox/",
          len(names) == 1 and (cfg.paths.folder(cfgmod.INBOX) / names[0]).is_file()
          and "\\" not in names[0] and "/" not in names[0], str(names))

    print("\n.torrent uploads")
    raw, ih = make_torrent("Uploaded.Film.2024")
    status, body = c.torrent(raw, tab)
    written = files(cfg, cfgmod.INBOX, "*.torrent")
    check("valid .torrent queued", status == 200 and body["infohash"] == ih, str(body))
    check("written whole, no temp file", len(written) == 1 and written[0].read_bytes() == raw
          and not list(cfg.paths.watch_root.rglob("*.tmp")))
    status, body = c.torrent(b"this is not bencode", tab)
    check("corrupt .torrent -> 400", status == 400 and body["result"] == "rejected")

    small = dataclasses.replace(cfg, api=dataclasses.replace(cfg.api, max_torrent_mb=0.0001))
    small_server, small_c = start(small)
    status, body = small_c.torrent(b"x" * 1000, phone)
    check("oversized upload -> 413", status == 413 and body["result"] == "too_large")
    small_server.shutdown()
    small_server.server_close()

    print("\nknown torrents")
    raw2, ih2 = make_torrent("Already.Here")
    db.insert(svc.conn, infohash=ih2, name="Already.Here", source_file="x", size_bytes=1,
              file_count=1, force_download=False, force_dup=False, state=db.FILED)
    before = len(files(cfg, cfgmod.INBOX))
    status, body = c.torrent(raw2, phone)
    check("known torrent reported with its state",
          body["result"] == "known" and body["state"] == db.FILED, str(body))
    check("known torrent not written", len(files(cfg, cfgmod.INBOX)) == before)
    c.torrent(raw2, phone, "?force=1")
    check("known torrent with force is written",
          any(ih2[:8] in p.name for p in files(cfg, cfgmod.FORCE)))
    db.update(svc.conn, ih2, state=db.FAILED)
    status, body = c.torrent(raw2, phone)
    check("failed torrent is queued again (re-drop retries)", body["result"] == "queued")

    print("\ndevices")
    stored = cfg.devices_file.read_text()
    check("token file holds no plaintext tokens", phone not in stored and tab not in stored)
    for name, label in (("phone-sj", "duplicate active name refused"),
                        ("Phone_SJ", "badly formed name refused"),
                        ("tv-lounge", "unknown device kind refused")):
        try:
            devices.add(cfg.devices_file, name)
            check(label, False)
        except devices.DeviceError:
            check(label, True)

    devices.revoke(cfg.devices_file, "phone-sj")
    status, body = c.magnet(LINK, phone)
    check("revoked token refused without a restart",
          status == 401 and body["result"] == "revoked")
    last = audit_lines(cfg)[-1]
    check("revoked attempt audited under its device",
          last["result"] == "revoked" and last["device"] == "phone-sj")
    status, body = c.magnet("magnet:?xt=urn:btih:" + "34" * 20, tab)
    check("other devices unaffected by a revoke", status == 200 and body["device"] == "tab-livingroom")
    new_phone = devices.add(cfg.devices_file, "phone-sj")
    check("re-adding a revoked name issues a working token",
          c.magnet("magnet:?xt=urn:btih:" + "56" * 20, new_phone)[0] == 200)

    print("\npipeline and reporting")
    svc.client.next_files = ()
    svc.tick()
    row = db.get(svc.conn, HEX)
    # Picked up and claimed; whether it is already downloading depends on
    # max_active (2) and how many other drops were ahead of it.
    check("API-added magnet picked up by the main service",
          row is not None and row["state"] in (db.QUEUED, db.DOWNLOADING)
          and Path(row["source_file"]).parent.name == cfgmod.PROCESSING,
          row["state"] if row else "none")

    raw3, ih3 = make_torrent("Dropped.Locally")
    (cfg.paths.folder(cfgmod.INBOX) / "local.torrent").write_bytes(raw3)
    svc.tick()
    text = audit.report(cfg.audit_log, cfg.paths.database)
    lines = {ln.split()[0]: ln.split()[1:] for ln in text.splitlines()
             if ln and ln.split()[0] in ("phone-sj", "tab-livingroom", "(local)", "(unknown)")}
    check("report has a row per device, (local) and (unknown)",
          set(lines) == {"phone-sj", "tab-livingroom", "(local)", "(unknown)"}, text)
    check("report counts each device's adds",
          lines.get("tab-livingroom", ["?"])[0] == "3", text)
    check("unauthorised attempts counted as rejected under (unknown)",
          lines.get("(unknown)", [0, 0, "?"])[2] == "2", text)
    detail = audit.report(cfg.audit_log, cfg.paths.database, device="phone-sj")
    check("per-device detail lists its adds and the current state",
          "Some.Show.S01E01" in detail and "downloading" in detail and "revoked" in detail)
    usage = audit.device_usage(cfg.audit_log)
    check("device usage from the audit log",
          usage["tab-livingroom"]["adds"] == 3 and usage["tab-livingroom"]["last_ip"] == "127.0.0.1")

    print("\nstatus for devices")
    server.transmission = svc.client  # the fake Transmission
    status, body = c.call("GET", "/api/status")
    check("status needs a token", status == 401 and body["result"] == "unauthorized")
    before = len(audit_lines(cfg))
    status, body = c.call("GET", "/api/status", token=tab)
    keys = [s["key"] for s in body.get("sections", [])]
    check("status JSON lists sections with items",
          status == 200 and body["result"] == "ok" and body["total"] > 0 and keys
          and all(s["items"] for s in body["sections"]), str(keys))
    item = body["sections"][0]["items"][0]
    check("each item has a name, state and one-line summary",
          {"name", "state", "summary", "progress", "error"} <= set(item))
    check("status attributed to the device", body["device"] == "tab-livingroom")
    status, text = c.call("GET", "/api/status?format=text", token=tab)
    lines = text.splitlines()
    check("text status fits a phone screen",
          status == 200 and text.startswith("torrentfiler:")
          and max(len(ln) for ln in lines if not ln.startswith("    -> ")) <= 60,
          str(max(len(ln) for ln in lines)))
    c.call("GET", "/api/status", token=tab)
    c.call("GET", "/api/status", token=tab)
    added = [e for e in audit_lines(cfg)[before:] if e["action"] == "status"]
    check("polling audited once per device, not on every refresh",
          len(added) == 1 and added[0]["device"] == "tab-livingroom", str(len(added)))
    calls = []
    original_get = svc.client.get
    svc.client.get = lambda ids: calls.append(ids) or original_get(ids)
    for _ in range(3):
        c.call("GET", "/api/status", token=tab)
    check("devices polling share one cached Transmission query", len(calls) == 0)
    svc.client.get = original_get
    _, page = c.call("GET", "/")
    check("page has the status tab", 'id="t-status"' in page and "/api/status" in page)

    server.shutdown()
    server.server_close()
    svc.conn.close()


def test_cli(work: Path):
    print("\ncommands")
    from torrentfiler.__main__ import main

    root = work / "cli"
    root.mkdir(parents=True)
    toml = root / "config.toml"
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
    args = ["--config", str(toml)]
    check("--api-add-device succeeds", main(args + ["--api-add-device", "laptop-work"]) == 0)
    check("devices file defaults next to the database", (root / "api_devices.json").is_file())
    check("--api-add-device rejects a bad name", main(args + ["--api-add-device", "Bad Name"]) == 1)
    check("--api-list-devices runs", main(args + ["--api-list-devices"]) == 0)
    check("--api-revoke-device succeeds", main(args + ["--api-revoke-device", "laptop-work"]) == 0)
    check("revoking twice is an error", main(args + ["--api-revoke-device", "laptop-work"]) == 1)
    check("--api-report runs with no activity", main(args + ["--api-report"]) == 0)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        test_api(work)
        test_cli(work)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
