import logging
import time

import aiosqlite
import pytest

from core.steam_api import AppDetails, SharedApp
from core.storage import FamilyStorage
from tests.mock_data import MOCK_PLAYTIME_SUMMARY_RESPONSE


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


async def _make_storage(tmp_path) -> FamilyStorage:
    """创建带临时数据库的存储实例（与现有测试一致的 tmp_path 风格，避免并发冲突）。"""
    storage = FamilyStorage(str(tmp_path / "persist.db"))
    await storage.init_db()
    return storage


async def _read_pending_row(storage: FamilyStorage, nid: int):
    """直接读库校验队列内部字段（retries / next_attempt_at）。"""
    async with aiosqlite.connect(storage.db_path) as db:
        async with db.execute(
            "SELECT retries, next_attempt_at FROM pending_notifications WHERE id = ?", (nid,)
        ) as cursor:
            return await cursor.fetchone()


# ---------------------------------------------------------------------------
# 待推送通知队列
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pending_notifications_dedupe_and_claim(tmp_path):
    storage = await _make_storage(tmp_path)

    # 空队列
    assert await storage.pending_count() == 0
    assert await storage.claim_due_pending() == []

    # 同 dedupe_key 重复入队：更新 payload，不产生新行
    await storage.enqueue_pending("added", {"appid": 1, "v": 1}, "added:1")
    await storage.enqueue_pending("added", {"appid": 1, "v": 2}, "added:1")
    await storage.enqueue_pending("removed", {"appid": 2}, "removed:2")
    assert await storage.pending_count() == 2

    # 只领取到期行，按 id 升序，limit 生效
    due = await storage.claim_due_pending(limit=1)
    assert len(due) == 1
    first = due[0]
    assert first["kind"] == "added"
    assert first["payload"] == {"appid": 1, "v": 2}
    assert first["retries"] == 0

    # 失败一次进入退避后不可立即重复领取（未到期）
    nid = first["id"]
    assert await storage.bump_pending_retry(nid) is True
    still = await storage.claim_due_pending()
    assert [d["kind"] for d in still] == ["removed"]

    # 退避期间同 dedupe_key 再次入队：payload 更新、retries 保留、next_attempt_at 归零
    await storage.enqueue_pending("added", {"appid": 1, "v": 3}, "added:1")
    redue = await storage.claim_due_pending()
    by_app = {d["payload"]["appid"]: d for d in redue}
    assert set(by_app) == {1, 2}
    assert by_app[1]["id"] == nid
    assert by_app[1]["payload"] == {"appid": 1, "v": 3}
    assert by_app[1]["retries"] == 1  # 保留原 retries

    # 标记完成即删除
    await storage.mark_pending_done(nid)
    assert await storage.pending_count() == 1
    await storage.mark_pending_done(by_app[2]["id"])
    assert await storage.pending_count() == 0


@pytest.mark.asyncio
async def test_pending_notifications_retry_backoff_and_cap(tmp_path):
    storage = await _make_storage(tmp_path)
    await storage.enqueue_pending("price_drop", {"appid": 7}, "price:7")
    batch = await storage.claim_due_pending()
    assert len(batch) == 1
    nid = batch[0]["id"]

    # 第一次失败：retries=1，next_attempt_at = now + 300 * 2**1 = 600 秒
    assert await storage.bump_pending_retry(nid) is True
    retries, next_at = await _read_pending_row(storage, nid)
    assert retries == 1
    assert next_at == pytest.approx(time.time() + 600, abs=10)
    assert await storage.claim_due_pending() == []  # 未到期不可领取

    # 指数退避 300 * 2**retries，最长 3600 秒封顶
    for expect_retries, delay in ((2, 1200), (3, 2400), (4, 3600)):
        assert await storage.bump_pending_retry(nid) is True
        retries, next_at = await _read_pending_row(storage, nid)
        assert retries == expect_retries
        assert next_at == pytest.approx(time.time() + delay, abs=10)

    # 达到重试上限（默认 5 次）：删除该行并返回 False
    assert await storage.bump_pending_retry(nid) is False
    assert await _read_pending_row(storage, nid) is None
    assert await storage.pending_count() == 0

    # 不存在的行：直接返回 False
    assert await storage.bump_pending_retry(999999) is False


