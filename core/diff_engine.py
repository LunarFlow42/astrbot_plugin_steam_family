import logging
from typing import NamedTuple

try:
    from .steam_api import SharedApp
except (ImportError, ValueError):
    from core.steam_api import SharedApp

logger = logging.getLogger(__name__)

# 参与状态 diff 的整型字段白名单。
# 注意：rt_last_played / rt_playtime / rt_time_acquired 这三个字段过于嘈杂（随游玩与同步频繁变化），
# 禁止参与 diff，避免产生大量无意义的变动事件与推送噪音。
STATUS_DIFF_FIELDS: tuple[str, ...] = ("exclude_reason", "app_type")


class OwnerChange(NamedTuple):
    """
    归属人变化事件。
    针对同时存在于当前与上一快照中的 appid，其 owner_steamids 集合发生变化：
      - added_owners: 本次新增的归属人（获得归属权）；
      - removed_owners: 本次失去的归属人（失去归属权）。
    """

    app: SharedApp
    added_owners: list[str]
    removed_owners: list[str]


class AppStatusChange(NamedTuple):
    """
    状态变化事件。
    针对同时存在于当前与上一快照中的 appid，单个状态字段发生变化：
      - exclude_reason: 0=在库正常，非 0=被排除/移出家庭库；
      - app_type: Steam 侧的应用类型标记。
    """

    app: SharedApp
    field: str
    old: int
    new: int


class DiffResult(NamedTuple):
    """家庭库增量比对结果。"""

    added_apps: list[SharedApp]
    removed_apps: list[SharedApp]
    is_first_run: bool
    # 默认值为不可变空元组，避免可变默认值问题
    owner_changes: list[OwnerChange] = ()
    status_changes: list[AppStatusChange] = ()

    @property
    def has_changes(self) -> bool:
        """added / removed / owner_changes / status_changes 任一非空即视为存在变动。"""
        return bool(
            self.added_apps or self.removed_apps or self.owner_changes or self.status_changes
        )


class FamilyDiffEngine:
    def compute_diff(
        self,
        current_apps: list[SharedApp],
        previous_snapshot: dict[int, SharedApp] | None = None,
    ) -> DiffResult:
        """
        计算当前家庭库与上一快照之间的增量差异。
        若 previous_snapshot 为 None，视为首次运行（仅建立基线，不产出变动事件）。

        在新增 / 移除游戏之外，本引擎还会针对同时存在于两侧的 appid 识别两类事件：
          - OwnerChange: 归属人集合变化（新增归属人 / 失去归属人）；
          - AppStatusChange: 状态字段变化（exclude_reason 与 app_type）。
        """
        if previous_snapshot is None:
            return DiffResult(added_apps=current_apps, removed_apps=[], is_first_run=True)

        current_app_map = {app.appid: app for app in current_apps}

        # 识别新增游戏
        added_apps = [
            app for appid, app in current_app_map.items() if appid not in previous_snapshot
        ]

        # 识别移除游戏
        removed_apps = [
            app for appid, app in previous_snapshot.items() if appid not in current_app_map
        ]

        # 识别归属人变化与状态字段变化（仅针对两侧同时存在的 appid）
        owner_changes: list[OwnerChange] = []
        status_changes: list[AppStatusChange] = []
        for appid, prev_app in previous_snapshot.items():
            cur_app = current_app_map.get(appid)
            if cur_app is None:
                continue

            # 归属人集合变化：新增归属人 / 失去归属人（保持原有列表顺序输出）
            prev_owners = set(prev_app.owner_steamids)
            cur_owners = set(cur_app.owner_steamids)
            added_owners = [sid for sid in cur_app.owner_steamids if sid not in prev_owners]
            removed_owners = [sid for sid in prev_app.owner_steamids if sid not in cur_owners]
            if added_owners or removed_owners:
                owner_changes.append(
                    OwnerChange(
                        app=cur_app,
                        added_owners=added_owners,
                        removed_owners=removed_owners,
                    )
                )

            # 状态字段变化：exclude_reason（0=在库正常，非 0=被排除/移出）与 app_type。
            # rt_last_played / rt_playtime / rt_time_acquired 过于嘈杂，禁止参与 diff。
            for field in STATUS_DIFF_FIELDS:
                old = int(getattr(prev_app, field, 0))
                new = int(getattr(cur_app, field, 0))
                if old != new:
                    status_changes.append(
                        AppStatusChange(app=cur_app, field=field, old=old, new=new)
                    )

        return DiffResult(
            added_apps=added_apps,
            removed_apps=removed_apps,
            is_first_run=False,
            owner_changes=owner_changes,
            status_changes=status_changes,
        )
