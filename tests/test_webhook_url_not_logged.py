"""A Discord webhook URL must never reach a log, a buffer or a socket.

The URL is a credential: whoever holds it can post into the channel. It used
to be printed verbatim by the settings manager on every write ("Setting
changed: discord_webhook_drops = https://discord.com/api/webhooks/<id>/<token>"),
which put it in the web console buffer, the ``console_output`` websocket event,
stdout, ``logs/TDM.log`` and the host's docker json log at the same time.

Three layers are pinned here: the key-name mask in the settings manager, the
redaction of any console line before it is buffered, and the URL pattern in the
logging filter for lines that come from anywhere else.
"""

import logging
import unittest

from src.utils.log_redaction import SecretRedactingFilter, redact_secrets
from src.web.managers.settings import _is_secret_key, _loggable_value


# Not a live credential: a syntactically valid webhook URL with a fake token.
FAKE_WEBHOOK = "https://discord.com/api/webhooks/1234567890123456789/AbCdEf-gh_ijKLmnOpQrStUvWxYz0123456789AbCdEfGhIjKlMnOpQrSt"
FAKE_TOKEN = FAKE_WEBHOOK.rsplit("/", 1)[1]


class TestRedactSecrets(unittest.TestCase):
    def test_webhook_url_token_is_masked(self):
        out = redact_secrets(f"posting to {FAKE_WEBHOOK} now")
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertIn("webhooks/1234567890123456789/***", out)

    def test_discordapp_and_versioned_forms_are_masked(self):
        for url in (
            f"https://discordapp.com/api/webhooks/1234567890123456789/{FAKE_TOKEN}",
            f"https://discord.com/api/v10/webhooks/1234567890123456789/{FAKE_TOKEN}",
            f"https://ptb.discord.com/api/webhooks/1234567890123456789/{FAKE_TOKEN}",
        ):
            self.assertNotIn(FAKE_TOKEN, redact_secrets(url), url)

    def test_key_value_form_is_masked(self):
        out = redact_secrets(f'"discord_webhook_points": "{FAKE_WEBHOOK}"')
        self.assertNotIn(FAKE_TOKEN, out)

    def test_already_safe_placeholders_survive(self):
        # The settings manager masks by key name first, so the redactor sees a
        # line that is already safe. Re-masking "(empty)" to "***" would throw
        # away the only operational fact the line carries.
        for line in (
            "Setting changed: discord_webhook_mentions = (empty)",
            "Setting changed: discord_webhook_mentions = ***",
        ):
            self.assertEqual(redact_secrets(line), line)

    def test_non_secret_text_is_untouched(self):
        line = "Setting changed: priority_only = True"
        self.assertEqual(redact_secrets(line), line)

    def test_logging_filter_scrubs_the_record(self):
        record = logging.LogRecord(
            "TwitchDrops", logging.INFO, __file__, 1, "sending to %s", (FAKE_WEBHOOK,), None
        )
        SecretRedactingFilter().filter(record)
        self.assertNotIn(FAKE_TOKEN, record.getMessage())


class TestSecretKeyMasking(unittest.TestCase):
    def test_webhook_keys_are_secret(self):
        for key in (
            "discord_webhook_drops",
            "discord_webhook_points",
            "discord_webhook_mentions",
            "proxy",
        ):
            self.assertTrue(_is_secret_key(key), key)

    def test_ordinary_keys_are_not_secret(self):
        for key in ("language", "priority_only", "discord_ping_mention", "games_to_watch"):
            self.assertFalse(_is_secret_key(key), key)

    def test_secret_value_never_appears_in_the_log_line(self):
        shown = _loggable_value("discord_webhook_drops", FAKE_WEBHOOK)
        self.assertEqual(shown, "***")
        self.assertNotIn(FAKE_TOKEN, f"Setting changed: discord_webhook_drops = {shown}")

    def test_cleared_secret_reads_as_empty_not_masked(self):
        self.assertEqual(_loggable_value("discord_webhook_drops", ""), "(empty)")

    def test_plain_value_passes_through(self):
        self.assertEqual(_loggable_value("language", "English"), "English")


class TestConsoleBufferRedaction(unittest.TestCase):
    def test_console_print_redacts_before_buffering(self):
        import asyncio

        from src.web.managers.console import ConsoleOutputManager

        class _Broadcaster:
            async def emit(self, *_args, **_kwargs):
                return None

        async def _run():
            console = ConsoleOutputManager(_Broadcaster())  # type: ignore[arg-type]
            console.print(f"Setting changed: discord_webhook_drops = {FAKE_WEBHOOK}")
            history = console.get_history()
            self.assertEqual(len(history), 1)
            self.assertNotIn(FAKE_TOKEN, history[0])
            self.assertIn("***", history[0])

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
