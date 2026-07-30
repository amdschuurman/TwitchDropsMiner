"""JSON serialization and deserialization utilities."""

from __future__ import annotations

import contextlib
import glob
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar, cast

from yarl import URL

from src.config import JsonType

logger = logging.getLogger("TwitchDrops")


_JSON_T = TypeVar("_JSON_T", bound=Mapping[Any, Any])
_MISSING = object()

# Naming of the files json_save/json_load create beside their target.
_TEMP_SUFFIX = ".tmp"
_CORRUPT_SUFFIX = ".corrupt"
# How many numbered ``.corrupt`` slots to probe before falling back to a
# timestamped name, so the search can never turn into an unbounded stat loop.
_MAX_QUARANTINE_SLOTS = 100
# A save takes milliseconds, so a temp file older than this belongs to a process
# that died before its rename - never to a save currently in flight.
_STALE_TEMP_AGE = 3600.0
# How long a quarantined file is kept before the reaper may take it. Far longer
# than a temp file's hour, because a quarantine is EVIDENCE: it can be the only
# surviving copy of the operator's settings, and someone who was away for a
# fortnight must still find it waiting. Thirty days is past the point where the
# values in it are worth recovering by hand.
_STALE_QUARANTINE_AGE = 30 * 24 * 3600.0
# ...and this many of the OLDEST quarantines survive the reaper no matter how old
# they get. :func:`quarantine_corrupt` already refuses to clobber an earlier
# quarantine because the earliest copy is the one most likely to hold real
# pre-corruption data; the reaper follows the same priority, so the operator is
# never left with no evidence at all.
_KEPT_QUARANTINES = 1
# The mode json_save guarantees on the live file (mkstemp creates 0o600 whatever
# the umask, and os.replace carries that onto the target) and quarantine_corrupt
# restores on the copy it preserves. These files can hold a proxy URL with
# credentials in it and the discord_webhook_* URLs, so other users on the host
# must not be able to read them.
_PRIVATE_FILE_MODE = 0o600


class _UnusableJSONError(ValueError):
    """The bytes parsed as JSON, but they are not the structure we wrote.

    A sibling of ``json.JSONDecodeError`` rather than a separate concept, and a
    ``ValueError`` for the same reason that one is: "this file is not what it is
    supposed to be" has exactly one right answer in :func:`json_load`, and both
    ways of being wrong deserve it. Two things raise it, both of them facts about
    the FILE:

    - the top-level value is not an object (``[]``, ``42``, ``"hello"``,
      ``null``, or a ``__type`` wrapper that decoded to a set);
    - a ``__type`` wrapper's payload could not be handed to its
      :data:`SERIALIZE_ENV` constructor (``{"__type": "set", "data": 5}``).

    Deliberately its own class, and deliberately raised only at those two points:
    catching the underlying ``AttributeError``/``TypeError``/``KeyError`` where
    they surfaced would also have swallowed the same exception types coming out
    of a genuine bug elsewhere in the load, which is how a data problem turns
    into an invisible logic problem.
    """


# Serialization environment - maps type names to deserialization functions
SERIALIZE_ENV: dict[str, Callable[[Any], object]] = {
    "set": set,
    "URL": URL,
    "datetime": lambda d: datetime.fromtimestamp(d, timezone.utc),
}


def json_minify(data: JsonType | list[JsonType]) -> str:
    """Return minified JSON string (no whitespace) for payload usage."""
    return json.dumps(data, separators=(",", ":"))


