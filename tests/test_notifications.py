"""库存变动监控可靠管线的离线单元测试。

覆盖：事件入队/补播、推送失败退避重试、移出/归属/状态/降价播报、
大规模移出安全阀、单飞锁并发合并、免费过滤新语义（details=None 仍播报）、
订阅者推送失败计数与死订阅者清理。
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

# 复用 test_main.py 顶部的 astrbot mock 注入方式，保证独立运行时也能离线导入插件。
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
        class PermissionType:
            ADMIN = "admin"

        @staticmethod
        def permission_type(*args, **kwargs):
            return lambda func: func

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
import logging
from contextlib import contextmanager

import pytest

from core.steam_api import AppDetails, SharedApp
from main import SteamFamilyPlugin


class FakeChain:
    """MessageChain 假实现：保存构造参数，便于断言推送文本内容。"""

    def __init__(self, items=None, *args, **kwargs):
        self.items = list(items) if items else []


class _TextPlain:
    """可读文本版 Plain（main 回退实现不保存 text，测试中替换用于断言）。"""

    def __init__(self, text):
        self.text = text


@contextmanager
def fake_message_chain():
    """同时替换 main.MessageChain 与 main.Comp.Plain，使推送文本可在链上被断言。"""
    with patch("main.MessageChain", FakeChain), patch("main.Comp.Plain", _TextPlain):
        yield


def _make_plugin(tmp_path, db_name="notify.db", **config_overrides):
    """构建一个完全离线的插件实例：假 context + 真 SQLite storage + 全 mock client。"""
    mock_context = MagicMock()
    cfg = {"access_token": "token", "steam_id": "76561198000000001"}
    cfg.update(config_overrides)
    mock_context.get_config.return_value = cfg
    mock_context.send_message = AsyncMock()
    with (
        patch.object(SteamFamilyPlugin, "_init_and_start_polling"),
        # (P2) 构造前打桩 db 路径：杜绝测试在仓库 CWD 生成 data/ 生产库文件
        patch("main._get_db_path", return_value=str(tmp_path / "engine.db")),
    ):
        plugin = SteamFamilyPlugin(mock_context)
    plugin.storage.db_path = str(tmp_path / db_name)
    return plugin


def _offline_client(plugin):
    """给 client 打齐全套离线 mock（各项可按测试需求再覆盖）。"""
    plugin.client.get_family_group_id = AsyncMock(return_value="group_id_1")
    plugin.client.get_shared_library = AsyncMock(return_value=[])
    plugin.client.get_player_summaries = AsyncMock(return_value={})
    plugin.client.get_review_summary = AsyncMock(return_value=None)
    plugin.client.get_app_details_batch = AsyncMock(return_value={})
    plugin.client.get_app_details = AsyncMock(return_value=None)
    # 0.3.0 新增接线点：游玩统计与降价监控批量价格也一并 mock，保证测试完全离线
    plugin.client.get_playtime_summary = AsyncMock(return_value=[])
    plugin.client.get_price_overview_batch = AsyncMock(return_value={})


async def _pending_rows(plugin) -> list[dict]:
    """绕开 storage 只读封装，直接查 pending_notifications 原始行（含 retries）。"""
    async with plugin.storage._get_conn() as db:
        async with db.execute(
            "SELECT id, kind, retries, next_attempt_at FROM pending_notifications ORDER BY id"
        ) as cursor:
            rows = await cursor.fetchall()
    return [{"id": r[0], "kind": r[1], "retries": r[2], "due": r[3]} for r in rows]


async def _force_due(plugin):
    """把所有待推送事件的 next_attempt_at 归零（模拟指数退避到期）。"""
    async with plugin.storage._get_conn() as db:
        await db.execute("UPDATE pending_notifications SET next_attempt_at = 0")
        await db.commit()


def _pushed_texts(send_mock) -> list[str]:
    """从 context.send_message 调用记录中提取所有推送过的纯文本内容。"""
    texts = []
    for call in send_mock.await_args_list:
        chain = call.args[1] if len(call.args) > 1 else call.kwargs.get("chain")
        for item in getattr(chain, "items", []):
            text = getattr(item, "text", None) if not isinstance(item, str) else item
            if isinstance(text, str):
                texts.append(text)
    return texts


async def test_failed_enqueue_retries_and_replays_later(tmp_path):
    """(a) 快照先落库 + 播报失败 → bump_pending_retry（retries+1），下一轮补播成功后消失。"""
    plugin = _make_plugin(tmp_path, db_name="retry.db", broadcast_free_games=True)
    _offline_client(plugin)
    await plugin.storage.init_db()
    await plugin.storage.subscribe("group_1")

    # 建立基线
    baseline = [SharedApp(appid=100, name="Base", owner_steamids=["76561198000000001"])]
    await plugin.storage.save_snapshot(baseline)

    # 详情批量/单发全部抛错（模拟 429 熔断期）→ 事件级失败 → 退避重试，不逃出炉子
    plugin.client.get_shared_library = AsyncMock(
        return_value=[
            SharedApp(appid=100, name="Base", owner_steamids=["76561198000000001"]),
            SharedApp(appid=300, name="New Game", owner_steamids=["76561198000000001"]),
        ]
    )
    plugin.client.get_app_details_batch = AsyncMock(side_effect=RuntimeError("bulk boom"))
    plugin.client.get_app_details = AsyncMock(side_effect=RuntimeError("single boom"))

    msg = await plugin._check_and_notify()
    assert "入队 1 条" in msg  # 返回消息基于"已入队 N 条事件"

    # 快照已先落库（基线永远以持久化为准），事件未丢且重试计数 +1
    snapshot = await plugin.storage.get_latest_snapshot()
    assert 300 in snapshot
    assert await plugin.storage.pending_count() == 1
    rows = await _pending_rows(plugin)
    assert rows[0]["kind"] == "added"
    assert rows[0]["retries"] == 1
    assert rows[0]["due"] > 0  # 已设置指数退避，不会立即重复推送

    # 模拟退避到期：归零 next_attempt_at，详情恢复后下一轮补播成功
    await _force_due(plugin)
    plugin.client.get_app_details_batch = AsyncMock(
        return_value={300: AppDetails(appid=300, name="New Game")}
    )
    plugin.context.send_message = AsyncMock()
    with fake_message_chain():
        stats = await plugin._drain_pending_notifications()
    assert stats["success"] == 1
    assert await plugin.storage.pending_count() == 0  # mark 后消失
    texts = _pushed_texts(plugin.context.send_message)
    assert any("New Game" in t for t in texts)


async def test_removed_event_pushes_text(tmp_path):
    """(b) 移出事件会推送纯文字消息（含商店链接与曾归属昵称）。"""
    plugin = _make_plugin(tmp_path, db_name="removed.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    await plugin.storage.subscribe("group_1")
    # 预存成员昵称，验证从 member_profiles 解析
    await plugin.storage.save_member_profiles({"76561198000000001": {"persona_name": "义父大人"}})

    baseline = [
        SharedApp(appid=100, name="Base", owner_steamids=["76561198000000001"]),
        SharedApp(appid=400, name="Old Game", owner_steamids=["76561198000000001"]),
    ]
    await plugin.storage.save_snapshot(baseline)

    plugin.client.get_shared_library = AsyncMock(
        return_value=[SharedApp(appid=100, name="Base", owner_steamids=["76561198000000001"])]
    )
    with fake_message_chain():
        msg = await plugin._check_and_notify()

    assert "移出 1 款" in msg
    texts = _pushed_texts(plugin.context.send_message)
    assert len(texts) == 1
    assert "🚪 【Steam 家庭库移出】《Old Game》(AppID 400) 已离开家庭共享库" in texts[0]
    assert "https://store.steampowered.com/app/400" in texts[0]
    assert "曾归属: 义父大人" in texts[0]
    # 事件处理完成，队列清空
    assert await plugin.storage.pending_count() == 0


async def test_owner_change_and_status_change_dispatch(tmp_path):
    """(c) owner_change / status_change 事件按 kind 分发并推送对应文案。"""
    plugin = _make_plugin(tmp_path, db_name="dispatch.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    await plugin.storage.subscribe("group_1")
    await plugin.storage.save_member_profiles(
        {"s1": {"persona_name": "甲"}, "s2": {"persona_name": "乙"}, "s3": {"persona_name": "丙"}}
    )

    await plugin.storage.enqueue_pending(
        "owner_change",
        {
            "appid": 800,
            "name": "Elden Ring",
            "added_owners": ["s1", "s2"],
            "removed_owners": ["s3"],
        },
        dedupe_key="owner:800",
    )
    await plugin.storage.enqueue_pending(
        "status_change",
        {"appid": 800, "name": "Elden Ring", "field": "exclude_reason", "old": 0, "new": 2},
        dedupe_key="status:800:exclude_reason",
    )

    with fake_message_chain():
        stats = await plugin._drain_pending_notifications()

    assert stats["success"] == 2
    texts = _pushed_texts(plugin.context.send_message)
    assert any("🔁 《Elden Ring》归属变动：新增义父 甲、乙 / 失去义父 丙" in t for t in texts)
    assert any(
        "【Steam 共享属性被去除】《Elden Ring》" in t and "该游戏已被移出家庭共享库" in t
        for t in texts
    )
    assert await plugin.storage.pending_count() == 0


async def test_price_drop_skeleton_dispatch(tmp_path):
    """(补充) price_drop kind 骨架分发：按约定文案推送降价提醒。"""
    plugin = _make_plugin(tmp_path, db_name="price.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    await plugin.storage.subscribe("group_1")
    await plugin.storage.enqueue_pending(
        "price_drop",
        {
            "appid": 900,
            "name": "Hades II",
            "old_price": 199,
            "new_price": 99,
            "discount_percent": 50,
        },
        dedupe_key="price:900",
    )
    with fake_message_chain():
        stats = await plugin._drain_pending_notifications()
    assert stats["success"] == 1
    texts = _pushed_texts(plugin.context.send_message)
    assert any("📉 降价提醒：《Hades II》 ¥199 → ¥99 (折扣 50%)" in t for t in texts)


async def test_large_removal_safety_valve(tmp_path):
    """(d) 大规模移出安全阀：removed 不入队、日志提示、基线照常更新。"""
    plugin = _make_plugin(tmp_path, db_name="valve.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    await plugin.storage.subscribe("group_1")

    # 基线 100 款，当前只剩 10 款 → 移出 90 > max(25, 15) → 触发安全阀
    prev_ids = list(range(1000, 1100))
    baseline = [
        SharedApp(appid=i, name=f"Game {i}", owner_steamids=["76561198000000001"]) for i in prev_ids
    ]
    await plugin.storage.save_snapshot(baseline)
    current = [
        SharedApp(appid=i, name=f"Game {i}", owner_steamids=["76561198000000001"])
        for i in range(1000, 1010)
    ]
    plugin.client.get_shared_library = AsyncMock(return_value=current)

    # 监控 enqueue_pending 调用，验证没有任何 removed 事件入队
    enqueue_kinds: list[str] = []
    orig_enqueue = plugin.storage.enqueue_pending

    async def spy_enqueue(kind, payload, dedupe_key):
        enqueue_kinds.append(kind)
        return await orig_enqueue(kind, payload, dedupe_key)

    plugin.storage.enqueue_pending = spy_enqueue

    with fake_message_chain():
        msg = await plugin._check_and_notify()

    assert msg.startswith("🛡️")  # 信息性提示：不得以 ❌/⚠️ 开头（否则分类器误判为同步失败）
    assert "大规模移出（90 款）" in msg
    assert "已跳过播报仅更新基线" in msg
    # 检查本身成功：受控消息协议标记应为成功（不触发无谓轮询退避 / 看板红标）
    assert plugin._last_check_success is True
    assert "removed" not in enqueue_kinds
    assert await plugin.storage.pending_count() == 0
    # 基线已更新为当前 10 款
    snapshot = await plugin.storage.get_latest_snapshot()
    assert len(snapshot) == 10


async def test_single_flight_lock_concurrent_once(tmp_path):
    """(e) 单飞锁：并发三次 _check_and_notify 仅执行一次 impl，结果复用。"""
    plugin = _make_plugin(tmp_path, db_name="lock.db")
    impl_calls = 0

    async def fake_impl(*args, **kwargs):
        nonlocal impl_calls
        impl_calls += 1
        await asyncio.sleep(0.05)  # 模拟同步耗时，确保其余并发调用在执行期间到达
        return "✅ 同步完成"

    with patch.object(SteamFamilyPlugin, "_check_and_notify_impl", fake_impl):
        results = await asyncio.gather(
            plugin._check_and_notify(),
            plugin._check_and_notify(),
            plugin._check_and_notify(),
        )

    assert impl_calls == 1
    assert results == ["✅ 同步完成"] * 3

    # 同步结束后再调用：应重新执行（新一轮检查）
    with patch.object(SteamFamilyPlugin, "_check_and_notify_impl", fake_impl):
        await plugin._check_and_notify()
    assert impl_calls == 2


async def test_details_none_still_broadcast(tmp_path):
    """(f) 免费过滤新语义：details=None（商店查询失败）视为未知 → 宁多勿漏照常播报。"""
    plugin = _make_plugin(tmp_path, db_name="unknown.db", broadcast_free_games=False)
    _offline_client(plugin)
    await plugin.storage.init_db()
    await plugin.storage.subscribe("group_1")

    baseline = [SharedApp(appid=100, name="Base", owner_steamids=["76561198000000001"])]
    await plugin.storage.save_snapshot(baseline)

    plugin.client.get_shared_library = AsyncMock(
        return_value=[
            SharedApp(appid=100, name="Base", owner_steamids=["76561198000000001"]),
            SharedApp(appid=500, name="Mystery Game", owner_steamids=["76561198000000001"]),
        ]
    )
    # 批量缺键 + 单发返回 None（商店查询失败的"未知"形态）
    plugin.client.get_app_details_batch = AsyncMock(return_value={})
    plugin.client.get_app_details = AsyncMock(return_value=None)

    send_count_before = len(_pushed_texts(plugin.context.send_message))
    with fake_message_chain():
        msg = await plugin._check_and_notify()

    texts = _pushed_texts(plugin.context.send_message)
    # details=None 仍照常播报（不得因查不到详情而吞掉通知），卡片字段用中性兜底
    assert any("Mystery Game" in t for t in texts)
    # 详情查询失败时价格不得虚构为 ¥ 0.00 / 免费，改用中性"价格未知"文案
    assert any("价格未知（商店查询失败）" in t for t in texts)
    assert not any("免费/已入库" in t for t in texts)
    assert len(texts) == send_count_before + 1
    assert await plugin.storage.pending_count() == 0
    assert "入队 1 条" in msg

    # 对照：确实拿到详情且明确免费（is_free）→ 按设置静默忽略，不推送
    plugin.client.get_shared_library = AsyncMock(
        return_value=[
            SharedApp(appid=100, name="Base", owner_steamids=["76561198000000001"]),
            SharedApp(appid=500, name="Mystery Game", owner_steamids=["76561198000000001"]),
            SharedApp(appid=600, name="Free Game F", owner_steamids=["76561198000000001"]),
        ]
    )
    plugin.client.get_app_details_batch = AsyncMock(
        return_value={
            600: AppDetails(
                appid=600, name="Free Game F", is_free=True, initial_price=0.0, final_price=0.0
            )
        }
    )
    send_count_before = len(_pushed_texts(plugin.context.send_message))
    with fake_message_chain():
        msg2 = await plugin._check_and_notify()
    # filtered 口径：免费游戏静默消解不计入 success，汇总消息与 stats 一致
    assert "1 条已静默消解（免费游戏/过期事件）" in msg2
    assert "成功发送 0 条" in msg2
    texts2 = _pushed_texts(plugin.context.send_message)
    assert len(texts2) == send_count_before  # 免费游戏未推送


async def test_push_failures_prune_dead_subscribers(tmp_path):
    """(g) 推送失败计入失败数；连续失败达 5 次的订阅者在管线收尾被 prune。"""
    plugin = _make_plugin(tmp_path, db_name="prune.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    await plugin.storage.subscribe("dead_target")
    await plugin.storage.subscribe("ok_target")

    async def send_side(target, chain):
        if target == "dead_target":
            raise RuntimeError("subscriber unreachable")
        return None

    plugin.context.send_message = AsyncMock(side_effect=send_side)

    # 6 条移出事件：dead_target 连续失败 6 次（>= 5），ok_target 全部成功
    with fake_message_chain():
        stats = await plugin._drain_pending_notifications()
    assert stats["success"] == 0  # 尚无事件，空跑不报错

    for i in range(6):
        await plugin.storage.enqueue_pending(
            "removed",
            {"appid": 900 + i, "name": f"Old Game {i}", "owner_steamids": ["76561198000000001"]},
            dedupe_key=f"removed:{900 + i}",
        )
    with fake_message_chain():
        stats = await plugin._drain_pending_notifications()

    # 事件全部处理完成（单条推送到死订阅者的失败只影响订阅者健康计数，不重试事件）
    assert stats["success"] == 6
    assert await plugin.storage.pending_count() == 0

    subscribers = await plugin.storage.get_subscribers()
    assert "dead_target" not in subscribers  # 达到失败上限，已被自动清理
    assert "ok_target" in subscribers

    # 清理后再次 drain：正常订阅者仍可收到推送，且不会重复处理老事件
    plugin.context.send_message = AsyncMock()
    with fake_message_chain():
        stats2 = await plugin._drain_pending_notifications()
    assert stats2["success"] == 0
    assert plugin.context.send_message.await_count == 0


async def test_drain_with_no_subscribers_preserves_queue(tmp_path, caplog):
    """(h) 订阅者为空时 drain 不消费事件：不 claim / 不 mark done，事件留给未来订阅者。"""
    plugin = _make_plugin(tmp_path, db_name="nosub.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    # 注意：不订阅任何会话
    await plugin.storage.enqueue_pending(
        "added",
        {"appid": 710, "name": "Late Game", "owner_steamids": ["76561198000000001"]},
        dedupe_key="added:710",
    )

    with caplog.at_level(logging.WARNING):
        stats = await plugin._drain_pending_notifications()

    assert stats == {"success": 0, "failed": 0, "filtered": 0, "abandoned": 0}
    # 事件原封不动保留在队列中（未被 claim / 消费）
    assert await plugin.storage.pending_count() == 1
    assert any("没有任何订阅者" in r.getMessage() for r in caplog.records)

    # 订阅者出现后：同一队列中的积压事件被正常补播
    await plugin.storage.subscribe("group_late")
    plugin.context.send_message = AsyncMock()
    with fake_message_chain():
        stats2 = await plugin._drain_pending_notifications()
    assert stats2["success"] == 1
    assert await plugin.storage.pending_count() == 0


async def test_unknown_kind_counted_as_filtered_and_log_truncated(tmp_path, caplog):
    """(i) 未知 kind：三态上的 filtered（不计 success）+ 日志 payload 截断 200 字。"""
    plugin = _make_plugin(tmp_path, db_name="unknown_kind.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    await plugin.storage.subscribe("group_1")
    long_junk = "A" * 1000
    await plugin.storage.enqueue_pending(
        "mystery_kind",
        {"appid": 1, "name": "X", "junk": long_junk},
        dedupe_key="mystery:1",
    )

    with fake_message_chain(), caplog.at_level(logging.WARNING):
        stats = await plugin._drain_pending_notifications()

    # 不计入 success、不重试、直接 mark done（防毒丸）
    assert stats["success"] == 0
    assert stats["filtered"] == 1
    assert await plugin.storage.pending_count() == 0

    warn_texts = [r.getMessage() for r in caplog.records if "未知类型" in r.getMessage()]
    assert warn_texts, "未知类型事件应有 warning 日志"
    assert all(long_junk not in t for t in warn_texts)
    assert all(len(t) < 1000 for t in warn_texts)  # 截断防刷屏


async def test_zero_priced_unreleased_game_still_broadcast(tmp_path):
    """(j) 免费过滤收紧：0/0 报价且 is_free=False（未发售/无报价）不按免费过滤，照常播报。"""
    plugin = _make_plugin(tmp_path, db_name="unreleased.db", broadcast_free_games=False)
    _offline_client(plugin)
    await plugin.storage.init_db()
    await plugin.storage.subscribe("group_1")

    await plugin.storage.save_snapshot(
        [
            SharedApp(appid=100, name="Base", owner_steamids=["76561198000000001"]),
            SharedApp(appid=700, name="Coming Soon", owner_steamids=["76561198000000001"]),
        ]
    )
    await plugin.storage.enqueue_pending(
        "added",
        {"appid": 700, "name": "Coming Soon", "owner_steamids": ["76561198000000001"]},
        dedupe_key="added:700",
    )
    # 商店详情存在且 is_free=False（未发售 / 暂无报价的 0/0 形态）
    plugin.client.get_app_details_batch = AsyncMock(return_value={})
    plugin.client.get_app_details = AsyncMock(
        return_value=AppDetails(
            appid=700, name="Coming Soon", is_free=False, initial_price=0.0, final_price=0.0
        )
    )

    with fake_message_chain():
        stats = await plugin._drain_pending_notifications()

    assert stats["success"] == 1
    assert stats["filtered"] == 0
    texts = _pushed_texts(plugin.context.send_message)
    assert any("Coming Soon" in t for t in texts)


async def test_stale_removed_event_resolved_by_snapshot(tmp_path):
    """(k) 过时事件对账：appid 已重新在库的 removed 事件静默消解，不再发矛盾播报。"""
    plugin = _make_plugin(tmp_path, db_name="stale_removed.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    await plugin.storage.subscribe("group_1")

    # 事件入队后、消费前，该游戏又回到了家庭库（removed 已过时）
    await plugin.storage.save_snapshot(
        [
            SharedApp(appid=100, name="Base", owner_steamids=["76561198000000001"]),
            SharedApp(appid=400, name="Back Game", owner_steamids=["76561198000000001"]),
        ]
    )
    await plugin.storage.enqueue_pending(
        "removed",
        {"appid": 400, "name": "Back Game", "owner_steamids": ["76561198000000001"]},
        dedupe_key="removed:400",
    )

    plugin.context.send_message = AsyncMock()
    with fake_message_chain():
        stats = await plugin._drain_pending_notifications()

    assert stats == {"success": 0, "failed": 0, "filtered": 1, "abandoned": 0}
    assert _pushed_texts(plugin.context.send_message) == []
    assert await plugin.storage.pending_count() == 0

    # 对照：真实移出（appid 不在最新快照中）照常播报
    await plugin.storage.save_snapshot(
        [SharedApp(appid=100, name="Base", owner_steamids=["76561198000000001"])]
    )
    await plugin.storage.enqueue_pending(
        "removed",
        {"appid": 400, "name": "Back Game", "owner_steamids": ["76561198000000001"]},
        dedupe_key="removed:400_replay",
    )
    with fake_message_chain():
        stats2 = await plugin._drain_pending_notifications()
    assert stats2["success"] == 1
    texts = _pushed_texts(plugin.context.send_message)
    assert any("400" in t and "移出" in t for t in texts)


async def test_stale_added_event_resolved_by_snapshot(tmp_path):
    """(l) 对称对账：最新快照中已不存在的 added 事件静默消解（入库后又移出的竞态）。"""
    plugin = _make_plugin(tmp_path, db_name="stale_added.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    await plugin.storage.subscribe("group_1")

    # 快照中已无 800（入库后又移出）：added 事件按过时事件消解
    await plugin.storage.save_snapshot(
        [SharedApp(appid=100, name="Base", owner_steamids=["76561198000000001"])]
    )
    await plugin.storage.enqueue_pending(
        "added",
        {"appid": 800, "name": "Gone Game", "owner_steamids": ["76561198000000001"]},
        dedupe_key="added:800",
    )

    with fake_message_chain():
        stats = await plugin._drain_pending_notifications()

    assert stats == {"success": 0, "failed": 0, "filtered": 1, "abandoned": 0}
    assert _pushed_texts(plugin.context.send_message) == []
    assert await plugin.storage.pending_count() == 0


async def test_single_flight_waiter_cancel_does_not_kill_shared_impl(tmp_path):
    """(m) 单飞壳 shield：某个等待者被取消（如 web 客户端断连）不连带取消共享 impl 任务。"""
    plugin = _make_plugin(tmp_path, db_name="shield.db")
    impl_finished = asyncio.Event()

    async def slow_impl(*args, **kwargs):
        try:
            await asyncio.sleep(0.3)
        finally:
            impl_finished.set()
        return "✅ 完成"

    with patch.object(SteamFamilyPlugin, "_check_and_notify_impl", slow_impl):
        first = asyncio.create_task(plugin._check_and_notify())
        await asyncio.sleep(0.05)  # first 已启动共享 impl 任务
        second = asyncio.create_task(plugin._check_and_notify())
        await asyncio.sleep(0.05)  # second 已进入 await shield(task)
        second.cancel()  # 模拟 web 客户端断连
        with pytest.raises(asyncio.CancelledError):
            await second
        # 共享 impl 未被连带取消：仍存活的等待者拿到完整结果
        assert await first == "✅ 完成"
        assert plugin._sync_task.done()
        assert not plugin._sync_task.cancelled()

    assert impl_finished.is_set()
