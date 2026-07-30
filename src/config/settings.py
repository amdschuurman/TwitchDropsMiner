from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import time
from typing import Any, ClassVar, TypedDict, cast

from src.config import DEFAULT_LANG, LANG_PATH, SETTINGS_PATH, JsonType
from src.utils import json_load, json_save


logger = logging.getLogger("TwitchDrops")

# The settings whose empty value silently disables the whole application, so the
# ones that get a floor on the write side (see Settings.save and MiningFloor).
_WATCHLIST_KEY = "games_to_watch"
_BENEFITS_KEY = "mining_benefits"

# Handed to json_load as a guarded key's default value, so that "the file could
# not be read" is distinguishable from "the file says this key is empty":
# json_load returns the defaults it was given on an unparseable file, and no value
# decoded from JSON can ever BE this object (see Settings._stored_value).
_UNREADABLE = object()


class InventoryFilters(TypedDict):
    game_name_search: list[str]
    show_active: bool
    show_benefit_badge: bool
    show_benefit_emote: bool
    show_benefit_item: bool
    show_benefit_other: bool
    show_expired: bool
    show_finished: bool
    show_linked: bool
    show_not_linked: bool
    show_sub_drops: bool
    show_upcoming: bool


default_settings = {
    "connection_quality": 1,
    "dark_mode": False,
    "games_to_watch": [],
    "language": DEFAULT_LANG,
    "inventory_filters": {
        "game_name_search": [],
        "show_active": False,
        "show_benefit_badge": True,
        "show_benefit_emote": True,
        "show_benefit_item": True,
        "show_benefit_other": True,
        "show_expired": False,
        "show_finished": False,
        "show_sub_drops": False,
        "show_linked": True,
        "show_not_linked": True,
        "show_upcoming": True,
    },
    "minimum_refresh_interval_minutes": 30,
    "mining_benefits": {
        "BADGE": True,
        "DIRECT_ENTITLEMENT": True,
        "EMOTE": True,
        "UNKNOWN": True,
    },
    "proxy": "",
    "claim_channel_points": True,
    "idle_channels": [],
    "idle_parallel": True,
    "idle_use_followed": False,
    "preferred_games": [],
    "scheduler_enabled": False,
    "scheduler_start": "22:00",
    "scheduler_stop": "08:00",
    "discord_webhook_drops": "",
    "discord_webhook_points": "",
    "drop_name_blacklist": [],
    "auto_prioritize": False,
    "auto_add_linked": False,
    # Opt-in only: when True the web UI is allowed to prune fully-claimed games
    # from games_to_watch on its own. Defaults to False because an unattended
    # auto-clean can empty the watch list and silently stop all mining.
    "auto_clean_watchlist": False,
    "tab_counter_enabled": True,
    "claim_moments": True,
    "irc_chat_presence": True,
    "discord_webhook_mentions": "",
    "irc_mention_notify": True,
    "make_predictions": False,
    "bet_strategy": "SMART",
    "bet_percentage": 5,
    "bet_max_points": 50000,
    "bet_minimum_points": 1000,
    "bet_percentage_gap": 20,
    "bet_delay_seconds": 30,
    "prediction_channels": [],
    "channel_strategies": {},
}

# The benefit types a ``mining_benefits`` selection may name, taken from the
# defaults so there is exactly one list of them in the codebase. They are the
# ``BenefitType`` member names (src/models/benefit.py), which is what
# ``Benefit.is_wanted`` looks itself up by; ``merge_json`` already enforces this
# exact key set on every load, so a key outside it is not a setting at all.
MINING_BENEFIT_KEYS: frozenset[str] = frozenset(cast(JsonType, default_settings[_BENEFITS_KEY]))


