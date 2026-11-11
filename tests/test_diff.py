from core.diff_engine import AppStatusChange, DiffResult, FamilyDiffEngine, OwnerChange
from core.steam_api import SharedApp


def test_first_run_diff():
    engine = FamilyDiffEngine()
    current_apps = [
        SharedApp(appid=1, name="Game 1", owner_steamids=["user_a"]),
        SharedApp(appid=2, name="Game 2", owner_steamids=["user_b"]),
    ]
    result = engine.compute_diff(current_apps=current_apps, previous_snapshot=None)
    assert result.is_first_run is True
    assert len(result.added_apps) == 2
    assert len(result.removed_apps) == 0


def test_incremental_diff_added_and_removed():
    engine = FamilyDiffEngine()
    prev_snapshot = {
        1: SharedApp(appid=1, name="Game 1", owner_steamids=["user_a"]),
        2: SharedApp(appid=2, name="Game 2", owner_steamids=["user_b"]),
    }
    current_apps = [
        SharedApp(appid=2, name="Game 2", owner_steamids=["user_b"]),
        SharedApp(appid=3, name="Game 3 (New)", owner_steamids=["user_c"]),
    ]
    result = engine.compute_diff(current_apps=current_apps, previous_snapshot=prev_snapshot)
    assert result.is_first_run is False
    assert len(result.added_apps) == 1
    assert result.added_apps[0].appid == 3
    assert len(result.removed_apps) == 1
    assert result.removed_apps[0].appid == 1


def test_no_changes():
    engine = FamilyDiffEngine()
    prev_snapshot = {1: SharedApp(appid=1, name="Game 1", owner_steamids=["user_a"])}
    current_apps = [SharedApp(appid=1, name="Game 1", owner_steamids=["user_a"])]
    result = engine.compute_diff(current_apps=current_apps, previous_snapshot=prev_snapshot)
    assert result.is_first_run is False
    assert len(result.added_apps) == 0
    assert len(result.removed_apps) == 0
    assert result.has_changes is False


def test_owner_change_transfer():
    """归属人转移：A 独有 → A+B 共同拥有，应产出 OwnerChange 且计入 has_changes。"""
    engine = FamilyDiffEngine()
    prev_snapshot = {1: SharedApp(appid=1, name="Game 1", owner_steamids=["user_a"])}
    current_apps = [
        SharedApp(
            appid=1,
            name="Game 1",
            owner_steamids=["user_a", "user_b"],
            rt_last_played=9999,
            rt_playtime=12345,
        )
    ]
    result = engine.compute_diff(current_apps=current_apps, previous_snapshot=prev_snapshot)
    assert result.is_first_run is False
    assert len(result.added_apps) == 0
    assert len(result.removed_apps) == 0
    assert len(result.owner_changes) == 1
    change = result.owner_changes[0]
    assert isinstance(change, OwnerChange)
    assert change.app.appid == 1
    assert change.added_owners == ["user_b"]
    assert change.removed_owners == []
    assert result.has_changes is True


def test_owner_change_removal():
    """纯移除归属人：A+B 共同拥有 → A 独有，只失去归属人不新增。"""
    engine = FamilyDiffEngine()
    prev_snapshot = {
        1: SharedApp(appid=1, name="Game 1", owner_steamids=["user_a", "user_b"]),
        2: SharedApp(appid=2, name="Game 2", owner_steamids=["user_b"]),
    }
    current_apps = [
        SharedApp(appid=1, name="Game 1", owner_steamids=["user_a"]),
        SharedApp(appid=2, name="Game 2", owner_steamids=["user_b"]),
    ]
    result = engine.compute_diff(current_apps=current_apps, previous_snapshot=prev_snapshot)
    assert len(result.added_apps) == 0
    assert len(result.removed_apps) == 0
    assert len(result.owner_changes) == 1
    change = result.owner_changes[0]
    assert change.app.appid == 1
    assert change.added_owners == []
    assert change.removed_owners == ["user_b"]
    assert result.has_changes is True


