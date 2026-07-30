"""The cookie jar is written atomically, and a failed write never stops mining.

``aiohttp.CookieJar.save()`` is ``path.open("wb")`` plus ``pickle.dump``, so it
truncates the jar BEFORE the new bytes land. A SIGKILL, a power cut or a full
disk in between left a truncated ``cookies.jar``; the next start could not
unpickle it, came up logged out, and a logged-out miner mines nothing. That is
the durability class ``json_save`` closed for settings.json, reached from the
credential side instead - so ``CookieJarStore`` gives the jar the same
mkstemp/fsync/``os.replace`` treatment, separately, because the payload is
aiohttp's pickle format rather than JSON.

Two further properties are pinned here because they are what makes the atomic
write worth having:

* the 0o600 mode now lands on the temp file BEFORE the auth-token is written to
  it, replacing a post-hoc ``chmod`` that left the token briefly world-readable;
* a failed jar write is reported and does NOT propagate. It used to abort
  ``_AuthState._validate()``, and every GQL request waits on that through
  ``Twitch.get_auth()``, so a full disk at login stopped mining entirely over a
  file only needed at the next start. Carrying on is only safe BECAUSE the write
  is atomic: the previous jar is left whole.

Every jar has to be built inside a running loop - ``aiohttp.CookieJar.__init__``
calls ``asyncio.get_running_loop()`` - hence ``IsolatedAsyncioTestCase``
throughout.
"""

import asyncio
import os
import stat
import time
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from yarl import URL

from src.api import http_client as http_client_module
from src.api.http_client import CookieJarStore, HTTPClient
from src.auth.auth_state import _AuthState
from src.config.client_info import ClientType


TOKEN = "hunter2-auth-token"
COOKIE_URL = URL("https://www.twitch.tv")


class CookieJarTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "cookies.jar"
        self.store = CookieJarStore(self.path)

    def jar(self, *, with_token: bool = True) -> aiohttp.CookieJar:
        jar = aiohttp.CookieJar()
        if with_token:
            cookie = SimpleCookie()
            cookie["auth-token"] = TOKEN
            jar.update_cookies(cookie, COOKIE_URL)
        return jar

    def strays(self) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir() if p != self.path)

    def temp_named(self, *, age: float, target: str = "cookies.jar") -> Path:
        path = self.dir / f".{target}.abc123{http_client_module._TEMP_SUFFIX}"
        path.write_bytes(b"half-written")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def token_in(self, jar: aiohttp.CookieJar) -> str | None:
        morsel = jar.filter_cookies(COOKIE_URL).get("auth-token")
        return None if morsel is None else morsel.value


class TestCookieJarRoundTrip(CookieJarTestBase):
    async def test_a_saved_jar_loads_back_and_leaves_no_temp_file(self):
        self.store.save(self.jar())

        restored = aiohttp.CookieJar()
        self.assertTrue(self.store.load(restored))
        self.assertEqual(self.token_in(restored), TOKEN)
        self.assertEqual(self.strays(), [])

    async def test_the_jar_is_created_private(self):
        self.store.save(self.jar())

        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    async def test_a_pre_existing_loose_mode_jar_is_tightened(self):
        # A jar written by a version that predates this class, or by hand.
        # os.replace carries the temp file's 0o600 onto the target.
        self.path.write_bytes(b"old")
        os.chmod(self.path, 0o644)

        self.store.save(self.jar())

        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    async def test_the_temp_file_is_private_while_it_holds_the_token(self):
        # The window the old post-hoc chmod left open: the token was on disk
        # world-readable for the length of one write. mkstemp creates the temp
        # 0o600 whatever the umask, so the mode is right BEFORE the write.
        seen: list[int] = []
        real_save = aiohttp.CookieJar.save

        def watching_save(jar_self, file_path):
            seen.append(stat.S_IMODE(Path(file_path).stat().st_mode))
            return real_save(jar_self, file_path)

        previous = os.umask(0o000)
        self.addCleanup(os.umask, previous)
        with patch.object(aiohttp.CookieJar, "save", watching_save):
            self.store.save(self.jar())

        self.assertEqual(seen, [0o600])

    async def test_the_temp_file_lives_in_the_jars_own_directory(self):
        # A cross-device rename is not atomic and would raise; the temp has to
        # share a filesystem with the target.
        seen: list[Path] = []
        real_save = aiohttp.CookieJar.save

        def watching_save(jar_self, file_path):
            seen.append(Path(file_path))
            return real_save(jar_self, file_path)

        with patch.object(aiohttp.CookieJar, "save", watching_save):
            self.store.save(self.jar())

        self.assertEqual(seen[0].parent, self.path.parent)
        self.assertTrue(seen[0].name.startswith(".cookies.jar."))


