from __future__ import annotations

import asyncio
import ipaddress
import logging
import secrets
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

import socketio
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.auth.api_token import COOKIE_MAX_AGE, COOKIE_NAME, load_or_create_token


if TYPE_CHECKING:
    import uvicorn

    from src.core.client import Twitch
    from src.web.gui_manager import WebGUIManager


logger = logging.getLogger("TwitchDrops")

# Create FastAPI app
app = FastAPI(title="Twitch Drops Miner Web", version="1.0.0")


# CORS is restrictive by default: same-origin only. Browsers loading the page
# from http://localhost:8080 send same-origin requests, which never trigger a
# CORS preflight — so the wildcard removal does not break the local UI. LAN
# users who reach the server by IP will be served from the matching Origin
# (which we accept reflectively below).
def _allow_origin(origin: str | None) -> bool:
    if origin is None:
        return False
    parsed = urllib.parse.urlparse(origin)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?",
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# Create Socket.IO server. CORS is restricted to localhost — Socket.IO clients
# from other origins must explicitly be allowed by an operator-controlled env
# var (kept off by default to neutralize CSRF via cross-origin connections).
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=[
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://[::1]:8080",
    ],
    logger=False,
    engineio_logger=False,
)

# Wrap with ASGI app
socket_app = socketio.ASGIApp(sio, app)

# Global references (set by main.py)
gui_manager: WebGUIManager | None = None
twitch_client: Twitch | None = None
_server_instance: uvicorn.Server | None = None


def set_managers(gui: WebGUIManager, twitch: Twitch):
    """Called by main.py to set up references"""
    global gui_manager, twitch_client
    gui_manager = gui
    twitch_client = twitch
    gui.set_socketio(sio)


# Pydantic models for API
class LoginRequest(BaseModel):
    username: str
    password: str
    token: str = ""


class ChannelSelectRequest(BaseModel):
    channel_id: int


class SettingsUpdate(BaseModel):
    games_to_watch: list[str] | None = None
    dark_mode: bool | None = None
    language: str | None = None
    proxy: str | None = None
    connection_quality: int | None = None
    minimum_refresh_interval_minutes: int | None = None
    inventory_filters: dict | None = None
    mining_benefits: dict[str, bool] | None = None


class ProxyVerifyRequest(BaseModel):
    proxy: str


# ==================== Auth ====================


def _client_is_loopback(request: Request) -> bool:
    if request.client is None:
        return False
    try:
        addr = ipaddress.ip_address(request.client.host)
    except ValueError:
        return False
    return addr.is_loopback


def _expected_token() -> str:
    # Cached after the first call; load_or_create_token() also caches behaviorally
    # since it reads from the same file each time.
    return load_or_create_token()


def require_auth(request: Request) -> str:
    """FastAPI dependency: validate the session cookie or Authorization bearer.

    Loopback clients without a session cookie or Authorization header are
    auto-authenticated (single-user, on-host deployment is the default UX).
    Non-loopback clients must present the cookie that was installed via the
    /api/session/bootstrap flow, or an explicit ``Authorization: Bearer``
    header.
    """
    expected = _expected_token()
    presented = request.cookies.get(COOKIE_NAME)
    if presented is None:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            presented = auth.split(None, 1)[1].strip()
    if presented and secrets.compare_digest(presented, expected):
        return expected
    if _client_is_loopback(request):
        # Loopback request without a credential — let it through so the
        # bootstrap-on-first-visit flow can install a cookie.
        return expected
    raise HTTPException(status_code=401, detail="Authentication required")


def _validate_proxy_url(url: str) -> None:
    """Reject obviously-dangerous proxy URLs.

    Allows ``http``, ``https``, ``socks4``, ``socks5`` schemes. Rejects bare
    URLs, javascript: / file: / data:, and proxies whose host resolves to a
    link-local or unspecified address that could be abused for SSRF probes.
    """
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme.lower() not in {"http", "https", "socks4", "socks5"}:
        raise HTTPException(
            status_code=400,
            detail=f"Proxy scheme '{parsed.scheme}' not allowed (use http/https/socks4/socks5)",
        )
    if not parsed.hostname:
        raise HTTPException(status_code=400, detail="Proxy URL is missing a host")
    # Block obviously-unsafe destinations. We *allow* loopback and RFC1918
    # because users do legitimately point at an in-network proxy, but reject
    # 0.0.0.0/::/link-local which has no legitimate proxy use.
    try:
        addr = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return  # hostname is a DNS name, leave it alone
    if addr.is_unspecified or addr.is_link_local or addr.is_multicast:
        raise HTTPException(status_code=400, detail="Proxy host is not a valid target")


