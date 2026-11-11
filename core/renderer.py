import logging
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)


class CardRenderer:
    """Steam 游戏卡片与看板 HTML 模板渲染引擎 (基于 Jinja2)。"""

    def __init__(self, template_dir: str | Path | None = None):
        if template_dir is None:
            template_dir = Path(__file__).parent.parent / "assets" / "templates"
        self.template_dir = Path(template_dir)
        # autoescape：游戏名/昵称/评价等外部文本（Steam API 或用户可控）输出前统一
        # HTML 转义，防止含 <script> 的昵称注入卡片/看板；两个内置模板（game_card /
        # dashboard）全部为纯文本插值、不向模板传入任何已构造的 HTML 片段，
        # 因此无需 | safe 豁免，外部文本一律转义。
        self.env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            autoescape=select_autoescape(["html", "xml"]),
        )

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
