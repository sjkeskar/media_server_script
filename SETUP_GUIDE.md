# torrentfiler — Setup Guide

Every step to build this machine's torrent pipeline from scratch, including
the ones the README skips. Commands are for Windows 10. Where it says
**Admin prompt**, use a Command Prompt or PowerShell opened with *Run as
administrator* (the window title starts with "Administrator:"). Without that,
NSSM and `sc` fail with `Access is denied`.

## What runs where

| Piece | What it does | Location |
|---|---|---|
| `transmission` service | Downloads and seeds torrents | `C:\Program Files (x86)\Transmission\transmission-daemon.exe` |
| Transmission settings | RPC login, limits, download folder | `C:\torrents\transmission\settings.json` |
| `torrentfiler` service | Watches drop folders, adds torrents, files media, cleans up | `Z:\torrent_script` |
| Drop folders | Where you put `.torrent` files | `C:\torrents\watch\inbox\` and friends |
| Downloads | Transmission's working copy, seeded from here | `Z:\torrent_temp` |
| Library | Jellyfin's "Movies And Shows" library | `Z:\jellyfin` |
| State database and log | torrentfiler's records and log | `C:\torrents\torrentfiler.db`, `C:\torrents\torrentfiler.log` |
| `torrentfiler-api` service | Lets laptops, phones and tablets add torrents (port 8765) | `Z:\torrent_script` |
| API device tokens, audit log, log | Who may add, who did add, API errors | `C:\torrents\api_devices.json`, `C:\torrents\api_audit.log`, `C:\torrents\api.log` |
| NSSM | Runs the programs as Windows services | `C:\Tools\nssm\nssm.exe` |

Transmission and torrentfiler must run together: Transmission does the
downloading, and torrentfiler only tells it what to do. torrentfiler is set to
start after Transmission and stop before it. `torrentfiler-api` is optional:
it only writes into the drop folders, so if it's stopped, adding from other
devices stops and everything else carries on.

---

## 1. Install Transmission

1. Install Transmission for Windows from https://transmissionbt.com/download.
2. The installer may register its own `Transmission` service (running as
   LocalService). It is replaced in step 3, because its settings sit in a
   protected folder and LocalService can't write to most folders.

## 2. Install NSSM somewhere permanent

```
winget install NSSM.NSSM
```

winget puts `nssm.exe` in a versioned folder under `%LOCALAPPDATA%`. A winget
upgrade or uninstall would break every service pointing there, so copy it to
a fixed location (**Admin prompt**):

```
mkdir C:\Tools\nssm
copy "%LOCALAPPDATA%\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe" C:\Tools\nssm\
setx /M PATH "%PATH%;C:\Tools\nssm"
```

Open a new Admin prompt so the PATH change takes effect.

**If the services already exist** and point at the winget copy, repoint them.
NSSM keeps each service's settings in the registry under the service name, so
only the executable path changes:

```
sc config transmission binPath= "C:\Tools\nssm\nssm.exe"
sc config torrentfiler binPath= "C:\Tools\nssm\nssm.exe"
```

The space after `binPath=` is required. Restart both services (step 10) for
it to take effect.

## 3. Run Transmission as an NSSM service

**Admin prompt.** Remove the installer's own service first, if it exists.
`sc query Transmission` shows whether it does.

```
sc stop Transmission
sc delete Transmission
```

Then create the NSSM one:

```
mkdir C:\torrents\transmission
nssm install transmission "C:\Program Files (x86)\Transmission\transmission-daemon.exe" "--foreground --config-dir C:\torrents\transmission"
nssm set transmission AppDirectory "C:\Program Files (x86)\Transmission"
nssm set transmission Start SERVICE_AUTO_START
nssm set transmission AppStdout C:\torrents\transmission\daemon.log
nssm set transmission AppStderr C:\torrents\transmission\daemon.log
nssm start transmission
```

- `--foreground` keeps the daemon attached so NSSM can supervise and restart it.
- `--config-dir` gives it a fixed settings folder. Without it, a SYSTEM
  service hides its settings inside the system profile.
- The service runs as LocalSystem, so it can write anywhere on `C:` and `Z:`.
  Both are local NVMe drives. A *mapped network* drive would not be visible
  to a service.

`nssm set` fails with "Service is not a valid NSSM service" if the service
was created by something other than NSSM. That means the installer's
service is still there: go back and delete it.

## 4. Configure Transmission

The first start creates `C:\torrents\transmission\settings.json`.
**Transmission overwrites this file when it exits**, so always stop it before
editing (**Admin prompt**):

```
nssm stop transmission
notepad C:\torrents\transmission\settings.json
nssm start transmission
```

Settings to set:

| Key | Value | Why |
|---|---|---|
| `rpc-enabled` | `true` | torrentfiler talks to Transmission over RPC |
| `rpc-bind-address` | `"127.0.0.1"` | RPC reachable from this PC only |
| `rpc-authentication-required` | `true` | |
| `rpc-username` / `rpc-password` | your choice | Plain-text password is fine; it's hashed on next start |
| `rpc-whitelist-enabled` / `rpc-whitelist` | `true` / `"127.0.0.1"` | |
| `download-dir` | `"Z:\\torrent_temp"` | Backslashes doubled inside JSON |
| `download-queue-size` | `2` | Match `max_active` in `config.toml` |
| `peer-limit-global` | `80` (currently 120) | |
| `peer-limit-per-torrent` | `20` (currently 50; torrentfiler sets 20 per torrent anyway) | |
| `cache-size-mb` | `16` | |
| `alt-speed-up` / `alt-speed-down` | e.g. `50` / `50` | "Turtle mode" quiet profile, in kB/s |
| `peer-port` | `51413` | Used by the firewall and router steps |
| `port-forwarding-enabled` | `true` | Lets Transmission ask the router to open the port (UPnP/NAT-PMP) |
| `ratio-limit-enabled` / `idle-seeding-limit-enabled` | `false` | torrentfiler decides when seeding ends |

**Check:** open http://127.0.0.1:9091. It should ask for the RPC login.

## 5. Open the peer port (firewall and router)

Without this, downloads work but **seeding barely uploads**: other peers can't
connect to you, and the peers you connect to yourself are mostly other seeders
who need nothing. Two things have to be open.

### 5a. Windows Firewall (Admin prompt)

Windows never shows the "Allow access?" popup for a service, so add the rules
by hand:

```
netsh advfirewall firewall add rule name="Transmission daemon" dir=in action=allow program="C:\Program Files (x86)\Transmission\transmission-daemon.exe" profile=private enable=yes
netsh advfirewall firewall add rule name="Transmission peer port TCP" dir=in action=allow protocol=TCP localport=51413 profile=private
netsh advfirewall firewall add rule name="Transmission peer port UDP" dir=in action=allow protocol=UDP localport=51413 profile=private
```

`profile=private` matches this PC's network, which is set to Private. If you
ever switch it to Public, the rules stop applying.

### 5b. Router

This PC is `192.168.7.5` on Ethernet, and the router is `192.168.7.1`.

1. Log in at http://192.168.7.1.
2. **DHCP reservation:** reserve `192.168.7.5` for this PC's MAC address so
   the forward below never points at the wrong machine. `ipconfig /all` shows
   the MAC as "Physical Address".
3. Do **one** of the following:
   - **Enable UPnP** (sometimes called "UPnP IGD" or "NAT-PMP"). Transmission
     then opens the port itself (`port-forwarding-enabled: true`).
   - **Or add a port forward:** external port `51413` → `192.168.7.5` port
     `51413`, protocol **TCP and UDP**. This is more reliable than UPnP.
4. **CGNAT check:** compare the router's WAN/Internet IP with your public IP
   (search "what is my IP" in a browser). If they differ, or the WAN IP starts
   with `100.64`–`100.127`, `10.` or `192.168.`, your ISP uses carrier-grade NAT.
   No router setting can open a port then. Ask the ISP for a public IP, or
   accept download-mostly behaviour.

### 5c. Verify

```
set TR_AUTH=<rpc-username>:<rpc-password>
"C:\Program Files (x86)\Transmission\transmission-remote.exe" -ne -pt
```

Expect `Port is open: Yes`. If it still says No after the firewall rules,
the router or ISP is the problem (5b).

## 6. Python environment

The code needs **Python 3.11 or newer** (`tomllib`). The default `python` on
this PC comes from pyenv and is **3.9**, which won't work, so always use the
project's venv, built with the installed Python 3.13:

```
cd /d Z:\torrent_script
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## 7. Run the tests

