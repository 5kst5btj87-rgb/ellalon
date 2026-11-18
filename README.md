# Ellalon – Modular Security Management Platform

Docker-based security platform for Ubuntu. One **core** container manages every other container
(module): VMS recording, live streaming, video wall, analytics, face recognition, API
connectivity, command & control and 3rd-party integrations (RISCO, Apollo). Every client (web
included; Windows and mobile later) uses the same REST + WebSocket API.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.

## Install on a new server

Copy the installer file to the server (Ubuntu 22.04 / 24.04 / 26.04, x86-64 or ARM64) and run:

```bash
sudo bash ellalon-installer-<version>.run
```

The installer installs Docker if needed, unpacks Ellalon to `/opt/ellalon`, generates secrets,
builds the images, starts everything and prints the web address and admin password.

| Option | |
|---|---|
| `--port 8080` | web port (default 80) |
| `--data /mnt/storage/ellalon` | where recordings and the database live (default `/var/lib/ellalon`) |
| `--all-modules` | also build analytics and face recognition |
| `--gpu` | install the NVIDIA Container Toolkit (GPU analytics / face recognition) |
| `--restore-db FILE` | load a database backup into the new installation |
| `--extract DIR` | only unpack the files |

Running the installer again on the same server upgrades it and keeps settings and data.

### Online install (one command)

On the new server:

```bash
curl -fsSL https://raw.githubusercontent.com/5kst5btj87-rgb/ellalon/main/install-online.sh | sudo bash
```

With options: `... | sudo bash -s -- --port 8080 --data /mnt/recordings`.
Later updates are just `sudo ellalon update` (settings and data are kept).

### Offline installer (sites without internet)

On a running Ellalon server with the same CPU type as the target (x86-64 or ARM64):

```bash
sudo ellalon bundle --offline        # -> /opt/ellalon/dist/ellalon-installer-<v>-offline-<arch>.run
```

It contains all Docker images, so the target needs no internet for Ellalon itself (Docker must be
installed, or reachable through the internet, on the target).

### Moving a site to a new server

```bash
sudo ellalon backup                  # on the old server: database + module settings
sudo bash ellalon-installer-<v>.run --restore-db ellalon-db-<date>.sql --no-start   # new server
sudo tar -xzf ellalon-modules-<date>.tar.gz -C /var/lib/ellalon/modules && sudo ellalon up
```

Copy recordings separately if you need them (`/var/lib/ellalon/modules/vms-recorder/recordings`).

## Recording storage

Web UI → **Storage**: add where recordings are kept.

| Type | Example | Notes |
|---|---|---|
| Local disk / folder | `/mnt/disk2/recordings` | any folder on the server, e.g. a second disk |
| SMB share | `//192.168.1.20/recordings` | Windows shares, Synology, QNAP; user needs read/write |
| NFS share | `192.168.1.20:/volume1/recordings` | allow the server's IP with read/write on the NAS |

Each storage has its own *keep days* and *keep free GB*. Cameras record to the default storage,
or to the one chosen in the camera's settings (Cameras → Edit → Recording storage; can be copied
to other cameras). If a network share is down, its cameras record to the default storage until
it is back. **Test** checks the connection and write access before saving.

### Recording servers (other Ubuntu / Windows machines)

Storage → **+ Add storage** → type **Recording server**. Save, and copy the one-line command shown
(one-time pairing code, valid 24 h) to the machine that will record:

```bash
# Ubuntu / Debian
curl -fsSL https://raw.githubusercontent.com/5kst5btj87-rgb/ellalon/main/recorder/install-recorder.sh | sudo bash -s -- --server http://ELLALON --code XXXX-XXXX
```
```powershell
# Windows 10 / 11 / Server - PowerShell as Administrator
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/5kst5btj87-rgb/ellalon/main/recorder/install-recorder.ps1))) -Server http://ELLALON -Code XXXX-XXXX
```

Then set **Cameras → Edit → Recording storage** to that recording server. It records those
cameras locally, keeps its camera list on disk, keeps recording when the Ellalon server is down,
and starts recording at boot even if the Ellalon server is unreachable (systemd service on Ubuntu,
SYSTEM startup task on Windows). Playback goes through the Ellalon server (recorder TCP 8090).
On the recorder: `sudo ellalon-recorder status` (Ubuntu) or see `C:\ProgramData\EllalonRecorder\recorder.log`.

## Everyday commands

| | |
|---|---|
| `sudo ellalon status` | core services and module containers |
| `sudo ellalon logs core` | follow a log (core, vms-recorder, integrations, ...) |
| `sudo ellalon restart` | restart everything |
| `sudo ellalon password [user]` | set a new password |
| `sudo ellalon backup` | back up the database and module settings |
| `sudo ellalon build <module>` | build one module (e.g. after enabling an optional module) |
| `sudo ellalon update` | install the latest version from GitHub (online installs) |
| `sudo ellalon bundle [--offline]` | create an installer file |

## Layout

```
core/            main manager container (FastAPI + Docker SDK)
sdk/python/      module SDK (heartbeat, bus, commands, core API client)
modules/<name>/  one folder per module: module.yaml + Dockerfile + code
templates/module copy this to start a new module
web/             web client (served by core at /)
installer/       installer and installer builder
```

## Adding a module

```bash
cp -r templates/module modules/lpr && $EDITOR modules/lpr/module.yaml modules/lpr/app.py
sudo ellalon build lpr
```

Web UI → System → **Rescan modules** → **Enable**. Core creates the container, routes
`/modules/lpr/` to it behind the login, and tracks its health.

## API

- Interactive docs: `http://<server>/api/docs` and `/modules/<name>/docs`.
- Login: `POST /api/auth/login {"username","password"}` → `{"token"}`; then `Authorization: Bearer <token>`.
- Live events: `ws://<server>/api/ws?token=<token>`.
- C&C: `POST /api/commands/videowall {"command":"push_camera","args":{"wall":"Main wall","tile":0,"camera_id":3}}`.

## Production checklist

- Put TLS on the web port and close plain HTTP; keep the server on its own VLAN.
- Change the admin password and create named operator accounts.
- Size the data disk for recordings (about 10.8 GB per Mbps per camera per day).
- Face recognition is biometric processing: confirm the legal basis before enabling it.

### Camera discovery and bulk delete
Cameras → **Discover** scans the server's networks (or ranges you enter) with ONVIF, Hikvision SADP and an RTSP port scan,
shows vendor/model/MAC, and adds the ticked cameras with one username/password. Tick rows to **Delete selected**, or **Delete all**
(type DELETE to confirm). Deleting a camera keeps its saved recordings.

### Camera analytics events and the Alarm manager
Edit a camera → **Camera analytics events**: receive detections straight from the camera — Hikvision event stream (ISAPI),
Dahua event stream, or ONVIF events for Axis, Provision, FLIR and other ONVIF cameras (HTTP/ONVIF port).
**Find event types** lists what the camera offers; tick the ones you want (none = all). With **Send to Alarm manager**
each detection raises a `camera.alarm` at the chosen priority; otherwise it is only logged in Events. The Cameras table
shows an Events column (Listening / Error, last event). The **Alarm manager** tab (formerly Incidents) groups repeats of an
open alarm (count), offers Live view, Acknowledge/Close (single, selected or all), an open-alarm badge, pop-ups and an optional sound.
