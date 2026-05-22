import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException


class TestAuthGate(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.patcher = patch(
            "src.auth.api_token._token_path", return_value=Path(self.tmpdir) / "api_token"
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        for f in Path(self.tmpdir).iterdir():
            f.unlink()
        Path(self.tmpdir).rmdir()

    def _request(self, *, cookies=None, headers=None, client_host="127.0.0.1"):
        req = MagicMock()
        req.cookies = cookies or {}
        req.headers = headers or {}
        req.client.host = client_host
        return req

    def test_loopback_request_without_cookie_is_allowed(self):
        from src.web.app import require_auth

        req = self._request(client_host="127.0.0.1")
        # Should not raise.
        require_auth(req)

    def test_lan_request_without_cookie_is_rejected(self):
        from src.web.app import require_auth

        req = self._request(client_host="192.168.1.50")
        with self.assertRaises(HTTPException) as ctx:
            require_auth(req)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_lan_request_with_valid_cookie_is_allowed(self):
        from src.auth.api_token import COOKIE_NAME, load_or_create_token
        from src.web.app import require_auth

        token = load_or_create_token()
        req = self._request(cookies={COOKIE_NAME: token}, client_host="192.168.1.50")
        require_auth(req)

    def test_lan_request_with_invalid_cookie_is_rejected(self):
        from src.auth.api_token import COOKIE_NAME
        from src.web.app import require_auth

        req = self._request(cookies={COOKIE_NAME: "obviously-wrong"}, client_host="10.0.0.5")
        with self.assertRaises(HTTPException) as ctx:
            require_auth(req)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_lan_request_with_valid_bearer_is_allowed(self):
        from src.auth.api_token import load_or_create_token
        from src.web.app import require_auth

        token = load_or_create_token()
        req = self._request(headers={"Authorization": f"Bearer {token}"}, client_host="10.0.0.7")
        require_auth(req)


if __name__ == "__main__":
    unittest.main()
