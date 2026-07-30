"""Tests for the unauthenticated readiness probe ``GET /api/health/mining``.

``/healthz`` only proves the web server answers. This endpoint reports whether
the miner has anything to mine at all, so an uptime monitor can alert on the
silent-failure state that once cost five days of mining (an emptied
``games_to_watch``).

Ten properties matter and all ten are exercised through the real ASGI stack
(FastAPI app + UnifiedAuthMiddleware), reusing the driver from
``tests/test_auth_gate.py``:

  * it is public — a monitor has no session cookie, and must still get 200,
  * it is public at ONE exact path: the same URL with a trailing slash is gated,
    so a monitor aimed at it stays green through any outage,
  * it always answers 200 and never raises, even mid-boot or with a client
    whose state reads blow up,
  * ``ok``/``watchlist_empty`` tell the truth about the watch list,
  * ``ok``/``benefits_disabled`` tell the truth about the OTHER road to the same
    standstill: a ``mining_benefits`` selection with every type turned off makes
    every drop unwanted, so nothing is mined next to a full watch list,
  * ``ok``/``login``/``login_pending`` tell the truth about being able to mine at
    all: a miner blocked in ``_oauth_login``'s device-code poll is waiting on a
    human, has no watch task at all, and used to answer ``ok: true``,
  * ``ok``/``watch_stalled``/``last_watch_age_seconds`` tell the truth about
    PROGRESS rather than configuration — every other field stays valid while a
    dead proxy or a rejected token earns nothing for days,
  * ``state``/``mining`` tell the truth about MINING, which is not the same
    question as "is a channel open" — an idle-watching miner reports ``idle``,
  * being public, it discloses nothing but booleans, counts, elapsed seconds, a
    coarse ``state`` and the login — never the watched channel, the targeted
    game or the pending device code,
  * the body the operator is told to keyword-match is the body that goes out on
    the wire, and README.md documents the key set this file pins.
"""

import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.config import WATCH_INTERVAL, State
from src.config import settings as settings_module
from src.config.settings import Settings, default_settings
from src.web import app as webapp
from tests.test_auth_gate import AuthGateTestBase


HEALTH_PATH = "/api/health/mining"

_REPO_ROOT = Path(__file__).resolve().parents[1]
README = _REPO_ROOT / "README.md"

# The full contract an external monitor is allowed to depend on. Anything added
# here is a new public field; anything removed breaks somebody's alert rule.
DOCUMENTED_KEYS = {
    "ok",
    "watchlist_empty",
    "benefits_disabled",
    "games_to_watch_count",
    "wanted_games_count",
    "state",
    "mining",
    "watch_stalled",
    "last_watch_age_seconds",
    "uptime_seconds",
    "login",
    "login_pending",
}

# The complete coarse-state vocabulary. Anything outside this set is either a
# leak (the raw status line) or an undocumented state nobody's alert rule knows.
DOCUMENTED_STATES = {"starting", "idle", "watching", "paused"}

# What a healthy install has: every benefit type enabled. Taken from the real
# defaults so a new benefit type cannot leave these fakes reporting a selection
# the application would never produce.
ALL_BENEFITS = dict(default_settings["mining_benefits"])

# What a healthy install has: an account name. Same reasoning as ALL_BENEFITS —
# a test that says nothing about login must not silently assert the logged-out
# outage, which now drops ``ok`` on its own.
LOGGED_IN_AS = "arend"

# "Use the healthy default" for _FakeTwitch.last_watch_ok. A plain None would
# mean "never watched", which is a different (and now meaningful) state.
_FRESH = object()


def _seconds_ago(seconds: float) -> datetime:
    """A timezone-aware stamp ``seconds`` in the past, like the miner writes."""
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


class _FakeSettings:
    def __init__(self, games_to_watch, mining_benefits=None):
        self.games_to_watch = games_to_watch
        # Defaults to the healthy selection, so a test that says nothing about
        # benefits is not silently asserting the outage state: every "ok is
        # true" case in this file would otherwise be measuring the wrong thing.
        self.mining_benefits = ALL_BENEFITS if mining_benefits is None else mining_benefits


class _FakeWatchingChannel:
    def __init__(self, channel):
        self._channel = channel

    def get_with_default(self, default):
        return self._channel if self._channel is not None else default


class _FakeTwitch:
    """Only the attributes the probe reads — deliberately not a MagicMock.

    A MagicMock would make every read succeed with a truthy sentinel and hide
    the ``or []`` / exception fallbacks the probe relies on.

    ``state`` is the miner's own state-machine value, which is what separates
    real mining from an idle watch (see ``_MiningHealthProbe._idle_watching``).
    It defaults to ``CHANNEL_SWITCH`` because that is the state a mining miner
    sits in, so a fake that holds a channel handle reads as mining unless a test
    says otherwise.

    ``last_watch_ok`` defaults to "just now" for the same reason ``mining_benefits``
    defaults to the full selection: a test that says nothing about progress must
    not silently assert a stall.
    """

    def __init__(
        self,
        games_to_watch,
        *,
        mining_benefits=None,
        wanted_games=(),
        watching=None,
        paused=False,
        state=State.CHANNEL_SWITCH,
        last_watch_ok=_FRESH,
    ):
        self.settings = _FakeSettings(games_to_watch, mining_benefits)
        self.wanted_games = list(wanted_games)
        self.watching_channel = _FakeWatchingChannel(watching)
        self._paused = paused
        self._state = state
        self.last_watch_ok = (
            datetime.now(timezone.utc) if last_watch_ok is _FRESH else last_watch_ok
        )

    def is_paused(self):
        return self._paused


class _StatelessTwitch(_FakeTwitch):
    """A client whose ``_state`` cannot be read at all.

    The probe must say "could not tell" rather than pick a state, because the
    only wrong answer here is the reassuring one.
    """

    def __init__(self, games_to_watch, **kwargs):
        super().__init__(games_to_watch, **kwargs)
        del self._state


class _FakeStatus:
    def __init__(self, text):
        self._text = text

    def get(self):
        return self._text