class TestAnInterruptedSaveKeepsThePreviousJar(CookieJarTestBase):
    """The regression: the previous jar must survive any failure mid-write.

    Reproduced against the old code for the record: interrupting
    ``CookieJar.save()`` took a 291-byte jar to 0 bytes, and the next ``load()``
    raised ``EOFError: Ran out of input``.
    """

    async def previous_jar(self) -> bytes:
        self.store.save(self.jar())
        return self.path.read_bytes()

    async def test_a_pickling_failure_leaves_the_jar_byte_identical(self):
        before = await self.previous_jar()

        with (
            patch.object(aiohttp.CookieJar, "save", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            self.store.save(self.jar())

        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.strays(), [])

    async def test_a_failing_rename_leaves_the_jar_byte_identical(self):
        # The guard for the atomicity itself: everything up to the replace
        # succeeded, and the target must still be the previous complete jar.
        before = await self.previous_jar()

        with (
            patch.object(http_client_module.os, "replace", side_effect=OSError("read-only")),
            self.assertRaises(OSError),
        ):
            self.store.save(self.jar())

        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.strays(), [])

    async def test_the_target_is_never_truncated_while_the_new_jar_is_written(self):
        before = await self.previous_jar()
        during: list[bytes] = []
        real_save = aiohttp.CookieJar.save

        def watching_save(jar_self, file_path):
            during.append(self.path.read_bytes())
            return real_save(jar_self, file_path)

        with patch.object(aiohttp.CookieJar, "save", watching_save):
            self.store.save(self.jar())

        self.assertEqual(during, [before])


class TestAnUnreadableJarDegradesLoudly(CookieJarTestBase):
    """A jar that cannot be read costs a re-login, and says so.

    ``CookieJar.load()`` assigns ``self._cookies`` only after ``pickle.load``
    returns, so the jar is left exactly as it came in - empty - and the miner
    asks for a fresh device-code login. What it used to do is that SILENTLY,
    which from the outside is indistinguishable from Twitch revoking the
    session, so the operator had nothing to look at.

    Deliberately NOT quarantined the way ``json_load`` preserves settings.json:
    that file holds choices which cannot be derived again, while the jar holds a
    credential regenerated by logging in. Keeping ``.corrupt`` copies of an
    auth-token buys nothing and adds exposure.
    """

    async def test_a_truncated_jar_reports_and_clears(self):
        self.store.save(self.jar())
        self.path.write_bytes(self.path.read_bytes()[:10])

        restored = aiohttp.CookieJar()
        with self.assertLogs("TwitchDrops", level="WARNING") as captured:
            self.assertFalse(self.store.load(restored))

        self.assertIsNone(self.token_in(restored))
        self.assertIn(str(self.path), captured.output[0])

    async def test_garbage_bytes_report_and_clear(self):
        self.path.write_bytes(b"\xff\xfe\x00not a pickle")

        restored = aiohttp.CookieJar()
        with self.assertLogs("TwitchDrops", level="WARNING"):
            self.assertFalse(self.store.load(restored))

        self.assertIsNone(self.token_in(restored))

    async def test_a_missing_jar_is_silent(self):
        # A fresh install is not a fault, and a WARNING on every first start is
        # how an operator learns to ignore this line.
        restored = aiohttp.CookieJar()

        with self.assertNoLogs("TwitchDrops", level="WARNING"):
            self.assertFalse(self.store.load(restored))

    async def test_the_unreadable_jar_is_left_where_it_is(self):
        self.path.write_bytes(b"not a pickle")

        with self.assertLogs("TwitchDrops", level="WARNING"):
            self.store.load(aiohttp.CookieJar())

        self.assertTrue(self.path.exists())
        self.assertEqual(self.strays(), [])


class TestStaleTempSweep(CookieJarTestBase):
    """The jar's own abandoned temps are collected, at startup as well.

    ``json_save`` sweeps only after a successful write, which is enough for a
    file written constantly. The jar is written on login and on clean shutdown
    only, so a container that is always SIGKILLed rarely reaches a successful
    save - but it passes through ``load()`` on every start, which makes startup
    the reliable collection point.
    """

    async def test_a_save_reaps_an_abandoned_temp(self):
        stale = self.temp_named(age=http_client_module._STALE_TEMP_AGE * 2)

        self.store.save(self.jar())

        self.assertFalse(stale.exists())
        self.assertEqual(self.strays(), [])

    async def test_a_load_reaps_an_abandoned_temp(self):
        stale = self.temp_named(age=http_client_module._STALE_TEMP_AGE * 2)

        self.store.load(aiohttp.CookieJar())

        self.assertFalse(stale.exists())

    async def test_a_concurrent_writes_fresh_temp_is_left_alone(self):
        fresh = self.temp_named(age=0)

        self.store.save(self.jar())

        self.assertTrue(fresh.exists())

    async def test_another_targets_temp_is_left_alone(self):
        other = self.temp_named(age=http_client_module._STALE_TEMP_AGE * 2, target="settings.json")

        self.store.save(self.jar())

        self.assertTrue(other.exists())

    async def test_a_sweep_that_cannot_delete_does_not_fail_the_save(self):
        self.temp_named(age=http_client_module._STALE_TEMP_AGE * 2)

        with patch.object(Path, "unlink", side_effect=OSError("busy")):
            self.store.save(self.jar())

        self.assertEqual(self.token_in(self._loaded()), TOKEN)

    def _loaded(self) -> aiohttp.CookieJar:
        # Unpickles a jar this test just wrote into its own temp directory, so
        # there is no untrusted input here; the production read carries the same
        # note (CookieJarStore.load).
        jar = aiohttp.CookieJar()
        jar.load(self.path)
        return jar


