"""Discord webhook payload construction.

One place builds every webhook body this application sends, so the optional
``discord_ping_mention`` setting reaches all of them (drop claims, channel
points, chat mentions, prediction results, campaign-ending alerts) instead of
being wired into five senders by hand.

The mention is a plain Discord mention string the operator configures per
instance, for example ``<@251794990462795776>`` for a user or ``<@&id>`` for a
role. It is NOT a credential, unlike the ``discord_webhook_*`` URLs it travels
with. When it is empty the payload is exactly the embeds-only body that was
sent before this setting existed, so an unconfigured install is unchanged.

``allowed_mentions`` is set to ``users``/``roles`` only when a mention is
actually present: it both makes the ping fire and keeps ``@everyone``/``@here``
inside an embed or a chat message quoted in one from ever pinging the server.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def build_webhook_payload(embeds: Iterable[dict[str, Any]], mention: str = "") -> dict[str, Any]:
    """Return the JSON body for a Discord webhook POST.

    ``embeds`` is the embed list that used to be passed as ``{"embeds": [...]}``.
    ``mention`` is ``Settings.discord_ping_mention``; anything falsy or
    whitespace-only leaves the body untouched.
    """
    payload: dict[str, Any] = {"embeds": list(embeds)}
    text = (mention or "").strip() if isinstance(mention, str) else ""
    if text:
        payload["content"] = text
        payload["allowed_mentions"] = {"parse": ["users", "roles"]}
    return payload
