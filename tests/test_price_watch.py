"""降价监控巡检与 0.3.0 运营观测能力的离线单元测试。

覆盖：降价/仅折扣加深/免费缺键/old=0 边界判定、20 小时节流、未缓存游戏跳过、
单轮 120 款上限、单游戏异常不阻断整轮、429 熔断中止、自适应退避因子纯函数、
游玩统计接线（成功写库 / 失败不阻塞检查）、web 数据接口的 sync_info / recent_plays、
商店接口 429 熔断指数退避（连续冷却递增与 2xx 复位）。
"""


import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

# 复用既有测试的 astrbot mock 注入方式，保证独立运行时也能离线导入插件。
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
    sys.modules["astrbot.api.event"] = mock_event_module

if "astrbot.api.message_components" not in sys.modules:
    mock_comp_module = MagicMock()
    mock_comp_module.Plain = lambda text: text
    mock_comp_module.Image = MagicMock()
    sys.modules["astrbot.api.message_components"] = mock_comp_module

import pytest

from core.steam_api import AppDetails, SharedApp, SteamFamilyClient, SteamRateLimitError
from main import SteamFamilyPlugin, _compute_poll_interval_secs


@pytest.fixture(autouse=True)
def _isolate_plugin_data_dir(tmp_path, monkeypatch):
    """(P2) 所有用例构造插件前打桩 _get_db_path，杜绝测试在仓库 CWD 生成 data/ 生产库文件。"""
    monkeypatch.setattr("main._get_db_path", lambda: str(tmp_path / "engine.db"))


def _make_plugin(tmp_path, db_name="price_watch.db", **config_overrides):
    """构建完全离线的插件实例：假 context + 真 SQLite storage + 全 mock client。"""
    mock_context = MagicMock()
    cfg = {
        "access_token": "token",
        "steam_id": "76561198000000001",
        "broadcast_free_games": True,
    }
    cfg.update(config_overrides)
    mock_context.get_config.return_value = cfg
    mock_context.send_message = AsyncMock()
    with patch.object(SteamFamilyPlugin, "_init_and_start_polling"):
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
    plugin.client.get_playtime_summary = AsyncMock(return_value=[])
    plugin.client.get_price_overview_batch = AsyncMock(return_value={})


async def _seed_library(plugin, snapshot_ids, cached_specs):
    """建立最新快照与 apps_cache 价格记录。"""
    apps = [
        SharedApp(appid=aid, name=f"Game {aid}", owner_steamids=["76561198000000001"])
        for aid in snapshot_ids
    ]
    await plugin.storage.save_snapshot(apps)
    for spec in cached_specs:
        await plugin.storage.cache_app_details(AppDetails(**spec))


async def _pending_price_events(plugin) -> list[tuple[str, dict]]:
    """读取队列中的 price_drop 事件（dedupe_key, payload）。"""
    async with plugin.storage._get_conn() as db:
        async with db.execute(
            "SELECT dedupe_key, payload_json FROM pending_notifications WHERE kind = 'price_drop' ORDER BY id"
        ) as cursor:
            rows = await cursor.fetchall()
    import json

    return [(r[0], json.loads(r[1])) for r in rows]


# ---------------------------------------------------------------------------
# 降价判定逻辑
# ---------------------------------------------------------------------------


async def test_price_drop_enqueues_event_and_refreshes_cache(tmp_path):
    """真实降价：old.final > 0 且 new_final < old.final → 入队 price_drop 并刷新缓存价格。"""
    plugin = _make_plugin(tmp_path, db_name="drop.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    await _seed_library(
        plugin,
        [100],
        [{"appid": 100, "name": "Hades II", "initial_price": 199.0, "final_price": 199.0}],
    )
    plugin.client.get_price_overview_batch = AsyncMock(return_value={100: (199.0, 99.0, 50)})

    await plugin._price_watch_sweep()

    events = await _pending_price_events(plugin)
    assert len(events) == 1
    dedupe_key, payload = events[0]
    # dedupe_key 含新价（分），payload 键名与 _notify_price_drop_event 解析逻辑匹配
    assert dedupe_key == "price_drop:100:9900"
    assert payload["appid"] == 100
    assert payload["name"] == "Hades II"
    assert payload["old_price"] == 199.0
    assert payload["new_price"] == 99.0
    assert payload["discount_percent"] == 50
    # 缓存价格已刷新为新价（下一轮不会重复触发）
    refreshed = await plugin.storage.get_cached_app(100)
    assert refreshed.final_price == 99.0
    assert refreshed.discount_percent == 50


