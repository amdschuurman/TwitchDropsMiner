"""Settings manager for application configuration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from src.config.settings import (
    MINING_BENEFIT_KEYS,
    ClockTime,
    LanguageNormalizer,
    MiningBenefitsFloor,
    MiningFloor,
    WatchlistFloor,
)
from src.i18n.translator import _
from src.models.game import Game


logger = logging.getLogger("TwitchDrops")

# Sentinel for "this attribute did not exist yet", so a rejected write can be
# rolled back to genuinely-absent instead of to None.
_MISSING = object()

# Request-only keys (see SettingsManager._consume_mining_intents): they travel on
# the /api/settings payload but are NEVER persisted settings. Every one of them
# is popped off a copy of the payload before any per-key write runs, so none can
# be setattr-ed onto Settings or serialized into settings.json.
_ALLOW_EMPTY_GAMES_KEY = "allow_empty_games_to_watch"
_ALLOW_EMPTY_BENEFITS_KEY = "allow_empty_mining_benefits"
_EXPECTED_GAMES_KEY = "expected_games_to_watch"
_REQUEST_ONLY_KEYS = (_ALLOW_EMPTY_GAMES_KEY, _ALLOW_EMPTY_BENEFITS_KEY, _EXPECTED_GAMES_KEY)


if TYPE_CHECKING:
    from src.config.settings import Settings
    from src.web.managers.broadcaster import WebSocketBroadcaster
    from src.web.managers.console import ConsoleOutputManager


class _EmptyingGuard:
    """The payload-side half of one "emptying this setting stops mining" rule.

    Every guarded setting has two halves. The save-side floor
    (:class:`~src.config.settings.MiningFloor`) protects what is ON DISK from a
    value that emptied with no request behind it. This half protects the
    IN-MEMORY value from a request that empties it without saying so - which
    matters because the in-memory value is what the miner reads, what the
    dashboard is shown, and what ``_on_change`` recomputes ``wanted_games`` from.

    The two halves share :attr:`FLOOR`, so "this value would leave nothing to
    mine" has exactly one definition. Letting them drift would mean a payload
    this half waves through and the floor then silently refuses: an HTTP 200
    reporting a change that never reached disk.

    Used as classes, not instances - there is one of each and they hold no state.
    """

    FLOOR: ClassVar[type[MiningFloor]]
    # The request-only flag that authorises the emptying, the Settings method
    # that passes that authorisation down to the save, and the line logged when
    # the emptying is refused instead.
    ALLOW_KEY: ClassVar[str]
    DECLARE: ClassVar[str]
    REFUSAL: ClassVar[str]

    @classmethod
    def resolve(cls, incoming: Any, stored: Any) -> Any:
        """What the payload really asks this setting to become."""
        return incoming

    @classmethod
    def refuse(cls, request: dict[str, Any], stored: Any) -> str | None:
        """``None`` to allow the emptying, or the line explaining the refusal."""
        if not request.get(cls.ALLOW_KEY):
            return cls.REFUSAL
        return None


class _WatchlistGuard(_EmptyingGuard):
    """``games_to_watch`` may only be emptied by a client that is up to date.

    The intent flag alone was not enough. ``saveSettings`` posts the client's
    ENTIRE list as authoritative, and the four single-game-removal gestures all
    carry the flag so that removing your last game works - so a tab that loaded
    while the list was ``['A']``, clicking the last ✕ it can see, sends exactly
    the same request as a user deliberately clearing a three-game list. Three
    games are wiped and the server declares the intent on the client's behalf,
    because from the payload alone the two are identical.

    So the destructive case - and ONLY the destructive case - also has to state
    what the client believed was stored. A non-empty ``games_to_watch`` is
    untouched by this, so normal saves cost nothing.
    """

    FLOOR = WatchlistFloor
    ALLOW_KEY = _ALLOW_EMPTY_GAMES_KEY
    DECLARE = "declare_empty_watchlist_intent"
    REFUSAL = "Refused to clear the games-to-watch list without explicit intent"

    @classmethod
    def refuse(cls, request: dict[str, Any], stored: Any) -> str | None:
        refusal = super().refuse(request, stored)
        if refusal is not None:
            return refusal
        current = list(stored or [])
        if not current:
            # Nothing is stored, so nothing can be lost and no client can be
            # stale about it. Demanding a match here would refuse the very first
            # "clear all" of a fresh install for no gain.
            return None
        expected = request.get(_EXPECTED_GAMES_KEY)
        if isinstance(expected, list) and expected == current:
            return None
        # Compared in ORDER, not as a set: the order of this list is the mining
        # order (src/services/stream_selector.py builds the wanted tree from it),
        # so a client holding the same names in a different order is looking at a
        # different setting and is, by definition, not the one that wrote it.
        seen = (
            f"expected {expected} to be stored"
            if isinstance(expected, list)
            else f"did not say which list it believed was stored ({_EXPECTED_GAMES_KEY} was "
            f"{expected!r})"
        )
        return (
            f"Refused to clear the games-to-watch list: this request {seen}, but the stored "
            f"list is {current}. The page that sent it is out of date, so clearing the list "
            "would delete games it never showed. Reload the page and try again."
        )


class _BenefitsGuard(_EmptyingGuard):
    """``mining_benefits`` is merged, not replaced, and may not be emptied unasked.

    Two separate holes, both closed here.

    Replacement was the quiet one. ``Benefit.is_wanted`` is fail-CLOSED
    (``allowed_benefits.get(name, False)``), so a key the payload omits is not
    "leave it as it was", it is "turn it off" - and the dashboard builds this
    dict from four ``document.getElementById(...)?.checked`` reads, each of
    which yields ``undefined`` for an element that has not rendered, which
    ``JSON.stringify`` then drops. A partial payload therefore disabled benefit
    types nobody touched. Merging into the stored selection makes an absent key
    mean "not supplied", which is the meaning ``check_and_update_setting``
    already gives to every other absent value.

    Emptying is the loud one made quiet: with every type disabled no drop is
    wanted, no game is selected, and mining stops as completely as it does with
    an empty watch list - while the Settings tab still lists every game. It stays
    possible, because "stop mining but keep my watch list" is a real thing to
    want, but it has to be asked for.
    """

    FLOOR = MiningBenefitsFloor
    ALLOW_KEY = _ALLOW_EMPTY_BENEFITS_KEY
    DECLARE = "declare_disabled_benefits_intent"
    REFUSAL = "Refused to disable every mining benefit type without explicit intent"

    @classmethod
    def resolve(cls, incoming: Any, stored: Any) -> Any:
        if not isinstance(incoming, dict):
            return incoming
        merged: dict[str, Any] = dict(stored) if isinstance(stored, dict) else {}
        # Keys outside the known benefit types are not settings and are dropped
        # silently, exactly as merge_json drops them on the next load; letting
        # them in would put junk in settings.json until that load happened.
        merged.update(
            (key, value)
            for key, value in incoming.items()
            if key in MINING_BENEFIT_KEYS and isinstance(value, bool)
        )
        return merged


class SettingsManager:
    """Manages application settings in the web interface.

    Provides access to and modification of user preferences including
    game priorities, proxy configuration, and UI preferences.
    """

    # Every payload-side "emptying this stops the miner" rule, in the order
    # _consume_mining_intents applies them.
    _GUARDS: tuple[type[_EmptyingGuard], ...] = (_WatchlistGuard, _BenefitsGuard)

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

    def _consume_mining_intents(self, settings_data: dict[str, Any]) -> dict[str, Any]:
        """Strip the request-only keys and refuse unintended mining-stopping writes.

        This is the server-side floor that protects the miner from ANY client,
        current or future. Two settings can silently stop all mining by going
        empty - ``games_to_watch`` (no game is ever selected) and
        ``mining_benefits`` (no drop is ever wanted, so no game is ever
        selected) - so a page load, a socket reconnect or an over-eager cleanup
        script must never be able to write either of them empty. Both stay
        emptiable: the caller just has to say so explicitly, in the SAME request,
        with ``allow_empty_games_to_watch`` / ``allow_empty_mining_benefits``
        (which is what the UI's "clear all" and "disable everything" gestures
        do). ``games_to_watch`` additionally has to prove it is not stale - see
        :class:`_WatchlistGuard`.

        Why it lives here and not in the endpoint: every settings write funnels
        through ``update_settings``, so this is the single choke point that no
        writer can go around.

        The request-only keys are popped off a COPY of the payload before any
        per-key update runs, which makes it impossible-by-construction for one
        of them to be ``setattr``-ed onto ``Settings`` or serialized into
        settings.json. They are popped whether or not their guard fires.

        There is a SECOND floor under this one, on the save side
        (``Settings.save`` and the ``declare_*_intent`` methods in
        ``src/config/settings.py``): it refuses to write a mine-nothing value
        over a stored one that still enables mining, unless this request
        declared the intent. So when the user really is clearing something, the
        intent has to be handed down - otherwise the save-side floor restores the
        stored value and the user's deliberate gesture silently does nothing.
        This method is the only place that knows the difference, which is why it
        is the only place that declares it. Each declaring method is named
        differently from its payload key on purpose: the keys must never exist as
        attributes of ``Settings``, or they would be serialized into
        settings.json as settings.

        Args:
            settings_data: Raw settings payload from the caller.

        Returns:
            A copy of the payload without the request-only keys, with
            ``mining_benefits`` merged onto the stored selection, and without any
            guarded key whose emptying was not explicitly (and credibly)
            requested.
        """
        settings_data = dict(settings_data)
        request = {key: settings_data.pop(key, None) for key in _REQUEST_ONLY_KEYS}
        for guard in self._GUARDS:
            key = guard.FLOOR.KEY
            incoming = settings_data.get(key)
            if incoming is None:
                continue  # not supplied by this request; nothing to guard
            stored = getattr(self._settings, key, None)
            value = guard.resolve(incoming, stored)
            settings_data[key] = value
            if not guard.FLOOR.enables_nothing(value):
                continue
            refusal = guard.refuse(request, stored)
            if refusal is None:
                # Narrow on purpose: only the payload that actually empties the
                # setting declares anything. The declaration is consumed by the
                # next save, so granting it for a request that does not empty
                # anything would spend it on a save nobody vetted.
                getattr(self._settings, guard.DECLARE)()
                continue
            # Drop the key entirely: the per-key updater treats a missing value
            # as "not supplied", so every OTHER key in this request still lands.
            del settings_data[key]
            if not guard.FLOOR.enables_nothing(stored):
                # Only a value that actually still enabled mining was protected.
                # On a fresh install the stored watch list is already empty, so
                # the same payload refuses nothing - warning about it there just
                # trains the operator to ignore the line that matters.
                self._log_change(refusal)
                logger.warning(refusal)
        return settings_data

    def update_settings(self, settings_data: dict[str, Any]):
        """Update settings from user input.

        Args:
            settings_data: Dictionary of settings to update
        """
        settings_data = self._consume_mining_intents(settings_data)
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
        # Already MERGED onto the stored selection by _consume_mining_intents, so
        # this applies "the four flags as they now are", not "the flags this
        # payload happened to mention". The equality short-circuit in
        # check_and_update_setting then makes a payload that changes nothing a
        # genuine no-op: no console line, no should_trigger_update.
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
        self.check_and_update_setting(
            "scheduler_start",
            settings_data.get("scheduler_start"),
            validator=self._validate_clock_time,
        )
        self.check_and_update_setting(
            "scheduler_stop",
            settings_data.get("scheduler_stop"),
            validator=self._validate_clock_time,
        )
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

    def _validate_clock_time(self, value: Any):
        """Reject a scheduler boundary the scheduler cannot parse, before it is stored.

        ``SchedulerService._parse_time`` has no guard of its own and nothing
        catches what it raises, so one ``"banana"`` (or ``""``, ``"22"``,
        ``"24:00"``, ``"-1:00"``) accepted here kills the scheduler task for the
        rest of the process - and a miner it had already paused then never
        resumes. See :class:`~src.config.settings.ClockTime`, which owns the
        parse so this validator and the scheduler cannot drift apart.
        """
        ClockTime.parse(value)

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