```
.venv\Scripts\python test_phase1.py
.venv\Scripts\python test_phase2.py
.venv\Scripts\python test_magnet.py
.venv\Scripts\python test_api.py
.venv\Scripts\python test_status.py
.venv\Scripts\python test_categories.py
.venv\Scripts\python test_remote.py
```

All seven should end with `0 failed`. None of them needs Transmission.

## 8. Configure torrentfiler

```
copy config.example.toml config.toml
notepad config.toml
```

| Section / key | This PC |
|---|---|
| `[paths] watch_root` | `'C:\torrents\watch'` |
| `[paths] temp_download` | `'Z:\torrent_temp'` (same as Transmission's `download-dir`) |
| `[paths] library_root` | `'Z:\jellyfin'` |
| `[paths] database` | `'C:\torrents\torrentfiler.db'` |
| `[transmission] username` / `password` | The RPC login from step 4 |
| `[filing.video]` | copy to `library_root`, seed to ratio `1.0` or `240` hours |
| `[filing.music]` | copy to `'Z:\jellyfin-music'`, seed `3` hours |
| `[filing.iso]` | **move** to `'Z:\download'`, no seeding |
| `[filing.other]` | copy to `'Z:\download'`, seed to ratio `0.5` or `24` hours |
| `[filing] stall_fraction` | `0.3`: seeding ends early after 30% of the seed time with no uploads |
| `[filing] min_free_gb` | `5`: below this, all seeding stops to free space |

Use single quotes around Windows paths so backslashes are literal.
`config.toml` holds the RPC password, so don't share it or commit it.

## 9. Try it once by hand

Only do this while the `torrentfiler` **service is stopped**. A manual run
alongside the service is a second instance working the same folders and
database.

```
.venv\Scripts\python -m torrentfiler --config config.toml --once
.venv\Scripts\python -m torrentfiler --config config.toml --status
```

The first run creates `C:\torrents\watch\{inbox,force,force-dup,processing,done,failed,skipped}`.

## 10. Run torrentfiler as an NSSM service

**Admin prompt:**

```
nssm install torrentfiler "Z:\torrent_script\.venv\Scripts\python.exe" "-m torrentfiler --config Z:\torrent_script\config.toml"
nssm set torrentfiler AppDirectory Z:\torrent_script
nssm set torrentfiler Start SERVICE_DELAYED_AUTO_START
nssm set torrentfiler DependOnService transmission
nssm start torrentfiler
```

- **Delayed start** together with **DependOnService** means Windows always
  starts Transmission first.
- Two `python.exe` processes for one service is normal: a venv's
  `python.exe` is a launcher that starts the real interpreter.

Service commands (**Admin prompt**):

```
nssm status torrentfiler
nssm restart torrentfiler
nssm stop torrentfiler
nssm start torrentfiler
nssm edit torrentfiler        (GUI editor for all service settings)
sc qc torrentfiler            (shows start type and dependencies)
```

**After any code or `config.toml` change, run `nssm restart torrentfiler`.**
The service only reads both at startup.

## 11. Make magnet links open in torrentfiler

Normal prompt (not admin); this is per-user:

```
cd /d Z:\torrent_script
.venv\Scripts\python -m torrentfiler --config config.toml --register-magnet-handler
```

This registers torrentfiler as an application that handles `magnet:` links,
and replaces uTorrent's plain handler entry, saving it so it can be restored.

**Then one manual step.** The command opens Windows Settings for it.
Windows 10 doesn't let programs set a default app, and Transmission's
installer also registered itself for magnet links, so until you choose,
browsers offer **Transmission**:

> Settings → Apps → Default apps → *Choose default apps by protocol* →
> **MAGNET** → pick **torrentfiler**

Test it by clicking any magnet link. The browser asks to open
**torrentfiler**: allow it, and tick "always allow" for that site. A
`.magnet` file should appear in `C:\torrents\watch\inbox\` straight away.

If the browser still offers Transmission after that, it remembered an
earlier choice:

- **Firefox:** Settings → General → Applications → *magnet* → set to
  "Use torrentfiler" or "Always ask".
- **Chrome / Edge / Brave:** the prompt has no saved app. If you once
  ticked "always open", clear it by setting
  `chrome://settings/content/siteDetails?site=<that site>` back to default.

- **Undo:** `.venv\Scripts\python -m torrentfiler --config config.toml --unregister-magnet-handler`
- **uTorrent:** don't run it alongside Transmission (they compete for ports
  and torrents). If uTorrent starts, it may re-register itself as the handler;
  run `--register-magnet-handler` again. Uninstalling uTorrent avoids this.
