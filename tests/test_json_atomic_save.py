"""Tests for crash-safe JSON files (src/utils/json_utils.py).

Why this file exists. ``json_save`` used to ``open(path, "w")``, which truncates
the target BEFORE the new bytes land. Any interruption in between — SIGKILL from
a container stop, a power cut, a full disk — left a truncated settings.json, and
``json_load(..., default_settings, merge=True)`` then falls back to the defaults,
whose ``games_to_watch`` is ``[]``. That is the five-day silent outage all over
again, from a completely different direction: no client, no endpoint, no cleaner
involved. So the first guarantee under test is the one that closes it — a reader
always sees either the complete previous file or the complete new one.

The second guarantee is the other half of the same story: when a file IS found
unreadable, falling back to defaults may not destroy it. ``json_load(...,
quarantine=True)`` preserves the bytes under a ``.corrupt`` name and reports it
at ERROR, so a settings.json that lost one byte no longer takes the operator's
watch list with it. Generic callers (derived caches) keep the quiet
defaults-and-carry-on behaviour.

``json_save`` is shared by point caches and settings, so the tests cover it
generically and one test walks the settings.json case end to end.
"""

import json
import os
import stat
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from yarl import URL

from src.config.settings import default_settings
from src.utils import json_utils
from src.utils.json_utils import json_load, json_save, merge_json, quarantine_corrupt


class _Interrupted(Exception):
    """Stands in for whatever kills the process mid-write."""


class JsonSaveTestBase(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "settings.json"

    def strays(self) -> list[str]:
        """Anything in the directory that is not the target file."""
        return sorted(p.name for p in self.dir.iterdir() if p != self.path)

    def seed(self, contents: dict) -> str:
        """Write a known-good file the way a previous successful save would."""
        json_save(self.path, contents)
        return self.path.read_text(encoding="utf8")


class TestJsonSaveHappyPath(JsonSaveTestBase):
    def test_round_trips_and_leaves_no_temp_file(self):
        json_save(self.path, {"games_to_watch": ["Overwatch"], "language": "English"})

        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf8")),
            {"games_to_watch": ["Overwatch"], "language": "English"},
        )
        self.assertEqual(self.strays(), [])

    def test_sort_and_indent_behavior_is_unchanged(self):
        json_save(self.path, {"b": 1, "a": 2}, sort=True)
        text = self.path.read_text(encoding="utf8")

        self.assertLess(text.index('"a"'), text.index('"b"'))
        self.assertIn('\n    "a": 2', text)  # indent=4 preserved

    def test_custom_serializer_is_still_used(self):
        # _serialize/_deserialize round-trip; sets are the cheapest witness.
        json_save(self.path, {"topics": {"a", "b"}})

        loaded = json_load(self.path, {}, merge=False)
        self.assertEqual(loaded["topics"], {"a", "b"})

    def test_file_is_created_0o600(self):
        json_save(self.path, {"proxy": "http://user:pass@host:8080"})

        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_a_pre_existing_loose_mode_file_ends_up_hardened(self):
        # The old code chmod'ed the target after writing it; os.replace now
        # carries mkstemp's 0o600 over instead. Same guarantee, so assert it.
        self.path.write_text("{}", encoding="utf8")
        os.chmod(self.path, 0o644)

        json_save(self.path, {"proxy": "http://user:pass@host:8080"})

        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_the_payload_is_fsynced_before_the_rename(self):
        # Without the fsync the rename can be durable while the bytes it points
        # at are not — the exact shape of corruption this is meant to prevent.
        order: list[str] = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            order.append("fsync")
            real_fsync(fd)

        def replace(src, dst):
            order.append("replace")
            real_replace(src, dst)

        with (
            patch("src.utils.json_utils.os.fsync", side_effect=fsync),
            patch("src.utils.json_utils.os.replace", side_effect=replace),
        ):
            json_save(self.path, {"a": 1})

        self.assertEqual(order, ["fsync", "replace"])

    def test_the_temp_file_lives_in_the_target_directory(self):
        # A temp file on another filesystem would make os.replace fail with
        # EXDEV, so the directory is part of the contract, not an incident.
        seen: list[Path] = []
        real_replace = os.replace

        def record(src, dst):
            seen.append(Path(src))
            real_replace(src, dst)

        with patch("src.utils.json_utils.os.replace", side_effect=record):
            json_save(self.path, {"a": 1})

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].parent, self.path.parent)


