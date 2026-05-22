"""Bearer/cookie token used to authenticate browser+API access to the local web UI.

Generated once on first launch and stored under ``DATA_DIR/api_token`` with
``0o600`` permissions. The token gates every state-changing endpoint and the
Socket.IO connect handshake so a LAN attacker cannot reconfigure the proxy,
close the miner, or read settings without first knowing the secret.

UX model:
  * On first launch the bootstrap URL (containing the token in a query string)
    is printed to stdout. The user opens that URL once; the server validates
    the token and sets an httpOnly cookie. Subsequent visits do not need the
    URL parameter.
  * Loopback requests (127.0.0.1 / ::1) that hit the bootstrap endpoint
    without a token are auto-bootstrapped — single-user, on-host installs
    therefore see no UX change beyond the cookie.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
from pathlib import Path
from typing import Final


logger = logging.getLogger("TwitchDrops")


# Cookie name and lifetime are deliberately conservative: httpOnly so JS can
# never read it (defense against XSS exfil), SameSite=Lax so cross-site POSTs
# (e.g. ``<form action="/api/close">`` from a malicious page) are not sent.
COOKIE_NAME: Final[str] = "tdm_session"
COOKIE_MAX_AGE: Final[int] = 60 * 60 * 24 * 30  # 30 days


def _token_path() -> Path:
    # Resolved lazily so test suites can swap DATA_DIR before import.
    from src.config import DATA_DIR

    return Path(DATA_DIR) / "api_token"


def load_or_create_token() -> str:
    """Return the persistent API token, creating it on first run.

    The token is 32 random bytes encoded as URL-safe base64 (≈ 43 chars).
    """
    path = _token_path()
    if path.exists():
        try:
            token = path.read_text(encoding="utf-8").strip()
            if token:
                _harden_perms(path)
                return token
        except OSError as exc:
            logger.warning(f"Could not read API token at {path}: {exc}; regenerating")

    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_CREAT|O_EXCL prevents racing another process; if it loses the race we
    # re-read on the next iteration.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(token)
    except BaseException:
        # Best-effort cleanup if write failed mid-flight.
        with contextlib.suppress(OSError):
            os.close(fd)
        raise
    return token


def _harden_perms(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def bootstrap_url(host: str, port: int, token: str) -> str:
    """URL the user opens once to install the session cookie."""
    # Force a literal IP rather than 0.0.0.0 in the printed hint — the user
    # cannot navigate to 0.0.0.0 from a browser on a remote machine.
    display_host = "localhost" if host in ("0.0.0.0", "::", "") else host
    return f"http://{display_host}:{port}/?token={token}"