class LanguageNormalizer:
    """Answers two DIFFERENT questions about a language string, on purpose.

    "Can the translator load this right now?" (:meth:`accepts`) and "is this a
    real language this install ships?" (:meth:`knows`) are not the same question,
    and answering both with one set caused a bug in each direction:

    - ``accepts`` guards a write, so it must be exactly as wide as
      ``Translator.set_language`` - no wider, no narrower. That setter maps
      locale codes through ``_LOCALE_MAP`` *before* the lookup, so ``"en"`` is as
      loadable as ``"English"`` and a validator that only knew the names rejected
      payloads the app handles fine. But a mapped code is only loadable if its
      TARGET actually loaded: ``accepts("ar")`` was True while
      ``set_language("ar")`` raised ``Unrecognized language العربية`` whenever
      that one file failed to parse, so the codes are intersected with the names
      the translator really holds.
    - ``knows`` guards a repair, so it must NOT depend on which files happened to
      parse during this process's import. ``Translator.__init__`` skips a file it
      cannot read with a ``continue``, and it runs once per process, so a single
      transient bad read used to make :meth:`repair` overwrite a perfectly
      legitimate stored ``"Nederlandse"`` with the default - and the next save
      wrote that loss to disk, unrecoverably. Existence of ``lang/<name>.json``
      is the parse-independent fact, so that is what a repair decision uses.

    The translator is imported lazily inside the methods on purpose:
    ``src.i18n.translator`` builds a module-level ``Translator`` that reads every
    file in ``lang/``, and importing ``src.config.settings`` must not pay for
    that (nor risk an import cycle, since the translator imports ``src.config``).
    """

    @staticmethod
    def _locale_map() -> dict[str, str]:
        """The translator's code -> language-name map, read live (never copied)."""
        from src.i18n.translator import Translator

        return Translator._LOCALE_MAP

    @staticmethod
    def loaded() -> set[str]:
        """Language names the translator actually holds in memory this run."""
        from src.i18n.translator import _

        return set(_.get_languages())

    @staticmethod
    def installed() -> set[str]:
        """Language names that have a file in ``lang/``, parsed or not.

        Every shipped translation stores its own ``language_name`` under that
        same name (``lang/Nederlandse.json`` -> ``"Nederlandse"``), so the stems
        are the name set - available without opening a single file, which is the
        whole point: this must keep working for a file that cannot be parsed.
        """
        try:
            return {path.stem for path in LANG_PATH.glob("*.json")}
        except OSError:
            # An unreadable lang/ must not make every stored language "unknown".
            return set()

    @classmethod
    def _with_codes(cls, names: set[str]) -> set[str]:
        """``names`` plus every locale code that maps into ``names``."""
        return names | {code for code, name in cls._locale_map().items() if name in names}

    @classmethod
    def accepted(cls) -> set[str]:
        """Every string ``Translator.set_language`` accepts right now."""
        return cls._with_codes(cls.loaded())

    @classmethod
    def known(cls) -> set[str]:
        """Every string that names a language this install ships."""
        return cls._with_codes(cls.installed() | cls.loaded())

    @classmethod
    def accepts(cls, language: Any) -> bool:
        """True when ``language`` can be handed to the translator as-is."""
        return isinstance(language, str) and language in cls.accepted()

    @classmethod
    def knows(cls, language: Any) -> bool:
        """True when ``language`` names a real language, however this boot went."""
        return isinstance(language, str) and language in cls.known()

    @classmethod
    def repair(cls, language: Any) -> str:
        """Return a real language, falling back to the default out loud.

        The live deployment really did end up with ``language: ""`` on disk (a
        ``<select>`` POSTed before its options existed). Nothing healed it: the
        equality short-circuit in ``check_and_update_setting`` means a stored
        value is never re-validated, so the miner kept reloading the broken one
        on every start. Repairing at load closes that loop - the value is fixed
        in memory now and rewritten on the next save.

        Deliberately judged by :meth:`knows`, not :meth:`accepts`: this repair is
        DESTRUCTIVE (the next save persists it), so it may only fire on a value
        that is blank or names no language at all. A language whose file exists
        but did not parse this boot is kept exactly as stored - the caller of
        ``set_language`` handles that as a temporary fallback (see
        ``src/__main__.py``) instead of throwing the choice away for good.
        """
        if cls.knows(language):
            return cast(str, language)
        logger.warning(
            f"Unusable language {language!r} in settings, falling back to {DEFAULT_LANG}"
        )
        return DEFAULT_LANG

    @classmethod
    def apply(cls, language: str) -> str:
        """Set the UI language best-effort, and return the one now in effect.

        The backstop for startup: ``Translator.set_language`` raises on anything
        it cannot load, and the boot path called it bare and outside any
        try/except, so one unreadable file in ``lang/`` took the whole process
        down - which under ``restart: unless-stopped`` is a container restart
        loop, not a translation problem. A language is never worth a boot
        failure, so a failure here is a warning and the translator keeps the
        language it already has (its constructor sets the default).

        Deliberately does NOT correct the stored setting: the value may name a
        real language whose file merely failed to parse this run, and rewriting
        it would let the next save destroy the operator's choice for good -
        exactly the loss :meth:`repair` now refuses to cause.
        """
        from src.i18n.translator import _

        try:
            _.set_language(language)
        except ValueError as exc:
            logger.warning(
                f"Could not load language {language!r} ({exc}); continuing in "
                f"{_.current_language} for this run and keeping the stored choice"
            )
        return _.current_language