class TestJsonSaveIsAtomic(JsonSaveTestBase):
    """A failure anywhere before the rename must be a complete no-op."""

    def _fail_during_dump(self, exc: BaseException):
        """Write a partial payload, then blow up — a kill mid-``json.dump``."""

        def dump(contents, file, **kwargs):
            file.write('{"games_to_watch": ["Ove')
            raise exc

        return patch("src.utils.json_utils.json.dump", side_effect=dump)

    def test_interrupted_write_leaves_the_original_intact(self):
        before = self.seed({"games_to_watch": ["Overwatch"], "language": "English"})

        with self._fail_during_dump(_Interrupted("killed")), self.assertRaises(_Interrupted):
            json_save(self.path, {"games_to_watch": ["Rust"]})

        self.assertEqual(self.path.read_text(encoding="utf8"), before)
        self.assertEqual(self.strays(), [])

    def test_disk_full_leaves_the_original_intact(self):
        before = self.seed({"games_to_watch": ["Overwatch"]})

        full_disk = OSError(28, "No space left on device")
        with self._fail_during_dump(full_disk), self.assertRaises(OSError):
            json_save(self.path, {"games_to_watch": ["Rust"]})

        self.assertEqual(self.path.read_text(encoding="utf8"), before)
        self.assertEqual(self.strays(), [])

    def test_a_base_exception_also_cleans_up(self):
        # KeyboardInterrupt/SystemExit are not Exceptions; a bare `except
        # Exception` would leak a temp file on every Ctrl-C.
        before = self.seed({"games_to_watch": ["Overwatch"]})

        with self._fail_during_dump(KeyboardInterrupt()), self.assertRaises(KeyboardInterrupt):
            json_save(self.path, {"games_to_watch": ["Rust"]})

        self.assertEqual(self.path.read_text(encoding="utf8"), before)
        self.assertEqual(self.strays(), [])

    def test_an_unserializable_value_leaves_the_original_intact(self):
        before = self.seed({"games_to_watch": ["Overwatch"]})

        with self.assertRaises(TypeError):
            json_save(self.path, {"nope": object()})

        self.assertEqual(self.path.read_text(encoding="utf8"), before)
        self.assertEqual(self.strays(), [])

    def test_the_target_is_never_truncated_while_the_new_file_is_written(self):
        # The heart of the fix: at every point during the write the target still
        # holds the complete OLD document, because the new bytes go elsewhere.
        before = self.seed({"games_to_watch": ["Overwatch"]})
        observed: list[str] = []
        real_dump = json.dump

        def dump(contents, file, **kwargs):
            observed.append(self.path.read_text(encoding="utf8"))
            real_dump(contents, file, **kwargs)

        with patch("src.utils.json_utils.json.dump", side_effect=dump):
            json_save(self.path, {"games_to_watch": ["Rust"]})

        self.assertEqual(observed, [before])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf8")),
                         {"games_to_watch": ["Rust"]})

    def test_settings_survive_an_interrupted_save_end_to_end(self):
        # The regression in the terms it actually bit: an interrupted save used
        # to truncate settings.json, so the next load fell back to defaults —
        # games_to_watch: [] — and the miner mined nothing.
        seed = dict(default_settings)
        seed["games_to_watch"] = ["Overwatch"]
        json_save(self.path, seed, sort=True)

        killed = _Interrupted("container stopped")
        with self._fail_during_dump(killed), self.assertRaises(_Interrupted):
            json_save(self.path, seed, sort=True)

        reloaded = json_load(self.path, default_settings, merge=True)
        self.assertEqual(reloaded["games_to_watch"], ["Overwatch"])
        self.assertNotEqual(reloaded["games_to_watch"], default_settings["games_to_watch"])


