# Arend's Twitch Drops Miner

Mines Twitch drops without downloading stream video: the miner sends the same
watch events a browser would, tracks campaign progress, claims finished drops,
and switches channels automatically. Everything is managed from a web dashboard.

## Lineage

- Original project by [DevilXD](https://github.com/DevilXD/TwitchDropsMiner) (desktop GUI).
- Headless web fork by [rangermix](https://github.com/rangermix/TwitchDropsMiner) (v1.2.4 base).
- Feature fork by [SimpliAj](https://github.com/SimpliAj/twitchdropsminer) (v1.3.15), merged into this fork on 2026-07-16: multi-account, parallel instances, predictions, Discord integration, and a reworked web UI.
- This fork ([amdschuurman/TwitchDropsMiner](https://github.com/amdschuurman/TwitchDropsMiner)) adds security and reliability hardening on top.

## Features

From the SimpliAj merge:

- Multi-account support: each Twitch account lives in its own isolated `data/accounts/<name>/` directory (cookies, settings, drop history, channel points); switch accounts from the System tab or via the `/api/accounts` REST API.
- Parallel instances: run several fully isolated miner processes at once, configured with `TDM_PORT` and `TDM_DATA_DIR`; add or remove instances from the System tab.
- Channel points auto-claimer (WebSocket plus 60s GQL polling fallback) and idle watch on followed or configured channels when no campaigns are active.
- Predictions auto-betting.
- Discord webhooks (drop claimed, channel points chest) and a dedicated Discord bot with slash commands, live dashboard embed, and multi-server notifications (`discord_bot/`).
- Campaign alerts, push notifications, stats and drop history APIs.
- Reworked dark web UI with Main, Inventory, Channel Points, History, Analytics, Settings, System and Help tabs, campaign drops modal, drop name blacklist, mobile-responsive layout.
- Immediate drop claiming, Spade watch-event fix, corrupt-cache JSON recovery.

From this fork:

- Bearer-token auth gate: a token in `data/api_token` (mode 0600) gates state-changing endpoints and the Socket.IO handshake; browsers get an httpOnly cookie via a one-time bootstrap URL printed at startup. Loopback clients bootstrap automatically.
- Optional `WEB_PASSWORD` dashboard login (from SimpliAj) works alongside this.
- CORS locked to same-origin by default; extendable with `TDM_TRUSTED_ORIGINS`.
- Proxy URL validation and a non-root Docker container (UID 1000) with an entrypoint that fixes volume ownership on upgrade.
- Reliability hardening: the main loop recovers from miner, request, GQL and websocket errors with exponential backoff; watch loops have timeouts; online checks tolerate per-batch GQL failures.
- CI publishes multi-arch images to `ghcr.io/amdschuurman/twitchdropsminer`.

## Quick start

Docker Compose:

```yaml
services:
  twitch-drops-miner:
    image: ghcr.io/amdschuurman/twitchdropsminer:latest
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
    environment:
      - TZ=Europe/Amsterdam
      - WEB_PASSWORD=yourpassword   # optional dashboard password
    restart: unless-stopped
```

```bash
docker compose up -d
```

From source:

```bash
pip install -e .
python main.py
```

Open `http://localhost:8080`, log in with the Twitch OAuth device flow, pick
the games to farm under Settings, and the miner runs on its own. If you open
the dashboard from another machine, use the bootstrap URL printed at startup
(the line `Bootstrap URL: http://.../?token=...`) once; after that a cookie
handles auth.

Link your Twitch account to your game accounts at
<https://www.twitch.tv/drops/campaigns> or drops will not credit.

## Multi-account parallel setup

Each instance is an independent process with its own port and data directory.
Instance 1 runs on port 8080 with `data/`; additional instances use ports
8082, 8084, ... and `data2/`, `data3/`, ... Instances can also be managed from
the System tab in the dashboard.

Docker Compose, two accounts:

```yaml
services:
  tdm-account1:
    image: ghcr.io/amdschuurman/twitchdropsminer:latest
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
    environment:
      - TZ=Europe/Amsterdam
      - WEB_PASSWORD=yourpassword
      - TDM_PORT=8080
      - TDM_DATA_DIR=data
    restart: unless-stopped

  tdm-account2:
    image: ghcr.io/amdschuurman/twitchdropsminer:latest
    ports:
      - "8082:8082"
    volumes:
      - ./data2:/app/data
      - ./logs2:/app/logs
    environment:
      - TZ=Europe/Amsterdam
      - WEB_PASSWORD=yourpassword
      - TDM_PORT=8082
      - TDM_DATA_DIR=data
    restart: unless-stopped
```

From source with PM2:

```bash
TDM_PORT=8080 TDM_DATA_DIR=data   pm2 start main.py --name twitchdrops  --interpreter python3
TDM_PORT=8082 TDM_DATA_DIR=data2  pm2 start main.py --name twitchdrops2 --interpreter python3
pm2 save
```

Or plainly in two terminals:

```bash
TDM_PORT=8080 TDM_DATA_DIR=data   python main.py   # terminal 1
TDM_PORT=8082 TDM_DATA_DIR=data2  python main.py   # terminal 2
```

Nginx reverse proxy serving two accounts from one domain:

```nginx
server {
    listen 443 ssl;
    server_name tdm.example.xyz;

    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host $host;
    }

    location /acc2/ {
        proxy_pass         http://127.0.0.1:8082/;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host $host;
    }
}
```

The dashboard shows account switcher buttons for every running instance;
`?acc=2` in the URL jumps directly to instance 2. Running 3 or more instances
from one IP may get flagged by Twitch; the dashboard warns about this.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `TDM_HOST` | `127.0.0.1` | Bind interface for the web server; set `0.0.0.0` to allow LAN access. |
| `TDM_PORT` | `8080` | Listening port; also selects the instance in parallel mode. |
| `TDM_DATA_DIR` | `data` | Data directory for this instance (cookies, settings, history). |
| `TDM_LABEL` | `Instance <port>` | Display name for this instance in the dashboard. |
| `WEB_PASSWORD` | unset | Password-protects the dashboard (30-day session cookie). Unset means no password prompt. |
| `TDM_AUTH_DISABLED` | unset | `true` disables the bearer-token auth gate. Only for deployments where a reverse proxy (Authelia, Cloudflare Access, an NPM access list) already enforces access control. |
| `TDM_TRUSTED_ORIGINS` | unset | Comma-separated extra origins allowed by CORS and the Socket.IO handshake, e.g. `https://tdm.example.xyz`. Needed when serving the UI through a reverse proxy on another origin. |
| `TDM_ALLOW_PRIVATE_WEBHOOKS` | unset | `true` allows Discord-webhook URLs that resolve to private/LAN addresses (self-hosted receivers). By default such URLs are rejected as an SSRF guard. |
| `TZ` | UTC | Container timezone, used for timestamps in stats and alerts. |

## Security notes

- The web server binds to loopback by default. For remote access, either bind
  `0.0.0.0` and use the bootstrap URL and `WEB_PASSWORD`, or put the miner
  behind an authenticating reverse proxy and set `TDM_AUTH_DISABLED=true`
  plus `TDM_TRUSTED_ORIGINS`.
- The API token lives in `data/api_token`; delete the file to rotate it.
- The Docker image runs as UID 1000. On the first start after upgrading from
  a root-based image, the entrypoint re-chowns `./data` and `./logs`;
  `data/cookies.jar` (your Twitch session) survives the upgrade.
- Discord bot secrets belong in `discord_bot/.env` (gitignored), never in the
  repository.

## Notes

- Do not watch Twitch on the mining account in a regular browser at the same
  time; it desyncs progress.
- Requires Python 3.12 or newer when running from source. Persistent state is
  stored in `data/`; back it up before updating.
- Screenshots of the dashboard are in `docs/screenshots/`.

## Acknowledgments

- [DevilXD](https://github.com/DevilXD), creator of the original TwitchDropsMiner, and all its translation contributors.
- [rangermix](https://github.com/rangermix) for the headless web rework.
- [SimpliAj](https://github.com/SimpliAj) for multi-account, predictions, Discord integration, and the web UI rework merged into this fork.

Use at your own risk; automated viewing may violate Twitch's terms of service.
