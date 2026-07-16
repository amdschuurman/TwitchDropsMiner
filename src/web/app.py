from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

import socketio
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from src.auth.api_token import COOKIE_MAX_AGE, COOKIE_NAME, load_or_create_token

_DATA_DIR = Path(os.environ.get("TDM_DATA_DIR", str(Path(__file__).parent.parent.parent / "data")))
_WEB_CONFIG_FILE = _DATA_DIR / "web_config.json"
_BOT_TOKEN_FILE = _DATA_DIR / "discord_bot_token.json"
_PAIRINGS_FILE = Path(__file__).parent.parent.parent / "discord_bot" / "pairings.json"
_pair_codes: dict[str, dict] = {}  # code -> {token, expires}


def _get_account_data_dir() -> Path:
    """Account-aware data dir — same logic as src.config.paths but importable here."""
    try:
        cfg = json.loads(_WEB_CONFIG_FILE.read_text()) if _WEB_CONFIG_FILE.exists() else {}
        account = cfg.get("active_account")
        if account:
            d = _DATA_DIR / "accounts" / account
            d.mkdir(parents=True, exist_ok=True)
            return d
    except Exception:
        pass
    return _DATA_DIR


_SHARED_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0e0e10;color:#efeff1;font-family:system-ui,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px}
.card{background:#18181b;border:1px solid #2d2d35;border-radius:12px;padding:32px;width:100%;max-width:380px;text-align:center}
.logo{width:90px;height:90px;margin:0 auto 16px;display:block}
h1{font-size:1.2rem;margin-bottom:6px;color:#efeff1}
.subtitle{font-size:.85rem;color:#adadb8;margin-bottom:24px}
input{width:100%;background:#0e0e10;border:1px solid #3d3d4a;border-radius:8px;padding:10px 14px;color:#efeff1;font-size:.95rem;margin-bottom:12px;outline:none;text-align:left}
input:focus{border-color:#9147ff}
.btn{width:100%;border:none;border-radius:8px;padding:11px;font-size:.95rem;font-weight:600;cursor:pointer;margin-bottom:8px}
.btn-primary{background:#9147ff;color:#fff}
.btn-primary:hover{background:#7d3ce0}
.btn-ghost{background:transparent;color:#adadb8;border:1px solid #3d3d4a}
.btn-ghost:hover{background:#2d2d35}
.err{color:#eb4a4a;font-size:.85rem;margin-bottom:12px;text-align:left}
.info{color:#adadb8;font-size:.82rem;margin-bottom:12px;text-align:left;line-height:1.5}
hr{border:none;border-top:1px solid #2d2d35;margin:16px 0}
"""


def _load_web_config() -> dict:
    if _WEB_CONFIG_FILE.exists():
        try:
            return json.loads(_WEB_CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_web_config(config: dict) -> None:
    _DATA_DIR.mkdir(exist_ok=True)
    _WEB_CONFIG_FILE.write_text(json.dumps(config, indent=2))


def _hash_password(password: str) -> str:
    """scrypt-hash a password for at-rest storage in web_config.json."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _verify_password_hash(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$", 2)
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1
        )
        return secrets.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def _password_configured() -> bool:
    cfg = _load_web_config()
    # Config file takes priority over env var once setup is done
    if cfg.get("setup_done"):
        return bool(cfg.get("password_hash", ""))
    return bool(os.environ.get("WEB_PASSWORD", ""))


def _verify_password(password: str) -> bool:
    if not password:
        return False
    cfg = _load_web_config()
    if cfg.get("setup_done"):
        stored = cfg.get("password_hash", "")
        return bool(stored) and _verify_password_hash(password, stored)
    env_pw = os.environ.get("WEB_PASSWORD", "")
    return bool(env_pw) and secrets.compare_digest(password, env_pw)


def _upgrade_legacy_password_storage() -> None:
    """One-time migration: hash a plaintext 'password' left by older versions."""
    cfg = _load_web_config()
    if "password" not in cfg:
        return
    legacy = cfg.pop("password")
    if legacy and not cfg.get("password_hash"):
        cfg["password_hash"] = _hash_password(legacy)
    _save_web_config(cfg)


_upgrade_legacy_password_storage()


def _get_bot_token() -> str:
    try:
        if _BOT_TOKEN_FILE.exists():
            return json.loads(_BOT_TOKEN_FILE.read_text()).get("token", "")
    except Exception:
        pass
    return ""


def _save_bot_token(token: str) -> None:
    _DATA_DIR.mkdir(exist_ok=True)
    _BOT_TOKEN_FILE.write_text(json.dumps({"token": token}))


def _is_setup_done() -> bool:
    return _load_web_config().get("setup_done", False)


def _get_push_config() -> dict:
    cfg = _load_web_config()
    return {
        "push_notifications_enabled": cfg.get("push_notifications_enabled", False),
        "push_sound_enabled": cfg.get("push_sound_enabled", True),
        "campaign_end_alerts_enabled": cfg.get("campaign_end_alerts_enabled", True),
    }


def _aggregate_stats() -> dict:
    from collections import defaultdict
    hist_file = _get_account_data_dir() / "drops_history.json"
    if not hist_file.exists():
        return {"total_claims": 0, "by_game": [], "by_day": [], "recent": []}
    try:
        history: list[dict] = json.loads(hist_file.read_text())
    except Exception:
        return {"total_claims": 0, "by_game": [], "by_day": [], "recent": []}

    by_game: dict[str, int] = defaultdict(int)
    by_day: dict[str, int] = defaultdict(int)

    for entry in history:
        by_game[entry.get("game", "Unknown")] += 1
        ts = entry.get("timestamp", "")
        if ts:
            day = ts[:10]  # "YYYY-MM-DD"
            by_day[day] += 1

    sorted_games = sorted(by_game.items(), key=lambda x: x[1], reverse=True)
    sorted_days = sorted(by_day.items())

    recent = [e for e in history if e.get("image_url")][:10]
    if len(recent) < 10:
        seen = {e.get("reward") for e in recent}
        for e in history:
            if e.get("reward") not in seen and len(recent) < 10:
                recent.append(e)
                seen.add(e.get("reward"))

    from src.services.drop_history import get_total_claims

    return {
        "total_claims": get_total_claims(_get_account_data_dir()),
        "by_game": [{"game": g, "count": c} for g, c in sorted_games[:10]],
        "by_day": [{"date": d, "count": c} for d, c in sorted_days[-365:]],
        "recent": recent[:10],
    }


def _vienna_today() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Europe/Vienna")).strftime("%Y-%m-%d")


def _load_daily_points() -> dict:
    try:
        p = _get_account_data_dir() / "daily_points.json"
        if p.exists():
            d = json.loads(p.read_text())
            if d.get("date") == _vienna_today():
                return d
    except Exception:
        pass
    return {"date": _vienna_today(), "total": 0}


def _save_daily_points(total: int) -> None:
    p = _get_account_data_dir() / "daily_points.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date": _vienna_today(), "total": total}))


def _load_channel_points_history() -> dict:
    try:
        p = _get_account_data_dir() / "channel_points.json"
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return {}


# ==================== Unified auth gate ====================
#
# Two credential systems, one gate. A request is authorized when ANY of these
# hold (checked in this order):
#
#   1. ``TDM_AUTH_DISABLED=true`` — the operator opted out entirely (deployments
#      fronted by an auth-providing reverse proxy: NPM access list, Authelia,
#      Cloudflare Access, ...).
#   2. Bearer token — the machine-generated secret from ``src/auth/api_token.py``,
#      presented either as the ``tdm_session`` cookie (installed by the one-shot
#      ``/api/session/bootstrap?token=...`` URL printed at startup, or
#      auto-installed for loopback visits) or as an ``Authorization: Bearer``
#      header (for scripts).
#   3. Discord-bot token — the ``X-Bot-Token`` header obtained via the pairing
#      flow; accepted for ``/api/*`` paths only.
#   4. Password session — when a web password is configured (first-run
#      ``/__setup`` flow, or the ``WEB_PASSWORD`` env var until setup is done),
#      a per-instance ``__tdm_session_<port>`` cookie holding a random token
#      issued at login; tokens are stored sha256-hashed with an expiry in
#      web_config.json, and the password itself is stored scrypt-hashed.
#   5. Loopback auto-auth — 127.0.0.1/::1 clients pass without credentials, but
#      ONLY while no password is configured. Once the operator sets a password,
#      it applies to loopback too (a same-host reverse proxy would otherwise
#      make every remote client look like loopback and neutralize the password).
#
# Public (never gated): the setup/login pages, favicon/logo/manifest, static
# assets, /healthz (Docker liveness — must return 200 with no session),
# /api/pair/claim (the one-time pairing code IS the credential),
# /api/session/bootstrap (validates its own token), the instance-switcher
# registry reads (GET /api/instance + GET /api/instances), and the static
# metadata endpoints /api/languages, /api/translations, /api/version that the
# first page render needs before any credential exists. The HTML shell ``/`` is
# additionally public while no password is set (token-only mode gates the data
# endpoints individually instead).
#
# Socket.IO connections do not pass through this HTTP middleware (they are
# routed by socketio.ASGIApp before FastAPI); the ``connect`` handler enforces
# the same rules via ``_sio_request_authenticated``.


def _trusted_origins() -> list[str]:
    """Return the operator-defined allowlist for cross-origin browser requests.

    Configured via the ``TDM_TRUSTED_ORIGINS`` env var as a comma-separated list
    of origins (e.g. ``https://twitchdrops.example.com,https://lan.example``).
    Empty by default - the regex below already covers the loopback origins that
    on-host installs need.
    """
    raw = os.environ.get("TDM_TRUSTED_ORIGINS", "").strip()
    if not raw:
        return []
    return [o.strip() for o in raw.split(",") if o.strip()]


def _auth_disabled() -> bool:
    """True when the operator opts out of the auth gate.

    Single-user deployments fronted by an auth-providing reverse proxy (NPM
    access list, Authelia, Cloudflare Access, etc.) can set ``TDM_AUTH_DISABLED=true``
    so the local UI works without the bootstrap-cookie or password flow.
    """
    return os.environ.get("TDM_AUTH_DISABLED", "").lower() in ("1", "true", "yes", "on")


def _host_is_loopback(host: str | None) -> bool:
    """True only for a real loopback peer address; fail closed otherwise.

    The single source of truth for loopback auto-auth on BOTH the HTTP and
    Socket.IO planes, so the two can never disagree. Never trust
    ``environ['REMOTE_ADDR']`` on the Socket.IO side: python-engineio's ASGI
    driver hardcodes it to ``127.0.0.1``, which would make every proxied
    client look local and silently bypass the gate. The genuine peer comes
    from the ASGI ``scope['client']`` tuple (what Starlette's request.client
    exposes too).
    """
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _client_is_loopback(request: Request) -> bool:
    return _host_is_loopback(request.client.host if request.client else None)


def _sio_client_host(environ: dict) -> str | None:
    """The genuine Socket.IO peer address from the ASGI scope (not REMOTE_ADDR)."""
    scope = environ.get("asgi.scope")
    client = scope.get("client") if isinstance(scope, dict) else None
    if isinstance(client, (list, tuple)) and client:
        return client[0]
    return None


def _expected_token() -> str:
    # Cached after the first call; load_or_create_token() also caches behaviorally
    # since it reads from the same file each time.
    return load_or_create_token()


# Password sessions are random bearer tokens, stored sha256-hashed (with an
# expiry) in web_config.json — the password itself never reaches the cookie
# jar and a leaked config file exposes neither passwords nor live sessions.
# The cookie name is suffixed with TDM_PORT so parallel instances behind one
# domain (SimpliAj's /acc2/ nginx setup) don't clobber each other's sessions.
_PW_SESSION_COOKIE = f"__tdm_session_{os.environ.get('TDM_PORT', '8080')}"
_PW_SESSION_MAX_AGE = 60 * 60 * 24 * 30
_PW_SESSION_CAP = 20  # concurrent logins kept per instance


def _issue_password_session() -> str:
    token = secrets.token_urlsafe(32)
    cfg = _load_web_config()
    now = time.time()
    sessions = [s for s in cfg.get("sessions", []) if s.get("expires", 0) > now]
    sessions.append({
        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "expires": now + _PW_SESSION_MAX_AGE,
    })
    cfg["sessions"] = sessions[-_PW_SESSION_CAP:]
    _save_web_config(cfg)
    return token


def _password_session_valid(token: str) -> bool:
    if not token:
        return False
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.time()
    return any(
        s.get("expires", 0) > now
        and secrets.compare_digest(s.get("token_sha256", ""), token_hash)
        for s in _load_web_config().get("sessions", [])
    )


def _revoke_password_session(token: str) -> None:
    if not token:
        return
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    cfg = _load_web_config()
    sessions = [
        s for s in cfg.get("sessions", [])
        if not secrets.compare_digest(s.get("token_sha256", ""), token_hash)
    ]
    cfg["sessions"] = sessions
    _save_web_config(cfg)


_PUBLIC_PATHS = {
    "/__auth_login", "/__auth_login_page",
    "/__setup", "/__setup_post",
    "/favicon.ico", "/logo.png", "/manifest.json",
    "/api/pair/claim",   # Discord bot pairing — the one-time code is the credential
    "/healthz",          # Docker/orchestrator liveness probe — must pass with no session
    "/api/session/bootstrap",  # validates its own ?token= parameter
    # Static metadata the very first page render needs before any credential
    # exists (language picker, translation bundle, footer version).
    "/api/languages",
    "/api/translations",
    "/api/version",
}
# Instance-switcher registry: public for cross-port switcher UI reads, but only
# reads — POST /api/instances (create) stays behind the gate.
_PUBLIC_GET_PATHS = {"/api/instance", "/api/instances"}
_UNPROTECTED_PREFIXES = ("/static/",)


def _is_public_request(path: str, method: str) -> bool:
    if path in _PUBLIC_PATHS:
        return True
    if path in _PUBLIC_GET_PATHS and method in ("GET", "HEAD", "OPTIONS"):
        return True
    return any(path.startswith(p) for p in _UNPROTECTED_PREFIXES)


def _http_request_authorized(request: Request) -> bool:
    """Single credential check used by the middleware for every gated path."""
    if _auth_disabled():
        return True
    expected = _expected_token()
    # Bearer token: session cookie or Authorization header.
    presented = request.cookies.get(COOKIE_NAME)
    if presented is None:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            presented = auth_header.split(None, 1)[1].strip()
    if presented and secrets.compare_digest(presented, expected):
        return True
    # Discord-bot token, API paths only.
    if request.url.path.startswith("/api/"):
        bot_token_header = request.headers.get("X-Bot-Token", "")
        saved_bot_token = _get_bot_token()
        if (
            saved_bot_token
            and bot_token_header
            and secrets.compare_digest(bot_token_header, saved_bot_token)
        ):
            return True
    if _password_configured():
        # Password mode: the password gates everyone, loopback included — a
        # same-host reverse proxy makes every client look like loopback, so a
        # loopback bypass here would silently defeat the operator's password.
        return _password_session_valid(request.cookies.get(_PW_SESSION_COOKIE, ""))
    # Token-only mode: loopback requests without a credential are allowed so the
    # bootstrap-on-first-visit flow can install the cookie (on-host UX unchanged).
    return _client_is_loopback(request)


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><title>TwitchDropsMiner</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<style>{css}</style></head>
<body><div class="card">
<img class="logo" src="/logo.png" alt="TwitchDropsMiner">
<h1>TwitchDropsMiner</h1>
<p class="subtitle">Web Interface</p>
{{error}}
<form method="POST" action="/__auth_login">
<input type="password" name="password" placeholder="Passwort" autofocus autocomplete="current-password">
<button class="btn btn-primary" type="submit">Einloggen</button>
</form></div></body></html>""".format(css=_SHARED_CSS)

_SETUP_HTML = """<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><title>TwitchDropsMiner – Setup</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<style>{css}</style></head>
<body><div class="card">
<img class="logo" src="/logo.png" alt="TwitchDropsMiner">
<h1>Willkommen!</h1>
<p class="subtitle">Erstmalige Einrichtung</p>
{{error}}
<p class="info">Möchtest du den Zugang mit einem Passwort schützen?<br>
Du kannst dies später in den Einstellungen ändern.</p>
<form method="POST" action="/__setup_post">
<input type="password" name="password" placeholder="Passwort festlegen (optional)" autocomplete="new-password">
<input type="password" name="password2" placeholder="Passwort wiederholen" autocomplete="new-password">
<button class="btn btn-primary" type="submit" name="action" value="set">Mit Passwort starten</button>
<hr>
<button class="btn btn-ghost" type="submit" name="action" value="skip">Ohne Passwort starten</button>
</form></div></body></html>""".format(css=_SHARED_CSS)


def _render_template(template: str, error: str = "") -> str:
    return template.replace("{error}", error)


class UnifiedAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # First-time setup: redirect everything non-public to the setup page.
        # Skipped when the operator disabled the gate entirely — a password
        # wizard whose result is never enforced would be misleading.
        if (
            not _auth_disabled()
            and not _is_setup_done()
            and not _is_public_request(path, request.method)
        ):
            return RedirectResponse("/__setup", status_code=302)
        # Public paths always pass through
        if _is_public_request(path, request.method):
            response = await call_next(request)
            if any(path.startswith(p) for p in _UNPROTECTED_PREFIXES):
                response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return response
        if _http_request_authorized(request):
            return await call_next(request)
        # The HTML shell stays public in token-only mode (it is static; every
        # data endpoint is gated individually and the frontend shows the
        # bootstrap-URL banner on 401s).
        if path == "/" and not _password_configured():
            return await call_next(request)
        # Unauthorized: JSON for API consumers, the login page for navigations
        # when a password is configured.
        if not path.startswith("/api/") and _password_configured():
            return HTMLResponse(_render_template(_LOGIN_HTML), status_code=401)
        return JSONResponse({"detail": "Authentication required"}, status_code=401)


if TYPE_CHECKING:
    import uvicorn

    from src.core.client import Twitch
    from src.web.gui_manager import WebGUIManager


logger = logging.getLogger("TwitchDrops")

# Create FastAPI app
app = FastAPI(title="Twitch Drops Miner Web", version="1.0.0")

# Unified auth gate (added first so it runs INSIDE the CORS middleware —
# preflights and 401 responses still get CORS headers).
app.add_middleware(UnifiedAuthMiddleware)

# CORS is restrictive by default: same-origin only. Browsers loading the page
# from http://localhost:8080 send same-origin requests, which never trigger a
# CORS preflight — so the wildcard removal does not break the local UI. The
# localhost regex covers any local port, which the multi-instance switcher
# needs for its cross-port registry reads. Operators extend the allowlist via
# ``TDM_TRUSTED_ORIGINS`` for reverse-proxied deployments.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_trusted_origins(),
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?",
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "DELETE", "PATCH"],
    allow_headers=["Content-Type", "Authorization", "X-Bot-Token"],
)

# Create Socket.IO server. CORS defaults to local origins only (built for this
# instance's TDM_PORT plus the default 8080); the operator extends the
# allowlist via ``TDM_TRUSTED_ORIGINS`` for reverse-proxied deployments.
_tdm_port = os.environ.get("TDM_PORT", "8080")
_local_sio_ports = sorted({"8080", _tdm_port})
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=[
        *[
            f"http://{host}:{port}"
            for port in _local_sio_ports
            for host in ("localhost", "127.0.0.1", "[::1]")
        ],
        *_trusted_origins(),
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
    _patch_status_for_persistence(gui)
    asyncio.create_task(_auto_resume_mode())


def _patch_status_for_persistence(gui: WebGUIManager) -> None:
    """Wrap StatusManager.update to auto-save last_mode when idle."""
    import re as _re
    _orig = gui.status.update

    def _patched(status: str) -> None:
        if "💤" in status or "idle watching" in status.lower():
            cfg = _load_web_config()
            cfg["last_mode"] = "idle_watch"
            m = _re.search(r"idle watching:\s*(\S+)", status, _re.IGNORECASE)
            if m:
                cfg["last_idle_channel"] = m.group(1)
            _save_web_config(cfg)
        _orig(status)

    gui.status.update = _patched


async def _auto_resume_mode() -> None:
    """After startup, resume idle_watch if that was the last mode."""
    await asyncio.sleep(20)  # Wait for miner to log in and initialize
    if not twitch_client or not gui_manager:
        return
    cfg = _load_web_config()
    if cfg.get("last_mode") != "idle_watch":
        return
    # Only resume if the miner isn't already doing something active
    current_status = gui_manager.status.get().lower()
    if "💤" in current_status or "idle" in current_status:
        return  # Already idle-watching
    last_channel = cfg.get("last_idle_channel")
    if not last_channel:
        return
    try:
        channel = await twitch_client._fetch_idle_channel_by_login(last_channel)
        if channel is not None:
            twitch_client.gui.clear_drop()
            twitch_client.watch(channel, update_status=False)
            twitch_client.gui.status.update(f"💤 Idle watching: {channel.name}")
    except Exception:
        pass


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
    claim_channel_points: bool | None = None
    idle_channels: list[str] | None = None
    idle_use_followed: bool | None = None
    idle_parallel: bool | None = None
    preferred_games: list[str] | None = None
    scheduler_enabled: bool | None = None
    scheduler_start: str | None = None
    scheduler_stop: str | None = None
    discord_webhook_drops: str | None = None
    discord_webhook_points: str | None = None
    discord_webhook_mentions: str | None = None
    drop_name_blacklist: list[str] | None = None
    auto_prioritize: bool | None = None
    auto_add_linked: bool | None = None
    tab_counter_enabled: bool | None = None
    make_predictions: bool | None = None
    bet_strategy: str | None = None
    bet_percentage: int | None = None
    bet_max_points: int | None = None
    bet_minimum_points: int | None = None
    bet_percentage_gap: int | None = None
    bet_delay_seconds: int | None = None
    prediction_channels: list[str] | None = None
    channel_strategies: dict[str, str] | None = None
    claim_moments: bool | None = None
    irc_chat_presence: bool | None = None
    irc_mention_notify: bool | None = None


class ProxyVerifyRequest(BaseModel):
    proxy: str


class PairClaimRequest(BaseModel):
    code: str


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


def _validate_webhook_url(url: str) -> None:
    """SSRF guard for operator-supplied webhook URLs.

    Applied at settings-write time and on test-webhook, so the notification
    send path only ever posts to validated targets. Rejects URLs whose host
    resolves to loopback/private/link-local/multicast/reserved addresses —
    the webhook feature is meant for Discord, not for probing the LAN behind
    the miner. Self-hosted receivers on private networks are a legitimate
    homelab use, so ``TDM_ALLOW_PRIVATE_WEBHOOKS=true`` opts out of the
    address check (scheme/host sanity always applies).
    """
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Webhook URL must be http(s)")
    if os.environ.get("TDM_ALLOW_PRIVATE_WEBHOOKS", "").lower() in ("1", "true", "yes", "on"):
        return
    import socket
    host = parsed.hostname.rstrip(".").lower()
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="Webhook host is not resolvable")
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Webhook URL resolves to a non-public address"
                    " (set TDM_ALLOW_PRIVATE_WEBHOOKS=true to allow LAN receivers)"
                ),
            )


# ==================== Auth Endpoints ====================


@app.get("/__setup", response_class=HTMLResponse)
async def setup_page():
    if _is_setup_done():
        return RedirectResponse("/", status_code=302)
    return HTMLResponse(_render_template(_SETUP_HTML))


@app.post("/__setup_post")
async def setup_post(request: Request):
    if _is_setup_done():
        return RedirectResponse("/", status_code=302)
    form = await request.form()
    action = form.get("action", "skip")
    if action == "set":
        pw = form.get("password", "")
        pw2 = form.get("password2", "")
        if not pw:
            return HTMLResponse(_render_template(_SETUP_HTML, '<p class="err">Bitte ein Passwort eingeben.</p>'))
        if pw != pw2:
            return HTMLResponse(_render_template(_SETUP_HTML, '<p class="err">Passwörter stimmen nicht überein.</p>'))
        cfg = _load_web_config()
        cfg["setup_done"] = True
        cfg["password_hash"] = _hash_password(pw)
        cfg.pop("password", None)
        _save_web_config(cfg)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            _PW_SESSION_COOKIE, _issue_password_session(),
            httponly=True, samesite="lax", max_age=_PW_SESSION_MAX_AGE,
        )
        return resp
    else:
        cfg = _load_web_config()
        cfg["setup_done"] = True
        cfg["password_hash"] = ""
        cfg.pop("password", None)
        _save_web_config(cfg)
        return RedirectResponse("/", status_code=303)


@app.post("/__auth_login")
async def auth_login_post(request: Request):
    form = await request.form()
    pw = form.get("password", "")
    if _verify_password(pw):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            _PW_SESSION_COOKIE, _issue_password_session(),
            httponly=True, samesite="lax", max_age=_PW_SESSION_MAX_AGE,
        )
        return resp
    return HTMLResponse(_render_template(_LOGIN_HTML, '<p class="err">Falsches Passwort</p>'), status_code=401)


@app.get("/__auth_logout")
async def auth_logout(request: Request):
    _revoke_password_session(request.cookies.get(_PW_SESSION_COOKIE, ""))
    resp = RedirectResponse("/__auth_login_page", status_code=303)
    resp.delete_cookie(_PW_SESSION_COOKIE)
    return resp


@app.get("/__auth_login_page", response_class=HTMLResponse)
async def auth_login_page():
    return HTMLResponse(_render_template(_LOGIN_HTML))


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


# ==================== Static Assets ====================

_web_dir = Path(__file__).parent.parent.parent / "web"


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(_web_dir / "favicon.ico", media_type="image/x-icon")


@app.get("/logo.png")
async def logo():
    return FileResponse(_web_dir / "logo.png", media_type="image/png")


@app.get("/manifest.json")
async def manifest():
    return FileResponse(_web_dir / "manifest.json", media_type="application/manifest+json")


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
    # Skipped when a password is configured: the token cookie must not outlive
    # or bypass a deliberately-set password.
    if (
        not _password_configured()
        and _client_is_loopback(request)
        and request.cookies.get(COOKIE_NAME) is None
    ):
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
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/healthz")
async def healthz():
    """Unauthenticated liveness probe for Docker/orchestrators — no session state exposed."""
    return {"status": "ok"}


@app.get("/api/status")
async def get_status():
    """Get current application status"""
    if not gui_manager or not twitch_client:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {
        "status": gui_manager.status.get(),
        "login": gui_manager.login.get_status(),
        "manual_mode": twitch_client.get_manual_mode_info(),
        "paused": twitch_client.is_paused() if twitch_client else False,
    }


@app.post("/api/pause")
async def pause_miner():
    """Pause the miner."""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Not ready")
    twitch_client.pause(source="user")
    return {"success": True, "paused": True}


@app.post("/api/resume")
async def resume_miner():
    """Resume the miner."""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Not ready")
    twitch_client.resume(user_override=True)
    return {"success": True, "paused": False}


@app.get("/api/channels")
async def get_channels():
    """Get list of tracked channels"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {"channels": gui_manager.channels.get_channels()}


@app.post("/api/channels/select")
async def select_channel(request: ChannelSelectRequest):
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
async def get_campaigns():
    """Get campaign inventory"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {"campaigns": gui_manager.inv.get_campaigns()}


@app.get("/api/console")
async def get_console_history():
    """Get console output history"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return {"lines": gui_manager.output.get_history()}


@app.get("/api/channel-points/{channel_login}")
async def get_channel_points(channel_login: str):
    """Fetch current channel points balance for a channel."""
    if not gui_manager or not gui_manager._twitch:
        raise HTTPException(status_code=503, detail="Not ready")
    try:
        from src.config import GQL_OPERATIONS
        resp = await gui_manager._twitch.gql_request(
            GQL_OPERATIONS["ChannelPointsContext"].with_variables({"channelLogin": channel_login})
        )
        data = resp.get("data") or {}
        cp_obj = None
        try:
            cp_obj = data["community"]["channel"]["self"]["communityPoints"]
        except (KeyError, TypeError):
            pass
        cp_enabled = cp_obj is not None
        points: int = int(cp_obj.get("balance", 0)) if cp_enabled else 0
        # Persist
        if points:
            from src.utils import json_load, json_save
            _cp_file = _get_account_data_dir() / "channel_points.json"
            history = json_load(_cp_file, {}, merge=False)
            history[channel_login] = points
            json_save(_cp_file, history)
        # Include last chest bonus info for Discord bot split notification
        last_chest = {}
        try:
            _chest_file = _get_account_data_dir() / "last_chest.json"
            if _chest_file.exists():
                _chest_data = json_load(_chest_file, {}, merge=False)
                last_chest = _chest_data.get(channel_login, {})
        except Exception:
            pass
        return {"channel": channel_login, "balance": points, "cp_enabled": cp_enabled, "last_chest": last_chest}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/analytics/points")
async def get_analytics_points(channel: str = "", days: int = 7):
    """Return timestamped channel points history for analytics chart."""
    days = max(1, min(days, 365))
    from datetime import datetime, timezone, timedelta
    ts_file = _get_account_data_dir() / "channel_points_ts.json"
    try:
        data = json.loads(ts_file.read_text()) if ts_file.exists() else {}
    except Exception:
        data = {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    if channel:
        raw = data.get(channel.lower(), [])
        filtered = [p for p in raw if p.get("ts", "") >= cutoff]
        return {"channels": {channel.lower(): filtered}}
    result = {}
    for login, snapshots in data.items():
        result[login] = [p for p in snapshots if p.get("ts", "") >= cutoff]
    return {"channels": result}


@app.get("/api/drops-history")
async def get_drops_history():
    """Return all claimed drops history."""
    hist_file = _get_account_data_dir() / "drops_history.json"
    try:
        if hist_file.exists():
            return json.loads(hist_file.read_text())
    except Exception:
        pass
    return []


@app.get("/api/stats")
async def get_stats(request: Request):
    return _aggregate_stats()


@app.get("/api/idle-watch/status")
async def idle_watch_status():
    """Check if the current idle channel is online."""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Not ready")
    ch = twitch_client.watching_channel.get_with_default(None)
    if ch is None:
        return {"watching": None, "online": False}
    # Re-fetch stream info to get current status
    try:
        from src.config import GQL_OPERATIONS
        resp = await twitch_client.gql_request(
            GQL_OPERATIONS["GetStreamInfo"].with_variables({"channel": ch._login})
        )
        user_data = resp["data"]["user"]
        online = bool(user_data and user_data.get("stream"))
        return {"watching": ch._login, "online": online, "display_name": ch.name}
    except Exception:
        return {"watching": ch._login, "online": ch.online}


@app.post("/api/idle-watch/switch")
async def idle_watch_switch():
    """Switch to the next idle channel (manual list or followed). Saves last_mode."""
    cfg = _load_web_config()
    cfg["last_mode"] = "idle_watch"
    _save_web_config(cfg)
    if not gui_manager or not twitch_client:
        raise HTTPException(status_code=503, detail="Not ready")

    # Build candidate list: manual channels first, then followed live channels
    candidates: list[str] = list(twitch_client.settings.idle_channels)
    if twitch_client.settings.idle_use_followed:
        followed = await twitch_client._fetch_followed_live_logins()
        seen = set(candidates)
        for login in followed:
            if login not in seen:
                candidates.append(login)
                seen.add(login)

    if not candidates:
        raise HTTPException(status_code=400, detail="No idle channels configured")

    current = twitch_client.watching_channel.get_with_default(None)
    current_login = current._login if current else None
    idx = candidates.index(current_login) if current_login in candidates else -1

    # Try channels starting after the current one, wrapping around
    for offset in range(1, len(candidates) + 1):
        next_login = candidates[(idx + offset) % len(candidates)]
        channel = await twitch_client._fetch_idle_channel_by_login(next_login)
        if channel is not None:
            twitch_client.gui.clear_drop()
            twitch_client.watch(channel, update_status=False)
            twitch_client.gui.status.update(f"💤 Idle watching: {channel.name}")
            cfg2 = _load_web_config()
            cfg2["last_idle_channel"] = next_login
            _save_web_config(cfg2)
            return {"switched_to": next_login}

    raise HTTPException(status_code=404, detail="No other idle channels are online")


@app.post("/api/idle-watch/resume")
async def idle_watch_resume():
    """Resume watching the last known idle channel, falling back to switch."""
    if not gui_manager or not twitch_client:
        raise HTTPException(status_code=503, detail="Not ready")
    cfg = _load_web_config()
    last_channel = cfg.get("last_idle_channel")
    if last_channel:
        channel = await twitch_client._fetch_idle_channel_by_login(last_channel)
        if channel is not None:
            twitch_client.gui.clear_drop()
            twitch_client.watch(channel, update_status=False)
            twitch_client.gui.status.update(f"💤 Idle watching: {channel.name}")
            return {"switched_to": last_channel, "resumed": True}
    return await idle_watch_switch()


@app.get("/api/settings")
async def get_settings():
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
    settings["bot_paired"] = bool(_get_bot_token())
    return settings


@app.get("/api/languages")
async def get_languages():
    # Public (see _PUBLIC_PATHS): language list is static metadata the frontend
    # fetches before any credential exists, so the picker renders on first load.
    """Get available languages"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    return gui_manager.settings.get_languages()


@app.get("/api/translations")
async def get_translations():
    # Public (see _PUBLIC_PATHS): translation bundles are static UI strings,
    # identical to what ships in the repo.
    """Get translations for current language"""
    from src.i18n.translator import _

    # Return the full Translation object
    return _.t


@app.post("/api/settings")
async def update_settings(settings: SettingsUpdate):
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
    # Webhook URLs are validated at write time so the notification send path
    # (message_handlers._send_discord_webhook) only ever sees vetted targets.
    for _wh_key in ("discord_webhook_drops", "discord_webhook_points", "discord_webhook_mentions"):
        _wh_url = settings_dict.get(_wh_key)
        if isinstance(_wh_url, str) and _wh_url.strip():
            _validate_webhook_url(_wh_url)
    gui_manager.settings.update_settings(settings_dict)
    return {"success": True, "settings": gui_manager.settings.get_settings()}


@app.post("/api/settings/verify-proxy")
async def verify_proxy(request: ProxyVerifyRequest):
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


@app.post("/api/settings/test-webhook")
async def test_webhook(request: Request):
    """Send a test message to a Discord webhook URL"""
    import aiohttp
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        return {"success": False, "message": "No URL provided"}
    _validate_webhook_url(url)
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.post(url, json={
                "embeds": [{
                    "title": "✅ Webhook Test",
                    "description": "TwitchDropsMiner webhook is working!",
                    "color": 0x9147ff,
                }]
            }, timeout=aiohttp.ClientTimeout(total=10), allow_redirects=False)
            if resp.status in (200, 204):
                return {"success": True, "message": "Sent!"}
            return {"success": False, "message": f"HTTP {resp.status}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/api/instance")
async def get_instance():
    port = int(os.environ.get("TDM_PORT", 8080))
    label = os.environ.get("TDM_LABEL", f"Instance {port}")
    login = None
    try:
        if twitch_client and hasattr(twitch_client._auth_state, "user_login"):
            login = twitch_client._auth_state.user_login
    except Exception:
        pass
    return {"port": port, "label": label, "login": login}


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
    # Public (see _PUBLIC_PATHS): just the installed version + GitHub release
    # lookup. Useful to show in the footer before the user authenticates.
    """Get current application version and check for updates"""
    import aiohttp

    from src.version import __version__

    current_version = __version__
    latest_version = None
    update_available = False
    download_url = None
    release_notes = ""

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
                release_notes = data.get("body", "")

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
        "release_notes": release_notes,
    }


@app.get("/api/wanted-items")
async def get_wanted_items_http():
    if not gui_manager:
        raise HTTPException(status_code=503, detail="Not initialized")
    return {"wanted_items": gui_manager.get_wanted_game_tree()}


@app.post("/api/login")
async def submit_login(login_data: LoginRequest):
    """Submit login credentials"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    gui_manager.login.submit_login(login_data.username, login_data.password, login_data.token)
    return {"success": True}


@app.post("/api/oauth/confirm")
async def confirm_oauth():
    """Confirm OAuth code has been entered by user"""
    if not gui_manager:
        raise HTTPException(status_code=503, detail="GUI not initialized")

    # Just set the event to signal the user has acknowledged the code
    gui_manager.login._login_event.set()
    return {"success": True}


@app.post("/api/reload")
async def trigger_reload():
    """Trigger application reload"""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")

    from src.config import State

    cfg = _load_web_config()
    cfg["last_mode"] = "drop_mining"
    _save_web_config(cfg)
    twitch_client.change_state(State.INVENTORY_FETCH)
    return {"success": True}


@app.post("/api/close")
async def trigger_close():
    """Trigger application shutdown"""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")

    twitch_client.close()
    return {"success": True}


@app.post("/api/restart")
async def trigger_restart():
    """Restart via PM2 (stop + auto-restart by PM2)"""
    import subprocess

    async def _restart():
        await asyncio.sleep(1)
        subprocess.Popen(["pm2", "restart", "twitchdrops"])

    asyncio.create_task(_restart())
    return {"success": True}


def _is_docker() -> bool:
    return Path("/.dockerenv").exists() or os.environ.get("DOCKER_CONTAINER") == "1"


@app.post("/api/self-update")
async def self_update():
    """Pull latest code from GitHub and restart — detects Docker vs PM2"""
    import subprocess

    if _is_docker():
        return {
            "success": False,
            "docker": True,
            "log": (
                "Running inside Docker.\n\n"
                "To update, run on your host:\n\n"
                "  docker compose pull\n"
                "  docker compose up -d"
            ),
        }

    repo_dir = Path(__file__).parent.parent.parent
    logs = []

    try:
        result = subprocess.run(
            ["git", "pull", "simpliaj", "main"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        logs.append(result.stdout.strip() or "(no output)")
        if result.stderr.strip():
            logs.append(result.stderr.strip())
        success = result.returncode == 0
    except Exception as e:
        logs.append(f"Error: {e}")
        success = False

    async def _restart():
        await asyncio.sleep(2)
        subprocess.Popen(["pm2", "restart", "twitchdrops", "twitchdrops2"])

    asyncio.create_task(_restart())
    return {"success": success, "log": "\n".join(logs)}


@app.post("/api/skip-game")
async def skip_game():
    """Skip current game and switch to a different game"""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")

    twitch_client.skip_current_game()
    return {"success": True}


@app.post("/api/mode/exit-manual")
async def exit_manual_mode():
    """Exit manual mode and return to automatic channel selection"""
    if not twitch_client:
        raise HTTPException(status_code=503, detail="Twitch client not initialized")

    if not twitch_client.is_manual_mode():
        return {"success": False, "message": "Not in manual mode"}

    twitch_client.exit_manual_mode("User requested")
    return {"success": True}


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class PasswordDisableRequest(BaseModel):
    current_password: str


@app.post("/api/auth/change-password")
async def change_password(data: PasswordChangeRequest, request: Request):
    if _password_configured() and not _verify_password(data.current_password):
        raise HTTPException(status_code=403, detail="Aktuelles Passwort falsch")
    cfg = _load_web_config()
    cfg["password_hash"] = _hash_password(data.new_password) if data.new_password else ""
    cfg.pop("password", None)
    cfg["setup_done"] = True
    cfg["sessions"] = []  # a password change revokes every existing session
    _save_web_config(cfg)
    response = JSONResponse({"success": True})
    if data.new_password:
        response.set_cookie(
            _PW_SESSION_COOKIE, _issue_password_session(),
            httponly=True, samesite="lax", max_age=_PW_SESSION_MAX_AGE,
        )
    else:
        response.delete_cookie(_PW_SESSION_COOKIE)
    return response


@app.post("/api/auth/disable-password")
async def disable_password(data: PasswordDisableRequest):
    if _password_configured() and not _verify_password(data.current_password):
        raise HTTPException(status_code=403, detail="Aktuelles Passwort falsch")
    cfg = _load_web_config()
    cfg["password_hash"] = ""
    cfg.pop("password", None)
    cfg["setup_done"] = True
    cfg["sessions"] = []
    _save_web_config(cfg)
    response = JSONResponse({"success": True})
    response.delete_cookie(_PW_SESSION_COOKIE)
    return response


@app.get("/api/auth/status")
async def auth_status():
    return {"password_set": _password_configured()}


# ==================== Discord Bot Pairing ====================


@app.post("/api/pair/generate")
async def pair_generate():
    """Generate a one-time pairing code for Discord bot."""
    import time
    code = "DROPS-" + secrets.token_hex(4).upper()
    token = secrets.token_hex(32)
    _pair_codes[code] = {"token": token, "expires": time.time() + 600}
    # Clean expired codes
    now = time.time()
    expired = [k for k, v in _pair_codes.items() if v["expires"] < now]
    for k in expired:
        del _pair_codes[k]
    return {"code": code, "expires_in": 600}


@app.post("/api/pair/claim")
async def pair_claim(req: PairClaimRequest):
    """Exchange pairing code for permanent bot token. No auth required."""
    import time
    entry = _pair_codes.get(req.code)
    if not entry or entry["expires"] < time.time():
        raise HTTPException(status_code=404, detail="Invalid or expired code")
    token = entry["token"]
    _save_bot_token(token)
    del _pair_codes[req.code]
    return {"token": token}


@app.get("/api/pair/status")
async def pair_status():
    """Check if a bot token is configured."""
    return {"paired": bool(_get_bot_token())}


@app.delete("/api/pair/revoke")
async def pair_revoke():
    """Revoke bot token."""
    if _BOT_TOKEN_FILE.exists():
        _BOT_TOKEN_FILE.unlink()
    return {"success": True}


def _get_bot_pairing() -> dict | None:
    """Find the pairings.json entry whose token matches the stored bot token."""
    token = _get_bot_token()
    if not token or not _PAIRINGS_FILE.exists():
        return None
    try:
        data = json.loads(_PAIRINGS_FILE.read_text())
        for uid, entry in data.get("users", {}).items():
            # New nested format: {uid: {name: pairing}}
            if isinstance(entry, dict) and "url" not in entry:
                for name, pairing in entry.items():
                    if isinstance(pairing, dict) and pairing.get("token") == token:
                        return {"discord_user_id": uid, "_pairing_name": name, **pairing}
            else:
                # Old flat format
                if entry.get("token") == token:
                    return {"discord_user_id": uid, "_pairing_name": "default", **entry}
    except Exception:
        pass
    return None


def _save_pairing_channels(discord_user_id: str, channels: dict) -> None:
    if not _PAIRINGS_FILE.exists():
        return
    data = json.loads(_PAIRINGS_FILE.read_text())
    users = data.get("users", {})
    if discord_user_id not in users:
        return
    entry = users[discord_user_id]
    if "url" not in entry:
        # New nested format — find which named pairing belongs to this instance
        token = _get_bot_token()
        for name, pairing in entry.items():
            if isinstance(pairing, dict) and pairing.get("token") == token:
                data["users"][discord_user_id][name]["channels"] = channels
                break
    else:
        data["users"][discord_user_id]["channels"] = channels
    _PAIRINGS_FILE.write_text(json.dumps(data, indent=2))


def _save_pairing_field(discord_user_id: str, field: str, value) -> None:
    if not _PAIRINGS_FILE.exists():
        return
    data = json.loads(_PAIRINGS_FILE.read_text())
    users = data.get("users", {})
    if discord_user_id not in users:
        return
    entry = users[discord_user_id]
    if "url" not in entry:
        token = _get_bot_token()
        for name, pairing in entry.items():
            if isinstance(pairing, dict) and pairing.get("token") == token:
                if value is None:
                    data["users"][discord_user_id][name].pop(field, None)
                else:
                    data["users"][discord_user_id][name][field] = value
                break
    else:
        if value is None:
            data["users"][discord_user_id].pop(field, None)
        else:
            data["users"][discord_user_id][field] = value
    _PAIRINGS_FILE.write_text(json.dumps(data, indent=2))


def _normalize_channel_entries(val) -> list[dict]:
    """Normalize a channel field (int, dict, or list of either) to list of {id,name,guild}."""
    if val is None:
        return []
    if not isinstance(val, list):
        val = [val]
    result = []
    for v in val:
        if v is None:
            continue
        if isinstance(v, dict):
            result.append({"id": str(v["id"]), "name": v.get("name", ""), "guild": v.get("guild", "")})
        else:
            result.append({"id": str(v), "name": "", "guild": ""})
    return result


@app.get("/api/discord-bot/config")
async def discord_bot_config():
    """Return channel configuration for the paired Discord bot user."""
    pairing = _get_bot_pairing()
    if not pairing:
        return {"paired": False, "channels": {}}
    channels = pairing.get("channels", {})
    return {
        "paired": True,
        "discord_user_id": pairing["discord_user_id"],
        "profile_name": pairing.get("profile_name", ""),
        "channels": {
            "drops": _normalize_channel_entries(channels.get("drops")),
            "points": _normalize_channel_entries(channels.get("points")),
        },
    }


@app.post("/api/daily-points")
async def update_daily_points(request: Request):
    body = await request.json()
    total = int(body.get("total", 0))
    _save_daily_points(max(0, total))
    return {"success": True}


@app.post("/api/discord-bot/profile-name")
async def discord_bot_set_profile_name(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()[:50]
    pairing = _get_bot_pairing()
    if not pairing:
        raise HTTPException(status_code=404, detail="No bot paired")
    _save_pairing_field(pairing["discord_user_id"], "profile_name", name or None)
    return {"success": True, "profile_name": name}


@app.delete("/api/discord-bot/config/channel/{channel_type}")
async def discord_bot_clear_channel(channel_type: str, channel_id: str | None = None):
    """Remove a specific channel (by channel_id) or all channels of a type."""
    if channel_type not in ("drops", "points"):
        raise HTTPException(status_code=400, detail="Invalid channel type")
    pairing = _get_bot_pairing()
    if not pairing:
        raise HTTPException(status_code=404, detail="No bot paired")
    channels = dict(pairing.get("channels", {}))
    if channel_id:
        current = _normalize_channel_entries(channels.get(channel_type))
        channels[channel_type] = [e for e in current if e["id"] != channel_id]
    else:
        channels[channel_type] = []
    _save_pairing_channels(pairing["discord_user_id"], channels)
    return {"success": True}


# ==================== Account Management ====================

def _safe_account_dir(label: str) -> Path:
    """Validate an account label and return its directory under data/accounts.

    Labels are directory NAMES, never paths: separators and dots are rejected,
    and the resolved result must sit directly under the accounts root, so no
    label can reach cookies/settings of another account or escape the data dir.
    """
    label = (label or "").strip()
    if not label or len(label) > 64 or any(c in label for c in "/\\.\0"):
        raise HTTPException(status_code=400, detail="Invalid account label")
    accounts_root = (_DATA_DIR / "accounts").resolve()
    account_dir = (accounts_root / label).resolve()
    if account_dir.parent != accounts_root:
        raise HTTPException(status_code=400, detail="Invalid account label")
    return account_dir


@app.get("/api/accounts")
async def list_accounts():
    """List all saved accounts and the currently active one"""
    cfg = _load_web_config()
    active = cfg.get("active_account", "")
    accounts_dir = _DATA_DIR / "accounts"
    accounts = []
    if accounts_dir.exists():
        for d in sorted(accounts_dir.iterdir()):
            if d.is_dir():
                has_cookies = (d / "cookies.jar").exists()
                accounts.append({"label": d.name, "active": d.name == active, "has_cookies": has_cookies})
    return {"accounts": accounts, "active": active}


class AccountSwitchRequest(BaseModel):
    label: str


@app.post("/api/accounts/switch")
async def switch_account(data: AccountSwitchRequest):
    """Switch active account and restart"""
    cfg = _load_web_config()
    account_dir = _safe_account_dir(data.label)
    if not account_dir.exists():
        raise HTTPException(status_code=404, detail="Account not found")
    cfg["active_account"] = data.label
    _save_web_config(cfg)
    pairing = _get_bot_pairing()
    if pairing:
        _save_pairing_field(pairing["discord_user_id"], "profile_name", data.label)
    async def _restart():
        await asyncio.sleep(1)
        import subprocess
        subprocess.Popen(["pm2", "restart", "twitchdrops"])
    asyncio.create_task(_restart())
    return {"success": True}


class AccountAddRequest(BaseModel):
    label: str


@app.post("/api/accounts/add")
async def add_account(data: AccountAddRequest):
    """Create a new account slot, switch to it, and restart for fresh login"""
    label = data.label.strip()
    account_dir = _safe_account_dir(label)
    if account_dir.exists():
        raise HTTPException(status_code=409, detail="Account label already exists")
    account_dir.mkdir(parents=True, exist_ok=True)
    cfg = _load_web_config()
    cfg["active_account"] = label
    _save_web_config(cfg)
    pairing = _get_bot_pairing()
    if pairing:
        _save_pairing_field(pairing["discord_user_id"], "profile_name", label)
    async def _restart():
        await asyncio.sleep(1)
        import subprocess
        subprocess.Popen(["pm2", "restart", "twitchdrops"])
    asyncio.create_task(_restart())
    return {"success": True}


@app.delete("/api/accounts/{label}")
async def remove_account(label: str):
    """Delete an account (cannot delete the active account)"""
    import shutil
    cfg = _load_web_config()
    if cfg.get("active_account") == label:
        raise HTTPException(status_code=400, detail="Cannot delete the active account. Switch first.")
    account_dir = _safe_account_dir(label)
    if not account_dir.exists():
        raise HTTPException(status_code=404, detail="Account not found")
    shutil.rmtree(account_dir)
    return {"success": True}


@app.patch("/api/accounts/{label}")
async def rename_account(label: str, request: Request):
    """Rename an account label (renames the folder, updates active_account and bot profile_name)"""
    import shutil
    body = await request.json()
    new_label = (body.get("new_label") or "").strip()
    account_dir = _safe_account_dir(label)
    if not account_dir.exists():
        raise HTTPException(status_code=404, detail="Account not found")
    new_dir = _safe_account_dir(new_label)
    if new_dir.exists():
        raise HTTPException(status_code=409, detail="Account label already exists")
    shutil.move(str(account_dir), str(new_dir))
    cfg = _load_web_config()
    was_active = cfg.get("active_account") == label
    if was_active:
        cfg["active_account"] = new_label
        _save_web_config(cfg)
        pairing = _get_bot_pairing()
        if pairing:
            _save_pairing_field(pairing["discord_user_id"], "profile_name", new_label)
    return {"success": True, "new_label": new_label}


@app.get("/api/accounts/migration-hint")
async def migration_hint():
    """Returns hint if legacy cookies exist but no accounts are configured"""
    legacy_cookies = (_DATA_DIR / "cookies.jar").exists()
    cfg = _load_web_config()
    has_active = bool(cfg.get("active_account"))
    return {"has_legacy": legacy_cookies and not has_active, "migrated": has_active}


@app.get("/api/push-config")
async def get_push_config(request: Request):
    return _get_push_config()


@app.post("/api/push-config")
async def set_push_config(request: Request):
    body = await request.json()
    cfg = _load_web_config()
    for key in ("push_notifications_enabled", "push_sound_enabled", "campaign_end_alerts_enabled"):
        if key in body:
            cfg[key] = bool(body[key])
    _save_web_config(cfg)
    return {"ok": True}


# ==================== Instance Management ====================

_INSTANCES_FILE = Path(__file__).parent.parent.parent / "instances.json"

def _load_instances_registry() -> dict:
    if _INSTANCES_FILE.exists():
        return json.loads(_INSTANCES_FILE.read_text())
    return {"instances": [
        {"n": 1, "port": 8080, "data_dir": "data", "pm2_name": "twitchdrops", "label": "Account 1"},
        {"n": 2, "port": 8082, "data_dir": "data2", "pm2_name": "twitchdrops2", "label": "Account 2"},
    ]}


@app.get("/api/instances")
async def get_instances():
    registry = _load_instances_registry()
    if len(registry.get("instances", [])) >= 3:
        registry["proxy_warning"] = True
    return registry


@app.post("/api/instances")
async def create_instance():
    import subprocess
    script = str(Path(__file__).parent.parent.parent / "scripts" / "manage_instance.sh")
    result = subprocess.run(["bash", script, "create"], capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr)
    return {"success": True, "instances": _load_instances_registry()["instances"]}


@app.delete("/api/instances/{n}")
async def remove_instance(n: int):
    if n == 1:
        raise HTTPException(status_code=400, detail="Cannot remove main instance")
    import subprocess
    script = str(Path(__file__).parent.parent.parent / "scripts" / "manage_instance.sh")
    result = subprocess.run(["bash", script, "remove", str(n)], capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr)
    return {"success": True, "instances": _load_instances_registry()["instances"]}


@app.get("/api/predictions")
async def get_predictions():
    """Return predictions history."""
    from src.services.prediction_service import _get_predictions_file
    import json as _j
    p = _get_predictions_file()
    try:
        hist = _j.loads(p.read_text()) if p.exists() else []
    except Exception:
        hist = []
    return {"predictions": list(reversed(hist[-200:]))}


@app.get("/api/streamer-overrides")
async def get_streamer_overrides():
    from src.services.prediction_service import _load_overrides
    return {"overrides": _load_overrides()}


class StreamerOverrideRequest(BaseModel):
    channel: str
    overrides: dict


@app.post("/api/streamer-overrides")
async def set_streamer_override(req: StreamerOverrideRequest):
    from src.services.prediction_service import _get_overrides_file
    import json as _j
    p = _get_overrides_file()
    try:
        data = _j.loads(p.read_text()) if p.exists() else {}
    except Exception:
        data = {}
    if req.overrides:
        data[req.channel.lower()] = req.overrides
    else:
        data.pop(req.channel.lower(), None)
    p.write_text(_j.dumps(data, indent=2))
    return {"ok": True}


@app.post("/api/session-report")
async def send_session_report():
    """Build and send session report to Discord."""
    import json as _j
    from datetime import datetime, timezone
    from src.services.prediction_service import _get_predictions_file
    webhook = twitch_client.settings.discord_webhook_points if twitch_client else ""
    if not webhook:
        raise HTTPException(status_code=400, detail="No webhook configured")
    # Points delta
    cp_file = _get_account_data_dir() / "channel_points.json"
    try:
        cp = _j.loads(cp_file.read_text()) if cp_file.exists() else {}
    except Exception:
        cp = {}
    # Drops
    hist_file = _get_account_data_dir() / "drops_history.json"
    try:
        drops = _j.loads(hist_file.read_text()) if hist_file.exists() else []
    except Exception:
        drops = []
    # Predictions
    ph = _get_predictions_file()
    try:
        preds = _j.loads(ph.read_text()) if ph.exists() else []
    except Exception:
        preds = []
    wins = sum(1 for p in preds if p.get("result") == "WIN")
    losses = sum(1 for p in preds if p.get("result") == "LOSE")
    net = sum(p.get("points_won", 0) - p.get("points_bet", 0) for p in preds if p.get("result") in ("WIN", "LOSE"))
    fields = [
        {"name": "📊 Channel Points", "value": "\n".join(f"{ch}: {bal:,}" for ch, bal in list(cp.items())[:10]) or "—", "inline": False},
        {"name": "🎁 Drops Claimed", "value": str(len(drops)), "inline": True},
        {"name": "🎯 Predictions", "value": f"W:{wins} L:{losses} Net:{net:+,}", "inline": True},
    ]
    embed = {"title": "📋 Session Report", "color": 0x9147FF, "fields": fields, "timestamp": datetime.now(timezone.utc).isoformat()}
    import aiohttp
    async with aiohttp.ClientSession() as session:
        await session.post(webhook, json={"embeds": [embed]}, timeout=aiohttp.ClientTimeout(total=10))
    return {"ok": True}


# ==================== Socket.IO Events ====================


def _sio_request_authenticated(environ: dict, auth: dict | None) -> bool:
    """Validate the credentials presented by a Socket.IO client.

    Socket.IO traffic bypasses the HTTP middleware (socketio.ASGIApp routes it
    before FastAPI), so this mirrors the unified gate: bearer token from the
    handshake ``auth={"token": ...}`` payload or the api-token cookie, password
    session via the per-instance session cookie, loopback auto-auth only in
    token-only mode (real ASGI-scope peer, never engineio's fake REMOTE_ADDR),
    and ``TDM_AUTH_DISABLED`` bypassing everything.
    """
    if _auth_disabled():
        return True
    expected = _expected_token()
    presented: str | None = None
    if isinstance(auth, dict):
        candidate = auth.get("token")
        if isinstance(candidate, str):
            presented = candidate
    cookies: dict[str, str] = {}
    raw_cookie = environ.get("HTTP_COOKIE", "")
    for chunk in raw_cookie.split(";"):
        name, _sep, value = chunk.strip().partition("=")
        if name:
            cookies.setdefault(name, value)
    if presented is None:
        presented = cookies.get(COOKIE_NAME)
    if presented and secrets.compare_digest(presented, expected):
        return True
    if _password_configured():
        # Password mode: same rule as HTTP — the password gates loopback too.
        return _password_session_valid(cookies.get(_PW_SESSION_COOKIE, ""))
    # Token-only mode: auto-allow loopback Socket.IO connections so the on-host
    # UI works without manual cookie wiring. Uses the real ASGI scope peer, not
    # engineio's hardcoded REMOTE_ADDR=127.0.0.1 (which would bypass the gate
    # for every proxied client).
    return _host_is_loopback(_sio_client_host(environ))


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
                "watching_channel": (
                    {
                        "id": ch.id,
                        "login": ch._login,
                        "game": ch.game.name if ch.game is not None else "",
                    }
                    if (ch := twitch_client.watching_channel.get_with_default(None)) is not None
                    else None
                ),
                "channel_points_history": _load_channel_points_history(),
                "daily_points": _load_daily_points(),
                "last_mode": _load_web_config().get("last_mode", "drop_mining"),
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


# Serve app.js with no-cache so PWA always gets the latest version
@app.get("/static/app.js")
async def serve_app_js():
    web_dir_path = Path(__file__).parent.parent.parent / "web"
    js_file = web_dir_path / "static" / "app.js"
    return FileResponse(js_file, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Content-Type": "application/javascript",
    })


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

    # uvicorn's legacy `websockets` protocol implementation logs a benign
    # ConnectionClosedError from a shielded background task whenever a browser
    # tab closes/reloads mid-connection. It's not an app error — silence it.
    logging.getLogger("websockets").setLevel(logging.CRITICAL)

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
