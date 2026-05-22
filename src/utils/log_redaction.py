"""Logging filter that redacts known sensitive token patterns from log records.

This is a defense in depth — secrets should never reach logs in the first place,
but a filter catches accidental DEBUG-level dumps of headers / response bodies
that would otherwise leak the Twitch ``auth-token`` to ``logs/TDM.log``.
"""

from __future__ import annotations

import logging
import re
from typing import Final


# Patterns are ordered: most specific (header-style) first, then generic
# key=value JSON patterns. Each match group 1 is preserved (key/prefix) and
# the secret-bearing tail is replaced with ``***``.
_REDACTORS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # Authorization: OAuth <token>  or  Authorization: Bearer <token>
    (
        re.compile(r"(Authorization\s*[:=]\s*['\"]?(?:OAuth|Bearer)\s+)[\w.\-]+", re.IGNORECASE),
        r"\1***",
    ),
    # auth-token: <token> (header form)
    (re.compile(r"(auth[-_]token['\"]?\s*[:=]\s*['\"]?)[\w.\-]+", re.IGNORECASE), r"\1***"),
    # "access_token": "..."  /  access_token=...
    (re.compile(r"(['\"]?access_token['\"]?\s*[:=]\s*['\"]?)[\w.\-]+", re.IGNORECASE), r"\1***"),
    # "refresh_token": "..."
    (re.compile(r"(['\"]?refresh_token['\"]?\s*[:=]\s*['\"]?)[\w.\-]+", re.IGNORECASE), r"\1***"),
    # "device_code": "..."  (OAuth device flow)
    (re.compile(r"(['\"]?device_code['\"]?\s*[:=]\s*['\"]?)[\w.\-]+", re.IGNORECASE), r"\1***"),
    # client_secret (defensive — TDM doesn't use one today)
    (re.compile(r"(['\"]?client_secret['\"]?\s*[:=]\s*['\"]?)[\w.\-]+", re.IGNORECASE), r"\1***"),
)


def _redact(text: str) -> str:
    for pattern, repl in _REDACTORS:
        text = pattern.sub(repl, text)
    return text


class SecretRedactingFilter(logging.Filter):
    """Scrub secret-bearing substrings from log records before emission.

    Applied to the handlers that write to stdout and ``logs/TDM.log``; child
    loggers (``TwitchDrops.gql``, ``TwitchDrops.websocket``) propagate through
    the same handlers so the filter covers them automatically.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        # The record may not have been formatted yet (lazy %-formatting), so
        # we redact both the raw msg and the eventual args.
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: _redact(v) if isinstance(v, str) else v for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(_redact(v) if isinstance(v, str) else v for v in record.args)
        return True
