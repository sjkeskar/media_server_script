"""Phase 2 tests: choosing media files, copying them into the library, and
removing seeded-out torrents -- against real files in a temp folder and a
fake Transmission."""

import dataclasses
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_phase1 import FakeFile, build_service, drop, make_torrent
from torrentfiler import config as cfgmod
from torrentfiler import db
from torrentfiler.filer import PART_SUFFIX, is_wanted

PASSED = []
FAILED = []


def check(label, condition, detail=""):
    (PASSED if condition else FAILED).append(label)
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}{(' -- ' + detail) if detail else ''}")


def stage(cfg, files: dict) -> list:
    """Write a finished download into temp_download; return its FakeFiles."""
    out = []
    for rel, payload in files.items():
        path = cfg.paths.temp_download / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        out.append(FakeFile(rel, len(payload)))
    return out


def download(svc, cfg, name, files: dict, folder=cfgmod.INBOX):
    """Drop a torrent, let it 'download', and tick once more so it is filed."""
    raw, ih = make_torrent(name)
    svc.client.next_files = stage(cfg, files)
    drop(cfg, folder, f"{name}.torrent", raw)
    svc.tick()
    row = db.get(svc.conn, ih)
    svc.client.torrents[row["transmission_id"]].percent_done = 1.0
    svc.tick()
    return ih, raw


def state(svc, ih):
    return db.get(svc.conn, ih)["state"]


def test_selection():
    print("\nfile selection")
    wanted = [
        "Show/S01E01.mkv", "Movie/movie.mp4", "Movie/movie.en.srt", "Movie/Subs/2_Eng.srt",
        "Movie/poster.jpg", "Movie/folder.jpg", "Movie/movie-fanart.png", "Movie/Cover.JPG",
    ]
    unwanted = [
        "Movie/sample.mkv", "Movie/movie-sample.mkv", "Movie/Sample/movie.mkv",
        "Movie/Screens/poster.jpg", "Movie/screenshot1.png", "Movie/movie.nfo",
        "Movie/readme.txt", "Movie/setup.exe", "distro.iso", "Movie/RARBG.com.txt",
    ]
    check("video, subtitles and artwork chosen", all(is_wanted(p) for p in wanted),
          str([p for p in wanted if not is_wanted(p)]))
    check("samples, screenshots and junk skipped", not any(is_wanted(p) for p in unwanted),
          str([p for p in unwanted if is_wanted(p)]))


def test_filing(work: Path):
    print("\nfiling")
    svc, cfg = build_service(work / "file")
    lib = cfg.paths.library_root

    ih, _ = download(svc, cfg, "Movie.2024", {
        "Movie.2024/Movie.2024.mkv": b"v" * 3000,
        "Movie.2024/Subs/English.srt": b"subtitle",
        "Movie.2024/poster.jpg": b"img",
        "Movie.2024/Movie.2024.nfo": b"nfo",
        "Movie.2024/Sample/sample.mkv": b"s" * 100,
    })
    check("multi-file torrent filed", state(svc, ih) == db.FILED, state(svc, ih))
    check("video copied with identical bytes",
          (lib / "Movie.2024/Movie.2024.mkv").read_bytes() == b"v" * 3000)
    check("subtitle folder structure kept", (lib / "Movie.2024/Subs/English.srt").is_file())
    check("cover copied", (lib / "Movie.2024/poster.jpg").is_file())
    check("nfo and sample not copied",
          not (lib / "Movie.2024/Movie.2024.nfo").exists()
          and not (lib / "Movie.2024/Sample").exists())
    check("temp copy left in place for seeding",
          (cfg.paths.temp_download / "Movie.2024/Movie.2024.mkv").is_file())
    check("no .part files left", not list(lib.rglob(f"*{PART_SUFFIX}")))
    row = db.get(svc.conn, ih)
    check("target_path and filed_at recorded",
          row["target_path"] == str(lib / "Movie.2024") and row["filed_at"] is not None)
    check("each copy recorded as verified",
          len(db.files_for(svc.conn, ih)) == 3
          and all(f["verified"] == 1 for f in db.files_for(svc.conn, ih)))
    check(".torrent moved to done/",
          Path(row["source_file"]).parent.name == cfgmod.DONE and Path(row["source_file"]).is_file())
    check("seeding torrent not counted against max_active", db.count_active(svc.conn) == 0)

    ih2, _ = download(svc, cfg, "Single", {"Single.Film.2023.mkv": b"x" * 500})
    check("single-file torrent wrapped in its own folder",
          (lib / "Single.Film.2023/Single.Film.2023.mkv").is_file(), state(svc, ih2))

    # Non-video downloads have their own rules now (see test_categories.py);
    # an ISO is moved out of the way rather than left for review.
    ih3, _ = download(svc, cfg, "Distro", {"distro-26.04.iso": b"i" * 200})
    check("disk image is not left for review", state(svc, ih3) == db.PURGED,
          state(svc, ih3))
    svc.conn.close()


