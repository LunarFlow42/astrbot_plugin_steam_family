from unittest.mock import MagicMock, patch

import pytest

from core.steam_api import SteamFamilyClient
from tests.mock_data import MOCK_FAMILY_APPS_RESPONSE


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
                {
                    "appid": 100,
                    "name": "Valid Game",
                    "ownerSteamids": ["123"],
                    "appType": 1,
                },
                {
                    "appid": 101,
                    "name": "Palworld Dedicated Server",
                    "ownerSteamids": ["123"],
                    "is_free": True,
                },
                {
                    "appid": 102,
                    "name": "Cyberpunk 2077 Soundtrack",
                    "ownerSteamids": ["123"],
                    "is_free": True,
                },
                {
                    "appid": 103,
                    "name": "Excluded Game",
                    "ownerSteamids": ["123"],
                    "excludeReason": 1,
                },
            ]
        }
    }
    apps = client.parse_shared_apps(raw_data, filter_non_games=True)
    assert len(apps) == 1
    assert apps[0].appid == 100


@pytest.mark.asyncio
async def test_refresh_token_via_cookie():
    client = SteamFamilyClient()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "success": 1,
        "data": {"webapi_token": "refreshed_jwt_token_123"},
    }
    with patch.object(client._client, "get", return_value=mock_resp):
        token = await client.refresh_token_via_cookie("mock_steamLoginSecure_value")
        assert token == "refreshed_jwt_token_123"


def test_decode_steam_jwt():
    import base64
    import json
    import time

    from core.steam_api import decode_steam_jwt

    # Create dummy JWT
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).decode().rstrip("=")
    exp_future = int(time.time()) + 7200
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"sub": "76561198000000001", "exp": exp_future, "iat": int(time.time())}
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    dummy_token = f"{header}.{payload}.dummy_signature"

    info = decode_steam_jwt(dummy_token)
    assert info is not None
    assert info["valid"] is True
    assert info["steamid"] == "76561198000000001"
    assert info["is_expired"] is False
    assert info["remaining_hours"] >= 1.9


@pytest.mark.asyncio
async def test_get_shared_library_and_playtime():
    client = SteamFamilyClient()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "response": {
            "entries": [{"steamid": "76561198000000001", "appid": 1086940, "seconds_played": 3600}]
        }
    }
    with patch.object(client._client, "get", return_value=mock_resp) as mock_get:
        entries = await client.get_playtime_summary("12345", "token")
        assert len(entries) == 1
        assert entries[0]["appid"] == 1086940
        mock_get.assert_called_once()
