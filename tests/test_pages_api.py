from unittest.mock import MagicMock

import pytest

from core.steam_api import SharedApp
from core.storage import FamilyStorage
from main import SteamFamilyPlugin


@pytest.mark.asyncio
async def test_plugin_web_apis(tmp_path):
    mock_context = MagicMock()
    mock_context.get_config.return_value = {
        "access_token": "mock_token",
        "steam_id": "76561198000000001",
    }
    plugin = SteamFamilyPlugin(mock_context)
    plugin.storage = FamilyStorage(str(tmp_path / "web_api_test.db"))
    await plugin.storage.init_db()

    # Pre-populate apps
    await plugin.storage.save_snapshot(
        [
            SharedApp(appid=1086940, name="Baldur's Gate 3", owner_steamids=["76561198000000001"]),
            SharedApp(appid=1091500, name="Cyberpunk 2077", owner_steamids=["76561198000000002"]),
        ]
    )

    # 1. Test web_api_get_data
    data = await plugin.web_api_get_data()
    assert data["total_games"] == 2
    assert len(data["all_snapshot_apps"]) == 2

    # 2. Test web_api_remove_app via dict payload
    resp = await plugin.web_api_remove_app({"appid": 1086940})
    assert resp["success"] is True
    snap = await plugin.storage.get_latest_snapshot()
    assert 1086940 not in snap
    assert 1091500 in snap

    # 3. Test web_api_reset_snapshots
    resp2 = await plugin.web_api_reset_snapshots()
    assert resp2["success"] is True
    assert await plugin.storage.get_latest_snapshot() is None