# ==================== REST API Endpoints ====================


@app.get("/", response_class=HTMLResponse)
async def serve_index(request: Request):
    """Serve the main web interface, auto-installing the session cookie for loopback."""
    web_dir = Path(__file__).parent.parent.parent / "web"
    index_file = web_dir / "index.html"
    if not index_file.exists():
        # Intentionally vague — leaking the resolved filesystem path here was
        # an information disclosure issue flagged by the audit (SEC-014).
        return HTMLResponse(
            content="<h1>Twitch Drops Miner</h1><p>Web interface files not found. Please check installation.</p>",
            status_code=500,
        )

    response = FileResponse(index_file)
    # Auto-bootstrap cookie for loopback visits so the local UX is unchanged.
    if _client_is_loopback(request) and request.cookies.get(COOKIE_NAME) is None:
        response.set_cookie(
            COOKIE_NAME,
            _expected_token(),
            max_age=COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
    # Never cache the HTML shell — asset references inside use cache-busted
    # query strings, so the index itself must always be fresh.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


@app.get("/api/session/bootstrap")
async def session_bootstrap(request: Request, token: str | None = None):
    """Install the session cookie from a one-shot URL containing ?token=.

    Used to onboard non-loopback (LAN, Docker) browsers: the operator runs
    ``main.py``, copies the printed bootstrap URL, and opens it once on the
    browser machine.
    """
    expected = _expected_token()
    if token is None and _client_is_loopback(request):
        # Loopback gets the cookie for free, no token required.
        token = expected
    if token is None or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Invalid or missing bootstrap token")
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        COOKIE_NAME, expected, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax"
    )
    return response


@app.get("/api/status")
async def get_status(_token: str = Depends(require_auth)):
    """Get current application status"""
    if not gui_manager or not twitch_client:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {
        "status": gui_manager.status.get(),
        "login": gui_manager.login.get_status(),
        "manual_mode": twitch_client.get_manual_mode_info(),
    }


@app.get("/api/channels")
async def get_channels(_token: str = Depends(require_auth)):
    """Get list of tracked channels"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {"channels": gui_manager.channels.get_channels()}


@app.post("/api/channels/select")
async def select_channel(request: ChannelSelectRequest, _token: str = Depends(require_auth)):
    """Select a channel to watch"""
    if not gui_manager or not twitch_client:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    # Validate channel exists
    channel = twitch_client.channels.get(request.channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Validate channel has a game
    if not channel.game:
        raise HTTPException(status_code=400, detail="Channel is not playing any game")

    # Warn if channel has no drops (shouldn't happen if GUI is filtering correctly)
    if not any(campaign.can_earn(channel) for campaign in twitch_client.inventory):
        logger.warning(f"User selected channel {channel.name} but it has no available drops")

    gui_manager.select_channel(request.channel_id)

    # Trigger channel switch to apply the selection
    from src.config import State

    twitch_client.change_state(State.CHANNEL_SWITCH)

    return {"success": True}


@app.get("/api/campaigns")
async def get_campaigns(_token: str = Depends(require_auth)):
    """Get campaign inventory"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {"campaigns": gui_manager.inv.get_campaigns()}


@app.get("/api/console")
async def get_console_history(_token: str = Depends(require_auth)):
    """Get console output history"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {"lines": gui_manager.output.get_history()}


@app.get("/api/settings")
async def get_settings(_token: str = Depends(require_auth)):
    """Get current settings"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    settings = gui_manager.settings.get_settings()
    # Mask credentials embedded in the proxy URL before returning. Even with
    # auth required, no benefit to handing the raw user:pass back to the UI.
    proxy = settings.get("proxy")
    if isinstance(proxy, str) and proxy:
        try:
            parsed = urllib.parse.urlparse(proxy)
            if parsed.username or parsed.password:
                netloc = parsed.hostname or ""
                if parsed.port:
                    netloc = f"{netloc}:{parsed.port}"
                settings = dict(settings)
                settings["proxy"] = urllib.parse.urlunparse(
                    (
                        parsed.scheme,
                        f"***:***@{netloc}",
                        parsed.path,
                        parsed.params,
                        parsed.query,
                        parsed.fragment,
                    )
                )
        except (ValueError, AttributeError):
            pass
    return settings


