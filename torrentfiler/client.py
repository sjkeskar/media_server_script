"""Thin wrapper over transmission-rpc.

Separates transient failures (daemon restarting, network blip) from
permanent ones (rejected torrent). Only transient failures get retried;
a rejected torrent goes straight to quarantine so the queue never fills
with records that will never succeed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence

from transmission_rpc import Client
from transmission_rpc.error import (
    TransmissionAuthError,
    TransmissionConnectError,
    TransmissionError,
    TransmissionTimeoutError,
)

from . import magnet
from .config import Transmission as TransmissionConfig

log = logging.getLogger(__name__)


class TransientClientError(Exception):
    """Worth retrying: daemon down, timeout, transport failure."""


class PermanentClientError(Exception):
    """Not worth retrying: Transmission rejected this torrent."""


class TransmissionClient:
    def __init__(self, cfg: TransmissionConfig) -> None:
        self._cfg = cfg
        self._client: Optional[Client] = None

    def _connect(self) -> Client:
        if self._client is not None:
            return self._client
        try:
            self._client = Client(
                host=self._cfg.host,
                port=self._cfg.port,
                path=self._cfg.path,
                username=self._cfg.username,
                password=self._cfg.password,
                timeout=self._cfg.timeout,
            )
        except TransmissionAuthError as exc:
            # Bad credentials will not fix themselves, but the service
            # should keep running and complaining rather than exit.
            raise TransientClientError(f"authentication rejected: {exc}") from exc
        except (TransmissionConnectError, TransmissionTimeoutError, OSError) as exc:
            raise TransientClientError(f"cannot reach Transmission: {exc}") from exc
        return self._client

    def _drop(self) -> None:
        self._client = None

    def ping(self) -> str:
        try:
            return str(self._connect().get_session().version)
        except (TransmissionConnectError, TransmissionTimeoutError, OSError) as exc:
            self._drop()
            raise TransientClientError(f"cannot reach Transmission: {exc}") from exc

    def add(
        self,
        torrent_path: Path,
        *,
        download_dir: Path,
        peer_limit: int,
        download_limit_kbps: int,
    ) -> int:
        """Add paused, apply limits, then start. Returns the Transmission id.

        Adding an infohash Transmission already knows is not an error --
        it returns the existing torrent, which makes this safe to replay
        after a crash.
        """
        client = self._connect()
        try:
            if torrent_path.suffix.lower() == magnet.SUFFIX:
                payload = magnet.read(torrent_path).uri  # Transmission takes the link itself
            else:
                payload = torrent_path.read_bytes()
        except OSError as exc:
            raise TransientClientError(f"cannot read {torrent_path.name}: {exc}") from exc
        except magnet.MagnetError as exc:
            raise PermanentClientError(f"{torrent_path.name}: {exc}") from exc

        try:
            torrent = client.add_torrent(
                payload,
                download_dir=str(download_dir),
                paused=True,
                peer_limit=peer_limit,
            )
        except (TransmissionConnectError, TransmissionTimeoutError, OSError) as exc:
            self._drop()
            raise TransientClientError(f"add failed: {exc}") from exc
        except TransmissionError as exc:
            raise PermanentClientError(f"Transmission rejected the torrent: {exc}") from exc

        torrent_id = int(torrent.id)

        if download_limit_kbps > 0:
            try:
                client.change_torrent(
                    ids=[torrent_id],
                    download_limit=download_limit_kbps,
                    download_limited=True,
                )
            except TransmissionError as exc:
                log.warning("could not apply speed limit to %s: %s", torrent_id, exc)

        try:
            client.start_torrent(ids=[torrent_id])
        except (TransmissionConnectError, TransmissionTimeoutError, OSError) as exc:
            self._drop()
            # The torrent exists and is paused; the next reconcile starts it.
            raise TransientClientError(f"could not start {torrent_id}: {exc}") from exc

        return torrent_id

    def get(self, ids: Sequence[int]):
        if not ids:
            return []
        try:
            return self._connect().get_torrents(ids=list(ids))
        except (TransmissionConnectError, TransmissionTimeoutError, OSError) as exc:
            self._drop()
            raise TransientClientError(f"status query failed: {exc}") from exc
        except TransmissionError as exc:
            # Unknown ids raise here; treat as "none of them exist".
            log.debug("status query rejected: %s", exc)
            return []

    def remove(self, torrent_id: int, *, delete_data: bool) -> None:
        """Remove a torrent; with delete_data, Transmission deletes its files too."""
        try:
            self._connect().remove_torrent(ids=[torrent_id], delete_data=delete_data)
        except (TransmissionConnectError, TransmissionTimeoutError, OSError) as exc:
            self._drop()
            raise TransientClientError(f"could not remove {torrent_id}: {exc}") from exc
        except TransmissionError as exc:
            raise PermanentClientError(f"could not remove {torrent_id}: {exc}") from exc

    def start(self, torrent_id: int) -> None:
        try:
            self._connect().start_torrent(ids=[torrent_id])
        except (TransmissionConnectError, TransmissionTimeoutError, OSError) as exc:
            self._drop()
            raise TransientClientError(f"could not start {torrent_id}: {exc}") from exc
        except TransmissionError as exc:
            raise PermanentClientError(f"could not start {torrent_id}: {exc}") from exc
