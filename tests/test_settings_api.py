import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.config import LANG_PATH
from src.config import settings as settings_module
from src.config.settings import LanguageNormalizer, Settings, default_settings
from src.i18n.translator import Translator, _
from src.web.app import SettingsUpdate
from src.web.managers.settings import SettingsManager


_REPO_ROOT = Path(__file__).resolve().parents[1]

# The exact line the server must log when it declines a watch-list wipe.
REFUSAL_LINE = "Refused to clear the games-to-watch list without explicit intent"


def without_loaded_language(name: str):
    """Patch the translator as if ``lang/<name>.json`` had failed to parse.

    ``Translator.__init__`` skips a file it cannot read with a ``continue`` and
    runs once per process, so this is exactly what one transient bad read leaves
    behind: the file is still on disk, the language is not in memory. Several
    wave-3 behaviours turn on telling those two facts apart, so they all
    simulate it through here.
    """
    assert name in _._langs, f"{name} is not a shipped language"
    remaining = {key: value for key, value in _._langs.items() if key != name}
    return patch.object(_, "_langs", remaining)


class RealSettingsFileTestBase(unittest.TestCase):
    """A real settings.json in a temp dir, with ``SETTINGS_PATH`` pointed at it.

    The stored-state invariants added in wave 3 are all about what is on disk at
    the moment of a save, so they cannot be proven against a mock: the fixture
    has to be a real file a real ``Settings`` loads from and saves to.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "settings.json"
        patcher = patch.object(settings_module, "SETTINGS_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed(self, **overrides) -> str:
        """Write a complete, valid settings.json and return its exact text."""
        text = json.dumps(dict(default_settings) | overrides, indent=4)
        self.path.write_text(text, encoding="utf-8")
        return text

    def stored(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def readme_prose(self) -> str:
        """README.md as one line, so a reflowed paragraph cannot fail a test."""
        return " ".join((_REPO_ROOT / "README.md").read_text(encoding="utf-8").split())


class TestSettingsAPI(unittest.IsolatedAsyncioTestCase):
    def test_settings_update_model(self):
        # Verify model accepts new fields
        update_data = {
            "inventory_filters": {"show_upcoming": True},
            "mining_benefits": {"BADGE": True},
        }
        model = SettingsUpdate(**update_data)
        self.assertEqual(model.inventory_filters, update_data["inventory_filters"])
        self.assertEqual(model.mining_benefits, update_data["mining_benefits"])

    async def test_settings_manager_networking(self):
        # Mock dependencies
        mock_broadcaster = AsyncMock()
        mock_settings = MagicMock(spec=Settings)
        # Initialize mock attributes with default values for comparison
        mock_settings.inventory_filters = {}
        mock_settings.mining_benefits = {}
        mock_settings.games_to_watch = []
        # update_settings() reads make_predictions unconditionally (predictions
        # enable-transition detection); real Settings.load() always provides it.
        mock_settings.make_predictions = False

        mock_console = MagicMock()
        mock_callback = MagicMock()

        manager = SettingsManager(
            mock_broadcaster, mock_settings, mock_console, on_change=mock_callback
        )

        # 1. Update Inventory Filters (does NOT trigger callback per implementation)
        inv_filters = {"show_upcoming": False}
        manager.update_settings({"inventory_filters": inv_filters})
        mock_callback.assert_not_called()  # inventory_filters has should_trigger_update=False
        self.assertEqual(mock_settings.inventory_filters, inv_filters)
        mock_console.print.assert_called_with(
            "Setting changed: inventory_filters = {'show_upcoming': False}"
        )

        # 2. Update Mining Benefits (SHOULD trigger callback)
        benefits = {"BADGE": False}
        manager.update_settings({"mining_benefits": benefits})
        mock_callback.assert_called_once()
        self.assertEqual(mock_settings.mining_benefits, benefits)
        mock_console.print.assert_called_with("Setting changed: mining_benefits = {'BADGE': False}")
        mock_callback.reset_mock()

        # 3. Update Games to Watch (SHOULD trigger callback)
        games = ["Game 1"]
        manager.update_settings({"games_to_watch": games})
        mock_callback.assert_called_once()


class NoStrayTasksTestCase(unittest.IsolatedAsyncioTestCase):
    """Base that keeps update_settings()' broadcaster emits out of the loop.

    update_settings() fires ``asyncio.create_task(broadcaster.emit(...))``. Left
    alone against a mock broadcaster that schedules a coroutine nothing ever
    awaits, which leaks a task past the test and surfaces later as a
    "coroutine was never awaited" RuntimeWarning attributed to whichever test
    the garbage collector happened to be in. Closing the coroutine instead is
    the same seam tests/test_proxy_settings.py patches, minus the leak.
    """

    def setUp(self):
        patcher = patch("asyncio.create_task", side_effect=lambda coro: coro.close())
        patcher.start()
        self.addCleanup(patcher.stop)


class SettingsManagerTestBase(NoStrayTasksTestCase):
    """Mocked-manager fixture shared by the watch-list / language guard tests.

    Same mock shape as TestSettingsAPI above: Settings' dataclass fields are
    annotation-only so ``spec=`` cannot see them, which means every attribute
    the code compares against has to be pre-set by hand.
    """

    def setUp(self):
        super().setUp()
        self.broadcaster = AsyncMock()
        self.settings = MagicMock(spec=Settings)
        self.settings.games_to_watch = ["Overwatch"]
        self.settings.language = "English"
        # update_settings() reads make_predictions unconditionally.
        self.settings.make_predictions = False
        self.console = MagicMock()
        self.manager = SettingsManager(self.broadcaster, self.settings, self.console)

    def console_lines(self) -> list[str]:
        return [call.args[0] for call in self.console.print.call_args_list]

    def lines_starting(self, prefix: str) -> list[str]:
        return [line for line in self.console_lines() if line.startswith(prefix)]


class TestGamesToWatchClearGuard(SettingsManagerTestBase):
    """An empty games_to_watch means "mine nothing" — it needs explicit intent.

    Regression guard for the incident: the web UI's auto-clean POSTed an empty
    list on a page load and the miner mined nothing for five days.
    """

    async def test_empty_list_without_intent_is_refused(self):
        self.manager.update_settings({"games_to_watch": [], "dark_mode": True})

        self.assertEqual(self.settings.games_to_watch, ["Overwatch"])
        lines = self.console_lines()
        self.assertIn(REFUSAL_LINE, lines)
        self.assertNotIn("Setting changed: games_to_watch = []", lines)
        # Every OTHER key of the same request must still be applied.
        self.assertIs(self.settings.dark_mode, True)
        self.assertIn("Setting changed: dark_mode = True", lines)
        self.settings.save.assert_called_once()

    async def test_empty_list_with_explicit_intent_is_applied(self):
        # The user really can still clear their list ("Deselect All").
        self.manager.update_settings(
            {"games_to_watch": [], "allow_empty_games_to_watch": True}
        )

        self.assertEqual(self.settings.games_to_watch, [])
        lines = self.console_lines()
        self.assertNotIn(REFUSAL_LINE, lines)
        self.assertIn("Setting changed: games_to_watch = []", lines)

    async def test_intent_flag_never_lands_on_the_settings_object(self):
        payload = {"games_to_watch": [], "allow_empty_games_to_watch": True}
        self.manager.update_settings(payload)

        self.assertFalse(hasattr(self.settings, "allow_empty_games_to_watch"))
        # It is popped off a COPY, so the caller's dict is left alone too.
        self.assertEqual(payload, {"games_to_watch": [], "allow_empty_games_to_watch": True})
        self.assertNotIn("allow_empty_games_to_watch", " ".join(self.console_lines()))

    async def test_intent_flag_alone_does_not_touch_the_list(self):
        self.manager.update_settings({"allow_empty_games_to_watch": True, "dark_mode": True})

        self.assertEqual(self.settings.games_to_watch, ["Overwatch"])
        self.assertFalse(hasattr(self.settings, "allow_empty_games_to_watch"))
        self.assertNotIn(REFUSAL_LINE, self.console_lines())

    async def test_non_empty_list_is_unaffected(self):
        self.manager.update_settings({"games_to_watch": ["Rust", "R6"]})

        self.assertEqual(self.settings.games_to_watch, ["Rust", "R6"])
        lines = self.console_lines()
        self.assertNotIn(REFUSAL_LINE, lines)
        self.assertIn("Setting changed: games_to_watch = ['Rust', 'R6']", lines)

    async def test_omitted_key_is_a_no_op(self):
        # The client omits games_to_watch rather than sending [] — proving the
        # omission changes nothing is what makes that strategy safe.
        self.manager.update_settings({"dark_mode": True})

        self.assertEqual(self.settings.games_to_watch, ["Overwatch"])
        self.assertNotIn(REFUSAL_LINE, self.console_lines())

    async def test_already_empty_list_is_dropped_without_crying_wolf(self):
        # Fresh install: the stored list is already empty, so an empty payload
        # refuses nothing. Logging a refusal there is noise, and noise is how an
        # operator learns to scroll past the line that does mean something.
        self.settings.games_to_watch = []

        self.manager.update_settings({"games_to_watch": [], "dark_mode": True})

        lines = self.console_lines()
        self.assertNotIn(REFUSAL_LINE, lines)
        # The key is still dropped, so nothing is "changed" and nothing is set.
        self.assertEqual(self.settings.games_to_watch, [])
        self.assertNotIn("Setting changed: games_to_watch = []", lines)
        self.assertIs(self.settings.dark_mode, True)

    async def test_a_populated_list_still_logs_the_refusal(self):
        # The counterpart to the test above: gating the log must not disarm it.
        self.settings.games_to_watch = ["Overwatch", "Rust"]

        self.manager.update_settings({"games_to_watch": []})

        self.assertIn(REFUSAL_LINE, self.console_lines())
        self.assertEqual(self.settings.games_to_watch, ["Overwatch", "Rust"])


class RaisingActionTestBase(SettingsManagerTestBase):
    """Shared fixture for the two invariants about an ``action`` that raises.

    A raising ``action`` is not hypothetical: ``_set_language`` calls into the
    translator, which raises on anything it cannot load, and the language
    validator only narrows that window - it does not close it (a file can stop
    parsing between the validator's set and the setter's lookup). Both tests
    below drive it through the real ``update_settings`` path rather than calling
    the helper directly, because the ordering they pin only exists there.
    """

    def target_language(self) -> str:
        """A language the validator accepts, so the ACTION is what fails."""
        return next(name for name in _.get_languages() if name != self.settings.language)

    def failing_language_action(self, exc: Exception):
        """Patch ``_set_language`` to raise, the way an unloadable file does."""
        return patch.object(SettingsManager, "_set_language", side_effect=exc)


class TestSettingIsAnnouncedOnlyAfterItHeld(RaisingActionTestBase):
    """``Setting changed:`` is a statement of fact, so it is logged last.

    It used to be printed BEFORE ``action`` ran, so a write the action went on
    to reject produced ``Setting changed: language = X`` immediately followed by
    ``Setting rejected: language = X (...)`` - two contradictory lines about one
    key. From the operator's side that is indistinguishable from the settings
    corruption this whole change exists to remove, and it is exactly the console
    they would be reading while trying to work out why mining stopped.
    """

    async def test_a_rejected_write_is_never_announced_as_a_change(self):
        target = self.target_language()

        with self.failing_language_action(RuntimeError("lang/ went away")), \
                self.assertLogs("TwitchDrops", level="ERROR") as captured:
            self.manager.update_settings({"language": target})

        self.assertEqual(self.lines_starting("Setting changed: language"), [])
        self.assertEqual(
            self.lines_starting("Setting rejected: language"),
            [f"Setting rejected: language = {target} (lang/ went away)"],
        )
        self.assertIn(f"Setting rejected: language = {target}", captured.output[0])

    async def test_the_announcement_comes_after_the_action_has_returned(self):
        # The success path of the same rule, asserted from inside the action:
        # at the moment it runs, its own key has not been announced yet, while
        # the keys applied earlier in the same request already have been.
        target = self.target_language()
        console_at_action_time: list[list[str]] = []

        with patch.object(
            SettingsManager,
            "_set_language",
            side_effect=lambda language: console_at_action_time.append(self.console_lines()),
        ):
            self.manager.update_settings({"language": target, "dark_mode": True})

        self.assertEqual(console_at_action_time, [["Setting changed: dark_mode = True"]])
        self.assertEqual(self.settings.language, target)
        self.assertIn(f"Setting changed: language = {target}", self.console_lines())

    async def test_an_action_line_replaces_the_generic_one_in_place(self):
        # tests/test_proxy_settings.py depends on "Proxy cleared" being the line
        # for a cleared proxy. Asserting the WHOLE console instead of the last
        # call additionally pins that the generic line it replaces is not also
        # emitted, and that moving the announcement did not move the key.
        self.settings.proxy = "http://user:pass@localhost:8080"

        self.manager.update_settings({"dark_mode": True, "proxy": ""})

        self.assertEqual(
            self.console_lines(), ["Setting changed: dark_mode = True", "Proxy cleared"]
        )
        self.assertEqual(self.settings.proxy, "")

    async def test_a_setting_whose_action_stays_quiet_keeps_the_generic_line(self):
        # The other half: returning None must not silence the announcement.
        self.settings.proxy = ""

        self.manager.update_settings({"proxy": "http://1.2.3.4:8080"})

        self.assertEqual(
            self.console_lines(), ["Setting changed: proxy = http://1.2.3.4:8080"]
        )


class TestRejectedSettingIsRolledBack(RaisingActionTestBase):
    """A value whose side effect failed may not stay in memory.

    Deleting the rollback leaves the in-memory ``Settings`` holding a value the
    application itself refused, with the file on disk still clean - so nothing
    looks wrong until the next save (a shutdown, or any unrelated settings
    write) copies the rejected value onto disk, where every later boot loads it.
    That is the same shape as the incident: memory diverges from disk quietly,
    and a routine save cements it.
    """

    async def test_a_failed_action_restores_the_previous_value(self):
        target = self.target_language()

        with self.failing_language_action(RuntimeError("boom")):
            self.manager.update_settings(
                {"dark_mode": True, "language": target, "connection_quality": 3}
            )

        self.assertEqual(self.settings.language, "English")
        # Keys before AND after the rejected one still land, and the request
        # still saves - one bad side effect is not a reason to drop the rest.
        self.assertIs(self.settings.dark_mode, True)
        self.assertEqual(self.settings.connection_quality, 3)
        self.settings.save.assert_called_once()

    async def test_the_rejected_value_never_reaches_a_save(self):
        target = self.target_language()
        at_save_time: list[str] = []
        self.settings.save.side_effect = lambda: at_save_time.append(self.settings.language)

        with self.failing_language_action(RuntimeError("boom")):
            self.manager.update_settings({"language": target})

        self.assertEqual(at_save_time, ["English"])

    def test_an_attribute_that_did_not_exist_does_not_exist_afterwards(self):
        # Restoring to None instead of to absent would materialise a key that
        # was never a setting, and vars(self) is what json_save serializes.
        self.assertFalse(hasattr(self.settings, "proxy"))

        applied = self.manager.check_and_update_setting(
            "proxy",
            "http://user:pass@host:8080",
            True,
            action=MagicMock(side_effect=RuntimeError("unreachable")),
        )

        self.assertFalse(applied)
        self.assertFalse(hasattr(self.settings, "proxy"))
        self.assertTrue(self.lines_starting("Setting rejected: proxy ="))


class TestLanguageValidation(SettingsManagerTestBase):
    """A language the translator cannot load must never reach Settings.

    The web UI POSTed ``language: ""`` from a not-yet-populated <select>; the
    old code mutated first and validated inside the action, so the rejected
    value stayed in memory while the exception aborted the rest of the request
    before save() — and the next shutdown save wrote the poison to disk.
    """

    async def test_empty_language_is_rejected_without_mutating_or_aborting(self):
        self.manager.update_settings({"language": "", "dark_mode": True})

        self.assertEqual(self.settings.language, "English")
        self.assertEqual(self.lines_starting("Setting changed: language"), [])
        self.assertTrue(self.lines_starting("Setting rejected: language ="))
        # One bad key must not abort the remaining keys, nor the save.
        self.assertIs(self.settings.dark_mode, True)
        self.assertIn("Setting changed: dark_mode = True", self.console_lines())
        self.settings.save.assert_called_once()

    async def test_unknown_language_is_rejected(self):
        self.manager.update_settings({"language": "Klingon", "dark_mode": True})

        self.assertEqual(self.settings.language, "English")
        self.assertEqual(self.lines_starting("Setting changed: language"), [])
        self.assertIn("Setting changed: dark_mode = True", self.console_lines())

    async def test_valid_language_is_still_applied(self):
        target = next(name for name in _.get_languages() if name != "English")
        with patch.object(Translator, "set_language", autospec=True) as set_language:
            self.manager.update_settings({"language": target})

        self.assertEqual(self.settings.language, target)
        set_language.assert_called_once()
        self.assertIn(f"Setting changed: language = {target}", self.console_lines())

    def test_validator_accepts_only_known_languages(self):
        for good in _.get_languages():
            with self.subTest(language=good):
                self.manager._validate_language(good)  # must not raise
        for bad in ("", "Klingon", "en-GB", None, 42, ["English"]):
            with self.subTest(language=bad), self.assertRaises(ValueError):
                self.manager._validate_language(bad)

    def test_validator_accepts_every_locale_code_the_translator_maps(self):
        # Regression: the validator checked _.get_languages() (NAMES only) while
        # Translator.set_language() maps locale codes through _LOCALE_MAP first,
        # so a payload of "en" — accepted before the guard existed — started
        # being rejected. A validator narrower than the setter it guards is a
        # bug, not extra safety.
        for code in Translator._LOCALE_MAP:
            with self.subTest(language=code):
                self.manager._validate_language(code)  # must not raise

    def test_accept_set_is_derived_from_the_translator_not_copied(self):
        # Pinning the derivation: a hardcoded copy of the map here would drift
        # the moment the translator learns a new locale code.
        with patch.dict(Translator._LOCALE_MAP, {"xq": "English"}):
            self.manager._validate_language("xq")  # must not raise
        with self.assertRaises(ValueError):
            self.manager._validate_language("xq")

    async def test_locale_code_payload_is_applied(self):
        with patch.object(Translator, "set_language", autospec=True) as set_language:
            self.manager.update_settings({"language": "en"})

        self.assertEqual(self.settings.language, "en")
        set_language.assert_called_once()
        self.assertIn("Setting changed: language = en", self.console_lines())


class TestAutoCleanWatchlistSetting(SettingsManagerTestBase):
    """The destructive auto-clean is opt-in, so its default must be False."""

    def test_default_is_off(self):
        self.assertIs(default_settings["auto_clean_watchlist"], False)
        self.assertIn("auto_clean_watchlist", Settings.__annotations__)

    def test_settings_update_model_round_trip(self):
        self.assertIsNone(SettingsUpdate().auto_clean_watchlist)
        self.assertIs(SettingsUpdate(auto_clean_watchlist=True).auto_clean_watchlist, True)
        self.assertIs(SettingsUpdate(auto_clean_watchlist=False).auto_clean_watchlist, False)

    def test_exclude_unset_makes_an_omitted_list_invisible(self):
        # src/web/app.py dumps with exclude_unset=True, which is what turns the
        # client's "omit games_to_watch" into a genuine server-side no-op.
        dumped = SettingsUpdate(dark_mode=True).model_dump(exclude_unset=True)
        self.assertEqual(dumped, {"dark_mode": True})

        dumped = SettingsUpdate(
            games_to_watch=[], allow_empty_games_to_watch=True
        ).model_dump(exclude_unset=True)
        self.assertEqual(dumped, {"games_to_watch": [], "allow_empty_games_to_watch": True})

    async def test_manager_applies_the_toggle(self):
        self.manager.update_settings({"auto_clean_watchlist": True})

        self.assertIs(self.settings.auto_clean_watchlist, True)
        self.assertIn("Setting changed: auto_clean_watchlist = True", self.console_lines())

    def test_legacy_settings_file_gains_the_default(self):
        # An install that predates the setting must merge in False, not blow up
        # and not inherit the old always-on behavior.
        legacy = dict(default_settings)
        legacy.pop("auto_clean_watchlist")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            with patch.object(settings_module, "SETTINGS_PATH", path):
                loaded = Settings()
        self.assertIs(loaded.auto_clean_watchlist, False)


class TestLanguageRepairOnLoad(unittest.TestCase):
    """A poisoned language on disk has to heal itself at load.

    The live deployment really did end up with ``language: ""`` written by the
    original bug, and nothing repaired it: ``check_and_update_setting`` compares
    the incoming value to the stored one and short-circuits when they match, so
    a stored ``""`` was never re-validated, and every start loaded it again.
    ``merge_json`` does not catch it either — it only enforces the TYPE of a key,
    and ``""`` is a perfectly good ``str``.
    """

    def load_with(self, language) -> Settings:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            stored = dict(default_settings)
            stored["language"] = language
            path.write_text(json.dumps(stored), encoding="utf-8")
            with patch.object(settings_module, "SETTINGS_PATH", path):
                return Settings()

    def test_blank_language_falls_back_to_the_default(self):
        self.assertEqual(self.load_with("").language, default_settings["language"])

    def test_unknown_language_falls_back_to_the_default(self):
        self.assertEqual(self.load_with("Klingon").language, default_settings["language"])

    def test_wrong_type_falls_back_to_the_default(self):
        # merge_json() replaces a wrong-typed value with the template's, so this
        # one is belt and braces — the normalizer must not assume a str either.
        self.assertEqual(self.load_with(None).language, default_settings["language"])

    def test_a_valid_language_name_is_left_alone(self):
        target = next(name for name in _.get_languages() if name != "English")
        self.assertEqual(self.load_with(target).language, target)

    def test_a_locale_code_is_left_alone(self):
        # set_language() maps it, so it is loadable and must not be "repaired".
        self.assertEqual(self.load_with("en").language, "en")

    def test_the_fallback_is_logged(self):
        with self.assertLogs("TwitchDrops", level="WARNING") as captured:
            self.load_with("")

        self.assertTrue(
            any("falling back to" in line and "language" in line.lower()
                for line in captured.output),
            captured.output,
        )

    def test_the_repair_is_persisted_on_the_next_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            stored = dict(default_settings)
            stored["language"] = ""
            path.write_text(json.dumps(stored), encoding="utf-8")
            with patch.object(settings_module, "SETTINGS_PATH", path):
                loaded = Settings()
                loaded.save()
            written = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(written["language"], default_settings["language"])


class TestWatchlistGuardPersistence(NoStrayTasksTestCase):
    """End to end over a REAL Settings object and a real settings.json.

    The mocked tests prove the branch; this proves what actually reaches disk,
    which is where the five-day outage was finally cemented.
    """

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "settings.json"
        seed = dict(default_settings)
        seed["games_to_watch"] = ["Overwatch"]
        self.path.write_text(json.dumps(seed), encoding="utf-8")
        patcher = patch.object(settings_module, "SETTINGS_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = Settings()
        self.manager = SettingsManager(AsyncMock(), self.settings, MagicMock())

    def stored(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    async def test_unintended_clear_leaves_the_stored_list_intact(self):
        self.manager.update_settings({"games_to_watch": [], "auto_clean_watchlist": True})

        self.assertEqual(self.settings.games_to_watch, ["Overwatch"])
        stored = self.stored()
        self.assertEqual(stored["games_to_watch"], ["Overwatch"])
        self.assertIs(stored["auto_clean_watchlist"], True)

    async def test_explicit_clear_is_persisted_without_the_intent_flag(self):
        self.manager.update_settings(
            {"games_to_watch": [], "allow_empty_games_to_watch": True}
        )

        self.assertEqual(self.settings.games_to_watch, [])
        stored = self.stored()
        self.assertEqual(stored["games_to_watch"], [])
        self.assertNotIn("allow_empty_games_to_watch", stored)
        self.assertFalse(hasattr(self.settings, "allow_empty_games_to_watch"))

    async def test_rejected_language_is_never_written_to_disk(self):
        self.manager.update_settings({"language": "", "dark_mode": True})

        self.assertEqual(self.settings.language, default_settings["language"])
        stored = self.stored()
        self.assertEqual(stored["language"], default_settings["language"])
        self.assertIs(stored["dark_mode"], True)


class TestCorruptSettingsFile(RealSettingsFileTestBase):
    """Booting from a settings.json that cannot be parsed.

    One byte short of valid JSON used to be enough to lose a watch list for
    good: ``json_load`` fell back to ``default_settings`` behind a WARNING, the
    miner came up with ``games_to_watch: []``, and the next save overwrote the
    only copy of the real list. Nothing in that sequence was visible to the
    operator, so it is the five-day outage again with no client, no endpoint and
    no cleaner involved.

    The file is now moved aside to ``settings.json.corrupt`` and the event is
    reported at ERROR. The miner still starts from defaults, so the outage is not
    prevented, it is made loud and reversible.
    """

    def truncate_by_one_byte(self) -> str:
        """The red-team's reproduction: a valid file minus its last byte."""
        truncated = self.seed(games_to_watch=["War Thunder", "Overwatch 2"])[:-1]
        self.path.write_text(truncated, encoding="utf-8")
        return truncated

    def preserved(self) -> Path:
        return self.dir / "settings.json.corrupt"

    def boot(self) -> tuple[Settings, list[str]]:
        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            settings = Settings()
        return settings, captured.output

    def test_the_boot_survives_and_reports_it(self):
        # A raise here reached __main__'s sys.exit(4), which under
        # `restart: unless-stopped` is a container restart loop.
        self.truncate_by_one_byte()

        settings, output = self.boot()

        self.assertEqual(settings.games_to_watch, [])
        self.assertIn(self.preserved().name, output[0])
        # The consequence is spelled out, not left for the operator to work out.
        self.assertIn("nothing will be mined", output[0])

    def test_the_operators_bytes_are_preserved_exactly(self):
        truncated = self.truncate_by_one_byte()

        self.boot()

        self.assertEqual(self.preserved().read_text(encoding="utf-8"), truncated)
        self.assertFalse(self.path.exists())
        # Recoverable by hand, which is the entire justification for starting
        # from defaults instead of refusing to boot.
        for game in ("War Thunder", "Overwatch 2"):
            self.assertIn(game, self.preserved().read_text(encoding="utf-8"))

    def test_a_later_save_does_not_destroy_the_preserved_copy(self):
        # The save-side floor cannot help here (see the test below), so the
        # preserved file is the only copy of the list. A settings write must not
        # touch it.
        truncated = self.truncate_by_one_byte()
        settings, _output = self.boot()

        settings.dark_mode = True
        settings.save()

        self.assertEqual(self.preserved().read_text(encoding="utf-8"), truncated)
        self.assertIs(self.stored()["dark_mode"], True)

    def test_the_quarantined_list_is_not_restored_automatically(self):
        """KNOWN GAP, pinned here so it cannot change or be lost silently.

        The save-side floor in ``Settings.save`` compares against the list ON
        DISK, and quarantine moved that file away, so the first save after a
        corrupt boot writes ``games_to_watch: []`` into a brand-new
        settings.json. Mining stays stopped until the operator restores the list
        from ``settings.json.corrupt`` by hand.

        Closing it would mean recovering a value out of bytes that are, by
        definition, not parseable, so it was deliberately not attempted. What
        stands in for it: the ERROR line above, and ``GET /api/health/mining``
        answering ``"ok":false`` in exactly this state
        (tests/test_health_mining_api.py::TestHealthMiningSeesACorruptSettingsFile).
        """
        self.truncate_by_one_byte()
        settings, _output = self.boot()

        settings.dark_mode = True
        settings.save()

        self.assertEqual(self.stored()["games_to_watch"], [])
        self.assertTrue(self.preserved().exists())

    def test_a_second_corruption_keeps_both_copies(self):
        self.truncate_by_one_byte()
        self.boot()
        self.truncate_by_one_byte()
        self.boot()

        self.assertTrue(self.preserved().exists())
        self.assertTrue((self.dir / "settings.json.corrupt.1").exists())

    def test_the_reported_line_is_the_one_the_README_documents(self):
        # A recovery procedure the operator cannot match to the line in their log
        # is a procedure they will not follow.
        self.truncate_by_one_byte()

        _settings, output = self.boot()

        prose = self.readme_prose()
        self.assertIn("settings.json.corrupt", prose)
        self.assertIn("The unreadable file has been kept", prose)
        self.assertIn("The unreadable file has been kept", output[0])