class TestCorruptFileQuarantine(JsonSaveTestBase):
    """An unreadable file whose contents cannot be derived again is preserved.

    Falling back to defaults is only safe while the bytes that failed to parse
    are still recoverable. For settings.json they were not: the file stayed put,
    the miner ran on defaults, and the first save afterwards overwrote the only
    copy of the operator's watch list. So the opt-in caller moves the file aside
    and reports it at ERROR, while callers whose data is derived (point caches,
    chest snapshots) keep the old quiet fallback.
    """

    TRUNCATED = '{"games_to_watch": ["Overwatch", "Rust"'

    def corrupt(self, name="settings.json", contents=None) -> Path:
        path = self.dir / name
        path.write_text(self.TRUNCATED if contents is None else contents, encoding="utf8")
        return path

    def names(self) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir())

    def test_a_generic_caller_still_falls_back_quietly_and_keeps_the_file(self):
        # A point cache is derived data: the next successful save heals it, and
        # a .corrupt file per restart would be litter, not evidence.
        path = self.corrupt("channel_points.json")

        with self.assertLogs("TwitchDrops", level="WARNING") as captured:
            loaded = json_load(path, {}, merge=False)

        self.assertEqual(loaded, {})
        self.assertEqual([record.levelname for record in captured.records], ["WARNING"])
        self.assertEqual(self.names(), ["channel_points.json"])

    def test_an_opt_in_caller_preserves_the_bytes_and_reports_at_error(self):
        path = self.corrupt()

        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            loaded = json_load(path, default_settings, merge=True, quarantine=True)

        preserved = self.dir / "settings.json.corrupt"
        self.assertTrue(preserved.exists())
        # Byte-for-byte: hand recovery is the whole point of keeping it.
        self.assertEqual(preserved.read_text(encoding="utf8"), self.TRUNCATED)
        self.assertFalse(path.exists())
        self.assertEqual(loaded["games_to_watch"], [])
        self.assertIn(preserved.name, captured.output[0])

    def test_byte_level_corruption_is_handled_like_bad_json(self):
        # Regression: the handler caught json.JSONDecodeError only, so a file
        # corrupted below the text layer raised UnicodeDecodeError straight past
        # it. For settings.json that was exit code 4, and under
        # `restart: unless-stopped`, a container restart loop.
        path = self.dir / "settings.json"
        path.write_bytes(b"\xff\xfe\x00{not json")

        with self.assertLogs("TwitchDrops", level="ERROR"):
            loaded = json_load(path, default_settings, merge=True, quarantine=True)

        self.assertTrue((self.dir / "settings.json.corrupt").exists())
        self.assertEqual(loaded["games_to_watch"], [])

    def test_a_second_corruption_does_not_clobber_the_first(self):
        for _ in range(2):
            path = self.corrupt()
            with self.assertLogs("TwitchDrops", level="ERROR"):
                json_load(path, default_settings, merge=True, quarantine=True)

        self.assertIn("settings.json.corrupt", self.names())
        self.assertIn("settings.json.corrupt.1", self.names())

    def test_the_numbered_slots_are_bounded_by_a_timestamped_fallback(self):
        # The search must not turn into an unbounded stat loop on a data
        # directory that has collected quarantines for months.
        with patch.object(json_utils, "_MAX_QUARANTINE_SLOTS", 2):
            for _ in range(3):
                quarantine_corrupt(self.corrupt())

        names = self.names()
        self.assertIn("settings.json.corrupt", names)
        self.assertIn("settings.json.corrupt.1", names)
        self.assertEqual(len(names), 3)
        timestamped = [n for n in names if n not in
                       ("settings.json.corrupt", "settings.json.corrupt.1")]
        self.assertRegex(timestamped[0], r"^settings\.json\.corrupt\.\d{8}T\d{6}\d+$")

    def test_a_move_that_fails_leaves_the_file_where_it_is(self):
        # A read-only data directory: the corrupt file must be left intact
        # rather than deleted, and the operator still has to be told.
        path = self.corrupt()

        with patch("src.utils.json_utils.os.replace", side_effect=OSError("read-only")):
            self.assertIsNone(quarantine_corrupt(path))
            with self.assertLogs("TwitchDrops", level="ERROR") as captured:
                loaded = json_load(path, default_settings, merge=True, quarantine=True)

        self.assertEqual(path.read_text(encoding="utf8"), self.TRUNCATED)
        self.assertEqual(self.names(), ["settings.json"])
        self.assertEqual(loaded["games_to_watch"], [])
        self.assertIn("could not be moved aside", captured.output[0])

    def test_quarantine_returns_the_path_it_moved_the_file_to(self):
        path = self.corrupt()

        preserved = quarantine_corrupt(path)

        self.assertEqual(preserved, self.dir / "settings.json.corrupt")
        self.assertEqual(preserved.read_text(encoding="utf8"), self.TRUNCATED)

    def test_the_preserved_copy_is_not_left_world_readable(self):
        # os.replace carries the ORIGINAL mode across, and nothing ever rewrites
        # a quarantined file, so a settings.json created by hand (or by a version
        # that predates json_save's 0o600 guarantee) stayed readable by every
        # user on the host, for good - while holding the proxy URL, which may
        # embed credentials, and the discord_webhook_* URLs.
        secret = '{"proxy": "http://user:hunter2@proxy.example:8080"'
        path = self.corrupt(contents=secret)
        os.chmod(path, 0o644)

        preserved = quarantine_corrupt(path)

        self.assertEqual(stat.S_IMODE(preserved.stat().st_mode), 0o600)
        # Tightening the mode may not touch the bytes - they are the evidence.
        self.assertEqual(preserved.read_text(encoding="utf8"), secret)

    def test_a_chmod_that_fails_does_not_lose_the_evidence(self):
        # Best-effort like the rest of this module: a filesystem that cannot
        # change the mode must not turn preserving the file into losing it.
        path = self.corrupt()
        os.chmod(path, 0o644)

        with patch("src.utils.json_utils.os.chmod", side_effect=OSError("unsupported")):
            preserved = quarantine_corrupt(path)

        self.assertIsNotNone(preserved)
        self.assertEqual(preserved.read_text(encoding="utf8"), self.TRUNCATED)

    def test_the_load_path_hardens_the_copy_it_preserves(self):
        # The same guarantee through the caller that actually quarantines.
        path = self.corrupt()
        os.chmod(path, 0o644)

        with self.assertLogs("TwitchDrops", level="ERROR"):
            json_load(path, default_settings, merge=True, quarantine=True)

        preserved = self.dir / "settings.json.corrupt"
        self.assertEqual(stat.S_IMODE(preserved.stat().st_mode), 0o600)