@app.get("/api/languages")
async def get_languages():
    # Public: language list is static metadata, no authentication required.
    # Frontend fetches this before the bootstrap cookie is installed so the
    # language picker can render on the very first page load.
    """Get available languages"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return gui_manager.settings.get_languages()


@app.get("/api/translations")
async def get_translations():
    # Public: translation bundles are static UI strings, identical to what ships
    # in the repo. Keeping auth on them would block the very first render before
    # the bootstrap cookie is installed and cause the UI to look broken.
    """Get translations for current language"""
    from src.i18n.translator import _

    # Return the full Translation object
    return _.t


@app.post("/api/settings")
async def update_settings(settings: SettingsUpdate, _token: str = Depends(require_auth)):
    """Update application settings"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    settings_dict = settings.model_dump(exclude_unset=True)
    # If the operator is updating the proxy, enforce the same scheme allowlist
    # used by verify-proxy. Stops a writer from installing javascript:/file:
    # proxies that aiohttp would happily attempt.
    new_proxy = settings_dict.get("proxy")
    if isinstance(new_proxy, str) and new_proxy.strip():
        _validate_proxy_url(new_proxy)
    gui_manager.settings.update_settings(settings_dict)
    return {"success": True, "settings": gui_manager.settings.get_settings()}


@app.post("/api/settings/verify-proxy")
async def verify_proxy(request: ProxyVerifyRequest, _token: str = Depends(require_auth)):
    """Verify proxy connectivity"""
    import time

    import aiohttp

    proxy_url = request.proxy.strip()
    if not proxy_url:
        return {"success": False, "message": "Proxy URL is empty"}
    _validate_proxy_url(proxy_url)

    try:
        start_time = time.time()
        # Test connection to Twitch
        async with (
            aiohttp.ClientSession() as session,
            session.get("https://www.twitch.tv", proxy=proxy_url, timeout=10) as response,
        ):
            # Just checking if we can connect and get a response
            if response.status < 500:
                latency = round((time.time() - start_time) * 1000)
                return {
                    "success": True,
                    "message": f"Connected! ({latency}ms)",
                    "latency": latency,
                }
            else:
                return {
                    "success": False,
                    "message": f"Proxy reachable but returned {response.status}",
                }
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {str(e)}"}


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a dotted version string like '1.2.4' into (1, 2, 4). Non-numeric parts become 0."""
    parts: list[int] = []
    for part in v.split("."):
        # strip prerelease/build suffixes ('1.2.4-rc1' -> 1, 2, 4)
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _is_newer_version(candidate: str, current: str) -> bool:
    """Return True if `candidate` is a strictly newer version than `current`."""
    try:
        return _parse_version(candidate) > _parse_version(current)
    except (ValueError, AttributeError):
        return False


@app.get("/api/version")
async def get_version():
    # Public: just the installed version + GitHub release lookup. Useful to
    # show in the footer before the user authenticates.
    """Get current application version and check for updates"""
    import aiohttp

    from src.version import __version__

    current_version = __version__
    latest_version = None
    update_available = False
    download_url = None

    try:
        # Check GitHub API for latest release
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                "https://api.github.com/repos/amdschuurman/TwitchDropsMiner/releases/latest",
                timeout=5,
            ) as response,
        ):
            if response.status == 200:
                data = await response.json()
                latest_version = data.get("tag_name", "").lstrip("v")
                download_url = data.get("html_url")

                # Compare versions by parsed numeric tuple so e.g. "1.10" > "1.9".
                # Plain string comparison is wrong for semver - lexicographic sort
                # would mark 1.10 as older than 1.9.
                if latest_version and _is_newer_version(latest_version, current_version):
                    update_available = True
    except Exception as e:
        logger.warning(f"Failed to check for updates: {str(e)}")

    return {
        "current_version": current_version,
        "latest_version": latest_version,
        "update_available": update_available,
        "download_url": download_url or "https://github.com/amdschuurman/TwitchDropsMiner/releases",
    }


@app.post("/api/login")
async def submit_login(login_data: LoginRequest, _token: str = Depends(require_auth)):
    """Submit login credentials"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    gui_manager.login.submit_login(login_data.username, login_data.password, login_data.token)
    return {"success": True}