- If you move the project folder or the venv, run `--register-magnet-handler`
  again, because the handler stores their paths.

## 12. Add torrents from laptops, phones and tablets (home network)

A small REST API, `torrentfiler-api`, accepts magnet links and `.torrent`
files from other devices on the home network and writes them into `inbox\`.
Each device gets its own token, so every add is recorded under the device
that made it. From outside the home network it isn't reachable at all. That
comes later via Tailscale (see `docs/PLAN_remote_add.md`, Phase B).

### 12a. Give this PC a fixed address

Devices find the PC at `192.168.7.5`. Make sure that address doesn't change:
reserve it for this PC in the router's DHCP settings (step 5b).

### 12b. Run the API as a service (Admin prompt)

```
nssm install torrentfiler-api "Z:\torrent_script\.venv\Scripts\python.exe" "-m torrentfiler --config Z:\torrent_script\config.toml --serve-api"
nssm set torrentfiler-api AppDirectory Z:\torrent_script
nssm set torrentfiler-api Start SERVICE_DELAYED_AUTO_START
nssm start torrentfiler-api
```

It doesn't depend on the other services. It only writes into the drop
folders, and the main service picks up whatever it writes. Its settings are
the `[api]` section of `config.toml` (port `8765`). Restart it after changing
them: `nssm restart torrentfiler-api`.

### 12c. Allow it through the firewall (Admin prompt), home network only

```
netsh advfirewall firewall add rule name="torrentfiler API" dir=in action=allow protocol=TCP localport=8765 profile=private remoteip=192.168.7.0/24
```

`remoteip=192.168.7.0/24` allows only devices on your home network. Use the
exact subnet rather than `LocalSubnet`: Windows counts every local network as
"local", including this PC's Hyper-V/WSL network and, once installed,
Tailscale. If the rule already exists, tighten it instead:

```
netsh advfirewall firewall set rule name="torrentfiler API" new remoteip=192.168.7.0/24
```

**Never** forward port 8765 (or 8766) on the router.

Check it from another device's browser: `http://192.168.7.5:8765/api/health`
should show `{"ok": true}`.

