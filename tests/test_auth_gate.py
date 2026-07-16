"""Security regression tests for the unified auth gate.

These originally targeted the per-endpoint ``Depends(require_auth)`` gate. The
merge replaced that with ``UnifiedAuthMiddleware`` (src/web/app.py), which gates
every request centrally, so the tests now drive the real ASGI stack — FastAPI
app + middleware — end to end instead of calling a dependency function.

The five original security properties are preserved verbatim (same test names):

  * loopback without credentials is allowed (token-only mode),
  * LAN without credentials is rejected,
  * LAN with a valid session cookie is allowed,
  * LAN with an invalid session cookie is rejected,
  * LAN with a valid ``Authorization: Bearer`` header is allowed.

New properties added for the merged architecture:

  * a configured password gates loopback too (reverse-proxy spoofing defense),
  * password sessions are random tokens that round-trip via
    ``_issue_password_session()`` / the ``__tdm_session_<port>`` cookie,
  * the Discord bot token (``X-Bot-Token``) unlocks ``/api/*`` paths ONLY,
  * ``TDM_AUTH_DISABLED`` bypasses the gate,
  * first-run (setup not done) redirects gated paths to ``/__setup``,
  * ``/healthz`` stays reachable without any credential,
  * ``_safe_account_dir()`` rejects path-traversal account labels.

No HTTP client dependency is available in this venv (httpx is not installed),
so a minimal ASGI driver issues requests directly against ``webapp.app`` —
which still exercises the full middleware chain exactly as a socket would.
"""

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from src.auth.api_token import COOKIE_NAME, load_or_create_token
from src.web import app as webapp


class _Response:
    def __init__(self, status_code: int, headers: list, body: bytes):
        self.status_code = status_code
        self.headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in headers}
        self.body = body

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    def text(self):
        return self.body.decode("utf-8", errors="replace")