class TestSaveSideWatchlistFloor(RealSettingsFileTestBase):
    """``games_to_watch`` may not go from a stored list to ``[]`` unasked.

    The first fix put this floor on the incoming HTTP payload, which cannot see
    the case that actually hurts: the in-memory list becoming ``[]`` with no
    payload at all, and then being written out by an unrelated save such as a
    dark-mode toggle. So the invariant lives on the stored state and is enforced
    at the one point where memory becomes disk.
    """

    def test_an_unrelated_save_cannot_empty_the_stored_list(self):
        self.seed(games_to_watch=["Overwatch"])
        settings = Settings()
        settings.games_to_watch = []  # no payload, no gesture, no cleaner
        settings.dark_mode = True

        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            settings.save()

        stored = self.stored()
        self.assertEqual(stored["games_to_watch"], ["Overwatch"])
        # The rest of the same save still lands.
        self.assertIs(stored["dark_mode"], True)
        # Memory is healed too: leaving it empty would keep mining stopped until
        # a restart and show a UI that disagrees with the settings file.
        self.assertEqual(settings.games_to_watch, ["Overwatch"])
        self.assertIn("Refused to save an empty games-to-watch list", captured.output[0])

    def test_the_floor_reads_the_file_and_not_the_value_it_loaded(self):
        # Another instance, or a hand edit, may have written the list since this
        # process started. What matters is what THIS save would overwrite.
        self.seed(games_to_watch=[])
        settings = Settings()
        self.seed(games_to_watch=["Rust"])

        with self.assertLogs("TwitchDrops", level="ERROR"):
            settings.save()

        self.assertEqual(self.stored()["games_to_watch"], ["Rust"])

    def test_a_declared_clear_round_trips_to_disk(self):
        # The user really can still clear the list, and must not be fought.
        self.seed(games_to_watch=["Overwatch"])
        settings = Settings()
        settings.declare_empty_watchlist_intent()
        settings.games_to_watch = []

        with self.assertNoLogs("TwitchDrops", level="ERROR"):
            settings.save()

        self.assertEqual(self.stored()["games_to_watch"], [])

    def test_the_declaration_is_spent_by_the_next_save_whatever_it_writes(self):
        # One-shot on purpose: permission granted for one deliberate clear must
        # not linger and wave through an accidental wipe later.
        self.seed(games_to_watch=["Overwatch"])
        settings = Settings()
        settings.declare_empty_watchlist_intent()
        settings.dark_mode = True
        settings.save()

        self.assertNotIn("_empty_watchlist_declared", vars(settings))
        settings.games_to_watch = []
        with self.assertLogs("TwitchDrops", level="ERROR"):
            settings.save()

        self.assertEqual(self.stored()["games_to_watch"], ["Overwatch"])

    def test_a_list_that_was_already_empty_is_written_without_complaint(self):
        # Fresh install, or a user who cleared the list on purpose earlier.
        # Crying wolf here is how an operator learns to ignore the ERROR that
        # does mean something.
        self.seed(games_to_watch=[])
        settings = Settings()
        settings.dark_mode = True

        with self.assertNoLogs("TwitchDrops", level="ERROR"):
            settings.save()

        self.assertEqual(self.stored()["games_to_watch"], [])

    def test_the_floors_bookkeeping_never_reaches_the_file(self):
        # A stray attribute would be serialized as if it were a setting, and
        # would also be shipped to the dashboard by GET /api/settings.
        self.seed(games_to_watch=["Overwatch"])
        settings = Settings()
        settings.declare_empty_watchlist_intent()
        settings.games_to_watch = []
        settings.save()

        self.assertEqual([key for key in self.stored() if key.startswith("_")], [])
        self.assertFalse(hasattr(settings, "allow_empty_games_to_watch"))

    def test_the_refusal_line_is_the_one_the_README_documents(self):
        self.seed(games_to_watch=["Overwatch"])
        settings = Settings()
        settings.games_to_watch = []

        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            settings.save()

        prose = self.readme_prose()
        self.assertIn("Refused to save an empty games-to-watch list over the stored", prose)
        self.assertIn("Refused to save an empty games-to-watch list over the stored",
                      captured.output[0])


