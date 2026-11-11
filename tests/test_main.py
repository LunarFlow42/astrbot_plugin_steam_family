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

import asyncio
import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

from core.auth_manager import SteamAuthManager
from core.steam_api import AppDetails, SharedApp
from core.steam_auth import SteamLoginError
from main import SteamFamilyPlugin
from tests.test_notifications import _offline_client


@pytest.fixture(autouse=True)
def _isolate_plugin_data_dir(tmp_path, monkeypatch):
    """(P2) 所有用例构造插件前打桩 _get_db_path，杜绝测试在仓库 CWD 生成 data/ 生产库文件。"""
    monkeypatch.setattr("main._get_db_path", lambda: str(tmp_path / "engine.db"))


@pytest.mark.asyncio
async def test_plugin_init_and_bind(tmp_path):
    mock_context = MagicMock()
    mock_context.get_config.return_value = {
        "access_token": "init_token",
        "steam_id": "76561198000000001",
    }

    with patch.object(SteamFamilyPlugin, "_init_and_start_polling"):
        plugin = SteamFamilyPlugin(mock_context)
        assert plugin.config.access_token == "init_token"
        assert plugin.config.steam_id == "76561198000000001"

        # storage 指向 tmp_path：cmd_bind 的 _save_credentials 会经后台任务写 KV 凭据，
        # 防止写入仓库真实 data 目录
        plugin.storage.db_path = str(tmp_path / "bind.db")
        await plugin.storage.init_db()

        # client 全量离线化：cmd_bind 末尾会 _spawn 一个后台补 check，
        # 补 check 链路触达的接口（含 playtime/价格批量/商店详情）逐项 AsyncMock
        _offline_client(plugin)
        plugin.client.get_shared_library = AsyncMock(
            return_value=[
                SharedApp(appid=100, name="Game A", owner_steamids=["76561198000000002"])
            ]
        )
        plugin.auth_manager.ensure_valid_access_token = AsyncMock(return_value=True)

        # 整体替换真实 httpx 传输通道：任何代码路径一旦尝试真实网络请求，后台任务
        # 会立刻以异常退出并被下方断言捕获（修复此前靠 loop 关闭时序侥幸通过的问题）
        plugin.client._client = MagicMock()

        # Test bind command
        mock_event = MagicMock()
        mock_event.plain_result = lambda text: text
        mock_event.get_group_id = lambda: ""  # 私聊会话（敏感命令群聊防御的放行路径）
        results = [r async for r in plugin.cmd_bind(mock_event, "new_token", "76561198000000002")]
        assert "绑定成功" in results[0]
        assert plugin.config.access_token == "new_token"
        assert plugin.config.steam_id == "76561198000000002"

        # 等待全部后台任务（KV 落库 + 补 check 及其级联 spawn 的任务）真正跑完，
        # 并断言没有任何任务以异常退出（= 全程只走了 mock，没有触达真实传输）
        for _ in range(5):
            pending = [t for t in plugin._bg_tasks if not t.done()]
            if not pending:
                break
            outcomes = await asyncio.gather(*pending, return_exceptions=True)
            errors = [o for o in outcomes if isinstance(o, BaseException)]
            assert errors == []
        assert all(t.done() for t in plugin._bg_tasks)

        # 后台补 check 已走 mock 数据建立了基线，凭据 KV 亦已落库到 tmp_path 存储
        snapshot = await plugin.storage.get_latest_snapshot()
        assert 100 in snapshot
        assert await plugin.storage.get_kv("access_token") == "new_token"


