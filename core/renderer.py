import logging
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)


class CardRenderer:
    """Steam 游戏卡片与看板 HTML 模板渲染引擎 (基于 Jinja2)。"""

    def __init__(self, template_dir: str | Path | None = None):
        if template_dir is None:
            template_dir = Path(__file__).parent.parent / "assets" / "templates"
        self.template_dir = Path(template_dir)
        self.env = Environment(loader=FileSystemLoader(str(self.template_dir)))

    def get_template_source(self, template_name: str = "game_card.html") -> str:
        """获取 Jinja2 原始模板源码。"""
        tmpl_file = self.template_dir / template_name
        if tmpl_file.exists():
            return tmpl_file.read_text(encoding="utf-8")
        return ""

    def render_html(self, data: dict[str, Any], template_name: str = "game_card.html") -> str:
        """渲染 HTML 字符串。"""
        template = self.env.get_template(template_name)
        return template.render(**data)

    def render_dashboard_html(self, data: dict[str, Any]) -> str:
        """渲染全景大盘 HTML 字符串。"""
        return self.render_html(data, template_name="dashboard.html")