class _FakeLogin:
    """Mirrors ``LoginFormManager.get_status``, ``oauth_pending`` key included.

    The real manager adds that key for exactly as long as ``ask_enter_code`` is
    blocked waiting for a human to type the device code, and omits it otherwise
    (``src/web/managers/login.py:104-108``) — so its ABSENCE is the signal, and
    the fake has to model absence rather than a ``None`` value.
    """

    def __init__(self, user_login, oauth_pending=None):
        self._user_login = user_login
        self._oauth_pending = oauth_pending

    def get_status(self):
        status = {"user_login": self._user_login, "status": "Logged in", "user_id": 1}
        if self._oauth_pending:
            status["oauth_pending"] = self._oauth_pending
        return status


class _FakeGui:
    def __init__(self, status="Idle", user_login=LOGGED_IN_AS, oauth_pending=None):
        self.status = _FakeStatus(status)
        self.login = _FakeLogin(user_login, oauth_pending)


class _ExplodingTwitch:
    """Every state read raises — the probe must still answer 200."""

    @property
    def settings(self):
        raise RuntimeError("client not ready")

    @property
    def wanted_games(self):
        raise RuntimeError("client not ready")


class _NoWantedGamesTwitch:
    """``settings`` reads fine, ``wanted_games`` raises.

    The granularity case: one raising attribute used to abandon the whole
    ``try`` block, so the fields that HAD been read successfully were reported
    as zero — while ``ok`` stayed true and the monitor saw a healthy miner.
    """

    def __init__(self, games_to_watch):
        self.settings = _FakeSettings(games_to_watch)
        self.watching_channel = _FakeWatchingChannel(None)
        self.last_watch_ok = datetime.now(timezone.utc)

    @property
    def wanted_games(self):
        raise RuntimeError("inventory not fetched yet")

    def is_paused(self):
        return False


class _NoSettingsTwitch:
    """The mirror image: ``wanted_games`` reads fine, ``settings`` raises."""

    def __init__(self, wanted_games=()):
        self.wanted_games = list(wanted_games)
        self.watching_channel = _FakeWatchingChannel(None)
        self.last_watch_ok = datetime.now(timezone.utc)

    @property
    def settings(self):
        raise RuntimeError("settings not loaded yet")

    def is_paused(self):
        return False


class _ExplodingGui:
    @property
    def status(self):
        raise RuntimeError("gui not ready")

    @property
    def login(self):
        raise RuntimeError("gui not ready")


class HealthMiningTestBase(AuthGateTestBase):
    """Isolated data dir + ASGI driver, with the app globals swappable."""

    def use(self, twitch, gui):
        self._start(patch.object(webapp, "twitch_client", twitch))
        self._start(patch.object(webapp, "gui_manager", gui))

    def probe(self, client_host="192.168.1.50"):
        response = self.request(HEALTH_PATH, client_host=client_host)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        # Every scenario in this file asserts the contract, not just the ones
        # that care about it: a stray key or an invented state is a break for
        # the docs, the tests and the external watchdog alike.
        self.assertEqual(set(body), DOCUMENTED_KEYS)
        self.assertIn(body["state"], DOCUMENTED_STATES)
        self.assertIs(body["mining"], body["state"] == "watching")
        # Elapsed-second fields are whole, non-negative numbers or (for an age
        # that does not exist yet) null. A monitor compares them numerically.
        self.assertIsInstance(body["uptime_seconds"], int)
        self.assertGreaterEqual(body["uptime_seconds"], 0)
        if body["last_watch_age_seconds"] is not None:
            self.assertIsInstance(body["last_watch_age_seconds"], int)
            self.assertGreaterEqual(body["last_watch_age_seconds"], 0)
        # ok is the single keyword the watchdog matches, so it must never stay
        # true next to a field that says mining cannot be happening.
        if body["ok"]:
            self.assertIs(body["watch_stalled"], False)
            self.assertIs(body["login_pending"], False)
            self.assertIsNotNone(body["login"])
        return body

    def probe_text(self, client_host="192.168.1.50"):
        """The raw response body — for asserting what is NOT in it."""
        return self.request(HEALTH_PATH, client_host=client_host).text()

    def probe_bytes(self, client_host="192.168.1.50"):
        """The response body exactly as it leaves the socket."""
        return self.request(HEALTH_PATH, client_host=client_host).body


class TestHealthMiningIsPublic(HealthMiningTestBase):
    def test_reachable_from_lan_without_any_credential(self):
        # A LAN uptime monitor has no cookie and no bearer token; the same
        # request against a gated path is a 401 (see test_auth_gate).
        self.use(_FakeTwitch(["Overwatch"]), _FakeGui())
        self.probe()  # asserts the documented key set on every call

    def test_listed_in_the_public_path_allowlist(self):
        self.assertIn(HEALTH_PATH, webapp._PUBLIC_PATHS)

    def test_reachable_before_the_setup_wizard_is_done(self):
        # Gated paths 302 to /__setup on first run; a probe must not.
        webapp._save_web_config({})
        self.use(_FakeTwitch(["Overwatch"]), _FakeGui())
        self.probe()

    def test_reachable_with_a_password_configured(self):
        # A configured password gates loopback too — but not this probe.
        webapp._save_web_config({
            "setup_done": True,
            "password_hash": webapp._hash_password("hunter2-correct-horse"),
        })
        self.use(_FakeTwitch(["Overwatch"]), _FakeGui())
        self.probe()


