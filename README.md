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
- Watch list guard: a settings write can no longer empty the games-to-watch list without an explicit user gesture, and `GET /api/health/mining` gives an uptime monitor something to alert on when there is nothing left to mine.
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
| `WEB_PASSWORD` | unset | Password-protects the dashboard. Unset means no password prompt. |
| `TDM_SESSION_TTL` | `permanent` | How long a login lasts. `permanent` (default) stays logged in indefinitely; `session` drops it when the browser closes; `12h` / `7d` keeps it for that long. |
| `TDM_AUTH_DISABLED` | unset | `true` disables the bearer-token auth gate. Only for deployments where a reverse proxy (Authelia, Cloudflare Access, an NPM access list) already enforces access control. |
| `TDM_TRUSTED_ORIGINS` | unset | Comma-separated extra origins allowed by CORS and the Socket.IO handshake, e.g. `https://tdm.example.xyz`. Needed when serving the UI through a reverse proxy on another origin. |
| `TDM_ALLOW_PRIVATE_WEBHOOKS` | unset | `true` allows Discord-webhook URLs that resolve to private/LAN addresses (self-hosted receivers). By default such URLs are rejected as an SSRF guard. |
| `TZ` | UTC | Container timezone, used for timestamps in stats and alerts. |

## Watch list safety

An empty games-to-watch list means the miner mines nothing, and no error is
raised while it sits idle, so that state can only be reached deliberately.