### 12d. Issue a token for each device

Names follow `<kind>-<owner or room>`, where kind is `phone`, `tab` or
`laptop`:

```
cd /d Z:\torrent_script
.venv\Scripts\python -m torrentfiler --config config.toml --api-add-device phone-sj
.venv\Scripts\python -m torrentfiler --config config.toml --api-add-device tab-livingroom
.venv\Scripts\python -m torrentfiler --config config.toml --api-add-device laptop-work
```

Each command prints that device's token **once**. Only a hash is stored, so it
can't be shown again. Set the device up straight away (12e or 12f). If a token
is lost, revoke the device and add it again.

```
.venv\Scripts\python -m torrentfiler --config config.toml --api-list-devices
.venv\Scripts\python -m torrentfiler --config config.toml --api-revoke-device phone-sj
```

Revoking works immediately, without restarting anything. The other devices
keep working.

### 12e. Laptops

Open **http://192.168.7.5:8765/** and bookmark it. On first visit, paste that
laptop's token; the browser remembers it. Then:

- paste a magnet link and press **Add**, or
- drop `.torrent` files on the box, or tap it to choose files.

*Force* adds even if the torrent was seen before. Each add shows a result
straight away: *Queued*, *Already in the library / downloading / …*, or
*Not added* with the reason.