class TestHealthMiningBody(HealthMiningTestBase):
    def test_empty_watch_list_reports_not_ok(self):
        # THE regression this endpoint exists for: nothing to mine.
        self.use(_FakeTwitch([]), _FakeGui(status="Idle"))
        body = self.probe()
        self.assertIs(body["ok"], False)
        self.assertIs(body["watchlist_empty"], True)
        self.assertEqual(body["games_to_watch_count"], 0)
        # The two roads to "nothing to mine" are reported separately, because
        # they need different fixes: this one needs games added.
        self.assertIs(body["benefits_disabled"], False)

    def test_populated_watch_list_reports_ok(self):
        self.use(
            _FakeTwitch(
                ["Overwatch", "Rust"],
                wanted_games=["Overwatch"],
                watching=object(),
            ),
            _FakeGui(status="Watching", user_login="arend"),
        )
        body = self.probe()
        self.assertIs(body["ok"], True)
        self.assertIs(body["watchlist_empty"], False)
        self.assertEqual(body["games_to_watch_count"], 2)
        self.assertEqual(body["wanted_games_count"], 1)
        self.assertEqual(body["state"], "watching")
        self.assertEqual(body["login"], "arend")
        self.assertIs(body["mining"], True)
        self.assertIs(body["benefits_disabled"], False)

    def test_paused_client_is_not_mining_but_stays_ok(self):
        # Paused is a deliberate operator state, not a broken watch list.
        self.use(
            _FakeTwitch(["Overwatch"], watching=object(), paused=True),
            _FakeGui(status="Paused"),
        )
        body = self.probe()
        self.assertEqual(body["state"], "paused")
        self.assertIs(body["mining"], False)
        self.assertIs(body["ok"], True)

    def test_running_but_not_watching_is_idle(self):
        self.use(_FakeTwitch(["Overwatch"], watching=None), _FakeGui(status="💤 Idle: somechannel"))
        body = self.probe()
        self.assertEqual(body["state"], "idle")
        self.assertIs(body["mining"], False)
        # Nothing is wrong with the watch list, so the monitor must not alert.
        self.assertIs(body["ok"], True)

    def test_watch_list_of_none_counts_as_empty(self):
        # settings.games_to_watch can legitimately be missing/None mid-boot.
        self.use(_FakeTwitch(None), _FakeGui())
        body = self.probe()
        self.assertIs(body["watchlist_empty"], True)
        self.assertIs(body["ok"], False)

    def test_uninitialized_app_degrades_instead_of_failing(self):
        self.use(None, None)
        body = self.probe()
        self.assertIs(body["ok"], False)
        self.assertIs(body["watchlist_empty"], True)
        self.assertIs(body["mining"], False)
        self.assertEqual(body["state"], "starting")
        self.assertIsNone(body["login"])

    def test_raising_state_reads_never_produce_a_5xx(self):
        # The probe must never be the thing that breaks; a 5xx here would make
        # the monitor cry wolf about the web server instead of the watch list.
        self.use(_ExplodingTwitch(), _ExplodingGui())
        body = self.probe()
        self.assertIs(body["ok"], False)
        # Unreadable state is reported as "not up yet", never guessed as idle.
        self.assertEqual(body["state"], "starting")
        self.assertIsNone(body["login"])


class TestHealthMiningIdleWatchIsNotMining(HealthMiningTestBase):
    """A held channel handle is not proof of mining, and must not read as it.

    The IDLE branch of the miner's state machine calls ``watch()`` too, to keep a
    configured idle channel open for channel points and predictions while there
    is nothing minable at all (src/core/client.py:328-351). The probe used to
    look at ``watching_channel`` alone, so that miner answered
    ``state: "watching"``, ``mining: true``, with ``wanted_games_count: 0``
    sitting right next to it. An operator watching ``mining`` for a stall would
    have been told everything was fine for as long as the idle watch lasted,
    which is precisely the silent failure this endpoint exists to surface.

    The state machine is the marker: it stays in ``State.IDLE`` for the whole
    idle watch, while genuine mining runs from ``State.CHANNEL_SWITCH``.
    """

    def test_an_idle_watching_miner_reports_idle_not_watching(self):
        # The reproduction: a channel IS open, nothing is being mined.
        self.use(
            _FakeTwitch(["Overwatch"], wanted_games=(), watching=object(), state=State.IDLE),
            _FakeGui(status="💤 Idle: somechannel"),
        )
        body = self.probe()

        self.assertEqual(body["state"], "idle")
        self.assertIs(body["mining"], False)
        self.assertEqual(body["games_to_watch_count"], 1)
        self.assertEqual(body["wanted_games_count"], 0)
        # The watch list is fine, so the watch-list alarm stays quiet. This is
        # why README tells the operator to alert on "mining":false as well.
        self.assertIs(body["ok"], True)

    def test_a_mining_miner_still_reports_watching(self):
        self.use(
            _FakeTwitch(
                ["Overwatch"],
                wanted_games=["Overwatch"],
                watching=object(),
                state=State.CHANNEL_SWITCH,
            ),
            _FakeGui(status="Watching somechannel"),
        )
        body = self.probe()

        self.assertEqual(body["state"], "watching")
        self.assertIs(body["mining"], True)
        self.assertIs(body["ok"], True)

    def test_every_non_idle_state_with_a_handle_still_counts_as_mining(self):
        # Mining does not stop while the miner refetches inventory or rebuilds
        # its channel list; treating those as idle would flap the alert.
        for state in (
            State.INVENTORY_FETCH,
            State.GAMES_UPDATE,
            State.CHANNELS_CLEANUP,
            State.CHANNELS_FETCH,
            State.CHANNEL_SWITCH,
        ):
            with self.subTest(state=state):
                self.use(
                    _FakeTwitch(["Overwatch"], watching=object(), state=state), _FakeGui()
                )
                self.assertEqual(self.probe()["state"], "watching")

    def test_the_idle_state_without_a_handle_is_still_idle(self):
        self.use(_FakeTwitch(["Overwatch"], watching=None, state=State.IDLE), _FakeGui())
        self.assertEqual(self.probe()["state"], "idle")

    def test_a_non_idle_state_without_a_handle_is_idle_too(self):
        # Nothing open means nothing being mined, whatever the state machine is
        # busy with.
        self.use(
            _FakeTwitch(["Overwatch"], watching=None, state=State.CHANNEL_SWITCH), _FakeGui()
        )
        self.assertEqual(self.probe()["state"], "idle")

    def test_paused_outranks_the_idle_watch(self):
        # A paused miner also holds no real work; "paused" is the more specific
        # answer and the one the operator asked for.
        self.use(
            _FakeTwitch(["Overwatch"], watching=object(), paused=True, state=State.IDLE),
            _FakeGui(status="⏸ Mining paused"),
        )
        body = self.probe()

        self.assertEqual(body["state"], "paused")
        self.assertIs(body["mining"], False)

    def test_an_unreadable_state_degrades_instead_of_guessing(self):
        self.use(_StatelessTwitch(["Overwatch"], watching=object()), _FakeGui())
        body = self.probe()

        self.assertEqual(body["state"], "starting")
        self.assertIs(body["mining"], False)
        # A read that failed must never leave ok true. That is the one answer
        # that would keep a monitor quiet about a report it cannot trust.
        self.assertIs(body["ok"], False)


