"""SQLite state store.

Every state transition is written here *before* the action it describes,
so a crash always resumes from a known point. The schema already carries
the columns later phases need; phase 1 simply leaves them null.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

SCHEMA_VERSION = 2

# Columns added after version 1, applied to existing databases on connect.
#   category      video | music | iso | other, decided when filing
#   seed_mark     bytes uploaded when last seen to change (stall detection)
#   seed_mark_at  when that was
_ADDED_COLUMNS = {
    "category": "TEXT",
    "seed_mark": "INTEGER",
    "seed_mark_at": "TEXT",
}

# Phase 1 states.
DISCOVERED = "discovered"
QUEUED = "queued"
DOWNLOADING = "downloading"
COMPLETE = "complete"
SKIPPED = "skipped"
FAILED = "failed"

# Phase 2 states. FILED means "copied into the library, still seeding";
# PURGED means "removed from Transmission, temp copy deleted".
FILING = "filing"
FILED = "filed"
PURGED = "purged"
NEEDS_REVIEW = "needs_review"

TERMINAL = (SKIPPED, FAILED, PURGED, NEEDS_REVIEW)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS torrents (
    infohash        TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    source_file     TEXT NOT NULL,
    state           TEXT NOT NULL,
    force_download  INTEGER NOT NULL DEFAULT 0,
    force_dup       INTEGER NOT NULL DEFAULT 0,
    size_bytes      INTEGER,
    file_count      INTEGER,
    transmission_id INTEGER,
    temp_path       TEXT,
    target_path     TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    stalled_since   TEXT,
    retry_after     TEXT,
    added_at        TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    filed_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_torrents_state ON torrents(state);

CREATE TABLE IF NOT EXISTS files (
    infohash  TEXT NOT NULL,
    src       TEXT NOT NULL,
    dst       TEXT,
    verified  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (infohash, src)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(torrents)")}
    for column, decl in _ADDED_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE torrents ADD COLUMN {column} {decl}")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    elif row["version"] < SCHEMA_VERSION:
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    return conn


def connect_readonly(path: Path) -> Optional[sqlite3.Connection]:
    """For processes other than the main service (the API, reports).

    Read-only at the SQLite level, and never creates the schema, so it can
    never interfere with the service's writes. None if there's no database yet.
    """
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def get(conn: sqlite3.Connection, infohash: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM torrents WHERE infohash = ?", (infohash,)
    ).fetchone()


def insert(
    conn: sqlite3.Connection,
    *,
    infohash: str,
    name: str,
    source_file: str,
    size_bytes: Optional[int],
    file_count: Optional[int],
    force_download: bool,
    force_dup: bool,
    state: str = DISCOVERED,
) -> None:
    stamp = now()
    conn.execute(
        """
        INSERT INTO torrents (
            infohash, name, source_file, state, force_download, force_dup,
            size_bytes, file_count, added_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            infohash,
            name,
            source_file,
            state,
            int(force_download),
            int(force_dup),
            size_bytes,
            file_count,
            stamp,
            stamp,
        ),
    )


_UPDATABLE = {
    "state",
    "category",
    "seed_mark",
    "seed_mark_at",
    "name",
    "size_bytes",
    "file_count",
    "source_file",
    "transmission_id",
    "temp_path",
    "target_path",
    "attempts",
    "last_error",
    "stalled_since",
    "retry_after",
    "filed_at",
    "force_download",
    "force_dup",
}


def update(conn: sqlite3.Connection, infohash: str, **fields) -> None:
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"cannot update unknown column(s): {sorted(unknown)}")
    fields["updated_at"] = now()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(
        f"UPDATE torrents SET {assignments} WHERE infohash = ?",
        (*fields.values(), infohash),
    )


def delete(conn: sqlite3.Connection, infohash: str) -> None:
    conn.execute("DELETE FROM files WHERE infohash = ?", (infohash,))
    conn.execute("DELETE FROM torrents WHERE infohash = ?", (infohash,))


def clear_files(conn: sqlite3.Connection, infohash: str) -> None:
    conn.execute("DELETE FROM files WHERE infohash = ?", (infohash,))


def record_file(conn: sqlite3.Connection, infohash: str, src: str, dst: str,
                verified: bool = True) -> None:
    """Record a file's destination. verified=True once the copy or move has
    been checked; False records a planned move before it starts, so an
    interrupted move can be finished after a crash."""
    conn.execute(
        """
        INSERT INTO files (infohash, src, dst, verified) VALUES (?, ?, ?, ?)
        ON CONFLICT (infohash, src) DO UPDATE SET dst = excluded.dst,
                                                  verified = excluded.verified
        """,
        (infohash, src, dst, int(verified)),
    )


def files_for(conn: sqlite3.Connection, infohash: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM files WHERE infohash = ? ORDER BY src", (infohash,)
    ).fetchall()


def by_state(conn: sqlite3.Connection, states: Iterable[str]) -> list[sqlite3.Row]:
    states = tuple(states)
    placeholders = ",".join("?" for _ in states)
    return conn.execute(
        f"SELECT * FROM torrents WHERE state IN ({placeholders}) ORDER BY added_at",
        states,
    ).fetchall()


def count_active(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM torrents WHERE state = ?", (DOWNLOADING,)
    ).fetchone()
    return int(row["n"])


def state_summary(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM torrents GROUP BY state"
    ).fetchall()
    return {row["state"]: int(row["n"]) for row in rows}
