"""Four small guards, one shared failure mode: mining stops and nothing says so.

Each of these shipped in the resilience wave with no test, and each of them can
be deleted without a single existing test noticing. They are grouped here
because they answer the same question in four different places - *what happens
to the miner when something it reads is not what it wrote?* - and the answer has
to be "it keeps mining, out loud" every time.

* ``get_streak_state`` returned the per-channel VALUE unchecked, and that value
  is the sort key of the ``CHANNEL_SWITCH`` branch. A non-object there raised
  ``AttributeError`` inside ``sorted()``, which ``Twitch.run()`` does not
  recover from: exit 1, restart, same file, same death.
* ``MaintenanceService._refresh_period`` floors a reload interval of zero or
  less. Unfloored, ``next_period`` is already in the past when the task starts,
  so it immediately asks for an ``INVENTORY_FETCH`` and is immediately
  restarted - a hot loop that refetches the whole inventory as fast as Twitch
  will answer and never lets the state machine reach ``CHANNEL_SWITCH``.
* ``StreamSelector`` now reports how many drops ``drop_name_blacklist``
  excluded. It is a plain substring match with no minimum length, so a
  one-letter keyword empties the wanted list while the only warning the
  operator gets names ``games_to_watch`` - the one setting that is not the
  problem.
* ``WatchService.watch_loop`` stamps ``last_watch_ok`` when, and only when,
  Twitch ACCEPTS a watch payload. It is the single progress signal the
  application has; ``GET /api/health/mining`` is built on it, and every other
  input the probe reads describes configuration, which stays valid through a
  dead proxy or a rejected token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.services import maintenance, message_handlers
from src.services.maintenance import _MINIMUM_REFRESH_MINUTES, MaintenanceService
from src.services.message_handlers import get_streak_state
from src.services.stream_selector import StreamSelector
from src.services.watch_service import WatchService


class StreakFileTestBase(unittest.TestCase):
    """A real ``watch_streaks.json`` in a temp dir."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "watch_streaks.json"
        patcher = patch.object(
            message_handlers, "_get_streaks_file", lambda: self.path
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed(self, mapping: dict) -> None:
        self.path.write_text(json.dumps(mapping), encoding="utf-8")


class TestACorruptStreakEntryCannotKillTheMainLoop(StreakFileTestBase):
    """Only ``_mark_streak_claimed`` writes here, and it always writes an object.

    So anything else is a hand edit or a foreign writer, and the miner's answer
    to that has to be "mine anyway", not "exit 1".
    """

    def test_every_non_object_channel_state_reads_as_empty(self):
        for label, value in {
            "a string": "yes",
            "a list": ["a"],
            "a number": 5,
            "a boolean": True,
            "a null": None,
        }.items():
            with self.subTest(state=label):
                self.seed({"chan": value})
                self.assertEqual(get_streak_state("chan"), {})

    def test_a_real_streak_object_is_returned_untouched(self):
        self.seed({"chan": {"active": True, "last_claimed_date": "2026-01-01"}})
        self.assertEqual(
            get_streak_state("chan"), {"active": True, "last_claimed_date": "2026-01-01"}
        )

    def test_an_unknown_channel_is_still_an_empty_state(self):
        self.seed({"other": {"active": True}})
        self.assertEqual(get_streak_state("chan"), {})

    def test_a_corrupt_neighbour_does_not_hide_a_healthy_entry(self):
        # The guard is per-channel, not per-file: one bad entry must not cost
        # the streak state of every other channel.
        self.seed({"bad": "yes", "good": {"active": True}})
        self.assertEqual(get_streak_state("bad"), {})
        self.assertEqual(get_streak_state("good"), {"active": True})

    def test_the_channel_switch_sort_key_no_longer_raises(self):
        # _has_unclaimed_streak_today is called from inside sorted() in the
        # CHANNEL_SWITCH branch. This is the call that used to end the process.
        self.seed({"chan": "yes"})
        selector = StreamSelector()

        self.assertFalse(selector._has_unclaimed_streak_today("chan"))

    def test_an_active_unclaimed_streak_is_still_reported(self):
        self.seed({"chan": {"active": True}})
        self.assertTrue(StreamSelector()._has_unclaimed_streak_today("chan"))

    def test_a_streak_already_claimed_today_is_still_reported_as_claimed(self):
        self.seed({"chan": {"active": True, "last_claimed_date": date.today().isoformat()}})
        self.assertFalse(StreamSelector()._has_unclaimed_streak_today("chan"))

    def test_an_unparseable_file_is_still_an_empty_state(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(get_streak_state("chan"), {})


class _Clock:
    """A ``datetime`` stand-in whose ``now()`` advances by ``step`` per call.

    The maintenance loop compares wall-clock readings against a deadline it
    computed from one, so real time is the only thing that ends it. Advancing
    the clock is what lets a one-minute period be exercised in a test.
    """

    def __init__(self, step: timedelta):
        self._next = datetime.now(timezone.utc)
        self._step = step

    def now(self, tz=None) -> datetime:
        value = self._next
        self._next = value + self._step
        return value


class _MaintenanceSettings:
    def __init__(self, minutes: int):
        self.minimum_refresh_interval_minutes = minutes


class _MaintenanceTwitch:
    def __init__(self, minutes: int):
        self.settings = _MaintenanceSettings(minutes)
        self._mnt_triggers: list = []
        self.states: list = []

    def change_state(self, state) -> None:
        self.states.append(state)


class TestTheReloadPeriodCannotBeZero(unittest.TestCase):
    """``minimum_refresh_interval_minutes`` has no bound anywhere.

    The settings model types it as a plain int, the UI box's ``min="1"`` is not
    read back by the client, and ``merge_json`` only checks that a stored value
    IS an int. So zero is reachable both through the API and through a hand edit.
    """

    def period(self, minutes: int) -> timedelta:
        return MaintenanceService(_MaintenanceTwitch(minutes))._refresh_period()

    def test_zero_and_below_are_floored(self):
        for minutes in (0, -1, -30, -100000):
            with self.subTest(minimum_refresh_interval_minutes=minutes), self.assertLogs(
                "TwitchDrops", level=logging.WARNING
            ):
                self.assertEqual(
                    self.period(minutes), timedelta(minutes=_MINIMUM_REFRESH_MINUTES)
                )

    def test_a_real_interval_is_left_exactly_as_configured(self):
        for minutes in (1, 30, 60, 1440):
            with self.subTest(minimum_refresh_interval_minutes=minutes):
                self.assertEqual(self.period(minutes), timedelta(minutes=minutes))

    def test_a_real_interval_is_not_complained_about(self):
        with self.assertNoLogs("TwitchDrops", level=logging.WARNING):
            self.period(60)

    def test_the_floor_says_what_it_did_and_what_it_prevented(self):
        with self.assertLogs("TwitchDrops", level=logging.WARNING) as captured:
            self.period(0)

        reported = " | ".join(captured.output)
        self.assertIn("minimum_refresh_interval_minutes is 0", reported)
        self.assertIn("refetch the inventory in a loop", reported)

    def test_the_stored_setting_is_never_rewritten(self):
        # The floor is applied to the period this loop sleeps for, not to the
        # operator's value: a validator here would fight the settings layer.
        twitch = _MaintenanceTwitch(0)
        with self.assertLogs("TwitchDrops", level=logging.WARNING):
            MaintenanceService(twitch)._refresh_period()

        self.assertEqual(twitch.settings.minimum_refresh_interval_minutes, 0)


class TestAZeroIntervalNoLongerSpins(unittest.IsolatedAsyncioTestCase):
    """The consequence the floor exists for, driven through the real task."""

    async def test_a_zero_interval_waits_instead_of_asking_for_a_reload(self):
        twitch = _MaintenanceTwitch(0)
        service = MaintenanceService(twitch)

        with self.assertLogs("TwitchDrops", level=logging.WARNING), self.assertRaises(
            asyncio.TimeoutError
        ):
            await asyncio.wait_for(service.run_maintenance_task(), timeout=0.2)

        # Unfloored, the loop breaks out immediately and the state machine is
        # sent straight back to INVENTORY_FETCH - over and over.
        self.assertEqual(twitch.states, [])

    async def test_the_floored_period_still_reloads_once_it_is_genuinely_due(self):
        # The floor delays the first reload; it must not remove it. Driven on a
        # fake clock so the assertion is about the period, not about how long
        # the test is willing to wait.
        twitch = _MaintenanceTwitch(0)
        service = MaintenanceService(twitch)
        real_sleep = asyncio.sleep

        async def instant(_seconds):
            await real_sleep(0)

        with patch("asyncio.sleep", instant), patch.object(
            maintenance, "datetime", _Clock(step=timedelta(seconds=30))
        ), self.assertLogs("TwitchDrops", level=logging.WARNING):
            await asyncio.wait_for(service.run_maintenance_task(), timeout=2)

        self.assertEqual([state.name for state in twitch.states], ["INVENTORY_FETCH"])


class _Benefit:
    def __init__(self, name: str = "Reward"):
        self.name = name
        self.image_url = "url"

    def is_wanted(self, allowed_benefits) -> bool:
        return True


class _Drop:
    def __init__(self, name: str):
        self.name = name
        self.is_claimed = False
        self.required_minutes = 10
        self.benefits = [_Benefit()]

    def _base_can_earn(self) -> bool:
        return True


class _Campaign:
    def __init__(self, game_name: str, drops: list[_Drop]):
        self.game = MagicMock(name=game_name, id="g1", box_art_url="url")
        self.game.name = game_name
        self.drops = drops
        self.id = "c1"
        self.name = "Campaign"
        self.campaign_url = "url"
        self.ends_at = datetime.now(timezone.utc) + timedelta(days=1)

    def can_earn_within(self, stamp) -> bool:
        return True


class _SelectorSettings:
    def __init__(self, blacklist: list[str]):
        self.games_to_watch = ["Rust"]
        self.preferred_games: list[str] = []
        self.mining_benefits: dict[str, bool] = {}
        self.drop_name_blacklist = blacklist


class TestTheBlacklistSaysWhatItExcluded(unittest.TestCase):
    """A substring match with no minimum length can empty the wanted list.

    Without this line the operator is told "No wanted games found!" next to a
    watch list that is visibly full, and goes looking at ``games_to_watch``.
    """

    def tree(self, blacklist, *, report_exclusions=True):
        campaigns = [_Campaign("Rust", [_Drop("Emote Pack"), _Drop("Weapon Skin")])]
        return StreamSelector()._get_wanted_game_tree(
            _SelectorSettings(blacklist), campaigns, report_exclusions=report_exclusions
        )

    def test_excluding_everything_warns_and_names_the_setting(self):
        with self.assertLogs("TwitchDrops", level=logging.WARNING) as captured:
            wanted = self.tree(["e"])

        self.assertEqual(wanted, [])
        reported = " | ".join(captured.output)
        self.assertIn("drop_name_blacklist excluded 2", reported)
        self.assertIn("nothing is left to mine", reported)
        self.assertIn("keywords: e", reported)

    def test_a_partial_exclusion_is_informational_only(self):
        # It did the job it was asked to do; a WARNING here trains the operator
        # to ignore the one that matters.
        with self.assertLogs("TwitchDrops", level=logging.INFO) as captured:
            wanted = self.tree(["emote"])

        self.assertEqual(len(wanted), 1)
        reported = " | ".join(captured.output)
        self.assertIn("excluded 1", reported)
        self.assertNotIn("nothing is left to mine", reported)
        self.assertEqual([line for line in captured.output if line.startswith("WARNING")], [])

    def test_no_blacklist_produces_no_line_at_all(self):
        with self.assertNoLogs("TwitchDrops", level=logging.INFO):
            self.assertEqual(len(self.tree([])), 1)

    def test_a_blacklist_that_excluded_nothing_produces_no_line(self):
        with self.assertNoLogs("TwitchDrops", level=logging.INFO):
            self.assertEqual(len(self.tree(["nothing-matches-this"])), 1)

    def test_the_web_path_stays_silent(self):
        # _get_wanted_game_tree is called on every dashboard load and every
        # wanted-items broadcast. A line per call buries the mining one.
        with self.assertNoLogs("TwitchDrops", level=logging.INFO):
            self.tree(["e"], report_exclusions=False)

    def test_the_default_is_silence(self):
        campaigns = [_Campaign("Rust", [_Drop("Emote Pack")])]
        with self.assertNoLogs("TwitchDrops", level=logging.INFO):
            StreamSelector()._get_wanted_game_tree(_SelectorSettings(["e"]), campaigns)

    def test_the_mining_path_opts_in(self):
        # get_wanted_games is the mining caller, and is the reason the flag is
        # not simply always-on.
        campaigns = [_Campaign("Rust", [_Drop("Emote Pack")])]
        with self.assertLogs("TwitchDrops", level=logging.WARNING) as captured:
            self.assertEqual(StreamSelector().get_wanted_games(_SelectorSettings(["e"]), campaigns), [])

        self.assertIn("drop_name_blacklist excluded 1", " | ".join(captured.output))


class _Stream:
    broadcast_id = "b1"


class _Channel:
    def __init__(self, *, accepted: bool):
        self.name = "somechannel"
        self.online = True
        self._stream = _Stream()
        self.send_watch = AsyncMock(return_value=accepted)


class _WatchTwitch:
    """Only what ``watch_loop`` touches before its first long sleep."""

    def __init__(self, channel: _Channel):
        from src.utils import AwaitableValue

        self.watching_channel: AwaitableValue = AwaitableValue()
        self.watching_channel.set(channel)
        self._watching_restart = asyncio.Event()
        self._idle_channels_set: set = set()
        self.settings = MagicMock(idle_parallel=False)
        self.gui = MagicMock()
        self.last_watch_ok: datetime | None = None


class TestTheWatchStampRecordsProgressNotConfiguration(unittest.IsolatedAsyncioTestCase):
    """``last_watch_ok`` is the only proof the miner is still getting through.

    Driven through the real ``watch_loop`` rather than asserted on its source:
    the guard is *where* the assignment sits relative to ``if not succeeded``,
    and a source check cannot tell a stamp on the success branch from a stamp
    above the branch.
    """

    async def run_one_pass(self, *, accepted: bool) -> _WatchTwitch:
        channel = _Channel(accepted=accepted)
        twitch = _WatchTwitch(channel)
        service = WatchService(twitch)
        task = asyncio.create_task(service.watch_loop())
        # The loop parks on a 20-second sleep immediately after the stamp point.
        for _ in range(20):
            await asyncio.sleep(0)
            if channel.send_watch.await_count:
                break
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(channel.send_watch.await_count, 1)
        return twitch

    async def test_an_accepted_watch_is_stamped(self):
        before = datetime.now(timezone.utc)
        twitch = await self.run_one_pass(accepted=True)

        self.assertIsNotNone(twitch.last_watch_ok)
        self.assertGreaterEqual(twitch.last_watch_ok, before)
        self.assertEqual(twitch.last_watch_ok.tzinfo, timezone.utc)

    async def test_a_refused_watch_is_not_stamped(self):
        # THE guard. A stamp on the failure path would make the health probe
        # report a healthy miner for precisely the outage it exists to catch:
        # a dead proxy or a rejected token still returns from send_watch.
        twitch = await self.run_one_pass(accepted=False)

        self.assertIsNone(twitch.last_watch_ok)

    async def test_a_refused_watch_does_not_clear_an_earlier_success(self):
        # The age is what the probe reads, so a single failure must not reset it
        # to "never watched" - that is a different, quieter alarm.
        channel = _Channel(accepted=False)
        twitch = _WatchTwitch(channel)
        earlier = datetime.now(timezone.utc) - timedelta(minutes=5)
        twitch.last_watch_ok = earlier
        service = WatchService(twitch)
        task = asyncio.create_task(service.watch_loop())
        for _ in range(20):
            await asyncio.sleep(0)
            if channel.send_watch.await_count:
                break
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(twitch.last_watch_ok, earlier)

    def test_the_client_starts_with_no_stamp_at_all(self):
        # None means "never watched", which the probe treats as a boot grace
        # period rather than as a stall. A datetime default would make every
        # restart look like a healthy miner.
        import inspect

        from src.core.client import Twitch

        self.assertIn(
            "self.last_watch_ok: datetime | None = None",
            inspect.getsource(Twitch.__init__),
        )


if __name__ == "__main__":
    unittest.main()
