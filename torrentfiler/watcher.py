"""Watch the drop folders for .torrent files that have finished being written.

Polling rather than filesystem events on purpose: Windows event
notification fires on the first write, not the last, and fires
unreliably for files arriving over SMB. A size-and-mtime stability check
across consecutive polls is boring and correct.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import DROP_FOLDERS, FORCE, FORCE_DUP, Paths

log = logging.getLogger(__name__)

# What can be dropped: a .torrent file, or a .magnet text file holding a link.
SOURCE_SUFFIXES = (".torrent", ".magnet")


@dataclass(frozen=True)
class Candidate:
    path: Path
    force_download: bool
    force_dup: bool

    @property
    def origin(self) -> str:
        return self.path.parent.name


class Watcher:
    def __init__(self, paths: Paths, stability_checks: int) -> None:
        self._paths = paths
        self._required = max(1, stability_checks)
        # path -> (size, mtime_ns, consecutive_unchanged_polls)
        self._seen: dict[Path, tuple[int, int, int]] = {}

    def scan(self) -> list[Candidate]:
        """Return candidates whose size and mtime have held steady."""
        ready: list[Candidate] = []
        present: set[Path] = set()

        for folder_name in DROP_FOLDERS:
            folder = self._paths.folder(folder_name)
            if not folder.is_dir():
                continue
            for entry in sorted(folder.iterdir()):
                if not entry.is_file() or entry.suffix.lower() not in SOURCE_SUFFIXES:
                    continue
                present.add(entry)
                if self._is_stable(entry):
                    ready.append(
                        Candidate(
                            path=entry,
                            force_download=folder_name in (FORCE, FORCE_DUP),
                            force_dup=folder_name == FORCE_DUP,
                        )
                    )

        # Forget files that have left the drop folders.
        for gone in set(self._seen) - present:
            self._seen.pop(gone, None)

        return ready

    def _is_stable(self, path: Path) -> bool:
        try:
            stat = path.stat()
        except OSError:
            # Vanished or locked mid-scan; try again next poll.
            self._seen.pop(path, None)
            return False

        if stat.st_size == 0:
            return False

        previous = self._seen.get(path)
        signature = (stat.st_size, stat.st_mtime_ns)
        if previous is not None and previous[:2] == signature:
            count = previous[2] + 1
        else:
            count = 1
        self._seen[path] = (*signature, count)
        return count >= self._required

    def forget(self, path: Path) -> None:
        self._seen.pop(path, None)

    # -- moving files out of the drop folders -------------------------

    def claim(self, path: Path, infohash: str) -> Path:
        """Move a parsed .torrent or .magnet into processing/, named by infohash."""
        from .config import PROCESSING

        target = self._paths.folder(PROCESSING) / f"{infohash}{path.suffix.lower()}"
        moved = _move(path, target)
        self.forget(path)
        return moved

    def park(self, path: Path, folder_name: str, note: str | None = None) -> Path:
        """Move a .torrent to done/, skipped/ or failed/, optionally with a note."""
        target = self._paths.folder(folder_name) / path.name
        moved = _move(path, target)
        self.forget(path)
        if note:
            try:
                moved.with_suffix(moved.suffix + ".error.txt").write_text(
                    note, encoding="utf-8"
                )
            except OSError as exc:
                log.warning("could not write note beside %s: %s", moved.name, exc)
        return moved


def _unique(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix, parent = target.stem, target.suffix, target.parent
    for n in range(1, 1000):
        candidate = parent / f"{stem}.{n}{suffix}"
        if not candidate.exists():
            return candidate
    raise OSError(f"cannot find a free name near {target}")


def _move(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    final = _unique(target)
    shutil.move(str(source), str(final))
    return final