class ClockTime:
    """A ``HH:MM`` scheduler boundary, checked where it is written.

    The third way a settings write silently stops all mining, and the loudest
    one to reproduce: ``SchedulerService._parse_time`` does
    ``time(int(parts[0]), int(parts[1]))`` with no guard, and nothing catches
    what that raises. ``"banana"`` and ``""`` raise ValueError from ``int``,
    ``"22"`` raises IndexError, ``"24:00"`` and ``"-1:00"`` raise ValueError
    from ``time`` - and each of them takes the exception straight out of
    ``run_scheduler``'s loop, which kills the task for the rest of the process
    (``src/services/scheduler_service.py:20-55``, started once at
    ``src/core/client.py:297``). If the scheduler had already paused the miner,
    nothing is left to resume it: mining stops until somebody restarts the
    container, with no console line and a health probe that still says ``ok``,
    because a paused miner is a legitimate state.

    Every one of those five values is reachable through ``POST /api/settings``
    today - the payload model types these as plain strings - and through a hand
    edit of settings.json, which ``merge_json`` waves through because ``str`` is
    the right TYPE. So the check goes where the language check went: at the
    write boundary, before the value is stored, and again at load, because the
    equality short-circuit in ``check_and_update_setting`` means an already
    poisoned stored value is never re-validated by the UI posting it back.

    Deliberately at least as strict as the parser it guards: it demands exactly
    two ``:``-separated fields, where that parser would also accept
    ``"22:00:30"``. Anything this accepts, the scheduler can parse.
    """

    @staticmethod
    def parse(value: Any) -> time:
        """The time this string names, or ValueError - never anything else."""
        if not isinstance(value, str):
            raise ValueError(f"Not a time of day: {value!r}")
        parts = value.strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"Expected a HH:MM time of day, found {value!r}")
        try:
            hour, minute = (int(part) for part in parts)
        except ValueError:
            raise ValueError(f"Expected a HH:MM time of day, found {value!r}") from None
        return time(hour, minute)

    @classmethod
    def accepts(cls, value: Any) -> bool:
        """True when the scheduler can parse this without raising."""
        try:
            cls.parse(value)
        except ValueError:
            return False
        return True

    @classmethod
    def repair(cls, value: Any, key: str) -> str:
        """Return a parseable time, falling back to the default out loud.

        Unlike :meth:`LanguageNormalizer.repair` there is no "valid but
        temporarily unusable" case to protect: a string either names a time of
        day or it does not, so replacing one that does not costs the operator
        nothing that was still recoverable.

        Deliberately does NOT also switch ``scheduler_enabled`` off. Turning the
        scheduler off would mutate a setting the operator really did choose, on
        evidence about a different one, and the next save would persist that
        loss. A repaired boundary is visible in the Settings tab and fixable
        there; the ERROR line names the key and the value it discarded.
        """
        if cls.accepts(value):
            return cast(str, value)
        default = cast(str, default_settings[key])
        logger.error(
            f"Unusable {key} {value!r} in settings: the scheduler cannot read it, and the "
            f"error it raises kills the scheduler task, which can leave mining paused with "
            f"nothing left to resume it. Falling back to {default}."
        )
        return default