class TestDeclarationSurvivesAFailedWrite(RealSettingsFileTestBase):
    """A write that never landed may not spend the user's "clear all".

    ``save()`` consumes the declaration just before handing the payload to
    ``json_save``. If that write then fails - a full disk, a read-only data
    directory, a SIGKILL caught as an exception on the way out - the permission
    is gone while the file still holds the old games. The retry, or any later
    save, then hits the floor, RESURRECTS the games the user deleted, and blames
    "nobody asked for it to be cleared" for a deletion somebody did ask for.
    A declaration that lingers can only ever authorise the empty list the user
    already asked for, which is the harmless direction.
    """

    def failing_write(self, exc: BaseException):
        return patch.object(settings_module, "json_save", side_effect=exc)

    def test_a_failed_write_does_not_spend_the_declaration(self):
        self.seed(games_to_watch=["Overwatch", "Rust"])
        settings = Settings()
        settings.declare_empty_watchlist_intent()
        settings.games_to_watch = []

        full_disk = OSError(28, "No space left on device")
        with self.failing_write(full_disk), self.assertRaises(OSError):
            settings.save()

        self.assertIs(settings._empty_watchlist_declared, True)
        # The retry writes what the user asked for, silently - and above all
        # does not hand them their deleted games back.
        with self.assertNoLogs("TwitchDrops", level="ERROR"):
            settings.save()
        self.assertEqual(self.stored()["games_to_watch"], [])
        self.assertEqual(settings.games_to_watch, [])

    def test_a_failed_write_without_a_declaration_invents_none(self):
        # The restore is conditional on there having BEEN a declaration; a
        # failed ordinary save must not leave a standing permission behind.
        self.seed(games_to_watch=["Overwatch"])
        settings = Settings()
        settings.dark_mode = True

        with self.failing_write(OSError("read-only file system")), self.assertRaises(OSError):
            settings.save()

        self.assertNotIn("_empty_watchlist_declared", vars(settings))
        settings.games_to_watch = []
        with self.assertLogs("TwitchDrops", level="ERROR"):
            settings.save()
        self.assertEqual(self.stored()["games_to_watch"], ["Overwatch"])

    def test_the_restored_declaration_is_still_never_serialized(self):
        # The restore is what makes the flag outlive a save, so the payload
        # filter is now the only thing keeping it out of settings.json - and out
        # of GET /api/settings, which hands vars(self) to the dashboard.
        self.seed(games_to_watch=["Overwatch"])
        settings = Settings()
        settings.declare_empty_watchlist_intent()
        settings.games_to_watch = []

        with self.failing_write(OSError("full")), self.assertRaises(OSError):
            settings.save()

        self.assertIn("_empty_watchlist_declared", vars(settings))
        settings.save()
        self.assertEqual([key for key in self.stored() if key.startswith("_")], [])

    def test_the_payload_handed_to_the_writer_carries_no_private_keys(self):
        # Asserted at the boundary as well as on disk: a failed write serializes
        # nothing, so the file alone cannot prove what was about to be written.
        self.seed(games_to_watch=["Overwatch"])
        settings = Settings()
        settings.declare_empty_watchlist_intent()
        settings.games_to_watch = []
        seen: list[list[str]] = []

        with patch.object(
            settings_module,
            "json_save",
            side_effect=lambda path, payload, **kwargs: seen.append(sorted(payload)),
        ):
            settings.save()

        self.assertEqual(len(seen), 1)
        self.assertEqual([key for key in seen[0] if key.startswith("_")], [])


