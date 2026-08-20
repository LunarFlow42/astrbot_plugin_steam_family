import pytest
from core.steam_api import SteamFamilyClient, SharedApp, AppDetails
from tests.mock_data import MOCK_FAMILY_APPS_RESPONSE, MOCK_APP_DETAILS_RESPONSE

@pytest.mark.asyncio
async def test_parse_shared_apps():
    client = SteamFamilyClient()
    apps = client.parse_shared_apps(MOCK_FAMILY_APPS_RESPONSE)
    assert len(apps) == 2
    assert apps[0].appid == 1086940
    assert apps[0].name == "Baldur's Gate 3"
    assert "76561198000000001" in apps[0].owner_steamids

@pytest.mark.asyncio
async def test_filter_non_games():
    client = SteamFamilyClient()
    raw_data = {
        "response": {
            "apps": [
                {"appid": 100, "name": "Valid Game", "owner_steamids": ["123"], "app_type": 1},
                {"appid": 101, "name": "Palworld Dedicated Server", "owner_steamids": ["123"], "is_free": True},
                {"appid": 102, "name": "Cyberpunk 2077 Soundtrack", "owner_steamids": ["123"], "is_free": True},
                {"appid": 103, "name": "Excluded Game", "owner_steamids": ["123"], "exclude_reason": 1}
            ]
        }
    }
    apps = client.parse_shared_apps(raw_data, filter_non_games=True)
    assert len(apps) == 1
    assert apps[0].appid == 100
