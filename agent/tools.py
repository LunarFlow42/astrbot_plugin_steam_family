import random
import logging
from typing import Any
try:
    from ..core.storage import FamilyStorage
except (ImportError, ValueError):
    from core.storage import FamilyStorage

logger = logging.getLogger(__name__)

class SteamAgentTools:
    def __init__(self, storage: FamilyStorage):
        self.storage = storage

    async def query_family_games(self, keyword: str = "", tag: str = "", max_price: float | None = None) -> str:
        """
        在当前 Steam 家庭共享库中按关键词、标签或价格检索游戏。
        Args:
            keyword: 游戏中文名或英文名关键词
            tag: 游戏类型标签（例如 RPG、Roguelike、动作、策略、独立等）
            max_price: 价格上限（元）
        """
        snapshot = await self.storage.get_latest_snapshot()
        if not snapshot:
            return "当前家庭库暂未同步数据，请稍后再试或提醒管理员执行 /steam_check。"

        matched = []
        for appid, app in snapshot.items():
            cached = await self.storage.get_cached_app(appid)
            c_name = cached.chinese_name if cached else app.name
            genres = cached.genres if cached else []
            price = cached.final_price if cached else 0.0

            if keyword and (keyword.lower() not in app.name.lower() and (not c_name or keyword.lower() not in c_name.lower())):
                continue
            if tag and not any(tag.lower() in g.lower() for g in genres):
                continue
            if max_price is not None and price > max_price:
                continue

            owners_str = ", ".join(app.owner_steamids) if app.owner_steamids else "共享成员"
            matched.append(f"🎮 【{c_name}】(AppID: {appid}) - 现价: ¥{price:.1f} | 标签: {','.join(genres[:3])} | 归属: {owners_str}")

        if not matched:
            return f"未在家庭库中找到符合条件的游戏 (关键词: '{keyword}', 标签: '{tag}')。"
        
        preview = "\n".join(matched[:10])
        total = len(matched)
        return f"家庭库共检索到 {total} 款匹配游戏：\n{preview}" + ("\n...(仅显示前10款)" if total > 10 else "")

    async def get_family_stats(self) -> str:
        """
        获取当前 Steam 家庭库的资产全貌、游戏总数与各成员贡献排行榜。
        """
        rankings = await self.storage.get_family_rankings()
        snapshot = await self.storage.get_latest_snapshot()
        total_games = len(snapshot) if snapshot else 0
        total_initial_val = sum(r["total_initial_price"] for r in rankings)
        total_current_val = sum(r["total_current_price"] for r in rankings)

        lines = [
            "📊 === Steam 家庭组资产全景简报 ===",
            f"总游戏数: {total_games} 款",
            f"库总资产 (原价估值): ¥{total_initial_val:,.2f}",
            f"库总资产 (现价估值): ¥{total_current_val:,.2f}",
            "\n👑 【家庭义父贡献榜】:"
        ]
        for i, r in enumerate(rankings[:5], 1):
            lines.append(f"{i}. 成员 {r['steamid'][-4:]}****: 贡献 {r['game_count']} 款游戏 (原值 ¥{r['total_initial_price']:,.2f})")

        return "\n".join(lines)

    async def roll_game(self, tag: str = "", min_rating: int = 0) -> str:
        """
        治愈选择困难症，从家庭库中随机抽取一款游戏并给出推荐理由。
        Args:
            tag: 指定游戏标签（如 恐怖、联机、卡牌、科幻）
            min_rating: 最低好评率百分比 (0-100)
        """
        snapshot = await self.storage.get_latest_snapshot()
        if not snapshot:
            return "家庭库数据为空，无法抽取游戏。"

        candidates = []
        for appid, app in snapshot.items():
            cached = await self.storage.get_cached_app(appid)
            if not cached:
                continue
            if tag and not any(tag.lower() in g.lower() for g in cached.genres):
                continue
            if cached.positive_rate < min_rating:
                continue
            candidates.append(cached)

        if not candidates:
            return f"没有找到符合筛选条件 (标签: {tag}, 最低好评: {min_rating}%) 的待选游戏。"

        picked = random.choice(candidates)
        return (
            f"🎲 今日赛博轮盘为你抽中了：\n"
            f"🎯 【{picked.chinese_name or picked.name}】 (AppID: {picked.appid})\n"
            f"🌟 好评率: {picked.positive_rate}% ({picked.review_summary})\n"
            f"🏷️ 类型: {', '.join(picked.genres[:3])}\n"
            f"💰 价格: ¥{picked.final_price:.2f}\n"
            f"💡 推荐评语: 赶紧拉上家庭组的小伙伴去体验吧！"
        )

    async def find_coop_games(self, member_ids: list[str]) -> str:
        """
        查询指定多位家庭成员共同拥有的合作联机游戏。
        """
        if len(member_ids) < 2:
            return "请提供至少两位成员的 SteamID。"
        games = await self.storage.get_coop_games(member_ids)
        if not games:
            return "未在家庭库中找到指定成员均拥有的联机游戏。"
        lines = [f"🚀 【车队雷达】共找到 {len(games)} 款全员拥有游戏："]
        for g in games[:10]:
            lines.append(f"🎮 【{g['chinese_name'] or g['name']}】 (AppID: {g['appid']})")
        return "\n".join(lines)