class TestHealthMiningPathIsExact(HealthMiningTestBase):
    """The allowlist matches by exact path, so the trailing slash is a trap.

    ``_is_public_request`` compares with ``in`` against ``_PUBLIC_PATHS``; only
    ``_UNPROTECTED_PREFIXES`` matches by prefix. A monitor pointed at
    ``/api/health/mining/`` therefore gets a 401 whose body contains no ``ok``
    field at all, and a monitor that alerts by inverting a match on
    ``"ok":false`` treats "keyword absent" as healthy. It would sit green
    through the entire outage it was installed to catch, which is why the
    difference is pinned here and spelled out in README.md.
    """

    def test_the_exact_path_answers_the_probe(self):
        self.use(_FakeTwitch([]), _FakeGui())
        response = self.request(HEALTH_PATH, client_host="192.168.1.50")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"ok":false', response.body)

    def test_the_trailing_slash_form_is_not_public(self):
        self.use(_FakeTwitch([]), _FakeGui())
        response = self.request(HEALTH_PATH + "/", client_host="192.168.1.50")

        self.assertEqual(response.status_code, 401)
        # Nothing for a keyword monitor to match, in either direction.
        self.assertNotIn(b'"ok"', response.body)

    def test_the_trailing_slash_form_only_redirects_even_when_authorized(self):
        # Allowlisting the slash form would not help either: an authorized
        # caller gets a bodyless redirect, so only a monitor that follows
        # redirects would ever see the probe.
        self.use(_FakeTwitch([]), _FakeGui())
        response = self.request(HEALTH_PATH + "/", client_host="127.0.0.1")

        self.assertEqual(response.status_code, 307)
        self.assertTrue(response.headers.get("location", "").endswith(HEALTH_PATH))
        self.assertNotIn(b'"ok"', response.body)


class TestHealthMiningSeesACorruptSettingsFile(HealthMiningTestBase):
    """The probe is the compensating control for a quarantined settings.json.

    An unparseable settings.json is preserved as ``settings.json.corrupt`` and
    the miner starts from defaults, which means ``games_to_watch: []``, so the
    miner mines nothing until the operator restores the list (see
    ``tests/test_settings_api.py::TestCorruptSettingsFile``). That state is a
    real outage, so it has to be visible from outside the process and not only
    in a log line nobody is tailing.
    """

    def test_a_defaults_boot_after_quarantine_reports_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            seed = dict(default_settings)
            seed["games_to_watch"] = ["Overwatch"]
            path.write_text(json.dumps(seed, indent=4)[:-1], encoding="utf-8")
            with patch.object(settings_module, "SETTINGS_PATH", path):
                settings = Settings()

        self.use(_FakeTwitch(settings.games_to_watch), _FakeGui())
        body = self.probe()

        self.assertIs(body["ok"], False)
        self.assertIs(body["watchlist_empty"], True)
        self.assertEqual(body["games_to_watch_count"], 0)


class TestHealthMiningSeesDisabledBenefits(HealthMiningTestBase):
    """Every benefit type off is the same outage with a healthy-looking UI.

    ``Benefit.is_wanted`` is fail-CLOSED (``allowed_benefits.get(name,
    False)``), so a ``mining_benefits`` selection that enables nothing makes
    every drop unwanted, ``wanted_games`` empty and mining stop as completely as
    an empty watch list does. Nothing about it looks wrong from the dashboard:
    the games are all still listed. The probe used to look at the watch list
    alone, so this state answered ``ok: true`` to the monitor installed to catch
    exactly this shape of outage.
    """

    def test_every_benefit_type_off_reports_not_ok(self):
        self.use(
            _FakeTwitch(["Overwatch"], mining_benefits=dict.fromkeys(ALL_BENEFITS, False)),
            _FakeGui(status="Idle"),
        )
        body = self.probe()

        self.assertIs(body["ok"], False)
        self.assertIs(body["benefits_disabled"], True)
        # The watch list is untouched, which is exactly why this needed its own
        # field: an operator reading watchlist_empty would look in the wrong place.
        self.assertIs(body["watchlist_empty"], False)
        self.assertEqual(body["games_to_watch_count"], 1)

    def test_one_benefit_type_left_on_is_still_ok(self):
        selection = dict.fromkeys(ALL_BENEFITS, False) | {"DIRECT_ENTITLEMENT": True}
        self.use(
            _FakeTwitch(
                ["Overwatch"],
                mining_benefits=selection,
                wanted_games=["Overwatch"],
                watching=object(),
            ),
            _FakeGui(status="Watching"),
        )
        body = self.probe()

        self.assertIs(body["ok"], True)
        self.assertIs(body["benefits_disabled"], False)

    def test_a_missing_selection_reads_as_disabled(self):
        # Mid-boot, or a settings.json that lost the key: nothing is enabled, so
        # nothing would be mined, and guessing "probably fine" is the one answer
        # that keeps the monitor quiet through an outage.
        self.use(_FakeTwitch(["Overwatch"], mining_benefits={}), _FakeGui())
        body = self.probe()

        self.assertIs(body["benefits_disabled"], True)
        self.assertIs(body["ok"], False)

    def test_an_unreadable_selection_reads_as_disabled(self):
        # A hand-edited string where the dict belongs. The probe must not raise
        # and must not report it as healthy.
        self.use(_FakeTwitch(["Overwatch"], mining_benefits="BADGE"), _FakeGui())
        body = self.probe()

        self.assertIs(body["benefits_disabled"], True)
        self.assertIs(body["ok"], False)

    def test_the_disabled_state_carries_the_alert_keyword_on_the_wire(self):
        # The monitor greps bytes, so the new reason has to reach them.
        self.use(
            _FakeTwitch(["Overwatch"], mining_benefits=dict.fromkeys(ALL_BENEFITS, False)),
            _FakeGui(),
        )
        body = self.probe_bytes()

        self.assertIn(b'"ok":false', body)
        self.assertIn(b'"benefits_disabled":true', body)


