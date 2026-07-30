"""``web_config.json`` names which account's settings and cookies are used.

It is truncate-written (``write_text(json.dumps(...))``) by the WebUI on every
password change, push-config toggle and account add/switch/rename, so a kill
mid-write leaves it half-written with no user action involved at all. Five
modules used to resolve the active account from it, each with its own
``except Exception: pass`` that answered with the DATA ROOT - which is the worst
available outcome and the reason this class exists:

    settings.json is missing there, so the watch list loads as EMPTY, and
    cookies.jar is missing there, so the miner is LOGGED OUT - both at once,
    with nothing in the log, while the real files sit intact one directory away.
    Nor does it heal: the next WebUI save writes a fresh config naming no
    account and the per-account directories are orphaned for good.

:class:`~src.config.paths.AccountPaths` replaces that with a rule - *a pointer
that cannot be read is not the same as a pointer that names nothing* - and a
resolution ladder that is asserted branch by branch below. Every test builds its
own ``AccountPaths(tmp_path)``, so nothing here touches the process-wide
resolver or the developer's own data directory.
"""

from __future__ import annotations

import json
import logging
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.config.paths import AccountDataDirUnresolved, AccountPaths


# Every way the pointer file can be there and yet unusable. The taxonomy matters
# because each of these arrives from a different real event.
CORRUPTIONS = {
    "truncated mid-write": '{"active_account": "are',
    "unparseable": "{not json",
    "an empty file": "",
    "a top-level list": "[]",
    "a top-level number": "42",
    "a top-level string": '"hi"',
    "a top-level null": "null",
}


class AccountPathsTestBase(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.paths = AccountPaths(self.root)

    def write_config(self, text: str) -> None:
        self.paths.config_file.write_text(text, encoding="utf-8")

    def point_at(self, label: str) -> None:
        self.write_config(json.dumps({"active_account": label}))

    def make_account(self, label: str, *, settings: dict | None = None) -> Path:
        """A populated account directory, the way a real install has one."""
        directory = self.paths.accounts_root / label
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "settings.json").write_text(
            json.dumps(settings or {"games_to_watch": ["Rust"]}), encoding="utf-8"
        )
        (directory / "cookies.jar").write_text("cookie-bytes", encoding="utf-8")
        return directory

    def quiet(self):
        return self.assertNoLogs("TwitchDrops", level=logging.DEBUG)

    def loud(self, level=logging.ERROR):
        return self.assertLogs("TwitchDrops", level=level)


class TestTheOrdinaryLayoutsResolveSilently(AccountPathsTestBase):
    """A fresh install has no pointer file at all. That is not an error."""

    def test_an_absent_config_resolves_to_the_data_root(self):
        with self.quiet():
            self.assertEqual(self.paths.data_dir(), self.root)

    def test_an_absent_config_names_no_account(self):
        with self.quiet():
            self.assertEqual(self.paths.active_account(), "")

    def test_a_config_that_names_nothing_resolves_to_the_data_root(self):
        for label, text in {
            "no key at all": "{}",
            "an empty label": '{"active_account": ""}',
            "a null label": '{"active_account": null}',
            "a non-string label": '{"active_account": 7}',
            "other keys only": '{"password_hash": "x"}',
        }.items():
            with self.subTest(config=label):
                self.write_config(text)
                with self.quiet():
                    self.assertEqual(self.paths.data_dir(), self.root)
                    self.assertEqual(self.paths.active_account(), "")

    def test_a_named_account_resolves_under_the_accounts_root(self):
        self.point_at("arend")
        with self.quiet():
            resolved = self.paths.data_dir()

        self.assertEqual(resolved, self.root / "accounts" / "arend")
        self.assertTrue(resolved.is_dir())
        self.assertEqual(self.paths.active_account(), "arend")

    def test_a_named_account_directory_is_created_when_missing(self):
        self.point_at("arend")
        self.assertFalse((self.root / "accounts" / "arend").exists())

        self.assertTrue(self.paths.data_dir().is_dir())

    def test_file_resolves_inside_the_active_account(self):
        self.point_at("arend")
        with self.quiet():
            self.assertEqual(
                self.paths.file("settings.json"),
                self.root / "accounts" / "arend" / "settings.json",
            )

    def test_a_label_containing_a_dot_is_still_a_plain_name(self):
        # The containment check is on the LABEL, not on a resolved path, so
        # ordinary names with dots in them must keep working.
        self.point_at("arend.2")
        with self.quiet():
            self.assertEqual(self.paths.data_dir(), self.root / "accounts" / "arend.2")