def test_exclude_reason_change():
    """exclude_reason 0→1（在库正常 → 被排除/移出）应产出 AppStatusChange 且计入 has_changes。"""
    engine = FamilyDiffEngine()
    prev_snapshot = {1: SharedApp(appid=1, name="Game 1", owner_steamids=["user_a"])}
    current_apps = [
        SharedApp(appid=1, name="Game 1", owner_steamids=["user_a"], exclude_reason=1)
    ]
    result = engine.compute_diff(current_apps=current_apps, previous_snapshot=prev_snapshot)
    assert len(result.added_apps) == 0
    assert len(result.removed_apps) == 0
    assert len(result.status_changes) == 1
    change = result.status_changes[0]
    assert isinstance(change, AppStatusChange)
    assert change.app.appid == 1
    assert change.field == "exclude_reason"
    assert change.old == 0
    assert change.new == 1
    assert result.has_changes is True


def test_app_type_change():
    """app_type 变化应产出 AppStatusChange 且计入 has_changes。"""
    engine = FamilyDiffEngine()
    prev_snapshot = {1: SharedApp(appid=1, name="Game 1", owner_steamids=["user_a"], app_type=1)}
    current_apps = [SharedApp(appid=1, name="Game 1", owner_steamids=["user_a"], app_type=2)]
    result = engine.compute_diff(current_apps=current_apps, previous_snapshot=prev_snapshot)
    assert len(result.status_changes) == 1
    change = result.status_changes[0]
    assert change.field == "app_type"
    assert change.old == 1
    assert change.new == 2
    assert result.has_changes is True


def test_multiple_status_fields_change():
    """exclude_reason 与 app_type 同时变化时，应按字段逐一产出两条状态事件。"""
    engine = FamilyDiffEngine()
    prev_snapshot = {
        1: SharedApp(appid=1, name="Game 1", owner_steamids=["user_a"], exclude_reason=0, app_type=1)
    }
    current_apps = [
        SharedApp(appid=1, name="Game 1", owner_steamids=["user_a"], exclude_reason=1, app_type=3)
    ]
    result = engine.compute_diff(current_apps=current_apps, previous_snapshot=prev_snapshot)
    assert len(result.status_changes) == 2
    assert [(c.field, c.old, c.new) for c in result.status_changes] == [
        ("exclude_reason", 0, 1),
        ("app_type", 1, 3),
    ]
    assert result.has_changes is True


def test_noisy_rt_fields_ignored():
    """rt_last_played / rt_playtime / rt_time_acquired 过于嘈杂，禁止参与 diff。"""
    engine = FamilyDiffEngine()
    prev_snapshot = {
        1: SharedApp(
            appid=1,
            name="Game 1",
            owner_steamids=["user_a"],
            rt_time_acquired=100,
            rt_last_played=200,
            rt_playtime=300,
        )
    }
    current_apps = [
        SharedApp(
            appid=1,
            name="Game 1",
            owner_steamids=["user_a"],
            rt_time_acquired=111,
            rt_last_played=222,
            rt_playtime=333,
        )
    ]
    result = engine.compute_diff(current_apps=current_apps, previous_snapshot=prev_snapshot)
    assert len(result.owner_changes) == 0
    assert len(result.status_changes) == 0
    assert result.has_changes is False


def test_added_removed_logic_unaffected():
    """新增/移出逻辑不受新事件类型影响：新增 + 归属人变化可并存。"""
    engine = FamilyDiffEngine()
    prev_snapshot = {1: SharedApp(appid=1, name="Game 1", owner_steamids=["user_a"])}
    current_apps = [
        SharedApp(appid=1, name="Game 1", owner_steamids=["user_a", "user_b"]),
        SharedApp(appid=3, name="Game 3 (New)", owner_steamids=["user_c"]),
    ]
    result = engine.compute_diff(current_apps=current_apps, previous_snapshot=prev_snapshot)
    assert result.is_first_run is False
    assert len(result.added_apps) == 1
    assert result.added_apps[0].appid == 3
    assert len(result.removed_apps) == 0
    assert len(result.owner_changes) == 1
    assert result.owner_changes[0].added_owners == ["user_b"]
    assert result.has_changes is True


def test_diff_result_backward_compatible_construction():
    """DiffResult 仍支持旧式构造（仅传 added/removed/is_first_run），新增字段默认为不可变空元组。"""
    result = DiffResult(added_apps=[], removed_apps=[], is_first_run=False)
    assert result.owner_changes == ()
    assert result.status_changes == ()
    assert result.has_changes is False