class TestHealthMiningSeesALoggedOutMiner(HealthMiningTestBase):
    """A miner that never logged in mines nothing, and used to answer ``ok: true``.

    ``Twitch._run`` blocks on ``await self.get_auth()`` before the watch task is
    even created, and when no usable token is on disk that call lands in
    ``_AuthState._oauth_login``'s ``while True`` device-code poll
    (``src/auth/auth_state.py:105-147``), which waits for a human to type a code
    into twitch.tv/activate. Nothing is mined for as long as that lasts, and no
    restart fixes it.

    Every field the probe had at that point described CONFIGURATION — a watch
    list, a benefit selection — all of which are perfectly valid while the miner
    is stuck, so the body read ``{"ok":true,"state":"idle","mining":false,
    "login":null}``. The login signal was already in hand and thrown away.
    """

    def test_a_miner_with_no_login_reports_not_ok(self):
        # The reproduction: a healthy configuration in front of a miner that
        # cannot mine a single minute.
        self.use(
            _FakeTwitch(["Overwatch"], wanted_games=["Overwatch"]),
            _FakeGui(user_login=None),
        )
        body = self.probe()

        self.assertIs(body["ok"], False)
        self.assertIsNone(body["login"])
        self.assertIs(body["login_pending"], False)
        # ...and the configuration fields still report healthy, which is exactly
        # why ok could not be derived from them alone.
        self.assertIs(body["watchlist_empty"], False)
        self.assertIs(body["benefits_disabled"], False)

    def test_a_pending_device_code_reports_not_ok_and_says_it_needs_a_human(self):
        self.use(
            _FakeTwitch(["Overwatch"]),
            _FakeGui(
                user_login=None,
                oauth_pending={"url": "https://www.twitch.tv/activate", "code": "ABCDEFGH"},
            ),
        )
        body = self.probe()

        self.assertIs(body["ok"], False)
        # The field that separates "restart it" from "go type the code": no
        # amount of restarting clears a device-code prompt.
        self.assertIs(body["login_pending"], True)

    def test_the_device_code_is_never_disclosed(self):
        # The endpoint is unauthenticated. The pending payload is an activation
        # URL plus the code itself, and handing either to an anonymous caller
        # would let them complete the operator's login.
        self.use(
            _FakeTwitch(["Overwatch"]),
            _FakeGui(
                user_login=None,
                oauth_pending={
                    "url": "https://www.twitch.tv/activate?device-code=SECRETCODE",
                    "code": "SECRETCODE",
                },
            ),
        )
        text = self.probe_text()

        self.assertNotIn("SECRETCODE", text)
        self.assertNotIn("activate", text)
        self.assertIn('"login_pending":true', self.probe_bytes().decode())

    def test_the_localized_status_line_is_not_what_is_matched(self):
        # get_status() also carries a status string straight out of lang/*.json.
        # Deriving login state from it would break the probe on a language
        # change, so the account name and the pending key are the only inputs.
        self.use(_FakeTwitch(["Overwatch"]), _FakeGui(user_login=None))
        text = self.probe_text()

        self.assertNotIn("Logged in", text)
        self.assertNotIn("status", self.probe())

    def test_an_empty_account_name_counts_as_no_login(self):
        # /oauth2/validate is read with .get("login", "") in
        # src/auth/auth_state.py:285, so "" is reachable. The documented value
        # for "no login" is null, and "" is nothing a monitor can act on.
        self.use(_FakeTwitch(["Overwatch"]), _FakeGui(user_login=""))
        body = self.probe()

        self.assertIsNone(body["login"])
        self.assertIs(body["ok"], False)

    def test_uptime_tells_a_fresh_boot_apart_from_a_stuck_login(self):
        # Both answer login: null. Only the uptime says whether that is the
        # normal first seconds of a restart or a miner that has been stuck for
        # half an hour, which is the difference between "wait" and "act".
        self.use(_FakeTwitch(["Overwatch"]), _FakeGui(user_login=None))

        with patch.object(webapp, "_PROCESS_START", _seconds_ago(3)):
            booting = self.probe()
        self.assertLess(booting["uptime_seconds"], 60)

        with patch.object(webapp, "_PROCESS_START", _seconds_ago(1800)):
            stuck = self.probe()
        self.assertGreaterEqual(stuck["uptime_seconds"], 1800)

        # Same login field in both, so uptime is what carries the difference.
        self.assertIsNone(booting["login"])
        self.assertIsNone(stuck["login"])

    def test_the_logged_out_state_carries_the_alert_keyword_on_the_wire(self):
        # The monitor greps bytes, not the decoded dict.
        self.use(_FakeTwitch(["Overwatch"]), _FakeGui(user_login=None))
        body = self.probe_bytes()

        self.assertIn(b'"ok":false', body)
        self.assertIn(b'"login":null', body)

    def test_a_logged_in_miner_is_not_reported_as_pending(self):
        self.use(
            _FakeTwitch(["Overwatch"], wanted_games=["Overwatch"], watching=object()),
            _FakeGui(user_login="arend"),
        )
        body = self.probe()

        self.assertEqual(body["login"], "arend")
        self.assertIs(body["login_pending"], False)
        self.assertIs(body["ok"], True)