class TestUnreadableStoredListRefusesTheSave(RealSettingsFileTestBase):
    """"I cannot read the stored list" is not the same as "the list is empty".

    Answering both with ``[]`` bypassed the floor and the quarantine at once: a
    settings.json corrupted while the process ran read back as "nothing stored",
    so the floor saw nothing worth protecting, and the atomic replace then
    destroyed the corrupt bytes - the last copy of the operator's list - behind
    a single WARNING. So an unreadable stored list refuses the save outright,
    which costs the other settings in that save and keeps the file.

    The file is deliberately NOT quarantined here. Quarantining renames it to
    settings.json.corrupt, leaving no settings.json at all, and the very next
    save would read "no file, nothing stored" and write the empty list
    unchallenged - the same loss, one step later.
    """

    REFUSAL = "Refused to save an empty games-to-watch list"

    def running_miner(self, unreadable: str) -> Settings:
        """Boot from a good file, then make the file unreadable behind us."""
        self.seed(games_to_watch=["War Thunder", "Overwatch 2"])
        settings = Settings()
        self.path.write_text(unreadable, encoding="utf-8")
        return settings

    def assert_refused(self, settings: Settings, unreadable: str):
        settings.games_to_watch = []  # no gesture, no declaration
        settings.dark_mode = True

        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            settings.save()

        self.assertIn(self.REFUSAL, captured.output[0])
        self.assertIn("could not be read", captured.output[0])
        # Nothing was written: not the empty list, not the dark mode toggle that
        # rode along with it. That cost is the point - see the class docstring.
        self.assertEqual(self.path.read_text(encoding="utf-8"), unreadable)
        # ...and the bytes are still under their own name, not moved aside.
        self.assertEqual(sorted(path.name for path in self.dir.iterdir()), ["settings.json"])

    def test_a_file_corrupted_while_the_miner_runs_refuses_the_save(self):
        truncated = json.dumps(dict(default_settings) | {"games_to_watch": ["Rust"]})[:-1]

        self.assert_refused(self.running_miner(truncated), truncated)

    def test_a_wrongly_typed_stored_list_refuses_the_save(self):
        # A hand edit: a bare string where the list belongs. merge=False is used
        # for this read, so nothing repairs the type on the way in.
        text = json.dumps(dict(default_settings) | {"games_to_watch": "War Thunder"})

        self.assert_refused(self.running_miner(text), text)

    def test_a_stored_list_with_no_usable_name_refuses_the_save(self):
        # Filtering [123, 456] down to [] would make "unreadable" look empty
        # again, which is the exact confusion this method exists to end.
        text = json.dumps(dict(default_settings) | {"games_to_watch": [123, 456]})

        self.assert_refused(self.running_miner(text), text)

    def test_a_top_level_array_refuses_the_save(self):
        # json_load only handles a JSON object at the top level and raises on
        # its way out for anything else; the floor must fail closed on that too.
        text = '["War Thunder", "Overwatch 2"]'

        self.assert_refused(self.running_miner(text), text)

    def test_unusable_entries_are_dropped_while_a_real_name_survives(self):
        text = json.dumps(dict(default_settings) | {"games_to_watch": ["Rust", 123]})
        settings = self.running_miner(text)
        settings.games_to_watch = []

        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            settings.save()

        self.assertIn("Restoring the stored list", captured.output[0])
        self.assertEqual(self.stored()["games_to_watch"], ["Rust"])
        self.assertEqual(settings.games_to_watch, ["Rust"])

    def test_a_stored_file_without_the_key_is_not_treated_as_unreadable(self):
        # It parses and it says nothing about games_to_watch, so there is
        # genuinely nothing stored to protect - the save must land.
        settings = self.running_miner(json.dumps({"dark_mode": True}))
        settings.games_to_watch = []

        with self.assertNoLogs("TwitchDrops", level="ERROR"):
            settings.save()

        self.assertEqual(self.stored()["games_to_watch"], [])

    def test_a_fresh_install_still_writes_its_very_first_save(self):
        # No file at all is the other genuine "nothing stored". Failing closed
        # here would make a new install unable to persist anything.
        self.assertFalse(self.path.exists())
        settings = Settings()
        settings.dark_mode = True

        with self.assertNoLogs("TwitchDrops", level="ERROR"):
            settings.save()

        self.assertEqual(self.stored()["games_to_watch"], [])
        self.assertIs(self.stored()["dark_mode"], True)

    def test_a_declared_clear_is_still_honoured_over_an_unreadable_file(self):
        # The refusal guards an UNASKED-FOR empty list. When the user really did
        # clear the list, the unreadable file holds nothing they want back.
        text = json.dumps(dict(default_settings) | {"games_to_watch": ["Rust"]})[:-1]
        settings = self.running_miner(text)
        settings.declare_empty_watchlist_intent()
        settings.games_to_watch = []

        with self.assertNoLogs("TwitchDrops", level="ERROR"):
            settings.save()

        self.assertEqual(self.stored()["games_to_watch"], [])


