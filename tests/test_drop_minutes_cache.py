"""``drop_minutes_cache`` is a derived file that used to be able to kill the miner.

Everything in this module is re-derivable from the API on the next inventory
fetch, which is exactly why none of it is worth an outage. Two ways it produced
one, both under test here:

* **A shape the module never wrote.** ``json.loads`` happily returns a ``list``
  or a ``str``-valued mapping, and the rest of the module then used it as
  ``dict[str, int]``. A stored ``[]`` made :func:`get` raise
  ``AttributeError: 'list' object has no attribute 'get'``; a stored
  ``{"<id>": "5"}`` made it raise ``TypeError: '>' not supported between
  instances of 'str' and 'int'``. Both raise from inside ``TimedDrop.__init__``,
  i.e. inside the inventory parse, and neither is in the tuple ``Twitch.run()``
  recovers from - so the process exits 1, the container restarts, reads the same
  file and dies again. :func:`~src.services.drop_minutes_cache._coerce` is the
  answer: an entry that could not have come from :func:`update` is dropped.
* **A number too big to be true.** A cached figure above the drop's
  ``required_minutes`` cannot have come from ``update`` (the model clamps before
  storing), and for a badge or emote drop it makes ``TimedDrop.__init__`` infer
  ``is_claimed`` on a drop that is not finished. The stream selector then skips
  it, the game leaves ``wanted_games``, and nothing is logged. The ``maximum=``
  refusal in :func:`get` and the ``maximum=self.required_minutes`` argument at
  the call site are one guard in two files, so both halves are pinned.

Every test drives the real module against a real file in a temp directory;
``_CACHE_FILE`` is patched rather than ``DATA_DIR`` so nothing here can touch
the developer's own cache.
"""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.models.drop import TimedDrop
from src.services import drop_minutes_cache as cache


