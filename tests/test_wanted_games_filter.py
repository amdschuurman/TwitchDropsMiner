import unittest
from unittest.mock import MagicMock

from src.models.benefit import Benefit, BenefitType
from src.models.campaign import DropsCampaign
from src.models.game import Game
from src.services.stream_selector import StreamSelector


def _make_benefit(name, benefit_type):
    b = MagicMock(spec=Benefit)
    b.name = name
    b.type = benefit_type
    b.image_url = f"http://img/{name}"
    # Bind the real wantedness logic so mining_benefits filtering is exercised.
    b.is_wanted = Benefit.is_wanted.__get__(b, Benefit)
    return b


def _make_drop(name, *, is_claimed=False, required_minutes=15, benefits=(), base_can_earn=True):
    d = MagicMock()
    d.name = name
    d.is_claimed = is_claimed
    d.required_minutes = required_minutes
    d._base_can_earn.return_value = base_can_earn
    d.benefits = list(benefits)
    return d


def _make_campaign(cid, game, drops, *, can_earn_within=True):
    c = MagicMock(spec=DropsCampaign)
    c.id = cid
    c.name = f"Campaign {cid}"
    c.campaign_url = f"http://test.url/{cid}"
    c.game = game
    c.can_earn_within.return_value = can_earn_within
    c.drops = list(drops)
    return c


class TestWantedGamesFilter(unittest.TestCase):
    def setUp(self):
        # Mock Settings
        self.settings = MagicMock()
        self.settings.games_to_watch = ["Game1", "Game2"]
        self.settings.preferred_games = []
        self.settings.drop_name_blacklist = []
        self.settings.mining_benefits = {
            "BADGE": True,
            "DIRECT_ENTITLEMENT": True,
        }  # both allowed by default

    def test_filter_wanted_campaigns(self):
        # Campaign 1: Game1, can earn, has wanted benefit -> should be selected
        c1 = _make_campaign(
            "c1",
            Game({"id": 1, "name": "Game1"}),
            [_make_drop("Drop1", benefits=[_make_benefit("Benefit1", BenefitType.BADGE)])],
        )

        # Campaign 2: Game2, can earn, benefits present but none wanted -> NOT selected
        # (UNKNOWN distribution type is not in mining_benefits, so is_wanted is False)
        c2 = _make_campaign(
            "c2",
            Game({"id": 2, "name": "Game2"}),
            [_make_drop("Drop2", benefits=[_make_benefit("Benefit2", BenefitType.UNKNOWN)])],
        )

        # Campaign 3: Game3 (not in games_to_watch), can earn, wanted benefit -> NOT selected
        c3 = _make_campaign(
            "c3",
            Game({"id": 3, "name": "Game3"}),
            [_make_drop("Drop3", benefits=[_make_benefit("Benefit3", BenefitType.BADGE)])],
        )

        # Campaign 4: Game1, can earn, wanted benefit but drop already claimed -> NOT selected
        c4 = _make_campaign(
            "c4",
            Game({"id": 1, "name": "Game1"}),
            [
                _make_drop(
                    "Drop4",
                    is_claimed=True,
                    benefits=[_make_benefit("Benefit4", BenefitType.BADGE)],
                )
            ],
        )

        # Campaign 5: Game1, can NOT earn within the next hour, wanted benefit -> NOT selected
        c5 = _make_campaign(
            "c5",
            Game({"id": 1, "name": "Game1"}),
            [_make_drop("Drop5", benefits=[_make_benefit("Benefit5", BenefitType.BADGE)])],
            can_earn_within=False,
        )

        # Campaign 6: Game1, can earn, wanted benefit but zero watch-time required
        # (required_minutes <= 0 drops are excluded) -> NOT selected
        c6 = _make_campaign(
            "c6",
            Game({"id": 1, "name": "Game1"}),
            [
                _make_drop(
                    "Drop6",
                    required_minutes=0,
                    benefits=[_make_benefit("Benefit6", BenefitType.BADGE)],
                )
            ],
        )

        # Campaign 7: Game2, can earn, wanted benefit but drop not currently earnable
        # (_base_can_earn False, e.g. preconditions unmet or outside timeframe) -> NOT selected
        c7 = _make_campaign(
            "c7",
            Game({"id": 2, "name": "Game2"}),
            [
                _make_drop(
                    "Drop7",
                    base_can_earn=False,
                    benefits=[_make_benefit("Benefit7", BenefitType.BADGE)],
                )
            ],
        )

        inventory = [c1, c2, c3, c4, c5, c6, c7]
        stream_selector = StreamSelector()
        wanted_games = stream_selector.get_wanted_games(self.settings, inventory)

        self.assertEqual(len(wanted_games), 1)
        self.assertEqual(wanted_games[0].name, "Game1")
        self.assertIs(wanted_games[0], c1.game)


if __name__ == "__main__":
    unittest.main()
