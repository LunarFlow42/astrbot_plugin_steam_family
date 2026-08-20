import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import sys

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

from astrbot_plugin_steam_family.main import SteamFamilyPlugin
from astrbot_plugin_steam_family.config import PluginConfig

@pytest.mark.asyncio
async def test_plugin_init_and_bind():
    mock_context = MagicMock()
    mock_context.get_config.return_value = {
        "access_token": "init_token",
        "steam_id": "76561198000000001"
    }
    
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
async def test_plugin_sub_unsub():
    mock_context = MagicMock()
    mock_context.get_config.return_value = {}
    plugin = SteamFamilyPlugin(mock_context)
    
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
