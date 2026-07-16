import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestApiToken(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.patcher = patch(
            "src.auth.api_token._token_path", return_value=Path(self.tmpdir) / "api_token"
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        # cleanup
        for f in Path(self.tmpdir).iterdir():
            f.unlink()
        os.rmdir(self.tmpdir)

    def test_create_then_reload_returns_same_token(self):
        from src.auth.api_token import load_or_create_token

        first = load_or_create_token()
        self.assertGreater(len(first), 16)
        second = load_or_create_token()
        self.assertEqual(first, second)

    def test_token_file_is_user_only_readable(self):
        from src.auth.api_token import _token_path, load_or_create_token

        load_or_create_token()
        mode = stat.S_IMODE(_token_path().stat().st_mode)
        # Group/world bits must be off.
        self.assertEqual(mode & 0o077, 0, f"Token file mode {oct(mode)} is too permissive")

    def test_corrupt_file_is_regenerated(self):
        from src.auth.api_token import _token_path, load_or_create_token

        path = _token_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")  # empty == invalid
        token = load_or_create_token()
        self.assertTrue(token)
        self.assertEqual(path.read_text().strip(), token)


class TestBootstrapUrl(unittest.TestCase):
    """The printed bootstrap link must be reachable from a remote browser."""

    def test_prefers_public_trusted_origin_and_direct_endpoint(self):
        from src.auth.api_token import bootstrap_url

        with patch.dict(os.environ, {"TDM_TRUSTED_ORIGINS": "https://tdm.example.xyz"}):
            url = bootstrap_url("0.0.0.0", 8080, "TESTTOKEN")
        # Public origin, https, and the direct cookie-setting endpoint — never
        # the internal bind host or the JS-dependent /?token= path.
        self.assertEqual(url, "https://tdm.example.xyz/api/session/bootstrap?token=TESTTOKEN")

    def test_first_origin_wins_and_trailing_slash_stripped(self):
        from src.auth.api_token import bootstrap_url

        with patch.dict(
            os.environ,
            {"TDM_TRUSTED_ORIGINS": "https://tdm.example.xyz/ , https://second.example"},
        ):
            url = bootstrap_url("0.0.0.0", 8080, "T")
        self.assertEqual(url, "https://tdm.example.xyz/api/session/bootstrap?token=T")

    def test_falls_back_to_local_host_when_no_public_origin(self):
        from src.auth.api_token import bootstrap_url

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TDM_TRUSTED_ORIGINS", None)
            url = bootstrap_url("0.0.0.0", 8080, "T")
        self.assertEqual(url, "http://localhost:8080/api/session/bootstrap?token=T")


if __name__ == "__main__":
    unittest.main()