def test_conflicts(work: Path):
    print("\nconflicts and recovery")
    svc, cfg = build_service(work / "conf")
    lib = cfg.paths.library_root

    # Something different already sits at the destination: never overwrite.
    (lib / "Clash").mkdir(parents=True)
    (lib / "Clash/Clash.mkv").write_bytes(b"precious")
    ih, _ = download(svc, cfg, "Clash", {"Clash/Clash.mkv": b"n" * 50, "Clash/a.srt": b"s"})
    check("differing file in library -> needs review", state(svc, ih) == db.NEEDS_REVIEW)
    check("existing library file untouched", (lib / "Clash/Clash.mkv").read_bytes() == b"precious")
    check("nothing partially copied on conflict", not (lib / "Clash/a.srt").exists())
    check("review note written beside the .torrent",
          any("NEEDS REVIEW" in p.read_text()
              for p in cfg.paths.folder(cfgmod.FAILED).glob("*.error.txt")))
    check("needs-review torrent left in Transmission",
          db.get(svc.conn, ih)["transmission_id"] in svc.client.torrents)

    # Identical file already present (e.g. copied by hand) is accepted.
    (lib / "Same").mkdir(parents=True)
    (lib / "Same/Same.mkv").write_bytes(b"q" * 40)
    ih2, _ = download(svc, cfg, "Same", {"Same/Same.mkv": b"q" * 40, "Same/Same.srt": b"t"})
    check("identical existing file accepted", state(svc, ih2) == db.FILED, state(svc, ih2))
    check("missing sibling still copied", (lib / "Same/Same.srt").is_file())

    # Crash mid-copy: record left in FILING with a .part file behind.
    raw, ih3 = make_torrent("Crashy")
    svc.client.next_files = stage(cfg, {"Crashy/Crashy.mkv": b"c" * 80})
    drop(cfg, cfgmod.INBOX, "Crashy.torrent", raw)
    svc.tick()
    (lib / "Crashy").mkdir(parents=True)
    (lib / f"Crashy/Crashy.mkv{PART_SUFFIX}").write_bytes(b"half")
    db.update(svc.conn, ih3, state=db.FILING)
    svc.client.torrents[db.get(svc.conn, ih3)["transmission_id"]].percent_done = 1.0
    svc.tick()
    check("interrupted filing resumes", state(svc, ih3) == db.FILED, state(svc, ih3))
    check("stale .part replaced by a whole copy",
          (lib / "Crashy/Crashy.mkv").read_bytes() == b"c" * 80
          and not (lib / f"Crashy/Crashy.mkv{PART_SUFFIX}").exists())

    # Force re-drop of a filed torrent goes round again without recopying.
    drop(cfg, cfgmod.FORCE, "Same-again.torrent", make_torrent("Same")[0])
    svc.tick()
    svc.client.torrents[db.get(svc.conn, ih2)["transmission_id"]].percent_done = 1.0
    svc.tick()
    check("force re-drop of a filed torrent refiles it", state(svc, ih2) == db.FILED,
          state(svc, ih2))
    svc.conn.close()


def test_space(work: Path):
    print("\nfree space")
    svc, cfg = build_service(work / "space")
    svc.cfg = dataclasses.replace(
        cfg, filing=dataclasses.replace(cfg.filing, min_free_gb=10**9))
    ih, _ = download(svc, cfg, "Huge", {"Huge/Huge.mkv": b"h" * 10})
    row = db.get(svc.conn, ih)
    check("low space defers instead of failing",
          row["state"] == db.FILING and row["attempts"] == 1 and row["retry_after"])
    check("nothing copied while short of space",
          not (cfg.paths.library_root / "Huge").exists())
    for _ in range(5):
        db.update(svc.conn, ih, retry_after=None)
        svc.tick()
    check("gives up to review after max_attempts", state(svc, ih) == db.NEEDS_REVIEW)
    svc.conn.close()

    # Low space with torrents still seeding: end all seeding to make room.
    svc, cfg = build_service(work / "space2")
    roomy, tight = cfg, dataclasses.replace(
        cfg, filing=dataclasses.replace(cfg.filing, min_free_gb=10**9))
    lib = cfg.paths.library_root
    ih_a, _ = download(svc, cfg, "SeedA", {"SeedA/A.mkv": b"a" * 64})
    ih_b, _ = download(svc, cfg, "SeedB", {"SeedB/B.mkv": b"b" * 64})
    tids = {db.get(svc.conn, i)["transmission_id"] for i in (ih_a, ih_b)}
    check("both seeding before space runs out",
          state(svc, ih_a) == db.FILED and state(svc, ih_b) == db.FILED)

    svc.cfg = tight
    ih_c, _ = download(svc, cfg, "NewC", {"NewC/C.mkv": b"c" * 64})
    check("copy that won't fit is deferred", state(svc, ih_c) == db.FILING)
    check("low space ends all seeding in the same tick",
          state(svc, ih_a) == db.PURGED and state(svc, ih_b) == db.PURGED)
    check("their temp copies deleted via Transmission",
          {(t, True) for t in tids} <= set(svc.client.removed))
    check("library copies kept when seeding is cut short",
          (lib / "SeedA/A.mkv").is_file() and (lib / "SeedB/B.mkv").is_file())

    svc.cfg = roomy  # space freed
    db.update(svc.conn, ih_c, retry_after=None)
    svc.tick()
    check("deferred copy goes through once space is freed", state(svc, ih_c) == db.FILED)
    check("and it is left seeding when space is fine",
          db.get(svc.conn, ih_c)["transmission_id"] in svc.client.torrents)

    # Download volume low with nothing waiting to file: still frees space.
    svc.cfg = tight
    svc.tick()
    check("low space on the download volume ends seeding", state(svc, ih_c) == db.PURGED)
    svc.conn.close()


