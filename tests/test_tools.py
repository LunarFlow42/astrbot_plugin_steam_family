import pytest
from astrbot_plugin_steam_family.core.storage import FamilyStorage
from astrbot_plugin_steam_family.core.steam_api import SharedApp, AppDetails
from astrbot_plugin_steam_family.agent.tools import SteamAgentTools

@pytest.mark.asyncio
async def test_agent_tools_queries(tmp_path):
    storage = FamilyStorage(str(tmp_path / "tools_test.db"))
    await storage.init_db()

    app1 = SharedApp(appid=1086940, name="Baldur's Gate 3", owner_steamids=["76561198000000001"])
    await storage.save_snapshot([app1])
    await storage.cache_app_details(AppDetails(
        appid=1086940,
        name="Baldur's Gate 3",
        chinese_name="博德之门3",
        genres=["RPG", "奇幻"],
        initial_price=298.0,
        final_price=298.0,
        positive_rate=96
    ))

    tools = SteamAgentTools(storage)
    
    # Query search
    res = await tools.query_family_games(keyword="博德之门")
    assert "博德之门3" in res
    assert "298" in res

    # Stats overview
    stats = await tools.get_family_stats()
    assert "Steam 家庭组资产全景简报" in stats

    # Roll game
    roll = await tools.roll_game(tag="RPG")
    assert "博德之门3" in roll