class MiningFloor:
    """One "emptying this setting silently stops the miner" rule.

    There is more than one setting whose empty value means "mine nothing", and
    they all fail in the same shape: no exception, no refusal, one bland
    ``Setting changed`` line, a health probe that still says ``ok``, and a miner
    that quietly does nothing until somebody notices days later. So the rule is
    written once here and instantiated per guarded key, rather than copied:

    * ``games_to_watch`` - the list of games to mine. Empty means no game is
      ever selected. This is the one that cost five days in production.
    * ``mining_benefits`` - which benefit types are worth mining.
      ``Benefit.is_wanted`` is fail-CLOSED (``allowed_benefits.get(name,
      False)``), so a selection that enables nothing makes every drop unwanted,
      ``wanted_games`` empty, and mining stops just as completely as an empty
      watch list does - with a watch list that still looks perfectly healthy.

    The invariant is on the STORED value, never on the incoming payload: the
    case that actually hurts is the value becoming empty in memory with no
    request behind it (a corrupt load, a rogue mutation, a future cleaner) and
    then being cemented by an unrelated save such as a dark-mode toggle. See
    :meth:`Settings.save`.

    Subclasses are used as classes, not instances: there is exactly one of each
    and they hold no state, so ``Settings._FLOORS`` is a tuple of the classes
    themselves.
    """

    # The setting this floor guards, and the instance attribute a declaration
    # materialises on ``Settings`` to authorise one emptying save.
    KEY: ClassVar[str]
    FLAG: ClassVar[str]
    # What "the file parses and says nothing at all about KEY" looks like, as
    # opposed to "the file could not be read" (see Settings._stored_value).
    ABSENT: ClassVar[Any]

    # Prose slots for the two refusal lines. Raw English, matching the idiom of
    # the surrounding log lines so no lang/*.json entry is needed.
    REFUSAL: ClassVar[str]  # "an empty games-to-watch list"
    CLEARED: ClassVar[str]  # "it to be cleared"
    CLEARED_LONG: ClassVar[str]  # same, spelled out where the subject is far away
    CONSEQUENCE: ClassVar[str]  # "an empty list means nothing gets mined"
    NOUN: ClassVar[str]  # "list"
    CONTENTS: ClassVar[str]  # "games"

    _RESTORE_LINE: ClassVar[str] = (
        "Refused to save {refusal} over the stored {stored} - nobody asked for "
        "{cleared}, and {consequence}. Restoring the stored {noun}."
    )
    _UNREADABLE_LINE: ClassVar[str] = (
        "Refused to save {refusal}: the stored {path} could not be read, so the "
        "{contents} it may still hold cannot be told apart from none at all, and "
        "nobody asked for {cleared_long}. Nothing was written - the unreadable file "
        "is left in place, so recover any values you need from it, then fix or "
        "delete it."
    )

    @classmethod
    def enables_nothing(cls, value: Any) -> bool:
        """True when this value would leave the miner with nothing to mine."""
        raise NotImplementedError

    @classmethod
    def salvage(cls, raw: Any) -> Any | None:
        """The stored value, cleaned - or ``None`` when it cannot be read.

        ``None`` is never "the stored value is empty": conflating the two is
        what let a settings.json corrupted mid-run read back as "nothing
        stored", so the floor saw nothing worth protecting and the atomic
        replace then destroyed the only remaining copy of it.
        """
        raise NotImplementedError

    @classmethod
    def copy(cls, value: Any) -> Any:
        """A private copy of ``value``, so memory and payload do not alias."""
        raise NotImplementedError

    @classmethod
    def restore_message(cls, stored: Any) -> str:
        return cls._RESTORE_LINE.format(
            refusal=cls.REFUSAL,
            stored=stored,
            cleared=cls.CLEARED,
            consequence=cls.CONSEQUENCE,
            noun=cls.NOUN,
        )

    @classmethod
    def unreadable_message(cls) -> str:
        return cls._UNREADABLE_LINE.format(
            refusal=cls.REFUSAL,
            path=SETTINGS_PATH,
            contents=cls.CONTENTS,
            cleared_long=cls.CLEARED_LONG,
        )


