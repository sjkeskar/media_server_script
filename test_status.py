"""--status report tests: every state listed with the right details, live
figures from a fake Transmission, and the database-only fallback."""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_phase1 import FakeTorrent, build_service
from torrentfiler import db, status
from torrentfiler.client import TransientClientError

PASSED = []
FAILED = []


def check(label, condition, detail=""):
    (PASSED if condition else FAILED).append(label)
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}{(' -- ' + detail) if detail else ''}")


class Offline:
    def get(self, ids):
        raise TransientClientError("cannot reach Transmission: refused")


def add(conn, ih, name, state, tid=None, **fields):
    db.insert(conn, infohash=ih * 40, name=name, source_file="x", size_bytes=3 * 1024**3,
              file_count=1, force_download=False, force_dup=False, state=state)
    if tid is not None or fields:
        db.update(conn, ih * 40, transmission_id=tid, **fields)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        svc, cfg = build_service(Path(tmp))
        conn = svc.conn
        print("\nstatus report")
        check("empty database", status.render(cfg, client=svc.client) == "no torrents on record yet")

        add(conn, "a", "Movie.Downloading", db.DOWNLOADING, tid=1)
        add(conn, "b", "Show.Queued", db.QUEUED)
        add(conn, "c", "Film.Filed", db.FILED, tid=3, target_path=r"Z:\jellyfin\Film.Filed",
            filed_at=db.now())
        add(conn, "d", "Distro.iso", db.NEEDS_REVIEW, tid=4,
            last_error="no video, subtitle or cover files found")
        add(conn, "e", "Dead.Magnet", db.FAILED, last_error="no metadata from peers")
        add(conn, "f", "Old.Film", db.PURGED, target_path=r"Z:\jellyfin\Old.Film")
        add(conn, "g", "Copying.Now", db.FILING, tid=7)

        dl = FakeTorrent(1, percent=0.42)
        dl.rate_download, dl.peers_connected = 5 * 1024**2, 12
        dl.eta = timedelta(minutes=8)
        dl.metadata_percent_complete = 1.0
        seed = FakeTorrent(3, percent=1.0, status="seeding")
        seed.ratio, seed.rate_upload = 0.37, 0
        seed.done_date = datetime.now(timezone.utc) - timedelta(days=2.5)
        svc.client.torrents = {1: dl, 3: seed, 4: FakeTorrent(4, percent=1.0), 7: FakeTorrent(7)}

        text = status.render(cfg, client=svc.client)
        print("\n" + "\n".join("      " + ln for ln in text.splitlines()) + "\n")
        check("headline counts every state", "7 torrent(s)" in text and "1 needs review" in text)
        check("downloading shows live progress, speed, ETA and peers",
              "42.0%" in text and "5.0 MB/s" in text and "ETA 8m" in text and "peers 12" in text)
        check("queued listed with its wait", "Show.Queued" in text and "waiting" in text)
        check("filing listed", "Copying.Now" in text and "copying or moving files" in text)
        check("filed shows ratio, seeding days and library path",
              "ratio 0.37 of 1" in text and "seeding 2.5d of 10.0d" in text and "[video]" in text
              and r"-> Z:\jellyfin\Film.Filed" in text)
        check("needs review shows the reason and that it is still in Transmission",
              "no video, subtitle or cover files found" in text and "still in Transmission" in text)
        check("failed shows the reason", "no metadata from peers" in text)
        check("finished torrents hidden by default but counted",
              "Old.Film" not in text and "1 done" in text and "--all" in text)
        check("--all lists finished torrents",
              "Old.Film" in status.render(cfg, show_all=True, client=svc.client))

        dl.metadata_percent_complete = 0.0
        db.update(conn, "a" * 40, stalled_since=(datetime.now(timezone.utc)
                                                - timedelta(minutes=4)).isoformat())
        check("magnet waiting for metadata says so",
              "fetching file list, waiting 4m" in status.render(cfg, client=svc.client))

        offline = status.render(cfg, client=Offline())
        check("works with Transmission down, from the database only",
              "not reachable" in offline and "Film.Filed" in offline and "Distro.iso" in offline)

        long_name = "A" * 300
        add(conn, "h", long_name, db.QUEUED)
        check("very long names are shortened",
              long_name not in status.render(cfg, client=svc.client))
        conn.close()

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
