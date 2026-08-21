from core.diff_engine import FamilyDiffEngine
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