- `auto_clean_watchlist` (Settings tab, "Auto-remove fully claimed games from
  the watch list") removes a game from the watch list once every campaign for
  it is fully claimed. It is off by default, and the removal is permanent, so
  turn it on only if you look at the watch list yourself now and then. Even
  when it is on, the cleanup refuses to remove the last remaining game.
- Emptying the list takes an explicit action in the dashboard: unchecking the
  last game, "Deselect All", or removing the last entry from the wanted queue.
  Any other request that would leave the list empty is ignored, and the server
  logs `Refused to clear the games-to-watch list without explicit intent` to
  the dashboard console.
- A request that clears the list must also say which list it believed was
  stored. A dashboard tab left open since before you added a game would
  otherwise delete games it never showed: removing the last game it can see and
  clearing a longer list are the same request. When the two disagree the write
  is refused with `Refused to clear the games-to-watch list: this request
  expected [...] to be stored, but the stored list is [...]`, the tab resyncs,
  and you can reload and try again.
- A value the server rejects (an empty language, for example) is logged as
  `Setting rejected: <key> = <value> (<reason>)` and skipped. The rest of the
  same save still applies, and the rejected value is never stored.

The reason for all of them: the dashboard used to run that cleanup on every page
load and socket reconnect and save the result. On 2026-07-24 it emptied the
watch list of an account watching a single game whose drops were all claimed,
and the miner mined nothing for the next five days without reporting a problem.

### Other settings that stop mining

An empty watch list is not the only way to end up mining nothing while the
dashboard looks healthy, so the same shape of protection covers the rest.

- **Mining benefits.** With every benefit type unchecked no drop counts as
  wanted, so no game is ever selected and mining stops with the whole watch list
  still on screen. Turning them all off stays possible, because "pause mining
  but keep my watch list" is a real thing to want, and it takes the same kind of
  deliberate action as clearing the list. Anything else that would leave nothing
  enabled is refused with `Refused to disable every mining benefit type without
  explicit intent`, and `GET /api/health/mining` reports `benefits_disabled`.
  A save that only mentions some of the four types no longer switches the others
  off: the selection is merged, not replaced.
- **Scheduler window.** `scheduler_start` and `scheduler_stop` must be `HH:MM`.
  A value the scheduler cannot read (`22`, `25:00`, an empty box) used to kill
  the scheduler task for the rest of the run, and if it had already paused
  mining nothing was left to lift the pause. Such a value is now rejected on
  write, and one already sitting in `settings.json` is replaced by the default
  at the next start, with the reason logged.

### When `settings.json` cannot be read

A `settings.json` the miner cannot parse is no longer quietly replaced by
defaults. That covers an interrupted write, a bad disk block and a hand edit
with a comma missing, and also a file that is valid JSON but not the right shape
at all: a list or a bare number where the settings object belongs used to crash
the miner on every start, which under `restart: unless-stopped` is a restart
loop that preserves nothing and reports nothing. All of them now take the same
route. The unreadable file is moved aside to `settings.json.corrupt` next to it,
and the log says so:

```text
Corrupt JSON in data/settings.json: <reason>. The unreadable file has been kept
as data/settings.json.corrupt - recover any values you need from it. Starting
from defaults for now, which for settings.json means an EMPTY games-to-watch
list, so nothing will be mined until it is set again.
```

Defaults mean an empty watch list, so the miner mines nothing until you set one,
and `GET /api/health/mining` answers `"ok":false` for exactly that reason. Your
old list is still there in `settings.json.corrupt`: open it in a text editor,
copy the `games_to_watch` entries back in from the Settings tab, then delete the
file. A second corruption does not overwrite the first one, it becomes
`settings.json.corrupt.1`, then `.2`, and so on. Preserved copies older than
thirty days are eventually cleaned up, except the oldest one, which is kept
whatever its age because it is the copy most likely to hold your real settings.

The other line worth recognising is:

```text
Refused to save an empty games-to-watch list over the stored ['Overwatch'] -
nobody asked for it to be cleared, and an empty list means nothing gets mined.
Restoring the stored list.
```

Something tried to save an empty watch list while a non-empty one was on disk
and no deliberate "clear all" was behind it. The stored list is put back both in
memory and on disk, and mining carries on. Emptying the list on purpose from the
dashboard is unaffected. There is a matching line for the mining benefits,
`Refused to save a mining-benefits selection with every benefit type disabled
over the stored ...`, which works the same way.

One more line comes from upgrading or downgrading rather than from damage:

```text
Unknown keys in stored JSON, discarded: data/settings.json:some_old_setting.
This version has no setting by those names, so whatever they held is gone -
recover it from a backup if it mattered.
```

That is a setting this version does not have being dropped from your file. It
used to happen in silence. Your own per-channel data is never dropped this way:
settings that hold a free-form map, such as the per-channel betting strategies,
are left exactly as you wrote them.

## Health and monitoring

`GET /api/health/mining` reports whether the miner has anything to mine. It
needs no authentication and always answers HTTP 200, so it cannot interfere
with the Docker health check (`/healthz`, unchanged) or a container restart
policy.

Point the monitor at that exact path, with GET and no trailing slash. The
allowlist that makes the endpoint public matches by exact path, so
`/api/health/mining/` is a different, gated URL: it answers 401 without a
credential, and a bodyless 307 redirect with one. Neither reply contains an
`ok` field, and a monitor that alerts by inverting a match on `"ok":false`
counts "keyword absent" as healthy, so one trailing slash buys a monitor that
stays green through the whole outage it was installed to catch.

```json
{
  "ok": false,
  "watchlist_empty": true,
  "benefits_disabled": false,
  "games_to_watch_count": 0,
  "wanted_games_count": 0,
  "state": "idle",
  "mining": false,
  "watch_stalled": false,
  "last_watch_age_seconds": null,
  "uptime_seconds": 42,
  "login": "yourtwitchname",
  "login_pending": false
}
```

`ok` is false whenever there is nothing to mine, nobody is logged in, the miner
has stopped making progress, it has not finished starting up, or the probe could
not read part of its own state. That is the field to alert on: point an uptime
monitor at the endpoint, have it keyword-match the literal `"ok":false`, and
invert the match so a hit counts as down.

Two of those causes are about the miner being stuck rather than misconfigured.
`login` is null and `login_pending` is true while the miner is waiting for
somebody to approve a device code at twitch.tv/activate: it will sit there
indefinitely, having never started watching anything, so it counts as not ok.
`watch_stalled` is true when the miner has games it wants, is not deliberately
paused, and has not successfully sent a watch tick for several minutes;
`last_watch_age_seconds` is how long it has been, or null if it has not managed
one yet this run. `uptime_seconds` is there so a monitor can tell a genuine
stall from a process that only just started.

An account with games configured and no active campaign right now is **not** an
error. That is the ordinary state of a single-game account between campaigns,
sometimes for days, and it reports `ok:true` with `wanted_games_count: 0`. A
probe that cried wolf on it would train you to ignore the alert, which is worse
than the gap it would close.

"Nothing to mine" has two causes and they need different fixes, so they are
reported separately. `watchlist_empty` means no game is on the watch list.
`benefits_disabled` means no benefit type is enabled under "Mining benefits" in
the Settings tab, which makes every drop unwanted and stops mining just as
completely, with every game still listed as if nothing were wrong.

Note the missing space. The sample above is pretty-printed for reading, but the
response goes out minified, so the bytes on the wire are
`{"ok":false,"watchlist_empty":true,...}`. A monitor configured to look for
`"ok": false` matches nothing and stays green straight through the outage it was
set up to catch.

`state` is a coarse lifecycle value, one of `starting`, `idle`, `watching` or
`paused`. It is derived from the miner's own objects, not from the status line
the dashboard displays. `watching` means the miner is watching a channel **for
drops**; `idle` means it is not mining, which covers both "nothing to mine" and
an idle watch, where a channel is held open for channel points or predictions
while no campaign is making progress. Having a channel open is therefore not
enough to report `watching`. `mining` is true only for `watching`. `login` is
the Twitch account name, or null before login. Since the endpoint is public it
returns nothing beyond those booleans, counts, the state and the login: no
tokens, no channel names, no campaign detail.

`ok` does not drop while the miner is idle, because an idle miner with a healthy
watch list is not a fault. If a miner that stays idle should page you, add a
second rule that matches `"mining":false` and give it a delay long enough not to
fire on the normal gaps between channel switches.

## Security notes

- The web server binds to loopback by default. For remote access, either bind
  `0.0.0.0` and use the bootstrap URL and `WEB_PASSWORD`, or put the miner
  behind an authenticating reverse proxy and set `TDM_AUTH_DISABLED=true`
  plus `TDM_TRUSTED_ORIGINS`.
- The API token lives in `data/api_token`; delete the file to rotate it.
- `GET /api/health/mining` is public by design. The auth gate matches it by
  path, so it answers without a session cookie, a bearer token or the web
  password, which is what lets an external uptime monitor reach it. That is also
  why its body is limited to booleans, counts, the coarse `state` and the Twitch
  login: anyone who can reach the port can read it, so anything richer added to
  that response is disclosed to them too.
- The Docker image runs as UID 1000. On the first start after upgrading from
  a root-based image, the entrypoint re-chowns `./data` and `./logs`;
  `data/cookies.jar` (your Twitch session) survives the upgrade.
- `data/cookies.jar` holds the Twitch auth token and is written user-only
  (`0600`). The mode is set on the temporary file before the token is written to
  it, so the token is never briefly readable by other users on the host, and a
  jar left loose by an older version is tightened the next time it is saved.
  The jar is also written atomically, so a container killed mid-write keeps the
  previous session instead of coming up logged out; a jar that cannot be read is
  reported and the miner asks you to log in again.
- Discord bot secrets belong in `discord_bot/.env` (gitignored), never in the
  repository.

## Notes

- Do not watch Twitch on the mining account in a regular browser at the same
  time; it desyncs progress.
- Requires Python 3.12 or newer when running from source. Persistent state is
  stored in `data/`; back it up before updating.
- `web/index.html` and `web/static/app.js` are the files the web server serves
  and are the authoritative copies. `src/web/index.html` and `src/web/app.js`
  are unserved mirrors: update a mirror from the served copy, never the other
  way round. The mirror `app.js` still assigns interpolated markup to
  `innerHTML` (`src/web/app.js:1780`, `:3648`) and puts raw markup on a drag
  transfer (`:2216`), all of which `tests/test_frontend_dom_safety.py` forbids
  in the served copy, so copying the mirror over it would reintroduce those
  holes. `tests/test_watchlist_guard.py` compares the two copies of the
  functions this watch-list fix touches (`autoCleanWantedQueue`,
  `saveSettings`) and fails when those drift; it does not compare the files as a
  whole, which do legitimately differ elsewhere.
- Screenshots of the dashboard are in `docs/screenshots/`.

## Acknowledgments

- [DevilXD](https://github.com/DevilXD), creator of the original TwitchDropsMiner, and all its translation contributors.
- [rangermix](https://github.com/rangermix) for the headless web rework.
- [SimpliAj](https://github.com/SimpliAj) for multi-account, predictions, Discord integration, and the web UI rework merged into this fork.

Use at your own risk; automated viewing may violate Twitch's terms of service.