class TestHealthMiningSeesAStalledWatch(HealthMiningTestBase):
    """``ok`` has to measure progress, and only ``last_watch_ok`` measures it.

    Watch list, benefit selection and wanted-game count are all configuration.
    They stay exactly as valid through a dead proxy, a rejected token or a stuck
    endpoint as they are while mining works, so a probe built only on them
    reports a healthy miner that has earned nothing in a week.

    ``Twitch.last_watch_ok`` (``src/core/client.py:95``) is stamped every time
    Twitch ACCEPTS a watch payload (``src/services/watch_service.py:282``). It
    was added in the previous wave and never read; these tests are what reading
    it has to mean.
    """

    def stamped(self, seconds_ago, **kwargs):
        """A miner that wants a game and last got a watch through then."""
        kwargs.setdefault("wanted_games", ["Overwatch"])
        kwargs.setdefault("watching", object())
        return _FakeTwitch(
            ["Overwatch"],
            last_watch_ok=None if seconds_ago is None else _seconds_ago(seconds_ago),
            **kwargs,
        )

    def up_for(self, seconds):
        """Pretend the process has been running that long.

        Every test that ages a stamp has to age the process with it, or the
        uptime floor answers the question instead of the guard under test: a
        real miner cannot hold a stamp from before it started, but a test
        process that imported the app two seconds ago can.
        """
        return patch.object(webapp, "_PROCESS_START", _seconds_ago(seconds))

    def test_a_miner_that_stopped_getting_through_reports_not_ok(self):
        # The reproduction: still "watching", still wanting the game, nothing
        # accepted for twenty minutes. This is the shape a monitor called green.
        self.use(self.stamped(1200), _FakeGui())
        with self.up_for(1300):
            body = self.probe()

        self.assertIs(body["ok"], False)
        self.assertIs(body["watch_stalled"], True)
        self.assertGreaterEqual(body["last_watch_age_seconds"], 1200)
        # Everything else still insists the miner is fine, which is the point.
        self.assertEqual(body["state"], "watching")
        self.assertIs(body["mining"], True)
        self.assertIs(body["watchlist_empty"], False)

    def test_a_fresh_watch_is_not_a_stall(self):
        self.use(self.stamped(30), _FakeGui())
        with self.up_for(3600):
            body = self.probe()

        self.assertIs(body["watch_stalled"], False)
        self.assertIs(body["ok"], True)
        self.assertLess(body["last_watch_age_seconds"], 60)

    def test_a_routine_gap_between_channel_switches_is_not_a_stall(self):
        # When a watched stream ends the miner must find a replacement, and a
        # candidate is only eligible after ONLINE_DELAY (120s) of being live, so
        # several quiet minutes is normal. A five-minute window would sit right
        # on top of that; ten watch intervals clears it.
        self.use(self.stamped(300), _FakeGui())
        with self.up_for(3600):
            body = self.probe()

        self.assertIs(body["watch_stalled"], False)
        self.assertIs(body["ok"], True)

    def test_no_wanted_games_never_stalls_however_long_the_silence(self):
        # THE false-alarm guard. wanted_games_count == 0 means "no active
        # campaign for my games right now", which is where a healthy single-game
        # account sits for days. An alert firing here teaches its owner to ignore
        # the alert, which is worse than the gap it would close.
        self.use(self.stamped(6 * 3600, wanted_games=(), watching=None), _FakeGui())
        with self.up_for(7 * 3600):
            body = self.probe()

        self.assertEqual(body["wanted_games_count"], 0)
        self.assertIs(body["watch_stalled"], False)
        self.assertIs(body["ok"], True)

    def test_a_wanted_game_with_no_live_channel_is_not_a_stall(self):
        # THE second false-alarm guard, and the subtler one. A campaign can be
        # active for a game on the watch list while no channel is actually
        # streaming it: CHANNEL_SWITCH finds no candidate, drops to IDLE with
        # wanted_games still populated, and the bottom-of-loop wait then holds
        # that for up to an hour with nothing stamped. Campaigns run around the
        # clock; streamers do not. Alerting here is the same "healthy but quiet"
        # shape as no-wanted-games, one step further along, and it would fire
        # for an hour at a time on a perfectly well account.
        self.use(
            self.stamped(2400, watching=None, state=State.IDLE),
            _FakeGui(),
        )
        with self.up_for(7200):
            body = self.probe()

        self.assertEqual(body["wanted_games_count"], 1)
        self.assertEqual(body["state"], "idle")
        self.assertGreaterEqual(body["last_watch_age_seconds"], 2400)
        self.assertIs(body["watch_stalled"], False)
        self.assertIs(body["ok"], True)

    def test_an_idle_watch_alone_does_not_prove_mining(self):
        # last_watch_ok is stamped by the IDLE branch too, so a fresh stamp next
        # to zero wanted games says nothing about mining. It must not be read as
        # health any more than its absence is read as failure.
        self.use(
            self.stamped(5, wanted_games=(), watching=object(), state=State.IDLE),
            _FakeGui(),
        )
        body = self.probe()

        self.assertEqual(body["state"], "idle")
        self.assertIs(body["mining"], False)
        self.assertIs(body["watch_stalled"], False)

    def test_a_deliberately_paused_miner_never_stalls(self):
        # Pausing is an operator decision, not an outage.
        self.use(self.stamped(6 * 3600, paused=True), _FakeGui())
        with self.up_for(7 * 3600):
            body = self.probe()

        self.assertEqual(body["state"], "paused")
        self.assertIs(body["watch_stalled"], False)
        self.assertIs(body["ok"], True)

    def test_a_boot_that_has_not_watched_yet_is_not_a_stall(self):
        # last_watch_ok is None until the first accepted payload, and a restart
        # legitimately spends its first minute logging in and fetching inventory.
        self.use(self.stamped(None), _FakeGui())
        with patch.object(webapp, "_PROCESS_START", _seconds_ago(20)):
            body = self.probe()

        self.assertIsNone(body["last_watch_age_seconds"])
        self.assertIs(body["watch_stalled"], False)
        self.assertIs(body["ok"], True)

    def test_a_miner_that_never_watched_at_all_stalls_once_the_window_passes(self):
        # The grace period is a grace period, not an exemption: a miner that has
        # been up for half an hour without one accepted payload is broken.
        self.use(self.stamped(None), _FakeGui())
        with patch.object(webapp, "_PROCESS_START", _seconds_ago(1800)):
            body = self.probe()

        self.assertIsNone(body["last_watch_age_seconds"])
        self.assertIs(body["watch_stalled"], True)
        self.assertIs(body["ok"], False)

    def test_an_age_older_than_the_process_cannot_trigger_a_stall(self):
        # A stamp from before this process started can only mean the clock moved
        # (NTP, a suspended host). The miner has had no time to fail yet.
        self.use(self.stamped(6 * 3600), _FakeGui())
        with patch.object(webapp, "_PROCESS_START", _seconds_ago(10)):
            body = self.probe()

        self.assertIs(body["watch_stalled"], False)
        self.assertIs(body["ok"], True)
        # The measurement itself is still reported honestly.
        self.assertGreaterEqual(body["last_watch_age_seconds"], 6 * 3600)

    def test_a_stamp_from_the_future_reports_zero_not_a_negative_age(self):
        # A clock step backwards would otherwise publish a negative number, and
        # a threshold comparison reads that as freshly alive.
        self.use(self.stamped(-600), _FakeGui())
        body = self.probe()

        self.assertEqual(body["last_watch_age_seconds"], 0)
        self.assertIs(body["watch_stalled"], False)

    def test_an_unreadable_stamp_degrades_instead_of_guessing(self):
        # A client object without the attribute is a broken client, not a miner
        # with nothing to report. "Could not tell" is never answered with ok.
        twitch = self.stamped(30)
        del twitch.last_watch_ok
        self.use(twitch, _FakeGui())
        body = self.probe()

        self.assertIsNone(body["last_watch_age_seconds"])
        self.assertIs(body["ok"], False)

    def test_the_window_is_ten_watch_intervals_and_clears_the_five_minute_floor(self):
        # Pinned because the number is the whole difference between an alert
        # that fires on outages and one that fires on channel switches.
        self.assertEqual(webapp._MiningHealthProbe.STALL_TICKS, 10)
        self.assertEqual(
            webapp._MiningHealthProbe.STALL_AFTER_SECONDS,
            WATCH_INTERVAL.total_seconds() * 10,
        )
        self.assertGreater(webapp._MiningHealthProbe.STALL_AFTER_SECONDS, 300)

    def test_the_stall_carries_the_alert_keyword_on_the_wire(self):
        self.use(self.stamped(1200), _FakeGui())
        with self.up_for(1300):
            body = self.probe_bytes()

        self.assertIn(b'"ok":false', body)
        self.assertIn(b'"watch_stalled":true', body)

    def test_a_healthy_miner_reports_no_stall_on_the_wire(self):
        # Monitors invert their match, so the down keywords must be absent from
        # a healthy body entirely.
        self.use(self.stamped(30), _FakeGui())
        with self.up_for(3600):
            body = self.probe_bytes()

        self.assertIn(b'"ok":true', body)
        self.assertIn(b'"watch_stalled":false', body)
        self.assertNotIn(b'"ok":false', body)


