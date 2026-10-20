"""Minimal bencode reader and .torrent metadata extraction.

The infohash must be the SHA-1 of the *original* bytes of the info
dictionary, not of a re-encoding. Re-encoding is canonical only if the
file happened to be canonical to begin with, and plenty of real-world
torrents are not. So the decoder records the byte span of each
top-level value and we hash the original slice.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


class BencodeError(ValueError):
    """The file is not valid bencode, or not a valid torrent."""


class _Decoder:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.depth = 0
        self.top_level_spans: dict[bytes, tuple[int, int]] = {}

    def decode(self):
        value = self._value()
        return value

    def _peek(self) -> bytes:
        if self.pos >= len(self.data):
            raise BencodeError("unexpected end of data")
        return self.data[self.pos : self.pos + 1]

    def _value(self):
        char = self._peek()
        if char == b"d":
            return self._dict()
        if char == b"l":
            return self._list()
        if char == b"i":
            return self._int()
        if char.isdigit():
            return self._bytes()
        raise BencodeError(f"unexpected byte {char!r} at offset {self.pos}")

    def _int(self):
        end = self.data.find(b"e", self.pos)
        if end == -1:
            raise BencodeError("unterminated integer")
        raw = self.data[self.pos + 1 : end]
        self.pos = end + 1
        try:
            return int(raw)
        except ValueError as exc:
            raise BencodeError(f"bad integer {raw!r}") from exc

    def _bytes(self):
        sep = self.data.find(b":", self.pos)
        if sep == -1:
            raise BencodeError("unterminated byte string length")
        try:
            length = int(self.data[self.pos : sep])
        except ValueError as exc:
            raise BencodeError("bad byte string length") from exc
        if length < 0:
            raise BencodeError("negative byte string length")
        start = sep + 1
        end = start + length
        if end > len(self.data):
            raise BencodeError("byte string runs past end of data")
        self.pos = end
        return self.data[start:end]

    def _list(self):
        self.pos += 1
        self.depth += 1
        out = []
        while self._peek() != b"e":
            out.append(self._value())
        self.pos += 1
        self.depth -= 1
        return out

    def _dict(self):
        self.pos += 1
        self.depth += 1
        out: dict[bytes, object] = {}
        while self._peek() != b"e":
            key = self._value()
            if not isinstance(key, bytes):
                raise BencodeError("dictionary key is not a byte string")
            value_start = self.pos
            value = self._value()
            if self.depth == 1:
                self.top_level_spans[key] = (value_start, self.pos)
            out[key] = value
        self.pos += 1
        self.depth -= 1
        return out


@dataclass(frozen=True)
class TorrentMeta:
    infohash: str
    name: str
    # None for a magnet link until its metadata arrives.
    size_bytes: int | None
    file_count: int | None

    @property
    def short(self) -> str:
        return self.infohash[:8]


def _decode_name(info: dict) -> str:
    raw = info.get(b"name.utf-8") or info.get(b"name")
    if not isinstance(raw, bytes):
        raise BencodeError("torrent info has no usable name")
    return raw.decode("utf-8", errors="replace")


def _total_size(info: dict) -> tuple[int, int]:
    if b"length" in info:
        length = info[b"length"]
        if not isinstance(length, int):
            raise BencodeError("info.length is not an integer")
        return length, 1
    files = info.get(b"files")
    if not isinstance(files, list) or not files:
        raise BencodeError("torrent info has neither length nor files")
    total = 0
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get(b"length"), int):
            raise BencodeError("malformed entry in info.files")
        total += entry[b"length"]
    return total, len(files)


def read_torrent(path: Path) -> TorrentMeta:
    """Parse a .torrent file. Raises BencodeError on anything malformed."""
    return parse_torrent(path.read_bytes())


def parse_torrent(data: bytes) -> TorrentMeta:
    """Parse .torrent bytes, e.g. an upload. Raises BencodeError if malformed."""
    if not data:
        raise BencodeError("file is empty")

    decoder = _Decoder(data)
    root = decoder.decode()
    if not isinstance(root, dict):
        raise BencodeError("torrent root is not a dictionary")

    span = decoder.top_level_spans.get(b"info")
    if span is None:
        raise BencodeError("torrent has no info dictionary")
    infohash = hashlib.sha1(data[span[0] : span[1]]).hexdigest()

    info = root.get(b"info")
    if not isinstance(info, dict):
        raise BencodeError("info is not a dictionary")

    size_bytes, file_count = _total_size(info)
    return TorrentMeta(
        infohash=infohash,
        name=_decode_name(info),
        size_bytes=size_bytes,
        file_count=file_count,
    )
