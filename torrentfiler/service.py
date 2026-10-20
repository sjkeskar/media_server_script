"""The service loop: intake, reconcile, file, clean up, submit."""

from __future__ import annotations

import logging
import logging.handlers
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as cfgmod
from . import db
from .client import PermanentClientError, TransientClientError, TransmissionClient
from .config import Config
from .filer import (
    CopyItem,
    FilingError,
    LowSpaceError,
    RetryableFilingError,
    TorrentFile,
    classify,
    copy_verified,
    free_bytes,
    library_copy_intact,
    move_verified,
    plan,
    prune_empty_dirs,
)
from . import magnet
from .torrentfile import BencodeError, TorrentMeta, read_torrent
from .watcher import SOURCE_SUFFIXES, Watcher

log = logging.getLogger(__name__)


def setup_logging(level: str, log_file: Path) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    log_file.parent.mkdir(parents=True, exist_ok=True)
    rotating = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    rotating.setFormatter(fmt)
    root.addHandler(rotating)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)


def lower_priority(mode: str) -> None:
    """Drop CPU and I/O priority so the service never competes with a game."""
    if mode == "normal":
        return
    try:
        import psutil
    except ImportError:
        log.warning("psutil not installed; running at normal priority")
        return

    proc = psutil.Process()
    try:
        if hasattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS"):
            proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            proc.nice(10)
    except (psutil.Error, OSError) as exc:
        log.warning("could not lower CPU priority: %s", exc)

    try:
        if hasattr(psutil, "IOPRIO_VERYLOW"):
            proc.ionice(psutil.IOPRIO_VERYLOW)
        elif hasattr(psutil, "IOPRIO_CLASS_IDLE"):
            proc.ionice(psutil.IOPRIO_CLASS_IDLE)
    except (psutil.Error, OSError, AttributeError) as exc:
        log.debug("could not lower I/O priority: %s", exc)