class TestHealthMiningReadIsolation(HealthMiningTestBase):
    """One raising field must not corrupt the others, nor leave ``ok`` true.

    The first cut wrapped every client read in a SINGLE try: the first raise
    skipped the remaining assignments, so their defaults (0 / empty) were
    published as if they were measurements — and ``ok`` only looked at the watch
    list, so a probe that had read almost nothing could still answer ``ok: true``.
    """

    def test_a_raising_field_does_not_zero_the_fields_that_read_fine(self):
        self.use(_NoWantedGamesTwitch(["Overwatch", "Rust"]), _FakeGui(user_login="arend"))
        body = self.probe()
        # The watch list was readable, so its count must be real, not zeroed.
        self.assertEqual(body["games_to_watch_count"], 2)
        self.assertIs(body["watchlist_empty"], False)
        self.assertEqual(body["login"], "arend")
        # ...and the unreadable one is the reason ok drops.
        self.assertEqual(body["wanted_games_count"], 0)
        self.assertIs(body["ok"], False)

    def test_a_raising_watch_list_does_not_zero_the_wanted_count(self):
        self.use(_NoSettingsTwitch(wanted_games=["Overwatch"]), _FakeGui())
        body = self.probe()
        self.assertEqual(body["wanted_games_count"], 1)
        self.assertEqual(body["games_to_watch_count"], 0)
        self.assertIs(body["ok"], False)

    def test_a_raising_login_read_leaves_the_mining_state_intact(self):
        self.use(_FakeTwitch(["Overwatch"], watching=object()), _ExplodingGui())
        body = self.probe()
        self.assertEqual(body["state"], "watching")
        self.assertEqual(body["games_to_watch_count"], 1)
        self.assertIsNone(body["login"])
        # An unreadable login manager cannot report a pending device code
        # either; the safe default is the one that raises no false alarm, and
        # the degraded flag is what drops ok.
        self.assertIs(body["login_pending"], False)
        self.assertIs(body["ok"], False)

    def test_a_raising_watch_stamp_does_not_zero_the_login(self):
        # The mirror of the above: last_watch_ok is the newest read, so it is
        # the newest way to lose the fields around it.
        twitch = _FakeTwitch(["Overwatch"], wanted_games=["Overwatch"], watching=object())
        del twitch.last_watch_ok
        self.use(twitch, _FakeGui(user_login="arend"))
        body = self.probe()

        self.assertEqual(body["login"], "arend")
        self.assertEqual(body["wanted_games_count"], 1)
        self.assertIsNone(body["last_watch_age_seconds"])
        self.assertIs(body["ok"], False)