class TestLanguageRepairKeepsALegitimateChoice(RealSettingsFileTestBase):
    """A repair may only fire on a value that names no language at all.

    ``repair`` is destructive: the next save persists whatever it returns. It
    used to judge by "can the translator load this right now", so one
    unparseable ``lang/Nederlandse.json`` was enough to rewrite a stored
    ``"Nederlandse"`` to English and then write that loss to disk, where no
    later boot could undo it. Existence of ``lang/<name>.json`` is the
    parse-independent fact, so that is what a repair decision uses.
    """

    def test_a_language_whose_file_failed_to_parse_is_kept(self):
        self.seed(language="Nederlandse")

        with without_loaded_language("Nederlandse"):
            settings = Settings()

            self.assertEqual(settings.language, "Nederlandse")
            settings.save()

        self.assertEqual(self.stored()["language"], "Nederlandse")

    def test_a_value_that_names_no_language_still_repairs(self):
        for bad in ("", "Klingon", "en-GB"):
            with self.subTest(language=bad):
                self.seed(language=bad)
                with self.assertLogs("TwitchDrops", level="WARNING"):
                    settings = Settings()
                self.assertEqual(settings.language, default_settings["language"])

    def test_a_language_no_file_ships_is_not_kept_either(self):
        # The other direction: "unknown" is still unknown when nothing on disk
        # claims the name, so the lenient repair rule is not a blanket pass.
        self.seed(language="Nederlandse")

        with without_loaded_language("Nederlandse"), \
                patch.object(LanguageNormalizer, "installed", return_value=set()), \
                self.assertLogs("TwitchDrops", level="WARNING"):
            settings = Settings()

        self.assertEqual(settings.language, default_settings["language"])

    def test_installed_is_the_set_of_language_files_on_disk(self):
        self.assertEqual(
            LanguageNormalizer.installed(), {path.stem for path in LANG_PATH.glob("*.json")}
        )

    def test_every_shipped_language_file_is_named_after_what_it_holds(self):
        # installed() reads the file STEMS, never the files, because it has to
        # keep working for a file that cannot be parsed. That makes it the name
        # set only while this holds, and it cannot check that itself.
        for path in LANG_PATH.glob("*.json"):
            with self.subTest(language=path.name):
                translation = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(translation["language_name"], path.stem)


