import pytest

from core.steam_api import AppDetails, SharedApp
from core.storage import FamilyStorage


@pytest.mark.asyncio
async def test_storage_lifecycle(tmp_path):
    db_path = str(tmp_path / "test.db")
    storage = FamilyStorage(db_path)
    await storage.init_db()

    # Test Subscription
    await storage.subscribe("session_group_1")
    subs = await storage.get_subscribers()
    assert "session_group_1" in subs
    await storage.unsubscribe("session_group_1")
    assert "session_group_1" not in await storage.get_subscribers()

    # Test Snapshot Save & Retrieve
    apps = [
        SharedApp(appid=1086940, name="Baldur's Gate 3", owner_steamids=["76561198000000001"]),
        SharedApp(appid=1245620, name="ELDEN RING", owner_steamids=["76561198000000002"]),
    ]
    snapshot_id = await storage.save_snapshot(apps)
    assert snapshot_id is not None

    latest = await storage.get_latest_snapshot()
    assert latest is not None
    assert len(latest) == 2
    assert latest[1086940].name == "Baldur's Gate 3"

    # Test Cache App Details and Rankings
    app_detail = AppDetails(
        appid=1086940,
        name="Baldur's Gate 3",
        initial_price=298.0,
        final_price=298.0,
        genres=["RPG"],
    )
    await storage.cache_app_details(app_detail)
    cached = await storage.get_cached_app(1086940)
    assert cached is not None
    assert cached.final_price == 298.0

    # Test Member Profiles
    profiles = {
        "76561198000000001": {
            "steamid": "76561198000000001",
            "persona_name": "义父张三",
            "avatar_url": "https://avatars.steamstatic.com/test_avatar.jpg",
        },
        "76561198000000002": {
            "steamid": "76561198000000002",
            "persona_name": "义父李四",
            "avatar_url": "",
        },
    }
    await storage.save_member_profiles(profiles)
    saved_profiles = await storage.get_member_profiles()
    assert "76561198000000001" in saved_profiles
    assert saved_profiles["76561198000000001"]["persona_name"] == "义父张三"

    rankings = await storage.get_family_rankings()
    assert len(rankings) >= 1
    assert rankings[0]["persona_name"] in ["义父张三", "义父李四"]

    dashboard = await storage.get_dashboard_data()
    assert dashboard["total_games"] == 2
    assert "member_profiles" in dashboard
    assert dashboard["member_profiles"]["76561198000000001"]["persona_name"] == "义父张三"
    assert (
        "义父张三" in dashboard["all_snapshot_apps"][0]["owners"]
        or "义父李四" in dashboard["all_snapshot_apps"][0]["owners"]
    )
