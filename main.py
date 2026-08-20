import asyncio
import logging
from typing import Any
from astrbot.api.star import Star, register, Context
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.message_components import Plain, Image

# Support both direct import and relative import in AstrBot loader
try:
    from .config import PluginConfig
    from .core.steam_api import SteamFamilyClient, SteamAuthExpiredError, SteamRateLimitError
    from .core.storage import FamilyStorage
    from .core.diff_engine import FamilyDiffEngine
    from .core.renderer import CardRenderer
    from .agent.tools import SteamAgentTools
except (ImportError, ValueError):
    from config import PluginConfig
    from core.steam_api import SteamFamilyClient, SteamAuthExpiredError, SteamRateLimitError
    from core.storage import FamilyStorage
    from core.diff_engine import FamilyDiffEngine
    from core.renderer import CardRenderer
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
        self._polling_task: asyncio.Task | None = None

        # Start async initialization
        asyncio.create_task(self._init_and_start_polling())

    async def _init_and_start_polling(self):
        try:
            await self.storage.init_db()
            # Start background polling
            self._polling_task = asyncio.create_task(self._polling_loop())
            logger.info("Steam Family Plugin initialized successfully.")
        except Exception as e:
            logger.error(f"Error during plugin startup: {e}")

    async def _check_and_notify(self) -> str:
        if not self.config.access_token or not self.config.steam_id:
            return "未配置 Steam Access Token 或 SteamID，请在 WebUI 设置或使用 /steam_bind 配置。"

        try:
            group_id = self.config.family_group_id
            if not group_id:
                group_id = await self.client.get_family_group_id(self.config.access_token, self.config.steam_id)
                if not group_id:
                    return "无法获取家庭组 ID，请检查 SteamID 或 Token 是否具备家庭组访问权限。"
                self.config.family_group_id = group_id

            current_apps = await self.client.get_shared_library(
                group_id, self.config.access_token, filter_non_games=self.config.filter_non_games
            )
            prev_snapshot = await self.storage.get_latest_snapshot()
            diff = self.diff_engine.compute_diff(current_apps, prev_snapshot)

            # Persist latest snapshot
            await self.storage.save_snapshot(current_apps)

            if diff.is_first_run:
                logger.info(f"Steam Family initial baseline saved with {len(current_apps)} games.")
                return f"家庭库基线数据已建立，共同步 {len(current_apps)} 款游戏。"

            if not diff.has_changes:
                return "家庭库检查完成：暂无游戏变动。"

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
            return "⚠️ Steam Access Token 已失效，请重新获取并绑定！"
        except SteamRateLimitError:
            return "⚠️ 请求过于频繁触发 Steam 限流 (429)，请稍后重试。"
        except Exception as e:
            logger.error(f"Error during steam family check: {e}")
            return f"检查家庭库变动时发生错误: {e}"

    async def _polling_loop(self):
        while True:
            try:
                interval_secs = max(60, self.config.poll_interval_minutes * 60)
                await asyncio.sleep(interval_secs)
                if self.config.access_token and self.config.steam_id:
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
        yield event.plain_result("✅ Steam 凭据绑定成功！输入 /steam_check 可立即拉取家庭库。")

    @filter.command("steam_check")
    async def cmd_check(self, event: AstrMessageEvent):
        """手动触发一次家庭库变动检查与同步。"""
        yield event.plain_result("⏳ 正在查询 Steam 家庭库数据，请稍候...")
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
        await self.renderer.close()
        logger.info("Steam Family Plugin terminated.")
