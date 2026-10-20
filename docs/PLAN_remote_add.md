# Plan: Adding torrents from other devices

**Status:**

- **Phase A (home network): built.** Code, tests (`test_api.py`) and docs
  (`SETUP_GUIDE.md` step 12) are done. The service, firewall rule and device
  tokens are installed by hand following step 12.
- **Phase B (away from home): built.** Code and tests (`test_remote.py`) are
  done, and `SETUP_GUIDE.md` step 13 covers installing Tailscale, `tailscale
  serve` and the devices. Changes from this plan, made for security:
  - **No firewall opening for Tailscale.** The API has a second listener on
    `127.0.0.1:8766` that only `tailscale serve` (HTTPS, tailnet only) can
    reach.
  - **Identity required.** That listener refuses requests without a verified
    Tailscale user, which also blocks Funnel.
  - **Home listener bound to the LAN address**, not `0.0.0.0`, so Tailscale
    peers can't reach it and skip the identity check.
  - **Firewall rule narrowed** to `192.168.7.0/24` instead of `LocalSubnet`.
  - **Lockout:** 5 failed authentications in 10 minutes block that address
    for 15 minutes.
  - **Audit** records the route, the Tailscale user and the address.

Changes from the plan made during the build:

- The API runs as `python -m torrentfiler --serve-api`; there is no separate
  `api_server.py`.
- There is no `enabled` switch in `[api]`. Having the `torrentfiler-api`
  service installed and running is what turns it on.
- Device usage (last used, last IP, add count) is derived from the audit log
  rather than stored in the token file. That way the API never writes the file
  that `--api-add-device` and `--api-revoke-device` edit.
- `POST /api/magnet` also accepts the link as plain text and pulls the link
  out of surrounding text, and `?format=text` returns one readable line.
  Both exist for Android HTTP Shortcuts.
- `registerProtocolHandler` on the laptop page was dropped: browsers allow it
  only on HTTPS pages.

## Goal

Queue torrents on the home PC from laptops and Android phones and tablets
(no Apple devices). Each device has its own token, so every add can be
traced to the device that made it:

- **Phase A:** on the home network (LAN) only.
- **Phase B:** also from away from home, without exposing anything to the
  internet.

### In scope

- Adding a **magnet link** or a **`.torrent` file**, normal or forced.
- Immediate feedback per add: accepted (new), already known (with its current
  state), or rejected (with the reason).
- **One token per device**, which can be issued, listed and revoked from the
  command line. Revoking takes effect immediately, with no restart.
- **An audit trail** of every request (who, when, from where, what, outcome),
  and a report per device of what each one added and how it ended up.

### Out of scope

- A dashboard, download progress, or pausing and removing torrents. The
  Transmission web UI and `--status` already cover monitoring on the PC.
- Any change to how torrents are downloaded, filed or cleaned up.

## Design

### Approach: write into the existing drop folders

The pipeline is already driven by files appearing in
`C:\torrents\watch\inbox\` and `force\`. The API only **validates** what it
receives and **writes it there** as a `.torrent` or `.magnet` file. That is
the same thing `--add-magnet` and the browser link handler do today. All of
the following stay exactly as they are:

- deduplication by infohash
- force handling
- the magnet metadata timeout
- filing into `Z:\jellyfin`
- seeding cleanup and low-space handling
- crash recovery

```
laptop / phone / tablet
        │  HTTP + token
        ▼
torrentfiler-api  (new NSSM service, port 8765)
        │  validate, then write .magnet / .torrent
        ▼
C:\torrents\watch\inbox\  or  force\
        │  (existing pipeline, unchanged)
        ▼
