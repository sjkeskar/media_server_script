"""Per-device API tokens.

Each laptop, phone and tablet has its own token, so every add can be traced
to a device and one device can be revoked without touching the others.

The file holds only SHA-256 hashes of tokens: a copied file can't be used to
add torrents. Tokens are random 256-bit values, so a plain (unsalted) hash
is enough. Nothing can be brute-forced back out of it.

The file is edited by the --api-add-device / --api-revoke-device commands
and only read by the API, which reloads it whenever it changes, so revoking
takes effect without a restart. Usage (last seen, add counts) is derived from
the audit log rather than stored here, so the API never writes this file.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

NAME = re.compile(r"^(phone|tab|laptop)-[a-z0-9]+(-[a-z0-9]+)*$")
NAME_HELP = ("device names are <kind>-<owner or room>, kind one of phone, tab, laptop; "
             "lowercase letters, digits and dashes (e.g. phone-sj, tab-livingroom)")


class DeviceError(ValueError):
    """Invalid device name, duplicate device, or unknown device."""


@dataclass(frozen=True)
class Device:
    name: str
    token_sha256: str
    created: str
    revoked: str | None

    @property
    def active(self) -> bool:
        return self.revoked is None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: Path) -> list[Device]:
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Device(**d) for d in raw.get("devices", [])]


def _save(path: Path, devices: list[Device]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"version": 1, "devices": [d.__dict__ for d in devices]},
                               indent=2), encoding="utf-8")
    os.replace(temp, path)


def add(path: Path, name: str) -> str:
    """Create a device and return its token. The token is never stored."""
    if not NAME.match(name):
        raise DeviceError(NAME_HELP)
    devices = load(path)
    if any(d.name == name and d.active for d in devices):
        raise DeviceError(f"{name} already has an active token; revoke it first")
    token = secrets.token_urlsafe(32)
    devices.append(Device(name=name, token_sha256=_hash(token), created=_now(), revoked=None))
    _save(path, devices)
    return token


def revoke(path: Path, name: str) -> None:
    devices = load(path)
    if not any(d.name == name and d.active for d in devices):
        raise DeviceError(f"no active device named {name}")
    _save(path, [Device(d.name, d.token_sha256, d.created, _now())
                 if d.name == name and d.active else d for d in devices])


class Registry:
    """Token verification for the API, reloading the file when it changes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._devices: list[Device] = []

    def verify(self, token: str | None) -> tuple[str, str | None]:
        """Returns (status, device name): ("ok", name), ("revoked", name)
        or ("unknown", None)."""
        devices = self._current()
        if not token:
            return "unknown", None
        digest = _hash(token)
        match = None
        # Compare against every device, in constant time, without stopping
        # early, so timing reveals nothing about which hashes exist.
        for device in devices:
            if hmac.compare_digest(device.token_sha256, digest):
                match = device
        if match is None:
            return "unknown", None
        return ("ok" if match.active else "revoked"), match.name

    def _current(self) -> list[Device]:
        try:
            stat = self._path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            stamp = None
        with self._lock:
            if stamp != self._stamp:
                self._devices = load(self._path) if stamp else []
                self._stamp = stamp
            return self._devices