class WatchlistFloor(MiningFloor):
    """``games_to_watch`` may not go from a stored list to ``[]`` unasked."""

    KEY = _WATCHLIST_KEY
    FLAG = "_empty_watchlist_declared"
    ABSENT: ClassVar[Any] = []

    REFUSAL = "an empty games-to-watch list"
    CLEARED = "it to be cleared"
    CLEARED_LONG = "the list to be cleared"
    CONSEQUENCE = "an empty list means nothing gets mined"
    NOUN = "list"
    CONTENTS = "games"

    @classmethod
    def enables_nothing(cls, value: Any) -> bool:
        return not value

    @classmethod
    def salvage(cls, raw: Any) -> list[str] | None:
        if not isinstance(raw, list):
            # A hand-edited ``"games_to_watch": "War Thunder"`` is a value this
            # code cannot read, not an empty list.
            return None
        names = [game for game in raw if isinstance(game, str)]
        if raw and not names:
            # Something is stored, and none of it is a game name this code can
            # read. Silently filtering it down to [] would let the save through.
            return None
        return names

    @classmethod
    def copy(cls, value: Any) -> list[str]:
        return list(value)


class MiningBenefitsFloor(MiningFloor):
    """``mining_benefits`` may not go from enabling something to enabling nothing.

    The un-floored twin of the watch list, and strictly quieter: emptying the
    watch list at least shows an empty picker, while turning every benefit type
    off leaves the whole Settings tab looking normal and the games still listed.
    ``StreamSelector`` drops every drop whose benefits are all unwanted, so the
    wanted-game tree comes out empty and the miner idles forever.
    """

    KEY = _BENEFITS_KEY
    FLAG = "_disabled_benefits_declared"
    ABSENT: ClassVar[Any] = {}

    REFUSAL = "a mining-benefits selection with every benefit type disabled"
    CLEARED = "them all to be turned off"
    CLEARED_LONG = "them all to be turned off"
    CONSEQUENCE = "with none of them enabled no drop is worth mining"
    NOUN = "selection"
    CONTENTS = "benefit types"

    @classmethod
    def enables_nothing(cls, value: Any) -> bool:
        # Not a dict counts as "enables nothing" on purpose: is_wanted() would
        # raise or answer False for every lookup against it, which is the same
        # outage, so it gets the same floor rather than a crash at watch time.
        return not isinstance(value, dict) or not any(value.values())

    @classmethod
    def salvage(cls, raw: Any) -> dict[str, bool] | None:
        if not isinstance(raw, dict):
            return None
        flags = {
            key: value
            for key, value in raw.items()
            if key in MINING_BENEFIT_KEYS and isinstance(value, bool)
        }
        if raw and not flags:
            # Stored, and not one readable benefit flag in it - the dict
            # equivalent of ["War Thunder"] stored as a bare string.
            return None
        return flags

    @classmethod
    def copy(cls, value: Any) -> dict[str, bool]:
        return dict(value)


