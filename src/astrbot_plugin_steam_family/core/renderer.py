import logging
from pathlib import Path
from typing import Any
from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

class CardRenderer:
    def __init__(self, template_dir: str | Path | None = None):
        if template_dir is None:
            template_dir = Path(__file__).parent.parent.parent.parent / "assets" / "templates"
        self.template_dir = Path(template_dir)
        self.env = Environment(loader=FileSystemLoader(str(self.template_dir)))
        self._browser = None
        self._playwright = None

    def render_html(self, data: dict[str, Any], template_name: str = "game_card.html") -> str:
        template = self.env.get_template(template_name)
        return template.render(**data)

    def render_dashboard_html(self, data: dict[str, Any]) -> str:
        return self.render_html(data, template_name="dashboard.html")

    async def render_game_card(self, data: dict[str, Any]) -> bytes | None:
        try:
            from playwright.async_api import async_playwright
            if self._playwright is None:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(headless=True)
            
            page = await self._browser.new_page(viewport={"width": 720, "height": 650})
            html_content = self.render_html(data, template_name="game_card.html")
            await page.set_content(html_content, wait_until="load")
            card_elem = await page.query_selector(".card")
            if card_elem:
                image_bytes = await card_elem.screenshot(type="png")
            else:
                image_bytes = await page.screenshot(type="png")
            await page.close()
            return image_bytes
        except Exception as e:
            logger.warning(f"Playwright card screenshot unavailable, falling back to text: {e}")
            return None

    async def close(self):
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
