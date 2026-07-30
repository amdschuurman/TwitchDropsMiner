"""A scheduler boundary the scheduler cannot parse stops mining, silently.

``SchedulerService._parse_time`` is ``time(int(parts[0]), int(parts[1]))`` with
no guard, and nothing catches what it raises. ``"banana"`` and ``""`` raise
ValueError from ``int``, ``"22"`` raises IndexError, ``"24:00"`` and ``"-1:00"``
raise ValueError from ``time``. Each of those takes the exception straight out
of ``run_scheduler``'s loop, which kills the task for the rest of the process -
it is started once at ``src/core/client.py:297`` and never restarted.

If the scheduler had already paused the miner, nothing is left to resume it.
Mining stops until somebody restarts the container, with no console line, and
``GET /api/health/mining`` still answers ``ok`` because a paused miner is a
legitimate state. The value is reachable through ``POST /api/settings`` (the
payload model types these as plain strings) and through a hand edit, which
``merge_json`` waves through because ``str`` is the right type.

So it is checked where the language is checked: at the write boundary before it
is stored, and again at load, because ``check_and_update_setting``
short-circuits on equality and a poisoned stored value is therefore never
revalidated by the UI posting it back.
"""

import json
import tempfile
import unittest
from datetime import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.config import settings as settings_module
from src.config.settings import ClockTime, Settings, default_settings
from src.services.scheduler_service import SchedulerService
from tests.test_settings_api import RealSettingsFileTestBase, SettingsManagerTestBase


# Every value that killed the scheduler task, with what it raised on the way.
UNPARSEABLE = {
    "banana": "int() rejects it",
    "": "the <select>-posted-empty shape that already hit `language`",
    "22": "no minute field at all - IndexError, not ValueError",
    "24:00": "an hour that is not a time of day",
    "-1:00": "a negative hour",
    "22:60": "a minute that is not a minute",
    "22:00:30": "three fields; the parser would take the first two",
}

PARSEABLE = ["00:00", "08:00", "22:00", "23:59", " 22:00 "]


class TestClockTimeAndTheSchedulerCannotDrift(unittest.TestCase):
    """One parse, used by the validator, the load repair and the scheduler.

    A validator more permissive than the parser it guards is not a guard, it is
    a second place for the same crash to come from. So ``SchedulerService``
    delegates to ``ClockTime`` rather than doing its own ``int()`` splitting,
    and this asserts that property against the real service rather than
    trusting the arrangement.
    """

    def setUp(self):
        self.service = SchedulerService(MagicMock())

    def test_every_accepted_value_parses_in_the_service(self):
        for value in PARSEABLE:
            with self.subTest(value=value):
                self.assertTrue(ClockTime.accepts(value))
                self.assertEqual(self.service._parse_time(value), ClockTime.parse(value))

    def test_every_rejected_value_is_rejected_by_both(self):
        for value, why in UNPARSEABLE.items():
            with self.subTest(value=value, why=why):
                self.assertFalse(ClockTime.accepts(value))
                with self.assertRaises(ValueError):
                    ClockTime.parse(value)
                # Hand-rolled here, this was IndexError for "22" and ValueError
                # for the rest, which is why nothing could catch it in one place.
                with self.assertRaises(ValueError):
                    self.service._parse_time(value)

    def test_a_non_string_is_rejected_rather_than_crashing_the_validator(self):
        for value in (None, 22, ["22:00"], {"hour": 22}):
            with self.subTest(value=value):
                self.assertFalse(ClockTime.accepts(value))

    def test_the_parse_returns_a_real_time(self):
        self.assertEqual(ClockTime.parse("08:05"), time(8, 5))


class TestSchedulerBoundariesAreValidatedOnWrite(SettingsManagerTestBase):
    """``POST /api/settings`` may not store a boundary that kills the task."""

    def setUp(self):
        super().setUp()
        self.settings.scheduler_enabled = True
        self.settings.scheduler_start = "22:00"
        self.settings.scheduler_stop = "08:00"

    def rejections(self) -> list[str]:
        return self.lines_starting("Setting rejected: scheduler_")

    def test_every_unparseable_start_is_rejected(self):
        for value, why in UNPARSEABLE.items():
            with self.subTest(value=value, why=why):
                self.console.reset_mock()

                self.manager.update_settings({"scheduler_start": value})

                self.assertEqual(self.settings.scheduler_start, "22:00")
                self.assertEqual(len(self.rejections()), 1)

    def test_every_unparseable_stop_is_rejected(self):
        for value, why in UNPARSEABLE.items():
            with self.subTest(value=value, why=why):
                self.console.reset_mock()

                self.manager.update_settings({"scheduler_stop": value})

                self.assertEqual(self.settings.scheduler_stop, "08:00")
                self.assertEqual(len(self.rejections()), 1)

    def test_a_real_boundary_is_still_accepted(self):
        self.manager.update_settings({"scheduler_start": "23:30", "scheduler_stop": "07:15"})

        self.assertEqual(self.settings.scheduler_start, "23:30")
        self.assertEqual(self.settings.scheduler_stop, "07:15")
        self.assertEqual(self.rejections(), [])

    def test_a_rejected_boundary_leaves_the_rest_of_the_request_applied(self):
        self.manager.update_settings({"scheduler_start": "banana", "dark_mode": True})

        self.assertEqual(self.settings.scheduler_start, "22:00")
        self.assertIs(self.settings.dark_mode, True)

    def test_a_rejected_boundary_does_not_switch_the_scheduler_off(self):
        # Rejecting one value may not mutate a different setting the operator
        # really did choose; the next save would persist that loss.
        self.manager.update_settings({"scheduler_start": "banana"})

        self.assertIs(self.settings.scheduler_enabled, True)