The **Status** tab shows the same information as `--status` on the PC:

- **Downloading:** progress bar, speed and ETA.
- **Queued** and **filing**.
- **Filed:** ratio and seeding time against their limits, and where the files
  went.
- **Needs review** and **failed:** with the reason.

It refreshes every 15 seconds while it's open. Tick *finished too* to include
finished torrents. The page remembers which tab you used last.

### 12f. Android phones and tablets

**Home-screen page:** open `http://192.168.7.5:8765/` in Chrome, paste the
device's token, then use *⋮ → Add to Home screen*. It works like the laptop
page, including the **Status** tab, which is the easiest way to keep an eye
on downloads from a phone or tablet.

**Share menu (recommended for adding):** install **HTTP Shortcuts** (free,
Play Store) and create the shortcuts below. Menu names may differ slightly
between app versions.

*Shortcut 1: "Add magnet"* (share a magnet link from any app)

1. **+** → *Regular Shortcut*, name it `Add magnet`.
2. *Basic Request Settings*: method **POST**, URL
   `http://192.168.7.5:8765/api/magnet?format=text`
3. *Request Headers*: add `Authorization` = `Bearer <this device's token>`
4. *Variables* (main screen, **{ }** icon): add a **Text** variable named
   `link`. Open its settings and turn on **allow receiving value from Share
   dialog** (shared text).
5. *Request Body / Parameters*: body type **Custom Text**, content type
   `text/plain`, body: insert the `{link}` variable.
6. *Response Handling*: show the response as a **Toast**.
7. *Trigger & Execution Settings*: make sure the shortcut appears in the
   **Share** menu.

Long-press a magnet link (or *Share* a page or message containing one), then
choose **Share → HTTP Shortcuts → Add magnet**. A toast says *Queued: …*. Any
text around the link is ignored.

*Shortcut 2: "Add torrent file"* (share a downloaded `.torrent`)

1. **+** → *Regular Shortcut*, name it `Add torrent file`.
2. Method **POST**, URL `http://192.168.7.5:8765/api/torrent?format=text`
3. The same `Authorization` header.
4. *Request Body*: body type **File**, receiving the file from the **Share**
   dialog. Content type `application/x-bittorrent`.
5. Response as **Toast**; show in the **Share** menu.

In the file manager or the browser's downloads, *Share* the `.torrent` file
→ **HTTP Shortcuts → Add torrent file**.

Add `&force=1` to either URL for a "force" variant.

*Shortcut 3: "Torrent status"* (one tap, no sharing needed)

1. **+** → *Regular Shortcut*, name it `Torrent status`.
2. Method **GET**, URL `http://192.168.7.5:8765/api/status?format=text`
   (add `&all=1` to include finished torrents).
3. The same `Authorization` header.
4. *Response Handling*: show the response in a **Dialog** (or *Window*), not
   a toast, because it's several lines.

Put it on the home screen (long-press the shortcut → *Place on home
screen*). One tap shows each torrent with its progress, seeding state, or
the reason it needs review. Names get their own line so it reads well on a
phone. Opening the status is recorded in the audit log at most once per
device every 10 minutes, so a page left open doesn't flood it.

### 12g. Who added what

```
.venv\Scripts\python -m torrentfiler --config config.toml --api-report
.venv\Scripts\python -m torrentfiler --config config.toml --api-report --days 30
.venv\Scripts\python -m torrentfiler --config config.toml --api-report --device phone-sj
```

The report shows, per device, what was added and **what became of it**:
filed, still active, waiting for review, or failed. `(local)` covers
torrents added on the PC itself, and `(unknown)` covers attempts with a bad
token. `--device` lists each add separately, with the reason for anything that
went wrong. Every request is recorded in `C:\torrents\api_audit.log` (one JSON
line each, rotated at 5 MB, 10 files kept).

