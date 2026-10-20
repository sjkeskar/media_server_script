"""Phase 1 tests: bencode parsing, infohash correctness, state store, and the
full intake -> submit -> reconcile loop against a fake Transmission."""

import hashlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from torrentfiler import config as cfgmod
from torrentfiler import db
from torrentfiler.client import PermanentClientError, TransientClientError
from torrentfiler.torrentfile import BencodeError, read_torrent

PASSED = []
FAILED = []


def check(label, condition, detail=""):
    (PASSED if condition else FAILED).append(label)
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}{(' -- ' + detail) if detail else ''}")


def benc(obj):
    if isinstance(obj, int):
        return b"i%de" % obj
    if isinstance(obj, bytes):
        return b"%d:%s" % (len(obj), obj)
    if isinstance(obj, list):
        return b"l" + b"".join(benc(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        return b"d" + b"".join(benc(k) + benc(v) for k, v in sorted(obj.items())) + b"e"
    raise TypeError(obj)


MULTI_INFO = {
    b"name": b"Some.Release.2024.1080p",
    b"piece length": 262144,
    b"pieces": b"\x00" * 20,
    b"files": [
        {b"length": 1500, b"path": [b"a.mkv"]},
        {b"length": 500, b"path": [b"b.srt"]},
    ],
}
MULTI_RAW = benc({b"announce": b"http://tracker.example/announce", b"info": MULTI_INFO})


def test_parsing(work: Path):
    print("\nparsing")

    multi = work / "multi.torrent"
    multi.write_bytes(MULTI_RAW)
    meta = read_torrent(multi)
    check("multi-file infohash matches SHA-1 of info dict",
          meta.infohash == hashlib.sha1(benc(MULTI_INFO)).hexdigest())
    check("multi-file size and count", meta.size_bytes == 2000 and meta.file_count == 2,
          f"{meta.size_bytes} bytes / {meta.file_count} files")

    single_info = {b"name": b"Single.mkv", b"piece length": 262144,
                   b"pieces": b"\x00" * 20, b"length": 4242}
    single = work / "single.torrent"
    single.write_bytes(benc({b"info": single_info}))
    m2 = read_torrent(single)
    check("single-file size and count", m2.size_bytes == 4242 and m2.file_count == 1)

    # The important one: an info dict whose keys are NOT in canonical order.
    # Hashing a re-encoding would give the wrong infohash here.
    # 'name' before 'length' is genuinely out of canonical order, so a
    # re-encoding would reorder the keys and produce a different hash.
    unsorted_info = (b"d4:name7:Odd.mkv6:lengthi99e12:piece lengthi16384e6:pieces20:"
                     + b"\x00" * 20 + b"e")
    odd = work / "unsorted.torrent"
    odd.write_bytes(b"d4:info" + unsorted_info + b"e")
    m3 = read_torrent(odd)
    reencoded = hashlib.sha1(benc({b"length": 99, b"name": b"Odd.mkv",
                                   b"piece length": 16384,
                                   b"pieces": b"\x00" * 20})).hexdigest()
    check("non-canonical info hashes original bytes",
          m3.infohash == hashlib.sha1(unsorted_info).hexdigest())
    check("non-canonical hash differs from re-encoded hash", m3.infohash != reencoded)

    utf8_info = dict(single_info)
    utf8_info[b"name.utf-8"] = "Préféré.mkv".encode("utf-8")
    utf8 = work / "utf8.torrent"
    utf8.write_bytes(benc({b"info": utf8_info}))
    check("utf-8 name preferred", read_torrent(utf8).name == "Préféré.mkv")

    rejects = {
        "garbage": b"this is not bencode",
        "truncated": MULTI_RAW[: len(MULTI_RAW) // 2],
        "empty": b"",
        "no info dict": benc({b"announce": b"x"}),
        "length past end": b"d4:infod6:lengthi99e4:name99:shortee",
        "info not a dict": benc({b"info": b"nope"}),
    }
    for label, data in rejects.items():
        bad = work / "bad.torrent"
        bad.write_bytes(data)
        try:
            read_torrent(bad)
            check(f"rejects {label}", False, "was accepted")
        except BencodeError:
            check(f"rejects {label}", True)


def test_db(work: Path):
    print("\nstate store")
    conn = db.connect(work / "state.db")
    ih = "a" * 40
    db.insert(conn, infohash=ih, name="Thing", source_file="x.torrent",
              size_bytes=10, file_count=1, force_download=False, force_dup=True)
    db.update(conn, ih, state=db.QUEUED, transmission_id=7)
    row = db.get(conn, ih)
    check("insert and update round-trip",
          row["state"] == db.QUEUED and row["transmission_id"] == 7 and row["force_dup"] == 1)
    check("by_state finds queued", len(db.by_state(conn, [db.QUEUED])) == 1)
    check("count_active excludes queued", db.count_active(conn) == 0)
    db.update(conn, ih, state=db.DOWNLOADING)
    check("count_active counts downloading", db.count_active(conn) == 1)
    try:
        db.update(conn, ih, nonsense=1)
        check("rejects unknown column", False, "was accepted")
    except ValueError:
        check("rejects unknown column", True)
    db.delete(conn, ih)
    check("delete removes record", db.get(conn, ih) is None)
    conn.close()


class FakeFile:
    def __init__(self, name, size, selected=True):
        self.name = name
        self.size = size
        self.completed = size
        self.selected = selected


class FakeTorrent:
    def __init__(self, tid, percent=0.0, status="downloading", error=0, error_string="",
                 download_dir="", files=()):
        self.id = tid
        self.percent_done = percent
        self.status = status
        self.error = error
        self.error_string = error_string
        self.download_dir = str(download_dir)
        self.files = list(files)
        self.ratio = 0.0
        self.done_date = None

    def get_files(self):
        return self.files


class FakeClient:
    """Stands in for TransmissionClient. Scriptable failures."""

    def __init__(self):
        self.torrents = {}
        self._next = 100
        self.fail_add_with = None
        self.started = []
        self.removed = []
        # Files the next added torrent reports, so phase 2 tests can shape
        # what a download contains.
        self.next_files = ()

    def ping(self):
        return "fake 4.0"

    def add(self, path, *, download_dir, peer_limit, download_limit_kbps):
        if self.fail_add_with is not None:
            raise self.fail_add_with
        tid = self._next
        self._next += 1
        self.torrents[tid] = FakeTorrent(tid, download_dir=download_dir,
                                         files=self.next_files)
        return tid

    def remove(self, tid, *, delete_data):
        self.removed.append((tid, delete_data))
        self.torrents.pop(tid, None)

    def get(self, ids):
        return [self.torrents[i] for i in ids if i in self.torrents]

    def start(self, tid):
        self.started.append(tid)
        if tid in self.torrents:
            self.torrents[tid].status = "downloading"


def build_service(work: Path):
    from torrentfiler.config import Config, Limits, Paths, Service as SvcCfg, Transmission
    from torrentfiler.service import Service

    paths = Paths(watch_root=work / "watch", temp_download=work / "incomplete",
                  library_root=work / "library", database=work / "svc.db")
    cfg = Config(
        paths=paths,
        transmission=Transmission("127.0.0.1", 9091, "/transmission/rpc", "u", "p", 5),
        limits=Limits(max_active=2, per_torrent_download_kbps=0, per_torrent_peer_limit=20),
        service=SvcCfg(poll_interval_seconds=1, stability_checks=1,
                       process_priority="normal", log_level="CRITICAL",
                       retry_base_seconds=5, retry_max_seconds=300, max_attempts=3),
    )
    cfgmod.ensure_folders(paths)
    service = Service(cfg)
    service.client = FakeClient()
    return service, cfg


def drop(cfg, folder, filename, payload):
    target = cfg.paths.folder(folder) / filename
    target.write_bytes(payload)
    return target


def make_torrent(name):
    info = dict(MULTI_INFO)
    info[b"name"] = name.encode()
    return benc({b"info": info}), hashlib.sha1(benc(info)).hexdigest()


def test_loop(work: Path):
    print("\nservice loop")
    svc, cfg = build_service(work)

    raw_a, ih_a = make_torrent("Release.A")
    drop(cfg, cfgmod.INBOX, "a.torrent", raw_a)
    svc.tick()
    row = db.get(svc.conn, ih_a)
    check("inbox torrent reaches downloading", row and row["state"] == db.DOWNLOADING,
          row["state"] if row else "no record")
    check("claimed file moved to processing/",
          Path(row["source_file"]).parent.name == cfgmod.PROCESSING)
    check("drop folder emptied",
          not list(cfg.paths.folder(cfgmod.INBOX).glob("*.torrent")))

    # Same infohash again -> skipped, not re-added.
    drop(cfg, cfgmod.INBOX, "a-again.torrent", raw_a)
    svc.tick()
    check("duplicate infohash skipped",
          len(list(cfg.paths.folder(cfgmod.SKIPPED).glob("*.torrent"))) == 1)
    check("duplicate did not create a second transmission torrent",
          len(svc.client.torrents) == 1)

    # Force folder overrides the dedup check.
    drop(cfg, cfgmod.FORCE, "a-forced.torrent", raw_a)
    svc.tick()
    row = db.get(svc.conn, ih_a)
    check("force/ re-adds a known infohash", len(svc.client.torrents) == 2)
    check("force_download recorded", row["force_download"] == 1)

    # force-dup sets the flag phase 3 will read.
    raw_b, ih_b = make_torrent("Release.B")
    drop(cfg, cfgmod.FORCE_DUP, "b.torrent", raw_b)
    svc.tick()
    row_b = db.get(svc.conn, ih_b)
    check("force-dup/ sets force_dup flag", row_b["force_dup"] == 1)

    # max_active = 2, so a third torrent must wait in queued.
    raw_c, ih_c = make_torrent("Release.C")
    drop(cfg, cfgmod.INBOX, "c.torrent", raw_c)
    svc.tick()
    row_c = db.get(svc.conn, ih_c)
    check("max_active caps concurrent downloads", row_c["state"] == db.QUEUED,
          f"state={row_c['state']}, active={db.count_active(svc.conn)}")

    # Completion moves a slot free and promotes the waiting torrent.
    for t in svc.client.torrents.values():
        t.percent_done = 1.0
        t.status = "seeding"
    svc.tick()
    # These fakes contain no media, so phase 2 hands them on to review in
    # the same tick; what matters here is that they left downloading.
    past_download = (db.COMPLETE, db.FILING, db.FILED, db.NEEDS_REVIEW)
    summary = db.state_summary(svc.conn)
    check("completed torrents marked complete",
          sum(summary.get(s, 0) for s in past_download) >= 2, str(summary))
    check("queued torrent promoted once a slot frees",
          db.get(svc.conn, ih_c)["state"] == db.DOWNLOADING)

    # A malformed file goes to failed/ with a note, and never enters the db.
    drop(cfg, cfgmod.INBOX, "junk.torrent", b"definitely not bencode")
    svc.tick()
    failed_dir = cfg.paths.folder(cfgmod.FAILED)
    check("malformed torrent quarantined",
          len(list(failed_dir.glob("junk*.torrent"))) == 1)
    check("quarantine note written",
          len(list(failed_dir.glob("junk*.error.txt"))) == 1)

    svc.conn.close()


def test_failures(work: Path):
    print("\nfailure handling")
    svc, cfg = build_service(work / "fail")

    raw, ih = make_torrent("Flaky.Release")
    drop(cfg, cfgmod.INBOX, "f.torrent", raw)

    svc.client.fail_add_with = TransientClientError("Transmission is down")
    svc.tick()
    row = db.get(svc.conn, ih)
    check("transient failure stays queued", row["state"] == db.QUEUED)
    check("attempt counted and retry scheduled",
          row["attempts"] == 1 and row["retry_after"] is not None)

    # Backoff holds it until retry_after passes.
    svc.tick()
    check("backoff suppresses immediate retry", db.get(svc.conn, ih)["attempts"] == 1)

    # Clear the backoff; exhaust the remaining attempts.
    for _ in range(5):
        db.update(svc.conn, ih, retry_after=None)
        svc.tick()
    row = db.get(svc.conn, ih)
    check("gives up after max_attempts",
          row["state"] == db.FAILED and row["attempts"] == 3,
          f"state={row['state']} attempts={row['attempts']}")
    check("failed torrent quarantined with note",
          len(list(cfg.paths.folder(cfgmod.FAILED).glob("*.error.txt"))) == 1)

    # Re-dropping a failed torrent retries it.
    svc.client.fail_add_with = None
    drop(cfg, cfgmod.INBOX, "f-retry.torrent", raw)
    svc.tick()
    check("re-dropping a failed torrent retries it",
          db.get(svc.conn, ih)["state"] == db.DOWNLOADING)

    # Permanent rejection fails immediately, no retries.
    raw2, ih2 = make_torrent("Rejected.Release")
    svc.client.fail_add_with = PermanentClientError("invalid or corrupt torrent file")
    drop(cfg, cfgmod.INBOX, "r.torrent", raw2)
    svc.tick()
    row2 = db.get(svc.conn, ih2)
    check("permanent rejection fails without retrying",
          row2["state"] == db.FAILED and row2["attempts"] == 0)

    # A torrent removed from Transmission by hand is detected.
    svc.client.fail_add_with = None
    svc.client.torrents.clear()
    svc.tick()
    check("torrent vanishing from Transmission is caught",
          db.get(svc.conn, ih)["state"] == db.FAILED)

    svc.conn.close()


def test_crash_recovery(work: Path):
    print("\ncrash recovery")
    svc, cfg = build_service(work / "crash")

    # Record inserted but the file was never claimed -- the crash case where
    # the .torrent is still sitting in the drop folder.
    raw, ih = make_torrent("Interrupted")
    drop(cfg, cfgmod.INBOX, "i.torrent", raw)
    db.insert(svc.conn, infohash=ih, name="Interrupted", source_file="gone.torrent",
              size_bytes=10, file_count=1, force_download=False, force_dup=False)
    svc._recover()
    check("unclaimed record discarded on startup", db.get(svc.conn, ih) is None)
    svc.tick()
    check("file is then picked up fresh",
          db.get(svc.conn, ih)["state"] == db.DOWNLOADING)

    # Record left in 'discovered' with the file already in processing/.
    raw2, ih2 = make_torrent("HalfClaimed")
    claimed = cfg.paths.folder(cfgmod.PROCESSING) / f"{ih2}.torrent"
    claimed.write_bytes(raw2)
    db.insert(svc.conn, infohash=ih2, name="HalfClaimed", source_file=str(claimed),
              size_bytes=10, file_count=1, force_download=False, force_dup=False)
    svc._recover()
    check("claimed record recovered as queued",
          db.get(svc.conn, ih2)["state"] == db.QUEUED)

    svc.conn.close()


def test_stability(work: Path):
    print("\nwatcher stability check")
    from torrentfiler.config import Paths
    from torrentfiler.watcher import Watcher

    paths = Paths(watch_root=work / "stab" / "watch", temp_download=work / "stab" / "tmp",
                  library_root=work / "stab" / "lib", database=work / "stab" / "x.db")
    cfgmod.ensure_folders(paths)
    watcher = Watcher(paths, stability_checks=2)

    growing = paths.folder(cfgmod.INBOX) / "growing.torrent"
    growing.write_bytes(b"partial")
    check("first sighting is not yet stable", watcher.scan() == [])
    growing.write_bytes(b"partial plus more data")
    check("changed file resets the counter", watcher.scan() == [])
    check("unchanged file becomes stable", len(watcher.scan()) == 1)

    empty = paths.folder(cfgmod.INBOX) / "empty.torrent"
    empty.write_bytes(b"")
    names = {c.path.name for c in watcher.scan()}
    check("zero-byte file never goes stable", "empty.torrent" not in names)

    (paths.folder(cfgmod.INBOX) / "notes.txt").write_text("ignore me")
    watcher.scan()
    names = {c.path.name for c in watcher.scan()}
    check("non-torrent files ignored", "notes.txt" not in names)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        test_parsing(work)
        test_db(work)
        test_loop(work / "loop")
        test_failures(work)
        test_crash_recovery(work)
        test_stability(work)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