class TestAFailedJarWriteDoesNotStopMining(CookieJarTestBase):
    """The second silent-stop class this lane closed, and the louder one.

    A jar write failure used to propagate out of both call sites.

    * In ``_AuthState._validate()`` the raise skipped ``self._logged_in.set()``,
      and ``Twitch.get_auth()`` gates EVERY GQL request on that event. A full
      disk or a read-only data dir at login therefore left the miner mining
      nothing, forever, over a file it does not need until the next start.
    * In ``HTTPClient.close()`` it skipped ``await self._session.close()`` and
      everything after it in ``Twitch.close()``, so shutdown state was never
      cleared and the aiohttp session leaked.

    Both now log at ERROR and carry on, which is only the right answer because
    the write is atomic: the previous jar is left whole either way.
    """

    def failing_store(self) -> MagicMock:
        store = MagicMock(spec=CookieJarStore)
        store.path = self.path
        store.save.side_effect = OSError(28, "No space left on device")
        return store

    async def test_close_still_closes_the_session_and_reports(self):
        session = aiohttp.ClientSession(cookie_jar=self.jar())
        self.addAsyncCleanup(session.close)
        # Built without __init__ on purpose: close() reads only these two
        # attributes, and a real HTTPClient would drag in Settings and the GUI.
        client = HTTPClient.__new__(HTTPClient)
        client._session = session
        client._cookie_store = self.failing_store()

        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            await client.close()

        self.assertTrue(session.closed)
        self.assertIsNone(client._session)
        self.assertEqual(len(captured.records), 1)
        self.assertIn(str(self.path), captured.output[0])

    async def test_a_login_whose_jar_write_fails_still_reports_logged_in(self):
        client_info = ClientType.ANDROID_APP
        jar = aiohttp.CookieJar()
        cookie = SimpleCookie()
        cookie["auth-token"] = TOKEN
        jar.update_cookies(cookie, client_info.CLIENT_URL)
        session = aiohttp.ClientSession(cookie_jar=jar)
        self.addAsyncCleanup(session.close)

        twitch = MagicMock()
        twitch.get_session = AsyncMock(return_value=session)
        twitch._client_type = client_info
        twitch.request = _FakeRequest(
            {"client_id": client_info.CLIENT_ID, "user_id": "42", "login": "arend"}
        )

        auth = _AuthState.__new__(_AuthState)
        auth._twitch = twitch
        auth._logged_in = asyncio.Event()
        auth._cookie_store = self.failing_store()
        auth.session_id = "0" * 16
        auth.device_id = "device"

        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            await auth._validate()

        # The session is live, so mining must go on.
        self.assertTrue(auth._logged_in.is_set())
        self.assertEqual(auth.user_id, 42)
        self.assertEqual(auth.user_login, "arend")
        self.assertEqual(len(captured.records), 1)
        self.assertIn(str(self.path), captured.output[0])

    async def test_a_login_whose_jar_write_succeeds_persists_the_token(self):
        # The other half: the failure path must not have cost the feature.
        client_info = ClientType.ANDROID_APP
        jar = aiohttp.CookieJar()
        cookie = SimpleCookie()
        cookie["auth-token"] = TOKEN
        jar.update_cookies(cookie, client_info.CLIENT_URL)
        session = aiohttp.ClientSession(cookie_jar=jar)
        self.addAsyncCleanup(session.close)

        twitch = MagicMock()
        twitch.get_session = AsyncMock(return_value=session)
        twitch._client_type = client_info
        twitch.request = _FakeRequest(
            {"client_id": client_info.CLIENT_ID, "user_id": "42", "login": "arend"}
        )

        auth = _AuthState.__new__(_AuthState)
        auth._twitch = twitch
        auth._logged_in = asyncio.Event()
        auth._cookie_store = self.store
        auth.session_id = "0" * 16
        auth.device_id = "device"

        await auth._validate()

        restored = aiohttp.CookieJar()
        self.assertTrue(self.store.load(restored))
        self.assertEqual(restored.filter_cookies(client_info.CLIENT_URL)["auth-token"].value, TOKEN)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)


class _FakeRequest:
    """Stands in for ``Twitch.request`` - an async context manager per call."""

    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self._status = status

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        response = MagicMock()
        response.status = self._status
        response.json = AsyncMock(return_value=self._payload)
        return response

    async def __aexit__(self, *exc_info):
        return False


if __name__ == "__main__":
    unittest.main()
