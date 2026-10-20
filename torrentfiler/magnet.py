"""Magnet links: parsing, .magnet drop files, and the Windows link handler.

A .magnet file is a text file holding one magnet link. It plays the same
role as a .torrent in the drop folders, so dedup, claiming, crash recovery
and quarantine all work unchanged. Clicking a magnet link in a browser
writes one for you once torrentfiler is registered as the magnet: handler.

A magnet carries only the infohash and, usually, a display name. Size and
file list arrive later, once Transmission fetches the metadata from peers.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SUFFIX = ".magnet"

_HEX = re.compile(r"^[0-9a-fA-F]{40}$")
_BASE32 = re.compile(r"^[A-Za-z2-7]{32}$")
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


class MagnetError(ValueError):
    """Not a usable magnet link."""


@dataclass(frozen=True)
class Magnet:
    uri: str
    infohash: str
    display_name: str | None

    @property
    def short(self) -> str:
        return self.infohash[:8]

    @property
    def name(self) -> str:
        """Best name available before metadata arrives."""
        return self.display_name or f"magnet {self.short}"


def parse(uri: str) -> Magnet:
    uri = uri.strip()
    parts = urlsplit(uri)
    if parts.scheme.lower() != "magnet":
        raise MagnetError("not a magnet: link")
    params = parse_qs(parts.query)

    infohash = None
    exact_topics = params.get("xt", [])
    for xt in exact_topics:
        if xt.lower().startswith("urn:btih:"):
            infohash = _btih_to_hex(xt[len("urn:btih:"):])
            break
    if infohash is None:
        if any(xt.lower().startswith("urn:btmh:") for xt in exact_topics):
            raise MagnetError("BitTorrent v2-only magnet links are not supported")
        raise MagnetError("magnet link has no urn:btih infohash")

    names = [n.strip() for n in params.get("dn", []) if n.strip()]
    return Magnet(uri=uri, infohash=infohash, display_name=names[0] if names else None)


def _btih_to_hex(value: str) -> str:
    if _HEX.match(value):
        return value.lower()
    if _BASE32.match(value):
        try:
            return base64.b32decode(value.upper()).hex()
        except binascii.Error as exc:
            raise MagnetError(f"bad base32 infohash {value!r}") from exc
    raise MagnetError(f"infohash {value!r} is neither 40 hex nor 32 base32 characters")


def read(path: Path) -> Magnet:
    """Parse a .magnet file: the first line that is a magnet link."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MagnetError("file is not UTF-8 text") from exc
    for line in text.splitlines():
        if line.strip().lower().startswith("magnet:"):
            return parse(line)
    raise MagnetError("file contains no magnet: link")


def write_drop_file(uri: str, folder: Path) -> Path:
    """Validate a link and write it into a drop folder as a .magnet file.

    Written under a temporary name and renamed, so the watcher never sees
    a half-written file.
    """
    magnet = parse(uri)
    return write_atomic(folder, drop_name(magnet.display_name or "magnet", magnet.short, SUFFIX),
                        (magnet.uri + "\n").encode("utf-8"))


def drop_name(name: str | None, short_hash: str, suffix: str) -> str:
    """A safe drop-file name: never contains path separators or reserved
    characters, whatever the (possibly hostile) display name says."""
    stem = _UNSAFE.sub("_", name or "").strip(" ._")[:80] or "torrent"
    return f"{stem} [{short_hash}]{suffix}"