class TestStructurallyCorruptFilesAreHandled(JsonSaveTestBase):
    """"Unparseable" has to mean structurally, not just syntactically.

    ``json_load`` promises a dict. A file whose JSON is perfectly well-formed
    but is not one - ``[]``, ``42``, ``"hello"``, ``null`` - used to sail past
    the ``ValueError`` handler and die in ``_remove_missing`` with ``'list'
    object has no attribute 'items'``. So did a recognized ``__type`` wrapper
    holding a payload its constructor rejects, with a ``TypeError``. Neither was
    caught anywhere: for settings.json that is ``sys.exit(4)`` on every boot,
    and under ``restart: unless-stopped`` a restart loop that quarantines
    nothing and tells the operator nothing - the container is down and the file
    that put it there is still sitting where it was.

    All of them are the same event as a truncated file ("these bytes are not the
    JSON we wrote"), so they now take the same route: defaults, quarantine when
    the caller asked for it, ERROR, and a process that comes up.
    """

    # Every shape that used to escape the handler. The names are what the
    # failure looks like on disk, not what raised, because the point is that the
    # caller no longer has to know which internal error each one produced.
    UNUSABLE = {
        "a top-level array": "[]",
        "a top-level array with data": '["War Thunder", "Overwatch 2"]',
        "a top-level string": '"hello"',
        "a top-level number": "42",
        "a top-level null": "null",
        # A recognized wrapper whose payload the constructor rejects: set(5).
        "a set built from a number": '{"__type": "set", "data": 5}',
        # ...and the one the brief did not name: no "data" key at all, KeyError.
        "a wrapper with no data key": '{"__type": "set"}',
        # datetime.fromtimestamp(1e300) -> OverflowError, not a TypeError.
        "a datetime out of range": '{"__type": "datetime", "data": 1e300}',
        # Decodes fine, and produces a set where the object belongs.
        "a top-level set": '{"__type": "set", "data": [1, 2]}',
        # An unknown wrapper at the top level decodes to the _MISSING sentinel.
        "a top-level unknown wrapper": '{"__type": "Decimal", "data": "1"}',
        # A sentinel inside a LIST: dropping it would silently shorten the watch
        # list, and leaving it made every later json_save raise TypeError.
        "an unknown wrapper inside a list": (
            '{"games_to_watch": ["Rust", {"__type": "Decimal", "data": "1"}]}'
        ),
        "an unknown wrapper nested deeper": (
            '{"games_to_watch": [["Rust", {"__type": "Decimal", "data": "1"}]]}'
        ),
    }

    def write(self, text: str) -> Path:
        self.path.write_text(text, encoding="utf8")
        return self.path

    def test_every_unusable_shape_loads_defaults_and_is_quarantined(self):
        for name, text in self.UNUSABLE.items():
            with self.subTest(stored=name):
                self.write(text)

                with self.assertLogs("TwitchDrops", level="ERROR") as captured:
                    loaded = json_load(self.path, default_settings, merge=True, quarantine=True)

                preserved = self.dir / "settings.json.corrupt"
                self.assertEqual(loaded["games_to_watch"], [])
                self.assertEqual(preserved.read_text(encoding="utf8"), text)
                self.assertFalse(self.path.exists())
                self.assertIn(preserved.name, captured.output[0])
                preserved.unlink()

    def test_a_generic_caller_keeps_the_quiet_fallback(self):
        for name, text in self.UNUSABLE.items():
            with self.subTest(stored=name):
                path = self.dir / "channel_points.json"
                path.write_text(text, encoding="utf8")

                with self.assertLogs("TwitchDrops", level="WARNING") as captured:
                    loaded = json_load(path, {}, merge=False)

                self.assertEqual(loaded, {})
                self.assertEqual([r.levelname for r in captured.records], ["WARNING"])
                # Derived data, so the file stays where it is and the next
                # successful save heals it.
                self.assertEqual(path.read_text(encoding="utf8"), text)

    def test_the_stored_value_is_never_logged(self):
        # The wrapper payload can be a proxy URL, and this text reaches
        # logs/TDM.log. Naming the TYPE is enough to find the offending line.
        self.write('{"__type": "URL", "data": {"user": "hunter2"}}')

        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            json_load(self.path, default_settings, merge=True, quarantine=True)

        self.assertNotIn("hunter2", " ".join(captured.output))

    def test_a_sentinel_in_a_list_can_no_longer_poison_every_later_save(self):
        # The regression itself. _remove_missing only ever walked dicts, so the
        # sentinel survived inside games_to_watch, and _serialize then raised
        # TypeError on that save AND every save after it: settings could never
        # be persisted again, and the miner was matching a bare object against
        # channel game names.
        self.write('{"games_to_watch": ["Rust", {"__type": "Decimal", "data": "1"}]}')

        with self.assertLogs("TwitchDrops", level="ERROR"):
            loaded = json_load(self.path, default_settings, merge=True, quarantine=True)

        self.assertEqual(loaded["games_to_watch"], [])
        json_save(self.path, loaded)  # used to raise TypeError(<object ...>)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf8"))["games_to_watch"], [])

    def test_a_dict_inside_a_list_is_cleaned_rather_than_refused(self):
        # A list has no per-element template, so an element cannot be dropped
        # without silently shortening it. A DICT inside one can lose a key,
        # because merge_json puts the template's default back.
        self.write('{"inventory_filters": [{"keep": 1, "gone": {"__type": "Decimal", "data": 1}}]}')

        loaded = json_load(self.path, {}, merge=False)

        self.assertEqual(loaded["inventory_filters"], [{"keep": 1}])

    def test_valid_special_types_still_round_trip(self):
        # The refusals must not have cost the feature they guard: sets,
        # datetimes and URLs are real stored values, including inside lists.
        payload = {
            "a_set": {"one", "two"},
            "a_time": datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
            "a_url": URL("https://example.invalid/x"),
            "in_a_list": [{"one"}, URL("https://example.invalid/y")],
        }

        json_save(self.path, payload)
        loaded = json_load(self.path, {}, merge=False)

        self.assertEqual(loaded, payload)