# ---------------------------------------------------------------------------
# update_prices 三态 rowcount 语义 / mark_pending_done 幂等
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_update_prices_three_state_rowcount_semantics(tmp_path):
    """存在异值/存在同值/不存在 三种情况下 update_prices 的返回值与落库结果。"""
    storage = await _make_storage(tmp_path)
    await storage.cache_app_details(
        AppDetails(appid=1, name="Game 1", initial_price=100.0, final_price=100.0, discount_percent=0)
    )

    # 存在且新值不同 → True，且三字段被覆盖
    assert await storage.update_prices(1, 100.0, 80.0, 20) is True
    cached = await storage.get_cached_app(1)
    assert (cached.initial_price, cached.final_price, cached.discount_percent) == (100.0, 80.0, 20)

    # 存在但写入同值（幂等刷新）→ SQLite rowcount 仍按命中行计数 → True，
    # 不得把同值刷新误报为"未缓存"
    assert await storage.update_prices(1, 100.0, 80.0, 20) is True
    cached = await storage.get_cached_app(1)
    assert (cached.final_price, cached.discount_percent) == (80.0, 20)

    # 从未缓存过的游戏（无匹配行）→ rowcount=0 → False，调用方据此跳过
    assert await storage.update_prices(999, 1.0, 1.0, 0) is False
    assert await storage.get_cached_app(999) is None


@pytest.mark.asyncio
async def test_mark_pending_done_idempotent_for_missing_id(tmp_path):
    """mark_pending_done 对已删除/不存在的 id 重复调用：不抛错、不影响队列（幂等）。"""
    storage = await _make_storage(tmp_path)
    await storage.enqueue_pending("added", {"appid": 1}, "added:1")
    nid = (await storage.claim_due_pending())[0]["id"]

    await storage.mark_pending_done(nid)
    assert await storage.pending_count() == 0

    # 再次标记同一 id / 标记从未存在的 id：均安静的 no-op
    await storage.mark_pending_done(nid)
    await storage.mark_pending_done(999999)
    assert await storage.pending_count() == 0


