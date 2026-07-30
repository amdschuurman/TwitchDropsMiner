"""The shutdown save may not change how the process exits.

Saving the settings is the LAST thing a clean shutdown does, and since the write
became atomic it is also the only thing left that can still fail: ``os.replace``
needs write permission on the data DIRECTORY and room for a second copy of the
file, so a read-only or full ``data/`` turns a normal SIGTERM into an uncaught
exception, a non-zero exit code, and - under ``restart: unless-stopped`` - a
container that keeps coming back. A disk problem then presents as a crashing
miner, which is about the least useful symptom it could have.

``src/__main__.py`` cannot be imported: everything in it lives under
``if __name__ == "__main__":``, and importing it would start a Twitch client. So
the shutdown tail is lifted out of the real file with ``ast`` and executed
against stand-ins for ``logger`` and ``settings``. That is a behavioural test of
the shipped statements - not a regex over them - and it fails if the guard is
removed, if it catches too much, or if the exit status is touched.
"""

import ast
import sys
import unittest
from pathlib import Path
from types import CodeType
from unittest.mock import MagicMock


_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENTRY_POINT = _REPO_ROOT / "src" / "__main__.py"


def _is_settings_save(node: ast.AST) -> bool:
    """True for the statement ``settings.save()``."""
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "save"
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "settings"
    )


def main_body() -> list[ast.stmt]:
    tree = ast.parse(_ENTRY_POINT.read_text(encoding="utf-8"), filename=str(_ENTRY_POINT))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main":
            return node.body
    raise AssertionError(f"{_ENTRY_POINT} no longer defines `async def main()`")


def shutdown_tail() -> CodeType:
    """Compile everything from the guarded save to the end of ``main()``.

    Anchored on the ``try`` that holds ``settings.save()`` and run to the end of
    the function, so the tail always includes whatever exit the file actually
    performs - a test that quietly stopped covering ``sys.exit`` would be worse
    than no test. The statements only reference ``logger``, ``settings``,
    ``sys`` and ``exit_status``, which is what makes executing them in isolation
    honest rather than a re-implementation.
    """
    body = main_body()
    guarded = [
        index
        for index, node in enumerate(body)
        if isinstance(node, ast.Try) and any(_is_settings_save(stmt) for stmt in node.body)
    ]
    assert len(guarded) == 1, (
        "expected exactly one try/except around settings.save() at the end of main(); "
        f"found {len(guarded)}"
    )
    tail = body[guarded[0]:]
    assert any(
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "exit"
        for node in tail
    ), "the shutdown tail no longer ends in a sys.exit() call"
    module = ast.Module(body=tail, type_ignores=[])
    return compile(ast.fix_missing_locations(module), str(_ENTRY_POINT), "exec")


class TestShutdownSaveCannotChangeTheExitStatus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tail = shutdown_tail()

    def exec_shutdown(self, logger: MagicMock, settings: MagicMock, exit_status: int):
        """Execute the real shutdown tail against the given stand-ins."""
        namespace = {
            "logger": logger,
            "settings": settings,
            "sys": sys,
            "exit_status": exit_status,
        }
        # exec, because the shipped statements ARE the subject: a re-implementation
        # here would pass however src/__main__.py is rewritten.
        exec(self.tail, namespace)

    def run_shutdown(self, save_effect=None, exit_status: int = 0):
        """The normal case: the tail always ends by exiting the process."""
        logger, settings = MagicMock(), MagicMock()
        settings.save.side_effect = save_effect
        with self.assertRaises(SystemExit) as raised:
            self.exec_shutdown(logger, settings, exit_status)
        return raised.exception.code, logger, settings

    def logged(self, logger: MagicMock) -> str:
        return " ".join(
            str(call.args[0]) for method in ("info", "error", "exception", "warning")
            for call in getattr(logger, method).call_args_list if call.args
        )

    def test_a_failing_save_does_not_change_the_exit_status(self):
        for exit_status in (0, 1):
            with self.subTest(exit_status=exit_status):
                full_disk = OSError(28, "No space left on device")

                code, _logger, settings = self.run_shutdown(full_disk, exit_status)

                self.assertEqual(code, exit_status)
                settings.save.assert_called_once_with()

    def test_a_failing_save_is_reported_with_its_traceback(self):
        _code, logger, _settings = self.run_shutdown(OSError(30, "Read-only file system"))

        # logger.exception, not logger.error: the operator needs to know WHICH
        # disk problem this was, and the tail is the last place it can be said.
        logger.exception.assert_called_once()
        self.assertIn("Could not save the application state", logger.exception.call_args.args[0])
        # A save that did not happen must not be reported as one that did.
        self.assertNotIn("Application state saved", self.logged(logger))

    def test_a_successful_save_still_reports_itself_and_exits(self):
        code, logger, settings = self.run_shutdown()

        self.assertEqual(code, 0)
        settings.save.assert_called_once_with()
        logger.exception.assert_not_called()
        self.assertIn("Application state saved", self.logged(logger))

    def test_an_interrupt_during_the_save_still_propagates(self):
        # ``except Exception``, deliberately not ``BaseException``: a Ctrl-C or a
        # cancellation arriving inside the save is the operator asking to stop
        # now, and swallowing it here would answer that with a normal exit.
        settings = MagicMock()
        settings.save.side_effect = KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.exec_shutdown(MagicMock(), settings, 0)

    def test_the_entry_point_has_exactly_one_shutdown_save(self):
        # The guard is only worth anything while it is the only save on the way
        # out; a second, unguarded one would restore the crash it prevents.
        tree = ast.parse(_ENTRY_POINT.read_text(encoding="utf-8"), filename=str(_ENTRY_POINT))
        saves = [node for node in ast.walk(tree) if _is_settings_save(node)]

        self.assertEqual(len(saves), 1)


if __name__ == "__main__":
    unittest.main()