class TestWrongTypesAreReported(JsonSaveTestBase):
    """Every branch of ``merge_json`` that destroys a stored value says so.

    ``merge_json`` guarantees the SHAPE of a stored file by overwriting any key
    whose type does not match the template. For ``games_to_watch`` that means a
    hand-edited ``"War Thunder"`` (a string where a list belongs) silently
    becomes ``[]`` - and an empty watch list means the miner mines nothing. The
    whole event left no trace on the console and none in the log, so the
    operator's only symptom was, once again, mining that stopped for no reason.

    Deleting a key the template does not list is destructive in exactly the same
    way - whatever the operator had put there is gone - so it is reported too,
    as ONE line per object rather than one per key, which keeps a legacy file
    that is a few settings behind down to a line instead of a wall. Filling in a
    MISSING key stays silent: it adds a default and takes nothing away.
    """

    def load(self, stored: dict):
        self.path.write_text(json.dumps(stored), encoding="utf8")
        return json_load(self.path, default_settings, merge=True)

    def test_a_wrong_typed_watch_list_is_reported_with_its_key(self):
        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            loaded = self.load(dict(default_settings) | {"games_to_watch": "War Thunder"})

        self.assertEqual(loaded["games_to_watch"], [])
        line = captured.output[0]
        # The file is named too: "games_to_watch" alone does not say WHICH json.
        self.assertIn(f"{self.path}:games_to_watch", line)
        self.assertIn("expected list, found str", line)

    def test_a_nested_mismatch_names_the_key_it_lives_under(self):
        stored = dict(default_settings)
        stored["inventory_filters"] = dict(stored["inventory_filters"]) | {"show_active": "yes"}

        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            loaded = self.load(stored)

        self.assertIn(f"{self.path}:inventory_filters.show_active", captured.output[0])
        self.assertIs(loaded["inventory_filters"]["show_active"], False)

    def test_the_offending_value_is_never_logged(self):
        # SecretRedactingFilter (src/utils/log_redaction.py) covers auth tokens
        # only - not proxy, not the webhooks - so a wrong-typed credential would
        # go to logs/TDM.log in the clear. The key is enough to find the line.
        secret = "http://user:hunter2@proxy.example:8080"

        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            self.load(dict(default_settings) | {"proxy": [secret]})

        self.assertIn("proxy", captured.output[0])
        self.assertNotIn("hunter2", " ".join(captured.output))

    def test_a_merge_that_only_adds_a_default_says_nothing(self):
        # A legacy settings.json, missing a key added since. Nothing of the
        # operator's is touched, and an ERROR per added default on every upgrade
        # is noise that teaches people to scroll past the line that matters.
        stored = dict(default_settings)
        stored.pop("dark_mode")

        with self.assertNoLogs("TwitchDrops", level="DEBUG"):
            loaded = self.load(stored)

        self.assertIs(loaded["dark_mode"], False)

    def test_a_discarded_unknown_key_is_reported(self):
        # A downgrade, or a rename: the key goes, and so does whatever it held.
        # Silence here meant a setting could vanish with no trace at all.
        stored = dict(default_settings) | {"a_setting_from_the_future": 1}

        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            loaded = self.load(stored)

        self.assertNotIn("a_setting_from_the_future", loaded)
        self.assertIn(f"{self.path}:a_setting_from_the_future", captured.output[0])

    def test_several_discarded_keys_are_reported_on_one_line(self):
        stored = dict(default_settings) | {"gone_one": 1, "gone_two": 2}

        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            self.load(stored)

        self.assertEqual(len(captured.output), 1)
        self.assertIn("gone_one", captured.output[0])
        self.assertIn("gone_two", captured.output[0])

    def test_a_discarded_keys_value_is_never_logged(self):
        # The mirror of test_the_offending_value_is_never_logged: a proxy URL
        # under a key this version no longer knows is still a credential.
        secret = "http://user:hunter2@proxy.example:8080"
        stored = dict(default_settings) | {"proxy_v2": secret}

        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            self.load(stored)

        self.assertIn("proxy_v2", captured.output[0])
        self.assertNotIn("hunter2", " ".join(captured.output))


