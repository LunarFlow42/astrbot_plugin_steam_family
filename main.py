import asyncio
import logging
from typing import Any
from astrbot.api.star import Star, register, Context
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.message_components import Plain, Image

try:
    from .config import PluginConfig
    from .core.steam_api import SteamFamilyClient, SteamAuthExpiredError, SteamRateLimitError
    from .core.storage import FamilyStorage
    from .core.diff_engine import FamilyDiffEngine
    from .core.renderer import CardRenderer
    from .core.web_server import DashboardWebServer
    from .agent.tools import SteamAgentTools
except (ImportError, ValueError):
    from config import PluginConfig
    from core.steam_api import SteamFamilyClient, SteamAuthExpiredError, SteamRateLimitError
    from core.storage import FamilyStorage
    from core.diff_engine import FamilyDiffEngine
    from core.renderer import CardRenderer
    from core.web_server import DashboardWebServer
    from agent.tools import SteamAgentTools

logger = logging.getLogger(__name__)

@register(
    "astrbot_plugin_steam_family",
    "LunarFlow42",
    "Steam 家庭资产监控与智能管家插件（支持卡片推送、义父榜与 LLM 找游戏）",
    "0.1.0"
)
class SteamFamilyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        raw_config = context.get_config() if hasattr(context, "get_config") else {}
        self.config = PluginConfig(**raw_config) if isinstance(raw_config, dict) else PluginConfig()

        self.client = SteamFamilyClient(proxy=self.config.proxy, timeout=self.config.request_timeout)
        self.storage = FamilyStorage("data/steam_family.db")
        self.diff_engine = FamilyDiffEngine()
        self.renderer = CardRenderer()
        self.agent_tools = SteamAgentTools(self.storage)
        self.web_server: DashboardWebServer | None = None
        self._polling_task: asyncio.Task | None = None

        # Start async initialization
        asyncio.create_task(self._init_and_start_polling())

    def _refresh_config(self):
        """Dynamically reload config from context if updated via WebUI."""
        if hasattr(self.context, "get_config"):
            try:
                raw = self.context.get_config()
                if isinstance(raw, dict) and raw:
                    self.config = PluginConfig(**raw)
                    # Recreate HTTP client if proxy or timeout changed
                    if self.client.proxy != self.config.proxy or self.client.timeout != self.config.request_timeout:
                        self.client = SteamFamilyClient(proxy=self.config.proxy, timeout=self.config.request_timeout)
            except Exception as e:
                logger.warning(f"Failed to refresh config from context: {e}")

    async def _init_and_start_polling(self):
        try:
            await self.storage.init_db()
            self._refresh_config()

            # Auto-acquire token if cookie is configured
            if not self.config.access_token and self.config.cookie:
                new_token = await self.client.refresh_token_via_cookie(self.config.cookie)
                if new_token:
                    self.config.access_token = new_token
                    logger.info("Automatically initialized Steam Access Token from Cookie.")

            # Start Web Dashboard Server if enabled
            if self.config.enable_dashboard_server:
                self.web_server = DashboardWebServer(
                    host=self.config.dashboard_host,
                    port=self.config.dashboard_port,
                    storage=self.storage,
                    renderer=self.renderer
                )
                await self.web_server.start()

            # Execute immediate initial sync if credentials are configured
            if (self.config.access_token or self.config.cookie) and self.config.steam_id:
                asyncio.create_task(self._check_and_notify())

            # Start background polling loop
            self._polling_task = asyncio.create_task(self._polling_loop())
            logger.info("Steam Family Plugin initialized successfully.")
        except Exception as e:
            logger.error(f"Error during plugin startup: {e}")

    async def _warmup_app_details(self, apps: list):
        """Asynchronously warm up cache for apps in the background."""
        for app in apps[:30]:
            try:
                details = await self.client.get_app_details(app.appid)
                if details:
                    await self.storage.cache_app_details(details)
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.debug(f"Detail warmup skipped for app {app.appid}: {e}")

    async def _check_and_notify(self) -> str:
        self._refresh_config()
        logger.info(f"Checking Steam Family. Token present: {bool(self.config.access_token)}, Cookie present: {bool(self.config.cookie)}, SteamID: {self.config.steam_id}")

        if not self.config.access_token and not self.config.cookie:
            return "⚠️ 未检测到有效配置：未填写 Steam Access Token 或 Cookie，请在 WebUI 设置或使用 /steam_bind 绑定。"
        
        if not self.config.steam_id:
            return "⚠️ 未检测到配置：未填写 17 位 SteamID，请在 WebUI 中填入你的 Steam 64 位 ID。"

        # Attempt auto-refresh token if cookie is set
        if not self.config.access_token and self.config.cookie:
            new_token = await self.client.refresh_token_via_cookie(self.config.cookie)
            if new_token:
                self.config.access_token = new_token
            else:
                return "⚠️ 尝试通过 Cookie 获取 Access Token 失败，请检查 Cookie 有效性或直接在 WebUI 中填入 Access Token。"

        try:
            group_id = self.config.family_group_id
            if not group_id:
                logger.info(f"Fetching family group id for SteamID {self.config.steam_id}...")
                group_id = await self.client.get_family_group_id(self.config.access_token, self.config.steam_id)
                if not group_id:
                    return f"⚠️ 无法获取家庭组 ID。该账号 ({self.config.steam_id}) 可能未加入任何 Steam 家庭组，或 Token 权限不足。"
                self.config.family_group_id = group_id
                logger.info(f"Detected family group id: {group_id}")

            logger.info(f"Fetching shared library apps for group {group_id}...")
            try:
                current_apps = await self.client.get_shared_library(
                    group_id, self.config.access_token, filter_non_games=self.config.filter_non_games
                )
            except SteamAuthExpiredError:
                if self.config.cookie:
                    logger.info("Token expired, attempting auto-refresh via cookie...")
                    new_token = await self.client.refresh_token_via_cookie(self.config.cookie)
                    if new_token:
                        self.config.access_token = new_token
                        current_apps = await self.client.get_shared_library(
                            group_id, self.config.access_token, filter_non_games=self.config.filter_non_games
                        )
                    else:
                        return "⚠️ Steam Access Token 已失效且 Cookie 刷新失败，请重新获取 Token！"
                else:
                    return "⚠️ Steam Access Token 已失效，请在 WebUI 中重新填入最新 Token！"

            if not current_apps:
                return f"⚠️ 成功连接到 Steam 家庭组 (ID: {group_id})，但获取到的共享游戏列表为空（可能家庭组内无共享游戏或被 Steam 过滤）。"

            prev_snapshot = await self.storage.get_latest_snapshot()
            diff = self.diff_engine.compute_diff(current_apps, prev_snapshot)

            # Persist latest snapshot
            await self.storage.save_snapshot(current_apps)

            if diff.is_first_run:
                asyncio.create_task(self._warmup_app_details(current_apps))
                logger.info(f"Steam Family initial baseline saved with {len(current_apps)} games.")
                return f"🎉 首次同步成功！已成功建立家庭库基线数据，共同步【{len(current_apps)}】款游戏！\n📊 资产大盘已实时生成，请访问 Web 看板查看。"

            if not diff.has_changes:
                return f"✅ 家庭库检查完成：当前库内共有 {len(current_apps)} 款游戏，暂无新游戏变动。"

            # Process added games and broadcast
            subscribers = await self.storage.get_subscribers()
            for app in diff.added_apps:
                details = await self.client.get_app_details(app.appid)
                if details:
                    await self.storage.cache_app_details(details)
                
                # Render card if enabled
                card_data = {
                    "appid": app.appid,
                    "game_name": details.chinese_name if details else app.name,
                    "original_name": app.name,
                    "header_image": details.header_image if details else f"https://cdn.akamai.steamstatic.com/steam/apps/{app.appid}/header.jpg",
                    "owner_name": ", ".join(app.owner_steamids) if app.owner_steamids else "家庭成员",
                    "initial_price": f"¥ {details.initial_price:.2f}" if details else "¥ 0.00",
                    "final_price": f"¥ {details.final_price:.2f}" if details else "免费/已入库",
                    "discount_percent": details.discount_percent if details else 0,
                    "review_summary": details.review_summary if details else "暂无评测",
                    "positive_rate": details.positive_rate if details else 0,
                    "genres": details.genres if details else ["Steam 共享"]
                }

                card_bytes = None
                if self.config.enable_render_card:
                    card_bytes = await self.renderer.render_game_card(card_data)

                # Broadcast to subscribers
                for target_id in subscribers:
                    try:
                        if card_bytes and hasattr(self.context, "send_message"):
                            await self.context.send_message(target_id, [Image.fromBytes(card_bytes)])
                        elif hasattr(self.context, "send_message"):
                            msg = f"✨ 【Steam 家庭库新游戏入库】\n🎮 《{card_data['game_name']}》\n👑 入库义父: {card_data['owner_name']}\n💰 价格: {card_data['final_price']}"
                            await self.context.send_message(target_id, [Plain(msg)])
                    except Exception as ex:
                        logger.error(f"Failed to send push to {target_id}: {ex}")

            return f"家庭库变动检查完成：发现 {len(diff.added_apps)} 款新入库游戏，{len(diff.removed_apps)} 款移出。"
        except SteamAuthExpiredError:
            return "⚠️ Steam Access Token 已失效，请重新获取或填入 Cookie 开启全自动永久保活！"
        except SteamRateLimitError:
            return "⚠️ 请求过于频繁触发 Steam 限流 (429)，请稍后重试。"
        except Exception as e:
            logger.error(f"Error during steam family check: {e}", exc_info=True)
            return f"❌ 检查家庭库失败: {type(e).__name__} ({e})。若在中国大陆服务器运行，请检查网络是否能直连 Steam API 或在 WebUI 配置代理 (proxy)。"

    async def _polling_loop(self):
        while True:
            try:
                interval_secs = max(60, self.config.poll_interval_minutes * 60)
                await asyncio.sleep(interval_secs)
                if (self.config.access_token or self.config.cookie) and self.config.steam_id:
                    await self._check_and_notify()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Polling loop encountered error: {e}")

    @filter.command("steam_bind")
    async def cmd_bind(self, event: AstrMessageEvent, access_token: str, steam_id: str = ""):
        """绑定 Steam Access Token 和 SteamID。例: /steam_bind eyJhb... 76561198..."""
        self.config.access_token = access_token
        if steam_id:
            self.config.steam_id = steam_id
        yield event.plain_result("✅ Steam 凭据绑定成功！正在立即拉取家庭库并更新大盘数据...")
        asyncio.create_task(self._check_and_notify())

    @filter.command("steam_check")
    async def cmd_check(self, event: AstrMessageEvent):
        """手动触发一次家庭库变动检查与同步。"""
        yield event.plain_result("⏳ 正在向 Steam 请求家庭库数据，请稍候...")
        result = await self._check_and_notify()
        yield event.plain_result(result)

    @filter.command("steam_sub")
    async def cmd_sub(self, event: AstrMessageEvent):
        """订阅当前群/私聊的家庭库新游戏变动推送。"""
        target_id = getattr(event, "unified_msg_origin", str(event))
        await self.storage.subscribe(target_id)
        yield event.plain_result("🔔 已成功订阅当前会话的 Steam 家庭库变动推送通知！")

    @filter.command("steam_unsub")
    async def cmd_unsub(self, event: AstrMessageEvent):
        """取消订阅当前群/私聊的变动推送。"""
        target_id = getattr(event, "unified_msg_origin", str(event))
        await self.storage.unsubscribe(target_id)
        yield event.plain_result("🔕 已取消当前会话的变动推送订阅。")

    @filter.command("steam rank")
    async def cmd_rank(self, event: AstrMessageEvent):
        """查看家庭组义父资产贡献榜。"""
        stats_text = await self.agent_tools.get_family_stats()
        yield event.plain_result(stats_text)

    @filter.command("steam roll")
    async def cmd_roll(self, event: AstrMessageEvent, tag: str = ""):
        """赛博轮盘：从家庭库中随机抽选一款游戏。例: /steam roll RPG"""
        roll_text = await self.agent_tools.roll_game(tag=tag)
        yield event.plain_result(roll_text)

    @filter.command("steam coop")
    async def cmd_coop(self, event: AstrMessageEvent, member_ids: str = ""):
        """开黑车队雷达：分析多人共同拥有的联机游戏。"""
        ids = [m.strip() for m in member_ids.split() if m.strip()]
        if len(ids) < 2:
            yield event.plain_result("请提供至少两位成员的 SteamID，例如：/steam coop 76561198... 76561198...")
            return
        result = await self.agent_tools.find_coop_games(ids)
        yield event.plain_result(result)

    @filter.command("steam dashboard")
    async def cmd_dashboard(self, event: AstrMessageEvent):
        """获取 Steam 家庭资产全景 Web 看板访问地址。"""
        if not self.config.enable_dashboard_server:
            yield event.plain_result("Web 看板服务未开启，请在插件配置中开启 enable_dashboard_server。")
            return
        host_display = "127.0.0.1" if self.config.dashboard_host in ("0.0.0.0", "") else self.config.dashboard_host
        url = f"http://{host_display}:{self.config.dashboard_port}"
        yield event.plain_result(f"📊 Steam 家庭资产全景 Web 看板地址：\n{url}\n(支持远程浏览器访问)")

    # --- Agent LLM Tools ---
    @filter.llm_tool(name="query_family_games")
    async def tool_query_family_games(self, event: AstrMessageEvent, keyword: str = "", tag: str = ""):
        """检索 Steam 家庭库中的游戏列表与归属者。 Args: keyword(string): 游戏名关键词, tag(string): 类型标签"""
        return await self.agent_tools.query_family_games(keyword=keyword, tag=tag)

    @filter.llm_tool(name="get_family_stats")
    async def tool_get_family_stats(self, event: AstrMessageEvent):
        """获取 Steam 家庭库资产全貌、游戏总数与成员贡献排行。"""
        return await self.agent_tools.get_family_stats()

    @filter.llm_tool(name="roll_family_game")
    async def tool_roll_game(self, event: AstrMessageEvent, tag: str = ""):
        """从 Steam 家庭库中随机抽选一款推荐游戏。 Args: tag(string): 筛选标签"""
        return await self.agent_tools.roll_game(tag=tag)

    async def terminate(self):
        if self._polling_task:
            self._polling_task.cancel()
        if self.web_server:
            await self.web_server.stop()
        await self.renderer.close()
        logger.info("Steam Family Plugin terminated.")
