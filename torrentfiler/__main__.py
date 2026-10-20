"""Entry point: python -m torrentfiler --config config.toml"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path

from . import config as cfgmod
from . import db
from . import magnet
from .config import ConfigError
from .service import Service, lower_priority, setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="torrentfiler")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--once", action="store_true",
                        help="run a single tick and exit (useful for testing)")
    parser.add_argument("--status", action="store_true",
                        help="list every torrent by state, with live progress, and exit")
    parser.add_argument("--all", action="store_true",
                        help="with --status: also list finished (purged) and skipped torrents")
    parser.add_argument("--add-magnet", metavar="LINK",
                        help="queue a magnet link by writing it into inbox/ and exit")
    parser.add_argument("--force", action="store_true",
                        help="with --add-magnet: write into force/ instead of inbox/")
    parser.add_argument("--register-magnet-handler", action="store_true",
                        help="make clicked magnet: links go to torrentfiler (this user)")
    parser.add_argument("--unregister-magnet-handler", action="store_true",
                        help="restore the magnet: handler that was there before")
    parser.add_argument("--refile", metavar="NAME|HASH|all",
                        help="send torrents in needs_review/failed through the pipeline "
                             "again (part of a name, a hash prefix, or 'all')")
    api = parser.add_argument_group("REST API for other devices")
    api.add_argument("--serve-api", action="store_true",
                     help="run the add-torrent API (normally as its own NSSM service)")
    api.add_argument("--api-add-device", metavar="NAME",
                     help="issue a token for a device, e.g. phone-sj (shown once)")
    api.add_argument("--api-revoke-device", metavar="NAME",
                     help="revoke a device's token immediately")
    api.add_argument("--api-list-devices", action="store_true",
                     help="list devices with their last use")
    api.add_argument("--api-report", action="store_true",
                     help="per-device report of what was added and what became of it")
    api.add_argument("--days", type=float, help="with --api-report: only the last N days")
    api.add_argument("--device", metavar="NAME",
                     help="with --api-report: list that device's individual adds")
    args = parser.parse_args(argv)

    if args.unregister_magnet_handler:
        previous = magnet.unregister_handler()
        print(f"magnet: links now open with {previous}" if previous
              else "magnet: handler removed; no previous handler to restore")
        return 0

    try:
        cfg = cfgmod.load(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        if args.add_magnet:
            magnet.show_error(f"Config error: {exc}")
        return 2

    cfgmod.ensure_folders(cfg.paths)

    if args.add_magnet:
        folder = cfg.paths.folder(cfgmod.FORCE if args.force else cfgmod.INBOX)
        try:
            written = magnet.write_drop_file(args.add_magnet, folder)
        except (magnet.MagnetError, OSError) as exc:
            print(f"could not add magnet: {exc}", file=sys.stderr)
            magnet.show_error(f"Could not add magnet link:\n\n{exc}")
            return 1
        print(f"queued {written}")
        return 0

    if args.register_magnet_handler:
        # pythonw has no console window, so a browser click doesn't flash one.
        python = Path(sys.executable).with_name("pythonw.exe")
        if not python.is_file():
            python = Path(sys.executable)
        launcher = Path(__file__).resolve().parent.parent / "magnet_handler.pyw"
        command = magnet.handler_command(python, launcher, args.config.resolve())
        previous = magnet.register_handler(command)
        print(f"registered torrentfiler for magnet: links\n  {command}")
        if previous and not magnet.is_ours(previous):
            print(f"previous handler saved (restore with --unregister-magnet-handler):\n"
                  f"  {previous}")
        chosen = magnet.chosen_default()
        if chosen == magnet.PROGID:
            print("Windows default for magnet: is already torrentfiler. Done.")
        else:
            print(f"\nOne manual step left (Windows doesn't let programs set this):\n"
                  f"  Settings > Apps > Default apps > 'Choose default apps by protocol'\n"
                  f"  > MAGNET > pick 'torrentfiler'"
                  f"{f'  (currently {chosen})' if chosen else ''}\n"
                  f"Opening that page now...")
            try:
                os.startfile("ms-settings:defaultapps")
            except OSError:
                pass
        return 0

    if args.refile:
        return _refile(args.refile, cfg)

    if args.api_add_device or args.api_revoke_device or args.api_list_devices or args.api_report:
        return _api_admin(args, cfg)

    if args.serve_api:
        from . import api as apimod

        setup_logging(cfg.service.log_level, cfg.paths.database.parent / "api.log")
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda _s, _f: stop.set())
            except (ValueError, OSError):
                pass
        try:
            apimod.serve(cfg, stop)
        except OSError as exc:
            print(f"cannot start the API on {cfg.api.host}:{cfg.api.port}: {exc}",
                  file=sys.stderr)
            return 1
        return 0

    if args.status:
        from . import status

        print(status.render(cfg, show_all=args.all))
        return 0

    setup_logging(cfg.service.log_level, cfg.paths.database.parent / "torrentfiler.log")
    lower_priority(cfg.service.process_priority)

    service = Service(cfg)

    def handle_stop(_signum, _frame):
        service.stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_stop)
        except (ValueError, OSError):
            pass

    if args.once:
        service.tick()
        service.conn.close()
        return 0

    service.run()
    return 0


def _refile(target: str, cfg) -> int:
    """Re-drop the saved .torrent/.magnet of reviewed or failed torrents into
    force/. The running service then re-adds and re-files them under the
    current rules. This command only moves files; the service stays the only
    writer of the database."""
    conn = db.connect_readonly(cfg.paths.database)
    if conn is None:
        print("no torrents on record", file=sys.stderr)
        return 1
    try:
        rows = conn.execute("SELECT infohash, name, state, source_file FROM torrents "
                            "WHERE state IN (?, ?)", (db.NEEDS_REVIEW, db.FAILED)).fetchall()
    finally:
        conn.close()
    needle = target.lower()
    if needle == "all":
        matches = [r for r in rows if r["state"] == db.NEEDS_REVIEW]
    else:
        matches = [r for r in rows if r["infohash"].startswith(needle)
                   or needle in r["name"].lower()]
    if not matches:
        print(f"nothing in needs_review or failed matches {target!r}", file=sys.stderr)
        return 1

    force = cfg.paths.folder(cfgmod.FORCE)
    status = 0
    for r in matches:
        source = Path(r["source_file"])
        if not source.is_file():
            print(f"skipped {r['name']}: its saved file {source} is gone; drop the "
                  f".torrent or magnet into {force} yourself", file=sys.stderr)
            status = 1
            continue
        dest = force / source.name
        source.replace(dest)
        note = source.with_suffix(source.suffix + ".error.txt")
        if note.is_file():
            note.unlink()
        print(f"re-queued {r['name']} ({r['state']}) -> {dest}")
    print("The service picks these up within about 20 seconds.")
    return status


def _api_admin(args, cfg) -> int:
    from . import audit, devices

    path = cfg.devices_file
    try:
        if args.api_add_device:
            token = devices.add(path, args.api_add_device)
            print(f"device:  {args.api_add_device}\ntoken:   {token}\n\n"
                  f"Enter this token on that device now. It is not stored anywhere\n"
                  f"and can't be shown again. If lost, revoke and add the device again.")
        elif args.api_revoke_device:
            devices.revoke(path, args.api_revoke_device)
            print(f"revoked {args.api_revoke_device}; its token stops working immediately")
        elif args.api_list_devices:
            usage = audit.device_usage(cfg.audit_log)
            known = devices.load(path)
            if not known:
                print("no devices yet; add one with --api-add-device NAME")
            print(f"{'device':<20}{'status':<10}{'created':<27}{'last used':<27}"
                  f"{'last ip':<17}adds")
            for d in known:
                u = usage.get(d.name, {}) if d.active else {}
                print(f"{d.name:<20}{'active' if d.active else 'revoked':<10}{d.created:<27}"
                      f"{u.get('last_used') or '-':<27}{u.get('last_ip') or '-':<17}"
                      f"{u.get('adds', 0)}")
        else:
            print(audit.report(cfg.audit_log, cfg.paths.database,
                               days=args.days, device=args.device))
    except devices.DeviceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
