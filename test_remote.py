"""Remote-access protections: the Tailscale listener (identity required,
identity recorded), the home-network listener ignoring spoofed identity
headers, the shared lockout, security headers, and config validation.

Both listeners run for real on random local ports. The Tailscale listener
is fed the headers `tailscale serve` adds, exactly as it would be in use."""

import dataclasses
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_phase1 import build_service
from torrentfiler import audit, devices
from torrentfiler import config as cfgmod
from torrentfiler.api import ApiServer, ApiState, Lockout

PASSED = []
FAILED = []
LINK = "magnet:?xt=urn:btih:{}&dn=Remote.Test"


def check(label, condition, detail=""):
    (PASSED if condition else FAILED).append(label)
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}{(' -- ' + detail) if detail else ''}")


def call(port, method, path, token=None, headers=None, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def add(port, token, n, headers=None):
    body = json.dumps({"link": LINK.format(f"{n:040x}")}).encode()
    status, hdrs, raw = call(port, "POST", "/api/magnet", token, headers, body)
    return status, hdrs, json.loads(raw)


def tailscale(user="sj@example.com", ip="100.101.102.103"):
    """Headers `tailscale serve` adds for a signed-in tailnet user."""
    h = {"X-Forwarded-For": ip}
    if user:
        h["Tailscale-User-Login"] = user
        h["Tailscale-User-Name"] = "SJ"
    return h


def start(cfg, via, state):
    server = ApiServer(cfg, ("127.0.0.1", 0), via=via, state=state)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def test_listeners(work: Path):
    svc, base = build_service(work / "remote")
    cfg = dataclasses.replace(base, api=dataclasses.replace(
        base.api, devices_file=work / "remote/devices.json",
        audit_log=work / "remote/audit.log", lockout_failures=5))
    token = devices.add(cfg.devices_file, "phone-sj")
    state = ApiState(cfg)
    lan, lan_port = start(cfg, "lan", state)
    tail, tail_port = start(cfg, "tailnet", state)
    inbox = cfg.paths.folder(cfgmod.INBOX)
    log = lambda: list(audit.entries(cfg.audit_log))  # noqa: E731

    print("\nTailscale listener")
    status, _, body = add(tail_port, token, 1, tailscale(user=None))
    check("no Tailscale identity -> 403 even with a valid token",
          status == 403 and body["result"] == "forbidden")
    check("nothing written for a request without identity", not list(inbox.glob("*")))
    status, _, page = call(tail_port, "GET", "/")
    check("page refused without identity (e.g. via Funnel)", status == 403)
    status, _, body = add(tail_port, None, 2, tailscale())
    check("identity but no token -> 401", status == 401)
    status, _, body = add(tail_port, token, 3, tailscale())
    check("identity + token -> queued", status == 200 and body["result"] == "queued")
    last = log()[-1]
    check("audit records route, Tailscale user and the device's tailnet IP",
          last["via"] == "tailnet" and last["tailnet_user"] == "sj@example.com"
          and last["ip"] == "100.101.102.103" and last["device"] == "phone-sj", str(last))
    status, _, page = call(tail_port, "GET", "/", headers=tailscale())
    check("page served to a signed-in tailnet user", status == 200 and b"Add torrent" in page)
    status, _, raw = call(tail_port, "GET", "/api/status", token, tailscale())
    check("status available over Tailscale", status == 200 and json.loads(raw)["result"] == "ok")

    print("\nhome-network listener")
    status, _, body = add(lan_port, token, 4, tailscale(user="attacker@evil", ip="100.9.9.9"))
    last = log()[-1]
    check("spoofed Tailscale headers ignored on the LAN listener",
          status == 200 and last["via"] == "lan" and last["tailnet_user"] is None
          and last["ip"] == "127.0.0.1", str(last))
    status, _, body = add(lan_port, None, 5)
    check("LAN still needs a token", status == 401)

    print("\nlockout")
    for _ in range(3):
        add(tail_port, "wrong-token", 6, tailscale(ip="100.1.1.1"))
    add(tail_port, token, 7, tailscale(ip="100.1.1.1"))
    for _ in range(4):
        add(tail_port, "wrong-token", 8, tailscale(ip="100.1.1.1"))
    status, _, _ = add(tail_port, token, 9, tailscale(ip="100.1.1.1"))
    check("a success clears earlier failures (3 + 4 failures, no block)", status == 200)
    for _ in range(5):
        add(tail_port, "wrong-token", 10, tailscale(ip="100.1.1.1"))
    status, hdrs, body = add(tail_port, token, 11, tailscale(ip="100.1.1.1"))
    check("5 failures -> blocked, even with a valid token",
          status == 429 and body["result"] == "blocked")
    check("Retry-After header sent", int(hdrs.get("Retry-After", 0)) > 0)
    check("blocked attempt audited", log()[-1]["result"] == "blocked")
    status, _, _ = add(tail_port, token, 12, tailscale(ip="100.2.2.2"))
    check("other addresses unaffected", status == 200)
    status, _, raw = call(tail_port, "GET", "/api/status", token, tailscale(ip="100.1.1.1"))
    check("lockout covers status too", status == 429)

    for _ in range(5):
        add(lan_port, "wrong-token", 13)
    status, _, _ = add(tail_port, token, 14, {"Tailscale-User-Login": "sj@example.com"})
    check("lockout shared between listeners (same address)", status == 429)

    print("\nresponses and report")
    status, hdrs, _ = call(tail_port, "GET", "/", headers=tailscale(ip="100.3.3.3"))
    csp = hdrs.get("Content-Security-Policy", "")
    check("page can't be framed or leak referrers",
          status == 200 and hdrs.get("X-Frame-Options") == "DENY"
          and "frame-ancestors 'none'" in csp and hdrs.get("Referrer-Policy") == "no-referrer")
    status, _, _ = call(lan_port, "GET", "/")
    check("a locked-out address can't even load the page", status == 429)
    report = audit.report(cfg.audit_log, cfg.paths.database)
    row = next(ln for ln in report.splitlines() if ln.startswith("(unknown)"))
    check("forbidden and blocked attempts counted as rejected", int(row.split()[3]) >= 8, row)

    for s in (lan, tail):
        s.shutdown()
        s.server_close()
    state.audit.close()
    svc.conn.close()


def test_lockout_unit():
    print("\nlockout timing")
    now = [0.0]
    lock = Lockout(failures=3, window=60, duration=900, clock=lambda: now[0])
    lock.fail("a")
    now[0] = 70
    lock.fail("a")
    lock.fail("a")
    check("failures outside the window don't count", lock.blocked_for("a") == 0)
    lock.fail("a")
    check("3 within the window block", lock.blocked_for("a") == 900)
    now[0] += 899
    check("still blocked just before expiry", lock.blocked_for("a") > 0)
    now[0] += 2
    check("block expires", lock.blocked_for("a") == 0)


def test_config(work: Path):
    print("\nconfig")
    root = work / "cfg"
    root.mkdir()
    base = f"""
[paths]
watch_root    = '{root / "w"}'
temp_download = '{root / "t"}'
library_root  = '{root / "l"}'
database      = '{root / "x.db"}'
[transmission]
username = "u"
password = "p"
"""

    def load(extra):
        (root / "c.toml").write_text(base + extra, encoding="utf-8")
        return cfgmod.load(root / "c.toml")

    c = load("")
    check("Tailscale listener off unless configured; identity required by default",
          c.api.tailnet_port == 0 and c.api.require_tailnet_identity)
    c = load('[api]\nhost = "192.168.7.5"\ntailnet_port = 8766\n')
    check("LAN address + Tailscale port accepted",
          c.api.host == "192.168.7.5" and c.api.tailnet_port == 8766)
    for extra, label in (('[api]\ntailnet_port = 80\n', "privileged tailnet port refused"),
                         ('[api]\nhost = "0.0.0.0"\ntailnet_port = 8765\n',
                          "tailnet port clashing with the LAN port refused"),
                         ('[api]\nlockout_failures = 0\n', "lockout can't be disabled by 0")):
        try:
            load(extra)
            check(label, False)
        except cfgmod.ConfigError:
            check(label, True)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        test_listeners(work)
        test_lockout_unit()
        test_config(work)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
