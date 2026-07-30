from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, TypedDict, cast

from src.config import DEFAULT_LANG, LANG_PATH, SETTINGS_PATH, JsonType
from src.utils import json_load, json_save


logger = logging.getLogger("TwitchDrops")

# The one setting whose empty value silently disables the whole application, so
# the one that gets a floor on the write side (see Settings.save).
_WATCHLIST_KEY = "games_to_watch"

# Handed to json_load as the stored watch list's default value, so that "the file
# could not be read" is distinguishable from "the file says the list is empty":
# json_load returns the defaults it was given on an unparseable file, and no value
# decoded from JSON can ever BE this object (see Settings._stored_games_to_watch).
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

    # Save-side floor state (see save()). A CLASS-level default, deliberately
    # unannotated so @dataclass does not mistake it for a field, and deliberately
    # not initialized in __init__: only declare_empty_watchlist_intent() ever
    # creates the instance attribute, and save() removes it again. So it stays
    # out of vars(self) - which is both what json_save serializes and what
    # SettingsManager.get_settings() hands to /api/settings - instead of showing
    # up there as a setting that does not exist. The class attribute itself is
    # only ever READ; nothing mutates it, so this is not shared state.
    _empty_watchlist_declared = False

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
        for key, value in settings.items():
            setattr(self, key, value)

    def declare_empty_watchlist_intent(self) -> None:
        """Declare that the NEXT save may persist an empty watch list.

        The only way past the floor in :meth:`save`, and deliberately a method
        rather than a value anyone can assign: a caller has to name the intent to
        get it. It is per-instance state, not module state, and it is consumed by
        the first following save, so permission granted for one deliberate
        "clear all" cannot linger and wave through an accidental wipe later.

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

    def _stored_games_to_watch(self) -> list[str] | None:
        """The watch list as it currently is ON DISK - the state the floor guards.

        Read at save time rather than remembered from load time, because the
        question is what this save is about to overwrite: a load that fell back
        to defaults, another instance's save, or a hand edit all change the
        answer, and the load-time value would be exactly the one the corrupt-file
        case already got wrong.

        Fails CLOSED, which is what the three-way return type is for. Answering
        "I could not read the stored list" with ``[]`` made it identical to
        "there is genuinely nothing stored", and that single confusion bypassed
        both protections at once: a settings.json corrupted while the process ran
        read back as ``[]``, so the floor saw nothing worth protecting, no-oped,
        and the atomic replace then destroyed the corrupt bytes - the only
        remaining copy of the operator's list - behind one WARNING.

        - ``[]`` - there is genuinely nothing stored: no file yet (a fresh
          install MUST be able to write its first settings.json) or a file that
          parses and carries no watch list at all.
        - ``None`` - the stored list could not be read: the file exists but does
          not parse, or ``games_to_watch`` is not a list, or it is a non-empty
          list holding no usable name (``[123, 456]``). The caller must not treat
          any of those as "the list is empty".
        - a list of names - what this save would overwrite. Non-string entries
          are dropped, but only while at least one real name survives.

        Deliberately read WITHOUT ``quarantine=True``, even though every other
        read of settings.json passes it. Quarantining renames the file to
        settings.json.corrupt, which leaves no settings.json at all - and the very
        next save would then read "no file, nothing stored" and write the empty
        list unchallenged, re-creating the loss this refusal exists to prevent,
        one step later. Refusing the save instead leaves the bytes exactly where
        they are, under their real name, where the boot path's own
        ``quarantine=True`` load preserves them with the recovery message the
        README documents.
        """
        if not SETTINGS_PATH.exists():
            # Nothing is stored yet, so nothing can be lost - and the first save
            # of a fresh install has to be allowed to land.
            return []
        # merge=False: only the one key matters here, and merging the full
        # default template in would report a defaults-shaped list as "stored".
        unreadable: JsonType = {_WATCHLIST_KEY: _UNREADABLE}
        try:
            stored = json_load(SETTINGS_PATH, unreadable, merge=False)
        except (AttributeError, TypeError):
            # json_load only handles a JSON OBJECT at the top level; a file that
            # parses to an array or a scalar raises on its way out instead of
            # returning the defaults. That is still "I could not read what is
            # stored", and the whole point of this method is that the floor never
            # has to guess which kind of unreadable it got.
            return None
        games = stored.get(_WATCHLIST_KEY, [])
        if games is _UNREADABLE or not isinstance(games, list):
            return None
        names = [game for game in games if isinstance(game, str)]
        if games and not names:
            # Something is stored, and none of it is a game name this code can
            # read. Silently filtering it down to [] would let the save through.
            return None
        return names

    def save(self) -> None:
        """Persist the settings, refusing to write a watch list nobody emptied.

        The rule, in one sentence: an empty ``games_to_watch`` is persisted only
        when the emptying was declared through
        :meth:`declare_empty_watchlist_intent`; any other save that would write
        ``[]`` over a non-empty stored list restores the stored list, in memory
        and on disk, and says so at ERROR level.

        Why here and not only at the HTTP boundary: the first fix put this floor
        on the incoming payload, which cannot see the case that actually hurts -
        the in-memory list becoming ``[]`` without any payload at all (a corrupt
        load, a rogue mutation, a future cleaner) and then being cemented by an
        unrelated save such as a dark-mode toggle. The invariant belongs on the
        stored state, so it is enforced against the stored state, at the one point
        where memory becomes disk.

        The in-memory list is healed too, not just the file. Leaving memory empty
        while disk holds the games would keep mining stopped until a restart and
        show the operator a UI that disagrees with their settings file; failing
        towards "mine what you asked for" is the safe direction, and the ERROR
        line plus a watch list that visibly comes back is a far better signal
        than five silent days.

        A save that cannot read the stored list writes NOTHING - see
        :meth:`_stored_games_to_watch`. Refusing costs the other settings in the
        same save; letting it through costs the operator's watch list and the
        only copy of the file it was in.
        """
        declared = self._empty_watchlist_declared
        payload = {key: value for key, value in vars(self).items() if not key.startswith("_")}
        if not payload.get(_WATCHLIST_KEY) and not declared:
            stored = self._stored_games_to_watch()
            if stored is None:
                logger.error(
                    "Refused to save an empty games-to-watch list: the stored "
                    f"{SETTINGS_PATH} could not be read, so the games it may still hold "
                    "cannot be told apart from none at all, and nobody asked for the list "
                    "to be cleared. Nothing was written - the unreadable file is left in "
                    "place, so recover any values you need from it, then fix or delete it."
                )
                return
            if stored:
                logger.error(
                    "Refused to save an empty games-to-watch list over the stored "
                    f"{stored} - nobody asked for it to be cleared, and an empty list "
                    "means nothing gets mined. Restoring the stored list."
                )
                payload[_WATCHLIST_KEY] = stored
                self.games_to_watch = list(stored)
        # Consume the declaration for the write it authorised, and only for a
        # write that actually lands. Popping rather than assigning False keeps
        # the instance dict - and therefore every payload derived from
        # vars(self) - free of it while json_save serializes; restoring it if
        # json_save raises keeps the user's "clear all" alive across a failed
        # write. Burning it there was a regression in the making: the retry, or
        # any later save, would find the permission spent, hit the floor above
        # and RESURRECT the games the user had just deleted - while blaming
        # "nobody asked for it to be cleared" for a deletion somebody did ask
        # for. A declaration that lingers can only ever authorise the empty list
        # the user already asked for, which is the harmless direction.
        self.__dict__.pop("_empty_watchlist_declared", None)
        try:
            json_save(SETTINGS_PATH, payload, sort=True)
        except BaseException:
            if declared:
                self._empty_watchlist_declared = True
            raise
