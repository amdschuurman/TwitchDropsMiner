"""Settings manager for application configuration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.config.settings import LanguageNormalizer
from src.i18n.translator import _
from src.models.game import Game


logger = logging.getLogger("TwitchDrops")

# Sentinel for "this attribute did not exist yet", so a rejected write can be
# rolled back to genuinely-absent instead of to None.
_MISSING = object()

# Request-only intent flag (see SettingsManager.update_settings): it travels on
# the /api/settings payload but is NEVER a persisted setting.
_ALLOW_EMPTY_GAMES_KEY = "allow_empty_games_to_watch"


if TYPE_CHECKING:
    from src.config.settings import Settings
    from src.web.managers.broadcaster import WebSocketBroadcaster
    from src.web.managers.console import ConsoleOutputManager


class SettingsManager:
    """Manages application settings in the web interface.

    Provides access to and modification of user preferences including
    game priorities, proxy configuration, and UI preferences.
    """

    def __init__(
        self,
        broadcaster: WebSocketBroadcaster,
        settings: Settings,
        console: ConsoleOutputManager,
        on_change: Callable[[], None] | None = None,
        on_scheduler_change: Callable[[], None] | None = None,
        on_predictions_enable: Callable[[], None] | None = None,
    ):
        self._broadcaster = broadcaster
        self._settings = settings
        self._console = console
        self._on_change = on_change
        self._on_scheduler_change = on_scheduler_change
        self._on_predictions_enable = on_predictions_enable
        self._available_games: list[str] = []

    def get_settings(self) -> dict[str, Any]:
        """Get current settings for display.

        Returns:
            Dictionary containing all user-configurable settings, plus the
            available-games list so the UI can render the games-to-watch
            picker before the first ``games_available`` socket emit fires.
        """
        # Underscore-prefixed attributes are bookkeeping, not settings:
        # ``_empty_watchlist_declared`` lives in ``vars()`` between a failed
        # declared save and the next successful one, and shipping it to the UI
        # would contradict the promise that it is never observable as a setting.
        settings = {
            key: value for key, value in vars(self._settings).items() if not key.startswith("_")
        }
        # Always include the watch list so manually-added games persist in
        # the dropdown across reloads even before the next inventory fetch
        # has broadcast a ``games_available`` event.
        from_watchlist = set(settings.get("games_to_watch") or [])
        from_known = set(self._available_games)
        settings["games_available"] = sorted(from_known | from_watchlist)
        return settings

    def get_languages(self) -> dict[str, Any]:
        """Get available languages and current selection.

        Returns:
            Dictionary with available languages and current language
        """
        return {
            "available": _.get_languages(),
            "current": _.current_language,
        }

    def _log_change(self, message: str):
        """Log setting change to both console and system logger."""
        self._console.print(message)

    def _consume_empty_watchlist_intent(self, settings_data: dict[str, Any]) -> dict[str, Any]:
        """Strip the request-only intent flag and refuse unintended watch-list wipes.

        This is the server-side floor that protects the miner from ANY client,
        current or future: an empty ``games_to_watch`` means "mine nothing", so
        a page load, a socket reconnect or an over-eager cleanup script must
        never be able to write it. Emptying the list stays possible - the
        caller just has to say so explicitly by sending
        ``allow_empty_games_to_watch: true`` in the SAME request (that is what
        the UI's "clear all" gesture does).

        Why it lives here and not in the endpoint: every settings write funnels
        through ``update_settings``, so this is the single choke point that no
        writer can go around.

        The intent flag itself is popped off a COPY of the payload before any
        per-key update runs, which makes it impossible-by-construction for it
        to be ``setattr``-ed onto ``Settings`` or serialized into settings.json.

        There is a SECOND floor under this one, on the save side
        (``Settings.save`` / ``Settings.declare_empty_watchlist_intent`` in
        ``src/config/settings.py``): it refuses to write ``[]`` over a non-empty
        stored list unless this request declared the intent. So when the user
        really is clearing the list, the intent has to be handed down - otherwise
        the save-side floor restores the stored list and the user's deliberate
        "clear all" silently does nothing. This method is the only place that
        knows the difference, which is why it is the only place that declares it.
        The declaring method is named differently from the payload key on purpose:
        ``allow_empty_games_to_watch`` must never exist as an attribute of
        ``Settings``, or it would be serialized into settings.json as a setting.

        Args:
            settings_data: Raw settings payload from the caller.

        Returns:
            A copy of the payload without the intent flag, and without
            ``games_to_watch`` when clearing it was not explicitly requested.
        """
        settings_data = dict(settings_data)
        allow_empty = bool(settings_data.pop(_ALLOW_EMPTY_GAMES_KEY, None))
        games = settings_data.get("games_to_watch")
        if allow_empty and isinstance(games, list) and not games:
            # Narrow on purpose: only the payload that actually empties the list
            # declares anything. The declaration is consumed by the next save, so
            # granting it for a request that does not clear the list would spend
            # it on a save nobody vetted.
            self._settings.declare_empty_watchlist_intent()
        if not allow_empty and isinstance(games, list) and not games:
            # Drop the key entirely: the per-key updater treats a missing value
            # as "not supplied", so every OTHER key in this request still lands.
            del settings_data["games_to_watch"]
            if getattr(self._settings, "games_to_watch", None):
                # Only a list that actually HAD games was protected from being
                # wiped. On a fresh install the stored list is already empty, so
                # the same payload refuses nothing - warning about it there just
                # trains the operator to ignore the line that matters.
                message = "Refused to clear the games-to-watch list without explicit intent"
                self._log_change(message)
                logger.warning(message)
        return settings_data

    def update_settings(self, settings_data: dict[str, Any]):
        """Update settings from user input.

        Args:
            settings_data: Dictionary of settings to update
        """
        settings_data = self._consume_empty_watchlist_intent(settings_data)
        should_trigger_update = False
        should_trigger_update |= self.check_and_update_setting(
            "games_to_watch", settings_data.get("games_to_watch"), True
        )
        should_trigger_update |= self.check_and_update_setting(
            "dark_mode", settings_data.get("dark_mode")
        )
        should_trigger_update |= self.check_and_update_setting(
            "language",
            settings_data.get("language"),
            False,
            self._set_language,
            self._validate_language,
        )
        should_trigger_update |= self.check_and_update_setting(
            "connection_quality", settings_data.get("connection_quality")
        )
        if "proxy" in settings_data:
            proxy_value = settings_data["proxy"]
            should_trigger_update |= self.check_and_update_setting(
                "proxy",
                str(proxy_value).strip() if proxy_value else "",
                True,
                # Returns its own console line instead of printing one, so the
                # generic "Setting changed: proxy = " it replaces is not also
                # emitted — see check_and_update_setting.
                lambda proxy: "Proxy cleared" if proxy == "" else None,
            )
        should_trigger_update |= self.check_and_update_setting(
            "minimum_refresh_interval_minutes",
            settings_data.get("minimum_refresh_interval_minutes"),
        )
        should_trigger_update |= self.check_and_update_setting(
            "inventory_filters", settings_data.get("inventory_filters")
        )
        should_trigger_update |= self.check_and_update_setting(
            "mining_benefits", settings_data.get("mining_benefits"), True
        )
        self.check_and_update_setting(
            "claim_channel_points", settings_data.get("claim_channel_points")
        )
        self.check_and_update_setting(
            "idle_channels", settings_data.get("idle_channels")
        )
        self.check_and_update_setting(
            "idle_use_followed", settings_data.get("idle_use_followed")
        )
        self.check_and_update_setting(
            "idle_parallel", settings_data.get("idle_parallel")
        )
        self.check_and_update_setting(
            "preferred_games", settings_data.get("preferred_games")
        )
        self.check_and_update_setting("scheduler_enabled", settings_data.get("scheduler_enabled"))
        self.check_and_update_setting("scheduler_start", settings_data.get("scheduler_start"))
        self.check_and_update_setting("scheduler_stop", settings_data.get("scheduler_stop"))
        if any(k in settings_data for k in ("scheduler_enabled", "scheduler_start", "scheduler_stop")):
            if self._on_scheduler_change:
                self._on_scheduler_change()
        self.check_and_update_setting(
            "discord_webhook_drops", settings_data.get("discord_webhook_drops")
        )
        self.check_and_update_setting(
            "discord_webhook_points", settings_data.get("discord_webhook_points")
        )
        self.check_and_update_setting(
            "discord_webhook_mentions", settings_data.get("discord_webhook_mentions")
        )
        self.check_and_update_setting(
            "drop_name_blacklist", settings_data.get("drop_name_blacklist")
        )
        self.check_and_update_setting(
            "auto_prioritize", settings_data.get("auto_prioritize")
        )
        self.check_and_update_setting(
            "auto_add_linked", settings_data.get("auto_add_linked")
        )
        self.check_and_update_setting(
            "auto_clean_watchlist", settings_data.get("auto_clean_watchlist")
        )
        self.check_and_update_setting(
            "tab_counter_enabled", settings_data.get("tab_counter_enabled")
        )
        prev_predictions = self._settings.make_predictions
        self.check_and_update_setting(
            "make_predictions", settings_data.get("make_predictions")
        )
        if not prev_predictions and self._settings.make_predictions and self._on_predictions_enable:
            self._on_predictions_enable()
        self.check_and_update_setting(
            "bet_strategy", settings_data.get("bet_strategy")
        )
        self.check_and_update_setting(
            "bet_percentage", settings_data.get("bet_percentage")
        )
        self.check_and_update_setting(
            "bet_max_points", settings_data.get("bet_max_points")
        )
        self.check_and_update_setting(
            "bet_minimum_points", settings_data.get("bet_minimum_points")
        )
        self.check_and_update_setting(
            "bet_percentage_gap", settings_data.get("bet_percentage_gap")
        )
        self.check_and_update_setting(
            "bet_delay_seconds", settings_data.get("bet_delay_seconds")
        )
        self.check_and_update_setting(
            "prediction_channels", settings_data.get("prediction_channels")
        )
        self.check_and_update_setting(
            "channel_strategies", settings_data.get("channel_strategies")
        )
        self.check_and_update_setting(
            "claim_moments", settings_data.get("claim_moments")
        )
        self.check_and_update_setting(
            "irc_chat_presence", settings_data.get("irc_chat_presence")
        )
        self.check_and_update_setting(
            "irc_mention_notify", settings_data.get("irc_mention_notify")
        )

        self._settings.save()
        asyncio.create_task(self._broadcaster.emit("settings_updated", self.get_settings()))

        if should_trigger_update and self._on_change:
            self._on_change()

    def check_and_update_setting(
        self,
        key: str,
        new_value: Any,
        should_trigger_update: bool = False,
        action: Callable[[Any], str | None] = lambda x: None,
        validator: Callable[[Any], None] | None = None,
    ):
        """Apply one setting, rejecting bad values WITHOUT mutating anything.

        Order of operations matters: ``validator`` runs before the ``setattr``,
        so a value the application cannot accept never reaches the in-memory
        ``Settings`` object. Previously the mutation happened first and a
        raising ``action`` left the rejected value behind while aborting the
        whole request before ``save()`` - memory poisoned, disk still clean,
        until the next shutdown save wrote the poison out.

        The success line is logged LAST, only once ``action`` has returned. It
        used to be logged before the action ran, so a write the action went on to
        reject printed ``Setting changed: language = ar`` immediately followed by
        ``Setting rejected: language = ar (...)`` - two contradictory lines about
        one key, which from the outside is indistinguishable from the settings
        bug this whole change exists to remove. Nothing is announced until it
        actually held.

        A rejected key is logged and skipped; the remaining keys of the same
        request are still applied by the caller.

        Args:
            key: Settings attribute name.
            new_value: Candidate value; ``None`` means "not supplied".
            should_trigger_update: Returned when the value actually changed.
            action: Side effect to run AFTER a successful mutation. It may return
                a console line to log INSTEAD of the generic
                ``Setting changed: <key> = <value>`` one (``None`` keeps the
                generic line), which is how a setting says something more useful
                than its raw value without printing a second, redundant line.
            validator: Optional check that raises when ``new_value`` is invalid.

        Returns:
            ``should_trigger_update`` when the value changed and was accepted,
            ``False`` on a no-op or a rejection.
        """
        if new_value is None or getattr(self._settings, key, None) == new_value:
            return False
        if validator is not None:
            try:
                validator(new_value)
            except Exception as exc:
                self._log_rejection(key, new_value, exc)
                return False
        previous = getattr(self._settings, key, _MISSING)
        setattr(self._settings, key, new_value)
        try:
            message = action(new_value)
        except Exception as exc:
            # Belt and braces for actions without a validator: roll the value
            # back so a failed side effect can never leave the in-memory
            # settings holding something the app rejected, and keep going with
            # the rest of the request instead of 500-ing the whole write.
            try:
                if previous is _MISSING:
                    delattr(self._settings, key)
                else:
                    setattr(self._settings, key, previous)
            except AttributeError:
                pass  # nothing to restore; the rejection log below still fires
            self._log_rejection(key, new_value, exc)
            return False
        # Only now is the change real: value stored, side effect done.
        self._log_change(message or f"Setting changed: {key} = {new_value}")
        return should_trigger_update

    def _log_rejection(self, key: str, new_value: Any, exc: BaseException):
        """Report a refused setting write to console and log, and mutate nothing."""
        message = f"Setting rejected: {key} = {new_value} ({exc})"
        self._log_change(message)
        logger.error(message)

    def _validate_language(self, language: Any):
        """Reject a language the translator cannot load, before it is stored.

        Guards the exact failure that poisoned the settings in production: the
        web UI POSTing ``language: ""`` from a ``<select>`` whose options had
        not been populated yet.

        The accept-set comes from ``LanguageNormalizer`` (which derives it from
        the translator) rather than from ``_.get_languages()`` alone: that list
        holds language NAMES only, while ``Translator.set_language`` also accepts
        the locale codes in its ``_LOCALE_MAP`` (``"en"``, ``"de"``, ...). A
        validator narrower than the setter it guards rejects payloads the app can
        handle perfectly well.
        """
        if not LanguageNormalizer.accepts(language):
            raise ValueError(f"Unrecognized language {language!r}")

    def _set_language(self, language: str):
        _.set_language(language)
        # Notify clients that translations need to be reloaded
        asyncio.create_task(self._broadcaster.emit("language_changed", {"language": language}))

    def set_games(self, games: set[Game]):
        """Update the list of available games for settings panel.

        Includes:
        - Games discovered from currently visible Twitch drop campaigns
        - Every entry the user already has on their watch list (so manually-added
          games like an unreleased War Thunder drop persist in the dropdown
          across restarts, even before a matching Twitch campaign appears)

        Args:
            games: Set of Game objects discovered from campaigns
        """
        from_campaigns = {g.name for g in games}
        from_watchlist = set(getattr(self._settings, "games_to_watch", []) or [])
        game_names = sorted(from_campaigns | from_watchlist)
        self._available_games = game_names
        asyncio.create_task(self._broadcaster.emit("games_available", {"games": game_names}))
