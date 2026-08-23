import sys
from unittest.mock import MagicMock

import pytest

# Create lightweight mock for astrbot api modules if running in standalone test environment
if "astrbot.api.star" not in sys.modules:
    mock_star_module = MagicMock()

    class MockStar:
        def __init__(self, context):
            self.context = context

    def mock_register(*args, **kwargs):
        def decorator(cls):
            return cls

        return decorator

    mock_star_module.Star = MockStar
    mock_star_module.register = mock_register
    mock_star_module.Context = MagicMock
    sys.modules["astrbot.api.star"] = mock_star_module

if "astrbot.api.event" not in sys.modules:
    mock_event_module = MagicMock()

    class MockFilter:
        @staticmethod
        def command(*args, **kwargs):
            def decorator(func):
                return func

            return decorator

        @staticmethod
        def llm_tool(*args, **kwargs):
            def decorator(func):
                return func

            return decorator

    mock_event_module.filter = MockFilter
    mock_event_module.AstrMessageEvent = MagicMock
    mock_event_module.MessageEventResult = MagicMock
    sys.modules["astrbot.api.event"] = mock_event_module

if "astrbot.api.message_components" not in sys.modules:
    mock_comp_module = MagicMock()
    mock_comp_module.Plain = lambda text: text
    mock_comp_module.Image = MagicMock()
    sys.modules["astrbot.api.message_components"] = mock_comp_module

from unittest.mock import AsyncMock, patch

from core.steam_api import AppDetails, SharedApp
from main import SteamFamilyPlugin


@pytest.mark.asyncio
async def test_plugin_init_and_bind():
    mock_context = MagicMock()
    mock_context.get_config.return_value = {
        "access_token": "init_token",
        "steam_id": "76561198000000001",
    }

    with patch.object(SteamFamilyPlugin, "_init_and_start_polling"):
        plugin = SteamFamilyPlugin(mock_context)
        assert plugin.config.access_token == "init_token"
        assert plugin.config.steam_id == "76561198000000001"

        # Test bind command
        mock_event = MagicMock()
        mock_event.plain_result = lambda text: text
        results = [r async for r in plugin.cmd_bind(mock_event, "new_token", "76561198000000002")]
        assert "绑定成功" in results[0]
        assert plugin.config.access_token == "new_token"
        assert plugin.config.steam_id == "76561198000000002"


@pytest.mark.asyncio
async def test_plugin_sub_unsub(tmp_path):
    mock_context = MagicMock()
    mock_context.get_config.return_value = {}
    with patch.object(SteamFamilyPlugin, "_init_and_start_polling"):
        plugin = SteamFamilyPlugin(mock_context)
        plugin.storage.db_path = str(tmp_path / "test_sub.db")
        await plugin.storage.init_db()

        mock_event = MagicMock()
        mock_event.unified_msg_origin = "session_group_999"
        mock_event.plain_result = lambda text: text

        results = [r async for r in plugin.cmd_sub(mock_event)]
        assert "成功订阅" in results[0]

        subs = await plugin.storage.get_subscribers()
        assert "session_group_999" in subs

        results2 = [r async for r in plugin.cmd_unsub(mock_event)]
        assert "已取消" in results2[0]

        subs2 = await plugin.storage.get_subscribers()
        assert "session_group_999" not in subs2


@pytest.mark.asyncio
async def test_free_games_skipped_from_broadcast(tmp_path):
    mock_context = MagicMock()
    mock_context.get_config.return_value = {
        "access_token": "token",
        "steam_id": "76561198000000001",
        "broadcast_free_games": False,
    }
    with patch.object(SteamFamilyPlugin, "_init_and_start_polling"):
        plugin = SteamFamilyPlugin(mock_context)
        plugin.storage.db_path = str(tmp_path / "test_free.db")
        await plugin.storage.init_db()
        await plugin.storage.subscribe("group_123")

        # Mock initial baseline
        initial_apps = [SharedApp(appid=100, name="Game A", owner_steamids=["76561198000000001"])]
        await plugin.storage.save_snapshot(initial_apps)

        # Mock second check with 1 free app added
        new_apps = [
            SharedApp(appid=100, name="Game A", owner_steamids=["76561198000000001"]),
            SharedApp(appid=200, name="Free Game B", owner_steamids=["76561198000000001"]),
        ]
        plugin.client.get_family_group_id = AsyncMock(return_value="group_id_1")
        plugin.client.get_shared_library = AsyncMock(return_value=new_apps)
        plugin.client.get_player_summaries = AsyncMock(return_value={})
        plugin.client.get_app_details = AsyncMock(
            return_value=AppDetails(
                appid=200,
                name="Free Game B",
                is_free=True,
                initial_price=0.0,
                final_price=0.0,
            )
        )

        msg = await plugin._check_and_notify()
        assert "免费游戏" in msg or "静默忽略" in msg
