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


if __name__ == "__main__":
    unittest.main()