## 13. Add torrents from anywhere (Tailscale)

Remote access goes through **Tailscale**, a private network made of your own
devices only. Traffic is end-to-end encrypted (WireGuard), it works from any
network (mobile data, hotels, behind ISP CGNAT), and **nothing on this PC or
your router is opened to the internet**.

Every request from outside passes four independent checks:

1. **Tailscale:** the device must be signed in to *your* Tailscale account.
   Anyone else can't even find the PC.
2. **Identity:** `tailscale serve` attaches the signed-in user, verified by
   Tailscale. The API refuses any request without it (`403`). That covers
   anything that bypasses the proxy, and Tailscale Funnel if someone ever
   enabled it.
3. **Device token:** the same per-device token as at home.
4. **Lockout:** 5 failed tokens from one address in 10 minutes block that
   address for 15 minutes (`429`).

Every request is audited with the device name, the Tailscale user and the
device's Tailscale address.

**Never do these.** Each one would undo the design:

- `tailscale funnel`: publishes a service to the whole internet.
- A router port forward of 8765 or 8766.
- Setting `[api] host` back to `0.0.0.0`. The service warns in its log if
  you do.

### 13a. Secure the account first

Your Tailscale login becomes the front door, so protect it:

- Sign up at https://login.tailscale.com with a Google or Microsoft account
  that has **two-factor authentication** turned on.
- Admin console → **Settings → Device management**: turn on **Device
  approval**, so new devices need your OK before they join.
- Leave **key expiry** on (the default). Devices re-authenticate
  periodically.

### 13b. Install Tailscale on this PC (Admin prompt)

```
winget install Tailscale.Tailscale
```

Sign in from the tray icon, then in the tray menu tick
**Preferences → Run unattended**. Without it, Tailscale disconnects when you
log off and remote access stops.

In the admin console → **DNS**, make sure **MagicDNS** is on and click
**Enable HTTPS**. The PC then gets a name like
`your-pc.tail1234.ts.net` with a real certificate.

### 13c. Point Tailscale at the API (Admin prompt)

`[api]` in `config.toml` already has `host = "192.168.7.5"` (home network
only) and `tailnet_port = 8766` (loopback only). Apply them, then publish the
loopback port **to your tailnet only**:

```
nssm restart torrentfiler-api
"C:\Program Files\Tailscale\tailscale.exe" serve --bg --https=443 http://127.0.0.1:8766
"C:\Program Files\Tailscale\tailscale.exe" serve status
```

`serve status` should show `https://your-pc.<tailnet>.ts.net (tailnet only)`.
If it ever says *Funnel on*, run `tailscale funnel off` immediately.

`C:\torrents\api.log` should show both listeners:
`home network on http://192.168.7.5:8765/` and
`Tailscale ... on http://127.0.0.1:8766/, identity required`.

### 13d. Phones, tablets and laptops

1. Install the **Tailscale** app (Play Store; tailscale.com/download for
   laptops) and sign in with the **same account**. Approve each device in the
   admin console.
2. **Android:** in the Tailscale app, turn on **Always-on VPN**, or keep it
   connected, so the address works wherever you are. On home Wi-Fi it
   connects directly over the LAN, with no slowdown.
3. Replace `http://192.168.7.5:8765` with
   `https://your-pc.<tailnet>.ts.net` everywhere:
   - the bookmark or home-screen page (re-enter the device token once, since
     the browser treats it as a new site);
   - the three HTTP Shortcuts URLs, for example
     `https://your-pc.<tailnet>.ts.net/api/magnet?format=text`.

   That one address works both at home and away.
4. Test from **mobile data with Wi-Fi off**: open the page, check the Status
   tab, and add a magnet.

A device **without** the Tailscale app can still use
`http://192.168.7.5:8765` while it's on the home Wi-Fi.

### 13e. Optional: restrict who can reach the PC inside your tailnet

If other people or devices share your tailnet, limit the PC to your own
devices on port 443 only. Admin console → **Access controls**:

```json
{
  "tagOwners": { "tag:torrent-pc": ["autogroup:admin"] },
  "acls": [
    { "action": "accept", "src": ["autogroup:member"], "dst": ["tag:torrent-pc:443"] }
  ]
}
```

Then tag this PC: Machines → this PC → **Edit ACL tags** → `tag:torrent-pc`.
Merge this with any rules you already have rather than replacing them,
because an ACL file denies everything it doesn't allow.

### 13f. If a device is lost or stolen

1. Admin console → Machines → that device → **Remove**. It drops off your
   tailnet at once.
2. `--api-revoke-device <name>` revokes its token immediately.
3. `--api-report --device <name>` shows what it did, with the Tailscale user
   and address of every request.

---

## Everyday use

### Adding a torrent

- **Magnet link:** click it in the browser (step 11). It's written into
  `inbox\` for you.
- **From a laptop, phone or tablet:** the page at `http://192.168.7.5:8765/`,
  or *Share → HTTP Shortcuts* on Android (step 12).
- **`.torrent` file:** save or move it into `C:\torrents\watch\inbox\`.
- **Magnet from the command line:**
  `.venv\Scripts\python -m torrentfiler --config config.toml --add-magnet "magnet:?xt=..."`
  (add `--force` to queue into `force\`).

Either way it's picked up after about 20 seconds: two polls to make sure the
file has finished being written. A magnet then needs its metadata from
peers. If none arrives in 30 minutes, it goes to `failed\` as probably dead.

| Folder | Use |
|---|---|
| `inbox\` | Normal. Skipped if this torrent was seen before. |
| `force\` | Add again even if seen before. Also how you retry a torrent in review. |
| `force-dup\` | Reserved for the phase 3 library-duplicate check. |
| `done\` | `.torrent` files of everything filed into the library |
| `failed\` | Broken `.torrent` files and torrents needing review, each with an `.error.txt` note |
| `skipped\` | Duplicates dropped into `inbox\` |

### What happens to it

`queued → downloading → complete → filing → filed (seeding) → purged`

1. Transmission downloads it into `Z:\torrent_temp`.
2. Video, subtitle and cover files are **copied** to
   `Z:\jellyfin\<release folder>\`. Jellyfin picks them up on its next scan.
3. Where it goes and how long it seeds depends on what it is:
   - **Video:** copied to `Z:\jellyfin`, seeds until ratio 1.0 or 10 days.
   - **Music:** copied to `Z:\jellyfin-music`, seeds 3 hours.
   - **ISO:** moved to `Z:\download` straight away, with no seeding and no
     duplicate.
   - **Anything else:** copied to `Z:\download`, seeds until ratio 0.5 or
     1 day.

   When seeding ends, it is removed from Transmission and the temp copy
   deleted. If nothing is uploaded for 30% of the seed time, seeding ends
   early.
4. If disk space drops below `min_free_gb`, all filed torrents stop seeding
   at once, and their temp copies are deleted to make room.

**needs_review** means it stopped and is waiting for you. The reason is in the
`.error.txt` note in `failed\`. Common causes:

- A different file with the same name already in the library.
- A library copy deleted before seeding finished.

Fix the cause, then re-queue it with `--refile`:

```
.venv\Scripts\python -m torrentfiler --config config.toml --refile <part of its name>
.venv\Scripts\python -m torrentfiler --config config.toml --refile all
```

`--refile all` re-queues everything in review.

### Checking on things

```
cd /d Z:\torrent_script
.venv\Scripts\python -m torrentfiler --config config.toml --status
.venv\Scripts\python -m torrentfiler --config config.toml --status --all
```

`--status` lists every torrent by state:

- **Downloading:** progress, speed, ETA and peers.
- **Queued** and **filing**.
- **Filed:** ratio and days seeded against the 1.0 / 10-day limits, and its
  library folder.
- **Needs review** and **failed:** each with its reason.

`--all` also lists finished and skipped torrents, which are otherwise only
counted. It only reads, so it's safe while the service runs. If Transmission
is down, it shows what the database knows.

- **Web UI:** http://127.0.0.1:9091. Use the filter to show *Seeding* and see
  upload speed and ratio.
- **Command line:**
  ```
  set TR_AUTH=<rpc-username>:<rpc-password>
  set TR="C:\Program Files (x86)\Transmission\transmission-remote.exe"
  %TR% -ne -l              (list torrents)
  %TR% -ne -t all -i       (details: peers, uploaded, seeding time)
  %TR% -ne -pt             (is the peer port open?)
  ```
- **Qt app:** in `transmission-qt`, use *Edit → Change Session → Connect to
  remote session* with `127.0.0.1:9091`. Never let it start its own local
  session, because it clashes with the daemon's ports.
- **Logs:** `C:\torrents\torrentfiler.log` (rotates at 2 MB),
  `C:\torrents\api.log` (API errors), and `C:\torrents\transmission\daemon.log`.
- **Adds from other devices:** `--api-report` (step 12g).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `OpenService(): Access is denied` | Prompt not elevated | Use an Admin prompt |
| `Service "Transmission" is not a valid NSSM service!` | The installer's own service is still there | `sc stop Transmission`, `sc delete Transmission`, redo step 3 |
| `ModuleNotFoundError: tomllib` or a syntax error | Ran with pyenv's Python 3.9 | Use `.venv\Scripts\python` |
| Settings changes lost | Edited `settings.json` while Transmission was running | Stop, edit, start |
| `401 Unauthorized` at http://127.0.0.1:9091 | Expected; RPC requires login | Log in with the RPC username and password |
| Seeding with ratio stuck at 0.00 | Peer port closed | Step 5 |
| Nothing picked up from `inbox\` | torrentfiler stopped, or not a `.torrent` | `sc query torrentfiler`, check the log |
| Torrent stuck in `filing` | Not enough space and nothing left to free | Free space on `Z:`; it retries, then goes to review |
| Services broke after a winget upgrade | Services pointed at winget's NSSM copy | Step 2, "If the services already exist" |
| Clicking a magnet opens uTorrent | uTorrent re-registered itself, or step 11 not done | Run `--register-magnet-handler` again |
| Browser offers to open Transmission | Default app for MAGNET not picked, so Windows prefers Transmission's registration | Step 11's manual step: pick torrentfiler for MAGNET in Default apps |
| Clicking a magnet does nothing | Browser prompt dismissed, or project or venv moved | Click again and allow; re-run step 11 |
| Magnet in `failed\` with "no metadata" | Dead or poorly seeded magnet | Try a different source, or re-drop it to retry |
| Phone or laptop: "Could not reach the PC" or a timeout | API service stopped, firewall rule missing, PC's IP changed, or the device is on mobile data or a guest network | `nssm status torrentfiler-api`; step 12c; check `ipconfig` shows 192.168.7.5; join the home Wi-Fi |
| "unknown token" | Token mistyped, or from another device | Re-enter it; if lost, `--api-revoke-device` then `--api-add-device` |
| "the token for … has been revoked" | That device was revoked | Issue a new one with `--api-add-device` |
| "Already in the library: …" | The torrent was added before | Expected; tick *Force* (or `&force=1`) to add it again anyway |
| Remote: page or shortcut times out away from home | Tailscale app off on the device, PC offline or not *Run unattended*, or `serve` not set | Turn Tailscale on; `tailscale status` on the PC; step 13c |
| Remote: "not from a signed-in device on your Tailscale network" | Request didn't come through `tailscale serve` with a signed-in user (e.g. a tagged device, or Funnel) | Use a device signed in as a user; check `tailscale serve status` shows *tailnet only* |
| "too many failed attempts; try again in N min" | 5 bad tokens from that address in 10 minutes | Wait it out, fix the token in the device's settings; see `--api-report --device` |
| `torrentfiler-api` won't start: "cannot assign requested address" | `[api] host` IP changed (DHCP) | Reserve 192.168.7.5 in the router (step 12a), or update `host` |
| `torrentfiler-api` won't start, log says the address is in use | Something else is on port 8765 | Change `[api] port` in `config.toml`, the firewall rule and the device URLs |
