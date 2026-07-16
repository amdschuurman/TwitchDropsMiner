from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from src.models.game import Game


def _make_campaign(id, game, ends_in_hours, remaining_drops):
    c = MagicMock()
    c.id = id
    c.name = f"Campaign {id}"
    c.game = game
    c.ends_at = datetime.now(timezone.utc) + timedelta(hours=ends_in_hours)
    # Mirror the DropsCampaign properties the service consumes:
    # finished == all drops claimed (or unearnable), remaining_drops == unclaimed count.
    c.finished = remaining_drops == 0
    c.remaining_drops = remaining_drops
    c.campaign_url = f"https://twitch.tv/drops/campaigns?dropID={id}"
    return c


def _make_service(wanted_games=None, alerted=None):
    from src.services.campaign_alert_service import CampaignAlertService

    service = CampaignAlertService.__new__(CampaignAlertService)
    service._alerted = set(alerted or ())
    # The service filters by twitch.wanted_games; an empty list disables the filter.
    service._twitch = MagicMock()
    service._twitch.wanted_games = list(wanted_games or ())
    return service


_R6 = Game({"id": 10, "name": "R6"})
_RUST = Game({"id": 20, "name": "Rust"})


def test_finds_expiring_campaigns():
    service = _make_service()

    expiring = _make_campaign("c1", _R6, 10, 2)
    not_expiring = _make_campaign("c2", _RUST, 48, 1)
    already_claimed = _make_campaign("c3", _R6, 5, 0)
    already_alerted = _make_campaign("c4", _RUST, 3, 1)
    service._alerted.add("c4")

    campaigns = [expiring, not_expiring, already_claimed, already_alerted]
    result = service._get_campaigns_to_alert(campaigns)

    assert len(result) == 1
    assert result[0].id == "c1"


def test_already_alerted_not_repeated():
    service = _make_service(alerted={"c1"})
    campaign = _make_campaign("c1", _R6, 5, 2)
    result = service._get_campaigns_to_alert([campaign])
    assert result == []


def test_finished_campaign_not_alerted():
    # finished=True must exclude a campaign even when drops appear to remain.
    service = _make_service()
    campaign = _make_campaign("c1", _R6, 5, 2)
    campaign.finished = True
    result = service._get_campaigns_to_alert([campaign])
    assert result == []


def test_wanted_games_filter_applies():
    # When wanted_games is non-empty, only campaigns for those games alert.
    service = _make_service(wanted_games=[_R6])

    r6_campaign = _make_campaign("c1", _R6, 5, 1)
    rust_campaign = _make_campaign("c2", _RUST, 5, 1)

    result = service._get_campaigns_to_alert([r6_campaign, rust_campaign])

    assert [c.id for c in result] == ["c1"]