class TestAnUnusablePointerIsNeverReadAsNoAccount(AccountPathsTestBase):
    """The whole defect in one sentence: these used to answer "the data root"."""

    def test_every_corruption_refuses_rather_than_resolving_to_the_root(self):
        self.make_account("arend")
        for label, text in CORRUPTIONS.items():
            with self.subTest(config=label):
                paths = AccountPaths(self.root)
                paths.config_file.write_text(text, encoding="utf-8")
                with self.assertLogs("TwitchDrops", level=logging.ERROR):
                    self.assertEqual(paths.data_dir(), self.root / "accounts" / "arend")

    def test_byte_level_corruption_is_handled_like_bad_json(self):
        # A bad block or a partially written page raises UnicodeDecodeError,
        # which is a ValueError but NOT a JSONDecodeError - the easiest one to
        # miss when writing the handler by hand.
        self.make_account("arend")
        self.paths.config_file.write_bytes(b'{"active_account": "\xff"}')

        with self.loud():
            self.assertEqual(self.paths.data_dir(), self.root / "accounts" / "arend")

    @unittest.skipIf(os.geteuid() == 0, "root can read a mode-000 file")
    def test_an_unreadable_file_refuses_rather_than_resolving_to_the_root(self):
        self.make_account("arend")
        self.point_at("arend")
        self.paths.config_file.chmod(0o000)
        self.addCleanup(self.paths.config_file.chmod, 0o600)

        with self.loud():
            self.assertEqual(self.paths.data_dir(), self.root / "accounts" / "arend")

    def test_active_account_answers_empty_and_never_raises(self):
        # The label has a safe unknown value because it is only ever displayed.
        # A PATH has none, which is why data_dir() is allowed to raise and this
        # is not.
        self.make_account("a")
        self.make_account("b")
        for label, text in CORRUPTIONS.items():
            with self.subTest(config=label):
                paths = AccountPaths(self.root)
                paths.config_file.write_text(text, encoding="utf-8")
                with self.assertLogs("TwitchDrops", level=logging.ERROR):
                    self.assertEqual(paths.active_account(), "")

    def test_the_report_names_the_file_and_says_it_was_left_alone(self):
        self.make_account("arend")
        self.write_config("{not json")

        with self.loud() as captured:
            self.paths.data_dir()

        reported = " | ".join(captured.output)
        self.assertIn("web_config.json", reported)
        self.assertIn("NOT been moved or rewritten", reported)


