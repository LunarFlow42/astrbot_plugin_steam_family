from pydantic import BaseModel, Field
from astrbot_plugin_steam_family.core.steam_api import SharedApp

class DiffResult(BaseModel):
    is_first_run: bool = False
    added_apps: list[SharedApp] = Field(default_factory=list)
    removed_apps: list[SharedApp] = Field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return len(self.added_apps) > 0 or len(self.removed_apps) > 0

class FamilyDiffEngine:
    def compute_diff(
        self,
        current_apps: list[SharedApp],
        previous_snapshot: dict[int, SharedApp] | None
    ) -> DiffResult:
        if previous_snapshot is None:
            return DiffResult(
                is_first_run=True,
                added_apps=current_apps,
                removed_apps=[]
            )

        current_map = {app.appid: app for app in current_apps}
        
        added: list[SharedApp] = [
            app for appid, app in current_map.items() if appid not in previous_snapshot
        ]
        removed: list[SharedApp] = [
            app for appid, app in previous_snapshot.items() if appid not in current_map
        ]

        return DiffResult(
            is_first_run=False,
            added_apps=added,
            removed_apps=removed
        )
