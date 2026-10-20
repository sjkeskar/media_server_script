# torrentfiler

Watches drop folders for `.torrent` files, hands them to Transmission, copies
the finished media into your library, and removes each torrent once it has
seeded enough. Every torrent is tracked in SQLite so a crash or reboot resumes
from a known point.

- **Phase 1** — intake and download.
- **Phase 2** — filing into the library and seeding cleanup.

## Setup

1. Install Transmission for Windows. Enable RPC, bind it to `127.0.0.1`, set a
   username and password, and whitelist `127.0.0.1` only.
2. Run `transmission-daemon` as a service via NSSM so it survives logoff.
3. In Transmission's settings: `download-queue-size: 2`, `peer-limit-global: 80`,
   `peer-limit-per-torrent: 20`, `cache-size-mb: 16`, and configure alt-speed
   (turtle mode) as your quiet profile.
4. Python 3.11+ is required (`tomllib`). Create a venv and install:
   `py -3.13 -m venv .venv` then `.venv\Scripts\pip install -r requirements.txt`
5. `copy config.example.toml config.toml` and edit the paths and credentials.

Filing copies files, so the library volume needs room for a second copy of
each download until it finishes seeding. `[filing] min_free_gb` is the
headroom kept free. When space runs short, seeding is cut short to make room
(see below).

Full step-by-step setup, including NSSM, firewall and router, is in
[SETUP_GUIDE.md](SETUP_GUIDE.md).

## Running

```
.venv\Scripts\python -m torrentfiler --config config.toml            # run the service
.venv\Scripts\python -m torrentfiler --config config.toml --once     # one tick, then exit
.venv\Scripts\python -m torrentfiler --config config.toml --status   # every torrent by state
.venv\Scripts\python -m torrentfiler --config config.toml --status --all   # incl. finished ones
```

Don't run `--once` while the service is running: it is a second instance
working the same folders and database. `--status` only reads and is safe.

`--status` lists every torrent grouped by state:

- **Downloading:** live progress, speed, ETA and peers from Transmission,
  or "fetching file list" for a magnet still waiting for metadata.
- **Queued** and **filing**.
- **Filed:** ratio and days seeded against the cleanup limits, and the
  library folder.
- **Needs review** and **failed:** with the reason.

Finished and skipped torrents are only counted unless you add `--all`. If
Transmission is down, it still shows everything from the database.

Install as a service with NSSM once you're happy with it:

```
nssm install torrentfiler "Z:\torrent_script\.venv\Scripts\python.exe" "-m torrentfiler --config Z:\torrent_script\config.toml"
nssm set torrentfiler AppDirectory Z:\torrent_script
nssm set torrentfiler Start SERVICE_DELAYED_AUTO_START
nssm set torrentfiler DependOnService transmission
```

## Drop folders

The folder you drop a `.torrent` or `.magnet` file into is the instruction.

| Folder | Meaning |
|---|---|
| `inbox/` | Normal. Skipped if the infohash is already known. |
| `force/` | Re-add even if the infohash is known. Also how you retry a torrent that needs review. |
| `force-dup/` | Flag it to bypass the phase 3 library-duplicate check. |

Everything else is managed by the service: `processing/` holds claimed files,
`done/` holds the `.torrent` of everything filed, `failed/` holds quarantined
ones and those needing review with a `.error.txt` note beside them, and
`skipped/` holds deduplicated drops.

## Magnet links

A `.magnet` file is a text file holding one magnet link. It behaves exactly
like a `.torrent` in the drop folders: same dedup by infohash, same filing and
cleanup. You rarely write one by hand:

- **Click it in the browser.** Run this once to make torrentfiler the
  `magnet:` handler for your Windows user (no admin needed):
  ```
  .venv\Scripts\python -m torrentfiler --config config.toml --register-magnet-handler
  ```
  Then pick **torrentfiler** for MAGNET in *Settings → Default apps →
  Choose default apps by protocol*. The command opens that page. Windows
  won't let a program make that choice itself, and until you do, browsers
  offer Transmission, which registered itself the same way. Clicking a
  magnet link then asks to open torrentfiler. Allow it, and tick "always
  allow" to skip the prompt next time. The link lands in `inbox\` as a `.magnet` file. The handler it
  replaced (uTorrent, on this PC) is saved, and
  `--unregister-magnet-handler` restores it. If a link is invalid, a message
  box explains why.