class TestTheRecoveryLadder(AccountPathsTestBase):
    """Three branches, each the only defensible answer for its evidence."""

    def test_one_account_directory_is_recovered_with_its_data_intact(self):
        # The contrast that makes this whole class worth having: the old
        # resolver answered with the data root, where settings.json does not
        # exist (empty watch list) and cookies.jar does not exist (logged out).
        account = self.make_account("arend")
        self.write_config('{"active_account": "are')

        with self.loud():
            resolved = self.paths.data_dir()

        self.assertEqual(resolved, account)
        self.assertEqual(
            json.loads((resolved / "settings.json").read_text(encoding="utf-8")),
            {"games_to_watch": ["Rust"]},
        )
        self.assertEqual((resolved / "cookies.jar").read_text(encoding="utf-8"), "cookie-bytes")

    def test_the_recovery_explains_itself(self):
        self.make_account("arend")
        self.write_config("{not json")

        with self.loud() as captured:
            self.paths.data_dir()

        reported = " | ".join(captured.output)
        self.assertIn("the only account directory that exists", reported)
        self.assertIn("arend", reported)

    def test_no_account_directories_means_the_root_really_is_the_data(self):
        # Nothing to strand: settings.json and cookies.jar are at the root,
        # which is where the miner will read them.
        self.write_config("{not json")

        with self.loud() as captured:
            self.assertEqual(self.paths.data_dir(), self.root)

        self.assertIn("no account directories exist", " | ".join(captured.output))

    def test_an_empty_accounts_directory_counts_as_none(self):
        self.paths.accounts_root.mkdir(parents=True)
        self.write_config("{not json")

        with self.loud():
            self.assertEqual(self.paths.data_dir(), self.root)

    def test_a_file_in_the_accounts_directory_is_not_an_account(self):
        self.paths.accounts_root.mkdir(parents=True)
        (self.paths.accounts_root / "stray.txt").write_text("x", encoding="utf-8")
        self.write_config("{not json")

        with self.loud():
            self.assertEqual(self.paths.data_dir(), self.root)

    def test_two_account_directories_are_refused_rather_than_guessed(self):
        # Every available answer is wrong. The root strands two intact
        # settings+cookies pairs, and picking one mines as the wrong user and
        # overwrites their settings.
        self.make_account("alice")
        self.make_account("bob")
        self.write_config("{not json")

        with self.loud(), self.assertRaises(AccountDataDirUnresolved) as raised:
            self.paths.data_dir()

        message = str(raised.exception)
        self.assertIn("alice", message)
        self.assertIn("bob", message)
        self.assertIn("web_config.json", message)

    def test_the_refusal_changes_nothing_on_disk(self):
        # The refusal is only defensible because it is inert: every account
        # survives untouched and recovery is one edit of one small file.
        alice = self.make_account("alice", settings={"games_to_watch": ["Rust"]})
        bob = self.make_account("bob", settings={"games_to_watch": ["Dota 2"]})
        self.write_config("{not json")
        before = self.paths.config_file.read_bytes()

        with self.loud(), self.assertRaises(AccountDataDirUnresolved):
            self.paths.data_dir()

        self.assertEqual(self.paths.config_file.read_bytes(), before)
        self.assertEqual(list(self.root.glob("**/*.corrupt*")), [])
        self.assertEqual(
            json.loads((alice / "settings.json").read_text(encoding="utf-8")),
            {"games_to_watch": ["Rust"]},
        )
        self.assertEqual(
            json.loads((bob / "settings.json").read_text(encoding="utf-8")),
            {"games_to_watch": ["Dota 2"]},
        )
        self.assertEqual((alice / "cookies.jar").read_text(encoding="utf-8"), "cookie-bytes")

    def test_nothing_is_ever_quarantined(self):
        # Renaming the pointer away converts "the pointer is broken" into
        # "there is no pointer", and the next WebUI save then cements the loss.
        # It also carries the WebUI password hash: moving it aside logs the
        # operator out of the interface they would use to repair it.
        self.make_account("arend")
        self.write_config("{not json")
        before = self.paths.config_file.read_bytes()

        with self.loud():
            self.paths.data_dir()

        self.assertEqual(self.paths.config_file.read_bytes(), before)
        self.assertEqual(list(self.root.glob("**/*.corrupt*")), [])


class TestALabelIsNeverJoinedBlindly(AccountPathsTestBase):
    """``_safe_account_dir`` refuses to WRITE such a label; corruption can hold one.

    This resolver is now the one every module shares, so it cannot be the thing
    that turns ``../../pwned`` into a mkdir outside the data directory.
    """

    TRAVERSALS = ("../../pwned", "..", ".", "a/b", "/abs", "a\\b", "x\0y")

    def test_a_traversing_label_creates_nothing_outside_the_data_root(self):
        # Run against a data root nested inside its own temp dir, so anything
        # that escapes shows up as an extra entry beside it.
        for label in self.TRAVERSALS:
            for accounts in (0, 1, 2):
                with self.subTest(label=label, accounts=accounts), TemporaryDirectory() as name:
                    enclosing = Path(name)
                    root = enclosing / "data"
                    root.mkdir()
                    paths = AccountPaths(root)
                    for index in range(accounts):
                        (root / "accounts" / f"acct{index}").mkdir(parents=True)
                    paths.config_file.write_text(
                        json.dumps({"active_account": label}), encoding="utf-8"
                    )
                    resolved: Path | None
                    try:
                        resolved = paths.data_dir()
                    except AccountDataDirUnresolved:
                        resolved = None

                    self.assertEqual([p.name for p in enclosing.iterdir()], ["data"])
                    if resolved is not None:
                        self.assertTrue(
                            resolved == root or root in resolved.parents,
                            f"{label!r} resolved to {resolved}, outside {root}",
                        )

    def test_a_traversing_label_is_refused_rather_than_joined(self):
        for label in self.TRAVERSALS:
            with self.subTest(label=label):
                paths = AccountPaths(self.root)
                paths.config_file.write_text(
                    json.dumps({"active_account": label}), encoding="utf-8"
                )
                with self.loud():
                    # No account directories exist here, so the ladder answers
                    # with the root - never with a joined traversal.
                    self.assertEqual(paths.data_dir(), self.root)

    def test_a_traversing_label_takes_the_same_refusal_ladder(self):
        self.make_account("arend")
        self.point_at("../../pwned")

        with self.loud() as captured:
            self.assertEqual(self.paths.data_dir(), self.root / "accounts" / "arend")

        self.assertIn("not an account directory name", " | ".join(captured.output))

    def test_a_traversing_label_is_not_reported_as_the_active_account(self):
        self.point_at("../../pwned")
        with self.loud():
            self.assertEqual(self.paths.active_account(), "")