async def test_discount_increase_only_triggers_event(tmp_path):
    """仅折扣加深（价格未变）同样判定为降价事件。"""
    plugin = _make_plugin(tmp_path, db_name="disc.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    await _seed_library(
        plugin,
        [300],
        [{"appid": 300, "name": "Old Game", "initial_price": 59.0, "final_price": 59.0}],
    )
    plugin.client.get_price_overview_batch = AsyncMock(return_value={300: (59.0, 59.0, 40)})

    await plugin._price_watch_sweep()

    events = await _pending_price_events(plugin)
    assert len(events) == 1
    dedupe_key, payload = events[0]
    assert dedupe_key == "price_drop:300:5900"
    assert payload["old_price"] == 59.0
    assert payload["new_price"] == 59.0
    assert payload["discount_percent"] == 40


async def test_free_game_missing_key_skipped(tmp_path):
    """免费游戏（批量结果 data=[] 缺键）：不触发事件、不覆盖缓存价格。"""
    plugin = _make_plugin(tmp_path, db_name="free.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    await _seed_library(
        plugin,
        [400],
        [{"appid": 400, "name": "Paid Game", "initial_price": 99.0, "final_price": 99.0}],
    )
    plugin.client.get_price_overview_batch = AsyncMock(return_value={})  # 400 缺键

    await plugin._price_watch_sweep()

    assert await plugin.storage.pending_count() == 0
    cached = await plugin.storage.get_cached_app(400)
    assert cached.final_price == 99.0  # 缓存价格保持不变


async def test_old_price_zero_boundary_no_event_but_refresh(tmp_path):
    """old.final_price=0 边界（从未记录过正价）：新价再高也不算降价，但缓存照常刷新。"""
    plugin = _make_plugin(tmp_path, db_name="zero.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    await _seed_library(
        plugin,
        [500],
        [{"appid": 500, "name": "Free Origin", "initial_price": 0.0, "final_price": 0.0}],
    )
    plugin.client.get_price_overview_batch = AsyncMock(return_value={500: (298.0, 198.0, 33)})

    await plugin._price_watch_sweep()

    assert await plugin.storage.pending_count() == 0  # old=0 不是降价
    cached = await plugin.storage.get_cached_app(500)
    assert cached.final_price == 198.0  # 价格观测照常回写


async def test_no_price_change_no_event_but_refresh(tmp_path):
    """无变动：不入队事件，但缓存价格被 update_prices 刷新。"""
    plugin = _make_plugin(tmp_path, db_name="same.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    await _seed_library(
        plugin,
        [600],
        [{"appid": 600, "name": "Stable", "initial_price": 100.0, "final_price": 100.0, "discount_percent": 10}],
    )
    plugin.client.get_price_overview_batch = AsyncMock(return_value={600: (100.0, 100.0, 10)})

    await plugin._price_watch_sweep()

    assert await plugin.storage.pending_count() == 0
    cached = await plugin.storage.get_cached_app(600)
    assert cached.final_price == 100.0
    assert cached.discount_percent == 10


# ---------------------------------------------------------------------------
# 节流 / 跳过与上限
# ---------------------------------------------------------------------------


