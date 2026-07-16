import unittest
from unittest.mock import MagicMock

from src.core.client import Twitch
from src.models.benefit import Benefit, BenefitType
from src.models.campaign import DropsCampaign
from src.models.drop import TimedDrop
from src.models.game import Game
from src.web.gui_manager import WebGUIManager


def _make_benefit(name, benefit_type, image_url):
    b = MagicMock(spec=Benefit)
    b.name = name
    b.type = benefit_type
    b.image_url = image_url
    # Bind the real wantedness logic so mining_benefits filtering is exercised.
    b.is_wanted = Benefit.is_wanted.__get__(b, Benefit)
    return b


def _make_drop(name, benefits, *, is_claimed=False, required_minutes=15, base_can_earn=True):
    d = MagicMock(spec=TimedDrop)
    d.name = name
    d.is_claimed = is_claimed
    d.required_minutes = required_minutes
    d._base_can_earn.return_value = base_can_earn
    d.benefits = list(benefits)
    return d


class TestWantedItems(unittest.TestCase):
    def setUp(self):
        # Mock Twitch Client
        self.twitch = MagicMock(spec=Twitch)
        self.twitch.settings = MagicMock()
        self.twitch.get_change_state_callable.return_value = lambda: None
        # Instance attributes created in Twitch.__init__ are invisible to spec=Twitch;
        # WebGUIManager wires SettingsManager callbacks to these services at init.
        self.twitch._scheduler_service = MagicMock()
        self.twitch._watch_service = MagicMock()

        self.gui = WebGUIManager(self.twitch)
        # Suppress broadcaster
        self.gui._broadcaster = MagicMock()

    def test_get_wanted_tree(self):
        # Setup Settings
        self.twitch.settings.games_to_watch = ["Game1", "Game2"]
        self.twitch.settings.preferred_games = []
        self.twitch.settings.drop_name_blacklist = []
        self.twitch.settings.mining_benefits = {"BADGE": True, "DIRECT_ENTITLEMENT": False}

        # Setup Inventory

        # Campaign 1: Game1, Drop with BADGE (Wanted)
        c1 = MagicMock(spec=DropsCampaign)
        c1.id = "c1_id"
        c1.name = "Campaign1"
        c1.campaign_url = "http://url1"
        c1.game = Game({"id": 1, "name": "Game1", "boxArtURL": "http://img1"})
        c1.can_earn_within.return_value = True
        c1.drops = [
            _make_drop("Drop1", [_make_benefit("Badge1", BenefitType.BADGE, "http://b1.png")])
        ]

        # Campaign 2: Game2, Drop with DIRECT_ENTITLEMENT (Unwanted)
        c2 = MagicMock(spec=DropsCampaign)
        c2.id = "c2_id"
        c2.name = "Campaign2"
        c2.campaign_url = "http://url2"
        c2.game = Game({"id": 2, "name": "Game2", "boxArtURL": "http://img2"})
        c2.can_earn_within.return_value = True
        c2.drops = [
            _make_drop(
                "Drop2",
                [_make_benefit("Item1", BenefitType.DIRECT_ENTITLEMENT, "http://b2.png")],
            )
        ]

        # Campaign 3: Game3 (Not in watch list), Drop with BADGE (Wanted but wrong game)
        c3 = MagicMock(spec=DropsCampaign)
        c3.id = "c3_id"
        c3.name = "Campaign3"
        c3.campaign_url = "http://url3"
        c3.game = Game({"id": 3, "name": "Game3", "boxArtURL": "http://img3"})
        c3.can_earn_within.return_value = True
        c3.drops = [
            _make_drop("Drop3", [_make_benefit("Badge2", BenefitType.BADGE, "http://b3.png")])
        ]

        # Campaign 4: Game1, Drop with BADGE, can't earn (Wanted)
        c4 = MagicMock(spec=DropsCampaign)
        c4.id = "c4_id"
        c4.name = "Campaign4"
        c4.campaign_url = "http://url4"
        c4.game = Game({"id": 1, "name": "Game1", "boxArtURL": "http://img1"})
        c4.can_earn_within.return_value = False
        c4.drops = [
            _make_drop("Drop4", [_make_benefit("Badge1", BenefitType.BADGE, "http://b4.png")])
        ]

        self.twitch.inventory = [c1, c2, c3, c4]

        # Execute
        result = self.gui.get_wanted_game_tree()

        # Verify
        # Expected: Game1 only
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["game_name"], "Game1")
        self.assertEqual(result[0]["game_icon"], "http://img1")
        self.assertFalse(result[0]["preferred"])
        # The public tree strips the internal Game object
        self.assertIsNone(result[0]["game_obj"])

        campaigns = result[0]["campaigns"]
        self.assertEqual(len(campaigns), 1)
        self.assertEqual(campaigns[0]["name"], "Campaign1")
        self.assertEqual(len(campaigns[0]["drops"]), 1)
        drop = campaigns[0]["drops"][0]
        self.assertEqual(drop["name"], "Drop1")
        self.assertEqual(drop["benefits"], [{"name": "Badge1", "image_url": "http://b1.png"}])
        self.assertEqual(drop["image_url"], "http://b1.png")

    def test_get_wanted_tree_claimed_filtering(self):
        # Setup Settings
        self.twitch.settings.games_to_watch = ["Game1"]
        self.twitch.settings.preferred_games = []
        self.twitch.settings.drop_name_blacklist = []
        self.twitch.settings.mining_benefits = {"BADGE": True}

        # Setup Inventory
        # Drop is claimed -> Should be hidden
        c1 = MagicMock(spec=DropsCampaign)
        c1.id = "c1_id"
        c1.name = "Campaign1"
        c1.campaign_url = "http://url1"
        c1.game = Game({"id": 1, "name": "Game1", "boxArtURL": "http://img1"})
        c1.can_earn_within.return_value = True
        c1.drops = [
            _make_drop(
                "Drop1",
                [_make_benefit("Badge1", BenefitType.BADGE, "http://b1.png")],
                is_claimed=True,
            )
        ]

        self.twitch.inventory = [c1]

        # Execute
        result = self.gui.get_wanted_game_tree()

        # Verify
        self.assertEqual(len(result), 0)


if __name__ == "__main__":
    unittest.main()