class Service:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stop_event = threading.Event()
        self.conn = db.connect(cfg.paths.database)
        self.watcher = Watcher(cfg.paths, cfg.service.stability_checks)
        self.client = TransmissionClient(cfg.transmission)
        self._warned_offline = False
        # Set by filing when a copy would not fit; tells cleanup to free
        # space by ending all seeding in the same tick.
        self._space_short = False

    # -- lifecycle ----------------------------------------------------

    def run(self) -> None:
        log.info("torrentfiler starting")
        log.info("watch root  %s", self.cfg.paths.watch_root)
        log.info("temp folder %s", self.cfg.paths.temp_download)
        log.info("library     %s", self.cfg.paths.library_root)
        self._probe_transmission()
        self._recover()

        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                # A bug in one tick must not kill the service.
                log.exception("unhandled error during tick")
            self.stop_event.wait(self.cfg.service.poll_interval_seconds)

        log.info("torrentfiler stopped; %s", db.state_summary(self.conn))
        self.conn.close()

    def tick(self) -> None:
        # Order matters: reconcile before submit, so a torrent that finished
        # since the last poll frees its slot in time for a queued torrent to
        # take it in this same tick rather than the next one.
        self._intake()
        self._reconcile()
        self._file()
        self._cleanup()
        self._submit()

    def _probe_transmission(self) -> None:
        try:
            log.info("connected to Transmission %s", self.client.ping())
        except TransientClientError as exc:
            log.warning("%s -- will keep retrying", exc)

    def _recover(self) -> None:
        """Clean up records left mid-transition by a crash."""
        stranded = db.by_state(self.conn, [db.DISCOVERED])
        processing = self.cfg.paths.folder(cfgmod.PROCESSING)
        for row in stranded:
            claimed = next((processing / f"{row['infohash']}{suffix}"
                            for suffix in SOURCE_SUFFIXES
                            if (processing / f"{row['infohash']}{suffix}").is_file()), None)
            if claimed is not None:
                db.update(self.conn, row["infohash"], state=db.QUEUED,
                          source_file=str(claimed))
                log.info("recovered %s as queued", row["name"])
            else:
                # Never got claimed -- the file is still in a drop folder
                # and will be picked up fresh on the next scan.
                db.delete(self.conn, row["infohash"])
                log.info("discarded unclaimed record for %s", row["name"])

    # -- stage 1: intake ----------------------------------------------

    def _intake(self) -> None:
        for candidate in self.watcher.scan():
            is_magnet = candidate.path.suffix.lower() == magnet.SUFFIX
            try:
                meta = _read_source(candidate.path)
            except (BencodeError, magnet.MagnetError) as exc:
                log.warning("quarantining %s: %s", candidate.path.name, exc)
                kind = "magnet link" if is_magnet else ".torrent file"
                self.watcher.park(
                    candidate.path,
                    cfgmod.FAILED,
                    note=f"Not a usable {kind}.\n\n{exc}\n",
                )
                continue
            except OSError as exc:
                log.warning("could not read %s: %s", candidate.path.name, exc)
                continue

            existing = db.get(self.conn, meta.infohash)

            if existing is not None and not candidate.force_download:
                if existing["state"] == db.FAILED:
                    log.info("retrying previously failed %s", meta.name)
                    db.update(self.conn, meta.infohash, state=db.DISCOVERED,
                              attempts=0, last_error=None, retry_after=None,
                              force_dup=int(candidate.force_dup))
                else:
                    log.info("skipping duplicate %s (%s)", meta.name, existing["state"])
                    self.watcher.park(candidate.path, cfgmod.SKIPPED)
                    continue
            elif existing is not None and candidate.force_download:
                log.info("forced re-add of %s", meta.name)
                db.update(self.conn, meta.infohash, state=db.DISCOVERED,
                          attempts=0, last_error=None, retry_after=None,
                          force_download=1, force_dup=int(candidate.force_dup))
            else:
                db.insert(
                    self.conn,
                    infohash=meta.infohash,
                    name=meta.name,
                    source_file=str(candidate.path),
                    size_bytes=meta.size_bytes,
                    file_count=meta.file_count,
                    force_download=candidate.force_download,
                    force_dup=candidate.force_dup,
                )
                if meta.size_bytes is None:
                    log.info("accepted magnet %s [%s] from %s; size known once "
                             "metadata arrives", meta.name, meta.short, candidate.origin)
                else:
                    log.info(
                        "accepted %s [%s] %.2f GiB, %d file(s), from %s",
                        meta.name,
                        meta.short,
                        meta.size_bytes / 1024**3,
                        meta.file_count,
                        candidate.origin,
                    )

            try:
                claimed = self.watcher.claim(candidate.path, meta.infohash)
            except OSError as exc:
                log.warning("could not claim %s: %s", candidate.path.name, exc)
                continue
            db.update(self.conn, meta.infohash, state=db.QUEUED,
                      source_file=str(claimed))

    # -- stage 2: submit ----------------------------------------------

    def _submit(self) -> None:
        active = db.count_active(self.conn)
        slots = self.cfg.limits.max_active - active
        if slots <= 0:
            return

        for row in db.by_state(self.conn, [db.QUEUED]):
            if slots <= 0 or self.stop_event.is_set():
                return
            if not self._retry_due(row):
                continue

            source = Path(row["source_file"])
            if not source.is_file():
                self._fail(row, "claimed .torrent file has gone missing")
                continue

            try:
                torrent_id = self.client.add(
                    source,
                    download_dir=self.cfg.paths.temp_download,
                    peer_limit=self.cfg.limits.per_torrent_peer_limit,
                    download_limit_kbps=self.cfg.limits.per_torrent_download_kbps,
                )
            except PermanentClientError as exc:
                self._fail(row, str(exc))
                continue
            except TransientClientError as exc:
                self._defer(row, str(exc))
                continue

            db.update(
                self.conn,
                row["infohash"],
                state=db.DOWNLOADING,
                transmission_id=torrent_id,
                temp_path=str(self.cfg.paths.temp_download),
                attempts=0,
                last_error=None,
                retry_after=None,
                # Magnets start the metadata timeout clock here.
                stalled_since=db.now() if row["size_bytes"] is None else None,
            )
            log.info("downloading %s (transmission id %d)", row["name"], torrent_id)
            slots -= 1

    # -- stage 3: reconcile -------------------------------------------

    def _reconcile(self) -> None:
        rows = db.by_state(self.conn, [db.DOWNLOADING])
        if not rows:
            return
        by_id = {int(r["transmission_id"]): r for r in rows if r["transmission_id"]}
        if not by_id:
            return

        try:
            torrents = self.client.get(list(by_id))
        except TransientClientError as exc:
            if not self._warned_offline:
                log.warning("%s", exc)
                self._warned_offline = True
            return
        self._warned_offline = False

        seen: set[int] = set()
        for torrent in torrents:
            tid = int(torrent.id)
            seen.add(tid)
            row = by_id[tid]
            if row["size_bytes"] is None and self._metadata_timed_out(row, torrent):
                continue
            status = str(getattr(torrent, "status", "")).lower()
            percent = float(getattr(torrent, "percent_done", 0.0) or 0.0)
            error_code = int(getattr(torrent, "error", 0) or 0)

            if error_code:
                message = str(getattr(torrent, "error_string", "") or "tracker error")
                if row["last_error"] != message:
                    log.warning("%s reports: %s", row["name"], message)
                    db.update(self.conn, row["infohash"], last_error=message)
                continue

            if percent >= 1.0:
                db.update(self.conn, row["infohash"], state=db.COMPLETE,
                          last_error=None)
                log.info("download complete: %s", row["name"])
                continue

            if "stop" in status:
                log.info("restarting paused torrent %s", row["name"])
                try:
                    self.client.start(tid)
                except (TransientClientError, PermanentClientError) as exc:
                    log.warning("could not restart %s: %s", row["name"], exc)

        for tid in set(by_id) - seen:
            row = by_id[tid]
            self._fail(row, "torrent is no longer present in Transmission")

    def _metadata_timed_out(self, row, torrent) -> bool:
        """For a magnet: record the metadata once it arrives, or give up on it.

        Returns True if the torrent was failed and needs no further handling.
        """
        if float(getattr(torrent, "metadata_percent_complete", 1.0) or 0.0) >= 1.0:
            name = str(getattr(torrent, "name", "") or row["name"])
            size = int(getattr(torrent, "total_size", 0) or 0)
            count = len(torrent.get_files())
            db.update(self.conn, row["infohash"], name=name, size_bytes=size,
                      file_count=count, stalled_since=None)
            log.info("metadata received for %s: %.2f GiB, %d file(s)",
                     name, size / 1024**3, count)
            return False

        since = row["stalled_since"]
        if since is None:
            db.update(self.conn, row["infohash"], stalled_since=db.now())
            return False
        waited = datetime.now(timezone.utc) - datetime.fromisoformat(since)
        limit = timedelta(minutes=self.cfg.service.metadata_timeout_minutes)
        if waited < limit:
            return False

        try:
            # Nothing has been downloaded yet, so there is no data to keep.
            self.client.remove(int(torrent.id), delete_data=True)
        except TransientClientError as exc:
            log.warning("could not remove stalled magnet %s yet: %s", row["name"], exc)
            return True
        except PermanentClientError as exc:
            log.warning("could not remove stalled magnet %s: %s", row["name"], exc)
        self._fail(row, f"no metadata from peers after "
                        f"{self.cfg.service.metadata_timeout_minutes} minutes -- the "
                        f"magnet may be dead. Re-drop it to try again.")
        return True

    # -- stage 4: file ------------------------------------------------

    def _file(self) -> None:
        """File each completed download according to its category's policy.

        The download is classified (video, music, iso, other) by its files.
        copy: the chosen files are copied and the torrent keeps seeding until
        cleanup ends it. move: the torrent is removed from Transmission and
        its files are moved -- no seeding, no duplicate.

        FILING is written before the first file is touched, and a move's plan
        is recorded before the torrent is removed, so a crash anywhere
        resumes here on restart without copying or moving anything twice.
        """
        for row in db.by_state(self.conn, [db.COMPLETE, db.FILING]):
            if self.stop_event.is_set():
                return
            if not self._retry_due(row):
                continue

            try:
                found = self.client.get([int(row["transmission_id"])])
            except TransientClientError as exc:
                log.warning("cannot file %s yet: %s", row["name"], exc)
                return
            if not found:
                if self._move_in_progress(row):
                    self._finish_move(row)  # removed from Transmission mid-move
                else:
                    self._review(row, "torrent left Transmission before it was filed; "
                                      "nothing was copied")
                continue
            torrent = found[0]

            files = [TorrentFile(name=f.name, size=f.size, completed=f.completed)
                     for f in torrent.get_files() if f.selected]
            category = classify(files)
            policy = self.cfg.filing.policy(category)
            root = self.cfg.root_for(category)
            moving = policy.mode == cfgmod.MOVE
            db.update(self.conn, row["infohash"], state=db.FILING, category=category)

            try:
                items = plan(
                    files,
                    download_dir=Path(torrent.download_dir),
                    library_root=root,
                    min_free_bytes=int(self.cfg.filing.min_free_gb * 1024**3),
                    category=category,
                    move=moving,
                )
                if not items:
                    self._review(row, f"classified as {category} but no files to file; "
                                      f"left in Transmission untouched")
                    continue

                db.clear_files(self.conn, row["infohash"])
                if moving:
                    for item in items:  # the plan, recorded before anything moves
                        db.record_file(self.conn, row["infohash"], str(item.src),
                                       str(item.dst), verified=False)
                    # Transmission must let go of the files before they move.
                    self.client.remove(int(torrent.id), delete_data=False)
                else:
                    for item in items:
                        if self.stop_event.is_set():
                            return  # still FILING; resumes on next start
                        copy_verified(item)
                        db.record_file(self.conn, row["infohash"], str(item.src),
                                       str(item.dst))
            except FilingError as exc:
                self._review(row, str(exc))
                continue
            except RetryableFilingError as exc:
                if isinstance(exc, LowSpaceError):
                    self._space_short = True
                self._defer(row, str(exc), give_up=self._review)
                continue
            except TransientClientError as exc:
                self._defer(row, str(exc), give_up=self._review)
                continue
            except PermanentClientError as exc:
                self._review(row, str(exc))
                continue

            target = root / items[0].dst.relative_to(root).parts[0]
            db.update(self.conn, row["infohash"], target_path=str(target))
            if moving:
                self._finish_move(db.get(self.conn, row["infohash"]))
                continue

            db.update(self.conn, row["infohash"], state=db.FILED, filed_at=db.now(),
                      attempts=0, last_error=None, retry_after=None,
                      seed_mark=_uploaded(torrent), seed_mark_at=db.now())
            copied = sum(1 for item in items if not item.already_present)
            log.info("filed %s [%s] -> %s (%d file(s) copied, %d already present)",
                     row["name"], category, target, copied, len(items) - copied)
            self._park_source(row, cfgmod.DONE)

    def _move_in_progress(self, row) -> bool:
        return (row["state"] == db.FILING and row["category"] is not None
                and self.cfg.filing.policy(row["category"]).mode == cfgmod.MOVE
                and bool(db.files_for(self.conn, row["infohash"])))

    def _finish_move(self, row) -> None:
        """Move every recorded file not yet moved. Safe to repeat after a crash."""
        moved = 0
        try:
            for f in db.files_for(self.conn, row["infohash"]):
                if f["verified"]:
                    continue
                src, dst = Path(f["src"]), Path(f["dst"])
                if not src.exists():
                    if dst.is_file():  # moved just before a crash
                        db.record_file(self.conn, row["infohash"], f["src"], f["dst"])
                        continue
                    raise FilingError(f"{src} vanished before it could be moved")
                size = src.stat().st_size
                present = dst.exists()
                if present and (not dst.is_file() or dst.stat().st_size != size):
                    raise FilingError(f"{dst} already exists and differs -- not overwriting")
                move_verified(CopyItem(src=src, dst=dst, size=size, already_present=present))
                db.record_file(self.conn, row["infohash"], f["src"], f["dst"])
                moved += 1
        except FilingError as exc:
            self._review(row, f"{exc}; already removed from Transmission, remaining "
                              f"files are still in {self.cfg.paths.temp_download}")
            return
        except RetryableFilingError as exc:
            self._defer(row, str(exc), give_up=self._review)
            return

        first = Path(db.files_for(self.conn, row["infohash"])[0]["src"])
        temp = self.cfg.paths.temp_download
        try:
            prune_empty_dirs(temp / first.relative_to(temp).parts[0], stop_at=temp)
        except ValueError:
            pass  # not under temp_download; nothing of ours to tidy
        db.update(self.conn, row["infohash"], state=db.PURGED, filed_at=db.now(),
                  attempts=0, last_error=None, retry_after=None)
        log.info("moved %s [%s] -> %s (%d file(s)); removed from Transmission, "
                 "no seeding", row["name"], row["category"], row["target_path"], moved)
        self._park_source(row, cfgmod.DONE)

    # -- stage 5: clean up --------------------------------------------

    def _cleanup(self) -> None:
        """Remove filed torrents from Transmission once they have seeded enough.

        When disk space runs short -- a copy into the library would not fit,
        or the download volume is below min_free_gb -- every filed torrent is
        removed at once, seeded enough or not. Their temp copies are pure
        duplicates of verified library copies, so deleting them is the one
        safe way to make room for new downloads.
        """
        low_space = self._space_short or self._temp_space_low()
        self._space_short = False
        rows = db.by_state(self.conn, [db.FILED])
        if not rows:
            return
        if low_space:
            log.warning("disk space is low; ending seeding for %d filed torrent(s) "
                        "to free their temp copies", len(rows))
        by_id = {int(r["transmission_id"]): r for r in rows if r["transmission_id"]}
        if not by_id:
            return

        try:
            torrents = self.client.get(list(by_id))
        except TransientClientError:
            return  # reconcile already warns about an unreachable daemon

        seen: set[int] = set()
        for torrent in torrents:
            tid = int(torrent.id)
            seen.add(tid)
            row = by_id[tid]
            reason = "low disk space" if low_space else self._seeding_done(row, torrent)
            if reason is None or self.stop_event.is_set():
                continue

            problem = self._purge_blocker(row, torrent)
            if problem:
                self._review(row, f"{problem}; temp copy kept")
                continue

            try:
                self.client.remove(tid, delete_data=True)
            except TransientClientError as exc:
                log.warning("could not remove %s yet: %s", row["name"], exc)
                continue
            except PermanentClientError as exc:
                self._review(row, str(exc))
                continue
            db.update(self.conn, row["infohash"], state=db.PURGED, last_error=None)
            log.info("seeding done for %s (%s); removed from Transmission and "
                     "deleted temp copy", row["name"], reason)

        for tid in set(by_id) - seen:
            row = by_id[tid]
            db.update(self.conn, row["infohash"], state=db.PURGED,
                      last_error="removed from Transmission outside torrentfiler; "
                                 "temp files left as they were")
            log.info("%s was removed from Transmission by hand; marking purged",
                     row["name"])

    def _temp_space_low(self) -> bool:
        try:
            free = free_bytes(self.cfg.paths.temp_download)
        except OSError as exc:
            log.warning("cannot check free space on %s: %s",
                        self.cfg.paths.temp_download, exc)
            return False
        return free < self.cfg.filing.min_free_gb * 1024**3

    def _seeding_done(self, row, torrent) -> str | None:
        """Why seeding should end now, or None to keep seeding.

        Per the category's policy: the ratio target, the seed time, or --
        for every category -- nothing uploaded for stall_fraction of the
        seed time (e.g. 30% of 10 days = 72 hours), which skips the rest.
        """
        policy = self.cfg.filing.policy(row["category"])
        now = datetime.now(timezone.utc)
        ratio = float(getattr(torrent, "ratio", 0.0) or 0.0)
        if policy.seed_ratio and ratio >= policy.seed_ratio:
            return f"ratio {ratio:.2f}"
        if not policy.seed_ratio and not policy.seed_hours:
            return "this category doesn't seed"

        # Age counts from when the download finished; fall back to when it
        # was filed if Transmission did not record a completion time.
        finished = getattr(torrent, "done_date", None)
        if finished is None and row["filed_at"]:
            finished = datetime.fromisoformat(row["filed_at"])
        if policy.seed_hours and finished is not None:
            hours = (now - finished).total_seconds() / 3600
            if hours >= policy.seed_hours:
                return f"seeded {_hours(hours)}"

        window = self.cfg.filing.stall_fraction * policy.seed_hours
        if window:
            uploaded = _uploaded(torrent)
            mark, mark_at = row["seed_mark"], row["seed_mark_at"]
            if mark is None or mark_at is None or uploaded > mark:
                db.update(self.conn, row["infohash"], seed_mark=uploaded,
                          seed_mark_at=now.isoformat(timespec="seconds"))
            else:
                idle = (now - datetime.fromisoformat(mark_at)).total_seconds() / 3600
                if idle >= window:
                    return (f"nothing uploaded for {_hours(idle)} "
                            f"({self.cfg.filing.stall_fraction:.0%} of the seed time)")
        return None

    def _purge_blocker(self, row, torrent) -> str | None:
        """Anything that makes deleting the temp copy unsafe."""
        temp_root = self.cfg.paths.temp_download.resolve()
        download_dir = Path(torrent.download_dir).resolve()
        if download_dir != temp_root and temp_root not in download_dir.parents:
            return f"download is in {download_dir}, outside temp_download"
        recorded = db.files_for(self.conn, row["infohash"])
        if not recorded:
            return "no record of which files were copied"
        for f in recorded:
            if not library_copy_intact(Path(f["dst"]), Path(f["src"])):
                return f"copy {f['dst']} is missing or changed"
        return None

    # -- failure handling ---------------------------------------------

    def _retry_due(self, row) -> bool:
        if not row["retry_after"]:
            return True
        try:
            due = datetime.fromisoformat(row["retry_after"])
        except ValueError:
            return True
        return datetime.now(timezone.utc) >= due

    def _defer(self, row, message: str, give_up=None) -> None:
        attempts = int(row["attempts"]) + 1
        if attempts >= self.cfg.service.max_attempts:
            # Persist the final count so the record agrees with the message.
            db.update(self.conn, row["infohash"], attempts=attempts)
            (give_up or self._fail)(row, f"{message} (gave up after {attempts} attempts)")
            return
        delay = min(
            self.cfg.service.retry_max_seconds,
            self.cfg.service.retry_base_seconds * (2 ** (attempts - 1)),
        )
        due = datetime.now(timezone.utc) + timedelta(seconds=delay)
        db.update(
            self.conn,
            row["infohash"],
            attempts=attempts,
            last_error=message,
            retry_after=due.isoformat(timespec="seconds"),
        )
        log.info("deferring %s for %ds: %s", row["name"], delay, message)

    def _fail(self, row, message: str) -> None:
        db.update(self.conn, row["infohash"], state=db.FAILED, last_error=message)
        log.error("failed %s: %s", row["name"], message)
        self._park_source(row, cfgmod.FAILED,
                          note=f"{row['name']}\n{row['infohash']}\n\n{message}\n")

    def _review(self, row, message: str) -> None:
        """Stop automatic handling. The torrent stays in Transmission as it is;
        dropping its .torrent into force/ runs it through again."""
        db.update(self.conn, row["infohash"], state=db.NEEDS_REVIEW, last_error=message)
        log.error("needs review: %s: %s", row["name"], message)
        self._park_source(row, cfgmod.FAILED,
                          note=f"{row['name']}\n{row['infohash']}\n\n"
                               f"NEEDS REVIEW: {message}\n")

    def _park_source(self, row, folder: str, note: str | None = None) -> None:
        source = Path(row["source_file"])
        if source.is_file() and source.parent.name == cfgmod.PROCESSING:
            try:
                moved = self.watcher.park(source, folder, note=note)
                db.update(self.conn, row["infohash"], source_file=str(moved))
            except OSError as exc:
                log.warning("could not move %s to %s/: %s", source.name, folder, exc)


def _uploaded(torrent) -> float:
    """Bytes uploaded so far, for stall detection. Falls back to the ratio,
    which moves whenever uploads happen, if the byte count isn't available."""
    value = getattr(torrent, "uploaded_ever", None)
    if value is None:
        value = getattr(torrent, "ratio", 0.0)
    return float(value or 0)


def _hours(hours: float) -> str:
    return f"{hours:.1f} hours" if hours < 48 else f"{hours / 24:.1f} days"


def _read_source(path: Path) -> TorrentMeta:
    """Identity and size of a dropped .torrent or .magnet.

    A magnet has no size or file count until Transmission fetches its
    metadata, so those are None until reconcile fills them in.
    """
    if path.suffix.lower() == magnet.SUFFIX:
        link = magnet.read(path)
        return TorrentMeta(infohash=link.infohash, name=link.name,
                           size_bytes=None, file_count=None)
    return read_torrent(path)
