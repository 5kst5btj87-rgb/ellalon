# Ellalon – Architecture

```
                    Clients: Web (included) · Windows (.NET, future) · Mobile · 3rd-party
                                          │  HTTPS / WSS (one API)
                                   ┌──────▼──────┐
                                   │   Traefik   │  :80 / :443   /api/* → core
                                   └──────┬──────┘              /modules/<name>/* → module (forward-auth by core)
         ┌────────────────────────────────┼─────────────────────────────────────────┐
         │                    ellalon_net (Docker network)                          │
         │  ┌──────────────────────────┐                                              │
         │  │  CORE  (main container)  │── Docker API (via socket-proxy) ──► creates, │
         │  │  • module lifecycle      │                                    starts,   │
         │  │  • reconciler / health   │                                    stops,    │
         │  │  • users / JWT / roles   │                                    monitors  │
         │  │  • cameras, events DB    │                                      │       │
         │  │  • WebSocket event hub   │                                      ▼       │
         │  │  • C&C command relay     │   ┌────────────┐ ┌────────────┐ ┌──────────┐ │
         │  └────┬───────────┬─────────┘   │vms-recorder│ │ streaming  │ │videowall │ │
         │       │           │             └────────────┘ │  (go2rtc)  │ └──────────┘ │
         │  ┌────▼───┐  ┌────▼────┐        ┌────────────┐ └────────────┘ ┌──────────┐ │
         │  │Postgres│  │  Redis  │◄──bus──│ analytics  │ ┌────────────┐ │    c2    │ │
         │  └────────┘  └─────────┘        └────────────┘ │face-recog. │ └──────────┘ │
         │                                 ┌────────────┐ └────────────┘              │
         │                                 │api-connect.│ │integrations│ … future modules │
         │                                 └────────────┘                              │
         └──────────────────────────────────────────────────────────────────────────────┘
```

## Principles

1. **Core is the only privileged container.** It reaches Docker through `docker-socket-proxy`
   (never the raw socket) and is the only thing that creates module containers.
2. **A module = a folder with `module.yaml`.** Core discovers it, stores its desired state
   (enabled/disabled + env overrides) in Postgres and the reconciler loop keeps reality in line
   (restarts crashed modules, honours `depends_on`).
3. **Modules never talk to each other directly.** They use the bus:
   | Channel / key | Direction | Use |
   |---|---|---|
   | `ellalon.events` | module → core | every event; core stores it and pushes to clients |
   | `ellalon.broadcast` | core → all | `camera.changed`, config changes |
   | `ellalon.cmd.<module>` | core → module | C&C commands; result returns as `command.result` event |
   | `ellalon:hb:<module>` | module → core | heartbeat (TTL) = health in UI |
4. **One API for every client.** Web, Windows, mobile and integrations all use the core REST API
   (`/api/docs` = OpenAPI) plus the `/api/ws` event stream. Module-specific APIs are reachable at
   `/modules/<name>/` and protected by the same tokens via Traefik forward-auth.
5. **Data lives on the host** under `ELLALON_DATA_ROOT/modules/<name>/…`, so containers are
   disposable and upgrades are `build → recreate`.

## Roles
`viewer` (watch) · `operator` (also acknowledge incidents, send commands, write to module APIs) ·
`admin` (also users, cameras, enable/disable modules). Modules get a service token (`role=module`).

## Event model
```json
{"id": 123, "ts": "...", "source": "face-recognition", "type": "face.match",
 "severity": "info|low|warning|high|critical", "camera_id": 4, "payload": {...}}
```

## Shipped modules
| Module | Status | Notes |
|---|---|---|
| vms-recorder | working | FFmpeg stream-copy, 5-min MP4 segments, retention by age + free space, playback API |
| streaming | working (upstream go2rtc) | WebRTC/MSE live view for browser & clients |
| videowall | working | layouts, walls, `push_camera` command, keeps go2rtc in sync |
| analytics | working (motion) | detector plug-in interface for YOLO / LPR / line-crossing |
| face-recognition | working with InsightFace build | watchlist enrolment + live matching; GPU optional |
| api-connector | working | signed outbound webhooks, inbound events from 3rd-party systems |
| c2 | working | rules → incidents + automatic actions, incident workflow |
| integrations | working (RISCO), beta (Apollo) | RISCO panels on the LAN: import, live state, arm/disarm/bypass; Apollo APACS: doors, events, unlock; device → camera links |

## Roadmap suggestions
- **Windows client**: .NET 8 WPF/WinUI app using the same REST + WebSocket API; video via
  WebView2 (go2rtc WebRTC) or FFmpeg/LibVLC directly on RTSP for low-latency decoding.
- **ONVIF module**: discovery, PTZ, camera events (motion/tamper) → bus.
- **Scale-out**: run modules on extra Ubuntu nodes (Docker Swarm or a lightweight agent per node
  that core talks to); move recordings to MinIO / NAS.
- **HA**: Postgres replication, Redis replication, two core instances behind a VIP.
- **TLS**: add Let's Encrypt or your CA to Traefik (`--entrypoints.websecure`), then disable :80.
- **Audit log** table for all operator actions; per-camera permissions.
