"""Report background tasks that die, instead of letting them disappear."""

from __future__ import annotations

import asyncio
import logging
from collections import abc
from typing import Any


logger = logging.getLogger("TwitchDrops")


def log_task_death(name: str) -> abc.Callable[[asyncio.Task[Any]], None]:
    """Build an ``add_done_callback`` that surfaces an unexpected task exit.

    A bare ``asyncio.create_task`` swallows the outcome. If the coroutine
    raises, the only trace is asyncio's "Task exception was never retrieved" on
    stderr at interpreter shutdown - which nobody reads and no log file keeps -
    while whatever the task was responsible for has silently stopped happening.
    For the two long-lived tasks this is attached to, that means the periodic
    inventory reload or the pause/resume schedule simply ceasing, with the
    application otherwise looking healthy.

    Cancellation is not a death: it is how ``Twitch.shutdown`` and the re-arm
    paths retire a task on purpose.
    """

    def _report(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("%s died: %r", name, exc, exc_info=exc)

    return _report