class TestLanguageAcceptSetMatchesTheSetter(unittest.TestCase):
    """The write-path accept-set has to be exactly as wide as ``set_language``.

    Not narrower: the setter maps locale codes through ``_LOCALE_MAP`` before the
    lookup, so a validator that knew only the names rejected an ``"en"`` payload
    the app handles fine. Not wider either, which is the part wave 3 fixed:
    ``accepts("ar")`` was True while ``set_language("ar")`` raised
    ``Unrecognized language العربية`` for as long as that one file failed to
    parse, so the validator waved through a value the setter then threw out.
    """

    def test_a_code_is_accepted_exactly_when_its_target_is_loaded(self):
        # Asserted as a property of the two sets rather than against a hardcoded
        # list, so it keeps holding when the translator learns a new code.
        for dropped in (None, "العربية", "Deutsch"):
            context = contextlib.nullcontext() if dropped is None else without_loaded_language(
                dropped
            )
            with context:
                loaded = set(_.get_languages())
                for code, name in Translator._LOCALE_MAP.items():
                    with self.subTest(dropped=dropped, code=code):
                        self.assertIs(LanguageNormalizer.accepts(code), name in loaded)

    def test_the_validator_and_the_setter_agree_on_an_unloadable_code(self):
        with without_loaded_language("العربية"):
            self.assertFalse(LanguageNormalizer.accepts("ar"))
            with self.assertRaises(ValueError):
                _.set_language("ar")

    def test_the_accept_set_never_exceeds_the_known_set(self):
        self.assertLessEqual(LanguageNormalizer.accepted(), LanguageNormalizer.known())

    def test_a_repair_is_judged_more_leniently_than_a_write(self):
        # The whole point of splitting the two questions, in two lines.
        with without_loaded_language("Nederlandse"):
            self.assertFalse(LanguageNormalizer.accepts("Nederlandse"))
            self.assertTrue(LanguageNormalizer.knows("Nederlandse"))


