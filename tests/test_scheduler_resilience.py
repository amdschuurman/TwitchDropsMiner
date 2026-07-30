"""The scheduler is the one task whose death stops mining outright.

Nothing else in the application lifts a pause the scheduler placed. So if
``run_scheduler``'s loop ever exits, a miner it had already paused stays paused
for the rest of the process - with no console line, and with
``GET /api/health/mining`` still answering ``ok``, because a paused miner is a
legitimate state and the probe cannot tell the two apart. The tests below are
that asymmetry, one guard at a time:

* the loop body is wrapped, so a failed check costs one check rather than the
  task,
* a check that cannot run does not walk away holding a pause only it can lift
  (:meth:`~src.services.scheduler_service.SchedulerService._release_scheduler_pause`),
* switching the scheduler off releases the pause it was holding, which used to
  strand the miner until somebody pressed resume by hand,
* an unreadable window (``""``, ``"22"``, ``"25:00"``, ``"banana"`` - all
  reachable through a hand-edited settings.json) is reported and released
  instead of raising out of the loop,
* a permanent fault is reported once instead of once a minute forever,
* and the death of the task itself is now surfaced by
  :mod:`src.services.task_supervision` instead of vanishing into asyncio's
  never-retrieved-exception message at interpreter shutdown.

Every guard here fails towards MINING. Resuming early inside a quiet window
costs one window and re-pauses on the next healthy check; the other way round
costs every drop until somebody restarts the container.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import unittest
from datetime import time
from unittest.mock import patch

from src.services.scheduler_service import _CHECK_INTERVAL, SchedulerService
from src.services.task_supervision import log_task_death


class _Settings:
    """Only the four fields a scheduler check reads."""

    def __init__(self, **overrides):
        self.scheduler_enabled = True
        self.scheduler_start = "22:00"
        self.scheduler_stop = "08:00"
        self.__dict__.update(overrides)


class _FakeTwitch:
    """The pause/resume surface, with the bookkeeping the scheduler inspects."""

    def __init__(self, settings: _Settings):
        self.settings = settings
        self._is_paused = False
        self._pause_source: str | None = None
        self._user_override = False
        self.pause_sources: list[str] = []
        self.resume_calls = 0

    def is_paused(self) -> bool:
        return self._is_paused

    def pause(self, source: str = "user") -> None:
        self.pause_sources.append(source)
        self._is_paused = True
        self._pause_source = source

    def resume(self, *, user_override: bool = False) -> None:
        self.resume_calls += 1
        self._is_paused = False
        self._pause_source = None


class _ExplodingSettingsTwitch(_FakeTwitch):
    """Reading ``settings`` raises - an unforeseen fault inside the check.

    The point is that the guard is not a list of the failures somebody thought
    of: the loop wrapper has to survive the one nobody predicted, because that
    is the class of bug that killed the task.
    """

    @property
    def settings(self):
        raise RuntimeError("settings blew up")

    @settings.setter
    def settings(self, value):
        self._settings = value


class SchedulerTestBase(unittest.IsolatedAsyncioTestCase):
    """Runs the REAL ``run_scheduler`` loop, briefly, and then cancels it.

    Deliberately the whole loop rather than ``_check`` alone: the try/except and
    the release-on-failure both live in the loop body, so calling ``_check``
    directly would test everything except the guard.
    """

    async def settle(self) -> None:
        """Let the loop run until it parks on its 60-second wait."""
        await asyncio.sleep(0.01)

    def start(self, twitch) -> SchedulerService:
        """Start the real loop and make sure it is cancelled afterwards."""
        service = SchedulerService(twitch)
        task = asyncio.create_task(service.run_scheduler())
        self.task = task
        self.addCleanup(task.cancel)
        return service

    async def run_scheduler(self, twitch) -> SchedulerService:
        """One pass of the real loop, asserting the task survived it."""
        service = self.start(twitch)
        await self.settle()
        self.assertFalse(
            self.task.done(),
            f"run_scheduler exited: "
            f"{self.task.exception() if self.task.done() else None!r}",
        )
        return service

    async def trigger_again(self, service: SchedulerService) -> None:
        """Ask a still-running service for one more immediate check."""
        service.trigger_check()
        await self.settle()


class TestAFailedCheckCostsOneCheckNotTheTask(SchedulerTestBase):
    """The loop body used to be bare."""

    async def test_an_unforeseen_failure_does_not_kill_the_loop(self):
        twitch = _ExplodingSettingsTwitch(_Settings())
        twitch._is_paused, twitch._pause_source = True, "scheduler"

        with self.assertLogs("TwitchDrops", level=logging.DEBUG):
            # run_scheduler asserts the task is still alive before cancelling.
            await self.run_scheduler(twitch)

    async def test_an_unforeseen_failure_releases_the_pause_it_was_holding(self):
        twitch = _ExplodingSettingsTwitch(_Settings())
        twitch._is_paused, twitch._pause_source = True, "scheduler"

        with self.assertLogs("TwitchDrops", level=logging.DEBUG):
            await self.run_scheduler(twitch)

        self.assertFalse(twitch.is_paused())
        self.assertEqual(twitch.resume_calls, 1)

    async def test_a_failure_never_releases_a_pause_the_operator_asked_for(self):
        # "user" is not the scheduler's to lift. Resuming somebody's deliberate
        # pause because an unrelated read failed is its own outage.
        twitch = _ExplodingSettingsTwitch(_Settings())
        twitch._is_paused, twitch._pause_source = True, "user"

        with self.assertLogs("TwitchDrops", level=logging.DEBUG):
            await self.run_scheduler(twitch)

        self.assertTrue(twitch.is_paused())
        self.assertEqual(twitch._pause_source, "user")
        self.assertEqual(twitch.resume_calls, 0)

    async def test_a_failure_with_nothing_paused_resumes_nothing(self):
        twitch = _ExplodingSettingsTwitch(_Settings())

        with self.assertLogs("TwitchDrops", level=logging.DEBUG):
            await self.run_scheduler(twitch)

        self.assertEqual(twitch.resume_calls, 0)

    async def test_the_failure_is_reported_with_the_retry_interval(self):
        twitch = _ExplodingSettingsTwitch(_Settings())

        with self.assertLogs("TwitchDrops", level=logging.ERROR) as captured:
            await self.run_scheduler(twitch)

        reported = " | ".join(captured.output)
        self.assertIn("Scheduler check failed", reported)
        self.assertIn(f"{_CHECK_INTERVAL:.0f}s", reported)


class TestAnUnreadableWindowIsReleasedNotRaised(SchedulerTestBase):
    """``_parse_time`` raises on values a hand-edited settings.json can hold.

    ``""`` and ``"banana"`` raise ValueError from ``int``, ``"22"`` used to raise
    IndexError, ``"25:00"`` raises ValueError from ``time``. Each of them used to
    leave the loop for good.
    """

    UNREADABLE = ("", "22", "25:00", "banana", "-1:00", "22:00:30")

    async def test_every_unreadable_start_leaves_the_task_alive(self):
        for value in self.UNREADABLE:
            with self.subTest(scheduler_start=value):
                twitch = _FakeTwitch(_Settings(scheduler_start=value))
                with self.assertLogs("TwitchDrops", level=logging.ERROR):
                    await self.run_scheduler(twitch)

    async def test_every_unreadable_stop_leaves_the_task_alive(self):
        for value in self.UNREADABLE:
            with self.subTest(scheduler_stop=value):
                twitch = _FakeTwitch(_Settings(scheduler_stop=value))
                with self.assertLogs("TwitchDrops", level=logging.ERROR):
                    await self.run_scheduler(twitch)

    async def test_an_unreadable_window_releases_a_scheduler_pause(self):
        twitch = _FakeTwitch(_Settings(scheduler_start="banana"))
        twitch.pause(source="scheduler")

        with self.assertLogs("TwitchDrops", level=logging.ERROR):
            await self.run_scheduler(twitch)

        self.assertFalse(twitch.is_paused())

    async def test_an_unreadable_window_leaves_a_user_pause_alone(self):
        twitch = _FakeTwitch(_Settings(scheduler_start="banana"))
        twitch.pause(source="user")

        with self.assertLogs("TwitchDrops", level=logging.ERROR):
            await self.run_scheduler(twitch)

        self.assertTrue(twitch.is_paused())
        self.assertEqual(twitch._pause_source, "user")

    async def test_an_unreadable_window_never_pauses_a_running_miner(self):
        # A window nobody can read is not a reason to stop mining.
        twitch = _FakeTwitch(_Settings(scheduler_stop=""))

        with self.assertLogs("TwitchDrops", level=logging.ERROR):
            await self.run_scheduler(twitch)

        self.assertEqual(twitch.pause_sources, [])

    async def test_the_report_names_both_settings_and_the_consequence(self):
        # The loop wrapper would catch the raise too, but it can only say
        # "Scheduler check failed: ValueError(...)". Naming the two settings and
        # what stops happening is the difference between a line the operator can
        # act on and one they cannot.
        twitch = _FakeTwitch(_Settings(scheduler_start="banana"))

        with self.assertLogs("TwitchDrops", level=logging.ERROR) as captured:
            await self.run_scheduler(twitch)

        reported = " | ".join(captured.output)
        self.assertIn("scheduler_start", reported)
        self.assertIn("scheduler_stop", reported)
        self.assertIn("Scheduler window unusable", reported)

    async def test_the_release_says_it_was_the_window(self):
        twitch = _FakeTwitch(_Settings(scheduler_stop="25:00"))
        twitch.pause(source="scheduler")

        with self.assertLogs("TwitchDrops", level=logging.WARNING) as captured:
            await self.run_scheduler(twitch)

        self.assertIn("its window can no longer be read", " | ".join(captured.output))


class TestSwitchingTheSchedulerOffReleasesItsOwnPause(SchedulerTestBase):
    """The branch that used to be a bare ``return``.

    Turning the scheduler off while it held the pause stranded the miner: no
    other code path clears a scheduler-sourced pause, so it sat there until
    somebody noticed.
    """

    async def test_the_pause_it_placed_is_lifted(self):
        twitch = _FakeTwitch(_Settings(scheduler_enabled=False))
        twitch.pause(source="scheduler")

        with self.assertLogs("TwitchDrops", level=logging.WARNING):
            await self.run_scheduler(twitch)

        self.assertFalse(twitch.is_paused())
        self.assertEqual(twitch.resume_calls, 1)

    async def test_a_user_pause_is_still_left_alone(self):
        twitch = _FakeTwitch(_Settings(scheduler_enabled=False))
        twitch.pause(source="user")

        await self.run_scheduler(twitch)

        self.assertTrue(twitch.is_paused())
        self.assertEqual(twitch.resume_calls, 0)

    async def test_with_nothing_paused_it_resumes_nothing(self):
        twitch = _FakeTwitch(_Settings(scheduler_enabled=False))

        await self.run_scheduler(twitch)

        self.assertEqual(twitch.resume_calls, 0)
        self.assertEqual(twitch.pause_sources, [])

    async def test_the_release_says_why_out_loud(self):
        twitch = _FakeTwitch(_Settings(scheduler_enabled=False))
        twitch.pause(source="scheduler")

        with self.assertLogs("TwitchDrops", level=logging.WARNING) as captured:
            await self.run_scheduler(twitch)

        self.assertIn("switched off", " | ".join(captured.output))


class TestAPermanentFaultIsReportedOnce(SchedulerTestBase):
    """A check runs every minute and these faults do not heal on their own."""

    async def test_the_same_failure_is_not_re_reported_every_pass(self):
        twitch = _FakeTwitch(_Settings(scheduler_start="banana"))
        service = self.start(twitch)

        with self.assertLogs("TwitchDrops", level=logging.DEBUG) as captured:
            await self.settle()
            for _ in range(5):
                await self.trigger_again(service)

        errors = [line for line in captured.output if line.startswith("ERROR")]
        self.assertEqual(len(errors), 1, errors)

    async def test_a_repaired_window_reports_that_checks_are_running_again(self):
        twitch = _FakeTwitch(_Settings(scheduler_start="banana"))
        service = self.start(twitch)
        await self.settle()

        twitch.settings.scheduler_start = "22:00"
        with self.assertLogs("TwitchDrops", level=logging.INFO) as captured:
            await self.trigger_again(service)

        self.assertIn("checks are running again", " | ".join(captured.output))
        self.assertIsNone(service._reported_failure)

    async def test_a_recurrence_after_a_repair_is_reported_again(self):
        twitch = _FakeTwitch(_Settings(scheduler_start="banana"))
        service = self.start(twitch)
        await self.settle()

        twitch.settings.scheduler_start = "22:00"
        await self.trigger_again(service)
        twitch.settings.scheduler_start = "banana"

        with self.assertLogs("TwitchDrops", level=logging.ERROR) as captured:
            await self.trigger_again(service)

        self.assertIn("Scheduler window unusable", " | ".join(captured.output))


class TestTheScheduleItselfStillWorks(SchedulerTestBase):
    """None of the guards may cost the behaviour they protect."""

    async def test_inside_the_window_nothing_is_paused(self):
        twitch = _FakeTwitch(_Settings(scheduler_start="00:00", scheduler_stop="23:59"))

        await self.run_scheduler(twitch)

        self.assertEqual(twitch.pause_sources, [])

    async def test_outside_the_window_the_scheduler_still_pauses(self):
        twitch = _FakeTwitch(_Settings(scheduler_start="00:00", scheduler_stop="00:01"))

        await self.run_scheduler(twitch)

        self.assertEqual(twitch.pause_sources, ["scheduler"])

    async def test_back_inside_the_window_the_scheduler_still_resumes(self):
        twitch = _FakeTwitch(_Settings(scheduler_start="00:00", scheduler_stop="23:59"))
        twitch.pause(source="scheduler")

        await self.run_scheduler(twitch)

        self.assertFalse(twitch.is_paused())

    async def test_a_user_override_still_blocks_a_scheduled_pause(self):
        twitch = _FakeTwitch(_Settings(scheduler_start="00:00", scheduler_stop="00:01"))
        twitch._user_override = True

        await self.run_scheduler(twitch)

        self.assertEqual(twitch.pause_sources, [])

    def test_a_good_boundary_still_parses(self):
        service = SchedulerService(_FakeTwitch(_Settings()))
        self.assertEqual(service._parse_time("07:05"), time(7, 5))


class TestATaskDeathIsReported(unittest.IsolatedAsyncioTestCase):
    """``src/services/task_supervision.py`` - the module with no tests at all.

    A bare ``create_task`` swallows the outcome: if the coroutine raises, the
    only trace is asyncio's "Task exception was never retrieved" on stderr at
    interpreter shutdown, which no log file keeps and nobody reads.
    """

    async def test_a_task_that_raises_is_logged_by_name(self):
        async def boom():
            raise RuntimeError("gone")

        task = asyncio.create_task(boom())
        task.add_done_callback(log_task_death("Scheduler task"))

        with self.assertLogs("TwitchDrops", level=logging.ERROR) as captured:
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        reported = " | ".join(captured.output)
        self.assertIn("Scheduler task died", reported)
        self.assertIn("RuntimeError", reported)

    async def test_the_traceback_is_kept_not_just_the_repr(self):
        async def boom():
            raise RuntimeError("gone")

        task = asyncio.create_task(boom())
        task.add_done_callback(log_task_death("Scheduler task"))

        with self.assertLogs("TwitchDrops", level=logging.ERROR) as captured:
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        self.assertIsNotNone(captured.records[0].exc_info)

    async def test_a_cancelled_task_is_not_reported_as_dead(self):
        # Cancellation is how Twitch.shutdown and the re-arm path in _run()
        # retire this task on purpose; reporting it would make the real line
        # unfindable.
        async def sleeper():
            await asyncio.sleep(10)

        task = asyncio.create_task(sleeper())
        task.add_done_callback(log_task_death("Scheduler task"))

        with self.assertNoLogs("TwitchDrops", level=logging.DEBUG):
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    async def test_the_callback_itself_survives_a_cancelled_task(self):
        # task.exception() RAISES CancelledError on a cancelled task, so an
        # unguarded callback throws inside the event loop's done-callback
        # machinery - which is reported through asyncio's own exception handler,
        # not through this application's log, and so is invisible in exactly the
        # place the guard is supposed to make things visible.
        async def sleeper():
            await asyncio.sleep(10)

        task = asyncio.create_task(sleeper())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        log_task_death("Scheduler task")(task)  # must not raise

    async def test_a_task_that_simply_finishes_is_not_reported(self):
        async def done():
            return None

        task = asyncio.create_task(done())
        task.add_done_callback(log_task_death("Scheduler task"))

        with self.assertNoLogs("TwitchDrops", level=logging.DEBUG):
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    async def test_the_death_of_the_real_scheduler_coroutine_is_reported(self):
        # End to end on the coroutine the callback is actually attached to. The
        # loop's own try/except is what keeps this from happening in practice,
        # so the failure is forced OUTSIDE the guarded body: the loop's foot
        # only catches TimeoutError, so anything else from the wait exits the
        # task exactly as an unguarded check used to.
        def explode(awaitable, *args, **kwargs):
            # Close the coroutine the loop just built, or it surfaces later as a
            # "never awaited" RuntimeWarning against an unrelated test.
            awaitable.close()
            raise RuntimeError("event loop gone")

        service = SchedulerService(_FakeTwitch(_Settings()))
        task = asyncio.create_task(service.run_scheduler())
        task.add_done_callback(log_task_death("Scheduler task"))

        with patch("asyncio.wait_for", side_effect=explode), self.assertLogs(
            "TwitchDrops", level=logging.ERROR
        ) as captured, self.assertRaises(RuntimeError):
            await task

        self.assertIn("Scheduler task died", " | ".join(captured.output))


class TestTheSchedulerTaskIsActuallySupervised(unittest.TestCase):
    """The callback has to be attached where the task is created.

    Asserted against the source of ``Twitch._run`` rather than by constructing a
    ``Twitch`` (which needs a GUI, an auth state, a websocket and a live event
    loop): what has to hold is a wiring fact, and this is the cheapest honest
    way to state it. Behaviour of the callback itself is covered above.
    """

    def source(self) -> str:
        from src.core.client import Twitch

        return inspect.getsource(Twitch._run)

    def test_the_scheduler_task_gets_a_death_callback(self):
        source = self.source()
        creation = source.index("self._scheduler_task = asyncio.create_task(")
        callback = source.index("self._scheduler_task.add_done_callback(log_task_death(")

        self.assertLess(
            creation,
            callback,
            "the death callback must be attached to the task that was just created",
        )

    def test_the_callback_names_the_task_it_guards(self):
        # "Task died" with no name is a line nobody can act on.
        self.assertIn('log_task_death("Scheduler task")', self.source())


if __name__ == "__main__":
    unittest.main()