def test_sanitize_error_masks_sensitive_tokens():
    """(安全) _sanitize_error：异常消息中的 token / cookie 值必须统一打码为 ***。"""
    from main import _sanitize_error

    # httpx.HTTPStatusError 风格：异常 str 内含完整请求 URL（access_token 查询参数）
    exc = RuntimeError(
        "Server error '401 Unauthorized' for url "
        "'https://api.steampowered.com/IFamilyGroupsService/GetFamilyGroup/v1/"
        "?access_token=eyJhbGciOi.leak.sig&steamid=76561198000000001'"
    )
    text = _sanitize_error(exc)
    assert text.startswith("RuntimeError:")
    assert "eyJhbGciOi" not in text
    assert "access_token=***" in text
    assert "steamid=76561198000000001" in text  # 非敏感参数不受影响

    # refresh_token（含 %5F URL 编码下划线变体）与 cookie 值同样覆盖
    text2 = _sanitize_error(
        RuntimeError(
            "refresh%5Ftoken=abc123 refresh_token=def456 "
            "cookie=76561198%7C%7CsteamLoginSecure; sessionid=keepme"
        )
    )
    assert "abc123" not in text2
    assert "def456" not in text2
    assert "76561198%7C%7CsteamLoginSecure" not in text2
    assert "sessionid=keepme" in text2  # 分号边界后的无关键值不误伤


@pytest.mark.asyncio
async def test_check_failure_reply_and_sync_state_sanitized(tmp_path):
    """兜底 except：群聊返回消息与 sync_last_message 落库内容均不得包含 access_token 明文。"""
    mock_context = MagicMock()
    mock_context.get_config.return_value = {
        "access_token": "token",
        "steam_id": "76561198000000001",
        "family_group_id": "group_id_1",
    }
    with patch.object(SteamFamilyPlugin, "_init_and_start_polling"):
        plugin = SteamFamilyPlugin(mock_context)
        plugin.storage.db_path = str(tmp_path / "sanitize.db")
        await plugin.storage.init_db()

        plugin.auth_manager.ensure_valid_access_token = AsyncMock(return_value=True)

        async def _boom(group_id, access_token, **kwargs):
            raise RuntimeError(
                "Server error '401 Unauthorized' for url "
                "'https://api.steampowered.com/?access_token=eyJhbGciOi.SECRET'"
            )

        plugin.client.get_shared_library = AsyncMock(side_effect=_boom)

        msg = await plugin._check_and_notify()
        assert msg.startswith("❌")
        assert "eyJhbGciOi" not in msg
        assert "access_token=***" in msg

        # 看板 last_message 同步状态已脱敏落库
        sync_msg = await plugin.storage.get_sync_state("sync_last_message")
        assert sync_msg
        assert "eyJhbGciOi" not in sync_msg


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

        # Mock second check with 1 free app added（批量详情一并 mock，保证用例完全离线）
        new_apps = [
            SharedApp(appid=100, name="Game A", owner_steamids=["76561198000000001"]),
            SharedApp(appid=200, name="Free Game B", owner_steamids=["76561198000000001"]),
        ]
        plugin.client.get_family_group_id = AsyncMock(return_value="group_id_1")
        plugin.client.get_shared_library = AsyncMock(return_value=new_apps)
        plugin.client.get_player_summaries = AsyncMock(return_value={})
        plugin.client.get_review_summary = AsyncMock(return_value=None)
        plugin.client.get_playtime_summary = AsyncMock(return_value=[])
        plugin.client.get_price_overview_batch = AsyncMock(return_value={})
        plugin.client.get_app_details_batch = AsyncMock(
            return_value={
                200: AppDetails(
                    appid=200,
                    name="Free Game B",
                    is_free=True,
                    initial_price=0.0,
                    final_price=0.0,
                )
            }
        )

        msg = await plugin._check_and_notify()
        # filtered 口径：免费游戏静默消解不计入 success，但保持"免费游戏"字样
        assert "免费游戏" in msg or "静默忽略" in msg
        assert "1 条已静默消解" in msg


