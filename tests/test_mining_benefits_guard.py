"""``mining_benefits`` is the un-floored twin of the watch list.

``Benefit.is_wanted`` is fail-CLOSED - it looks the benefit type up with
``allowed_benefits.get(name, False)`` - so a selection that enables nothing
makes every drop unwanted, ``wanted_games`` empty, and mining stop exactly as
completely as an empty ``games_to_watch`` does. It is the quieter of the two:
an emptied watch list at least shows an empty picker, while every benefit type
turned off leaves the Settings tab looking normal with all the games still
listed.

Two separate holes, both under test here.

* **Replacement.** The dashboard builds the dict from four
  ``document.getElementById(...)?.checked`` reads, each of which yields
  ``undefined`` for an element that has not rendered, which ``JSON.stringify``
  then drops. Because ``is_wanted`` is fail-closed, an omitted key was not
  "leave it alone", it was "turn it off", so a partial payload disabled benefit
  types nobody touched. The payload is now MERGED onto the stored selection,
  which gives an absent key the same meaning every other absent value has in
  ``check_and_update_setting``: not supplied.
* **Emptying.** Turning everything off stays possible, because "stop mining but
  keep my watch list" is a real thing to want. It just has to be asked for,
  with ``allow_empty_mining_benefits`` on the same request, and the save-side
  floor refuses it again on the way to disk if it was not.

The two halves share one definition of "this value would leave nothing to mine"
(``MiningFloor.enables_nothing``). Letting them drift would mean a payload the
HTTP layer waves through and the floor then silently refuses: an HTTP 200
reporting a change that never reached disk.
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import settings as settings_module
from src.config.settings import (
    MINING_BENEFIT_KEYS,
    MiningBenefitsFloor,
    Settings,
    WatchlistFloor,
    default_settings,
)
from src.web.managers.settings import SettingsManager
from tests.test_settings_api import RealSettingsFileTestBase, SettingsManagerTestBase
from tests.test_watchlist_guard import APP_JS_COPIES, extract_function


ALL_ON = dict(default_settings["mining_benefits"])
ALL_OFF = dict.fromkeys(ALL_ON, False)

# The exact line the server logs when it declines to disable everything.
PAYLOAD_REFUSAL = "Refused to disable every mining benefit type without explicit intent"
SAVE_REFUSAL = "Refused to save a mining-benefits selection with every benefit type disabled"

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Drives the REAL saveSettings() out of an app.js copy under a stub DOM; see the
# header of that file. Node is optional - the static assertions above it hold
# either way - so a machine without it skips rather than fails.
_DRIVER = Path(__file__).resolve().parent / "js" / "drive_save_settings.mjs"
_NODE = shutil.which("node")


class TestTheBenefitKeysComeFromTheDefaults(unittest.TestCase):
    """One list of benefit types in the codebase, not two.

    ``MINING_BENEFIT_KEYS`` is what the merge filters by and what the floor
    salvages by. A hand-maintained copy of it would drift the first time a
    benefit type is added, and the symptom would be a new type silently dropped
    from every save.
    """

    def test_the_key_set_is_the_defaults_key_set(self):
        self.assertEqual(MINING_BENEFIT_KEYS, frozenset(default_settings["mining_benefits"]))

    def test_the_shipped_default_enables_everything(self):
        # If a future default ever disabled a type, ALL_ON above stops being
        # "the healthy selection" and several tests here quietly weaken.
        self.assertTrue(all(default_settings["mining_benefits"].values()))


class TestMiningBenefitsAreMerged(SettingsManagerTestBase):
    """A payload says what it mentions, and nothing about what it omits."""

    async def test_an_omitted_type_keeps_its_stored_value(self):
        # The reproduction: one checkbox not rendered, three types switched off.
        self.manager.update_settings({"mining_benefits": {"BADGE": False}})

        self.assertEqual(self.settings.mining_benefits, ALL_ON | {"BADGE": False})

    async def test_an_empty_selection_object_changes_nothing(self):
        # Every checkbox unrendered. This is the shape a page that has not
        # finished loading actually posts, and it must be a true no-op: no
        # write, no console line, no wanted-games recomputation.
        self.manager.update_settings({"mining_benefits": {}})

        self.assertEqual(self.settings.mining_benefits, ALL_ON)
        self.assertEqual(self.lines_starting("Setting changed: mining_benefits"), [])

    async def test_re_enabling_a_type_still_works(self):
        self.settings.mining_benefits = ALL_ON | {"EMOTE": False}

        self.manager.update_settings({"mining_benefits": {"EMOTE": True}})

        self.assertEqual(self.settings.mining_benefits, ALL_ON)

    async def test_a_key_outside_the_benefit_types_is_dropped(self):
        # merge_json drops it on the next load anyway, so letting it in would
        # only put junk in settings.json until that load happened.
        self.manager.update_settings({"mining_benefits": {"WEIRD": True}})

        self.assertEqual(self.settings.mining_benefits, ALL_ON)

    async def test_a_non_boolean_value_is_dropped(self):
        self.manager.update_settings({"mining_benefits": {"BADGE": "no"}})

        self.assertEqual(self.settings.mining_benefits, ALL_ON)

    async def test_an_unreadable_stored_selection_is_replaced_not_merged_into(self):
        # A hand-edited string where the dict belongs. There is nothing to merge
        # onto, so the payload stands on its own - and the four types it does
        # not name stay absent, which the floor below then judges.
        self.settings.mining_benefits = "BADGE"

        self.manager.update_settings({"mining_benefits": {"BADGE": True}})

        self.assertEqual(self.settings.mining_benefits, {"BADGE": True})


class TestDisablingEveryBenefitTypeNeedsIntent(SettingsManagerTestBase):
    """Nothing enabled is nothing mined, so it takes the same kind of gesture."""

    def disable_all(self, *, allow=None, **extra):
        payload = {"mining_benefits": ALL_OFF, **extra}
        if allow is not None:
            payload["allow_empty_mining_benefits"] = allow
        self.manager.update_settings(payload)

    async def test_disabling_everything_without_intent_is_refused(self):
        self.disable_all(dark_mode=True)

        self.assertEqual(self.settings.mining_benefits, ALL_ON)
        self.assertIn(PAYLOAD_REFUSAL, self.console_lines())
        # Every OTHER key of the same request still applies.
        self.assertIs(self.settings.dark_mode, True)

    async def test_disabling_everything_with_intent_is_applied(self):
        self.disable_all(allow=True)

        self.assertEqual(self.settings.mining_benefits, ALL_OFF)
        self.assertNotIn(PAYLOAD_REFUSAL, self.console_lines())
        self.settings.declare_disabled_benefits_intent.assert_called_once()

    async def test_a_merge_that_lands_on_nothing_enabled_is_refused_too(self):
        # The payload itself is a single innocuous-looking key; only the MERGED
        # result enables nothing. Judging the payload would have missed it.
        self.settings.mining_benefits = ALL_OFF | {"BADGE": True}

        self.manager.update_settings({"mining_benefits": {"BADGE": False}})

        self.assertEqual(self.settings.mining_benefits, ALL_OFF | {"BADGE": True})
        self.assertIn(PAYLOAD_REFUSAL, self.console_lines())

    async def test_an_already_disabled_selection_does_not_cry_wolf(self):
        # Nothing was protected, so warning here only trains the operator to
        # scroll past the line that matters. Same rule as the watch list.
        self.settings.mining_benefits = ALL_OFF

        self.manager.update_settings({"mining_benefits": ALL_OFF, "dark_mode": True})

        self.assertNotIn(PAYLOAD_REFUSAL, self.console_lines())
        self.assertIs(self.settings.dark_mode, True)

    async def test_the_intent_flag_never_lands_on_the_settings_object(self):
        payload = {"mining_benefits": ALL_OFF, "allow_empty_mining_benefits": True}
        original = dict(payload)

        self.manager.update_settings(payload)

        self.assertFalse(hasattr(self.settings, "allow_empty_mining_benefits"))
        self.assertNotIn("allow_empty_mining_benefits", " ".join(self.console_lines()))
        # Popped off a COPY, so the caller's dict is untouched.
        self.assertEqual(payload, original)

    async def test_the_flag_alone_declares_nothing(self):
        # A declaration is consumed by the next save whatever that save writes,
        # so granting one for a request that empties nothing would spend it on a
        # save nobody vetted.
        self.manager.update_settings({"allow_empty_mining_benefits": True, "dark_mode": True})

        self.settings.declare_disabled_benefits_intent.assert_not_called()

    async def test_the_watchlist_flag_does_not_authorise_disabling_benefits(self):
        # Two floors, two declarations. One gesture may not pay for the other.
        self.manager.update_settings(
            {"mining_benefits": ALL_OFF, "allow_empty_games_to_watch": True}
        )

        self.assertEqual(self.settings.mining_benefits, ALL_ON)
        self.assertIn(PAYLOAD_REFUSAL, self.console_lines())


class TestSaveSideBenefitsFloor(RealSettingsFileTestBase):
    """The stored selection is what is protected, exactly as for the watch list.

    The payload guard cannot see the case that actually hurts: the in-memory
    selection emptying with no request behind it - a corrupt load, a rogue
    mutation - and then being cemented by an unrelated save such as a dark-mode
    toggle. So the invariant lives on the stored state and is enforced where
    memory becomes disk.
    """

    def test_an_unrelated_save_cannot_disable_every_stored_type(self):
        self.seed(mining_benefits=ALL_ON)
        settings = Settings()
        settings.mining_benefits = ALL_OFF  # no payload, no gesture
        settings.dark_mode = True

        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            settings.save()

        stored = self.stored()
        self.assertEqual(stored["mining_benefits"], ALL_ON)
        # The rest of the same save still lands, and memory is healed too.
        self.assertIs(stored["dark_mode"], True)
        self.assertEqual(settings.mining_benefits, ALL_ON)
        self.assertIn(SAVE_REFUSAL, captured.output[0])

    def test_a_declared_disable_round_trips_to_disk(self):
        self.seed(mining_benefits=ALL_ON)
        settings = Settings()
        settings.declare_disabled_benefits_intent()
        settings.mining_benefits = ALL_OFF

        with self.assertNoLogs("TwitchDrops", level="ERROR"):
            settings.save()

        self.assertEqual(self.stored()["mining_benefits"], ALL_OFF)

    def test_the_declaration_is_spent_by_the_next_save(self):
        self.seed(mining_benefits=ALL_ON)
        settings = Settings()
        settings.declare_disabled_benefits_intent()
        settings.dark_mode = True
        settings.save()

        self.assertNotIn("_disabled_benefits_declared", vars(settings))
        settings.mining_benefits = ALL_OFF
        with self.assertLogs("TwitchDrops", level="ERROR"):
            settings.save()

        self.assertEqual(self.stored()["mining_benefits"], ALL_ON)

    def test_a_failed_write_does_not_spend_the_declaration(self):
        # Burning it on a write that never landed would make the retry hit the
        # floor and RESURRECT the types the operator had just switched off.
        self.seed(mining_benefits=ALL_ON)
        settings = Settings()
        settings.declare_disabled_benefits_intent()
        settings.mining_benefits = ALL_OFF

        with (
            patch.object(
                settings_module, "json_save", side_effect=OSError(28, "No space left on device")
            ),
            self.assertRaises(OSError),
        ):
            settings.save()

        self.assertIs(settings._disabled_benefits_declared, True)
        with self.assertNoLogs("TwitchDrops", level="ERROR"):
            settings.save()
        self.assertEqual(self.stored()["mining_benefits"], ALL_OFF)

    def test_an_unreadable_stored_file_refuses_the_whole_save(self):
        # "I cannot read the stored selection" is not "the stored selection is
        # empty". Writing would destroy the only remaining copy of it.
        #
        # The watch list is seeded non-empty on purpose: it is the first floor in
        # the loop, and an unreadable file makes IT refuse the save too, so a
        # defaults-shaped (empty) watch list would leave this test passing on the
        # other floor's message.
        self.seed(games_to_watch=["Overwatch"], mining_benefits=ALL_ON)
        settings = Settings()
        unreadable = json.dumps(
            dict(default_settings) | {"games_to_watch": ["Overwatch"], "mining_benefits": ALL_ON}
        )[:-1]
        self.path.write_text(unreadable, encoding="utf-8")
        settings.mining_benefits = ALL_OFF

        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            settings.save()

        self.assertIn(SAVE_REFUSAL, captured.output[0])
        self.assertIn("could not be read", captured.output[0])
        # Nothing written, and the bytes left under their own name so the boot
        # path's quarantine can preserve them with a recovery message.
        self.assertEqual(self.path.read_text(encoding="utf-8"), unreadable)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["settings.json"])

    def test_a_wrongly_typed_stored_selection_also_refuses(self):
        self.seed(games_to_watch=["Overwatch"], mining_benefits=ALL_ON)
        settings = Settings()
        text = json.dumps(
            dict(default_settings) | {"games_to_watch": ["Overwatch"], "mining_benefits": "BADGE"}
        )
        self.path.write_text(text, encoding="utf-8")
        settings.mining_benefits = ALL_OFF

        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            settings.save()

        self.assertIn("could not be read", captured.output[0])
        self.assertEqual(self.path.read_text(encoding="utf-8"), text)

    def test_a_stored_selection_that_was_already_off_is_written_quietly(self):
        self.seed(mining_benefits=ALL_OFF)
        settings = Settings()
        settings.dark_mode = True

        with self.assertNoLogs("TwitchDrops", level="ERROR"):
            settings.save()

        self.assertEqual(self.stored()["mining_benefits"], ALL_OFF)

    def test_a_non_dict_in_memory_is_treated_as_enabling_nothing(self):
        # is_wanted() would answer False for every lookup against a string, so
        # it is the same outage and gets the same floor rather than a crash at
        # watch time.
        self.seed(mining_benefits=ALL_ON)
        settings = Settings()
        settings.mining_benefits = "BADGE"

        with self.assertLogs("TwitchDrops", level="ERROR"):
            settings.save()

        self.assertEqual(self.stored()["mining_benefits"], ALL_ON)
        self.assertEqual(settings.mining_benefits, ALL_ON)

    def test_the_floors_bookkeeping_never_reaches_the_file(self):
        self.seed(mining_benefits=ALL_ON)
        settings = Settings()
        settings.declare_disabled_benefits_intent()
        settings.mining_benefits = ALL_OFF
        settings.save()

        self.assertEqual([key for key in self.stored() if key.startswith("_")], [])
        self.assertFalse(hasattr(settings, "allow_empty_mining_benefits"))


class TestTheFloorsAreIndependent(RealSettingsFileTestBase):
    """Two rules, one loop - so the wiring is asserted, not assumed.

    They are one class instantiated twice (``MiningFloor``), which is what keeps
    them honest, and exactly why a bug here would look like "declaring one
    intent silently authorised the other".
    """

    def test_the_registered_floors_are_the_two_documented_ones(self):
        self.assertEqual(Settings._FLOORS, (WatchlistFloor, MiningBenefitsFloor))

    def test_each_floor_guards_its_own_key(self):
        self.assertEqual(
            [floor.KEY for floor in Settings._FLOORS], ["games_to_watch", "mining_benefits"]
        )

    def test_a_watchlist_declaration_does_not_let_the_benefits_empty(self):
        self.seed(games_to_watch=["Overwatch"], mining_benefits=ALL_ON)
        settings = Settings()
        settings.declare_empty_watchlist_intent()
        settings.games_to_watch = []
        settings.mining_benefits = ALL_OFF

        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            settings.save()

        stored = self.stored()
        self.assertEqual(stored["games_to_watch"], [])  # declared, so it lands
        self.assertEqual(stored["mining_benefits"], ALL_ON)  # not declared, restored
        self.assertEqual(len(captured.output), 1)
        self.assertIn(SAVE_REFUSAL, captured.output[0])

    def test_both_declared_together_both_land(self):
        self.seed(games_to_watch=["Overwatch"], mining_benefits=ALL_ON)
        settings = Settings()
        settings.declare_empty_watchlist_intent()
        settings.declare_disabled_benefits_intent()
        settings.games_to_watch = []
        settings.mining_benefits = ALL_OFF

        with self.assertNoLogs("TwitchDrops", level="ERROR"):
            settings.save()

        stored = self.stored()
        self.assertEqual(stored["games_to_watch"], [])
        self.assertEqual(stored["mining_benefits"], ALL_OFF)


class TestRequestOnlyKeysNeverBecomeSettings(unittest.IsolatedAsyncioTestCase):
    """End to end over a real file: three payload keys, none of them a setting.

    A request-only key that reached ``setattr`` would be serialized into
    settings.json as if it were a setting, and handed to the dashboard by
    ``GET /api/settings`` - and ``allow_empty_*`` persisted as a standing
    permission is the floor disarmed for good.
    """

    def setUp(self):
        patcher = patch("asyncio.create_task", side_effect=lambda coro: coro.close())
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "settings.json"
        seed = dict(default_settings) | {"games_to_watch": ["Overwatch"]}
        self.path.write_text(json.dumps(seed), encoding="utf-8")
        path_patcher = patch.object(settings_module, "SETTINGS_PATH", self.path)
        path_patcher.start()
        self.addCleanup(path_patcher.stop)
        self.settings = Settings()
        self.manager = SettingsManager(AsyncMock(), self.settings, MagicMock())

    async def test_none_of_the_three_reaches_memory_or_disk(self):
        self.manager.update_settings(
            {
                "games_to_watch": [],
                "allow_empty_games_to_watch": True,
                "expected_games_to_watch": ["Overwatch"],
                "mining_benefits": ALL_OFF,
                "allow_empty_mining_benefits": True,
            }
        )

        stored = json.loads(self.path.read_text(encoding="utf-8"))
        for key in (
            "allow_empty_games_to_watch",
            "allow_empty_mining_benefits",
            "expected_games_to_watch",
        ):
            self.assertNotIn(key, stored)
            self.assertFalse(hasattr(self.settings, key))
        # ...and the two deliberate gestures both actually landed.
        self.assertEqual(stored["games_to_watch"], [])
        self.assertEqual(stored["mining_benefits"], ALL_OFF)


# ---------------------------------------------------------------------------
# The client half. The server refuses to disable every benefit type without
# ``allow_empty_mining_benefits`` on the same request - so a dashboard that
# never sends the flag turns "Deselect all" into a silent no-op, and a dashboard
# that sends it too eagerly asks the server to disable types nobody touched.
# ---------------------------------------------------------------------------


def _save_settings(path: str) -> str:
    return extract_function((_REPO_ROOT / path).read_text(encoding="utf-8"), "saveSettings")


@pytest.mark.parametrize("path", APP_JS_COPIES)
def test_the_intent_flag_exists_in_the_client_at_all(path):
    """Without it the server refuses every deliberate deselect-all, silently."""
    assert "allow_empty_mining_benefits" in _save_settings(path), (
        f"{path}: saveSettings() must be able to send allow_empty_mining_benefits, "
        "or turning every benefit type off is refused server-side with no way to ask"
    )


@pytest.mark.parametrize("path", APP_JS_COPIES)
def test_presence_is_decided_by_type_not_by_truthiness(path):
    """``typeof … === 'boolean'`` is the whole fix.

    ``?.checked`` yields ``undefined`` for a control that has not rendered, and
    ``JSON.stringify`` then drops that key. A falsy check cannot tell that
    ``undefined`` from a genuine ``false``, so a save fired before the Settings
    tab exists reads four undefineds and would declare an intent the user never
    expressed.
    """
    body = _save_settings(path)

    assert re.search(r"typeof\s+\w+\s*===?\s*'boolean'", body), (
        f"{path}: the benefit reads must be filtered by typeof … === 'boolean'; "
        "a truthiness test reads an unrendered checkbox as a deliberate 'off'"
    )


@pytest.mark.parametrize("path", APP_JS_COPIES)
def test_the_flag_needs_every_control_present_and_every_one_off(path):
    """Two conditions, not one. Either alone re-opens the defect.

    "All present values are false" is true of an empty set, so four undefineds
    would satisfy it. "Every control rendered" says nothing about their values.
    """
    body = _save_settings(path)

    completeness = re.search(
        r"(\w+)\.length\s*===?\s*Object\.keys\((\w+)\)\.length", body
    )
    assert completeness, (
        f"{path}: the intent flag must require that every benefit control rendered"
    )
    assert re.search(r"\.every\(\s*\(\[\s*,\s*\w+\s*\]\)\s*=>\s*\w+\s*===?\s*false\s*\)", body), (
        f"{path}: the intent flag must require that every rendered control is OFF"
    )


@pytest.mark.parametrize("path", APP_JS_COPIES)
def test_one_dom_read_feeds_both_the_dict_and_the_flag(path):
    """Two sweeps of the DOM can disagree with each other.

    The user can toggle a checkbox between them, and then the dict says one
    thing while the flag says another - which is either a refused save or a
    disable nobody asked for.
    """
    body = _save_settings(path)

    reads = re.findall(r"getElementById\('mining-benefit-[a-z]+'\)", body)
    assert len(reads) == len(MINING_BENEFIT_KEYS), (
        f"{path}: expected exactly {len(MINING_BENEFIT_KEYS)} benefit-checkbox reads in "
        f"saveSettings(), found {len(reads)}: {reads}"
    )


@pytest.mark.parametrize("path", APP_JS_COPIES)
def test_the_benefits_dict_is_omitted_rather_than_sent_empty(path):
    """An empty dict is not "no opinion" - it re-submits the stored selection.

    ``{}`` merges onto whatever is stored and comes back out as the stored
    selection, so for a user who has deliberately turned everything off it means
    "disable everything" on EVERY unrelated save - and every one of those saves
    is then refused for an intent the request never expressed.
    """
    body = _save_settings(path)

    sent = [line for line in body.splitlines() if re.search(r"(?<!\w)mining_benefits\s*:", line)]
    assert len(sent) == 1, (
        f"{path}: saveSettings() must mention mining_benefits exactly once, found {sent!r}"
    )
    assert "..." in sent[0] and "?" in sent[0], (
        f"{path}: the mining_benefits key must be conditional, not unconditional: "
        f"{sent[0].strip()}"
    )


@pytest.mark.parametrize("path", APP_JS_COPIES)
def test_the_flag_rides_only_on_a_real_declaration(path):
    body = _save_settings(path)

    sent = [
        line for line in body.splitlines()
        if re.search(r"allow_empty_mining_benefits\s*:", line)
    ]
    assert len(sent) == 1, (
        f"{path}: allow_empty_mining_benefits must be sent exactly once, found {sent!r}"
    )
    assert "..." in sent[0] and "?" in sent[0], (
        f"{path}: the intent flag must be conditional: {sent[0].strip()}"
    )


@unittest.skipIf(_NODE is None, "node is not installed; the static assertions still hold")
class TestTheClientPayloadSaysWhatItMeans(unittest.TestCase):
    """The behavioural half: the REAL ``saveSettings`` under a stub DOM.

    A regex cannot tell "present and false" apart from "absent" - which is
    exactly the distinction the fix turns on - so the shipped function is
    extracted and executed, and the assertions are made against the bytes it
    would actually POST. Both copies are driven, because a fix in one only is a
    fix waiting to be undone by the next person who copies the other back.
    """

    def post_body(self, path: str, benefits: dict | None) -> dict:
        completed = subprocess.run(
            [_NODE, str(_DRIVER), str(_REPO_ROOT / path), json.dumps(benefits)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_turning_every_type_off_declares_the_intent(self):
        # The reproduction. Pre-fix the payload carried the all-false dict and
        # no flag, so the server logged PAYLOAD_REFUSAL and nothing changed.
        for path in APP_JS_COPIES:
            with self.subTest(copy=path):
                body = self.post_body(path, dict.fromkeys(MINING_BENEFIT_KEYS, False))

                self.assertEqual(body["mining_benefits"], ALL_OFF)
                self.assertIs(body["allow_empty_mining_benefits"], True)

    def test_leaving_one_type_on_declares_nothing(self):
        for path in APP_JS_COPIES:
            with self.subTest(copy=path):
                benefits = dict.fromkeys(MINING_BENEFIT_KEYS, False) | {"BADGE": True}
                body = self.post_body(path, benefits)

                self.assertEqual(body["mining_benefits"], benefits)
                self.assertNotIn("allow_empty_mining_benefits", body)

    def test_an_unrendered_settings_tab_says_nothing_about_benefits(self):
        # THE case the flag has to get right: four undefineds are "this request
        # is not about benefits", not "the user turned everything off".
        for path in APP_JS_COPIES:
            with self.subTest(copy=path):
                body = self.post_body(path, None)

                self.assertNotIn("mining_benefits", body)
                self.assertNotIn("allow_empty_mining_benefits", body)

    def test_a_partially_rendered_tab_sends_only_what_rendered(self):
        for path in APP_JS_COPIES:
            with self.subTest(copy=path):
                body = self.post_body(path, {"BADGE": False, "EMOTE": False})

                self.assertEqual(body["mining_benefits"], {"BADGE": False, "EMOTE": False})
                self.assertNotIn("allow_empty_mining_benefits", body)

    def test_turning_everything_back_on_declares_nothing(self):
        for path in APP_JS_COPIES:
            with self.subTest(copy=path):
                body = self.post_body(path, dict.fromkeys(MINING_BENEFIT_KEYS, True))

                self.assertEqual(body["mining_benefits"], ALL_ON)
                self.assertNotIn("allow_empty_mining_benefits", body)


@unittest.skipIf(_NODE is None, "node is not installed; the static assertions still hold")
class TestTheClientPayloadRoundTripsToDisk(unittest.IsolatedAsyncioTestCase):
    """The two halves joined: real ``saveSettings`` output, real settings.json.

    Neither half proves the fix on its own. The client can produce a perfect
    payload that the server floor still refuses, and the server can accept a
    payload no client ever sends. What has to hold is that the bytes the
    dashboard POSTs reach disk with the meaning the user gave them.
    """

    def setUp(self):
        patcher = patch("asyncio.create_task", side_effect=lambda coro: coro.close())
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "settings.json"
        path_patcher = patch.object(settings_module, "SETTINGS_PATH", self.path)
        path_patcher.start()
        self.addCleanup(path_patcher.stop)

    def seed(self, mining_benefits: dict) -> SettingsManager:
        seed = dict(default_settings) | {
            "games_to_watch": ["Overwatch"],
            "mining_benefits": dict(mining_benefits),
        }
        self.path.write_text(json.dumps(seed), encoding="utf-8")
        self.settings = Settings()
        return SettingsManager(AsyncMock(), self.settings, MagicMock())

    def post_body(self, path: str, benefits: dict | None) -> dict:
        completed = subprocess.run(
            [_NODE, str(_DRIVER), str(_REPO_ROOT / path), json.dumps(benefits)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def stored(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    async def test_a_deselect_all_from_the_dashboard_reaches_disk(self):
        for path in APP_JS_COPIES:
            with self.subTest(copy=path):
                manager = self.seed(ALL_ON)
                manager.update_settings(
                    self.post_body(path, dict.fromkeys(MINING_BENEFIT_KEYS, False))
                )

                self.assertEqual(self.settings.mining_benefits, ALL_OFF)
                self.assertEqual(self.stored()["mining_benefits"], ALL_OFF)

    async def test_an_unrelated_save_never_disables_an_already_off_selection(self):
        # The regression the omitted key prevents: with an empty dict on the
        # wire this save was refused, and every later one with it, for a user
        # whose stored selection was already all-off.
        for path in APP_JS_COPIES:
            with self.subTest(copy=path):
                manager = self.seed(ALL_OFF)
                manager.update_settings(self.post_body(path, None))

                self.assertEqual(self.settings.mining_benefits, ALL_OFF)
                self.assertEqual(self.stored()["mining_benefits"], ALL_OFF)
                self.assertEqual(self.stored()["games_to_watch"], ["Overwatch"])

    async def test_an_unrendered_tab_leaves_a_healthy_selection_alone(self):
        for path in APP_JS_COPIES:
            with self.subTest(copy=path):
                manager = self.seed(ALL_ON)
                manager.update_settings(self.post_body(path, None))

                self.assertEqual(self.settings.mining_benefits, ALL_ON)
                self.assertEqual(self.stored()["mining_benefits"], ALL_ON)

    async def test_a_partial_deselect_lands_without_any_declaration(self):
        for path in APP_JS_COPIES:
            with self.subTest(copy=path):
                manager = self.seed(ALL_ON)
                benefits = dict.fromkeys(MINING_BENEFIT_KEYS, False) | {"BADGE": True}
                manager.update_settings(self.post_body(path, benefits))

                self.assertEqual(self.settings.mining_benefits, benefits)
                self.assertEqual(self.stored()["mining_benefits"], benefits)

    async def test_turning_everything_back_on_from_an_all_off_account_works(self):
        for path in APP_JS_COPIES:
            with self.subTest(copy=path):
                manager = self.seed(ALL_OFF)
                manager.update_settings(
                    self.post_body(path, dict.fromkeys(MINING_BENEFIT_KEYS, True))
                )

                self.assertEqual(self.settings.mining_benefits, ALL_ON)
                self.assertEqual(self.stored()["mining_benefits"], ALL_ON)


if __name__ == "__main__":
    unittest.main()
