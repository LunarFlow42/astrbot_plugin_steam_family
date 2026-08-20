"""AstrBot plugin root entrypoint for direct directory loading."""

import sys
from pathlib import Path

# Ensure src/ is in sys.path when loaded directly from plugins folder
_src_dir = str(Path(__file__).parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from astrbot_plugin_steam_family.main import SteamFamilyPlugin

__all__ = ["SteamFamilyPlugin"]