@app.post("/api/oauth/confirm")
async def confirm_oauth(_token: str = Depends(require_auth)):
    """Confirm OAuth code has been entered by user"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    # Just set the event to signal the user has acknowledged the code
    gui_manager.login._login_event.set()
    return {"success": True}


@app.post("/api/reload")
async def trigger_reload(_token: str = Depends(require_auth)):
    """Trigger application reload"""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")

    from src.config import State

    twitch_client.change_state(State.INVENTORY_FETCH)
    return {"success": True}


@app.post("/api/close")
async def trigger_close(_token: str = Depends(require_auth)):
    """Trigger application shutdown"""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")

    twitch_client.close()
    return {"success": True}


@app.post("/api/mode/exit-manual")
async def exit_manual_mode(_token: str = Depends(require_auth)):
    """Exit manual mode and return to automatic channel selection"""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")

    if not twitch_client.is_manual_mode():
        return {"success": False, "message": "Not in manual mode"}

    twitch_client.exit_manual_mode("User requested")
    return {"success": True}


# ==================== Socket.IO Events ====================


def _sio_request_authenticated(environ: dict, auth: dict | None) -> bool:
    """Validate cookie or token presented by a Socket.IO client.

    Mirrors :func:`require_auth` for HTTP, but reads the cookie from the WSGI
    environ and accepts an optional ``auth={"token": ...}`` handshake payload.
    Loopback clients are auto-authenticated.
    """
    expected = _expected_token()
    presented: str | None = None
    if isinstance(auth, dict):
        candidate = auth.get("token")
        if isinstance(candidate, str):
            presented = candidate
    if presented is None:
        raw_cookie = environ.get("HTTP_COOKIE", "")
        for chunk in raw_cookie.split(";"):
            name, _, value = chunk.strip().partition("=")
            if name == COOKIE_NAME:
                presented = value
                break
    if presented and secrets.compare_digest(presented, expected):
        return True
    # Auto-allow loopback Socket.IO connections so the on-host UI works
    # without manual cookie wiring.
    remote = environ.get("REMOTE_ADDR") or environ.get("asgi.scope", {}).get("client", [None])[0]
    if isinstance(remote, str):
        try:
            return ipaddress.ip_address(remote).is_loopback
        except ValueError:
            return False
    return False


@sio.event
async def connect(sid, environ, auth=None):
    """Client connected — reject if no valid auth cookie / handshake token."""
    if not _sio_request_authenticated(environ, auth):
        logger.warning(f"Rejecting unauthenticated Socket.IO connect from {sid}")
        raise socketio.exceptions.ConnectionRefusedError("Authentication required")
    logger.info(f"Web client connected: {sid}")

    # Send initial state to new client
    if gui_manager and twitch_client:
        await sio.emit(
            "initial_state",
            {
                "status": gui_manager.status.get(),
                "channels": gui_manager.channels.get_channels(),
                "campaigns": gui_manager.inv.get_campaigns(),
                "console": gui_manager.output.get_history(),
                "settings": gui_manager.settings.get_settings(),
                "login": gui_manager.login.get_status(),
                "manual_mode": twitch_client.get_manual_mode_info(),
                "current_drop": gui_manager.progress.get_current_drop(),
                "wanted_items": gui_manager.get_wanted_game_tree(),
            },
            room=sid,
        )


@sio.event
async def disconnect(sid):
    """Client disconnected"""
    logger.info(f"Web client disconnected: {sid}")


@sio.event
async def request_login(sid):
    """Client requested login form submission"""
    logger.info(f"Login request from client: {sid}")
    # The actual login data comes via REST API


@sio.event
async def request_reload(sid):
    """Client requested application reload"""
    if twitch_client:
        from src.config import State

        twitch_client.change_state(State.INVENTORY_FETCH)


@sio.event
async def get_wanted_items(sid):
    """Client requested wanted items list"""
    if gui_manager:
        await sio.emit("wanted_items_update", gui_manager.get_wanted_game_tree(), to=sid)


# Mount static files (CSS, JS, images)
# Web files are in project_root/web/, we're in project_root/src/web/
web_dir = Path(__file__).parent.parent.parent / "web"
if web_dir.exists():
    static_dir = web_dir / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")


# Development server runner
async def run_server(host: str = "0.0.0.0", port: int = 8080):
    """Run the web server (used for development/testing)"""
    global _server_instance
    import uvicorn

    config = uvicorn.Config(socket_app, host=host, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    _server_instance = server
    try:
        await server.serve()
    finally:
        _server_instance = None


async def shutdown_server():
    """Gracefully shutdown the web server"""
    if _server_instance:
        logger.info("Setting server.should_exit = True")
        _server_instance.should_exit = True
        # Give the server a moment to process the shutdown signal
        # The uvicorn server checks should_exit periodically
        await asyncio.sleep(0.1)


if __name__ == "__main__":
    # For standalone testing
    asyncio.run(run_server())
