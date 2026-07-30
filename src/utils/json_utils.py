"""JSON serialization and deserialization utilities."""

from __future__ import annotations

import contextlib
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
# The mode json_save guarantees on the live file (mkstemp creates 0o600 whatever
# the umask, and os.replace carries that onto the target) and quarantine_corrupt
# restores on the copy it preserves. These files can hold a proxy URL with
# credentials in it and the discord_webhook_* URLs, so other users on the host
# must not be able to read them.
_PRIVATE_FILE_MODE = 0o600


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


def _remove_missing(obj: JsonType) -> JsonType:
    """
    Remove _MISSING sentinel values from a dictionary recursively.

    This modifies obj in place, but returns it for convenience.
    Used during deserialization to clean up unrecognized types.
    """
    for key, value in obj.copy().items():
        if value is _MISSING:
            del obj[key]
        elif isinstance(value, dict):
            _remove_missing(value)
            if not value:
                # the dict is empty now, so remove it's key entirely
                del obj[key]
    return obj


def _deserialize(obj: JsonType) -> Any:
    """
    Custom JSON decoder hook for special types.

    Reconstructs objects from serialized format using SERIALIZE_ENV.
    Returns _MISSING sentinel for unrecognized types (to be cleaned up later).
    """
    if "__type" in obj:
        obj_type = obj["__type"]
        if obj_type in SERIALIZE_ENV:
            return SERIALIZE_ENV[obj_type](obj["data"])
        else:
            return _MISSING
    return obj


def merge_json(obj: JsonType, template: Mapping[Any, Any], *, label: str = "") -> None:
    """
    Merge a JSON object with a template, ensuring all expected keys exist.

    NOTE: This modifies object in place.

    - Removes keys not present in template
    - Overwrites values with wrong type from template, reporting it at ERROR
    - Recursively merges nested dictionaries
    - Adds missing keys from template

    The type overwrite is the destructive one, so it is the one that is logged:
    a hand-edited ``"games_to_watch": "War Thunder"`` (a string where a list
    belongs) used to become ``[]`` right here, with nothing on the console and
    nothing in the log - and an empty watch list means the miner mines nothing.
    The message names the key and both types but deliberately NOT the value: a
    wrong-typed ``proxy`` or ``discord_webhook_*`` would put credentials into
    logs/TDM.log, and the key is already enough to find the offending line.

    Args:
        obj: The loaded JSON object, modified in place
        template: The expected shape and default values
        label: Prefix for reported keys, so a nested mismatch is identifiable.
            :func:`json_load` seeds it with the file it read
            (``data/settings.json:``) and the recursion appends the key it
            descends into (``inventory_filters.``).
    """
    for k, v in list(obj.items()):
        if k not in template:
            # unknown key: overwrite from template
            del obj[k]
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
    # ensure the object is not missing any keys
    for k in template:
        if k not in obj:
            obj[k] = template[k]


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
                combined: JsonType = _remove_missing(json.load(file, object_hook=_deserialize))
        except ValueError as e:
            # ValueError, not JSONDecodeError: a file corrupted at the byte level
            # (a bad block, a partially-written page) raises UnicodeDecodeError
            # instead, which used to escape this handler and reach the caller as
            # a crash - for settings.json that meant exit code 4 and, under
            # `restart: unless-stopped`, a container restart loop. Both are the
            # same event, "these bytes are not the JSON we wrote", so both are
            # handled the same way here.
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
    """
    cutoff = time.time() - _STALE_TEMP_AGE
    with contextlib.suppress(OSError):
        for temp in path.parent.glob(f".{path.name}.*{_TEMP_SUFFIX}"):
            with contextlib.suppress(OSError):
                if temp.stat().st_mtime < cutoff:
                    temp.unlink()


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
