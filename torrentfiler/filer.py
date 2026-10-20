"""Phase 2 filing: classify a download, choose its files, then copy or move them.

Every download is classified by its contents (video, music, iso, other); the
category's policy in config decides the destination and whether files are
copied (the torrent keeps seeding from the temp folder) or moved (no
seeding, no duplicate). A copied download's temp copy is only deleted later,
by the cleanup stage, once seeding is done and every copy has been re-checked.

Planning is separate from copying so every problem that can be detected
up front (a conflicting file already in the library, too little free space,
a path that would escape the library) is found before a single byte is
written.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

log = logging.getLogger(__name__)

VIDEO = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".mts",
    ".webm", ".mpg", ".mpeg", ".flv", ".vob",
}
SUBTITLE = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup", ".smi"}
IMAGE = {".jpg", ".jpeg", ".png", ".webp", ".tbn"}
AUDIO = {
    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".wma",
    ".alac", ".ape", ".wv", ".aiff", ".aif", ".dsf", ".dff", ".mka",
}
# Kept alongside music: lyrics and CUE sheets (track splits for single-file rips).
MUSIC_EXTRAS = {".lrc", ".cue"}
DISK_IMAGE = {".iso", ".img"}

# Images are only artwork if named like it -- release screenshots are not.
_ARTWORK = re.compile(
    r"(^|[\W_])(poster|cover|folder|fanart|backdrop|background|banner|logo|"
    r"clearlogo|clearart|landscape|thumb|disc|discart|front|back|albumart\w*|"
    r"artwork|cd)([\W_]|$)",
    re.IGNORECASE,
)
_SAMPLE = re.compile(r"(^|[\W_])sample([\W_]|$)", re.IGNORECASE)
_SKIP_DIRS = {"sample", "samples", "screens", "screenshots", "proof", "extras-screens"}

PART_SUFFIX = ".part"


class FilingError(Exception):
    """Filing cannot proceed without a human looking at it."""


class RetryableFilingError(Exception):
    """Filing hit something that may clear up on its own (space, locks)."""


class LowSpaceError(RetryableFilingError):
    """Not enough room to copy; freeing seeding temp copies may fix it."""


@dataclass(frozen=True)
class TorrentFile:
    """One file as Transmission reports it: path relative to download_dir."""

    name: str
    size: int
    completed: int


@dataclass(frozen=True)
class CopyItem:
    src: Path
    dst: Path
    size: int
    already_present: bool


def _junk(relative: str) -> tuple[bool, PurePosixPath]:
    """(is it a sample or screenshot, its file name)"""
    parts = PurePosixPath(relative.replace("\\", "/")).parts
    name = PurePosixPath(parts[-1])
    skip = (any(part.lower() in _SKIP_DIRS for part in parts[:-1])
            or bool(_SAMPLE.search(name.stem)))
    return skip, name


def is_wanted(relative: str, category: str = "video") -> bool:
    """Whether a file is copied for this category.

    video: video, subtitles and cover artwork.
    music: audio, lyrics, CUE sheets and cover artwork.
    Both skip samples and screenshots. iso and other take every file.
    """
    if category in ("iso", "other"):
        return True
    skip, name = _junk(relative)
    if skip:
        return False
    suffix = name.suffix.lower()
    if category == "music":
        if suffix in AUDIO or suffix in MUSIC_EXTRAS:
            return True
    elif suffix in VIDEO or suffix in SUBTITLE:
        return True
    if suffix in IMAGE:
        return bool(_ARTWORK.search(name.stem))
    return False


def classify(files: Iterable[TorrentFile]) -> str:
    """video if it has a (non-sample) video file, else music if it has audio,
    else iso if it has a disk image, else other."""
    found = set()
    for f in files:
        skip, name = _junk(f.name)
        suffix = name.suffix.lower()
        if suffix in VIDEO and not skip:
            return "video"
        if suffix in AUDIO and not skip:
            found.add("music")
        elif suffix in DISK_IMAGE:
            found.add("iso")
    for category in ("music", "iso"):
        if category in found:
            return category
    return "other"


def library_relative(name: str, single_file: bool) -> PurePosixPath:
    """Where a torrent file lands, relative to the library root.

    Multi-file torrents already have a top-level folder (the release name)
    and keep their structure. A single-file torrent is wrapped in a folder
    named after the file, so every download gets its own folder.
    """
    rel = PurePosixPath(name.replace("\\", "/"))
    if single_file:
        return PurePosixPath(rel.stem) / rel.name
    return rel


def plan(
    files: Iterable[TorrentFile],
    *,
    download_dir: Path,
    library_root: Path,
    min_free_bytes: int,
    category: str = "video",
    move: bool = False,
) -> list[CopyItem]:
    """Work out every copy (or move) and check it is safe. Writes nothing.

    library_root is the category's destination folder.
    """
    files = list(files)
    single_file = len(files) == 1 and "/" not in files[0].name.replace("\\", "/")
    library = library_root.resolve()
    items: list[CopyItem] = []

    for f in files:
        if not is_wanted(f.name, category):
            continue
        if f.completed < f.size:
            raise FilingError(f"{f.name} is incomplete ({f.completed}/{f.size} bytes)")

        src = download_dir / f.name
        dst = library_root / library_relative(f.name, single_file)
        if library not in dst.resolve().parents:
            raise FilingError(f"{f.name} would land outside {library_root}")

        try:
            src_size = src.stat().st_size
        except FileNotFoundError as exc:
            # Gone, not locked: retrying can't bring it back. Transmission
            # still says "complete" because it only notices on its next read.
            raise FilingError(
                f"{src} is missing from the download folder (deleted outside "
                f"torrentfiler?). Restore it and --refile, or Verify the torrent in "
                f"Transmission to download it again, or remove the torrent") from exc
        except OSError as exc:
            raise RetryableFilingError(f"cannot read {src}: {exc}") from exc
        if src_size != f.size:
            raise FilingError(f"{src} is {src_size} bytes, expected {f.size}")

        present = False
        if dst.exists():
            if dst.is_file() and dst.stat().st_size == f.size:
                present = True
            else:
                raise FilingError(f"{dst} already exists and differs -- not overwriting")
        items.append(CopyItem(src=src, dst=dst, size=f.size, already_present=present))

    needed = sum(item.size for item in items if not item.already_present)
    if move and same_volume(download_dir, library_root):
        needed = 0  # a move within one volume is a rename: no extra space
    if needed:
        free = free_bytes(library_root)
        if free - needed < min_free_bytes:
            raise LowSpaceError(
                f"not enough space: need {needed / 1024**3:.1f} GiB plus "
                f"{min_free_bytes / 1024**3:.1f} GiB headroom, "
                f"{free / 1024**3:.1f} GiB free"
            )
    return items


def copy_verified(item: CopyItem) -> None:
    """Copy to a .part file, check its size, then rename into place.

    A crash mid-copy leaves only a .part file, which the next attempt
    overwrites -- the real name never points at a partial file.
    """
    if item.already_present:
        return
    item.dst.parent.mkdir(parents=True, exist_ok=True)
    part = item.dst.with_name(item.dst.name + PART_SUFFIX)
    try:
        shutil.copyfile(item.src, part)
        shutil.copystat(item.src, part)
        copied = part.stat().st_size
        if copied != item.size:
            raise RetryableFilingError(
                f"copy of {item.src.name} is {copied} bytes, expected {item.size}"
            )
        os.replace(part, item.dst)
    except OSError as exc:
        raise RetryableFilingError(f"copying {item.src.name} failed: {exc}") from exc
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass


def move_verified(item: CopyItem) -> None:
    """Move a file into place, leaving no duplicate behind.

    Within one volume this is an atomic rename. Across volumes it is a
    verified copy (via .part) followed by deleting the source. If an
    identical file is already at the destination, the source is deleted.
    """
    try:
        if item.already_present:
            item.src.unlink()
            return
        item.dst.parent.mkdir(parents=True, exist_ok=True)
        if same_volume(item.src.parent, item.dst.parent):
            os.replace(item.src, item.dst)
            return
    except OSError as exc:
        raise RetryableFilingError(f"moving {item.src.name} failed: {exc}") from exc
    copy_verified(item)
    try:
        item.src.unlink()
    except OSError as exc:
        raise RetryableFilingError(f"copied {item.src.name} but could not delete the "
                                   f"original: {exc}") from exc


def same_volume(a: Path, b: Path) -> bool:
    return (_existing_ancestor(a).resolve().anchor.lower()
            == _existing_ancestor(b).resolve().anchor.lower())


def prune_empty_dirs(top: Path, stop_at: Path) -> None:
    """After a move, remove the torrent's now-empty folders under stop_at."""
    stop_at = stop_at.resolve()
    try:
        top = top.resolve()
    except OSError:
        return
    if stop_at not in top.parents or not top.is_dir():
        return
    folders = sorted((p for p in top.rglob("*") if p.is_dir()),
                     key=lambda p: len(p.parts), reverse=True)
    for folder in [*folders, top]:
        try:
            folder.rmdir()  # only succeeds when empty
        except OSError:
            pass


def library_copy_intact(dst: Path, src: Path) -> bool:
    """Before deleting the temp copy: is the library copy still there and whole?"""
    try:
        return dst.is_file() and dst.stat().st_size == src.stat().st_size
    except OSError:
        return False


def free_bytes(path: Path) -> int:
    """Free space on the volume holding path (which need not exist yet)."""
    return shutil.disk_usage(_existing_ancestor(path)).free


def _existing_ancestor(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return path