class CacheTestBase(unittest.TestCase):
    """A real cache file in a temp dir, with the module pointed at it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "drop_minutes_cache.json"
        patcher = patch.object(cache, "_CACHE_FILE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The module keeps process-global state; restore whatever the rest of
        # the suite had so import order cannot make these tests order-dependent.
        previous, previous_dirty = dict(cache._cache), cache._dirty
        self.addCleanup(lambda: (cache.__setattr__("_cache", previous),
                                 cache.__setattr__("_dirty", previous_dirty)))
        cache._cache = {}
        cache._dirty = False

    def load(self, text: str) -> None:
        """Write raw bytes into the cache file and load them through the module."""
        self.path.write_text(text, encoding="utf-8")
        cache.load()

    def logs(self):
        """Capture what the cache module itself reports while the block runs."""
        return self.assertLogs(cache.logger, level=logging.DEBUG)

    def json_logs(self):
        """Capture what the shared JSON layer reports (a different logger)."""
        return self.assertLogs("TwitchDrops", level=logging.DEBUG)


class TestACacheShapeTheModuleNeverWroteIsDropped(CacheTestBase):
    """``_coerce``: the guard that stops a derived file from ending the process."""

    def test_a_list_cache_does_not_raise_on_the_next_read(self):
        # The reproduction. Pre-guard this stored a list in _cache and the get()
        # below raised AttributeError inside the inventory parse. (The shared
        # JSON layer refuses a non-object file first, so _coerce receives {} -
        # belt and braces, and both belts are asserted.)
        with self.json_logs():
            self.load("[]")

        self.assertEqual(cache._cache, {})
        self.assertEqual(cache.get("d1", 0), 0)

    def test_a_string_minutes_value_is_dropped_rather_than_compared(self):
        # max("5", 0) is the TypeError half of the same failure.
        with self.logs():
            self.load('{"d1": "5"}')

        self.assertEqual(cache.get("d1", 3), 3)

    def test_every_unusable_entry_type_is_dropped_and_the_good_ones_survive(self):
        cases = {
            "a string": '{"good": 12, "bad": "5"}',
            "a null": '{"good": 12, "bad": null}',
            "a float": '{"good": 12, "bad": 4.5}',
            "a nested object": '{"good": 12, "bad": {"minutes": 4}}',
            "a list": '{"good": 12, "bad": [4]}',
            "a negative count": '{"good": 12, "bad": -4}',
        }
        for label, text in cases.items():
            with self.subTest(entry=label):
                with self.logs():
                    self.load(text)
                self.assertEqual(cache._cache, {"good": 12})

    def test_a_boolean_is_not_accepted_as_one_minute(self):
        # bool subclasses int, so a plain isinstance(minutes, int) would store
        # True and hand back 1 - a number that looks like a measurement.
        with self.logs():
            self.load('{"d1": true, "d2": false}')

        self.assertEqual(cache._cache, {})

    def test_a_non_string_key_is_dropped(self):
        # JSON object keys are always strings on the way in, but the coercion is
        # what makes that a guarantee rather than an assumption - _cache is also
        # written in-process by update().
        with self.logs():
            coerced = cache._coerce({7: 7, "d1": 7})

        self.assertEqual(coerced, {"d1": 7})

    def test_a_zero_minute_entry_is_kept(self):
        # Zero is a real measurement, not corruption; dropping it would make the
        # guard lossy for no reason.
        self.load('{"d1": 0}')
        self.assertEqual(cache._cache, {"d1": 0})

    def test_a_healthy_cache_is_left_completely_alone(self):
        self.load('{"d1": 7, "d2": 0, "d3": 1440}')
        self.assertEqual(cache._cache, {"d1": 7, "d2": 0, "d3": 1440})

    def test_the_discarded_entries_are_reported_with_a_count(self):
        with self.logs() as captured:
            self.load('{"d1": "5", "d2": true, "d3": 9}')

        reported = " | ".join(captured.output)
        self.assertIn("discarded 2 unusable entries", reported)
        self.assertIn("drop_minutes_cache", reported)

    def test_one_discarded_entry_is_reported_in_the_singular(self):
        with self.logs() as captured:
            self.load('{"d1": "5", "d3": 9}')

        self.assertIn("discarded 1 unusable entry", " | ".join(captured.output))

    def test_a_healthy_cache_is_loaded_silently(self):
        # A WARNING on every boot is a WARNING nobody reads.
        self.path.write_text('{"d1": 7}', encoding="utf-8")
        with self.assertNoLogs(cache.logger, level=logging.WARNING):
            cache.load()

    def test_a_non_object_value_is_refused_by_type_rather_than_indexed(self):
        # _coerce is called with whatever the JSON layer returns, and stays total
        # for a caller that hands it something else: "ignoring" beats
        # AttributeError, which is the failure mode this whole guard replaces.
        for raw in ([], 42, "hi", None):
            with self.subTest(raw=raw):
                with self.logs() as captured:
                    self.assertEqual(cache._coerce(raw), {})
                self.assertIn("not an object", " | ".join(captured.output))

    def test_a_non_object_file_is_refused_before_it_reaches_the_cache(self):
        with self.json_logs() as captured:
            self.load("42")

        self.assertEqual(cache._cache, {})
        self.assertIn("not an object", " | ".join(captured.output))


class TestLoadingNeverRaises(CacheTestBase):
    """``load()`` runs before the client exists, outside every handler."""

    def test_an_unparseable_file_loads_empty_instead_of_raising(self):
        # It is also no longer silent: the pre-wave `except Exception: _cache = {}`
        # said nothing at all.
        with self.json_logs() as captured:
            self.load("{not json")

        self.assertEqual(cache._cache, {})
        self.assertEqual(cache.get("d1", 0), 0)
        self.assertIn("Corrupt JSON", " | ".join(captured.output))

    def test_a_missing_file_is_not_an_error(self):
        self.assertFalse(self.path.exists())
        with self.assertNoLogs(cache.logger, level=logging.WARNING):
            cache.load()
        self.assertEqual(cache._cache, {})

    def test_an_unreadable_file_does_not_kill_the_process(self):
        # json_load does not catch OSError, and this runs at import time, so the
        # broad catch in load() is the only thing between a permissions problem
        # and a miner that will not start.
        self.path.write_text('{"d1": 7}', encoding="utf-8")
        with patch.object(
            cache, "json_load", side_effect=OSError("Permission denied")
        ), self.logs() as captured:
            cache.load()

        self.assertEqual(cache._cache, {})
        self.assertIn("starting empty", " | ".join(captured.output))


class TestACachedFigureAboveTheRequirementIsRefused(CacheTestBase):
    """``maximum=``: the quiet half, which stops a game being mined at all."""

    def setUp(self):
        super().setUp()
        self.load('{"d1": 7}')

    def test_a_cached_count_above_the_requirement_is_not_used(self):
        with self.logs():
            self.assertEqual(cache.get("d1", 0, maximum=5), 0)

    def test_the_api_figure_still_answers_when_the_cache_is_refused(self):
        # Refusing the cached value must not also throw away the real one.
        with self.logs():
            self.assertEqual(cache.get("d1", 3, maximum=5), 3)

    def test_a_cached_count_exactly_at_the_requirement_is_kept(self):
        # The boundary is the legitimate "this drop is finished" value.
        self.assertEqual(cache.get("d1", 0, maximum=7), 7)

    def test_a_cached_count_below_the_requirement_is_kept(self):
        self.assertEqual(cache.get("d1", 0, maximum=60), 7)

    def test_without_a_maximum_nothing_is_refused(self):
        # Callers that have no requirement to compare against keep the old
        # behaviour exactly.
        self.assertEqual(cache.get("d1", 0), 7)

    def test_the_refusal_names_the_drop_and_both_numbers(self):
        with self.logs() as captured:
            cache.get("d1", 0, maximum=5)

        reported = " | ".join(captured.output)
        self.assertIn("d1", reported)
        self.assertIn("7", reported)
        self.assertIn("5", reported)

    def test_a_believable_cache_is_read_silently(self):
        with self.assertNoLogs(cache.logger, level=logging.WARNING):
            cache.get("d1", 0, maximum=60)


def _campaign() -> MagicMock:
    campaign = MagicMock()
    campaign._twitch = MagicMock()
    return campaign


def _drop_data(drop_id: str, required_minutes: int, distribution_type: str) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": drop_id,
        "name": "Test Drop",
        "benefitEdges": [
            {
                "benefit": {
                    "id": "b1",
                    "name": "Test Benefit",
                    "distributionType": distribution_type,
                    "imageAssetURL": "url",
                }
            }
        ],
        "startAt": (now - timedelta(days=1)).isoformat(),
        "endAt": (now + timedelta(days=1)).isoformat(),
        "preconditionDrops": [],
        "requiredMinutesWatched": required_minutes,
    }


class TestTheCallSitePassesTheRequirement(CacheTestBase):
    """The refusal is worthless if ``TimedDrop`` does not ask for it.

    ``src/models/drop.py`` is the only caller that knows the requirement, so the
    guard lives half in the cache and half at the call site. Reverting either
    half restores the whole defect, which is why both are asserted.
    """

    def build(self, *, required_minutes: int, distribution_type: str) -> TimedDrop:
        return TimedDrop(
            _campaign(), _drop_data("d1", required_minutes, distribution_type), {}
        )

    def test_a_poisoned_cache_cannot_mark_an_unfinished_badge_drop_as_claimed(self):
        # The outage: 9999 cached minutes on a 60-minute badge drop makes the
        # auto-granted inference fire, the selector skip the drop, and the game
        # leave wanted_games with nothing logged.
        self.load('{"d1": 9999}')

        with self.logs():
            drop = self.build(required_minutes=60, distribution_type="BADGE")

        self.assertFalse(drop.is_claimed)
        self.assertEqual(drop.real_current_minutes, 0)

    def test_the_same_poison_on_an_item_drop_is_also_refused(self):
        # DIRECT_ENTITLEMENT drops do not auto-grant, so this one never flipped
        # is_claimed - but a fake 9999/60 progress bar is still a lie.
        self.load('{"d1": 9999}')

        with self.logs():
            drop = self.build(required_minutes=60, distribution_type="DIRECT_ENTITLEMENT")

        self.assertEqual(drop.real_current_minutes, 0)

    def test_a_genuinely_finished_badge_drop_still_reads_as_claimed(self):
        # The guard must not cost the behaviour it is protecting: a cached count
        # AT the requirement is exactly what a completed drop looks like.
        self.load('{"d1": 60}')

        drop = self.build(required_minutes=60, distribution_type="BADGE")

        self.assertTrue(drop.is_claimed)
        self.assertEqual(drop.real_current_minutes, 60)

    def test_ordinary_cached_progress_still_survives_the_api_reporting_zero(self):
        # The whole point of the cache: Twitch forgetting the minutes must not.
        self.load('{"d1": 42}')

        drop = self.build(required_minutes=60, distribution_type="BADGE")

        self.assertEqual(drop.real_current_minutes, 42)
        self.assertFalse(drop.is_claimed)


class TestFlushingIsAtomicAndNeverFatal(CacheTestBase):
    """This is the most frequently written file in the application.

    Once per earned minute per drop, which made a plain ``write_text`` - which
    truncates the target before the new bytes land - the likeliest of every file
    here to be caught half-written by a container stop.
    """

    def test_a_flush_round_trips_and_leaves_no_temp_file_behind(self):
        cache._cache = {"d1": 4}
        cache._dirty = True
        cache._flush()

        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"d1": 4})
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [self.path.name])

    def test_a_failed_write_leaves_the_previous_file_intact(self):
        cache._cache = {"d1": 4}
        cache._dirty = True
        cache._flush()

        cache._cache = {"d1": 99}
        cache._dirty = True
        with patch.object(cache, "json_save", side_effect=OSError("disk full")):
            cache._flush()  # must not raise

        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"d1": 4})


if __name__ == "__main__":
    unittest.main()
