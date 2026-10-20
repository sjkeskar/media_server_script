"""REST API for adding torrents from other devices on the home network.

Runs as its own service (--serve-api), separate from the download pipeline.
It only validates what it receives and writes it into inbox/ or force/ as a
.magnet or .torrent file. From there the main service handles it exactly
like a file dropped by hand. Dedup, filing and cleanup are untouched.

It never writes to the database; a read-only lookup lets it answer
"already known" straight away. Every request is recorded in the audit log
under the device whose token was used.

    POST /api/magnet     JSON {"link": "...", "force": false}, or the link as plain text
    POST /api/torrent    raw .torrent bytes
    GET  /api/status     every torrent by state (?all=1 adds finished ones)
    GET  /api/health     reachability check, no token
    GET  /               the add and status page for browsers

Add ?force=1 to queue into force/, and ?format=text for a one-line plain-text
reply (for Android HTTP Shortcuts toasts).

Two listeners, sharing tokens, audit log, lockout and status cache:

  lan      [api] host:port, e.g. 192.168.7.5:8765. Home network. Device
           token required. Bound to the LAN address (not 0.0.0.0) so the
           Tailscale interface can't reach it and skip the identity check.
  tailnet  127.0.0.1:[api] tailnet_port. Reachable only from this PC, i.e.
           through `tailscale serve`, which terminates HTTPS and adds the
           caller's verified Tailscale identity. Requires that identity AND
           a device token. Requests without it -- e.g. via Tailscale Funnel
           from the public internet -- are refused.

On both, repeated failed authentications from one address lock it out.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import config as cfgmod
from . import db, magnet
from .audit import AuditLog
from .config import Config
from .devices import Registry
from .torrentfile import BencodeError, parse_torrent

log = logging.getLogger(__name__)

MAX_TEXT_BYTES = 64 * 1024
STATUS_CACHE_SECONDS = 5      # several devices polling share one Transmission query
STATUS_AUDIT_SECONDS = 600    # a polling page is audited once per device per 10 min
PHONE_TEXT_WIDTH = 46         # ?format=text status fits a phone dialog
_MAGNET_IN_TEXT = re.compile(r"magnet:\?[^\s\"'<>]+", re.IGNORECASE)
_TRUE = {"1", "true", "yes", "on"}


class _Reply(Exception):
    """Ends a request early with a status and a result."""

    def __init__(self, status: HTTPStatus, result: str, message: str, **extra) -> None:
        super().__init__(message)
        self.status, self.result, self.message, self.extra = status, result, message, extra


class Lockout:
    """Blocks an address after repeated failed authentications.

    `failures` failures within `window` seconds block the address for
    `duration` seconds. A success clears its failures. Thread-safe.
    """

    def __init__(self, failures: int, window: float, duration: float, clock=time.monotonic):
        self.failures, self.window, self.duration, self._clock = failures, window, duration, clock
        self._lock = threading.Lock()
        self._seen: dict[str, list[float]] = {}
        self._blocked: dict[str, float] = {}

    def blocked_for(self, key: str) -> float:
        """Seconds left on a block, or 0."""
        with self._lock:
            until = self._blocked.get(key)
            if until is None:
                return 0.0
            left = until - self._clock()
            if left <= 0:
                del self._blocked[key]
                return 0.0
            return left

    def fail(self, key: str) -> bool:
        """Record a failure. True if this one triggered a block."""
        with self._lock:
            now = self._clock()
            recent = [t for t in self._seen.get(key, []) if now - t < self.window]
            recent.append(now)
            if len(recent) >= self.failures:
                self._blocked[key] = now + self.duration
                self._seen.pop(key, None)
                return True
            self._seen[key] = recent
            # Keep memory bounded if many addresses probe.
            if len(self._seen) > 10_000:
                self._seen = {k: v for k, v in self._seen.items() if now - v[-1] < self.window}
            return False

    def succeed(self, key: str) -> None:
        with self._lock:
            self._seen.pop(key, None)


class ApiState:
    """Shared by both listeners, so a revoke, a lockout or the audit log
    covers every way in."""

    def __init__(self, cfg: Config) -> None:
        a = cfg.api
        self.registry = Registry(cfg.devices_file)
        self.audit = AuditLog(cfg.audit_log)
        self.lockout = Lockout(a.lockout_failures, a.lockout_window_minutes * 60,
                               a.lockout_minutes * 60)
        self.transmission = None  # created on first status request; tests swap a fake in
        self.status_lock = threading.Lock()
        self.status_cache: dict[bool, tuple[float, dict]] = {}
        self.status_audited: dict[str, float] = {}


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, cfg: Config, address: tuple[str, int] | None = None, *,
                 via: str = "lan", state: ApiState | None = None) -> None:
        self.cfg = cfg
        self.via = via  # "lan" or "tailnet"
        self.state = state or ApiState(cfg)
        self._owns_state = state is None
        self.max_torrent_bytes = int(cfg.api.max_torrent_mb * 1024 * 1024)
        super().__init__(address or (cfg.api.host, cfg.api.port), _Handler)

    registry = property(lambda self: self.state.registry)
    audit = property(lambda self: self.state.audit)
    lockout = property(lambda self: self.state.lockout)

    @property
    def transmission(self):
        return self.state.transmission

    @transmission.setter
    def transmission(self, client) -> None:
        self.state.transmission = client

    def status(self, show_all: bool) -> dict:
        """A status snapshot, shared by every device for a few seconds."""
        from . import status
        from .client import TransmissionClient

        st = self.state
        with st.status_lock:
            cached = st.status_cache.get(show_all)
            if cached and time.monotonic() - cached[0] < STATUS_CACHE_SECONDS:
                return cached[1]
            if st.transmission is None:
                st.transmission = TransmissionClient(self.cfg.transmission)
            snap = status.snapshot(self.cfg, show_all=show_all, client=st.transmission)
            st.status_cache[show_all] = (time.monotonic(), snap)
            return snap

    def should_audit_status(self, device: str) -> bool:
        st = self.state
        with st.status_lock:
            last = st.status_audited.get(device)
            if last is not None and time.monotonic() - last < STATUS_AUDIT_SECONDS:
                return False
            st.status_audited[device] = time.monotonic()
            return True

    def server_close(self) -> None:
        super().server_close()
        if self._owns_state:
            self.audit.close()


class _Handler(BaseHTTPRequestHandler):
    server: ApiServer
    server_version = "torrentfiler"
    sys_version = ""
    timeout = 30  # a stalled client can't hold a thread forever

    # -- routing ------------------------------------------------------

    def _origin(self) -> dict:
        """Who is asking: route, client address and (Tailscale) user.

        Identity headers are trusted only on the tailnet listener, which is
        bound to 127.0.0.1: there, the peer is `tailscale serve`, which sets
        X-Forwarded-For and Tailscale-User-Login from the verified WireGuard
        identity. On the LAN listener those headers are client-supplied and
        ignored.
        """
        peer = self.client_address[0]
        if self.server.via == "tailnet" and peer in ("127.0.0.1", "::1"):
            forwarded = self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            user = self.headers.get("Tailscale-User-Login", "").strip() or None
            return {"via": "tailnet", "ip": forwarded or peer, "tailnet_user": user}
        return {"via": self.server.via, "ip": peer, "tailnet_user": None}

    def _gate(self, origin: dict) -> None:
        """Checks before any token is looked at: lockout, then identity."""
        left = self.server.lockout.blocked_for(origin["ip"])
        if left:
            self.close_connection = True
            raise _Reply(HTTPStatus.TOO_MANY_REQUESTS, "blocked",
                         f"too many failed attempts; try again in {int(left // 60) + 1} min",
                         retry_after=int(left) + 1)
        if (origin["via"] == "tailnet" and self.server.cfg.api.require_tailnet_identity
                and not origin["tailnet_user"]):
            self.close_connection = True
            raise _Reply(HTTPStatus.FORBIDDEN, "forbidden",
                         "not from a signed-in device on your Tailscale network")

    def _authenticate_from(self, origin: dict) -> str:
        """Token check that feeds the lockout."""
        try:
            device = self._authenticate()
        except _Reply:
            if self.server.lockout.fail(origin["ip"]):
                log.warning("locked out %s after repeated failed authentications",
                            origin["ip"])
            raise
        self.server.lockout.succeed(origin["ip"])
        return device

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/api/health":
            self._send(HTTPStatus.OK, {"ok": True})
        elif path == "/api/status":
            self._status(parse_qs(parts.query))
        elif path in ("/", "/index.html"):
            try:
                self._gate(self._origin())
            except _Reply as reply:
                self._send_bytes(reply.status, f"{reply.message}\n".encode(),
                                 "text/plain; charset=utf-8")
                return
            self._send_bytes(HTTPStatus.OK, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._send(HTTPStatus.NOT_FOUND, {"result": "rejected", "error": "not found"})

    def do_POST(self) -> None:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        action = {"/api/magnet": "magnet", "/api/torrent": "torrent"}.get(parts.path)
        as_text = query.get("format", [""])[0] == "text"
        if action is None:
            self._send(HTTPStatus.NOT_FOUND, {"result": "rejected", "error": "not found"})
            return

        entry = {"device": None, **self._origin(), "action": action,
                 "force": query.get("force", [""])[0].lower() in _TRUE}
        retry_after = None
        try:
            self._gate(entry)
            entry["device"] = self._authenticate_from(entry)
            body = self._body(action)
            if action == "magnet":
                outcome = self._add_magnet(body, entry)
            else:
                outcome = self._add_torrent(body, entry)
            status = HTTPStatus.OK
        except _Reply as reply:
            status, outcome = reply.status, {"result": reply.result, "error": reply.message,
                                             **reply.extra}
            retry_after = outcome.pop("retry_after", None)
        except Exception as exc:  # never let one request take the server down
            log.exception("unhandled error in %s request", action)
            status, outcome = HTTPStatus.INTERNAL_SERVER_ERROR, {
                "result": "error", "error": f"server error: {exc}"}

        # A revoked token still names its device, so the attempt is attributed.
        entry["device"] = entry["device"] or outcome.get("device")
        if entry["device"]:
            outcome.setdefault("device", entry["device"])
        self.server.audit.write(**entry, result=outcome["result"],
                                infohash=outcome.get("infohash"), name=outcome.get("name"),
                                state=outcome.get("state"), error=outcome.get("error"))
        extra = {"Retry-After": str(retry_after)} if status == HTTPStatus.TOO_MANY_REQUESTS \
            and retry_after else None
        if as_text:
            self._send_bytes(status, (_message(outcome) + "\n").encode("utf-8"),
                             "text/plain; charset=utf-8", extra)
        else:
            self._send(status, {**outcome, "message": _message(outcome)}, extra)

    def _status(self, query: dict) -> None:
        as_text = query.get("format", [""])[0] == "text"
        show_all = query.get("all", [""])[0].lower() in _TRUE
        entry = {"device": None, **self._origin(), "action": "status"}
        try:
            self._gate(entry)
            entry["device"] = self._authenticate_from(entry)
        except _Reply as reply:
            entry["device"] = reply.extra.get("device")
            self.server.audit.write(**entry, result=reply.result, error=reply.message)
            outcome = {"result": reply.result, "error": reply.message}
            extra = ({"Retry-After": str(reply.extra["retry_after"])}
                     if "retry_after" in reply.extra else None)
            if as_text:
                self._send_bytes(reply.status, f"Not allowed: {reply.message}\n".encode(),
                                 "text/plain; charset=utf-8", extra)
            else:
                self._send(reply.status, {**outcome, "message": _message(outcome)}, extra)
            return

        try:
            snap = self.server.status(show_all)
        except Exception as exc:
            log.exception("status request failed")
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                       {"result": "error", "error": f"server error: {exc}"})
            return
        if self.server.should_audit_status(entry["device"]):
            self.server.audit.write(**entry, result="ok")

        if as_text:
            from . import status

            text = status.format_text(snap, width=PHONE_TEXT_WIDTH,
                                      more_hint="add ?all=1 to list")
            self._send_bytes(HTTPStatus.OK, (text + "\n").encode("utf-8"),
                             "text/plain; charset=utf-8")
        else:
            self._send(HTTPStatus.OK, {"result": "ok", "device": entry["device"], **snap})

    # -- steps --------------------------------------------------------

    def _authenticate(self) -> str:
        header = self.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else None
        status, device = self.server.registry.verify(token)
        if status == "ok":
            return device
        self.close_connection = True  # don't read whatever body follows
        if status == "revoked":
            raise _Reply(HTTPStatus.UNAUTHORIZED, "revoked",
                         f"the token for {device} has been revoked", device=device)
        raise _Reply(HTTPStatus.UNAUTHORIZED, "unauthorized",
                     "missing token" if not token else "unknown token")

    def _body(self, action: str) -> bytes:
        limit = self.server.max_torrent_bytes if action == "torrent" else MAX_TEXT_BYTES
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            raise _Reply(HTTPStatus.BAD_REQUEST, "rejected", "bad Content-Length")
        if length > limit:
            self.close_connection = True
            raise _Reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large",
                         f"body is {length} bytes; the limit is {limit}")
        return self.rfile.read(length)

    def _add_magnet(self, body: bytes, entry: dict) -> dict:
        text = body.decode("utf-8", errors="replace")
        if "json" in self.headers.get("Content-Type", ""):
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise _Reply(HTTPStatus.BAD_REQUEST, "rejected", f"invalid JSON: {exc}")
            if not isinstance(data, dict):
                raise _Reply(HTTPStatus.BAD_REQUEST, "rejected", 'expected {"link": "..."}')
            text = str(data.get("link", ""))
            if data.get("force") is True:
                entry["force"] = True
        # Shared text from a phone often has words around the link.
        found = _MAGNET_IN_TEXT.search(text)
        try:
            link = magnet.parse(found.group(0) if found else text)
        except magnet.MagnetError as exc:
            raise _Reply(HTTPStatus.BAD_REQUEST, "rejected", str(exc))

        outcome = {"infohash": link.infohash, "name": link.name}
        known = self._known(link.infohash, entry["force"])
        if known:
            return {"result": "known", **outcome, **known}
        magnet.write_drop_file(link.uri, self._folder(entry["force"]))
        return {"result": "queued", **outcome}

    def _add_torrent(self, body: bytes, entry: dict) -> dict:
        try:
            meta = parse_torrent(body)
        except BencodeError as exc:
            raise _Reply(HTTPStatus.BAD_REQUEST, "rejected", f"not a usable .torrent: {exc}")
        outcome = {"infohash": meta.infohash, "name": meta.name}
        known = self._known(meta.infohash, entry["force"])
        if known:
            return {"result": "known", **outcome, **known}
        magnet.write_atomic(self._folder(entry["force"]),
                            magnet.drop_name(meta.name, meta.short, ".torrent"), body)
        return {"result": "queued", **outcome}

    def _known(self, infohash: str, force: bool) -> dict | None:
        """Mirrors the main service's intake: anything on record is skipped
        unless forced, except a failed torrent, which re-dropping retries."""
        if force:
            return None
        conn = db.connect_readonly(self.server.cfg.paths.database)
        if conn is None:
            return None
        try:
            row = db.get(conn, infohash)
        finally:
            conn.close()
        if row is None or row["state"] == db.FAILED:
            return None
        return {"state": row["state"], "name": row["name"]}

    def _folder(self, force: bool) -> Path:
        return self.server.cfg.paths.folder(cfgmod.FORCE if force else cfgmod.INBOX)

    # -- responses ----------------------------------------------------

    def _send(self, status: HTTPStatus, payload: dict, extra: dict | None = None) -> None:
        self._send_bytes(status, json.dumps(payload).encode("utf-8"), "application/json",
                         extra)

    def _send_bytes(self, status: HTTPStatus, data: bytes, content_type: str,
                    extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        if content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; frame-ancestors 'none'; "
                             "base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s %s", self.client_address[0], fmt % args)


_STATE_WORDS = {
    db.FILED: "in the library", db.PURGED: "in the library",
    db.DOWNLOADING: "downloading", db.COMPLETE: "being filed", db.FILING: "being filed",
    db.QUEUED: "queued", db.DISCOVERED: "queued",
    db.NEEDS_REVIEW: "waiting for review on the PC", db.SKIPPED: "skipped",
}


def _message(outcome: dict) -> str:
    """One line a person can read, e.g. in an Android toast."""
    result, name = outcome.get("result"), outcome.get("name") or ""
    if result == "queued":
        return f"Queued: {name}"
    if result == "known":
        state = outcome.get("state")
        return f"Already {_STATE_WORDS.get(state, state)}: {name}"
    return f"Not added: {outcome.get('error')}"


def serve(cfg: Config, stop: threading.Event) -> None:
    """Serve every configured listener until `stop` is set (by the service's
    signal handler)."""
    state = ApiState(cfg)
    servers: list[ApiServer] = []
    try:
        if cfg.api.host:
            if cfg.api.host == "0.0.0.0" and cfg.api.tailnet_port:
                log.warning("[api] host is 0.0.0.0: Tailscale peers can reach the LAN "
                            "listener directly and skip the identity check. Set host to "
                            "this PC's LAN address, e.g. 192.168.7.5")
            servers.append(ApiServer(cfg, (cfg.api.host, cfg.api.port), via="lan", state=state))
            log.info("API (home network) on http://%s:%d/", cfg.api.host, cfg.api.port)
        if cfg.api.tailnet_port:
            servers.append(ApiServer(cfg, ("127.0.0.1", cfg.api.tailnet_port), via="tailnet",
                                     state=state))
            log.info("API (Tailscale, via `tailscale serve`) on http://127.0.0.1:%d/, "
                     "identity %s", cfg.api.tailnet_port,
                     "required" if cfg.api.require_tailnet_identity else "NOT required")
        if not servers:
            raise OSError("no listener configured: set [api] host and/or tailnet_port")
        log.info("devices: %s, audit: %s", cfg.devices_file, cfg.audit_log)
        for server in servers:
            threading.Thread(target=server.serve_forever, name=f"api-{server.via}",
                             daemon=True).start()
        # A bare wait() can't be interrupted by Ctrl+C on Windows, which is
        # how NSSM stops a service; waking every second lets the signal land.
        while not stop.wait(1.0):
            pass
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        state.audit.close()
        log.info("API stopped")


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Add torrent</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --text:#1c1f24; --muted:#5d6570; --line:#d9dde3;
          --accent:#2f6fde; --ok:#1f7a3a; --warn:#8a5a00; --bad:#b3261e; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#14171b; --card:#1d2126; --text:#e6e8eb; --muted:#9aa3ad; --line:#333a42;
            --accent:#7aa7ff; --ok:#6fcf8b; --warn:#e5b45a; --bad:#ff8a80; } }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:16px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  main { max-width:560px; margin:0 auto; padding:20px 16px 40px; }
  h1 { font-size:1.35rem; margin:0 0 4px; }
  .sub { color:var(--muted); margin:0 0 18px; font-size:.9rem; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:16px; margin-bottom:14px; }
  label { display:block; font-weight:600; margin-bottom:6px; }
  textarea, input[type=password] { width:100%; font:inherit; color:inherit; background:transparent;
          border:1px solid var(--line); border-radius:8px; padding:10px; }
  textarea { min-height:96px; resize:vertical; word-break:break-all; }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:10px; }
  button { font:inherit; font-weight:600; border:0; border-radius:8px; padding:10px 18px;
           background:var(--accent); color:#fff; cursor:pointer; }
  button.plain { background:transparent; color:var(--accent); padding:4px 0; font-weight:500; }
  .drop { border:2px dashed var(--line); border-radius:10px; padding:18px; text-align:center;
          color:var(--muted); cursor:pointer; }
  .drop.over { border-color:var(--accent); color:var(--accent); }
  ul { list-style:none; padding:0; margin:0; }
  li { padding:8px 0; border-top:1px solid var(--line); word-break:break-word; }
  li:first-child { border-top:0; }
  .queued { color:var(--ok); } .known { color:var(--warn); } .rejected { color:var(--bad); }
  .hidden { display:none; }
  .who { color:var(--muted); font-size:.85rem; }
  .tabs { display:flex; gap:6px; margin-bottom:14px; }
  .tabs button { flex:1; background:transparent; color:var(--text); border:1px solid var(--line); }
  .tabs button.on { background:var(--accent); color:#fff; border-color:var(--accent); }
  .sec h2 { font-size:1rem; margin:0 0 6px; }
  .item { padding:8px 0; border-top:1px solid var(--line); }
  .item:first-of-type { border-top:0; }
  .nm { font-weight:600; word-break:break-word; }
  .sm { font-size:.88rem; color:var(--muted); word-break:break-word; }
  .er { font-size:.88rem; color:var(--bad); word-break:break-word; }
  .bar { height:6px; background:var(--line); border-radius:3px; margin-top:6px; overflow:hidden; }
  .bar > i { display:block; height:100%; background:var(--accent); }
  .head { display:flex; justify-content:space-between; align-items:center; gap:8px;
          margin-bottom:10px; flex-wrap:wrap; }
</style>
</head>
<body>
<main>
  <h1>Add torrent</h1>
  <p class="sub">Queues on the home PC. It downloads, then appears in Jellyfin.</p>

  <section id="setup" class="card hidden">
    <label for="token">This device's token</label>
    <input id="token" type="password" autocomplete="off" placeholder="paste the token for this device">
    <div class="row"><button id="save">Save</button>
      <span class="who">Issued on the PC with --api-add-device. Stored only in this browser.</span></div>
  </section>

  <div id="app" class="hidden">
    <nav class="tabs"><button id="t-add" class="on">Add</button><button id="t-status">Status</button></nav>
    <div id="tab-status" class="hidden">
      <div class="head"><span id="st-head" class="who">Loading...</span>
        <span><label class="who" style="display:inline;font-weight:400"><input id="st-all" type="checkbox"> finished too</label>
        <button id="st-refresh" class="plain">Refresh</button></span></div>
      <div id="st-body"></div>
    </div>
    <div id="tab-add">
    <section class="card">
      <label for="link">Magnet link</label>
      <textarea id="link" placeholder="magnet:?xt=urn:btih:..."></textarea>
      <div class="row"><button id="add">Add</button>
        <label style="font-weight:400;margin:0"><input id="force" type="checkbox"> Force (add even if seen before)</label></div>
    </section>
    <section class="card">
      <label>.torrent files</label>
      <div id="drop" class="drop">Tap to choose, or drop files here</div>
      <input id="file" type="file" accept=".torrent,application/x-bittorrent" multiple class="hidden">
    </section>
    <section class="card"><label>Results</label><ul id="results"><li class="who">Nothing added yet.</li></ul></section>
    </div>
    <button id="forget" class="plain">Forget this device's token</button>
  </div>
</main>
<script>
const $ = id => document.getElementById(id);
const KEY = "torrentfiler-token";
const store = {
  get() { try { return localStorage.getItem(KEY); } catch (e) { return null; } },
  set(v) { try { localStorage.setItem(KEY, v); } catch (e) {} },
  clear() { try { localStorage.removeItem(KEY); } catch (e) {} },
};
let token = store.get();

function show() {
  $("setup").classList.toggle("hidden", !!token);
  $("app").classList.toggle("hidden", !token);
}

function report(outcome) {
  const list = $("results");
  if (list.firstElementChild && list.firstElementChild.classList.contains("who")) list.innerHTML = "";
  const li = document.createElement("li");
  li.className = outcome.result === "queued" ? "queued" : outcome.result === "known" ? "known" : "rejected";
  li.textContent = outcome.message || outcome.error || "Unexpected reply";
  list.prepend(li);
  if (outcome.result === "unauthorized" || outcome.result === "revoked") { token = null; store.clear(); show(); }
}

async function send(path, body, type) {
  const force = $("force").checked ? "?force=1" : "";
  try {
    const r = await fetch(path + force, { method: "POST", body,
      headers: { "Authorization": "Bearer " + token, "Content-Type": type } });
    report(await r.json());
  } catch (e) { report({ result: "rejected", message: "Could not reach the PC: " + e.message }); }
}

$("save").onclick = () => { const v = $("token").value.trim(); if (v) { token = v; store.set(v); $("token").value = ""; show(); tab("add"); } };
$("forget").onclick = () => { token = null; store.clear(); show(); };
$("add").onclick = () => {
  const text = $("link").value.trim();
  if (!text) return;
  send("/api/magnet", JSON.stringify({ link: text }), "application/json");
  $("link").value = "";
};
const files = list => { for (const f of list) send("/api/torrent", f, "application/x-bittorrent"); };
$("drop").onclick = () => $("file").click();
$("file").onchange = e => { files(e.target.files); e.target.value = ""; };
["dragenter", "dragover"].forEach(t => $("drop").addEventListener(t, e => { e.preventDefault(); $("drop").classList.add("over"); }));
["dragleave", "drop"].forEach(t => $("drop").addEventListener(t, e => { e.preventDefault(); $("drop").classList.remove("over"); }));
$("drop").addEventListener("drop", e => files(e.dataTransfer.files));

// -- status tab: refreshed every 15 s while visible --
const TAB = "torrentfiler-tab";
let timer = null;
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function size(n) { if (!n) return ""; const u = ["B","KB","MB","GB","TB"]; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return (i ? n.toFixed(1) : n) + " " + u[i]; }

async function loadStatus() {
  if (!token) return;
  try {
    const all = $("st-all").checked ? "?all=1" : "";
    const r = await fetch("/api/status" + all, { headers: { "Authorization": "Bearer " + token }, cache: "no-store" });
    const s = await r.json();
    if (s.result === "unauthorized" || s.result === "revoked") { report(s); return; }
    if (s.result !== "ok") throw new Error(s.error || "unexpected reply");
    const parts = Object.entries(s.counts).map(([k, v]) => v + " " + k);
    $("st-head").textContent = s.total ? s.total + " torrent(s): " + parts.join(", ") + " · " +
      new Date(s.generated).toLocaleTimeString() : "No torrents on record yet.";
    const body = $("st-body"); body.innerHTML = "";
    if (s.transmission && !s.transmission.startsWith("live")) body.append(el("p", "who", s.transmission));
    for (const sec of s.sections) {
      const card = el("section", "card sec"); card.append(el("h2", null, sec.title + " (" + sec.items.length + ")"));
      for (const it of sec.items) {
        const d = el("div", "item");
        d.append(el("div", "nm", it.name));
        d.append(el("div", "sm", [size(it.size_bytes), it.summary].filter(Boolean).join("  ·  ")));
        if (it.progress != null) { const b = el("div", "bar"), f = el("i"); f.style.width = Math.round(it.progress * 100) + "%"; b.append(f); d.append(b); }
        if (it.target_path) d.append(el("div", "sm", "→ " + it.target_path));
        if (it.error) d.append(el("div", "er", it.error));
        card.append(d);
      }
      body.append(card);
    }
    const hidden = Object.entries(s.hidden || {}).map(([k, v]) => v + " " + k);
    if (hidden.length) body.append(el("p", "who", "Also on record: " + hidden.join(", ") + " (tick “finished too”)"));
  } catch (e) { $("st-head").textContent = "Could not load status: " + e.message; }
}

function tab(name) {
  $("t-add").classList.toggle("on", name === "add");
  $("t-status").classList.toggle("on", name === "status");
  $("tab-add").classList.toggle("hidden", name !== "add");
  $("tab-status").classList.toggle("hidden", name !== "status");
  try { localStorage.setItem(TAB, name); } catch (e) {}
  clearInterval(timer); timer = null;
  if (name === "status") { loadStatus(); timer = setInterval(() => { if (!document.hidden) loadStatus(); }, 15000); }
}
$("t-add").onclick = () => tab("add");
$("t-status").onclick = () => tab("status");
$("st-refresh").onclick = loadStatus;
$("st-all").onchange = loadStatus;
document.addEventListener("visibilitychange", () => { if (!document.hidden && !$("tab-status").classList.contains("hidden")) loadStatus(); });
show();
let start = "add"; try { start = localStorage.getItem(TAB) || "add"; } catch (e) {}
if (token) tab(start);
</script>
</body>
</html>
"""