def test_cleanup(work: Path):
    print("\nseeding cleanup")
    svc, cfg = build_service(work / "clean")
    lib = cfg.paths.library_root

    ih, _ = download(svc, cfg, "Seed.Me", {"Seed.Me/Seed.Me.mkv": b"m" * 64})
    tid = db.get(svc.conn, ih)["transmission_id"]
    svc.tick()
    check("keeps seeding below ratio and age", state(svc, ih) == db.FILED
          and tid in svc.client.torrents)

    svc.client.torrents[tid].ratio = 1.0
    svc.tick()
    check("ratio >= 1.0 -> purged", state(svc, ih) == db.PURGED)
    check("removed with delete_data", (tid, True) in svc.client.removed)
    check("library copy survives purge", (lib / "Seed.Me/Seed.Me.mkv").is_file())

    ih2, _ = download(svc, cfg, "Old", {"Old/Old.mkv": b"o" * 64})
    tid2 = db.get(svc.conn, ih2)["transmission_id"]
    svc.client.torrents[tid2].done_date = datetime.now(timezone.utc) - timedelta(days=9.9)
    svc.tick()
    check("9.9 days old keeps seeding", state(svc, ih2) == db.FILED)
    svc.client.torrents[tid2].done_date = datetime.now(timezone.utc) - timedelta(days=10.1)
    svc.tick()
    check("10 days old -> purged", state(svc, ih2) == db.PURGED)

    # Library copy deleted by hand before seeding ends: keep the temp copy.
    ih3, _ = download(svc, cfg, "Gone", {"Gone/Gone.mkv": b"g" * 64})
    tid3 = db.get(svc.conn, ih3)["transmission_id"]
    (lib / "Gone/Gone.mkv").unlink()
    svc.client.torrents[tid3].ratio = 2.0
    svc.tick()
    check("missing library copy blocks purge", state(svc, ih3) == db.NEEDS_REVIEW)
    check("temp copy not deleted when library copy is missing",
          tid3 in svc.client.torrents and all(r[0] != tid3 for r in svc.client.removed))

    # Download living outside temp_download is never deleted.
    ih4, _ = download(svc, cfg, "Elsewhere", {"Elsewhere/E.mkv": b"e" * 64})
    tid4 = db.get(svc.conn, ih4)["transmission_id"]
    svc.client.torrents[tid4].download_dir = str(work / "clean" / "somewhere-else")
    svc.client.torrents[tid4].ratio = 5.0
    svc.tick()
    check("download outside temp_download blocks purge", state(svc, ih4) == db.NEEDS_REVIEW)

    # Removed in the web UI while seeding: just record it.
    ih5, _ = download(svc, cfg, "ByHand", {"ByHand/B.mkv": b"b" * 64})
    tid5 = db.get(svc.conn, ih5)["transmission_id"]
    del svc.client.torrents[tid5]
    removed_before = len(svc.client.removed)
    svc.tick()
    check("removed by hand -> purged without another remove",
          state(svc, ih5) == db.PURGED and len(svc.client.removed) == removed_before)

    raw, _ = make_torrent("Seed.Me")
    drop(cfg, cfgmod.INBOX, "seed-again.torrent", raw)
    svc.tick()
    check("re-dropping a purged torrent is skipped as a duplicate",
          len(list(cfg.paths.folder(cfgmod.SKIPPED).glob("*.torrent"))) == 1)
    svc.conn.close()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        test_selection()
        test_filing(work)
        test_conflicts(work)
        test_space(work)
        test_cleanup(work)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