class TestOpenMapsAreNotPruned(JsonSaveTestBase):
    """An empty template is an open map, and its keys are the operator's data.

    A NON-EMPTY template is a schema: it enumerates every key that may exist, so
    a stored key it does not list is a setting this version removed and pruning
    it is right. An EMPTY template enumerates nothing, so it cannot be an
    authority on what is unknown - and treating it as one deleted real data.

    ``channel_strategies`` is ``{}`` in ``default_settings`` and holds one entry
    per channel the operator picked a betting strategy for. Every load wiped all
    of them, and the next save persisted ``{}``: the same silent loss as the
    empty watch list, one setting over.
    """

    def load(self, stored: dict):
        self.path.write_text(json.dumps(stored), encoding="utf8")
        return json_load(self.path, default_settings, merge=True)

    def test_an_open_maps_entries_survive_a_merge(self):
        strategies = {"somechannel": "conservative", "otherchannel": "aggressive"}
        stored = dict(default_settings) | {"channel_strategies": strategies}

        with self.assertNoLogs("TwitchDrops", level="DEBUG"):
            loaded = self.load(stored)

        self.assertEqual(loaded["channel_strategies"], strategies)

    def test_an_open_maps_entries_survive_a_save_and_load_round_trip(self):
        # The loss was only cemented by the save that followed the load, so the
        # round trip is the regression, not the load alone.
        strategies = {"somechannel": "conservative"}
        loaded = self.load(dict(default_settings) | {"channel_strategies": strategies})

        json_save(self.path, loaded)

        self.assertEqual(
            json_load(self.path, default_settings, merge=True)["channel_strategies"],
            strategies,
        )

    def test_an_open_map_does_not_have_value_types_enforced(self):
        # There is no template entry to compare against, so there is no wrong
        # type to report - only data this version does not interpret.
        stored = dict(default_settings) | {"channel_strategies": {"somechannel": 7}}

        with self.assertNoLogs("TwitchDrops", level="DEBUG"):
            loaded = self.load(stored)

        self.assertEqual(loaded["channel_strategies"], {"somechannel": 7})

    def test_a_non_empty_template_still_prunes(self):
        # The other half: mining_benefits IS a schema (a fixed enum of benefit
        # types), so a key outside it is not data and must still go.
        stored = dict(default_settings)
        stored["mining_benefits"] = dict(stored["mining_benefits"]) | {"WEIRD": True}

        with self.assertLogs("TwitchDrops", level="ERROR") as captured:
            loaded = self.load(stored)

        self.assertNotIn("WEIRD", loaded["mining_benefits"])
        self.assertIn(f"{self.path}:mining_benefits.WEIRD", captured.output[0])

    def test_merging_against_an_empty_template_is_a_no_op(self):
        stored = {"anything": 1, "nested": {"deeper": 2}}
        subject = dict(stored)

        merge_json(subject, {}, label="x:")

        self.assertEqual(subject, stored)