class TestHealthMiningDisclosesNothingExtra(HealthMiningTestBase):
    """Public endpoint, so the body is a contract about what it must NOT say.

    ``gui_manager.status.get()`` used to be echoed verbatim. That string carries
    the watched channel name, and in manual mode the targeted game as well
    (src/core/client.py:741-744), so an unauthenticated caller could read both
    off a probe whose own docstring promised "no campaign detail".
    """

    def test_watched_channel_name_is_never_disclosed(self):
        self.use(
            _FakeTwitch(["Overwatch"], watching=object()),
            _FakeGui(status="Watching supersecretchannel"),
        )
        self.assertNotIn("supersecretchannel", self.probe_text())

    def test_manual_mode_target_game_is_never_disclosed(self):
        self.use(
            _FakeTwitch(["Overwatch"], watching=object()),
            _FakeGui(status="🎯 Manual Mode: Watching supersecretchannel for Secret Playtest"),
        )
        text = self.probe_text()
        self.assertNotIn("supersecretchannel", text)
        self.assertNotIn("Secret Playtest", text)

    def test_idle_channel_names_are_never_disclosed(self):
        self.use(
            _FakeTwitch(["Overwatch"]),
            _FakeGui(status="💤 Idle: chan_one, chan_two"),
        )
        text = self.probe_text()
        self.assertNotIn("chan_one", text)
        self.assertNotIn("chan_two", text)

    def test_state_is_derived_from_objects_not_from_the_status_text(self):
        # A status line that says "Watching" while nothing is being watched must
        # not be able to talk the probe into reporting mining.
        self.use(_FakeTwitch(["Overwatch"], watching=None), _FakeGui(status="Watching lies"))
        body = self.probe()
        self.assertEqual(body["state"], "idle")
        self.assertIs(body["mining"], False)

    def test_the_raw_status_key_is_gone(self):
        # Named explicitly: a re-added "status" key would silently reopen the leak.
        self.use(_FakeTwitch(["Overwatch"]), _FakeGui(status="Watching supersecretchannel"))
        self.assertNotIn("status", self.probe())

    def test_healthz_stays_independent_of_mining_state(self):
        # Docker's HEALTHCHECK targets /healthz — an empty watch list must not
        # start restarting the container.
        self.use(_FakeTwitch([]), _FakeGui())
        response = self.request("/healthz", client_host="192.168.1.50")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class TestHealthMiningWireForm(HealthMiningTestBase):
    """The keyword an uptime monitor matches has to exist in the actual bytes.

    A monitor does not parse JSON, it greps the response body, so the alerting
    contract is the serialized form and not the decoded dict every other test in
    this file inspects. Starlette's ``JSONResponse.render`` serializes with
    ``separators=(",", ":")``, so the body reads ``{"ok":false,...}`` with no
    space after the colon — while a hand-written JSON sample in the docs reads
    ``"ok": false``. A monitor configured from the pretty form matches nothing,
    stays green forever, and the silent outage this endpoint exists to surface
    goes unreported: the failure is invisible in exactly the way that cost five
    days of mining. So the bytes are pinned here, and the README's instruction is
    pinned against them below.
    """

    def test_the_alert_keyword_is_the_spaceless_wire_form(self):
        self.use(_FakeTwitch([]), _FakeGui())
        body = self.probe_bytes()

        self.assertIn(b'"ok":false', body)
        self.assertNotIn(b'"ok": false', body)

    def test_a_healthy_body_does_not_contain_the_alert_keyword(self):
        # Monitors invert the match, so a healthy body containing the down
        # keyword anywhere — even inside another field — would alert forever.
        self.use(
            _FakeTwitch(["Overwatch"], wanted_games=["Overwatch"], watching=object()),
            _FakeGui(user_login="arend"),
        )
        body = self.probe_bytes()

        self.assertIn(b'"ok":true', body)
        self.assertNotIn(b'"ok":false', body)


class TestHealthMiningIsDocumentedAccurately(unittest.TestCase):
    """README.md is the operator's copy of this contract, so it is asserted too.

    Not pedantry: the documented keyword was wrong (the pretty form, which never
    matches), and a wrong monitoring instruction fails exactly as quietly as no
    monitoring at all. Pinning the sample and the keyword makes a drifted doc a
    red test instead of an alert that never fires.
    """

    def setUp(self):
        self.section = self.health_section()
        # Prose wraps, so every phrase assertion runs against a single-spaced
        # copy: a reflowed paragraph must not be able to fail a docs test.
        self.prose = " ".join(self.section.split())

    def health_section(self) -> str:
        """Everything under the Health and monitoring heading, up to the next one."""
        readme = README.read_text(encoding="utf-8")
        parts = readme.split("## Health and monitoring", 1)
        self.assertEqual(len(parts), 2, "README.md lost its Health and monitoring section")
        return parts[1].split("\n## ", 1)[0]

    def documented_sample(self) -> dict:
        """The ```json response sample inside that section."""
        block = re.search(r"```json\n(.*?)```", self.section, re.S)
        self.assertIsNotNone(block, "the documented response sample is gone")
        return json.loads(block.group(1))

    def test_the_documented_sample_carries_exactly_the_documented_keys(self):
        self.assertEqual(set(self.documented_sample()), DOCUMENTED_KEYS)

    def test_the_documented_sample_uses_a_real_state_value(self):
        self.assertIn(self.documented_sample()["state"], DOCUMENTED_STATES)

    def test_the_keyword_the_operator_is_told_to_match_is_the_wire_form(self):
        # Scoped to the one code span the instruction hands over, so the
        # paragraph that quotes the WRONG form in order to warn about it, and the
        # pretty-printed sample above it, are both left alone.
        instruction = re.search(r"keyword-match the literal `([^`]*)`", self.section)
        self.assertIsNotNone(instruction, "the README no longer tells the operator what to match")
        self.assertEqual(instruction.group(1), '"ok":false')

    def test_the_section_no_longer_documents_a_status_field(self):
        # The leaked key. Documenting it again is how it comes back.
        self.assertNotIn('"status"', self.section)

    def test_the_operator_is_warned_that_the_path_is_matched_exactly(self):
        # TestHealthMiningPathIsExact proves the 401; this proves the operator
        # was told, which is the half that decides whether a monitor works.
        self.assertIn("exact path", self.prose)
        self.assertIn(f"{HEALTH_PATH}/", self.prose)
        self.assertIn("401", self.prose)

    def test_the_documented_state_vocabulary_covers_the_idle_watch(self):
        # The word "idle" alone reads as "nothing to mine". A channel being open
        # while nothing is mined is the case that misleads, so it is named.
        self.assertIn("idle watch", self.prose)
        self.assertRegex(
            self.prose,
            r"`watching` means the miner is watching a channel \*\*for\s+drops\*\*",
        )

    def test_the_operator_is_told_the_second_reason_ok_can_drop(self):
        # An operator who reads only "ok is false when the watch list is empty"
        # will look at a full watch list and conclude the probe is lying.
        self.assertIn("`benefits_disabled`", self.prose)
        self.assertIn("no benefit type is enabled", self.prose)

    def test_the_operator_is_told_ok_survives_an_idle_miner(self):
        # An alert rule built on ok alone will not fire on a stalled idle watch,
        # so the README has to hand over the second keyword.
        self.assertIn("`ok` does not drop while the miner is idle", self.prose)
        self.assertIn('`"mining":false`', self.prose)


if __name__ == "__main__":
    unittest.main()