class TestPoisonedBoundariesAreRepairedAtLoad(RealSettingsFileTestBase):
    """A stored bad boundary can never heal itself, so load has to do it.

    ``check_and_update_setting`` short-circuits when the incoming value equals
    the stored one, and the UI only ever posts back what it was given, so a
    poisoned value survives every restart. ``merge_json`` does not catch it
    either: it enforces the TYPE of a key, and ``"banana"`` is a valid ``str``.
    Same reasoning, same fix as ``LanguageNormalizer.repair``.
    """

    def load(self, **overrides) -> Settings:
        self.seed(**overrides)
        return Settings()

    def test_an_unparseable_start_falls_back_to_the_default(self):
        for value in UNPARSEABLE:
            with self.subTest(value=value):
                settings = self.load(scheduler_start=value)

                self.assertEqual(settings.scheduler_start, default_settings["scheduler_start"])

    def test_an_unparseable_stop_falls_back_to_the_default(self):
        for value in UNPARSEABLE:
            with self.subTest(value=value):
                settings = self.load(scheduler_stop=value)

                self.assertEqual(settings.scheduler_stop, default_settings["scheduler_stop"])

    def test_a_valid_boundary_is_left_exactly_as_stored(self):
        settings = self.load(scheduler_start="23:30", scheduler_stop="07:15")

        self.assertEqual(settings.scheduler_start, "23:30")
        self.assertEqual(settings.scheduler_stop, "07:15")

    def test_the_repair_is_reported_with_its_key_and_its_consequence(self):
        self.seed(scheduler_start="banana")

        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            Settings()

        line = captured.output[0]
        self.assertIn("scheduler_start", line)
        self.assertIn("'banana'", line)
        # The operator has to be told why this one mattered, not just that a
        # value was replaced.
        self.assertIn("kills the scheduler task", line)

    def test_the_repair_does_not_touch_scheduler_enabled(self):
        settings = self.load(scheduler_enabled=True, scheduler_start="banana")

        self.assertIs(settings.scheduler_enabled, True)

    def test_the_repair_is_persisted_by_the_next_save(self):
        self.seed(games_to_watch=["Overwatch"], scheduler_stop="25:00")
        settings = Settings()

        settings.save()

        self.assertEqual(self.stored()["scheduler_stop"], default_settings["scheduler_stop"])

    def test_a_repaired_boundary_lets_a_scheduler_check_run(self):
        # The end of the story: the value that used to kill the task is gone, so
        # a check reads a real window and pauses or resumes on it.
        settings = self.load(
            scheduler_enabled=True, scheduler_start="banana", scheduler_stop="08:00"
        )
        twitch = MagicMock()
        twitch.settings = settings
        twitch.is_paused.return_value = False
        twitch._user_override = False

        SchedulerService(twitch)._check()

        self.assertEqual(settings.scheduler_start, default_settings["scheduler_start"])


class TestBoundariesSurviveTheWholeRoundTrip(unittest.IsolatedAsyncioTestCase):
    """Write, save, reload: the value the operator picked is the value stored."""

    def setUp(self):
        patcher = patch("asyncio.create_task", side_effect=lambda coro: coro.close())
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "settings.json"
        seed = dict(default_settings) | {"games_to_watch": ["Overwatch"]}
        self.path.write_text(json.dumps(seed), encoding="utf-8")
        path_patcher = patch.object(settings_module, "SETTINGS_PATH", self.path)
        path_patcher.start()
        self.addCleanup(path_patcher.stop)

    def manager(self, settings):
        from unittest.mock import AsyncMock

        from src.web.managers.settings import SettingsManager

        return SettingsManager(AsyncMock(), settings, MagicMock())

    async def test_a_bad_boundary_never_reaches_the_file(self):
        settings = Settings()

        self.manager(settings).update_settings({"scheduler_start": "banana"})

        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["scheduler_start"], default_settings["scheduler_start"])

    async def test_a_good_boundary_reaches_the_file_and_comes_back(self):
        settings = Settings()

        self.manager(settings).update_settings({"scheduler_start": "23:30"})

        self.assertEqual(Settings().scheduler_start, "23:30")


if __name__ == "__main__":
    unittest.main()
