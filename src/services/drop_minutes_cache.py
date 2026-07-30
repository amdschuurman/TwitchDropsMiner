"""Persists drop watch minutes locally to survive Twitch API inconsistencies."""

from __future__ import annotations

import logging
from typing import Any

from src.config.paths import DATA_DIR
from src.utils import json_load, json_save

logger = logging.getLogger(__name__)

_CACHE_FILE = DATA_DIR / "drop_minutes_cache.json"
_cache: dict[str, int] = {}
_dirty = False


def _coerce(raw: Any) -> dict[str, int]:
    """Keep only the ``{drop id: minutes}`` pairs this module could have written.

    A shape check rather than a parse check, because a parse check is not what
    was missing: ``json.loads`` accepted this file's contents happily and handed
    back a value the rest of the module then used as a dict. A stored ``[]``
    made :func:`get` raise ``AttributeError: 'list' object has no attribute
    'get'`` and a stored ``{"<id>": "5"}`` made it raise ``TypeError: '>' not
    supported between instances of 'str' and 'int'`` - both from inside
    ``TimedDrop.__init__``, i.e. inside the inventory parse, and neither is in
    the tuple ``Twitch.run()`` recovers from. They reach ``__main__``'s
    catch-all, exit the process with status 1, and under
    ``restart: unless-stopped`` the container comes back, reads the same file and
    dies the same way: mining stops permanently over a derived cache the miner
    does not need at all.

    So an entry that could not have come from :func:`update` is dropped rather
    than trusted. Nothing is lost by dropping one - the value is re-derived from
    the API minutes on the next inventory fetch, which is what this cache is a
    backstop for, not a source of.
    """
    if not isinstance(raw, dict):
        # json_load already reports and defaults a non-object file; this keeps
        # _coerce total for every other caller and shape.
        logger.warning(
            "drop_minutes_cache: ignoring a cache file that is not an object "
            f"(found {type(raw).__name__!r}) - watched minutes will come from the API"
        )
        return {}
    clean: dict[str, int] = {}
    dropped = 0
    for drop_id, minutes in raw.items():
        # bool is a subclass of int, and True would silently mean "1 minute".
        if (
            isinstance(drop_id, str)
            and isinstance(minutes, int)
            and not isinstance(minutes, bool)
            and minutes >= 0
        ):
            clean[drop_id] = minutes
        else:
            dropped += 1
    if dropped:
        logger.warning(
            f"drop_minutes_cache: discarded {dropped} unusable entr"
            f"{'y' if dropped == 1 else 'ies'} from {_CACHE_FILE} - those drops fall back "
            "to the minutes Twitch reports"
        )
    return clean


def load():
    global _cache
    raw: Any
    try:
        # merge=False: the cache is an open map of drop IDs, so there is no
        # template to merge against. json_load still turns an unparseable or
        # non-object file into defaults plus a WARNING, where this used to be
        # silent.
        raw = json_load(_CACHE_FILE, {}, merge=False)
    except Exception as exc:
        # Deliberately broad, and deliberately not fatal. This runs before the
        # client exists, outside any handler, so anything escaping here (an
        # unreadable file, a permission problem - json_load does not catch
        # OSError) would kill the process at startup over a cache whose entire
        # contents are re-derivable.
        logger.warning(f"drop_minutes_cache load failed, starting empty: {exc!r}")
        raw = {}
    _cache = _coerce(raw)


def get(drop_id: str, api_minutes: int, *, maximum: int | None = None) -> int:
    """The best known watched-minutes count for this drop.

    ``maximum`` is the drop's ``required_minutes``, and a cached value above it
    is refused rather than clamped. :func:`update` cannot produce one - the
    model clamps ``real_current_minutes`` to ``required_minutes`` before it gets
    here - so such a value is corruption, and it is the quiet half of this
    file's failure mode: for a badge or emote drop, minutes at or over the
    requirement make ``TimedDrop.__init__`` infer ``is_claimed``, the stream
    selector then skips the drop, and the game drops out of ``wanted_games``
    with nothing logged. Clamping to the maximum would keep that inference
    intact; refusing the value removes it, and costs nothing, because the API
    figure below is the number the cache exists to protect, not replace.
    """
    cached = _cache.get(drop_id, 0)
    if maximum is not None and cached > maximum:
        logger.warning(
            f"drop_minutes_cache: ignoring {cached} cached minutes for drop {drop_id} - "
            f"more than the {maximum} the drop requires, so the cache is wrong about it"
        )
        cached = 0
    return max(api_minutes, cached)


def update(drop_id: str, minutes: int):
    global _dirty
    if minutes > 0 and minutes > _cache.get(drop_id, 0):
        _cache[drop_id] = minutes
        _dirty = True
        _flush()


def _flush():
    global _dirty
    if not _dirty:
        return
    try:
        # Via json_save, so this write is atomic. It used to be a plain
        # write_text, which truncates the target before the new bytes land, and
        # this is the most frequently written file in the application - once per
        # earned minute per drop - so it was the likeliest of all of them to be
        # caught half-written by a container stop.
        json_save(_CACHE_FILE, _cache)
        _dirty = False
    except Exception as e:
        logger.debug(f"drop_minutes_cache flush failed: {e}")
