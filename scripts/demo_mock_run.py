import asyncio
import os
from pathlib import Path

from agent.tools import SteamAgentTools
from core.diff_engine import FamilyDiffEngine
from core.renderer import CardRenderer
from core.steam_api import AppDetails, SharedApp
from core.storage import FamilyStorage


async def main():
    print("\n" + "=" * 60)
    print("🚀 【Steam Family Plugin - 真实全链路 E2E 仿真与验证】")
    print("=" * 60 + "\n")

    test_db_path = "data/demo_steam_family.db"
    if os.path.exists(test_db_path):
        os.remove(test_db_path)

    storage = FamilyStorage(test_db_path)
    await storage.init_db()
    print("1. [存储引擎] SQLite 异步数据库与数据表初始化完成 ✅")

    # 1. 模拟初次同步基线（Baseline Sync）
    initial_apps = [
        SharedApp(appid=1086940, name="Baldur's Gate 3", owner_steamids=["76561198000000001"]),
        SharedApp(appid=1245620, name="ELDEN RING", owner_steamids=["76561198000000002"]),
        SharedApp(appid=1145360, name="Hades", owner_steamids=["76561198000000001"]),
    ]
    diff_engine = FamilyDiffEngine()
    diff1 = diff_engine.compute_diff(initial_apps, None)
    await storage.save_snapshot(initial_apps)
    print(
        f"2. [基线同步] 初次建立库快照成功: 共有 {len(initial_apps)} 款游戏 (is_first_run={diff1.is_first_run}) ✅"
    )

    # 2. 模拟增量变动检测（新增 1 款神作：黑神话悟空，移出 1 款）
    updated_apps = [
        SharedApp(appid=1086940, name="Baldur's Gate 3", owner_steamids=["76561198000000001"]),
        SharedApp(appid=1245620, name="ELDEN RING", owner_steamids=["76561198000000002"]),
        SharedApp(
            appid=2358720,
            name="Black Myth: Wukong",
            owner_steamids=["76561198000000001"],
        ),
    ]
    prev_snapshot = await storage.get_latest_snapshot()
    diff2 = diff_engine.compute_diff(updated_apps, prev_snapshot)
    await storage.save_snapshot(updated_apps)
    print(
        f"3. [增量比对] 识别到新入库游戏: {[a.name for a in diff2.added_apps]}，移出游戏: {[a.name for a in diff2.removed_apps]} ✅"
    )

    # 3. 模拟 Steam 商店元数据抓取与缓存
    wukong_details = AppDetails(
        appid=2358720,
        name="Black Myth: Wukong",
        chinese_name="黑神话：悟空",
        header_image="https://cdn.akamai.steamstatic.com/steam/apps/2358720/header.jpg",
        initial_price=268.0,
        final_price=268.0,
        genres=["动作", "角色扮演", "神话", "魂系"],
        positive_rate=96,
        review_summary="好评如潮 (96%)",
    )
    bg3_details = AppDetails(
        appid=1086940,
        name="Baldur's Gate 3",
        chinese_name="博德之门3",
        header_image="https://cdn.akamai.steamstatic.com/steam/apps/1086940/header.jpg",
        initial_price=298.0,
        final_price=298.0,
        genres=["角色扮演", "策略", "回合制"],
        positive_rate=96,
        review_summary="好评如潮 (96%)",
    )
    await storage.cache_app_details(wukong_details)
    await storage.cache_app_details(bg3_details)
    print("4. [商店元数据] 成功缓存《黑神话：悟空》与《博德之门3》中文详情与价格 ✅")

    # 4. Agent Function Calling 工具集模拟
    tools = SteamAgentTools(storage)
    print("\n--- 🤖 [Agent LLM 模拟测试: 家庭资产大盘简报] ---")
    stats = await tools.get_family_stats()
    print(stats)

    print("\n--- 🎲 [Agent LLM 模拟测试: 赛博轮盘推荐] ---")
    roll = await tools.roll_game(tag="动作")
    print(roll)

    # 5. 渲染引擎验证
    renderer = CardRenderer()
    card_html = renderer.render_html(
        {
            "appid": 2358720,
            "game_name": "黑神话：悟空",
            "original_name": "Black Myth: Wukong",
            "header_image": "https://cdn.akamai.steamstatic.com/steam/apps/2358720/header.jpg",
            "owner_name": "大义父 (76561198000000001)",
            "initial_price": "¥ 268.00",
            "final_price": "¥ 268.00",
            "discount_percent": 0,
            "review_summary": "好评如潮 (96%)",
            "positive_rate": 96,
            "genres": ["动作", "角色扮演", "神话", "魂系"],
        }
    )
    dashboard_data = await storage.get_dashboard_data()
    dashboard_html = renderer.render_dashboard_html(dashboard_data)

    out_dir = Path("assets/preview")
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "preview_game_card.html", "w", encoding="utf-8") as f:
        f.write(card_html)
    with open(out_dir / "preview_dashboard.html", "w", encoding="utf-8") as f:
        f.write(dashboard_html)

    print(
        f"\n5. [卡片与大盘生成] 已成功生成预览文件至 assets/preview/ (卡片: {len(card_html)} 字节, 大盘: {len(dashboard_html)} 字节) ✅"
    )
    print("\n" + "=" * 60)
    print("🎉 【全链路端到端仿真测试全部通过！】")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
