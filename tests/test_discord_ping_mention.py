"""``discord_ping_mention`` turns every webhook body into a real ping.

The setting exists because a webhook that posts only ``embeds`` cannot notify
anyone: Discord pings on ``content``, never on embed text. These tests pin both
halves of the contract - the body is unchanged when no mention is configured,
and it carries ``content`` plus a scoped ``allowed_mentions`` when one is.
"""

import unittest

from src.config.settings import Settings, default_settings
from src.utils import build_webhook_payload


class TestBuildWebhookPayload(unittest.TestCase):
    def test_no_mention_is_the_old_embeds_only_body(self):
        embed = {"title": "🎁 Drop Claimed!"}
        self.assertEqual(build_webhook_payload([embed]), {"embeds": [embed]})
        self.assertEqual(build_webhook_payload([embed], ""), {"embeds": [embed]})

    def test_whitespace_only_mention_does_not_add_content(self):
        payload = build_webhook_payload([{"title": "x"}], "   ")
        self.assertNotIn("content", payload)
        self.assertNotIn("allowed_mentions", payload)

    def test_non_string_mention_is_ignored(self):
        # A hand-edited settings.json can put anything here; it must not raise
        # inside a notification path whose failure mode is a silent drop.
        payload = build_webhook_payload([{"title": "x"}], None)  # type: ignore[arg-type]
        self.assertEqual(payload, {"embeds": [{"title": "x"}]})

    def test_mention_becomes_content_with_scoped_allowed_mentions(self):
        embed = {"title": "🎁 Drop Claimed!"}
        payload = build_webhook_payload([embed], "<@251794990462795776>")
        self.assertEqual(payload["content"], "<@251794990462795776>")
        self.assertEqual(payload["embeds"], [embed])
        # users/roles only: an @everyone inside a quoted chat message must never
        # ping the whole server just because the body now has a content field.
        self.assertEqual(payload["allowed_mentions"], {"parse": ["users", "roles"]})

    def test_embeds_list_is_copied_not_aliased(self):
        embeds = [{"title": "x"}]
        payload = build_webhook_payload(embeds, "<@1>")
        payload["embeds"].append({"title": "y"})
        self.assertEqual(len(embeds), 1)


class TestSettingWiring(unittest.TestCase):
    def test_default_is_empty_string(self):
        self.assertEqual(default_settings["discord_ping_mention"], "")

    def test_settings_dataclass_carries_the_field(self):
        self.assertIn("discord_ping_mention", Settings.__annotations__)


if __name__ == "__main__":
    unittest.main()
