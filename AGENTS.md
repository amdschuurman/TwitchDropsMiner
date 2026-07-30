# AGENTS.md


## AGENTS.md Specific Instructions

This file provides guidance to AI Agents when working with code in this repository.

## Development Guidelines

1. **Testing**:
   - Always add unit tests for backend changes.
   - Frontend changes should have tests if possible.

2. **Code Style & Architecture**:
   - **DRY (Don't Repeat Yourself)**: Codebase must follow DRY principle.
   - **OOP (Object-Oriented Programming)**: Required for all backend code.

3. **Refactoring**:
   - You are authorized to refactor code to align with DRY/OOP principles.
   - **Permission Required**: You MUST ask for user permission before significant refactoring.

4. **Localization (i18n)**:
   - Update translation files if there are changes to UI text or console messages.
   - Frontend translation rendering must use safe DOM construction. Do not inject translated strings with non-clearing `innerHTML`; allowlist any intentional links and build them as DOM nodes.

5. **Documentation**:
   - Always update `README.md` and all agent instruction files when making changes.
   - The contents of all agent instruction files should be identical except for the `Specific Instructions` section. Any agent-specific instructions must be added to that section.

## Project Overview

Twitch Drops Miner is a Python application that automatically mines timed Twitch drops without downloading stream data. It uses Twitch's GraphQL API and websocket connections to simulate watching streams while tracking drop progress.

**Key Characteristics:**

- Python 3.12+ required
- Web-based GUI using FastAPI and Socket.IO
- Async/await architecture with asyncio
- Session persistence via cookies
- No stream video/audio download (bandwidth-efficient)
- Docker-ready for easy deployment

## Architecture


The application now uses a clean `src/` package structure with clear separation of concerns.

### Project Structure

```text
src/
├── models/          # Domain models (Game, Channel, Campaign, Drop, Benefit)
├── config/          # Configuration (constants, paths, operations, settings, client_info)
├── utils/           # Pure utilities (string, JSON, async helpers, rate_limiter, backoff)
├── i18n/            # Translation system (Translator class, TypedDict schemas)
├── auth/            # Authentication (auth_state for OAuth and token management)
├── api/             # External API (HTTP client, GraphQL client)
├── websocket/       # Real-time updates (websocket connection, pool)
├── web/             # Web GUI (app, gui_manager, api/)
│   └── managers/    # Individual UI managers (status, console, channels, campaigns, inventory, login, settings, cache, broadcaster)
├── services/        # Business logic services (channel, inventory, watch, maintenance, message_handlers)
├── core/            # Core client (Twitch client)
├── exceptions.py    # Custom exceptions
├── version.py       # Version string
└── __main__.py      # Entry point

lang/                # Translation JSON files (19 languages)
├── English.json     # Default/fallback translations
├── Español.json
├── Français.json
├── Deutsch.json
└── ...              # 15 more languages
```

### Core Components

**main.py** - Simple launcher:

- Runs the `src` package as a module using `runpy.run_module("src")`
- All application logic is now in `src/__main__.py`

**src/__main__.py** - Entry point:

- Parses command-line arguments
- Initializes Settings, Twitch client, and WebGUIManager
- Starts the FastAPI web server (uvicorn on port 8080)
- Runs the main asyncio event loop
- Handles signals (SIGINT, SIGTERM on Linux) and exit codes

**src/core/client.py** - Central client (`Twitch` class):

- State machine: IDLE, INVENTORY_FETCH, GAMES_UPDATE, CHANNELS_CLEANUP, CHANNELS_FETCH, CHANNEL_SWITCH, EXIT
- Composes `_AuthState`, `HTTPClient`, and `GQLClient`
- Delegates to service layer for business logic
- Drop progress monitoring via periodic "watch" payloads
- Manages WebsocketPool and maintenance tasks

**src/services/** - Business logic layer (fully implemented):

- `ChannelService`: Channel management and selection logic
- `InventoryService`: Campaign and drop inventory operations
- `WatchService`: Drop mining watch payload logic
- `MaintenanceService`: Periodic maintenance tasks
- `MessageHandlerService`: Websocket message routing and handling


**src/models/channel.py** - Channel and Stream:

- `Channel` class: Twitch channel with online/offline status
- `Stream` class: Active stream with game, viewers, drop status
- Stream URL fetching and validation
- ACL-based vs directory channels

**src/models/campaign.py** - Drop campaigns:

- `DropsCampaign`: Campaign with game, timeframe, allowed channels
- Time-based eligibility and progress tracking

**src/models/drop.py** - Drop types:

- `TimedDrop`: Drops with minute requirements and progress
- `BaseDrop`: Base class with claim logic
- Precondition chains for sequential drops

**src/web/gui_manager.py** - Web GUI:

- `WebGUIManager`: Main GUI coordinator
- Composes individual managers for different UI concerns (status, console, channels, campaigns, inventory, login, settings, cache)
- Uses `WebSocketBroadcaster` for real-time Socket.IO updates
- Pure asyncio, no tkinter dependency

**src/web/app.py** - FastAPI application:

- REST API endpoints: `/api/status`, `/api/channels`, `/api/campaigns`, `/api/settings`, `/api/login`, `/api/oauth/confirm`, `/api/reload`, `/api/close`, `/api/version`, `/api/health/mining` (readiness probe for an external uptime monitor: always HTTP 200, `ok` false while the watch list is empty, the miner is still starting, or a state read failed. Public by EXACT path in the auth gate allowlist, so `/api/health/mining/` with a trailing slash is gated and answers 401, which a monitor inverting a match on `"ok":false` reads as healthy forever. The body is limited to booleans, counts, a coarse `state` of `starting` | `idle` | `watching` | `paused`, and the Twitch login. `watching` means watching a channel *for drops*, so an idle watch (a channel held open for channel points or predictions while nothing is minable) reports `idle`, and `mining` follows `state`; a held `watching_channel` is not on its own proof of mining. `ok` does not drop on `idle`. No channel or campaign names: `state` is derived from the miner objects, never from the status line, which embeds both)
- Socket.IO server for real-time bi-directional communication
- Serves static web frontend from `web/` directory
- Integrates with WebGUIManager via `set_managers()`

**src/websocket/pool.py** - WebSocket management:

- Sharded connections (up to 50 topics per socket, max 199 channels)
- Topics: User.Drops, User.Notifications, Channel.StreamState, Channel.StreamUpdate
- Automatic reconnection with exponential backoff
- Message routing to registered callbacks

**src/config/settings.py** - Application settings:

- Games to watch list (auto-populated from available campaigns if empty)
- Games can also be added manually from the web settings search box
- `auto_clean_watchlist` (default `False`): opt-in removal of fully claimed games from the watch list, see Settings Write Invariants below
- Connection quality multiplier
- Language selection
- Proxy support (including verification)
- Logging and dump flags from command-line arguments
- Persistence to JSON file (`settings.json`) in DATA_DIR
- Inventory filters (Status, Benefit Type, Game Search)

### Settings Write Invariants

These rules exist because breaking them caused a silent five-day mining outage on 2026-07-24. Do not reintroduce the patterns they forbid.

- **No code path may write settings as a side effect of a page load or socket connect.** Persisting settings requires a user gesture behind it. The outage came from `autoCleanWantedQueue()` being called from the `initial_state` Socket.IO handler: every dashboard open pruned fully claimed games from `games_to_watch` and saved the result, so an account watching one fully claimed game was left watching nothing.
- **`games_to_watch` may only be emptied on explicit user intent.** The client sends `allow_empty_games_to_watch: true` on the single request that empties the list (unchecking the last game, "Deselect All", removing the last wanted-queue entry), and omits `games_to_watch` from the payload entirely when it resolves to `[]` without that intent. `SettingsManager.update_settings` enforces the same rule for any client: an empty `games_to_watch` without the flag is dropped from the payload and logged as `Refused to clear the games-to-watch list without explicit intent`, while every other key in the request still applies. The key is dropped unconditionally, but the line is only logged when the stored list actually had games in it: on a fresh install the list is already empty, so the same payload refuses nothing and a warning there would only teach the operator to scroll past the line that matters. `allow_empty_games_to_watch` is request-only. It is not a `Settings` field and must never reach `settings.json`.
- **Validate before mutating.** `check_and_update_setting` runs its validator before the `setattr`, and rolls the value back if the side-effect action raises, so a value the application rejects never reaches the in-memory `Settings`. It used to mutate first: `language: ""` from a `<select>` whose options had not loaded yet was written to `Settings`, `_set_language("")` then raised, the request aborted before `save()`, and the graceful shutdown wrote the blank language to disk later. A rejected key is logged as `Setting rejected: <key> = <value> (<reason>)` and skipped, and the remaining keys of the same request still apply.
- **A setting change is announced only after it held.** `check_and_update_setting` logs `Setting changed: <key> = <value>` after `action` has returned, never before. Logging first meant a write the action then rejected printed `Setting changed: language = ...` immediately followed by `Setting rejected: language = ...`, two contradictory lines about one key, which from outside is indistinguishable from the settings bug this whole change exists to remove. An `action` may return a console line to log INSTEAD of the generic one, which is how `Proxy cleared` replaces `Setting changed: proxy = ` rather than adding a second line to it.
- **The destructive cleanup stays opt-in.** `auto_clean_watchlist` defaults to `False`. Even when it is on, the frontend cleanup refuses to remove the last remaining game, and `GET /api/health/mining` reports `ok` false while the watch list is empty so an external monitor can catch the state.
- **Every settings and cache write is atomic. The cookie jar is not.** `json_save` (`src/utils/json_utils.py`) serializes into a temporary file in the target's own directory, `fsync`s it, then `os.replace`s it onto the target. Its callers are `settings.json` (`src/config/settings.py`) and the channel-point caches (`src/services/message_handlers.py`, `src/web/app.py`). It must never truncate in place: `open(path, "w")` empties the file before the new bytes land, so an interrupted save left a truncated `settings.json` behind, and `json_load(..., default_settings, merge=True)` then falls back to the defaults, whose `games_to_watch` is `[]`. That is the same silent outage reached without any client, endpoint or cleaner being involved. The temporary file also carries the 0o600 mode onto the target, which is what keeps credential-bearing proxy URLs unreadable to other users on the host, and a successful save reaps this target's own abandoned temp files once they are older than an hour. `cookies.jar` does NOT go through `json_save`: `aiohttp.CookieJar.save()` owns that file (called from `src/api/http_client.py:233` and `src/auth/auth_state.py:289`) and does `open(path, "wb")` plus `pickle.dump`, so it truncates in place and an interrupted cookie write can still leave a truncated jar and cost the Twitch session. Do not restate this invariant as covering cookies, and do not route the jar through `json_save` to make it true: the file is aiohttp's pickle format, not JSON.
- **An unreadable `settings.json` is preserved, not replaced.** `Settings.load` passes `quarantine=True`, so `json_load` moves a file it cannot parse aside to `settings.json.corrupt` (then `.corrupt.1`, `.2`, ... and finally a timestamped name) and reports it at ERROR, naming the new path and spelling out that nothing will be mined until the watch list is set again. The handler catches `ValueError`, not `json.JSONDecodeError`, because byte-level corruption raises `UnicodeDecodeError`, which used to escape it, make `Settings()` raise, hit `sys.exit(4)` in `src/__main__.py` and turn into a restart loop under `restart: unless-stopped`. Generic callers (the derived caches) keep the quiet WARNING-and-defaults path and never quarantine. Accepted residual, do not "fix" it silently: the miner still starts from defaults, so `games_to_watch` is `[]` and the save-side floor below has no stored list left to compare against; the operator's list survives only in the `.corrupt` file, and `GET /api/health/mining` answering `ok` false is what makes that state visible from outside the process. Salvaging a list out of bytes that by definition do not parse would mean guessing, so it is deliberately not attempted.
- **An empty `games_to_watch` reaches disk only when someone declared it.** `Settings.save` reads the list currently ON DISK (`_stored_games_to_watch`, read at save time rather than remembered from load, because the question is what this save would overwrite) and, when the in-memory list is empty, the stored one is not, and no intent was declared, restores the stored list into both the payload and memory and logs `Refused to save an empty games-to-watch list over the stored ...` at ERROR. Every other key of the same save still lands. This is the case the payload guard above cannot see: the list becoming `[]` in memory with no request behind it, then being cemented by an unrelated save such as a dark-mode toggle. The only way past the floor is `Settings.declare_empty_watchlist_intent()`, a one-shot consumed by the next save whatever that save writes; `SettingsManager.update_settings` is its only caller and calls it only for a payload that both carries `allow_empty_games_to_watch` and actually empties the list. It is deliberately not named after that payload key, and its flag is a class-level default that only the declaring method materialises and `save()` pops again, so neither name ever reaches `vars(self)`, which is both what `json_save` writes and what `GET /api/settings` returns.
- **An unusable stored `language` is repaired at load; a merely unloadable one is kept.** `Settings.load` runs the stored value through `LanguageNormalizer.repair` (`src/config/settings.py`). Without it a poisoned value never heals: `check_and_update_setting` short-circuits when the incoming value equals the stored one, so a stored `""` is never revalidated and every start loads it again, and `merge_json` does not catch it either since it only enforces the type of a key and `""` is a valid `str`. `LanguageNormalizer` answers two different questions on purpose. `accepts`/`accepted` is "can the translator load this right now", guards the write path, and must be exactly as wide as `Translator.set_language`: it keeps accepting locale codes like `en`, but only codes whose target language actually loaded, since `accepts("ar")` returning true while `set_language("ar")` raised `Unrecognized language العربية` is a validator lying about the setter it guards. `knows`/`known` is "does this name a language this install ships", built from the stems of `lang/*.json` so it does not depend on which files parsed this run, and that is what `repair` judges by: `repair` is destructive because the next save persists its answer, and `Translator.__init__` skips a file it cannot read, so judging a repair by loadability let one transient bad read overwrite a legitimate stored `Nederlandse` with the default and then write that loss to disk for good.
- **A language is never worth a failed boot.** `src/__main__.py` sets the startup language through `LanguageNormalizer.apply`, never `Translator.set_language` directly. That call sits outside the try/except further down, so a `ValueError` from an unloadable language escaped `asyncio.run()` and killed the process, which under `restart: unless-stopped` is a container restart loop caused by one bad file in `lang/`, taking the web UI needed to fix it down with it. `apply` logs a warning, leaves the translator on the language it already has, returns the one now in effect, and deliberately does not correct `settings.language` so that a stored choice whose file merely failed to parse survives the shutdown save.

### State Machine Flow

1. **IDLE** - Waiting for campaigns or user action
2. **INVENTORY_FETCH** - Fetch campaigns from GraphQL, claim completed drops
3. **GAMES_UPDATE** - Determine wanted games based on priority/exclude lists
4. **CHANNELS_CLEANUP** - Remove channels not streaming wanted games
5. **CHANNELS_FETCH** - Discover channels via ACL lists or game directories
6. **CHANNEL_SWITCH** - Select best channel to watch based on priority/ACL
7. Loop between CHANNEL_SWITCH and periodic INVENTORY_FETCH (hourly)

### Authentication

- Uses OAuth device code flow (user enters code at twitch.tv/activate)
- Managed by `src/auth/auth_state.py` (`_AuthState` class)
- Access tokens stored in `cookies.jar` in DATA_DIR
- Device ID from Twitch's `unique_id` cookie
- Session ID generated per run
- Client info defined in `src/config/client_info.py` (presents as Android app with Client-Id and User-Agent spoofing)

### Drop Mining Mechanism

The application sends periodic "watch" payloads through Twitch GraphQL `sendSpadeEvents`:

- Payload contains gzip/base64-encoded minute-watched events with channel/broadcast IDs
- Twitch reports progress via websocket (User.Drops topic)
- If websocket updates stop, fallback to GQL CurrentDrop query
- Extrapolation via "bump minutes" when no updates received

### GraphQL Operations

Persisted operations are defined in `src/config/operations.py` as `GQL_OPERATIONS`; raw GraphQL payloads such as `sendSpadeEvents` use `GQLQuery`:

- **Inventory** - Fetch in-progress campaigns and claimed benefits
- **Campaigns** - List available active/upcoming campaigns
- **CampaignDetails** - Detailed drop info for a campaign
- **GameDirectory** - Find live streams for a game with drops enabled
- **GetStreamInfo** - Check if channel is online and get stream details
- **CurrentDrop** - Query currently mined drop progress
- **ClaimDrop** - Claim a completed drop
- **AvailableDrops** - Check which campaigns a channel qualifies for (badge validation)
- **NotificationsDelete** - Delete Twitch notifications

### Channel Selection Priority

1. Selected channel (if user clicked one)
2. ACL-based channels over directory channels
3. Game priority order (from settings)
4. Viewer count (descending)
5. Maximum 199 channels tracked simultaneously

### Maintenance Task

Runs in background to trigger:

- Channel cleanup when drops start/end (based on time_triggers)
- Inventory reload every ~60 minutes

### Translation System

**Architecture:**

- All translations stored as JSON files in `lang/` directory (19 languages supported)
- English (`lang/English.json`) is the single source of truth and fallback language
- Strongly typed with TypedDict schema defined in `src/i18n/translator.py`
- Translator class (`src/i18n/translator.py`) handles language loading and fallback
- Singleton instance `_` available via `from src.i18n import _`

**Supported Languages:**

- English, Dansk (Danish), Deutsch (German), Español (Spanish), Français (French)
- Indonesian, Italiano (Italian), Nederlandse (Dutch), Polski (Polish), Português (Portuguese)
- Română (Romanian), Türkçe (Turkish), Čeština (Czech)
- Русский (Russian), Українська (Ukrainian), العربية (Arabic)
- 日本語 (Japanese), 简体中文 (Simplified Chinese), 繁體中文 (Traditional Chinese)

**Translation Structure:**

```python
Translation = {
    "language_name": str,      # Display name of language
    "english_name": str,       # English name of language
    "status": StatusMessages,  # Console status messages
    "login": LoginMessages,    # Login-related messages
    "error": ErrorMessages,    # Error messages
    "gui": GUIMessages        # All web GUI text (tabs, settings, help, etc.)
}
```

**Usage:**

```python
from src.i18n import _

# Access translations
status_text = _.t["gui"]["status"]["idle"]  # Returns "Idle"
login_text = _.t["login"]["status"]["logged_in"]  # Returns "Logged in"
```

**Language Persistence:**

- Language selection persisted in `settings.json` (DATA_DIR)
- Dynamic language switching supported in web GUI
- Changes take effect immediately without restart

## Key Files

- **src/config/constants.py** - Core enums (State, WebsocketTopic), logging config, type aliases
- **src/config/operations.py** - GraphQL operation definitions (GQL_OPERATIONS)
- **src/config/paths.py** - Path management and Docker environment detection
- **src/config/client_info.py** - Twitch client info (Client-Id, User-Agent)
- **src/config/settings.py** - Application settings with JSON persistence
- **src/exceptions.py** - Custom exceptions (MinerException, ExitRequest, RequestException, RequestInvalid, WebsocketClosed, LoginException, CaptchaRequired, GQLException)
- **src/utils/** - Helper utilities (string_utils, json_utils, async_helpers, rate_limiter, backoff)
- **src/i18n/** - Internationalization package with TypedDict schema and Translator class
  - **translator.py** - Translator class with typed translation schema (Translation TypedDict)
  - **__init__.py** - Exports translation types and `_` (Translator instance)
- **lang/** - Translation JSON files for 19 languages (English.json is the single source of truth)
- **src/version.py** - Version string
- **src/web/app.py** - FastAPI application with REST API and Socket.IO
- **src/web/managers/cache.py** - ImageCache for campaign artwork caching
- **web/** - Frontend assets (index.html, static/app.js, static/styles.css); this is the directory the server actually serves, and the authoritative copy of every frontend file. `src/web/app.js` and `src/web/index.html` are unserved mirrors of it, updated FROM the served copies only, never copied back over them (see Frontend Copies below)

## Development Commands

**IMPORTANT: Always activate the virtual environment first!**

The project uses a virtual environment located at `.venv/`. All Python commands must be run within it, either by activating it or by calling `.venv/bin/python` directly:

```bash
# Activate the virtual environment (required before any Python commands)
source .venv/bin/activate
```

### Running the Application

```bash
# Run from source (remember to activate venv first!)
source .venv/bin/activate && python main.py

# With verbose logging (stackable: -vv, -vvv)
source .venv/bin/activate && python main.py -v

# Create data dump for debugging
source .venv/bin/activate && python main.py --dump

# Access the web interface at http://localhost:8080
```

### Development Setup

The application requires:

- Python 3.12+
- Virtual environment at `.venv/` (activate it, or call `.venv/bin/python` directly); there is no `env/`
- Dependencies from `pyproject.toml` (includes FastAPI, uvicorn, Socket.IO)

Docker deployment:

```bash
# Build and run with docker-compose
docker-compose up -d

# Access at http://localhost:8080
```

## Testing

### Automated Tests

The project includes a test suite in the `tests/` directory:

```bash
# Activate virtual environment and run tests
source .venv/bin/activate && python -m pytest tests/
```

**Test Files:**

- `tests/test_proxy_settings.py` - Tests for proxy settings configuration
- `tests/test_verify_proxy.py` - Tests for proxy verification functionality
- `tests/test_settings_api.py` - Tests for the settings write path and the stored-state invariants above: the `games_to_watch` clear guard on the payload and the save-side floor under it, booting from a quarantined `settings.json`, language validation, the load-time language repair, the accepts/knows split, and `LanguageNormalizer.apply` at startup
- `tests/test_json_atomic_save.py` - Tests that `json_save` is crash-atomic, keeps the file 0o600 and reaps its own stale temp files, and that `json_load(..., quarantine=True)` preserves an unparseable file instead of letting the next save overwrite it
- `tests/test_health_mining_api.py` - Tests for `GET /api/health/mining`: its public key set, its coarse `state` (including that an idle watch is not mining), that it is public at that exact path only, what it must not disclose, and that the alert keyword README documents matches the bytes on the wire
- `tests/test_watchlist_guard.py` - Static-source guards for the client half of the Settings Write Invariants, in both `app.js` copies and both `index.html` copies. Per-function and per-control, not whole-file (see Frontend Copies)


### Manual Testing

1. Run with `-vvv` for maximum verbosity (levels: -v, -vv, -vvv, -vvvv)
2. Check log files in `./logs/` directory
3. Monitor web GUI console output and browser developer tools

## Web GUI Architecture

The application uses a web-based interface accessible via browser:

### Web GUI Components

**src/web/gui_manager.py** - WebGUIManager class:

- Managers: StatusManager, ConsoleOutputManager, ChannelListManager, CampaignProgressManager, InventoryManager, LoginFormManager, SettingsManager, CacheManager
- Uses WebSocketBroadcaster to push real-time updates to connected clients via Socket.IO
- Pure async/await implementation

**src/web/app.py** - FastAPI application:

- REST API endpoints: `/api/status`, `/api/channels`, `/api/campaigns`, `/api/settings`, `/api/login`, `/api/oauth/confirm`, `/api/reload`, `/api/close`, `/api/version`, `/api/health/mining` (readiness probe for an external uptime monitor: always HTTP 200, `ok` false while the watch list is empty, the miner is still starting, or a state read failed. Public by EXACT path in the auth gate allowlist, so `/api/health/mining/` with a trailing slash is gated and answers 401, which a monitor inverting a match on `"ok":false` reads as healthy forever. The body is limited to booleans, counts, a coarse `state` of `starting` | `idle` | `watching` | `paused`, and the Twitch login. `watching` means watching a channel *for drops*, so an idle watch (a channel held open for channel points or predictions while nothing is minable) reports `idle`, and `mining` follows `state`; a held `watching_channel` is not on its own proof of mining. `ok` does not drop on `idle`. No channel or campaign names: `state` is derived from the miner objects, never from the status line, which embeds both)
- Socket.IO server for real-time bi-directional communication
- Serves static web frontend from `web/` directory
- Integrates with WebGUIManager via `set_managers()`

**web/** - Frontend assets:

- `index.html` - Single-page application layout with tabs
- `static/app.js` - Socket.IO client, real-time UI updates, API calls, Inventory Filtering logic
- `static/styles.css` - Responsive design with dark mode support

#### Frontend Copies

`web/index.html` and `web/static/app.js` are the copies the server serves (`src/web/app.py` resolves the web dir to `<repo>/web`) and are authoritative. `src/web/index.html` and `src/web/app.js` are unserved mirrors.

**The rule has a direction: patch the served copy, then bring the mirror up to it. Never copy a mirror over a served file.** It is not a symmetric lockstep rule, because the mirror is security-regressed relative to the served copy: `src/web/app.js:1780` and `:3648` assign interpolated markup to `innerHTML` and `:2216` puts raw markup on a drag transfer, all three of which `tests/test_frontend_dom_safety.py` forbids in the served copy. Copying the mirror over `web/static/app.js` to "sync" them would reintroduce those holes.

Landing a frontend change in both files in the same commit is still the goal, and `tests/test_watchlist_guard.py` compares the two copies of the functions the watch-list fix touches (`autoCleanWantedQueue`, `saveSettings`) plus the controls it depends on in both `index.html` copies. It does not compare either pair as a whole file, and it cannot: the copies legitimately differ in fork branding and cache-bust query strings.

### Communication Protocol

**Server → Client (Socket.IO events):**

- `initial_state` - Full state on connect
- `status_update` - Status bar changes
- `console_output` - New log lines
- `channel_add/update/remove` - Channel list changes
- `drop_progress` - Drop mining progress
- `campaign_add` - New campaign added
- `login_required` - Prompt for credentials
- `settings_updated` - Settings changed

**Client → Server:**

- REST API for actions (login, settings, channel selection)
- Socket.IO for connection management

### Docker Integration

**src/config/paths.py:**

- Detects Docker environment via `DOCKER_ENV` env var or `/.dockerenv` file
- Docker: Uses `/app` for code, `/app/data` for persistent storage
- Development: Uses `<project_root>/data` for persistent storage
- All user data (cookies, settings, cache, logs) stored in DATA_DIR
- Provides `_resource_path()` helper for locating bundled resources

**Dockerfile:**

- Based on `python:3`
- Installs dependencies from `pyproject.toml`
- Exposes port 8080
- Health check on `/api/status`

**docker-compose.yml:**

- Volume mounts `./data:/app/data` for persistence
- Port mapping `8080:8080`
- Auto-restart policy
- Timezone configuration

### Key Design Decisions

- **WebSocket for real-time** - Socket.IO chosen for reliability (fallback to polling)
- **Single-page app** - Simpler than full framework (React/Vue), fast load times
- **Direct Docker support** - Environment detection, proper path handling
- **OAuth device code flow** - Works great for web-based deployment

## Project Scope

**Supported:**

- ✅ Web GUI - browser-based interface with advanced filtering
- ✅ Docker deployment - containerized for any platform
- ✅ Remote access - access from any device on network
- ✅ Headless operation - no display server required

**NOT supported:**

- Multi-account support
- Channel points mining
- Mining for unlinked campaigns
- Desktop GUI