def isonow() -> str:
    """Return the current UTC time in Twitch's expected ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _serialize(obj: Any) -> Any:
    """
    Custom JSON encoder for special types.

    Converts datetime, set, Enum, and URL objects to serializable format.
    Stores both the type name and the converted data for proper deserialization.
    """
    # convert data
    d: int | str | float | list[Any] | JsonType
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            # assume naive objects are UTC
            obj = obj.replace(tzinfo=timezone.utc)
        d = obj.timestamp()
    elif isinstance(obj, set):
        d = list(obj)
    elif isinstance(obj, Enum):
        # NOTE: IntEnum cannot be used, as it will get serialized as a plain integer,
        # then loaded back as an integer as well.
        d = obj.value
    elif isinstance(obj, URL):
        d = str(obj)
    else:
        raise TypeError(obj)
    # store with type
    return {
        "__type": type(obj).__name__,
        "data": d,
    }


def _reject_missing_in_list(values: list[Any], label: str) -> None:
    """
    Refuse a list holding an unrecognized ``__type`` wrapper, and clean the rest.

    The sibling of :func:`_remove_missing` for the one container it cannot treat
    the same way. Dropping a key whose value did not deserialize is safe because
    :func:`merge_json` puts the template's default back, so the shape survives and
    nothing quietly shrinks. A LIST has no per-element template: dropping an
    element would shorten ``games_to_watch`` by one game with nothing on the
    console and nothing in the log - the exact silent loss this module now exists
    to refuse.

    Leaving the sentinel alone was worse still, and is what actually happened:
    ``_remove_missing`` only ever walked dicts, so ``{"games_to_watch": ["Rust",
    {"__type": "Decimal", "data": "1"}]}`` loaded with a bare sentinel object
    sitting in the watch list, and the next :func:`json_save` hit it in
    :func:`_serialize` and raised ``TypeError`` - on that save and on every save
    after it. Settings could never be persisted again, and the miner was left
    trying to match a sentinel against channel game names.

    So the file goes down the corrupt route instead: it is not the structure this
    version writes (only a version that registered a type in
    :data:`SERIALIZE_ENV` which this one does not could produce it), the bytes are
    worth preserving, and the operator gets told. Recognized wrappers inside lists
    are untouched - a list of datetimes round-trips exactly as before.
    """
    for index, value in enumerate(values):
        if value is _MISSING:
            raise _UnusableJSONError(
                f"the list at {label}[{index}] holds a __type wrapper this version "
                "does not recognize"
            )
        elif isinstance(value, dict):
            _remove_missing(value, label=f"{label}[{index}].")
        elif isinstance(value, list):
            _reject_missing_in_list(value, f"{label}[{index}]")


def _remove_missing(obj: JsonType, *, label: str = "") -> JsonType:
    """
    Remove _MISSING sentinel values from a dictionary recursively.

    This modifies obj in place, but returns it for convenience.
    Used during deserialization to clean up unrecognized types.

    Guarantees that no sentinel survives anywhere below ``obj`` - inside a nested
    list, or inside a dict inside one - because :func:`json_save` cannot
    serialize one and the failure surfaces at the next save rather than here.
    Lists are handled by :func:`_reject_missing_in_list`, which explains why they
    refuse rather than prune. ``label`` names the position for that message.
    """
    for key, value in obj.copy().items():
        if value is _MISSING:
            del obj[key]
        elif isinstance(value, dict):
            _remove_missing(value, label=f"{label}{key}.")
            if not value:
                # the dict is empty now, so remove it's key entirely
                del obj[key]
        elif isinstance(value, list):
            _reject_missing_in_list(value, f"{label}{key}")
    return obj


def _deserialize(obj: JsonType) -> Any:
    """
    Custom JSON decoder hook for special types.

    Reconstructs objects from serialized format using SERIALIZE_ENV.
    Returns _MISSING sentinel for unrecognized types (to be cleaned up later).

    A wrapper whose type IS recognized but whose payload the constructor rejects
    is a corrupt file, not an unknown type, so it raises :class:`_UnusableJSONError`
    instead of degrading to ``_MISSING``: ``{"__type": "set", "data": 5}`` used to
    raise ``TypeError`` and ``{"__type": "set"}`` a ``KeyError`` straight out of
    :func:`json_load`, past its handler, up to ``sys.exit(4)`` - a container
    restart loop, with the file left in place to do it again on the next boot.
    Every exception the constructor can raise is about the stored payload (the
    call itself is fixed), which is why the catch here is broad; the raise is
    narrow, so nothing else in the load is affected.

    The message names the wrapper's TYPE and never its data: a ``proxy`` or
    ``discord_webhook_*`` URL can be stored inside one, and this text reaches
    logs/TDM.log.
    """
    if "__type" in obj:
        obj_type = obj["__type"]
        if obj_type in SERIALIZE_ENV:
            try:
                return SERIALIZE_ENV[obj_type](obj["data"])
            except Exception as exc:
                raise _UnusableJSONError(
                    f"a stored {obj_type!r} value could not be decoded "
                    f"({type(exc).__name__})"
                ) from exc
        else:
            return _MISSING
    return obj


def merge_json(obj: JsonType, template: Mapping[Any, Any], *, label: str = "") -> None:
    """
    Merge a JSON object with a template, ensuring all expected keys exist.

    NOTE: This modifies object in place.

    - An EMPTY template is an open map and is left completely alone (see below)
    - Removes keys not present in a non-empty template, reporting it at ERROR
    - Overwrites values with wrong type from template, reporting it at ERROR
    - Recursively merges nested dictionaries
    - Adds missing keys from template

    A template says two different things depending on whether it has any keys,
    and treating both the same destroyed real data:

    - A NON-EMPTY template is a SCHEMA. It enumerates every key that may exist,
      so a stored key it does not list is a setting this version removed, and
      pruning it is right.
    - An EMPTY template is an OPEN MAP. It enumerates nothing, so it cannot
      possibly be an authority on what is unknown - every stored key is the
      user's own DATA. ``channel_strategies`` is one (``{}`` in
      ``default_settings``, filled with one entry per channel the user chose a
      betting strategy for), and the old rule deleted every one of those keys on
      load, then let the next save persist ``{}``: per-channel strategies simply
      vanished, silently, exactly like the empty watch list did. So an empty
      template short-circuits - nothing to prune against, no types to check, no
      keys to add.

    Both destructive branches are logged at ERROR now. Deleting a key discards
    whatever the operator had put there just as surely as overwriting a
    wrong-typed one does, and the previous silence meant a downgrade could drop a
    setting with no trace at all. The removals are reported as ONE line per
    object rather than one per key, so a legacy file that is a few settings
    behind produces a line, not a wall.

    Both messages name keys and types but deliberately NOT values: a wrong-typed
    or newly-unknown ``proxy`` or ``discord_webhook_*`` would put credentials
    into logs/TDM.log, and the key is already enough to find the offending line.
    Keys are safe to print here precisely because open maps are never pruned -
    every key this function can delete comes from a schema, so it is the name of
    a setting, never something the user typed.

    Args:
        obj: The loaded JSON object, modified in place
        template: The expected shape and default values
        label: Prefix for reported keys, so a nested mismatch is identifiable.
            :func:`json_load` seeds it with the file it read
            (``data/settings.json:``) and the recursion appends the key it
            descends into (``inventory_filters.``).
    """
    if not template:
        # An open map: the stored keys are data, and there is nothing to merge
        # them against. Leaving early is the whole behaviour.
        return
    discarded: list[str] = []
    for k, v in list(obj.items()):
        if k not in template:
            # unknown key: this template lists every key there is, so the stored
            # one belongs to no setting and its value cannot be kept
            del obj[k]
            discarded.append(f"{label}{k}")
        elif type(v) is not type(template[k]):
            # types don't match: overwrite from template
            logger.error(
                f"Wrong type in stored JSON for {f'{label}{k}'!r}: expected "
                f"{type(template[k]).__name__}, found {type(v).__name__}. Replacing it "
                "with the default value, so whatever it held is discarded."
            )
            obj[k] = template[k]
        elif isinstance(v, dict):
            assert isinstance(template[k], dict)
            merge_json(v, template[k], label=f"{label}{k}.")
    if discarded:
        logger.error(
            f"Unknown keys in stored JSON, discarded: {', '.join(discarded)}. This "
            "version has no setting by those names, so whatever they held is gone - "
            "recover it from a backup if it mattered."
        )
    # ensure the object is not missing any keys
    for k in template:
        if k not in obj:
            obj[k] = template[k]


def _sweep_stale(path: Path, pattern: str, max_age: float, *, keep_oldest: int = 0) -> None:
    """
    Delete this target's own leftovers matching ``pattern``, oldest ones spared.

    The one reaper behind both :func:`_sweep_stale_temps` and
    :func:`_sweep_stale_quarantines`, because the rules they need are the same
    three:

    - Scoped to ONE target. ``pattern`` is built around ``path.name`` (escaped,
      so a target whose name contains ``*``, ``?`` or ``[`` cannot widen the
      glob into another target's files), and never matches the live file itself.
    - Age-based, off mtime, so a file another process may still be about to use
      is never taken. Both callers guarantee mtime means "when this file stopped
      being live" - ``mkstemp`` has just created the temp, and
      :func:`quarantine_corrupt` re-stamps the copy it preserves.
    - Every failure suppressed, at both levels: an unreadable directory ends the
      sweep, an unreadable or undeletable entry skips just that entry. Reaping
      garbage must never be the reason a save or a quarantine fails.

    ``keep_oldest`` spares that many of the oldest matches unconditionally, for
    the caller whose leftovers are evidence rather than litter.
    """
    cutoff = time.time() - max_age
    found: list[tuple[float, Path]] = []
    with contextlib.suppress(OSError):
        for candidate in path.parent.glob(pattern):
            with contextlib.suppress(OSError):
                found.append((candidate.stat().st_mtime, candidate))
    # Name breaks a tie, so which files are spared never depends on the order the
    # directory happens to enumerate in: a burst of corruption inside one clock
    # tick gives every quarantine the same mtime, and the unnumbered
    # ``<name>.corrupt`` - the FIRST one ever taken - sorts ahead of every
    # ``.corrupt.N`` under a plain string sort, which is exactly the one to keep.
    found.sort(key=lambda item: (item[0], item[1].name))
    for mtime, candidate in found[keep_oldest:]:
        if mtime < cutoff:
            with contextlib.suppress(OSError):
                candidate.unlink()


def _sweep_stale_quarantines(path: Path) -> None:
    """
    Delete this target's long-expired ``.corrupt`` files (see :func:`quarantine_corrupt`).

    Quarantining preserves the bytes and nothing ever collected them again: a
    filesystem that corrupts settings.json on every boot fills the data directory
    with ``settings.json.corrupt``, ``.corrupt.1`` ... ``.corrupt.99`` and then an
    unbounded run of timestamped names, and once the numbered slots are gone
    every later quarantine costs 100 failed ``stat`` calls as well.

    Deliberately far more timid than the temp sweep, because these files are the
    opposite of garbage - one of them may be the only surviving copy of the
    operator's settings:

    - nothing is touched until it is ``_STALE_QUARANTINE_AGE`` old (thirty days,
      not the temps' hour);
    - the oldest ``_KEPT_QUARANTINES`` survive whatever their age, so a directory
      that has been collecting these for a year still hands the operator the
      copy most likely to hold real pre-corruption data - the same "an earlier
      quarantine outranks a later one" rule :func:`quarantine_corrupt` follows
      when it refuses to clobber one.

    The pattern cannot match the live file (it requires the ``.corrupt`` suffix)
    nor a temp file (those are dot-prefixed), and cannot reach another target's
    quarantines (they carry that target's name).
    """
    _sweep_stale(
        path,
        f"{glob.escape(path.name)}{_CORRUPT_SUFFIX}*",
        _STALE_QUARANTINE_AGE,
        keep_oldest=_KEPT_QUARANTINES,
    )


def quarantine_corrupt(path: Path) -> Path | None:
    """
    Move an unparseable file aside so its bytes survive, and return its new path.

    Falling back to defaults is only safe if the bytes that could not be parsed
    are still recoverable: a settings.json truncated by a single byte still holds
    the operator's whole games_to_watch list, and replacing it with defaults
    (games_to_watch = [], i.e. mine nothing) is unrecoverable the moment the next
    save rewrites the file. So the corrupt file is renamed instead of left to be
    overwritten, and the caller logs where it went.

    Never clobbers an earlier quarantine: ``settings.json.corrupt`` is tried
    first, then ``settings.json.corrupt.1``, ``.2``, ... and after
    ``_MAX_QUARANTINE_SLOTS`` of those a timestamped name, which also bounds the
    number of stat calls. ``os.replace`` within one directory is an atomic rename
    with no copy, so the move cannot itself truncate anything.

    The preserved copy is tightened to ``_PRIVATE_FILE_MODE``: ``os.replace``
    carries the ORIGINAL mode across, so a settings.json that pre-dated
    json_save's 0o600 guarantee (or was created by hand at 0o644) stayed
    world-readable here for good - nothing ever rewrites a quarantined file -
    while holding the ``proxy`` URL, which may embed credentials, and the three
    ``discord_webhook_*`` URLs. Best-effort like the rest of this module: a mode
    that cannot be changed must not turn preserving the evidence into losing it.

    The preserved copy's mtime is re-stamped to NOW, which is what makes
    :func:`_sweep_stale_quarantines` safe: ``os.replace`` carries the original
    mtime across, so a settings.json last saved months before it was corrupted
    would land here already looking ancient and be reaped on the spot. The
    stamped time answers the only question the reaper asks - "when did this file
    stop being live?" - and the original mtime is not worth keeping at the price
    of deleting the evidence early.

    Returns ``None`` when the file could not be moved (a read-only data
    directory, for instance); the corrupt file is then left exactly where it is
    rather than deleted, and the caller still logs the failure.
    """
    base = f"{path.name}{_CORRUPT_SUFFIX}"
    names = [base]
    names.extend(f"{base}.{slot}" for slot in range(1, _MAX_QUARANTINE_SLOTS))
    names.append(f"{base}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}")
    for name in names:
        target = path.with_name(name)
        if target.exists():
            continue
        try:
            os.replace(path, target)
        except OSError:
            return None
        with contextlib.suppress(OSError):
            os.chmod(target, _PRIVATE_FILE_MODE)
        with contextlib.suppress(OSError):
            os.utime(target, None)
        # Only after this one has landed, so a reaper failure can never be what
        # loses the file it was called to make room for.
        _sweep_stale_quarantines(path)
        return target
    return None


def json_load(
    path: Path, defaults: _JSON_T, *, merge: bool = True, quarantine: bool = False
) -> _JSON_T:
    """
    Load JSON from a file with defaults and optional merging.

    On an unparseable file the generic behaviour is to fall back to ``defaults``:
    raising used to propagate up into whatever caller's own try/except swallowed
    it, which silently froze that file's cached value forever, since every
    later load hit the same exception before reaching the code that would have
    rewritten it. For a point cache or a chest snapshot that fallback is right -
    the data is derived, and the next successful save heals it.

    For a file whose contents cannot be derived again - settings.json above all -
    it is NOT right on its own, which is what ``quarantine`` is for: the bad file
    is moved aside by :func:`quarantine_corrupt` and the event is logged at ERROR
    instead of WARNING, so the values are recoverable by hand and the operator is
    told rather than left to discover that their configuration became defaults.
    Callers that pass ``quarantine=True`` are accepting a fresh-defaults start in
    exchange for the preserved evidence.

    "Unparseable" is meant structurally, not just syntactically. A file whose
    JSON is perfectly well-formed but is not the OBJECT this function returns -
    ``[]``, ``42``, ``"hello"``, ``null`` - used to reach ``_remove_missing`` and
    die there with ``'list' object has no attribute 'items'``, and a recognized
    ``__type`` wrapper holding a payload its constructor rejects died the same
    way with a ``TypeError``. Neither was caught anywhere: for settings.json that
    is ``sys.exit(4)`` on every boot and, under ``restart: unless-stopped``, a
    restart loop that quarantines nothing and tells the operator nothing. Such a
    file is corrupt in exactly the sense this function already knows how to
    handle, so it now takes the same route - defaults, quarantine when asked for,
    ERROR - and the container comes up.

    Args:
        path: Path to JSON file
        defaults: Default values to use if file doesn't exist or merge is enabled
        merge: If True, merge loaded data with defaults template
        quarantine: If True, an unparseable file is preserved under a
            ``.corrupt`` name and reported at ERROR level

    Returns:
        Loaded and optionally merged JSON data
    """
    defaults_dict: JsonType = dict(defaults)
    if path.exists():
        try:
            with open(path, encoding="utf8") as file:
                loaded = json.load(file, object_hook=_deserialize)
            if not isinstance(loaded, dict):
                # Checked here rather than left to blow up in _remove_missing,
                # so that "the file is not an object" is a fact about the file
                # and not an AttributeError indistinguishable from a bug.
                found = (
                    "a __type wrapper this version does not recognize"
                    if loaded is _MISSING
                    else f"of type {type(loaded).__name__!r}"
                )
                raise _UnusableJSONError(f"the top-level value is {found}, not an object")
            combined: JsonType = _remove_missing(loaded)
        except ValueError as e:
            # ValueError, not JSONDecodeError: a file corrupted at the byte level
            # (a bad block, a partially-written page) raises UnicodeDecodeError
            # instead, which used to escape this handler and reach the caller as
            # a crash - for settings.json that meant exit code 4 and, under
            # `restart: unless-stopped`, a container restart loop. Both are the
            # same event, "these bytes are not the JSON we wrote", so both are
            # handled the same way here - as is _UnusableJSONError, a ValueError
            # for precisely that reason.
            if quarantine:
                preserved = quarantine_corrupt(path)
                if preserved is not None:
                    logger.error(
                        f"Corrupt JSON in {path}: {e}. The unreadable file has been kept as "
                        f"{preserved} - recover any values you need from it. Starting from "
                        f"defaults for now, which for settings.json means an EMPTY "
                        f"games-to-watch list, so nothing will be mined until it is set again."
                    )
                else:
                    logger.error(
                        f"Corrupt JSON in {path}: {e}. It could not be moved aside either, so "
                        f"it is left in place - copy it somewhere safe before saving over it. "
                        f"Starting from defaults for now."
                    )
            else:
                logger.warning(f"Corrupt JSON in {path}, falling back to defaults: {e}")
            combined = defaults_dict
        else:
            if merge:
                # The file name goes into the label so a type mismatch names the
                # file it came from, not just the key.
                merge_json(combined, defaults_dict, label=f"{path}:")
    else:
        combined = defaults_dict
    return cast(_JSON_T, combined)


def _sweep_stale_temps(path: Path) -> None:
    """
    Delete this target's own abandoned temp files (see :func:`json_save`).

    A kill between the write and the ``os.replace`` leaves a
    ``.<name>.XXXXXX.tmp`` behind, and nothing ever collected those: a container
    that is SIGKILLed on every stop slowly fills its data directory with them.
    Only temps belonging to THIS target are considered, and only ones older than
    ``_STALE_TEMP_AGE``, so a save running concurrently in another process - its
    temp file is seconds old - is never robbed of the file it is about to rename.
    Every failure is ignored: reaping garbage must never turn a successful save
    into a failed one.

    No ``keep_oldest``: unlike a quarantine, an abandoned temp holds a payload
    that was superseded by the very save that replaced it, so there is nothing
    here worth sparing.
    """
    _sweep_stale(path, f".{glob.escape(path.name)}.*{_TEMP_SUFFIX}", _STALE_TEMP_AGE)


def json_save(path: Path, contents: Mapping[Any, Any], *, sort: bool = False) -> None:
    """
    Save data to a JSON file with custom serialization, atomically.

    Atomic because ``open(path, "w")`` truncates the target BEFORE the new bytes
    are written: a crash, a SIGKILL, or a full disk halfway through left a
    truncated file behind, and ``json_load`` then fell back to ``defaults`` -
    which for settings.json means ``games_to_watch: []``, i.e. the miner
    silently stops mining. So the payload is serialized into a temporary file in
    the SAME directory (same filesystem, so the rename cannot cross devices),
    flushed and ``fsync``ed, and only then ``os.replace``d onto the target. A
    reader therefore always sees either the complete previous file or the
    complete new one, never a half of either, and a failure anywhere before the
    replace leaves the original untouched and removes the temporary file.

    The file keeps restrictive permissions (0o600) so that other users on the
    host cannot read potentially sensitive content such as proxy URLs that embed
    credentials: ``mkstemp`` creates the temporary file 0o600 regardless of
    umask, and ``os.replace`` carries that mode onto the target - which also
    hardens a target that pre-existed with looser permissions, as the previous
    explicit ``chmod`` did.

    Generic on purpose: settings.json (src/config/settings.py) and the channel
    point caches (src/services/message_handlers.py, src/web/app.py) save through
    here, so the cost stays one extra create + rename per save (no directory
    fsync). Cookies are NOT one of them - ``aiohttp.CookieJar.save()`` owns that
    file and truncates it in place, so it keeps its own separate exposure.

    Two residuals of the ``os.replace`` approach, both accepted:

    - The rename needs write permission on the *directory*, not just on the
      target file. A data directory that is read-only to the process while
      settings.json is writable used to save fine and now raises - correctly, as
      such a save could never have been made durable anyway.
    - Replacing a symlinked target leaves a regular file where the symlink was
      (``os.replace`` renames onto the link itself, it does not follow it). No
      code here creates such a layout, and an operator who symlinks settings.json
      elsewhere gets a real file at that path after the first save.

    Args:
        path: Path to save JSON file
        contents: Data to serialize
        sort: If True, sort keys alphabetically
    """
    # A dotted prefix keeps a leftover temp file (kill -9 between write and
    # replace) out of the way of any glob that scans the data directory.
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=_TEMP_SUFFIX
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf8") as file:
            json.dump(contents, file, default=_serialize, sort_keys=sort, indent=4)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
        # Only after the save succeeded: a sweep must never be what makes one
        # fail, and there is nothing to collect until this one is safely landed.
        _sweep_stale_temps(path)
    except BaseException:
        # Serialization error, disk full, permission error - the original file
        # is still whole, so just take the temp file back out of the directory.
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise
