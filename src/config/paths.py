"""Path-related configuration and environment detection."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from src.exceptions import MinerException


logger = logging.getLogger("TwitchDrops")


def _merge_vars(base_vars: dict[str, Any], vars: dict[str, Any]) -> None:
    """
    Merge variables recursively.

    NOTE: This modifies base_vars in place.
    """
    for k, v in vars.items():
        if k not in base_vars:
            base_vars[k] = v
        elif isinstance(v, dict):
            if isinstance(base_vars[k], dict):
                _merge_vars(base_vars[k], v)
            elif base_vars[k] is Ellipsis:
                # unspecified base, use the passed in var
                base_vars[k] = v
            else:
                raise RuntimeError(f"Var is a dict, base is not: '{k}'")
        elif isinstance(base_vars[k], dict):
            raise RuntimeError(f"Base is a dict, var is not: '{k}'")
        else:
            # simple overwrite
            base_vars[k] = v
    # ensure none of the vars are ellipsis (unset value)
    for k, v in base_vars.items():
        if v is Ellipsis:
            raise RuntimeError(f"Unspecified variable: '{k}'")


# Base Paths - environment-specific resolution
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_data_dir_env = os.environ.get("TDM_DATA_DIR")
DATA_DIR = Path(_data_dir_env) if _data_dir_env else PROJECT_ROOT / "data"

# Ensure data directory exists
if not DATA_DIR.exists():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

# Translations path
# NOTE: These don't have to be available to the end-user, so the path points to the internal dir
LANG_PATH = PROJECT_ROOT / "lang"


# ---------------------------------------------------------------------------
# Persistent storage paths - account-aware
# ---------------------------------------------------------------------------

# The file that names the active account. The WebUI truncate-writes it
# (``write_text(json.dumps(...))``) on every password change, push-config toggle
# and account add/switch/rename, so a kill mid-write leaves it half-written with
# no user action involved at all.
WEB_CONFIG_NAME = "web_config.json"
ACCOUNTS_DIR_NAME = "accounts"
ACTIVE_ACCOUNT_KEY = "active_account"

# Returned in place of a label when the pointer file exists and cannot be
# understood, so that "the file could not be read" is never confused with "the
# file names no account". No value decoded from JSON can ever BE this object.
# Same idiom as Settings._stored_value in src/config/settings.py.
_UNREADABLE = object()


class AccountDataDirUnresolved(MinerException):
    """
    web_config.json is unreadable and which account is active cannot be deduced.

    Raised only from :meth:`AccountPaths.data_dir`, and only in the single case
    where every available answer is wrong: the pointer file is corrupt AND two or
    more account directories exist, so the active one is genuinely ambiguous.
    """


class AccountPaths:
    """
    Resolves the active account's data directory, refusing to guess it wrong.

    Every per-account file - ``settings.json`` (the games-to-watch list) and
    ``cookies.jar`` (the login session) above all - lives under
    ``<data>/accounts/<label>/``, and the only thing naming ``<label>`` is
    ``web_config.json``. That makes this class the single point where a bad
    pointer file turns into a wrong path, so the rule it enforces is:

        **A pointer that cannot be read is not the same as a pointer that names
        nothing, and must never be treated as one.**

    Returning the data ROOT for an unreadable pointer is what the previous
    ``except Exception: pass`` did, and it is the worst available outcome: the
    miner loads settings from defaults (empty watch list) and finds no cookie jar
    (logged out), both at once, with nothing in the log - while the real
    settings.json and cookies.jar sit intact one directory away. Nor does it
    heal: the next WebUI save writes a fresh config with no active account and
    the per-account directories are orphaned for good.

    Instantiable rather than module-level state so a caller (and a test) can
    resolve against any data root; :data:`ACCOUNT_PATHS` is the process-wide
    instance every module should use.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        # Failure signatures already reported, so a corrupt pointer produces one
        # ERROR rather than one per HTTP request - the WebUI resolves an account
        # path on nearly every stats call.
        #
        # Two sets rather than one "last reported" string, because ONE resolution
        # emits TWO lines (what is wrong with the file, then what was done about
        # it) and a single slot made them evict each other: every call re-reported
        # both, which is the log flood this guard exists to prevent.
        #
        # Config signatures clear on a clean read, so a recurrence after a repair
        # is reported again. Mkdir signatures do not: they are scoped to a
        # directory rather than to a moment, a clean read happens on every call in
        # that state, and clearing them would restore the same flood.
        self._config_reported: set[str] = set()
        self._mkdir_reported: set[str] = set()

    @property
    def data_root(self) -> Path:
        """The data directory this instance resolves against."""
        return self._data_dir

    @property
    def config_file(self) -> Path:
        """The pointer file naming the active account."""
        return self._data_dir / WEB_CONFIG_NAME

    @property
    def accounts_root(self) -> Path:
        """The directory holding one subdirectory per account."""
        return self._data_dir / ACCOUNTS_DIR_NAME

    def _existing_accounts(self) -> list[str]:
        """Account labels that actually have a directory on disk, sorted."""
        try:
            return sorted(d.name for d in self.accounts_root.iterdir() if d.is_dir())
        except OSError:
            # No accounts root, or it cannot be listed. Either way there is no
            # evidence of per-account storage to recover a pointer from.
            return []

    def _report(self, seen: set[str], signature: str, message: str) -> None:
        """Log ``message`` at ERROR unless this exact failure was already logged."""
        if signature not in seen:
            seen.add(signature)
            logger.error(message)

    def _read_active_account(self) -> str | None | object:
        """
        Return the stored account label, ``None`` when none is named, or
        :data:`_UNREADABLE` when the pointer file exists and cannot be understood.

        An ABSENT file is the normal single-account case and stays silent - it is
        also how a fresh install starts, so it must never be reported.

        Failure taxonomy, deliberately the same one :func:`~src.utils.json_load`
        applies to every other stored file:

        - ``OSError`` - unreadable for a filesystem reason (permissions, a
          directory sitting in the file's place, an I/O error). ``exists()`` said
          yes, so a pointer IS there and we simply cannot see it.
        - ``UnicodeDecodeError`` - corruption at the byte level (a bad block, a
          partially written page). A ``ValueError``, and easy to miss precisely
          because it is not a ``JSONDecodeError``.
        - ``json.JSONDecodeError`` - the truncated write this resolver exists for.
        - a top-level value that is not an object (``[]``, ``42``, ``null``) -
          structurally not what we wrote, so corrupt in the same sense.

        NOT routed through ``json_load`` despite being exactly its job, and this
        is a structural constraint rather than a preference: ``src.utils`` imports
        ``src.config`` (``src/utils/json_utils.py`` needs ``JsonType``), so a
        ``src.config.paths`` that imports ``src.utils`` closes a cycle. It is not
        theoretical - ``src/models/channel.py`` imports ``src.utils.json_utils``
        before anything touches ``src.config``, and the import fails outright with
        ``cannot import name 'json_load' from partially initialized module``.
        Resolving the account directory happens at import time (COOKIES_PATH and
        SETTINGS_PATH below are module constants that ``src/config/__init__.py``
        re-exports eagerly), so there is no lazy-import escape either. The read
        below is therefore kept deliberately narrow - this file holds one string
        key, so none of ``json_load``'s ``__type`` decoding, template merging or
        quarantining applies - and only its FAILURE taxonomy is mirrored, which is
        the part that matters. See the module note on collapsing the two.

        Quarantining is deliberately NOT done here even though settings.json gets
        it, for three reasons specific to this file:

        - Quarantining renames the pointer away, which converts "the pointer is
          broken" into "there is no pointer" - and the very next WebUI save then
          writes a fresh config with no active account, cementing exactly the loss
          this class exists to refuse. Left in place under its real name, the
          bytes stay visible to the operator and to the next boot.
        - This is a path RESOLVER, run at process start and on the WebUI's hot
          paths. Mutating the data directory as a side effect of asking where a
          file lives is a surprise, and five other modules read this same file - a
          rename underneath them is a race nobody asked for.
        - The file also carries the WebUI password hash and the push-notification
          config. Moving it aside logs the operator out of the very interface they
          would use to repair the account pointer.
        """
        config_file = self.config_file
        if not config_file.exists():
            self._config_reported.clear()
            return None
        try:
            parsed = json.loads(config_file.read_text(encoding="utf8"))
        except Exception as exc:
            # Broad on purpose, and safe only because of what the except body
            # does: the bug this replaces was ALSO a broad catch, but it answered
            # with the data root - a wrong path, silently. Answering with
            # _UNREADABLE cannot hide anything, because every caller treats it as
            # a refusal rather than a value. Anything this catches that is not a
            # corrupt file is still a real reason not to trust the pointer.
            self._report(
                self._config_reported,
                f"unreadable:{type(exc).__name__}",
                f"Could not read {config_file}: {exc}. It names which account's "
                "settings and cookies are used, so the active account is unknown. "
                "The file has NOT been moved or rewritten - repair or delete it.",
            )
            return _UNREADABLE
        if not isinstance(parsed, dict):
            self._report(
                self._config_reported,
                "not-an-object",
                f"Corrupt {config_file}: the top-level value is "
                f"{type(parsed).__name__}, not an object, so the active account is "
                "unknown. The file has NOT been moved or rewritten - repair or "
                "delete it.",
            )
            return _UNREADABLE
        self._config_reported.clear()
        account = parsed.get(ACTIVE_ACCOUNT_KEY)
        if isinstance(account, str) and account:
            if not self._is_plain_component(account):
                # The file parses but names something that is not a directory
                # NAME. src/web/app.py's _safe_account_dir already refuses to
                # write such a label, so this only arrives by hand-editing or by
                # corruption that happened to stay valid JSON - and this resolver
                # is now the one every module shares, so it cannot be the thing
                # that turns "../../etc" into a mkdir outside the data directory.
                # Untrusted is not "no account": it goes down the same refusal
                # ladder as an unparseable file.
                self._report(
                    self._config_reported,
                    f"bad-label:{account}",
                    f"Corrupt {config_file}: {ACTIVE_ACCOUNT_KEY} is "
                    f"{account!r}, which is not an account directory name, so the "
                    "active account is unknown. The file has NOT been moved or "
                    "rewritten - repair or delete it.",
                )
                return _UNREADABLE
            return account
        # Present-but-empty, absent, or a non-string: the file parsed and names no
        # usable account, which is the legacy/single-account layout at the root.
        return None

    @staticmethod
    def _is_plain_component(label: str) -> bool:
        """
        True when ``label`` is one plain directory name, safe to join onto a path.

        Checked on the LABEL rather than by resolving the joined path and testing
        containment, deliberately: ``Path.resolve`` follows symlinks, so the
        containment form would also reject an operator who symlinked
        ``data/accounts/<label>`` onto another disk - a legitimate layout this
        code has no business breaking. Comparing against ``Path(label).name``
        rejects every separator and both dot entries while accepting any ordinary
        name, including ones containing dots.
        """
        return (
            label not in (".", "..")
            and "\0" not in label
            and "\\" not in label
            and label == Path(label).name
        )

    def active_account(self) -> str:
        """
        The active account's label, or ``""`` when there is none or it is unknown.

        Never raises, unlike :meth:`data_dir`, and the asymmetry is deliberate: a
        label has a safe unknown value because it is only ever displayed (the
        Discord webhook footer, the accounts list in the WebUI), whereas a PATH
        has none - the data root is not "unknown", it is different storage.
        """
        account = self._read_active_account()
        return account if isinstance(account, str) else ""

    def data_dir(self) -> Path:
        """
        The directory holding the active account's settings and cookies.

        The outcomes, and why each is the only defensible one:

        - **The pointer names an account** - that account's directory, created if
          missing. If it cannot be created the path is still returned: the answer
          is not wrong, the filesystem is, and a caller that fails on a missing
          directory reports the real problem, whereas quietly switching to the
          data root would hide it behind an empty watch list.
        - **The pointer is absent, or parses and names nothing** - the data root.
          The normal single-account (and legacy, pre-accounts) layout. Silent.

        The remaining three cover the pointer being UNUSABLE, which means either
        unreadable (see :meth:`_read_active_account`) or naming a label that is
        not a directory name (see :meth:`_is_plain_component`). Both mean the same
        thing here - the active account is unknown - so both take the same ladder:

        - **Unusable, and exactly ONE account directory exists** - that
          directory. Not a guess: the WebUI creates an account directory only
          while pointing the config at it, and refuses to delete the active
          account, so a single surviving directory IS the active account. The
          pointer is recovered from the filesystem, nothing is lost, and the
          single-account case - the overwhelmingly common one - heals itself
          instead of turning a broken pointer into a logged-out miner.
        - **Unusable, and NO account directory exists** - the data root,
          reported but not refused. There is no per-account storage to strand:
          settings.json and cookies.jar are at the root, which is where the miner
          will read them. The rule enforced here is "never repoint away from real
          data", and here the root IS the real data.
        - **Unusable, and TWO OR MORE account directories exist** -
          :class:`AccountDataDirUnresolved`. Every available answer is wrong. The
          root is certainly wrong (the operator demonstrably uses per-account
          storage) and stranding it loses two or more intact settings.json +
          cookies.jar pairs at once, which the next save then cements. Picking one
          of the accounts is worse still: it would mine as the wrong user and
          overwrite that user's settings.

          Refusing means raising, and raising at import time means the process
          does not start. That is accepted here and ONLY here, because this is the
          one branch where coming up is itself the damage. The refusal writes
          nothing, moves nothing and quarantines nothing, so every account's data
          survives untouched; the message names the file, the labels found and the
          fix; and recovery is one edit of one small file. A miner that is loudly
          down beats a miner quietly mining nothing as nobody, and a restart loop
          that preserves the data beats a clean boot that overwrites it.
        """
        account = self._read_active_account()
        if account is _UNREADABLE:
            candidates = self._existing_accounts()
            if len(candidates) == 1:
                recovered = self.accounts_root / candidates[0]
                self._report(
                    self._config_reported,
                    f"recovered:{candidates[0]}",
                    f"Corrupt {self.config_file}: using {recovered}, the only "
                    "account directory that exists, so mining continues with the "
                    "right settings and cookies. Repair or delete the config file "
                    "to make the active account explicit again.",
                )
                return self._ensure(recovered)
            if not candidates:
                self._report(
                    self._config_reported,
                    "root",
                    f"Corrupt {self.config_file}: no account directories exist, so "
                    f"settings and cookies are read from {self._data_dir}, exactly "
                    "as before. Repair or delete the config file.",
                )
                return self._data_dir
            raise AccountDataDirUnresolved(
                f"{self.config_file} is unreadable and {len(candidates)} account "
                f"directories exist ({', '.join(candidates)}), so which one is "
                "active cannot be determined. Refusing to start rather than mine "
                "as the wrong account or overwrite the wrong account's settings. "
                "Nothing has been modified: repair or delete that file, then start "
                "again."
            )
        if isinstance(account, str) and account:
            return self._ensure(self.accounts_root / account)
        return self._data_dir

    def file(self, name: str) -> Path:
        """A file inside the active account's data directory."""
        return self.data_dir() / name

    def _ensure(self, account_dir: Path) -> Path:
        """Create ``account_dir`` if it is missing, and return it either way."""
        try:
            account_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._report(
                self._mkdir_reported,
                f"mkdir:{account_dir}",
                f"Could not create the account directory {account_dir}: {exc}. "
                "Using it anyway - reading settings and cookies from the data root "
                "instead would silently mine nothing under a different identity.",
            )
        return account_dir


# The process-wide resolver. Every module needing a per-account path must go
# through this instead of re-deriving it: the resolver was duplicated across
# src/web/app.py, src/services/message_handlers.py, src/services/drop_history.py,
# src/services/prediction_service.py and src/core/client.py, each carrying its own
# `except Exception: pass` that silently repointed at the data root.
ACCOUNT_PATHS = AccountPaths(DATA_DIR)


def account_data_dir() -> Path:
    """The active account's data directory (see :meth:`AccountPaths.data_dir`)."""
    return ACCOUNT_PATHS.data_dir()


def account_file(name: str) -> Path:
    """A file inside the active account's data directory."""
    return ACCOUNT_PATHS.file(name)


def active_account() -> str:
    """The active account's label, or ``""`` when there is none or it is unknown."""
    return ACCOUNT_PATHS.active_account()


_ACCOUNT_DATA_DIR = account_data_dir()
COOKIES_PATH = _ACCOUNT_DATA_DIR / "cookies.jar"
SETTINGS_PATH = _ACCOUNT_DATA_DIR / "settings.json"