class TestStaleTempSweep(JsonSaveTestBase):
    """A successful save collects the temp files earlier kills left behind.

    ``json_save`` writes ``.<target>.XXXXXX.tmp`` and renames it into place, so a
    SIGKILL in between leaves the temp file on disk. Nothing ever removed those,
    and a container that is killed on every stop accumulates one per stop. The
    sweep is deliberately narrow: only this target's temps, only ones old enough
    that no save could still be holding them.
    """

    def temp_for(self, target: str, *, age: float) -> Path:
        path = self.dir / f".{target}.abc123{json_utils._TEMP_SUFFIX}"
        path.write_text('{"half-written', encoding="utf8")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def test_a_save_reaps_this_targets_abandoned_temp_file(self):
        stale = self.temp_for("settings.json", age=json_utils._STALE_TEMP_AGE * 2)

        json_save(self.path, {"games_to_watch": ["Overwatch"]})

        self.assertFalse(stale.exists())
        self.assertEqual(self.strays(), [])

    def test_a_concurrent_saves_fresh_temp_file_is_left_alone(self):
        # A second process may be milliseconds from renaming this into place;
        # deleting it would turn a housekeeping chore into data loss.
        fresh = self.temp_for("settings.json", age=0)

        json_save(self.path, {"games_to_watch": ["Overwatch"]})

        self.assertTrue(fresh.exists())

    def test_another_targets_temp_file_is_left_alone(self):
        other = self.temp_for("channel_points.json", age=json_utils._STALE_TEMP_AGE * 2)

        json_save(self.path, {"games_to_watch": ["Overwatch"]})

        self.assertTrue(other.exists())

    def test_a_sweep_that_cannot_delete_does_not_fail_the_save(self):
        # Reaping garbage must never be the reason a settings write fails.
        self.temp_for("settings.json", age=json_utils._STALE_TEMP_AGE * 2)

        with patch.object(Path, "unlink", side_effect=OSError("busy")):
            json_save(self.path, {"games_to_watch": ["Overwatch"]})

        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf8")),
            {"games_to_watch": ["Overwatch"]},
        )