def write_atomic(folder: Path, name: str, data: bytes) -> Path:
    """Write under a temporary name and rename, so the watcher never sees
    a half-written file."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    temp = target.with_suffix(".tmp")
    temp.write_bytes(data)
    os.replace(temp, target)
    return target


# -- Windows magnet: handler ------------------------------------------
#
# Two registrations, both per-user (HKCU, no admin rights):
#
# 1. The classic HKCU\Software\Classes\magnet command. Used when no default
#    has been picked in Windows Settings. It replaces whatever was there
#    (uTorrent on this PC), which is saved so unregistering can restore it.
# 2. A "registered application": a torrentfiler.magnet ProgId plus
#    Capabilities declaring it handles magnet:. This is what makes
#    torrentfiler appear in Settings > Default apps > by protocol, and what
#    browsers name in their "Open ...?" prompt. Other clients (Transmission's
#    installer) register the same way, so without it Windows and the browser
#    prefer them. Windows 10 hash-protects the user's default choice, so the
#    final pick in Settings has to be made by hand.

_ROOT = "Software"  # tests point this at a scratch key
_BACKUP_VALUE = "torrentfiler_previous_command"
PROGID = "torrentfiler.magnet"
APP_NAME = "torrentfiler"
_MARKER = '--add-magnet "%1"'


def _keys() -> dict[str, str]:
    return {
        "classic": rf"{_ROOT}\Classes\magnet",
        "classic_cmd": rf"{_ROOT}\Classes\magnet\shell\open\command",
        "progid": rf"{_ROOT}\Classes\{PROGID}",
        "app": rf"{_ROOT}\{APP_NAME}",
        "regapps": rf"{_ROOT}\RegisteredApplications",
    }


def handler_command(python: Path, launcher: Path, config: Path) -> str:
    return f'"{python}" "{launcher}" --config "{config}" {_MARKER}'


def is_ours(command: str) -> bool:
    """Re-registering must never save our own command as 'the previous one'."""
    return _MARKER in command


def register_handler(command: str) -> str | None:
    """Make clicked magnet links run `command`. Returns the classic handler replaced."""
    import winreg

    k = _keys()
    hkcu = winreg.HKEY_CURRENT_USER
    previous = current_handler()

    with winreg.CreateKey(hkcu, k["classic"]) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Magnet Protocol")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        if previous and not is_ours(previous):
            winreg.SetValueEx(key, _BACKUP_VALUE, 0, winreg.REG_SZ, previous)
    with winreg.CreateKey(hkcu, k["classic_cmd"]) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)

    with winreg.CreateKey(hkcu, k["progid"]) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "torrentfiler magnet link")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(hkcu, k["progid"] + r"\Application") as key:
        winreg.SetValueEx(key, "ApplicationName", 0, winreg.REG_SZ, APP_NAME)
    with winreg.CreateKey(hkcu, k["progid"] + r"\shell\open") as key:
        winreg.SetValueEx(key, "FriendlyAppName", 0, winreg.REG_SZ, APP_NAME)
    with winreg.CreateKey(hkcu, k["progid"] + r"\shell\open\command") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)

    with winreg.CreateKey(hkcu, k["app"] + r"\Capabilities") as key:
        winreg.SetValueEx(key, "ApplicationName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "ApplicationDescription", 0, winreg.REG_SZ,
                          "Queue magnet links for Transmission and file them into the library")
    with winreg.CreateKey(hkcu, k["app"] + r"\Capabilities\URLAssociations") as key:
        winreg.SetValueEx(key, "magnet", 0, winreg.REG_SZ, PROGID)
    with winreg.CreateKey(hkcu, k["regapps"]) as key:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, k["app"] + r"\Capabilities")
    return previous


def unregister_handler() -> str | None:
    """Remove both registrations and restore the previous classic handler."""
    import winreg

    k = _keys()
    hkcu = winreg.HKEY_CURRENT_USER
    previous = None
    try:
        with winreg.OpenKey(hkcu, k["classic"], 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            try:
                previous = winreg.QueryValueEx(key, _BACKUP_VALUE)[0]
                winreg.DeleteValue(key, _BACKUP_VALUE)
            except FileNotFoundError:
                pass
        with winreg.OpenKey(hkcu, k["classic_cmd"], 0, winreg.KEY_SET_VALUE) as key:
            if previous:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, previous)
            else:
                winreg.DeleteValue(key, "")
    except FileNotFoundError:
        pass

    try:
        with winreg.OpenKey(hkcu, k["regapps"], 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
    except FileNotFoundError:
        pass
    _delete_tree(hkcu, k["app"])
    _delete_tree(hkcu, k["progid"])
    return previous


def current_handler() -> str | None:
    """The classic magnet: command (not the Settings choice, which may differ)."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _keys()["classic_cmd"]) as key:
            return winreg.QueryValueEx(key, "")[0] or None
    except FileNotFoundError:
        return None


def chosen_default() -> str | None:
    """The ProgId picked for magnet: in Windows Settings, if any."""
    import winreg

    path = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\magnet\UserChoice"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            return winreg.QueryValueEx(key, "ProgId")[0] or None
    except FileNotFoundError:
        return None


def _delete_tree(root, path: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(root, path) as key:
            children = []
            i = 0
            while True:
                try:
                    children.append(winreg.EnumKey(key, i))
                except OSError:
                    break
                i += 1
    except FileNotFoundError:
        return
    for child in children:
        _delete_tree(root, rf"{path}\{child}")
    winreg.DeleteKey(root, path)


def show_error(message: str) -> None:
    """Report an error when launched from a browser, where there's no console."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "torrentfiler", 0x10)
    except (AttributeError, OSError):
        pass
