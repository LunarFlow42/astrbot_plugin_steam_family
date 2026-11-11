from unittest.mock import MagicMock, patch

import pytest

from core.steam_api import SharedApp
from core.storage import FamilyStorage
from main import SteamFamilyPlugin

# 破坏性（幂等性差/副作用强）接口的末段路由名集合：一律不允许注册 GET
_DESTRUCTIVE_TAIL_SEGMENTS = {"sync", "remove_app", "reset_snapshots", "reset"}


@pytest.fixture(autouse=True)
def _isolate_plugin_data_dir(tmp_path, monkeypatch):
    """(P2) 所有用例构造插件前打桩 _get_db_path，杜绝测试在仓库 CWD 生成 data/ 生产库文件。"""
    monkeypatch.setattr("main._get_db_path", lambda: str(tmp_path / "engine.db"))


@pytest.mark.asyncio
async def test_destructive_web_api_routes_do_not_register_get():
    """(P2-10) /sync、/reset*、/remove_app* 等破坏性接口只注册 POST+OPTIONS；/data 保持 GET。"""
    mock_context = MagicMock()
    mock_context.get_config.return_value = {
        "access_token": "mock_token",
        "steam_id": "76561198000000001",
    }
    with patch.object(SteamFamilyPlugin, "_init_and_start_polling"):
        SteamFamilyPlugin(mock_context)

    registered: dict[str, list[str]] = {}
    for call in mock_context.register_web_api.call_args_list:
        route, methods = call.args[0], call.args[2]
        registered[route] = [str(m).upper() for m in methods]

    assert registered, "应至少注册一个 Web API 路由"

    destructive_routes = [
        route
        for route in registered
        if route.rstrip("/").split("/")[-1] in _DESTRUCTIVE_TAIL_SEGMENTS
    ]
    assert destructive_routes, "未发现任何破坏性路由（测试覆盖失效）"
    for route in destructive_routes:
        methods = registered[route]
        assert "GET" not in methods, f"破坏性接口 {route} 不得注册 GET"
        assert "POST" in methods, f"破坏性接口 {route} 必须保留 POST"
        assert "OPTIONS" in methods, f"{route} 必须保留 OPTIONS（CORS 预检）"

    # 只读数据接口 /data 保持 GET 可用（前端看板轮询依赖）
    for route in ("data", "/data", "/api/data"):
        if route in registered:
            assert "GET" in registered[route], f"{route} 应继续保持 GET"


@pytest.mark.asyncio
async def test_plugin_web_apis(tmp_path):
    mock_context = MagicMock()
    mock_context.get_config.return_value = {
        "access_token": "mock_token",
        "steam_id": "76561198000000001",
    }
    with patch.object(SteamFamilyPlugin, "_init_and_start_polling"):
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