# ---------------------------------------------------------------------------
# 敏感凭据命令群聊防御（/steam_bind、/steam_code、/steam_cred）
# ---------------------------------------------------------------------------
def _chat_event(group_id: str = ""):
    """构造带群聊标记的假事件：group_id 真值=群聊，空串=私聊。"""
    event = MagicMock()
    event.plain_result = lambda text: text
    event.get_group_id = lambda: group_id
    return event


@pytest.mark.asyncio
async def test_sensitive_commands_refused_in_group_chat(tmp_path):
    """(P2-11) /steam_bind、/steam_code、/steam_cred 群聊一律拒绝；私聊与异常兼容路径照常。"""
    mock_context = MagicMock()
    mock_context.get_config.return_value = {
        "access_token": "old_token",
        "steam_id": "76561198000000001",
    }
    with patch.object(SteamFamilyPlugin, "_init_and_start_polling"):
        plugin = SteamFamilyPlugin(mock_context)
        plugin.storage.db_path = str(tmp_path / "group_guard.db")
        await plugin.storage.init_db()
        plugin.config.shared_secret = "cWFhYmJjY2RkZA=="

        group_event = _chat_event(group_id="12345")

        # /steam_bind：群聊拒绝，且绝不落库保存 token
        results = [r async for r in plugin.cmd_bind(group_event, "group_token", "76561198000000002")]
        assert "请勿在群聊执行" in results[0]
        assert plugin.config.access_token == "old_token"

        # /steam_code：群聊拒绝（不输出令牌验证码）
        results = [r async for r in plugin.cmd_code(group_event)]
        assert "请勿在群聊执行" in results[0]

        # /steam_cred：群聊拒绝，且不保存任何凭据
        results = [r async for r in plugin.cmd_cred(group_event, "acc", "pwd", "sec")]
        assert "请勿在群聊执行" in results[0]
        assert plugin.config.account_name != "acc"
        assert plugin.config.password != "pwd"

        # 兼容未来适配器：get_group_id 抛异常时保守放行（不误伤私聊场景）
        weird_event = MagicMock()
        weird_event.plain_result = lambda text: text
        weird_event.get_group_id = MagicMock(side_effect=RuntimeError("adapter not supported"))
        results = [r async for r in plugin.cmd_code(weird_event)]
        assert "请勿在群聊执行" not in results[0]

        # 私聊（get_group_id 返回空）照常执行：不出现拒绝文案
        private_event = _chat_event(group_id="")
        results = [r async for r in plugin.cmd_code(private_event)]
        assert "请勿在群聊执行" not in results[0]


# ---------------------------------------------------------------------------
# auth_manager 保活打磨（P2-9a / 9b）
# ---------------------------------------------------------------------------
def _make_jwt(remaining_seconds: float, steamid: str = "76561198000000001") -> str:
    """构造本地可解析的假 Steam JWT（exp/iat/sub），供凭据寿命断言使用。"""
    payload = {
        "sub": steamid,
        "aud": ["https://api.steampowered.com"],
        "exp": int(time.time() + remaining_seconds),
        "iat": int(time.time()),
    }
    seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzUxMiJ9.{seg}.fake-signature"


def _make_auth_manager(store: dict | None = None):
    """构建全离线 SteamAuthManager：凭据存内存 dict，底层 auth 客户端全 mock。"""
    credentials = store if store is not None else {}
    auth = MagicMock()
    manager = SteamAuthManager(
        auth=auth,
        get_cred=lambda name: credentials.get(name, ""),
        set_cred=lambda key, value: credentials.__setitem__(key, value),
        cookie_refresh_fn=None,  # 显式禁用 Cookie 兜底，便于精确断言分支
        notify_fn=None,
    )
    return manager, auth, credentials