async def test_price_watch_throttled_within_20_hours(tmp_path):
    """距上次巡检不足 20 小时：直接 return，不发批量请求、不重复盖时间戳。"""
    plugin = _make_plugin(tmp_path, db_name="throttle.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    recent = int(time.time()) - 19 * 3600
    await plugin.storage.set_sync_state("price_watch_last_ts", str(recent))

    await plugin._price_watch_sweep()

    plugin.client.get_price_overview_batch.assert_not_called()
    # 时间戳保持原值未被覆盖
    assert await plugin.storage.get_sync_state("price_watch_last_ts") == str(recent)


async def test_price_watch_disabled_by_config(tmp_path):
    """enable_price_watch=False：直接 return，完全不触达客户端与 kv。"""
    plugin = _make_plugin(tmp_path, db_name="disabled.db", enable_price_watch=False)
    _offline_client(plugin)
    await plugin.storage.init_db()

    await plugin._price_watch_sweep()

    plugin.client.get_price_overview_batch.assert_not_called()
    assert await plugin.storage.get_sync_state("price_watch_last_ts") is None


async def test_uncached_games_skipped(tmp_path):
    """从未缓存过详情的游戏直接跳过，不为未知游戏打商店接口。"""
    plugin = _make_plugin(tmp_path, db_name="uncached.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    await _seed_library(
        plugin,
        [700, 701, 702],  # 快照 3 款，只缓存了 701
        [{"appid": 701, "name": "Cached Only", "initial_price": 66.0, "final_price": 55.0}],
    )
    plugin.client.get_price_overview_batch = AsyncMock(return_value={701: (66.0, 44.0, 20)})

    await plugin._price_watch_sweep()

    # 批量请求只包含已缓存的那一款
    plugin.client.get_price_overview_batch.assert_awaited_once_with([701])
    events = await _pending_price_events(plugin)
    assert len(events) == 1  # 701 的降价正常触发


async def test_watch_ids_capped_at_120_per_round(tmp_path):
    """单轮最多巡检 120 款，多余的留待下一轮。"""
    plugin = _make_plugin(tmp_path, db_name="cap.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    ids = list(range(1000, 1125))  # 125 款全部有缓存记录
    await _seed_library(
        plugin,
        ids,
        [
            {
                "appid": aid,
                "name": f"Game {aid}",
                "initial_price": 100.0,
                "final_price": 100.0,
            }
            for aid in ids
        ],
    )
    plugin.client.get_price_overview_batch = AsyncMock(return_value={})

    await plugin._price_watch_sweep()

    args, _ = plugin.client.get_price_overview_batch.await_args
    batch_ids = args[0]
    assert len(batch_ids) == 120
    assert set(batch_ids) <= set(ids)


async def test_watch_round_resumes_next_round(tmp_path):
    """时间戳以巡检开头落库：裁剪窗口外（>20 小时前）的旧时间戳允许新一轮扫描。"""
    plugin = _make_plugin(tmp_path, db_name="resume.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    await _seed_library(
        plugin,
        [800],
        [{"appid": 800, "name": "Old Price", "initial_price": 129.0, "final_price": 129.0}],
    )
    await plugin.storage.set_sync_state("price_watch_last_ts", str(int(time.time()) - 21 * 3600))
    plugin.client.get_price_overview_batch = AsyncMock(return_value={800: (129.0, 129.0, 0)})

    await plugin._price_watch_sweep()

    plugin.client.get_price_overview_batch.assert_awaited_once_with([800])
    # 巡检后时间戳已刷新到当前时间
    new_ts = int(await plugin.storage.get_sync_state("price_watch_last_ts"))
    assert abs(new_ts - int(time.time())) < 60


# ---------------------------------------------------------------------------
# 异常安全
# ---------------------------------------------------------------------------


async def test_single_game_exception_does_not_break_round(tmp_path):
    """单个游戏处理异常（如写缓存失败）不影响同轮其他游戏。"""
    plugin = _make_plugin(tmp_path, db_name="isolation.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    await _seed_library(
        plugin,
        [900, 901],
        [
            {"appid": 900, "name": "Boom", "initial_price": 80.0, "final_price": 80.0},
            {"appid": 901, "name": "Fine", "initial_price": 60.0, "final_price": 60.0},
        ],
    )
    plugin.client.get_price_overview_batch = AsyncMock(
        return_value={900: (80.0, 40.0, 50), 901: (60.0, 60.0, 0)}
    )

    orig_update = plugin.storage.update_prices

    async def flaky_update(appid, initial, final, discount):
        if appid == 900:
            raise RuntimeError("db boom")
        return await orig_update(appid, initial, final, discount)

    plugin.storage.update_prices = flaky_update

    # 不应抛出异常（整轮吞掉单游戏错误）
    await plugin._price_watch_sweep()

    # 901 的价格照常刷新；900 的降价事件仍已入队（update 在入队之后才执行失败）
    fine = await plugin.storage.get_cached_app(901)
    assert fine.final_price == 60.0
    events = await _pending_price_events(plugin)
    assert [e[1]["appid"] for e in events] == [900]


async def test_rate_limited_batch_aborts_sweep_silently(tmp_path):
    """批量价格请求抛 429 熔断：巡检静默中止，不向调用方抛错。"""
    plugin = _make_plugin(tmp_path, db_name="ration.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    await _seed_library(
        plugin,
        [950],
        [{"appid": 950, "name": "RL", "initial_price": 70.0, "final_price": 70.0}],
    )
    plugin.client.get_price_overview_batch = AsyncMock(
        side_effect=SteamRateLimitError("429 breaker")
    )

    await plugin._price_watch_sweep()  # 不应抛异常

    assert await plugin.storage.pending_count() == 0
    cached = await plugin.storage.get_cached_app(950)
    assert cached.final_price == 70.0  # 未被误改


# ---------------------------------------------------------------------------
# 自适应退避因子（纯函数）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base_minutes,failures,expected",
    [
        (15, 0, 900),  # 成功：基础间隔
        (15, 1, 1800),  # 失败 1 次：2 倍（min(4, 2)=2）
        (15, 2, 3600),  # 失败 2 次：4 倍（min(4, 4)=4）
        (15, 3, 3600),  # 失败 3 次：仍 4 倍（min(4, 8)=4，因子封顶）
        (15, 4, 3600),  # 继续失败封顶 4 倍
        (15, 99, 3600),  # 更多失败仍封顶 4 倍
        (1, 0, 60),  # 过小配置被抬升到下限 60 秒
        (0, 0, 60),
        (30, 0, 1800),
        (30, 1, 3600),
    ],
)
def test_compute_poll_interval_secs(base_minutes, failures, expected):
    assert _compute_poll_interval_secs(base_minutes, failures) == expected


# ---------------------------------------------------------------------------
# 游玩统计接线（GetPlaytimeSummary → playtime_stats）
# ---------------------------------------------------------------------------


async def test_playtime_wiring_success_persists_stats(tmp_path):
    """检查成功拿到 current_apps 后调用 get_playtime_summary 并写入 playtime_stats。"""
    plugin = _make_plugin(tmp_path, db_name="playtime.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    apps = [SharedApp(appid=100, name="Base", owner_steamids=["76561198000000001"])]
    await plugin.storage.save_snapshot(apps)
    plugin.client.get_shared_library = AsyncMock(return_value=apps)
    # 真实 IFamilyGroupsService/GetPlaytimeSummary entry 键形状（camelCase，时长单位已是秒）：
    # steamid / appid / firstPlayed / latestPlayed / secondsPlayed。
    # 该接口并无 rt_playtime 字段（那是家庭库共享列表 SharedApp 的字段），
    # 且 secondsPlayed=1800 直接入库为秒，绝不能再被"分钟 ×60"之类换算放大。
    plugin.client.get_playtime_summary = AsyncMock(
        return_value=[
            {
                "steamid": "76561198000000001",
                "appid": 100,
                "firstPlayed": 1758000000,
                "latestPlayed": 1759000000,
                "secondsPlayed": 1800,
            }
        ]
    )

    msg = await plugin._check_and_notify()

    assert msg.startswith("✅")
    plugin.client.get_playtime_summary.assert_awaited_once_with("group_id_1", "token")
    plays = await plugin.storage.get_recent_plays(8)
    assert len(plays) == 1
    assert plays[0]["appid"] == 100
    # 直接入库为秒：1800 进 1800 出，不被任何 ×60 换算放大
    assert plays[0]["seconds_played"] == 1800.0
    # latestPlayed 作为排序键入库（未被 rt_/firstPlayed 之类旧键顶替）
    assert plays[0]["rt_last_played"] == 1759000000


async def test_playtime_wiring_failure_does_not_block_check(tmp_path):
    """游玩统计接口抛错：仅记 debug，检查主流程照常成功返回。"""
    plugin = _make_plugin(tmp_path, db_name="playtime_fail.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    apps = [SharedApp(appid=100, name="Base", owner_steamids=["76561198000000001"])]
    await plugin.storage.save_snapshot(apps)
    plugin.client.get_shared_library = AsyncMock(return_value=apps)
    plugin.client.get_playtime_summary = AsyncMock(side_effect=RuntimeError("api boom"))

    msg = await plugin._check_and_notify()

    assert plugin._last_check_success is True
    assert msg.startswith("✅")  # 检查结果不被游玩统计失败拖垮
    assert await plugin.storage.get_recent_plays(8) == []


async def test_check_impl_writes_sync_state_and_schedules_sweep(tmp_path):
    """impl 成功收尾写入 sync_* 观测键，并异步调度降价巡检（落 price_watch 时间戳）。"""
    plugin = _make_plugin(tmp_path, db_name="sync_state.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    apps = [SharedApp(appid=100, name="Base", owner_steamids=["76561198000000001"])]
    await plugin.storage.save_snapshot(apps)
    plugin.client.get_shared_library = AsyncMock(return_value=apps)

    await plugin._check_and_notify()

    info = await plugin.storage.get_sync_state_info()
    assert info["last_success_ts"] > 0
    assert info["last_duration_ms"] >= 0
    assert await plugin.storage.get_sync_state("last_status_failed") == "0"
    assert "家庭库检查完成" in (await plugin.storage.get_sync_state("last_message") or "")
    # 确定性等待后台 price watch 首轮完成：不押注调度时机——时间戳未落库就主动驱动一轮巡检
    # （sweep 自身有 20h 节流，重复驱动幂等；后台任务与直调两条路径都会写同一 kv 键）。
    deadline = time.monotonic() + 5.0
    ts = await plugin.storage.get_sync_state("price_watch_last_ts")
    while (ts is None or int(ts) <= 0) and time.monotonic() < deadline:
        await plugin._price_watch_sweep()
        ts = await plugin.storage.get_sync_state("price_watch_last_ts")
    assert ts is not None and int(ts) > 0


async def test_check_impl_marks_failure_state(tmp_path):
    """检查失败（⚠️ 开头）：status_failed=1，不覆盖 sync_last_success_ts。"""
    plugin = _make_plugin(tmp_path, db_name="sync_fail.db")
    _offline_client(plugin)
    await plugin.storage.init_db()
    plugin.client.get_shared_library = AsyncMock(return_value=[])  # 空库 → ⚠️
    plugin.config.access_token = "token"

    msg = await plugin._check_and_notify()

    assert msg.startswith("⚠️")
    assert plugin._last_check_success is False
    assert await plugin.storage.get_sync_state("last_status_failed") == "1"
    assert await plugin.storage.get_sync_state("last_success_ts") is None
    # 失败原因被截断留痕（<=200 字）
    stored_msg = await plugin.storage.get_sync_state("last_message")
    assert stored_msg and len(stored_msg) <= 200 and "共享游戏列表为空" in stored_msg


# ---------------------------------------------------------------------------
# Web 数据接口：sync_info / recent_plays
# ---------------------------------------------------------------------------


async def test_web_api_get_data_includes_sync_info_and_recent_plays(tmp_path):
    """web_api_get_data 返回 sync_info（含 last_message/failed）与 recent_plays。"""
    plugin = _make_plugin(tmp_path, db_name="web.db")
    _offline_client(plugin)
    await plugin.storage.init_db()

    data = (await plugin.web_api_get_data())["data"]

    assert "sync_info" in data
    assert data["sync_info"]["failed"] is False  # 从未执行时不误报失败
    assert "recent_plays" in data
    assert isinstance(data["recent_plays"], list)


# ---------------------------------------------------------------------------
# 429 熔断指数退避（core/steam_api.py 的 SteamFamilyClient）
# 说明：熔断逻辑归属 core/steam_api.py，而 tests/test_steam_api.py 由并行代理
# 维护，故退避行为的单元测试统一收敛在本文件。用 fake 时钟替换模块内
# time.time 并把限流器间隔清零，让测试免等真实 1.6 秒间隔与冷却窗。
# ---------------------------------------------------------------------------


class _FakeSteamClock:
    """可控 fake 时钟：patch 到 core.steam_api.time 上，替代 time.time()。"""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _steam_resp(status_code: int, json_data: dict | None = None):
    """构造与 httpx 响应同形状的 mock（本节 429 熔断测试专用）。"""
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


@pytest.mark.asyncio
async def test_consecutive_429_escalates_cooldown_and_2xx_resets():
    """连续 429：冷却 60 → 120 → 240 → 300（封顶）；评价接口 2xx 归零后重回 60 秒。"""
    client = SteamFamilyClient()
    client._store_limiter.min_interval = 0.0  # 不等 1.6 秒限流间隔
    clock = _FakeSteamClock()
    with patch("core.steam_api.time", clock):
        # 连续 5 次真实 429（每次把 fake 时钟快进到冷却结束，模拟等冷却后再次触发）
        for call_no, expected_cooldown in enumerate((60, 120, 240, 300, 300), start=1):
            with patch.object(client._client, "get", return_value=_steam_resp(429, {})):
                assert await client.get_review_summary(730) is None
            assert client._consecutive_429_count == call_no
            assert client._rate_limited_until == pytest.approx(clock.now + expected_cooldown)
            assert client.is_rate_limited is True
            clock.advance(client._rate_limited_until - clock.now + 1.0)

        # 快进到熔断结束后，评价接口 2xx 成功 → 连续 429 计数归零
        review_payload = {
            "success": 1,
            "query_summary": {
                "review_score_desc": "好评如潮",
                "total_positive": 96,
                "total_reviews": 100,
            },
        }
        with patch.object(client._client, "get", return_value=_steam_resp(200, review_payload)):
            assert await client.get_review_summary(1086940) == (96, "好评如潮")
        assert client._consecutive_429_count == 0

        # 复位后的下一次 429 从基础 60 秒重新起步（不残留历史封顶冷却）
        with patch.object(client._client, "get", return_value=_steam_resp(429, {})):
            assert await client.get_review_summary(730) is None
        assert client._consecutive_429_count == 1
        assert client._rate_limited_until == pytest.approx(clock.now + 60)


@pytest.mark.asyncio
async def test_price_batch_429_escalates_and_breaker_blocks_requests():
    """get_price_overview_batch 429：冷却逐次递增且抛熔断错误；冷却期内零请求。"""
    client = SteamFamilyClient()
    client._store_limiter.min_interval = 0.0
    clock = _FakeSteamClock()
    with patch("core.steam_api.time", clock), patch.object(
        client._client, "get", return_value=_steam_resp(429, {})
    ):
        with pytest.raises(SteamRateLimitError):
            await client.get_price_overview_batch([730])
        assert client._consecutive_429_count == 1
        assert client._rate_limited_until == pytest.approx(clock.now + 60)

        clock.advance(61.0)  # 快进到冷却结束
        with pytest.raises(SteamRateLimitError):
            await client.get_price_overview_batch([731])
        assert client._consecutive_429_count == 2
        assert client._rate_limited_until == pytest.approx(clock.now + 120)

        # 冷却期内入口即熔断：静默返回空 dict，不发任何网络请求（计数不变）
        net_guard = AsyncMock(side_effect=AssertionError("冷却期内不应发起网络请求"))
        with patch.object(client._client, "get", net_guard):
            assert await client.get_price_overview_batch([732]) == {}
        assert net_guard.await_count == 0
        assert client._consecutive_429_count == 2


@pytest.mark.asyncio
async def test_family_group_2xx_resets_429_streak():
    """商店接口 429 计数后，家庭组接口 2xx 成功同样作为放行信号归零计数。"""
    client = SteamFamilyClient()
    client._store_limiter.min_interval = 0.0
    clock = _FakeSteamClock()
    with patch("core.steam_api.time", clock):
        with patch.object(client._client, "get", return_value=_steam_resp(429, {})):
            with pytest.raises(SteamRateLimitError):
                await client.get_app_details(730)
        assert client._consecutive_429_count == 1
        assert client._rate_limited_until == pytest.approx(clock.now + 60)

        # 家庭组接口（GetFamilyGroupForUser）2xx 成功 → 连续 429 计数归零
        fg_resp = _steam_resp(200, {"response": {"family_groupid": "12345"}})
        with patch.object(client._client, "get", return_value=fg_resp):
            assert await client.get_family_group_id("token", "sid") == "12345"
        assert client._consecutive_429_count == 0