@dataclass
class Settings:
    connection_quality: int
    dark_mode: bool
    games_to_watch: list[str]
    language: str
    inventory_filters: InventoryFilters
    minimum_refresh_interval_minutes: int
    mining_benefits: dict[str, bool]
    proxy: str
    claim_channel_points: bool
    idle_channels: list[str]
    idle_parallel: bool
    idle_use_followed: bool
    preferred_games: list[str]
    scheduler_enabled: bool
    scheduler_start: str
    scheduler_stop: str
    discord_webhook_drops: str
    discord_webhook_points: str
    drop_name_blacklist: list[str]
    auto_prioritize: bool
    auto_add_linked: bool
    auto_clean_watchlist: bool
    tab_counter_enabled: bool
    claim_moments: bool
    irc_chat_presence: bool
    discord_webhook_mentions: str
    irc_mention_notify: bool
    make_predictions: bool
    bet_strategy: str
    bet_percentage: int
    bet_max_points: int
    bet_minimum_points: int
    bet_percentage_gap: int
    bet_delay_seconds: int
    prediction_channels: list[str]
    channel_strategies: dict[str, str]

    # Save-side floor state (see save()). CLASS-level defaults, deliberately
    # unannotated so @dataclass does not mistake them for fields, and deliberately
    # not initialized in __init__: only the matching declare_*_intent() method
    # ever creates the instance attribute, and save() removes it again. So they
    # stay out of vars(self) - which is both what json_save serializes and what
    # SettingsManager.get_settings() hands to /api/settings - instead of showing
    # up there as settings that do not exist. The class attributes themselves are
    # only ever READ; nothing mutates them, so this is not shared state.
    _empty_watchlist_declared = False
    _disabled_benefits_declared = False

    # Every "emptying this stops the miner" rule save() enforces, in the order
    # it enforces them. Unannotated for the same reason as the flags above.
    _FLOORS = (WatchlistFloor, MiningBenefitsFloor)

    def __init__(self):
        self.load()

    def load(self):
        # quarantine=True: an unparseable settings.json is preserved as
        # settings.json.corrupt and reported at ERROR instead of being replaced
        # by defaults behind a warning. One truncated byte used to be enough to
        # turn the operator's whole games_to_watch list into [] in memory, and
        # the next save then cemented that on disk with nothing left to recover.
        settings = json_load(SETTINGS_PATH, default_settings, merge=True, quarantine=True)
        # merge_json() only guarantees the TYPE of each key, not that the value
        # is usable, so a poisoned language has to be repaired here - before it
        # is setattr-ed and handed to the translator.
        settings["language"] = LanguageNormalizer.repair(settings.get("language"))
        # Same reasoning, same reason it cannot heal itself: a stored
        # scheduler boundary is only ever re-posted by the UI as the value it
        # already is, and check_and_update_setting short-circuits on equality,
        # so a poisoned one survives every restart until it is repaired here.
        for boundary in ("scheduler_start", "scheduler_stop"):
            settings[boundary] = ClockTime.repair(settings.get(boundary), boundary)
        for key, value in settings.items():
            setattr(self, key, value)

    def declare_empty_watchlist_intent(self) -> None:
        """Declare that the NEXT save may persist an empty watch list.

        One of the two ways past the floors in :meth:`save`, and deliberately a
        method rather than a value anyone can assign: a caller has to name the
        intent to get it. It is per-instance state, not module state, and it is
        consumed by the first following save, so permission granted for one
        deliberate "clear all" cannot linger and wave through an accidental wipe
        later.

        The name deliberately does NOT match the ``allow_empty_games_to_watch``
        payload key: that key must never exist as an attribute of this object
        (tests/test_settings_api.py pins that, since an attribute would be
        serialized into settings.json as if it were a setting).

        The caller that has the intent is ``SettingsManager.update_settings``:
        the ``allow_empty_games_to_watch`` flag on the /api/settings payload is
        what distinguishes the user's "clear all" gesture from a page load that
        merely happens to post an empty list.
        """
        self._empty_watchlist_declared = True

    def declare_disabled_benefits_intent(self) -> None:
        """Declare that the NEXT save may persist an all-disabled benefit selection.

        The ``mining_benefits`` twin of :meth:`declare_empty_watchlist_intent`,
        with the same one-shot semantics and the same reason for existing:
        turning every benefit type off is a legitimate gesture (it is how a user
        says "pause mining but keep my watch list"), so it is not banned - it
        just has to be asked for, because the same value arriving unasked means
        the miner silently stops.

        Its payload key is ``allow_empty_mining_benefits``, and as above the
        names are deliberately different so the request-only flag can never
        become an attribute of this object.
        """
        self._disabled_benefits_declared = True

    def _stored_value(self, floor: type[MiningFloor]) -> Any | None:
        """A guarded setting as it currently is ON DISK - the state the floor guards.

        Read at save time rather than remembered from load time, because the
        question is what this save is about to overwrite: a load that fell back
        to defaults, another instance's save, or a hand edit all change the
        answer, and the load-time value would be exactly the one the corrupt-file
        case already got wrong.

        Fails CLOSED, which is what the three-way return type is for. Answering
        "I could not read the stored value" with an empty one made it identical
        to "there is genuinely nothing stored", and that single confusion
        bypassed both protections at once: a settings.json corrupted while the
        process ran read back as ``[]``, so the floor saw nothing worth
        protecting, no-oped, and the atomic replace then destroyed the corrupt
        bytes - the only remaining copy of the operator's list - behind one
        WARNING.

        - ``floor.ABSENT`` (``[]`` / ``{}``) - there is genuinely nothing
          stored: no file yet (a fresh install MUST be able to write its first
          settings.json) or a file that parses and carries no such key at all.
        - ``None`` - the stored value could not be read: the file exists but does
          not parse, or the key holds the wrong type, or it holds a non-empty
          value with nothing usable in it (``[123, 456]``). The caller must not
          treat any of those as "the value is empty".
        - anything else - what this save would overwrite, cleaned by
          :meth:`MiningFloor.salvage`.

        Deliberately read WITHOUT ``quarantine=True``, even though every other
        read of settings.json passes it. Quarantining renames the file to
        settings.json.corrupt, which leaves no settings.json at all - and the very
        next save would then read "no file, nothing stored" and write the empty
        value unchallenged, re-creating the loss this refusal exists to prevent,
        one step later. Refusing the save instead leaves the bytes exactly where
        they are, under their real name, where the boot path's own
        ``quarantine=True`` load preserves them with the recovery message the
        README documents.
        """
        if not SETTINGS_PATH.exists():
            # Nothing is stored yet, so nothing can be lost - and the first save
            # of a fresh install has to be allowed to land.
            return floor.ABSENT
        # merge=False: only the one key matters here, and merging the full
        # default template in would report a defaults-shaped value as "stored".
        unreadable: JsonType = {floor.KEY: _UNREADABLE}
        try:
            stored = json_load(SETTINGS_PATH, unreadable, merge=False)
        except (AttributeError, TypeError):
            # json_load only handles a JSON OBJECT at the top level; a file that
            # parses to an array or a scalar raises on its way out instead of
            # returning the defaults. That is still "I could not read what is
            # stored", and the whole point of this method is that the floor never
            # has to guess which kind of unreadable it got.
            return None
        raw = stored.get(floor.KEY, floor.ABSENT)
        if raw is _UNREADABLE:
            return None
        return floor.salvage(raw)

    def save(self) -> None:
        """Persist the settings, refusing to write an emptiness nobody asked for.

        The rule, in one sentence: a guarded setting (see :class:`MiningFloor`)
        may only be persisted in its mine-nothing state when that state was
        declared through the matching ``declare_*_intent`` method; any other save
        that would write it over a stored value which still enables mining
        restores the stored value, in memory and on disk, and says so at ERROR
        level.

        Why here and not only at the HTTP boundary: the first fix put this floor
        on the incoming payload, which cannot see the case that actually hurts -
        the in-memory value emptying without any payload at all (a corrupt load,
        a rogue mutation, a future cleaner) and then being cemented by an
        unrelated save such as a dark-mode toggle. The invariant belongs on the
        stored state, so it is enforced against the stored state, at the one point
        where memory becomes disk.

        The in-memory value is healed too, not just the file. Leaving memory
        empty while disk holds the games would keep mining stopped until a
        restart and show the operator a UI that disagrees with their settings
        file; failing towards "mine what you asked for" is the safe direction,
        and the ERROR line plus a watch list that visibly comes back is a far
        better signal than five silent days.

        A save that cannot read the stored value writes NOTHING - see
        :meth:`_stored_value`. Refusing costs the other settings in the same
        save; letting it through costs the operator's configuration and the only
        copy of the file it was in.
        """
        declared = {floor.FLAG: getattr(self, floor.FLAG) for floor in self._FLOORS}
        payload = {key: value for key, value in vars(self).items() if not key.startswith("_")}
        for floor in self._FLOORS:
            if declared[floor.FLAG] or not floor.enables_nothing(payload.get(floor.KEY)):
                continue
            stored = self._stored_value(floor)
            if stored is None:
                logger.error(floor.unreadable_message())
                return
            if not floor.enables_nothing(stored):
                logger.error(floor.restore_message(stored))
                payload[floor.KEY] = stored
                setattr(self, floor.KEY, floor.copy(stored))
        # Consume the declarations for the write they authorised, and only for a
        # write that actually lands. Popping rather than assigning False keeps
        # the instance dict - and therefore every payload derived from
        # vars(self) - free of them while json_save serializes; restoring them if
        # json_save raises keeps the user's "clear all" alive across a failed
        # write. Burning it there was a regression in the making: the retry, or
        # any later save, would find the permission spent, hit the floor above
        # and RESURRECT the games the user had just deleted - while blaming
        # "nobody asked for it to be cleared" for a deletion somebody did ask
        # for. A declaration that lingers can only ever authorise the empty value
        # the user already asked for, which is the harmless direction.
        for floor in self._FLOORS:
            self.__dict__.pop(floor.FLAG, None)
        try:
            json_save(SETTINGS_PATH, payload, sort=True)
        except BaseException:
            for floor in self._FLOORS:
                if declared[floor.FLAG]:
                    setattr(self, floor.FLAG, True)
            raise