# ---------------------------------------------------------------------------
# 快照修剪
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_prune_snapshots(tmp_path):
    storage = await _make_storage(tmp_path)

    # 空表不报错
    assert await storage.prune_snapshots(keep_min=3, keep_days=30) == 0

    apps = [SharedApp(appid=500, name="Game 500", owner_steamids=["76561198000000001"])]
    for _ in range(5):
        await storage.save_snapshot(apps)

    # 将最老的 2 个快照回溯到 40 天前
    async with aiosqlite.connect(storage.db_path) as db:
        await db.execute(
            "UPDATE library_snapshots SET created_at = datetime('now', '-40 days') "
            "WHERE id <= (SELECT MIN(id) + 1 FROM library_snapshots)"
        )
        await db.commit()

    # 最新 keep_min=3 条永远保留；更早且超过 keep_days=30 天的老快照被删除
    assert await storage.prune_snapshots(keep_min=3, keep_days=30) == 2
    async with aiosqlite.connect(storage.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM library_snapshots") as cursor:
            assert (await cursor.fetchone())[0] == 3
    assert await storage.get_latest_snapshot() is not None

    # 更早的行（最新 keep_min 之外）但未超过 keep_days 时不会被误删
    await storage.save_snapshot(apps)
    await storage.save_snapshot(apps)
    assert await storage.prune_snapshots(keep_min=3, keep_days=30) == 0
    async with aiosqlite.connect(storage.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM library_snapshots") as cursor:
            assert (await cursor.fetchone())[0] == 5


# ---------------------------------------------------------------------------
# 同步状态观测
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sync_state(tmp_path):
    storage = await _make_storage(tmp_path)

    # 键缺失时的缺省值
    assert await storage.get_sync_state("last_success_ts") is None
    assert await storage.get_sync_state_info() == {
        "last_success_ts": 0.0,
        "last_duration_ms": 0,
        "last_success_ago_minutes": 0.0,
    }

    await storage.set_sync_state("last_success_ts", "1700000000")
    await storage.set_sync_state("last_duration_ms", "1234")
    await storage.set_sync_state("last_message", "ok")

    assert await storage.get_sync_state("last_success_ts") == "1700000000"
    # 带 sync_ 前缀读取等价（同一把 kv 薄封装）
    assert await storage.get_sync_state("sync_last_message") == "ok"

    info = await storage.get_sync_state_info()
    assert info["last_success_ts"] == 1700000000.0
    assert info["last_duration_ms"] == 1234
    assert info["last_success_ago_minutes"] > 0

    # 非法值回退缺省，不抛异常
    await storage.set_sync_state("last_duration_ms", "not-a-number")
    assert (await storage.get_sync_state_info())["last_duration_ms"] == 0


# ---------------------------------------------------------------------------
# 推送失败计数与死订阅清理
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_push_failures_and_dead_subscribers(tmp_path):
    storage = await _make_storage(tmp_path)

    # 累加计数
    assert await storage.record_push_failure("t1") == 1
    assert await storage.record_push_failure("t1") == 2
    assert await storage.record_push_failure("t2") == 1

    # last_fail_at 被记录为时间戳
    async with aiosqlite.connect(storage.db_path) as db:
        async with db.execute(
            "SELECT last_fail_at FROM push_failures WHERE target_id = 't1'"
        ) as cursor:
            last_fail_at = (await cursor.fetchone())[0]
    assert last_fail_at is not None and last_fail_at > 0

    # 重置后从 0 重新计数
    await storage.reset_push_failures("t2")
    assert await storage.record_push_failure("t2") == 1

    # 未达阈值时不清理任何订阅
    await storage.subscribe("t1")
    await storage.subscribe("t2")
    await storage.subscribe("t3")
    assert await storage.prune_dead_subscribers(max_failures=5) == []
    assert len(await storage.get_subscribers()) == 3

    # t1 失败达到阈值（5 次）→ 订阅与计数一并移除
    for _ in range(3):  # 2 + 3 = 5
        await storage.record_push_failure("t1")
    assert await storage.prune_dead_subscribers(max_failures=5) == ["t1"]
    assert "t1" not in await storage.get_subscribers()
    assert set(await storage.get_subscribers()) == {"t2", "t3"}
    # 失败计数也被清掉，重新从 1 开始
    assert await storage.record_push_failure("t1") == 1


# ---------------------------------------------------------------------------
# 游玩时长统计（真实 GetPlaytimeSummary 键形状：steamid/appid/firstPlayed/latestPlayed/secondsPlayed）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_playtime_stats_refresh_and_recent_plays(tmp_path):
    storage = await _make_storage(tmp_path)

    # 空列表直接返回 0；缺 steamid/appid 的脏数据被跳过
    assert await storage.refresh_playtime_stats([]) == 0
    assert await storage.refresh_playtime_stats([{"appid": 1}, {"steamid": "x"}]) == 0

    # 真实接口形状（camelCase，值单位为秒），取自 tests/mock_data.py
    entries = [dict(e) for e in MOCK_PLAYTIME_SUMMARY_RESPONSE["response"]["entries"]]
    # 别名容错：snake_case 的 latest_played / 旧字段 rtLastPlayed
    entries.append(
        {"steamid": "76561198000000001", "appid": 500, "latest_played": 1790000400, "seconds_played": 60}
    )
    entries.append(
        {"steamid": "76561198000000002", "appid": 500, "rtLastPlayed": 1790000500, "secondsPlayed": 30}
    )
    assert await storage.refresh_playtime_stats(entries) == 5

    # 联表所需的成员资料与游戏详情缓存
    await storage.save_member_profiles(
        {
            "76561198000000001": {
                "steamid": "76561198000000001",
                "persona_name": "义父张三",
                "avatar_url": "https://avatars.steamstatic.com/test_avatar.jpg",
            }
        }
    )
    await storage.cache_app_details_batch(
        [
            AppDetails(
                appid=1086940,
                name="Baldur's Gate 3",
                chinese_name="博德之门3",
                header_image="https://img/b.jpg",
            ),
            AppDetails(
                appid=1245620,
                name="ELDEN RING",
                chinese_name="艾尔登法环",
                header_image="https://img/e.jpg",
            ),
        ]
    )

    recent = await storage.get_recent_plays()
    assert len(recent) == 5
    # rt_last_played 全部成功入库（无 latestPlayed 别名缺失导致的 0）
    assert all(r["rt_last_played"] != 0 for r in recent)
    # 按 rt_last_played 严格倒序
    rts = [r["rt_last_played"] for r in recent]
    assert rts == sorted(rts, reverse=True)

    # 旧字段别名 rtLastPlayed 行
    assert recent[0]["steamid"] == "76561198000000002"
    assert recent[0]["appid"] == 500
    assert recent[0]["rt_last_played"] == 1790000500
    assert recent[0]["seconds_played"] == 30.0  # 直接入库为秒，不做 ×60 换算
    assert not recent[0]["persona_name"]  # 无成员资料 → 昵称/头像为空（LEFT JOIN）

    # snake_case 别名 latest_played 行
    assert recent[1]["steamid"] == "76561198000000001"
    assert recent[1]["appid"] == 500
    assert recent[1]["rt_last_played"] == 1790000400

    # latestPlayed 真实键行：中文游戏名优先，资料联表正确
    assert recent[2]["steamid"] == "76561198000000001"
    assert recent[2]["appid"] == 1086940
    assert recent[2]["rt_last_played"] == 1790000300
    assert recent[2]["seconds_played"] == 21600.0  # 6 小时，绝不能再被 ×60 虚增
    assert recent[2]["game_name"] == "博德之门3"
    assert recent[2]["persona_name"] == "义父张三"
    assert recent[2]["avatar_url"] == "https://avatars.steamstatic.com/test_avatar.jpg"
    assert recent[2]["header_image"] == "https://img/b.jpg"

    # latestPlayed 优先于 firstPlayed（firstPlayed 只作兜底，不可顶替排序键）
    assert recent[3]["appid"] == 1245620
    assert recent[3]["steamid"] == "76561198000000002"
    assert recent[3]["rt_last_played"] == 1790000100
    assert recent[3]["rt_last_played"] != 1715000000  # firstPlayed 未被误用
    assert recent[3]["seconds_played"] == 5400.0

    # limit 生效
    assert await storage.get_recent_plays(limit=1) == [recent[0]]

    # upsert 覆盖：同一 (steamid, appid) 重复刷新不产生新行、值被覆盖
    updated = [
        {
            "steamid": "76561198000000001",
            "appid": 1086940,
            "firstPlayed": 1718600000,
            "latestPlayed": 1900000000,
            "secondsPlayed": 25200,
        }
    ]
    assert await storage.refresh_playtime_stats(updated) == 1
    assert len(await storage.get_recent_plays(limit=50)) == 5
    top = await storage.get_recent_plays(limit=1)
    assert top[0]["appid"] == 1086940
    assert top[0]["seconds_played"] == 25200.0
    assert top[0]["rt_last_played"] == 1900000000


# ---------------------------------------------------------------------------
# claim_due_pending 毒丸防护（损坏 payload_json 不能炸掉整批领取）
# ---------------------------------------------------------------------------
async def _expire_all_pending(storage: FamilyStorage):
    """把全部待推送行的 next_attempt_at 归零（强制下一轮可领取）。"""
    async with aiosqlite.connect(storage.db_path) as db:
        await db.execute("UPDATE pending_notifications SET next_attempt_at = 0")
        await db.commit()


@pytest.mark.asyncio
async def test_claim_due_pending_survives_poison_payload(tmp_path, caplog):
    storage = await _make_storage(tmp_path)
    await storage.enqueue_pending("added", {"appid": 1}, "added:1")
    await storage.enqueue_pending("removed", {"appid": 2}, "removed:2")

    # 人为把第一行 payload_json 写坏（模拟历史脏数据/毒丸）
    async with aiosqlite.connect(storage.db_path) as db:
        await db.execute(
            "UPDATE pending_notifications SET payload_json = '{broken-json' "
            "WHERE dedupe_key = 'added:1'"
        )
        await db.commit()

    with caplog.at_level(logging.WARNING, logger="core.storage"):
        # 不抛 JSONDecodeError，整批不被炸掉：健康行正常领取
        due = await storage.claim_due_pending()
    assert [d["kind"] for d in due] == ["removed"]
    assert due[0]["payload"] == {"appid": 2}

    # 毒丸行按退避处理：retries+1，且下轮被推后（未到期）
    async with aiosqlite.connect(storage.db_path) as db:
        async with db.execute(
            "SELECT retries, next_attempt_at FROM pending_notifications "
            "WHERE dedupe_key = 'added:1'"
        ) as cursor:
            retries, next_at = await cursor.fetchone()
    assert retries == 1
    assert next_at > time.time()

    # 反复到期领到退避上限（默认 5 次）→ 毒丸行被删除，并记录含 dedupe_key 的 error 日志
    # （每轮健康行 removed:2 随行返回，毒丸行仅自身退避，不影响其领取）
    for _ in range(4):
        await _expire_all_pending(storage)
        batch = await storage.claim_due_pending()
        assert [d["kind"] for d in batch] == ["removed"]

    async with aiosqlite.connect(storage.db_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM pending_notifications WHERE dedupe_key = 'added:1'"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0
    assert "added:1" in caplog.text
    assert any(r.levelno == logging.ERROR for r in caplog.records)


# ---------------------------------------------------------------------------
# save_snapshot 整表重建 app_ownership（移出家庭库的旧映射必须消失）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_save_snapshot_rebuilds_app_ownership(tmp_path):
    storage = await _make_storage(tmp_path)

    async def _ownership_rows():
        async with aiosqlite.connect(storage.db_path) as db:
            async with db.execute(
                "SELECT appid, owner_steamid FROM app_ownership ORDER BY appid"
            ) as cursor:
                return await cursor.fetchall()

    # 快照 A：两部游戏归 ...001 所有
    apps_a = [
        SharedApp(appid=1, name="Game 1", owner_steamids=["76561198000000001"]),
        SharedApp(appid=2, name="Game 2", owner_steamids=["76561198000000001"]),
    ]
    await storage.save_snapshot(apps_a)
    assert await _ownership_rows() == [
        (1, "76561198000000001"),
        (2, "76561198000000001"),
    ]

    # 快照 B：Game 1 移出家庭库、新增 Game 3 → 旧的 (1, ...001) 映射必须消失
    apps_b = [
        SharedApp(appid=2, name="Game 2", owner_steamids=["76561198000000001"]),
        SharedApp(appid=3, name="Game 3", owner_steamids=["76561198000000002"]),
    ]
    await storage.save_snapshot(apps_b)
    assert await _ownership_rows() == [
        (2, "76561198000000001"),
        (3, "76561198000000002"),
    ]

    # 空快照 → 所有权整表清空
    await storage.save_snapshot([])
    assert await _ownership_rows() == []


# ---------------------------------------------------------------------------
# prune_dead_subscribers 失败时间窗（超阈值但很久没失败的 stale 订阅不删）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_prune_dead_subscribers_respects_failure_window(tmp_path):
    storage = await _make_storage(tmp_path)
    await storage.subscribe("t_stale")
    await storage.subscribe("t_fresh")

    for _ in range(6):
        await storage.record_push_failure("t_stale")
        await storage.record_push_failure("t_fresh")

    # t_stale 的失败发生在 48 小时前：目标可能早已恢复、只是一直没触发重发，不应删
    stale_ts = time.time() - 48 * 3600
    async with aiosqlite.connect(storage.db_path) as db:
        await db.execute(
            "UPDATE push_failures SET last_fail_at = ? WHERE target_id = 't_stale'",
            (stale_ts,),
        )
        await db.commit()

    # 默认参数（保持 main.py 无参调用向后兼容）：24 小时窗口内 t_fresh 被删，t_stale 保留
    assert await storage.prune_dead_subscribers() == ["t_fresh"]
    subscribers = await storage.get_subscribers()
    assert "t_stale" in subscribers
    assert "t_fresh" not in subscribers

    # 显式放宽窗口到 72 小时 → stale 订阅这次可被清理
    assert await storage.prune_dead_subscribers(max_failures=5, failure_window_hours=72) == [
        "t_stale"
    ]
    assert "t_stale" not in await storage.get_subscribers()