class TestStaleQuarantineSweep(JsonSaveTestBase):
    """Quarantines are collected too, but timidly - they may be the last copy.

    Nothing ever collected them: a filesystem that corrupts settings.json on
    every boot fills the data directory with ``settings.json.corrupt``,
    ``.corrupt.1`` ... ``.corrupt.99`` and then an unbounded run of timestamped
    names, and once the numbered slots are gone every later quarantine costs a
    hundred failed ``stat`` calls as well.

    So the reaper is deliberately far more timid than the temp sweep: thirty
    days rather than an hour, and the OLDEST one survives whatever its age,
    because ``quarantine_corrupt`` already refuses to clobber an earlier
    quarantine for the same reason - the earliest copy is the one most likely to
    hold real pre-corruption data.

    Age is never slept for: ``_STALE_QUARANTINE_AGE`` is patched down and mtimes
    are set with ``os.utime``, which is honest because plain mtime IS the clock
    the reaper reads, and ``quarantine_corrupt`` stamps it deliberately.
    """

    TRUNCATED = '{"games_to_watch": ["Overwatch", "Rust"'

    def setUp(self):
        super().setUp()
        # Patched rather than slept through; see the class docstring.
        patcher = patch.object(json_utils, "_STALE_QUARANTINE_AGE", 60.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def aged(self, name: str, *, age: float) -> Path:
        path = self.dir / name
        path.write_text(self.TRUNCATED, encoding="utf8")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def corrupt_now(self) -> Path:
        self.path.write_text(self.TRUNCATED, encoding="utf8")
        preserved = quarantine_corrupt(self.path)
        assert preserved is not None
        return preserved

    def names(self) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir())

    def old(self) -> float:
        return json_utils._STALE_QUARANTINE_AGE * 2

    def test_expired_quarantines_are_reaped_except_the_oldest(self):
        # The oldest is the unnumbered .corrupt: the first one ever taken, and
        # the one whose bytes pre-date every later corruption.
        for index, name in enumerate(
            ["settings.json.corrupt", "settings.json.corrupt.1", "settings.json.corrupt.2"]
        ):
            self.aged(name, age=self.old() + (10 - index))

        self.corrupt_now()

        self.assertEqual(
            self.names(), ["settings.json.corrupt", "settings.json.corrupt.3"]
        )

    def test_a_lone_expired_quarantine_is_never_reaped(self):
        # Reaping it would leave the operator with no evidence at all, which is
        # strictly worse than one stale file.
        self.aged("settings.json.corrupt", age=self.old())

        preserved = self.corrupt_now()

        self.assertTrue((self.dir / "settings.json.corrupt").exists())
        self.assertEqual(preserved.name, "settings.json.corrupt.1")

    def test_fresh_quarantines_are_left_alone(self):
        for name in ("settings.json.corrupt", "settings.json.corrupt.1"):
            self.aged(name, age=0)

        self.corrupt_now()

        self.assertEqual(len(self.names()), 3)

    def test_another_targets_quarantine_is_left_alone(self):
        self.aged("settings.json.corrupt", age=self.old())
        self.aged("settings.json.corrupt.1", age=self.old())
        other = self.aged("channel_points.json.corrupt", age=self.old())

        self.corrupt_now()

        self.assertTrue(other.exists())

    def test_a_temp_file_is_not_reaped_by_the_quarantine_sweep(self):
        # Different pattern, different age rule: the two sweeps must not reach
        # into each other's files.
        self.aged("settings.json.corrupt", age=self.old())
        self.aged("settings.json.corrupt.1", age=self.old())
        temp = self.aged(f".settings.json.abc123{json_utils._TEMP_SUFFIX}", age=0)

        self.corrupt_now()

        self.assertTrue(temp.exists())

    def test_a_sweep_that_cannot_delete_still_returns_the_new_quarantine(self):
        # Reaping is housekeeping; failing it must never be what loses the file
        # the quarantine was called to preserve.
        self.aged("settings.json.corrupt", age=self.old())
        self.aged("settings.json.corrupt.1", age=self.old())
        self.path.write_text(self.TRUNCATED, encoding="utf8")

        with patch.object(Path, "unlink", side_effect=OSError("busy")):
            preserved = quarantine_corrupt(self.path)

        self.assertEqual(preserved, self.dir / "settings.json.corrupt.2")
        self.assertEqual(preserved.read_text(encoding="utf8"), self.TRUNCATED)

    def test_the_preserved_copys_mtime_is_stamped_to_now(self):
        # Load-bearing: os.replace carries the ORIGINAL mtime across, so a
        # settings.json last saved months before it was corrupted would land
        # already looking ancient and be reaped on the spot.
        ancient = time.time() - self.old()
        self.path.write_text(self.TRUNCATED, encoding="utf8")
        os.utime(self.path, (ancient, ancient))
        before = time.time()

        preserved = quarantine_corrupt(self.path)

        self.assertGreaterEqual(preserved.stat().st_mtime, before - 1)


if __name__ == "__main__":
    unittest.main()