@pytest.mark.asyncio
async def test_exchange_refresh_token_at_still_valid_skips_relogin():
    """(P2-9a) 续期失败但当前 Access Token JWT 仍有效（>20 分钟）→ 视为可用，不触发昂贵密码重登。"""
    manager, auth, _ = _make_auth_manager(
        {
            "refresh_token": "rt_old",
            "access_token": _make_jwt(2 * 3600),  # 剩余 2 小时
            "account_name": "user",
            "password": "pw",
        }
    )
    auth.refresh_tokens = AsyncMock(side_effect=SteamLoginError("exchange rejected"))
    auth.login_with_password = AsyncMock()  # 一旦被调用测试即失败

    ok = await manager.exchange_refresh_token()

    assert ok is True
    assert "仍可用" in manager.last_keepalive_note
    assert "续期暂失败" in manager.last_keepalive_note
    auth.login_with_password.assert_not_called()  # AT 未真过期，不走密码重登

    # 对照：AT 真过期（剩余 < 20 分钟）→ 照常返回失败，允许走重登链路
    manager2, auth2, _ = _make_auth_manager(
        {
            "refresh_token": "rt_old",
            "access_token": _make_jwt(5 * 60),
            "account_name": "user",
            "password": "pw",
        }
    )
    auth2.refresh_tokens = AsyncMock(side_effect=SteamLoginError("boom"))
    ok2 = await manager2.exchange_refresh_token()
    assert ok2 is False
    assert "续期失败" in manager2.last_keepalive_note


@pytest.mark.asyncio
async def test_exchange_refresh_token_force_new_keeps_failure_semantics():
    """(P2-9a 边界) force_new=True（手动续期 / 401 恢复链）时 "JWT 未过期" 不可信，照常返回失败。"""
    manager, auth, _ = _make_auth_manager(
        {
            "refresh_token": "rt_old",
            "access_token": _make_jwt(2 * 3600),
            "account_name": "user",
            "password": "pw",
        }
    )
    auth.refresh_tokens = AsyncMock(side_effect=SteamLoginError("exchange rejected"))

    ok = await manager.exchange_refresh_token(force_new=True)

    assert ok is False
    assert "续期失败" in manager.last_keepalive_note


@pytest.mark.asyncio
async def test_auto_relogin_requires_shared_secret():
    """(P2-9b) 自动重登需密码+shared_secret 齐备；只有密码时跳过并记录明确提示，手动路径不受影响。"""
    cfg = {
        "access_token": _make_jwt(-60),  # 已过期
        "refresh_token": "rt_dead",
        "account_name": "user",
        "password": "pw",
        "enable_auto_relogin": True,
    }
    manager, auth, cfg = _make_auth_manager(cfg)
    auth.refresh_tokens = AsyncMock(side_effect=SteamLoginError("rt dead"))
    auth.login_with_password = AsyncMock(
        return_value={
            "access_token": _make_jwt(24 * 3600),
            "refresh_token": "new_rt",
        }
    )

    # 只有密码没有 shared_secret：跳过自动重登，绝不触发 password 登录
    ok = await manager.ensure_valid_access_token()

    assert ok is False
    assert manager.last_keepalive_note == "已配置密码但缺 shared_secret，自动重登不可用"
    auth.login_with_password.assert_not_called()

    # 补上 shared_secret：同一情形下自动重登链路正常生效
    cfg["shared_secret"] = "cWFhYmJjY2RkZA=="
    auth.refresh_tokens = AsyncMock(side_effect=SteamLoginError("rt dead"))
    ok2 = await manager.ensure_valid_access_token()
    assert ok2 is True
    auth.login_with_password.assert_awaited_once()

    # 手动 /steam_relogin 路径（relogin_with_password 直接调用）不受 shared_secret 缺失限制：
    manager4, auth4, _ = _make_auth_manager(
        {"account_name": "user", "password": "pw"}  # 无 shared_secret
    )
    auth4.login_with_password = AsyncMock(
        return_value={
            "access_token": _make_jwt(24 * 3600),
            "refresh_token": "new_rt",
        }
    )
    result = await manager4.relogin_with_password()
    assert result.startswith("✅")
    auth4.login_with_password.assert_awaited_once()
