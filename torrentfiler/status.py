"""Status of every torrent, grouped by where it is in the pipeline.

One source for every view: snapshot() gathers the facts (as plain data, also
served as JSON by the API for phones and tablets) and render() formats them
as the text --status prints.

Read-only. It opens the database in read-only mode and only *asks*
Transmission for live numbers (progress, speed, ratio), so it is safe to run
while the service is running. If Transmission can't be reached, it still
reports everything the database knows.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone

from . import db
from .client import TransientClientError, TransmissionClient
from .config import Config

# Display order, keys and headings. Purged and skipped torrents are history,
# included only on request (--all / ?all=1).
SECTIONS = [
    ("downloading", (db.DOWNLOADING,), "Downloading"),
    ("queued", (db.DISCOVERED, db.QUEUED), "Queued"),
    ("filing", (db.COMPLETE, db.FILING), "Filing into the library"),
    ("filed", (db.FILED,), "Filed - copied, still seeding"),
    ("review", (db.NEEDS_REVIEW,), "Needs review"),
    ("failed", (db.FAILED,), "Failed"),
    ("done", (db.PURGED,), "Done - filed and no longer in Transmission"),
    ("skipped", (db.SKIPPED,), "Skipped"),
]
HISTORY = ("done", "skipped")
_WORDS = {db.PURGED: "done", db.NEEDS_REVIEW: "needs review"}


def snapshot(cfg: Config, *, show_all: bool = False, client=None) -> dict:
    """Everything the status views show, as JSON-ready data."""
    now = datetime.now(timezone.utc)
    result = {"generated": now.isoformat(timespec="seconds"), "total": 0, "counts": {},
              "transmission": "", "sections": [], "hidden": {}}
    conn = db.connect_readonly(cfg.paths.database)
    if conn is None:
        return result
    try:
        rows = conn.execute("SELECT * FROM torrents ORDER BY added_at").fetchall()
    finally:
        conn.close()

    live, result["transmission"] = _live(cfg, rows, client)
    result["total"] = len(rows)
    for r in rows:
        word = _WORDS.get(r["state"], r["state"])
        result["counts"][word] = result["counts"].get(word, 0) + 1

    for key, states, title in SECTIONS:
        group = [r for r in rows if r["state"] in states]
        if not group:
            continue
        if key in HISTORY and not show_all:
            result["hidden"][key] = len(group)
            continue
        result["sections"].append({
            "key": key, "title": title,
            "items": [_facts(r, live.get(r["transmission_id"]), cfg, now) for r in group],
        })
    return result


def render(cfg: Config, *, show_all: bool = False, client=None, width: int | None = None,
           more_hint: str = "--status --all to list them") -> str:
    """The text --status prints."""
    return format_text(snapshot(cfg, show_all=show_all, client=client),
                       width=width, more_hint=more_hint)


def format_text(snap: dict, *, width: int | None = None,
                more_hint: str = "--status --all to list them") -> str:
    """A snapshot as text. Under 80 columns (a phone), each name gets its
    own line with the details below it."""
    if not snap["total"]:
        return "no torrents on record yet"
    if width is None:
        width = max(60, min(shutil.get_terminal_size((100, 20)).columns, 140))
    name_width = max(30, width - 50) if width >= 80 else max(20, width - 4)
    compact = width < 80  # phones: name on its own line, details below

    counts = ", ".join(f"{n} {state}" for state, n in sorted(snap["counts"].items()))
    out = [f"torrentfiler: {snap['total']} torrent(s) -- {counts}",
           f"({snap['transmission']})", ""]
    for section in snap["sections"]:
        out.append(f"{section['title']} ({len(section['items'])})")
        for item in section["items"]:
            name = _cut(item["name"], name_width)
            if compact:
                out.append(f"  {name}")
                out.append(f"    {_size(item['size_bytes'])}  {item['summary']}")
            else:
                out.append(f"  {name:<{name_width}}  {_size(item['size_bytes']):>9}  "
                           f"{item['summary']}")
            if item["target_path"]:
                out.append(f"    -> {item['target_path']}")
            if item["error"]:
                out.append(f"    ! {item['error']}")
        out.append("")

    hidden = [f"{n} {key}" for key, n in snap["hidden"].items()]
    if hidden:
        out.append(f"Also on record: {', '.join(hidden)} ({more_hint})")
    return "\n".join(out).rstrip()


def _live(cfg: Config, rows, client) -> tuple[dict, str]:
    """Live Transmission numbers for torrents it is still handling."""
    ids = [int(r["transmission_id"]) for r in rows if r["transmission_id"]
           and r["state"] in (db.DOWNLOADING, db.COMPLETE, db.FILING, db.FILED,
                              db.NEEDS_REVIEW)]
    if not ids:
        return {}, "nothing in Transmission right now"
    client = client or TransmissionClient(cfg.transmission)
    try:
        torrents = client.get(ids)
    except TransientClientError as exc:
        return {}, f"Transmission not reachable, showing database only: {exc}"
    return {int(t.id): t for t in torrents}, "live figures from Transmission"


def _facts(r, t, cfg: Config, now: datetime) -> dict:
    """One torrent as data. `summary` is the one-line description every view
    shows; the numeric fields let a page draw bars."""
    state = r["state"]
    category = _col(r, "category")
    size = r["size_bytes"] or (_get(t, "total_size") if t is not None else None)
    facts = {"name": r["name"] or "", "infohash": r["infohash"], "state": state,
             "category": category, "size_bytes": size, "summary": "", "error": None,
             "target_path": None, "progress": None, "in_transmission": t is not None}

    if state == db.DOWNLOADING:
        facts["error"] = r["last_error"]
        if t is None:
            facts["summary"] = "not found in Transmission"
        elif float(_get(t, "metadata_percent_complete", 1.0) or 0) < 1.0:
            facts["summary"] = (f"magnet: fetching file list, waiting "
                                f"{_ago(r['stalled_since'], now)}")
        else:
            progress = float(_get(t, "percent_done", 0) or 0)
            eta = _get(t, "eta")
            facts["progress"] = progress
            facts["summary"] = (f"{progress * 100:5.1f}%  {_rate(_get(t, 'rate_download', 0))}  "
                                f"ETA {_span(eta.total_seconds()) if eta else '?'}  "
                                f"peers {_get(t, 'peers_connected', '?')}")

    elif state in (db.DISCOVERED, db.QUEUED):
        facts["error"] = r["last_error"]
        facts["summary"] = f"waiting {_ago(r['added_at'], now)}"
        if r["retry_after"]:
            facts["summary"] += f", retry {_until(r['retry_after'], now)}"

    elif state in (db.COMPLETE, db.FILING):
        facts["error"] = r["last_error"]
        facts["summary"] = ("copying or moving files" if state == db.FILING
                            else "about to be filed")
        if r["retry_after"]:
            facts["summary"] = f"waiting to retry {_until(r['retry_after'], now)}"

    elif state == db.FILED:
        category = category or "video"
        facts["category"] = category
        facts["target_path"] = r["target_path"]
        policy = cfg.filing.policy(category)
        if t is None:
            facts["summary"] = f"[{category}] not in Transmission"
        else:
            # Seeding ends at whichever limit is reached first; progress is
            # how far along the nearer one is.
            parts, toward = [], []
            ratio = float(_get(t, "ratio", 0) or 0)
            parts.append(f"ratio {ratio:.2f}" + (f" of {policy.seed_ratio:g}"
                                                 if policy.seed_ratio else ""))
            if policy.seed_ratio:
                toward.append(ratio / policy.seed_ratio)
            done = _get(t, "done_date") or _parse(r["filed_at"])
            if policy.seed_hours and done:
                seeded = (now - done).total_seconds()
                parts.append(f"seeding {_span(seeded)} of {_span(policy.seed_hours * 3600)}")
                toward.append(seeded / (policy.seed_hours * 3600))
            window = cfg.filing.stall_fraction * policy.seed_hours
            mark_at = _parse(_col(r, "seed_mark_at"))
            if window and mark_at:
                idle = (now - mark_at).total_seconds()
                if idle >= 600:
                    parts.append(f"no upload for {_span(idle)} "
                                 f"(ends early at {_span(window * 3600)})")
                    toward.append(idle / (window * 3600))
            parts.append(f"up {_rate(_get(t, 'rate_upload', 0))}")
            facts["summary"] = f"[{category}] " + "  ".join(parts)
            facts["progress"] = min(1.0, max(toward)) if toward else None

    elif state in (db.NEEDS_REVIEW, db.FAILED):
        facts["summary"] = f"{_ago(r['updated_at'], now)} ago"
        if state == db.NEEDS_REVIEW and t is not None:
            facts["summary"] += "  (still in Transmission)"
        facts["error"] = r["last_error"] or "no reason recorded"

    elif state == db.PURGED:
        label = f"[{category}] " if category else ""
        facts["summary"] = f"{label}-> {r['target_path'] or '-'}"

    return facts


# -- formatting -------------------------------------------------------

def _col(row, column: str):
    """A column that older databases may not have until the service upgrades them."""
    return row[column] if column in row.keys() else None


def _get(obj, attr, default=None):
    try:
        return getattr(obj, attr)
    except (KeyError, AttributeError):  # field not fetched / not present
        return default


def _cut(text: str, width: int) -> str:
    text = text or ""
    return text if len(text) <= width else text[: width - 3] + "..."


def _size(n) -> str:
    if not n:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "-"


def _rate(n) -> str:
    return f"{_size(n)}/s" if n else "0 B/s"


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def _span(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 90 * 60:
        return f"{seconds // 60}m"
    if seconds < 36 * 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds / 86400:.1f}d"


def _ago(stamp: str | None, now: datetime) -> str:
    then = _parse(stamp)
    return _span((now - then).total_seconds()) if then else "?"


def _until(stamp: str | None, now: datetime) -> str:
    then = _parse(stamp)
    if not then:
        return "soon"
    seconds = (then - now).total_seconds()
    return "now" if seconds <= 0 else f"in {_span(seconds)}"