class AuthGateTestBase(unittest.TestCase):
    """Shared fixture: isolated data dir + ASGI request driver.

    Everything the gate persists (api_token, web_config.json with password
    hash / sessions, discord_bot_token.json) is redirected into a per-test
    temp directory so the repo's real data/ is never touched.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tdm-auth-gate-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

        # api_token lives in the temp dir (same seam the pre-middleware tests used).
        self._start(patch(
            "src.auth.api_token._token_path", return_value=self.tmpdir / "api_token"
        ))
        # Redirect the module-level data paths; every helper reads these
        # globals at call time, so patching the attributes is race-free even
        # though the module was imported (and _DATA_DIR frozen) earlier.
        self._start(patch.object(webapp, "_DATA_DIR", self.tmpdir))
        self._start(patch.object(webapp, "_WEB_CONFIG_FILE", self.tmpdir / "web_config.json"))
        self._start(patch.object(webapp, "_BOT_TOKEN_FILE", self.tmpdir / "discord_bot_token.json"))
        # Neutralize ambient env that would change gate behavior.
        self._start(patch.dict(
            "os.environ", {"TDM_AUTH_DISABLED": "", "WEB_PASSWORD": ""}
        ))
        # Default fixture state: setup wizard completed, token-only mode
        # (no password configured). Individual tests overwrite as needed.
        webapp._save_web_config({"setup_done": True})

    def _start(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---- minimal ASGI driver (integration through UnifiedAuthMiddleware) ----

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        client_host: str = "127.0.0.1",
        cookies: dict | None = None,
        headers: dict | None = None,
    ) -> _Response:
        raw_headers = [(b"host", b"testserver")]
        if cookies:
            cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())
            raw_headers.append((b"cookie", cookie.encode("latin-1")))
        for key, value in (headers or {}).items():
            raw_headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("latin-1"),
            "query_string": b"",
            "root_path": "",
            "headers": raw_headers,
            "client": (client_host, 51515),
            "server": ("testserver", 8080),
        }
        sent_body = False
        status: list = []
        resp_headers: list = []
        chunks: list = []

        async def receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                status.append(message["status"])
                resp_headers.extend(message.get("headers", []))
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        asyncio.run(webapp.app(scope, receive, send))
        self.assertTrue(status, f"no response produced for {method} {path}")
        return _Response(status[0], resp_headers, b"".join(chunks))


# /api/accounts is a real gated endpoint with no dependency on the Twitch
# client globals: authorized requests get a genuine 200 from the route,
# unauthorized ones a 401 from the middleware.
GATED_PATH = "/api/accounts"


class TestTokenOnlyMode(AuthGateTestBase):
    """Token-only mode (no password configured) — the original five properties."""

    def test_loopback_request_without_cookie_is_allowed(self):
        resp = self.request(GATED_PATH, client_host="127.0.0.1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("accounts", resp.json())

    def test_lan_request_without_cookie_is_rejected(self):
        resp = self.request(GATED_PATH, client_host="192.168.1.50")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json(), {"detail": "Authentication required"})

    def test_lan_request_with_valid_cookie_is_allowed(self):
        token = load_or_create_token()
        resp = self.request(
            GATED_PATH, client_host="192.168.1.50", cookies={COOKIE_NAME: token}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("accounts", resp.json())

    def test_lan_request_with_invalid_cookie_is_rejected(self):
        load_or_create_token()  # a real token exists; we present a wrong one
        resp = self.request(
            GATED_PATH, client_host="10.0.0.5", cookies={COOKIE_NAME: "obviously-wrong"}
        )
        self.assertEqual(resp.status_code, 401)

    def test_lan_request_with_valid_bearer_is_allowed(self):
        token = load_or_create_token()
        resp = self.request(
            GATED_PATH,
            client_host="10.0.0.7",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_lan_request_with_invalid_bearer_is_rejected(self):
        load_or_create_token()
        resp = self.request(
            GATED_PATH,
            client_host="10.0.0.7",
            headers={"Authorization": "Bearer forged-token"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_bot_token_unlocks_api_paths_only(self):
        (self.tmpdir / "discord_bot_token.json").write_text(
            json.dumps({"token": "bot-secret-token"})
        )
        allowed = self.request(
            GATED_PATH, client_host="10.0.0.9", headers={"X-Bot-Token": "bot-secret-token"}
        )
        self.assertEqual(allowed.status_code, 200)
        # The same credential must NOT unlock non-API paths.
        blocked = self.request(
            "/some-page", client_host="10.0.0.9", headers={"X-Bot-Token": "bot-secret-token"}
        )
        self.assertEqual(blocked.status_code, 401)
        # And a wrong bot token is rejected even on /api/*.
        forged = self.request(
            GATED_PATH, client_host="10.0.0.9", headers={"X-Bot-Token": "wrong"}
        )
        self.assertEqual(forged.status_code, 401)

    def test_auth_disabled_env_bypasses_gate(self):
        with patch.dict("os.environ", {"TDM_AUTH_DISABLED": "true"}):
            resp = self.request(GATED_PATH, client_host="192.168.1.50")
        self.assertEqual(resp.status_code, 200)

    def test_healthz_is_public(self):
        resp = self.request("/healthz", client_host="192.168.1.50")
        self.assertEqual(resp.status_code, 200)

    def test_first_run_redirects_gated_paths_to_setup(self):
        webapp._save_web_config({})  # setup NOT done
        resp = self.request(GATED_PATH, client_host="192.168.1.50")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("location"), "/__setup")


class TestPasswordMode(AuthGateTestBase):
    """A configured password gates everyone — loopback included."""

    PASSWORD = "hunter2-correct-horse"

    def setUp(self):
        super().setUp()
        webapp._save_web_config({
            "setup_done": True,
            "password_hash": webapp._hash_password(self.PASSWORD),
        })

    def test_password_mode_gates_loopback(self):
        # Loopback auto-auth must be OFF once a password exists: a same-host
        # reverse proxy makes every remote client look like loopback, so a
        # loopback bypass would silently defeat the operator's password.
        resp = self.request(GATED_PATH, client_host="127.0.0.1")
        self.assertEqual(resp.status_code, 401)

    def test_valid_password_session_cookie_is_allowed(self):
        session_token = webapp._issue_password_session()
        # The issued token is stored hashed, never verbatim.
        stored = webapp._load_web_config().get("sessions", [])
        self.assertTrue(stored)
        self.assertNotIn(session_token, json.dumps(stored))
        resp = self.request(
            GATED_PATH,
            client_host="192.168.1.50",
            cookies={webapp._PW_SESSION_COOKIE: session_token},
        )
        self.assertEqual(resp.status_code, 200)

    def test_invalid_password_session_cookie_is_rejected(self):
        webapp._issue_password_session()  # a real session exists
        resp = self.request(
            GATED_PATH,
            client_host="127.0.0.1",
            cookies={webapp._PW_SESSION_COOKIE: "forged-session-token"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_password_mode_still_accepts_bearer_token(self):
        # Credential order: the machine token outranks the password gate, so
        # API scripts keep working after the operator sets a password.
        token = load_or_create_token()
        resp = self.request(
            GATED_PATH,
            client_host="192.168.1.50",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_unauthorized_navigation_gets_login_page(self):
        resp = self.request("/", client_host="192.168.1.50")
        self.assertEqual(resp.status_code, 401)
        self.assertIn('action="/__auth_login"', resp.text())


class TestSafeAccountDir(AuthGateTestBase):
    """Account labels are directory NAMES — traversal attempts must 400."""

    def test_traversal_labels_are_rejected(self):
        for label in ("../x", "a/b", ".", "", "a\\b", "..", "a.b", "x" * 65, "a\0b"):
            with self.subTest(label=label):
                with self.assertRaises(HTTPException) as ctx:
                    webapp._safe_account_dir(label)
                self.assertEqual(ctx.exception.status_code, 400)

    def test_valid_label_resolves_under_accounts_root(self):
        accounts_root = (self.tmpdir / "accounts").resolve()
        result = webapp._safe_account_dir("main")
        self.assertEqual(result.parent, accounts_root)
        self.assertEqual(result.name, "main")


if __name__ == "__main__":
    unittest.main()
