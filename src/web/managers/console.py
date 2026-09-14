"""Console output manager for logging to web interface."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from src.web.managers.broadcaster import WebSocketBroadcaster


import logging

from src.utils.log_redaction import redact_secrets


logger = logging.getLogger("TwitchDrops")


class ConsoleOutputManager:
    """Manages console output display in the web interface.

    Buffers log messages and broadcasts them to connected clients in real-time,
    maintaining a rolling history of recent messages.
    """

    def __init__(self, broadcaster: WebSocketBroadcaster, max_lines: int = 1000):
        self._broadcaster = broadcaster
        self._buffer: deque[str] = deque(maxlen=max_lines)

    def print(self, message: str):
        """Print a message to the console output with timestamp.

        The line is redacted BEFORE it is buffered. This buffer is served
        verbatim by ``GET /api/console`` and broadcast over the
        ``console_output`` websocket event, neither of which passes through
        the logging handlers that carry :class:`SecretRedactingFilter`, so
        without this call a credential printed here would reach the dashboard
        and the log file even when the filter is installed.

        Args:
            message: The message to display
        """
        message = redact_secrets(message)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] | {message}"
        self._buffer.append(line)
        asyncio.create_task(self._broadcaster.emit("console_output", {"message": line}))
        logger.info(message)

    def get_history(self) -> list[str]:
        """Get the current console history buffer.

        Returns:
            List of timestamped console messages
        """
        return list(self._buffer)
