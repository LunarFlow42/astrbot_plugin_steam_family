import httpx
import pytest

from core.renderer import CardRenderer
from core.steam_api import SharedApp
from core.storage import FamilyStorage
from core.web_server import DashboardWebServer


@pytest.mark.asyncio
async def test_dashboard_web_server(tmp_path):
    db_path = str(tmp_path / "web_test.db")
    storage = FamilyStorage(db_path)
    await storage.init_db()
    renderer = CardRenderer()

    # Pre-populate sample snapshot
    await storage.save_snapshot(
        [
            SharedApp(appid=1086940, name="Baldur's Gate 3", owner_steamids=["76561198000000001"]),
            SharedApp(appid=1091500, name="Cyberpunk 2077", owner_steamids=["76561198000000002"]),
        ]
    )

    async def mock_sync():
        return "同步测试成功：共同步 2 款游戏"

    # Start server on free port
    server = DashboardWebServer(
        host="127.0.0.1",
        port=16188,
        storage=storage,
        renderer=renderer,
        sync_callback=mock_sync,
    )
    await server.start()

    try:
        async with httpx.AsyncClient() as client:
            # 1. Test HTML Dashboard view
            resp = await client.get("http://127.0.0.1:16188/")
            assert resp.status_code == 200
            assert "Steam 家庭资产全景看板" in resp.text
            assert "立即手动同步检查" in resp.text
            assert "Baldur's Gate 3" in resp.text

            # 2. Test /api/data
            api_resp = await client.get("http://127.0.0.1:16188/api/data")
            assert api_resp.status_code == 200
            data = api_resp.json()
            assert data["total_games"] == 2
            assert len(data["all_snapshot_apps"]) == 2

            # 3. Test /api/sync
            sync_resp = await client.post("http://127.0.0.1:16188/api/sync")
            assert sync_resp.status_code == 200
            assert sync_resp.json()["success"] is True
            assert "同步测试成功" in sync_resp.json()["message"]

            # 4. Test /api/db/remove_app (remove Baldur's Gate 3)
            rm_resp = await client.post("http://127.0.0.1:16188/api/db/remove_app?appid=1086940")
            assert rm_resp.status_code == 200
            assert rm_resp.json()["success"] is True

            # Verify app was removed from snapshot
            snap = await storage.get_latest_snapshot()
            assert 1086940 not in snap
            assert 1091500 in snap

            # 5. Test /api/db/reset_snapshots
            reset_resp = await client.post("http://127.0.0.1:16188/api/db/reset_snapshots")
            assert reset_resp.status_code == 200
            assert reset_resp.json()["success"] is True
            assert await storage.get_latest_snapshot() is None
    finally:
        await server.stop()