torrentfiler service → Transmission → Z:\jellyfin
```

### Why a separate service

- If the API crashes, or chokes on bad input from the network, downloading and
  filing keep running.
- It never writes to the database. Its only database access is a read-only
  lookup, so a request can say "already filed" straight away. The database
  runs in WAL mode, so that read is safe alongside the main service. It writes
  only drop files, its own audit log and its own device-token file.
- It can be stopped or disabled on its own, for example when travelling
  without wanting remote access.

### Implementation

- Python standard library only (`http.server.ThreadingHTTPServer`), so no
  new dependencies.
- New files:
  - `torrentfiler/api.py`: request handling, validation, the HTML page.
  - `api_server.py`: launcher for the NSSM service.
  - `test_api.py`: tests.
- Reuses the existing code:
  - `magnet.parse` / `magnet.write_drop_file` for magnet links.
  - `torrentfile.read_torrent` for validating `.torrent` uploads.
  - `db.get` for the "already known?" lookup.
- `.torrent` uploads are written under a temporary name and renamed into
  place, like `.magnet` files today, so the watcher never sees half a file.

## API

All `/api/*` calls except `/api/health` need the header
`Authorization: Bearer <device token>`. The token identifies the device.

| Method | Path | Body | Result |
|---|---|---|---|
| `POST` | `/api/magnet` | JSON `{"link": "magnet:?...", "force": false}` | Writes a `.magnet` into `inbox\` (or `force\`) |
| `POST` | `/api/torrent?force=0` | raw `.torrent` bytes (`application/x-bittorrent`) | Writes the file into `inbox\` (or `force\`) |
| `GET` | `/` | — | Mobile-friendly add page (see Clients) |
| `GET` | `/api/health` | — | `{"ok": true}`, for checking reachability (no token needed) |

**Response** (JSON) for both add endpoints:

```json
{"result": "queued",  "infohash": "c9e15763…", "name": "Some.Show.S01E01", "device": "phone-sj"}
{"result": "known",   "infohash": "c9e15763…", "name": "…", "state": "filed", "device": "phone-sj"}
{"result": "rejected", "error": "magnet link has no urn:btih infohash"}
```

- `known` means the main service would skip it as a duplicate. Nothing is
  written unless `force` is set.
- HTTP status codes: `200` queued or known, `400` invalid input, `401` bad or
  missing token, `413` upload too large.

### Limits and validation

- `.torrent` upload cap: 10 MB (real files are a few hundred KB at most).
- JSON body cap: 64 KB.
- Magnets are validated with the existing parser. `.torrent` files must parse
  and have an info dictionary, otherwise the request is rejected and nothing
  is written.

## Device tokens

Every laptop, phone and tablet gets its own token. Tokens are managed with
commands, never by hand:

```
.venv\Scripts\python -m torrentfiler --config config.toml --api-add-device phone-sj
    → prints the new token ONCE; enter it on that device
.venv\Scripts\python -m torrentfiler --config config.toml --api-list-devices
    → name, created, last used, last IP, add count, revoked?
.venv\Scripts\python -m torrentfiler --config config.toml --api-revoke-device phone-sj
```

- **Storage:** `C:\torrents\api_devices.json`, next to the database and
  outside the project folder. It holds a **SHA-256 hash** of each token, never
  the token itself, plus name, created date and revoked flag. A leaked copy of
  the file can't be used to add torrents.
- **Checking:** the incoming token is hashed and compared in constant time
  (`hmac.compare_digest`) against every active device.
- **Revocation is immediate:** the API re-reads the file when its
  modification time changes, so `--api-revoke-device` works without restarting
  the service. A revoked token gets `401`, and the attempt is logged with the
  device name.
- **Device names:** `<kind>-<owner or room>`, with kind `phone`, `tab` or
  `laptop` (for example `phone-sj`, `tab-livingroom`, `laptop-work`).
  Lowercase letters, digits and dashes only; the command refuses anything
  else. A name can't be reused while a token for it is active.
- **No per-device limits:** every active device has the same rights,
  including `force`.
- **Lost device:** revoke it. The other devices keep working untouched.
- **Replacing a token:** revoke, then add the same name again.

## Audit log and reporting

### Audit log

`C:\torrents\api_audit.log`: one JSON object per line, appended for **every**
request, including rejected and unauthorised ones:

```json
{"time": "2026-11-02T19:04:11+00:00", "device": "phone-sj", "ip": "192.168.7.23",
 "action": "magnet", "force": false, "result": "queued",
 "infohash": "c9e15763…", "name": "Some.Show.S01E01", "error": null}
{"time": "…", "device": null, "ip": "192.168.7.40", "action": "magnet",
 "result": "unauthorized", "error": "unknown token"}
```

- `device` is `null` when the token didn't match any device; the source IP
  still records where the attempt came from. A revoked token is logged with
  its device name and `result: "revoked"`.
- Over Tailscale (Phase B), the IP is the device's `100.x` address, so remote
  adds stay traceable too.
- Rotation: start a new file at 5 MB and keep 10 old ones. That's years of
  history at home volumes.
- The log is append-only for the API. Nothing in the pipeline reads it, so
  it can't affect downloads.

### Reporting

```
.venv\Scripts\python -m torrentfiler --config config.toml --api-report [--days 30] [--device phone-sj]
```

The report joins the audit log with the torrent database by infohash, so each
add shows **what actually happened to it**, not just that it was accepted:

```
device          adds  known  rejected  filed  downloading  review  failed  purged
phone-sj       14      2         1      9            1       1       1       2
tab-livingroom     3      0         0      3            0       0       0       0
laptop-work        6      1         0      4            1       0       0       1
(unknown)          —      —         4      —            —       —       —       —   ← unauthorised attempts
```

`--device NAME` lists that device's individual adds: time, name, current
state, and `last_error` for anything in review or failed.

**How the device is linked to a torrent.** The main service doesn't need to
know about devices. The audit log records the infohash of every add, and the
database is keyed by infohash, so the report can join them. No database schema
change is needed, and the main service stays untouched.

- Torrents dropped into `inbox\` by hand or via the browser link handler on
  the PC have no audit entry, and the report shows them under `(local)`.
- If one infohash was added by two devices, the report credits the first, and
  the later one appears as `known`.

## Security

| Concern | Measure |
|---|---|
| Strangers on the network adding torrents | A random token per device (`secrets.token_urlsafe(32)`), stored only as a hash. The page asks for it once and stores it in that device's browser. |
| A lost or stolen device | `--api-revoke-device NAME` takes effect immediately; the other devices are unaffected. |
| Knowing who did what | The audit log records the device, IP, and the outcome of every request, including failed authentication. |
| Exposure to the internet | **No router port forward, ever.** Remote access goes through Tailscale (Phase B). |
| Which networks can reach it | Windows Firewall rule: Private profile only, with remote addresses limited to `LocalSubnet` (and `100.64.0.0/10` for Tailscale in Phase B). |
| Token sniffed on the LAN | Plain HTTP on the home LAN is an accepted risk. Tailscale traffic in Phase B is encrypted end to end. |
| Malicious input | Size caps, strict parsing, filenames generated by the API (never taken from the client), writes only into `inbox\` and `force\`. |

### Config addition (`config.toml`)

```toml
[api]
enabled = true
host    = "0.0.0.0"     # all interfaces; the firewall rule does the restricting
port    = 8765
max_torrent_mb = 10
# Managed by --api-add-device / --api-revoke-device; holds token hashes only.
devices_file = 'C:\torrents\api_devices.json'
audit_log    = 'C:\torrents\api_audit.log'
```

There are no tokens in `config.toml`. Device tokens live only (as hashes) in
`devices_file`.

## Clients

Each device is set up with **its own** token from `--api-add-device <name>`.

### Laptops (any OS)

- Open `http://192.168.7.5:8765/` and bookmark it. The first visit asks for
  that laptop's token and remembers it in the browser. The page has:
  - a box to paste a magnet link, with an *Add* button and a *Force* toggle;
  - a file picker for `.torrent` files, which also accepts drag-and-drop;
  - the result shown inline: queued, already known (with state), or the error.
- Optional: register the page as the browser's magnet handler with
  `navigator.registerProtocolHandler`. Clicking a magnet link on that laptop
  then sends it straight to the home PC.

### Android phones and tablets

All mobile devices are Android, so one setup recipe covers them all, each with
its own token.

- **Share sheet (main way):** the free *HTTP Shortcuts* app, set up as a share
  target. The device's token goes in the `Authorization` header.
  - Long-press a magnet link in any app → **Share → torrentfiler** → it POSTs
    to `/api/magnet` and shows the API's response as a toast.
  - A second shortcut takes shared `.torrent` files and POSTs them to
    `/api/torrent`.
- **Browser page:** the same `/` page, added to the home screen
  (*Add to Home screen*) so it opens like an app.
- The setup steps for HTTP Shortcuts (URL, header, body template) go into
  `SETUP_GUIDE.md`, along with an importable shortcut file if practical.

## Phase B: adding while away from home

### Recommended: Tailscale

Tailscale is a free private network between your own devices, encrypted end to
end. The home PC gets a stable address (`100.x.y.z`, or a name like
`home-pc`) that only your devices can reach, from anywhere.

1. Install Tailscale on the home PC, the laptops, and the Android phones and
   tablets, all signed in to the same account. Device tokens don't change:
   the same token works at home and away.
2. Add `100.64.0.0/10` to the API's firewall rule (remote addresses).
3. Point the clients at `http://home-pc:8765/` (Tailscale's MagicDNS name).
   That address works both at home and away, so one bookmark and one HTTP
   Shortcuts setup cover both.
4. Optional: Tailscale ACLs to allow only port 8765 from the phone and tablets
   to the PC.

There are no code changes for Phase B, only setup. That's why Phase A binds
to all interfaces and relies on the firewall.

### Alternatives considered

| Option | Verdict |
|---|---|
| Router port forward of 8765 | **Rejected.** Exposes the API to the internet. It's also impossible if the ISP uses CGNAT (see the setup guide, step 5b). |
| Cloudflare Tunnel plus Cloudflare Access | Workable with no client app, but needs a domain, puts a third party in the path, and is more setup. Keep as a fallback if Tailscale can't be installed on a device. |
| Self-hosted WireGuard | Same result as Tailscale but needs an open port, so it fails under CGNAT. More upkeep. |

## Rollout steps

### Phase A (LAN)

1. Implement:
   - `torrentfiler/api.py` (server and page)
   - `torrentfiler/devices.py` (token file: add, list, revoke, verify)
   - `torrentfiler/audit.py` (audit log writer and `--api-report`)
   - `api_server.py`, the `[api]` config section, and `test_api.py`

   The existing suites must still pass.
2. Test locally with `curl` while the main service runs.
3. Add the `[api]` section to `config.toml`.
4. Issue one token per device: `--api-add-device laptop-work`,
   `--api-add-device phone-sj`, `--api-add-device tab-livingroom`, and so on.
   Note each token when it's printed; it is shown only once.
5. Install the `torrentfiler-api` service with NSSM (depends on nothing; the
   main service picks up whatever it writes).
6. Add the firewall rule: TCP 8765, Private profile, `remoteip=LocalSubnet`.
7. Set up clients, each with its own token: a bookmark on each laptop, and
   HTTP Shortcuts plus a home-screen page on each phone and tablet.
8. Add one torrent from each device and check `--api-report` attributes each
   one correctly.
9. Update `SETUP_GUIDE.md` (new steps, device management, troubleshooting)
   and `README.md`.

### Phase B (remote)

1. Install Tailscale on the PC and every device.
2. Widen the firewall rule to include `100.64.0.0/10`.
3. Switch the clients to the MagicDNS name. Test from mobile data with Wi-Fi
   off.
4. Optional: tighten with Tailscale ACLs.

## Tests planned (`test_api.py`)

Run against a real server on a random local port and temporary folders. No
Transmission is needed.

- A valid magnet is written into `inbox\`; with `force` it goes into `force\`.
- A valid `.torrent` upload is written whole; no temporary file is left behind.
- Missing or wrong token → `401`, nothing written, audit entry with
  `device: null` and the source IP.
- Each device's token is attributed to that device in the response and the
  audit log.
- A revoked token → `401` **without restarting the server**, audited as
  `revoked` with its device name. The other devices' tokens keep working.
- The token file contains no plaintext tokens; a duplicate active device name
  is refused.
- `--api-report` joins the audit log with the database: per-device counts
  match, `(local)` covers torrents added outside the API, and `(unknown)`
  counts unauthorised attempts.
- Invalid magnet or corrupt `.torrent` → `400`, nothing written.
- An oversized upload → `413`, nothing written.
- A known infohash → `known` with its state, nothing written; with `force`,
  it is written.
- Filenames are generated by the server: a hostile `dn=` like `..\..\x` can't
  escape the drop folder.
- End to end with the main service's test fakes: an API add reaches
  `downloading`.

## Open questions

**Decided:**

- **Devices:** laptops plus Android phones and tablets only; no Apple
  devices.
- **Tokens:** one per device, for auditing and per-device reporting.
- **Device names:** `<kind>-<owner or room>`, where kind is `phone`, `tab` or
  `laptop`. Examples: `phone-sj`, `tab-livingroom`, `laptop-work`. The
  `--api-add-device` command enforces the pattern
  (`^(phone|tab|laptop)-[a-z0-9]+(-[a-z0-9]+)*$`), so reports stay
  consistent.
- **No per-device limits:** every active device can add and use `force`, with
  no caps. Control is by issuing and revoking tokens.

**Still open:**

- **Feedback when the download finishes** (for example a phone notification)
  is out of scope for now. If wanted later, a small "notify" hook in the main
  service (for example ntfy.sh, self-hosted, over Tailscale) is the natural
  fit.
