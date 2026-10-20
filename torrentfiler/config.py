"""Configuration loading and validation."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

# Subfolders created under watch_root. The folder a .torrent lands in is
# the instruction -- no filename conventions to remember.
INBOX = "inbox"
FORCE = "force"
FORCE_DUP = "force-dup"
PROCESSING = "processing"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"

DROP_FOLDERS = (INBOX, FORCE, FORCE_DUP)
ALL_FOLDERS = DROP_FOLDERS + (PROCESSING, DONE, FAILED, SKIPPED)


class ConfigError(Exception):
    """Configuration is missing or invalid."""


@dataclass(frozen=True)
class Paths:
    watch_root: Path
    temp_download: Path
    library_root: Path
    database: Path

    def folder(self, name: str) -> Path:
        return self.watch_root / name


@dataclass(frozen=True)
class Transmission:
    host: str
    port: int
    path: str
    username: str
    password: str
    timeout: int


@dataclass(frozen=True)
class Limits:
    max_active: int
    per_torrent_download_kbps: int
    per_torrent_peer_limit: int


@dataclass(frozen=True)
class Service:
    poll_interval_seconds: int
    stability_checks: int
    process_priority: str
    log_level: str
    retry_base_seconds: int
    retry_max_seconds: int
    max_attempts: int
    # A magnet whose metadata hasn't arrived by then is given up on.
    metadata_timeout_minutes: int = 30


CATEGORIES = ("video", "music", "iso", "other")
COPY, MOVE = "copy", "move"


@dataclass(frozen=True)
class Policy:
    """How one category of download is filed and how long it seeds.

    copy: files are copied to `root` and the torrent keeps seeding from the
          temp folder until seed_ratio OR seed_hours is reached (0 = that
          limit is not used), then the temp copy is deleted.
    move: the torrent is removed from Transmission straight away and its
          files moved to `root`. No seeding, no duplicate.
    """

    root: Path | None = None  # None: see Config.root_for
    mode: str = COPY
    seed_ratio: float = 0.0
    seed_hours: float = 0.0


@dataclass(frozen=True)
class Filing:
    video: Policy = Policy(mode=COPY, seed_ratio=1.0, seed_hours=240)
    music: Policy = Policy(mode=COPY, seed_hours=3)
    iso: Policy = Policy(mode=MOVE)
    other: Policy = Policy(mode=COPY, seed_ratio=0.5, seed_hours=24)
    # End seeding early once nothing has been uploaded for this fraction of
    # the category's seed time (0 = never end early).
    stall_fraction: float = 0.3
    # Free space to leave on a destination volume after copying.
    min_free_gb: float = 5.0

    def policy(self, category: str | None) -> Policy:
        return getattr(self, category or "video")


@dataclass(frozen=True)
class Api:
    """The add-torrent REST API (a separate service: --serve-api)."""

    # Home-network listener: token only. "" turns it off.
    host: str = "0.0.0.0"
    port: int = 8765
    max_torrent_mb: float = 10.0
    # None = next to the database.
    devices_file: Path | None = None
    audit_log: Path | None = None
    # Tailscale listener, always on 127.0.0.1 so only `tailscale serve` on
    # this PC can reach it. 0 turns it off.
    tailnet_port: int = 0
    # Refuse Tailscale-path requests without a signed-in Tailscale user.
    require_tailnet_identity: bool = True
    # Lockout: this many failed authentications from one address within
    # lockout_window_minutes blocks it for lockout_minutes.
    lockout_failures: int = 5
    lockout_window_minutes: float = 10.0
    lockout_minutes: float = 15.0


@dataclass(frozen=True)
class Config:
    paths: Paths
    transmission: Transmission
    limits: Limits
    service: Service
    filing: Filing = Filing()
    api: Api = Api()

    def root_for(self, category: str | None) -> Path:
        """Destination folder for a category. Unset roots default to
        library_root for video, and to folders beside it for the rest:
        <library parent>\\jellyfin-music and <library parent>\\download."""
        category = category or "video"
        root = self.filing.policy(category).root
        if root is not None:
            return root
        if category == "video":
            return self.paths.library_root
        name = "jellyfin-music" if category == "music" else "download"
        return self.paths.library_root.parent / name

    @property
    def devices_file(self) -> Path:
        return self.api.devices_file or self.paths.database.parent / "api_devices.json"

    @property
    def audit_log(self) -> Path:
        return self.api.audit_log or self.paths.database.parent / "api_audit.log"


def _require(table: dict, section: str, key: str):
    if key not in table:
        raise ConfigError(f"missing [{section}] {key}")
    return table[key]


def load(path: Path) -> Config:
    if not path.is_file():
        raise ConfigError(f"no config file at {path}")
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    paths_raw = raw.get("paths", {})
    tr_raw = raw.get("transmission", {})
    lim_raw = raw.get("limits", {})
    svc_raw = raw.get("service", {})
    fil_raw = raw.get("filing", {})
    api_raw = raw.get("api", {})

    paths = Paths(
        watch_root=Path(_require(paths_raw, "paths", "watch_root")),
        temp_download=Path(_require(paths_raw, "paths", "temp_download")),
        library_root=Path(_require(paths_raw, "paths", "library_root")),
        database=Path(_require(paths_raw, "paths", "database")),
    )

    transmission = Transmission(
        host=tr_raw.get("host", "127.0.0.1"),
        port=int(tr_raw.get("port", 9091)),
        path=tr_raw.get("path", "/transmission/rpc"),
        username=_require(tr_raw, "transmission", "username"),
        password=_require(tr_raw, "transmission", "password"),
        timeout=int(tr_raw.get("timeout", 15)),
    )

    limits = Limits(
        max_active=int(lim_raw.get("max_active", 2)),
        per_torrent_download_kbps=int(lim_raw.get("per_torrent_download_kbps", 0)),
        per_torrent_peer_limit=int(lim_raw.get("per_torrent_peer_limit", 20)),
    )

    service = Service(
        poll_interval_seconds=int(svc_raw.get("poll_interval_seconds", 10)),
        stability_checks=int(svc_raw.get("stability_checks", 2)),
        process_priority=svc_raw.get("process_priority", "below_normal"),
        log_level=svc_raw.get("log_level", "INFO"),
        retry_base_seconds=int(svc_raw.get("retry_base_seconds", 5)),
        retry_max_seconds=int(svc_raw.get("retry_max_seconds", 300)),
        max_attempts=int(svc_raw.get("max_attempts", 8)),
        metadata_timeout_minutes=int(svc_raw.get("metadata_timeout_minutes", 30)),
    )

    filing = _load_filing(fil_raw)

    api = Api(
        host=str(api_raw.get("host", "0.0.0.0")),
        port=int(api_raw.get("port", 8765)),
        max_torrent_mb=float(api_raw.get("max_torrent_mb", 10)),
        devices_file=Path(api_raw["devices_file"]) if "devices_file" in api_raw else None,
        audit_log=Path(api_raw["audit_log"]) if "audit_log" in api_raw else None,
        tailnet_port=int(api_raw.get("tailnet_port", 0)),
        require_tailnet_identity=bool(api_raw.get("require_tailnet_identity", True)),
        lockout_failures=int(api_raw.get("lockout_failures", 5)),
        lockout_window_minutes=float(api_raw.get("lockout_window_minutes", 10)),
        lockout_minutes=float(api_raw.get("lockout_minutes", 15)),
    )

    if limits.max_active < 1:
        raise ConfigError("[limits] max_active must be at least 1")
    if service.stability_checks < 1:
        raise ConfigError("[service] stability_checks must be at least 1")
    if service.poll_interval_seconds < 1:
        raise ConfigError("[service] poll_interval_seconds must be at least 1")

    if not 1 <= api.port <= 65535:
        raise ConfigError("[api] port must be between 1 and 65535")
    if api.tailnet_port and not 1024 <= api.tailnet_port <= 65535:
        raise ConfigError("[api] tailnet_port must be 0 (off) or 1024-65535")
    if api.tailnet_port and api.tailnet_port == api.port and api.host in ("0.0.0.0", "127.0.0.1"):
        raise ConfigError("[api] tailnet_port must differ from port")
    if api.lockout_failures < 1:
        raise ConfigError("[api] lockout_failures must be at least 1")

    return Config(paths=paths, transmission=transmission, limits=limits,
                  service=service, filing=filing, api=api)


def _load_filing(raw: dict) -> Filing:
    defaults = Filing()
    policies = {}
    for category in CATEGORIES:
        base = defaults.policy(category)
        table = raw.get(category, {})
        if category == "video":
            # Before categories existed, [filing] seed_ratio / seed_days
            # applied to everything, which was all video.
            table = {"seed_ratio": raw.get("seed_ratio", base.seed_ratio),
                     "seed_hours": float(raw["seed_days"]) * 24 if "seed_days" in raw
                     else base.seed_hours, **table}
        policy = Policy(
            root=Path(table["root"]) if "root" in table else None,
            mode=str(table.get("mode", base.mode)),
            seed_ratio=float(table.get("seed_ratio", base.seed_ratio)),
            seed_hours=float(table.get("seed_hours", base.seed_hours)),
        )
        where = f"[filing.{category}]"
        if policy.mode not in (COPY, MOVE):
            raise ConfigError(f"{where} mode must be \"copy\" or \"move\"")
        if policy.seed_ratio < 0 or policy.seed_hours < 0:
            raise ConfigError(f"{where} seed_ratio and seed_hours can't be negative")
        if policy.mode == MOVE and (policy.seed_ratio or policy.seed_hours):
            raise ConfigError(f"{where} a moved download can't seed; "
                              f"remove seed_ratio / seed_hours or use mode = \"copy\"")
        policies[category] = policy

    stall = float(raw.get("stall_fraction", defaults.stall_fraction))
    if not 0 <= stall <= 1:
        raise ConfigError("[filing] stall_fraction must be between 0 and 1")
    return Filing(**policies, stall_fraction=stall,
                  min_free_gb=float(raw.get("min_free_gb", defaults.min_free_gb)))


def ensure_folders(paths: Paths) -> None:
    """Create the watch tree, temp folder, and database parent."""
    for name in ALL_FOLDERS:
        paths.folder(name).mkdir(parents=True, exist_ok=True)
    paths.temp_download.mkdir(parents=True, exist_ok=True)
    paths.database.parent.mkdir(parents=True, exist_ok=True)
