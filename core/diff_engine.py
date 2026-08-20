import logging
from typing import NamedTuple
try:
    from .steam_api import SharedApp
except (ImportError, ValueError):
    from core.steam_api import SharedApp

logger = logging.getLogger(__name__)

class DiffResult(NamedTuple):
    added_apps: list[SharedApp]
    removed_apps: list[SharedApp]
    is_first_run: bool

    @property
    def has_changes(self) -> bool:
        return bool(self.added_apps or self.removed_apps)

class FamilyDiffEngine:
    def compute_diff(
        self,
        current_apps: list[SharedApp],
        previous_snapshot: dict[int, SharedApp] | None = None
    ) -> DiffResult:
        """
        Compute incremental diff between current library apps and previous snapshot.
        If previous_snapshot is None, treat as initial run (baseline establishment).
        """
        if previous_snapshot is None:
            return DiffResult(
                added_apps=current_apps,
                removed_apps=[],
                is_first_run=True
            )

        current_app_map = {app.appid: app for app in current_apps}
        
        # Identify newly added apps
        added_apps = [
            app for appid, app in current_app_map.items()
            if appid not in previous_snapshot
        ]
        
        # Identify removed apps
        removed_apps = [
            app for appid, app in previous_snapshot.items()
            if appid not in current_app_map
        ]
        
        return DiffResult(
            added_apps=added_apps,
            removed_apps=removed_apps,
            is_first_run=False
        )