class TestLanguageApplyAtStartup(RealSettingsFileTestBase):
    """A language is never worth a failed boot.

    ``src/__main__.py`` called ``_.set_language`` bare, outside any try/except,
    so a stored language the translator could not load raised out of
    ``asyncio.run()`` and killed the process. Under ``restart: unless-stopped``
    that is a container restart loop caused by one unreadable file in ``lang/``,
    with the web UI needed to fix it gone as well.
    """

    def setUp(self):
        super().setUp()
        # apply() mutates the process-wide translator, so put it back.
        self.addCleanup(_.set_language, _.current_language)

    def test_an_unloadable_language_does_not_raise(self):
        with self.assertLogs("TwitchDrops", level="WARNING") as captured:
            in_effect = LanguageNormalizer.apply("Klingon")

        self.assertEqual(in_effect, _.current_language)
        self.assertIn("Klingon", captured.output[0])

    def test_a_loadable_language_is_applied_and_returned(self):
        target = next(name for name in _.get_languages() if name != _.current_language)

        self.assertEqual(LanguageNormalizer.apply(target), target)
        self.assertEqual(_.current_language, target)

    def test_a_transient_failure_falls_back_for_this_run_only(self):
        self.seed(language="Nederlandse")
        settings = Settings()

        with without_loaded_language("Nederlandse"), \
                self.assertLogs("TwitchDrops", level="WARNING"):
            in_effect = LanguageNormalizer.apply(settings.language)

        self.assertEqual(in_effect, _.current_language)
        self.assertNotEqual(in_effect, "Nederlandse")
        # The stored choice is deliberately NOT corrected: doing so would let
        # the shutdown save destroy a language whose file merely failed to parse
        # this once, which is the loss repair() now refuses to cause.
        self.assertEqual(settings.language, "Nederlandse")
        settings.save()
        self.assertEqual(self.stored()["language"], "Nederlandse")
        # ...and once the file parses again the choice takes effect.
        self.assertEqual(LanguageNormalizer.apply("Nederlandse"), "Nederlandse")

    def test_the_entry_point_goes_through_apply(self):
        # src/__main__.py cannot be imported (it is all under `if __name__ ==
        # "__main__"`), so the call site is asserted statically, same idiom as
        # tests/test_watchlist_guard.py.
        source = (_REPO_ROOT / "src" / "__main__.py").read_text(encoding="utf-8")

        self.assertIn("LanguageNormalizer.apply(settings.language)", source)
        self.assertNotIn("_.set_language(", source)


if __name__ == "__main__":
    unittest.main()