- **From the command line:**
  `.venv\Scripts\python -m torrentfiler --config config.toml --add-magnet "magnet:?xt=..."`.
  Add `--force` to queue into `force\`.

A magnet carries only the infohash and usually a name. Size and file list
arrive once Transmission fetches the metadata from peers, and are logged then.
If the metadata hasn't arrived after `metadata_timeout_minutes` (30), the
magnet is probably dead: it is removed from Transmission and quarantined in
`failed\` with a note, freeing its download slot. Re-dropping it tries again.

## Adding from other devices (REST API)

`torrentfiler-api` is a separate service (`--serve-api`) that lets laptops and
Android phones and tablets on the home network add torrents. It validates
what it receives and writes it into `inbox\` or `force\`, so everything after
that is the normal pipeline. It never writes to the database.

| Method | Path | Body |
|---|---|---|
| `POST` | `/api/magnet` | JSON `{"link": "magnet:?...", "force": false}`, or the link as plain text (surrounding text ignored) |
| `POST` | `/api/torrent` | raw `.torrent` bytes, max `[api] max_torrent_mb` |
| `GET` | `/api/status` | every torrent by state, same as `--status`: JSON, or `?format=text` for a phone dialog; `?all=1` adds finished ones |
| `GET` | `/` | page for browsers: an **Add** tab (paste a link, drop files) and a **Status** tab that refreshes every 15 s |
| `GET` | `/api/health` | `{"ok": true}`, no token |

- **Query options:** `?force=1` queues into `force\`. `?format=text` replies
  with one readable line instead of JSON, for Android toasts.
- **Replies:** `queued`, `known` (already on record, with its state; nothing
  written unless forced), or an error with the reason. Status codes: `401`
  bad or revoked token, `400` invalid input, `413` too large.
- **Authentication:** `Authorization: Bearer <token>`, with **one token per
  device**:
  - issued with `--api-add-device <kind>-<name>`, where kind is `phone`,
    `tab` or `laptop`;
  - listed with `--api-list-devices`;
  - revoked with `--api-revoke-device`, which takes effect immediately.

  Only SHA-256 hashes are stored (`api_devices.json`).
- **Audit:** every request, including refused ones, is appended to
  `api_audit.log` (JSON lines) with device, IP and outcome.
  `--api-report [--days N] [--device NAME]` joins it with the database to
  show what each device added and what became of it.

**Two ways in**, sharing tokens, audit log, lockout and status cache:

| Listener | Address | Requires |
|---|---|---|
| Home network | `[api] host:port`, e.g. `192.168.7.5:8765`, firewall limited to the home subnet | device token |
| Anywhere | `127.0.0.1:[api] tailnet_port` (8766), reached only through `tailscale serve` (HTTPS, tailnet only) | signed-in Tailscale identity **and** device token |

- **Home listener address:** it's bound to the LAN address, not `0.0.0.0`.
  Otherwise Tailscale peers could reach it directly and skip the identity
  check, and the service warns in its log if you set `0.0.0.0`.
- **Identity headers** (`Tailscale-User-Login`, `X-Forwarded-For`) are trusted
  only on the loopback listener, and ignored on the LAN one.
- **Lockout:** 5 failed authentications from an address within 10 minutes
  block it for 15 minutes (`429`), on both listeners.
- **Audit:** the log and `--api-report --device` record the route, the
  Tailscale user and the address of each request.
- **Never** use `tailscale funnel` or a router port forward.

Setup, firewall and the Android shortcuts are in
[SETUP_GUIDE.md](SETUP_GUIDE.md), steps 12 (home) and 13 (anywhere).

## Filing and cleanup (phase 2)

When a download completes, it is **classified by its files** and handled
by its category's rules (`[filing.<category>]` in `config.toml`):

| Category | When | Goes to | How | Seeding ends at |
|---|---|---|---|---|
| video | any video file (not a sample) | `library_root` (`Z:\jellyfin`) | copy video, subtitles, cover art | ratio 1.0 or 10 days |
| music | audio, no video | `Z:\jellyfin-music` | copy audio, cover art, lyrics, `.cue` | 3 hours |
| iso | `.iso`/`.img`, no video or audio | `Z:\download` | **move** every file; removed from Transmission at once | no seeding |
| other | anything else | `Z:\download` | copy every file | ratio 0.5 or 1 day |

Each download keeps the torrent's folder structure. A single-file torrent is
wrapped in a folder named after the file.

- **Video** skips samples, `Screens/`/`Sample/` folders, screenshots, `.nfo`
  and `.txt` files. Cover art means images named like `poster`, `cover`,
  `folder`, `fanart`, `banner` or `logo`.
- **Music** skips rip logs, `.nfo` files, scans and samples. Its cover art
  also matches `front`, `back`, `albumart`, `artwork` and `cd`.

For **copied** categories, the download stays in `temp_download` and keeps
seeding until either limit is reached. It is then removed from Transmission
and its temp copy deleted, while the copy is untouched.

**Stalled seeding ends early.** For every category, if nothing has been
uploaded for `stall_fraction` (30%) of that category's seed time, seeding ends
then: 72 hours for video, 54 minutes for music, 7.2 hours for other.
Uploaded bytes are tracked, so any upload restarts the clock.

A **moved** download (iso) is removed from Transmission first, so the files
are free, then moved. On the same drive that is an instant rename, which
needs no extra space and leaves no duplicate. Its emptied temp folder is
removed. The move plan is recorded before the torrent is removed, so a crash
mid-move finishes on restart. A conflicting file at the destination is
detected *before* anything happens, and the torrent goes to review untouched.

**Low disk space overrides that.** If a copy into the library wouldn't leave
`min_free_gb` free, or the download volume drops below `min_free_gb`, *every*
filed torrent stops seeding at once and its temp copy is deleted, however
little it has seeded. Those temp copies are duplicates of verified library
copies, so they are the safe thing to delete. The copy that didn't fit
retries a few seconds later. Torrents still downloading or in review are never
touched.

Safety rules:

- Everything is checked before the first byte is written: free space, files
  already in the library, paths escaping the library.
- **Existing library files are never overwritten.** An identical file (same
  size) counts as already copied; a different one stops filing for review.
- Each file is copied to `name.part`, size-checked, then renamed, so the
  library never holds a half-written file under its real name. A crash
  mid-copy resumes on restart.
- Before the temp copy is deleted, every library copy is re-checked. If one
  is missing or changed, or the download isn't inside `temp_download`,
  nothing is deleted and the torrent goes to review.

### States

`queued → downloading → complete → filing → filed (seeding) → purged`
(a moved download goes straight from `filing` to `purged`)

**`needs_review`** means automatic handling stopped. The torrent is left in
Transmission as it is, and the reason is in `last_error` and in the
`.error.txt` note in `failed/`. Typical causes are a conflicting file at the
destination, or a copy deleted before seeding finished. After fixing the
cause, run it through again:

```
.venv\Scripts\python -m torrentfiler --config config.toml --refile <part of name | hash | all>
```

This moves its saved `.torrent` or `.magnet` from `failed/` into `force/`.
`all` re-queues everything in review. Files already copied are not copied
twice.

## How it behaves

- **Half-written files** are never parsed. A `.torrent` must have an unchanged
  size and mtime across `stability_checks` consecutive polls first.
- **Identity is the infohash**, computed as SHA-1 over the original bytes of the
  info dictionary — not a re-encoding, which would give the wrong hash for any
  torrent whose keys aren't in canonical order.
- **Transient failures** (Transmission down, file locked) retry with exponential
  backoff up to `max_attempts`, then quarantine. Torrents Transmission outright
  rejects fail immediately without burning retries.
- **Re-dropping a failed torrent** retries it from scratch.
- **Crash recovery** runs at startup: records that never got claimed are
  discarded so the file is picked up fresh, and records whose file is already in
  `processing/` resume as queued.
- **Low priority** by default — `BELOW_NORMAL_PRIORITY_CLASS` and lowest I/O
  priority, so it never competes with a game.

## Tests

```
.venv\Scripts\python test_phase1.py
.venv\Scripts\python test_phase2.py
.venv\Scripts\python test_magnet.py
.venv\Scripts\python test_api.py
.venv\Scripts\python test_status.py
.venv\Scripts\python test_categories.py
.venv\Scripts\python test_remote.py
```

Remote access (29 checks), with both listeners running:

- **Tailscale listener:** refuses requests without a Tailscale identity
  (including the page), needs a token as well, and records the user and
  address.
- **LAN listener:** ignores spoofed identity headers.
- **Lockout:** triggers, sends `Retry-After`, expires, is cleared by a
  success, is per address, covers status, and is shared between listeners.
- **Also:** anti-framing headers, the report counting blocked attempts, and
  config validation.

Categories (47 checks):

- classification, and which files each category keeps;
- music copy and the 3-hour limit; iso move with no duplicate, folder
  cleanup, conflict before removal, and resuming an interrupted move; other
  with its 0.5 ratio and 1-day limit;
- the stall rule (including an upload restarting the clock);
- config defaults, overrides and validation;
- upgrading an old database in place; `--refile`.

Status (13 checks): every state listed with its details, live figures,
hiding finished torrents unless `--all`, and the database-only fallback when
Transmission is down.

API (51 checks), against a real server on a random port: tokens (missing,
wrong, revoked without a restart, other devices unaffected, no plaintext
storage, name rules), magnet and `.torrent` adds, force, text replies,
known-torrent answers, size and validity limits, hostile names, the main
service picking up API drops, the per-device report, and device status (token
required, JSON and phone text, shared cache, throttled auditing).

Magnets (39 checks): parsing hex and base32 infohashes, `.magnet` files,
`--add-magnet`, metadata arrival and the 30-minute timeout, dedup against a
`.torrent` of the same content, crash recovery, and the link handler's
register/unregister round trip. The handler test uses a scratch registry key,
never the real `magnet:` handler.

Phase 1 (47 checks): bencode parsing and rejection of malformed files,
infohash correctness, the state store, the full intake/reconcile/submit loop
against a fake Transmission, dedup and both force paths, the `max_active` cap,
retry and quarantine behaviour, crash recovery, and the watcher stability
check.

Phase 2 (47 checks), on real files in a temp folder: which files are chosen,
structure-preserving copies, single-file wrapping, no-media and conflict
review, accepting identical existing files, resuming an interrupted copy,
low-space backoff and ending all seeding to free space, ratio and age
cleanup, and refusing to delete when a
library copy is missing or the download is outside `temp_download`.

Neither needs a Transmission daemon.
