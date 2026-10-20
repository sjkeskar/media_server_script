"""The API's audit log, and the per-device report built from it.

One JSON object per line for every API request, accepted or not: who
(device), from where (IP), what (action, infohash, name), and the outcome.
Only the API writes it; nothing in the download pipeline reads it.

The report joins the log with the torrent database on infohash, so each
device's adds show what actually became of them (filed, failed, ...). The
main service never needs to know which device added what.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from . import db

MAX_BYTES = 5_000_000
BACKUPS = 10

# Result values written by the API.
QUEUED = "queued"
KNOWN = "known"
REJECTED_RESULTS = ("rejected", "unauthorized", "revoked", "too_large", "forbidden", "blocked")


class AuditLog:
    """Thread-safe, size-rotated JSON-lines writer.

    Owns its file handler outright rather than going through a named
    logger: loggers are process-wide, so two instances on one file would
    share one, and closing either would silently drop the other's entries.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
        self._handler.setFormatter(logging.Formatter("%(message)s"))

    def write(self, **entry) -> None:
        record = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
        # handle() takes the handler's lock, so concurrent requests can't
        # interleave lines or race a rotation.
        self._handler.handle(logging.makeLogRecord(
            {"msg": json.dumps(record, ensure_ascii=False), "levelno": logging.INFO,
             "levelname": "INFO"}))

    def close(self) -> None:
        self._handler.close()


def entries(path: Path) -> Iterator[dict]:
    """Every entry, oldest first, across the rotated files."""
    files = [path.with_name(f"{path.name}.{n}") for n in range(BACKUPS, 0, -1)] + [path]
    for file in files:
        if not file.is_file():
            continue
        with file.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue  # a torn line from a crash; skip it


def _since(days: float | None) -> str | None:
    if days is None:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


# Torrent states grouped into the report's columns.
_STATE_COLUMNS = {
    "filed": (db.FILED, db.PURGED),
    "active": (db.DISCOVERED, db.QUEUED, db.DOWNLOADING, db.COMPLETE, db.FILING),
    "review": (db.NEEDS_REVIEW,),
    "failed": (db.FAILED,),
}
_COLUMNS = ("adds", "known", "rejected", "filed", "active", "review", "failed")


def _column(state: str | None) -> str | None:
    for column, states in _STATE_COLUMNS.items():
        if state in states:
            return column
    return None


def device_usage(path: Path) -> dict[str, dict]:
    """Per device: last request time and IP, and number of queued adds."""
    usage: dict[str, dict] = {}
    for e in entries(path):
        name = e.get("device")
        if not name:
            continue
        u = usage.setdefault(name, {"last_used": None, "last_ip": None, "adds": 0})
        u["last_used"], u["last_ip"] = e.get("time"), e.get("ip")
        if e.get("result") == QUEUED:
            u["adds"] += 1
    return usage


def report(path: Path, database: Path, *, days: float | None = None,
           device: str | None = None) -> str:
    since = _since(days)
    everything = list(entries(path))
    log = [e for e in everything if since is None or e.get("time", "") >= since]
    # Any torrent the API ever saw, regardless of --days, is not "(local)".
    via_api = {e["infohash"] for e in everything if e.get("infohash")}
    conn = db.connect_readonly(database)
    try:
        states = _states(conn)
        if device:
            return _device_detail(log, states, device)
        return _summary(log, states, via_api, since)
    finally:
        if conn is not None:
            conn.close()


def _states(conn: sqlite3.Connection | None) -> dict[str, sqlite3.Row]:
    if conn is None:
        return {}
    rows = conn.execute("SELECT infohash, name, state, last_error, added_at FROM torrents")
    return {r["infohash"]: r for r in rows}


def _summary(log: list[dict], states: dict, via_api: set, since: str | None) -> str:
    counts: dict[str, Counter] = defaultdict(Counter)
    credited: dict[str, str] = {}  # infohash -> first device that queued it

    for e in log:
        name = e.get("device") or "(unknown)"
        result = e.get("result")
        if result == QUEUED:
            counts[name]["adds"] += 1
            ih = e.get("infohash")
            if ih and ih not in credited:
                credited[ih] = name
        elif result == KNOWN:
            counts[name]["known"] += 1
        elif result in REJECTED_RESULTS:
            counts[name]["rejected"] += 1

    for ih, name in credited.items():
        row = states.get(ih)
        column = _column(row["state"]) if row else None
        if column:
            counts[name][column] += 1

    # Torrents that never came through the API: dropped on the PC itself.
    for ih, row in states.items():
        if ih in via_api or (since and row["added_at"] < since):
            continue
        counts["(local)"]["adds"] += 1
        column = _column(row["state"])
        if column:
            counts["(local)"][column] += 1

    if not counts:
        return "no API activity or torrents on record"
    order = sorted(n for n in counts if not n.startswith("(")) + \
        [n for n in ("(local)", "(unknown)") if n in counts]
    width = max(16, *(len(n) + 2 for n in order))
    lines = ["device".ljust(width) + "".join(c.rjust(10) for c in _COLUMNS)]
    for name in order:
        lines.append(name.ljust(width) + "".join(str(counts[name][c]).rjust(10)
                                                  for c in _COLUMNS))
    lines.append("")
    lines.append("adds: newly queued   known: already on record   rejected: invalid, locked out, "
                 "too large, bad or revoked token")
    lines.append("filed: in the library (seeding or done)   active: queued to filing   "
                 "review/failed: see --device NAME")
    return "\n".join(lines)


def _device_detail(log: list[dict], states: dict, device: str) -> str:
    rows = [e for e in log if e.get("device") == device]
    if not rows:
        return f"no API activity for {device}"
    lines = [f"{'time':<26}{'via':<9}{'action':<9}{'result':<14}{'now':<14}name"]
    for e in rows:
        row = states.get(e.get("infohash") or "")
        now = row["state"] if row else ""
        name = (row["name"] if row else None) or e.get("name") or ""
        lines.append(f"{e.get('time', ''):<26}{e.get('via') or 'lan':<9}"
                     f"{e.get('action', ''):<9}{e.get('result', ''):<14}{now:<14}{name}")
        if e.get("tailnet_user"):
            lines.append(f"{'':<26}  from {e['tailnet_user']} at {e.get('ip')}")
        problem = e.get("error") or (row["last_error"] if row and now in
                                     (db.NEEDS_REVIEW, db.FAILED) else None)
        if problem:
            lines.append(f"{'':<26}  -> {problem}")
    return "\n".join(lines)
