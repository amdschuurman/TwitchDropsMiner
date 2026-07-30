"""Scheduler service for automatic pause/resume based on time of day."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time
from typing import TYPE_CHECKING

from src.config.settings import ClockTime

if TYPE_CHECKING:
    from src.core.client import Twitch

logger = logging.getLogger("TwitchDrops")

# How long a check waits before re-running, when nothing triggers it sooner.
_CHECK_INTERVAL: float = 60


class SchedulerService:
    """Pauses and resumes mining on a daily window.

    This task is the only thing that ever lifts a pause it placed, which makes
    it the one background task whose death stops mining outright: a miner the
    scheduler had already paused stays paused for the rest of the process, with
    no console line, and ``GET /api/health/mining`` still answering ``ok`` -
    a paused miner is a legitimate state, so the probe cannot tell the two
    apart. Every guard below exists for that one asymmetry, and each of them
    fails towards mining rather than towards staying paused.
    """

    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch
        self._trigger: asyncio.Event | None = None
        # What the last failed check complained about, so a permanent fault is
        # reported once instead of once a minute forever.
        self._reported_failure: str | None = None

    def _parse_time(self, time_str: str) -> time:
        """Delegate to ``ClockTime`` so parser and validator cannot drift apart.

        ``POST /api/settings`` rejects a boundary this cannot read, and
        ``Settings.load`` repairs a stored one, both through ``ClockTime``. Doing
        the parsing here by hand is what let ``""``, ``"22"`` and ``"25:00"``
        through in the first place, so it is done in exactly one place now.
        """
        return ClockTime.parse(time_str)

    def _window(self) -> tuple[time, time] | None:
        """The configured active window, or ``None`` if a boundary is unreadable."""
        settings = self._twitch.settings
        try:
            return (
                self._parse_time(settings.scheduler_start),
                self._parse_time(settings.scheduler_stop),
            )
        except ValueError as exc:
            self._report_failure(
                f"window:{exc}",
                f"Scheduler window unusable: {exc}. Mining will not be paused or resumed on "
                "a schedule until scheduler_start and scheduler_stop are valid HH:MM times",
            )
            return None

    def _should_be_paused(self, window: tuple[time, time]) -> bool:
        start, stop = window
        now = datetime.now().time()
        if start < stop:
            return not (start <= now < stop)
        else:
            return not (now >= start or now < stop)

    def _release_scheduler_pause(self, reason: str) -> None:
        """Lift a pause the scheduler placed, once it can no longer lift it later.

        Only ever touches a pause whose source is the scheduler itself. A pause
        the operator asked for (``_pause_source == "user"``) is theirs and is
        left exactly where it is - this resumes nothing a person chose, it only
        refuses to walk away holding a pause that nothing else knows how to
        release.
        """
        if self._twitch.is_paused() and self._twitch._pause_source == "scheduler":
            logger.warning(f"Scheduler: resuming mining because {reason}")
            self._twitch.resume()

    def _report_failure(self, key: str, message: str) -> None:
        """Report a check failure at ERROR the first time, quietly afterwards.

        A check runs every minute and the conditions that reach here do not heal
        on their own, so reporting every pass would bury the line that matters
        under a thousand copies of itself.
        """
        if self._reported_failure == key:
            logger.debug(message)
            return
        self._reported_failure = key
        logger.error(message)

    def _clear_failure(self) -> None:
        if self._reported_failure is not None:
            logger.info("Scheduler: the previous failure is gone, checks are running again")
            self._reported_failure = None

    def _check(self) -> None:
        """One scheduler pass. Its only caller, :meth:`run_scheduler`, guards it."""
        twitch = self._twitch
        if not twitch.settings.scheduler_enabled:
            # Switching the scheduler off while it holds the pause used to strand
            # it: this branch simply returned, and nothing else in the
            # application clears a scheduler-sourced pause. The miner sat paused
            # until somebody noticed and pressed resume by hand.
            self._release_scheduler_pause("the scheduler was switched off while it held the pause")
            return
        window = self._window()
        if window is None:
            self._release_scheduler_pause("its window can no longer be read")
            return
        self._clear_failure()
        should_pause = self._should_be_paused(window)
        logger.info(
            f"Scheduler check: should_pause={should_pause}, is_paused={twitch.is_paused()}, "
            f"override={twitch._user_override}"
        )
        if should_pause and not twitch.is_paused() and not twitch._user_override:
            logger.info("Scheduler: pausing mining (outside active window)")
            twitch.pause(source="scheduler")
        elif not should_pause and twitch.is_paused() and twitch._pause_source == "scheduler":
            logger.info("Scheduler: resuming mining (entering active window)")
            twitch.resume()

    def trigger_check(self) -> None:
        """Trigger an immediate scheduler check."""
        if self._trigger is not None:
            self._trigger.set()

    async def run_scheduler(self) -> None:
        self._trigger = asyncio.Event()  # create inside running loop
        logger.info("Scheduler service started")
        while True:
            try:
                self._check()
            except Exception as exc:
                # The loop body used to be bare, so a single bad read took the
                # whole task down for the rest of the process - and with it the
                # only code that can lift a scheduler pause. A failed check now
                # costs one check.
                self._report_failure(
                    repr(exc),
                    f"Scheduler check failed, retrying in {_CHECK_INTERVAL:.0f}s: {exc!r}",
                )
                # A check that cannot run must not be left holding a pause only
                # it can lift. Resuming early inside a quiet window costs one
                # window and re-pauses on the next healthy check; the other way
                # round costs every drop until somebody restarts the container.
                try:
                    self._release_scheduler_pause("its checks are failing")
                except Exception:
                    logger.exception("Scheduler could not resume mining after a failed check")
            self._trigger.clear()
            try:
                await asyncio.wait_for(self._trigger.wait(), timeout=_CHECK_INTERVAL)
            except asyncio.TimeoutError:
                pass