class TestTheReportingDoesNotFlood(AccountPathsTestBase):
    """The WebUI resolves an account path on nearly every request.

    One ERROR per HTTP call would make the log useless within a minute, and the
    first cut had exactly that bug: a single "last reported" slot, evicted twice
    per resolution by the two lines one resolution emits.
    """

    def test_a_hundred_calls_on_a_corrupt_config_report_twice(self):
        self.make_account("arend")
        self.write_config("{not json")

        with self.loud(logging.DEBUG) as captured:
            for _ in range(50):
                self.paths.data_dir()
                self.paths.active_account()
                self.paths.file("settings.json")

        self.assertEqual(len(captured.records), 2, captured.output)

    def test_both_lines_of_one_resolution_survive_each_other(self):
        # What is wrong with the file, and what was done about it. Deduping into
        # a single slot lost one of them and re-reported both forever.
        self.make_account("arend")
        self.write_config("{not json")

        with self.loud() as captured:
            self.paths.data_dir()

        reported = " | ".join(captured.output)
        self.assertEqual(len(captured.records), 2, captured.output)
        self.assertIn("Could not read", reported)
        self.assertIn("the only account directory that exists", reported)

    def test_a_repair_goes_quiet_and_a_recurrence_is_reported_again(self):
        self.make_account("arend")
        self.write_config("{not json")
        with self.loud():
            self.paths.data_dir()

        self.point_at("arend")
        with self.quiet():
            self.assertEqual(self.paths.data_dir(), self.root / "accounts" / "arend")

        self.write_config("{not json")
        with self.loud() as captured:
            self.paths.data_dir()

        self.assertEqual(len(captured.records), 2, captured.output)

    def test_a_different_corruption_is_reported_on_its_own_terms(self):
        # The signature is the failure, not the file: a file that goes from
        # unparseable to structurally wrong is a new fact.
        self.make_account("arend")
        self.write_config("{not json")
        with self.loud():
            self.paths.data_dir()

        self.write_config("[]")
        with self.loud() as captured:
            self.paths.data_dir()

        self.assertIn("not an object", " | ".join(captured.output))


class TestAnUncreatableAccountDirectoryStillAnswers(AccountPathsTestBase):
    """The answer is not wrong, the filesystem is."""

    @unittest.skipIf(os.geteuid() == 0, "root can write into a mode-500 directory")
    def test_the_account_path_is_returned_even_when_it_cannot_be_created(self):
        self.paths.accounts_root.mkdir(parents=True)
        self.paths.accounts_root.chmod(0o500)
        self.addCleanup(self.paths.accounts_root.chmod, 0o700)
        self.point_at("arend")

        with self.loud() as captured:
            resolved = self.paths.data_dir()

        # Never the data root: switching there would silently mine nothing under
        # a different identity, which is the failure this class exists to refuse.
        self.assertEqual(resolved, self.paths.accounts_root / "arend")
        self.assertNotEqual(resolved, self.root)
        self.assertIn("Could not create the account directory", " | ".join(captured.output))

    @unittest.skipIf(os.geteuid() == 0, "root can write into a mode-500 directory")
    def test_the_mkdir_failure_is_not_repeated_on_every_call(self):
        self.paths.accounts_root.mkdir(parents=True)
        self.paths.accounts_root.chmod(0o500)
        self.addCleanup(self.paths.accounts_root.chmod, 0o700)
        self.point_at("arend")

        with self.loud(logging.DEBUG) as captured:
            for _ in range(20):
                self.paths.data_dir()

        self.assertEqual(len(captured.records), 1, captured.output)


if __name__ == "__main__":
    unittest.main()
