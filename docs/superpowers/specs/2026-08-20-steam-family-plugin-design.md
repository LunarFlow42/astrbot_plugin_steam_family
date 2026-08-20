# Design Specification: `astrbot_plugin_steam_family`

## 1. 概述与设计目标
`astrbot_plugin_steam_family` 是一个专为 AstrBot 机器人框架打造的现代化 Steam 家庭库资产监控与智能管家插件。
插件旨在实现全自动家庭共享库变动监控、卡片化视觉通知、家庭资产数据挖掘与排行（“义父榜”）、联机决策辅助（车队雷达与赛博轮盘），并深度集成 AstrBot Agent LLM Function Calling 能力。

---

## 2. 整体架构与模块分解

```
astrbot_plugin_steam_family/
├── pyproject.toml                         # 现代 uv 打包与依赖声明 (Python >= 3.10)
├── README.md                              # 插件安装、配置与使用说明
├── assets/
│   └── templates/
│       └── game_card.html                 # 赛博深色风格卡片渲染模板 (Jinja2)
├── src/
│   └── astrbot_plugin_steam_family/
│       ├── __init__.py                    # 包导出与元数据
│       ├── config.py                      # Pydantic v2 配置模型（支持 WebUI 反射）
│       ├── main.py                        # AstrBot Star 插件入口、指令注册与后台监控任务
│       ├── core/
│       │   ├── __init__.py
│       │   ├── steam_api.py               # Steam Web API / Store API / 扫码登录异步客户端
│       │   ├── diff_engine.py             # 家庭库增量比对引擎 (Diff Engine)
│       │   ├── renderer.py                # Playwright 异步无头卡片渲染器
│       │   └── storage.py                 # aiosqlite 异步数据库与状态快照管理
│       └── agent/
│           ├── __init__.py
│           └── tools.py                   # LLM Function Calling 工具集
└── tests/
    ├── __init__.py
    ├── test_diff.py                       # 增量比对逻辑单元测试
    ├── test_storage.py                    # 数据库存储与快照单元测试
    └── mock_data.py                       # 仿真 Mock 数据与测试夹具
```

---

## 3. 详细设计规范

### 3.1 `config.py` 配置模型
采用 `pydantic.BaseModel` (v2)，包含字段：
- `access_token: str = ""`：Steam Web API / 网页端 Access Token
- `steam_id: str = ""`：绑定用户的 64 位 SteamID
- `family_group_id: str = ""`：家庭组 ID（留空时可自动查询）
- `poll_interval_minutes: int = 15`：后台自动轮询间隔（分钟，默认 15）
- `proxy: str = ""`：HTTP/HTTPS/SOCKS5 代理地址
- `request_timeout: int = 20`：请求超时时间（秒）
- `enable_render_card: bool = True`：是否开启 HTML 卡片图片渲染
- `filter_non_games: bool = True`：是否过滤测试服/DLC/工具类非游戏应用
- `subscribers: list[str]`：已订阅变动推送的目标 UMO 会话标识（群聊/私聊）

### 3.2 `core/steam_api.py` 客户端
- **类名**：`SteamFamilyClient`
- **主要方法**：
  - `get_family_group_id(access_token: str, steam_id: str) -> str | None`：调用 `IFamilyGroupsService/GetFamilyGroupForUser/v1/`。
  - `get_family_group_summary(family_group_id: str, access_token: str) -> FamilyGroupSummary`：获取家庭组名称及成员信息（SteamID、角色、昵称）。
  - `get_shared_library(family_group_id: str, access_token: str, include_non_shared: bool = True) -> list[SharedApp]`：调用 `IFamilyGroupsService/GetSharedLibraryApps/v1/`，内置过滤规则（根据 `exclude_reason`、`app_type` 等剔除 Dedicated Server、Demo、Soundtrack、DLC 等）。
  - `get_app_details(appid: int) -> AppDetails | None`：调用 `store.steampowered.com/api/appdetails`，获取中文名称、封面图、原价、折后价、折扣率、好评率（来自 reviews 接口或 store 数据）、标签，并集成基于内存的 LRU 缓存。
  - `create_qr_session() -> QRSessionInfo` 与 `poll_qr_status(client_id, request_id)`：支持 Steam 扫码登录认证流程。
- **容错与重试**：
  - 基于 `httpx.AsyncClient`，封装自适应指数退避重试（针对 429 Too Many Requests、5xx 及 ConnectionTimeout）。
  - 遇到 401 Unauthorized 时准确向上层抛出 `SteamAuthExpiredError`。

### 3.3 `core/storage.py` 存储与状态机
- **数据库**：基于 `aiosqlite` 的 SQLite 本地数据库，默认存放在插件工作目录下 `data/steam_family.db`。
- **数据表结构**：
  - `family_group`：记录家庭组基本信息与上次同步时间。
  - `family_members`：记录家庭成员 SteamID、昵称、角色、头像。
  - `apps_cache`：记录全量 App 详情缓存（appid, name, header_image, initial_price, final_price, discount_percent, genres, is_free, updated_at）。
  - `app_ownership`：记录 App 与各成员的拥有关系（appid, owner_steamid）。
  - `library_snapshots`：每次成功轮询生成的快照记录（snapshot_id, app_count, created_at, raw_appids_json）。
  - `subscriptions`：订阅推送的目标 Unified Message Object (UMO) 会话 ID 列表。
- **核心操作**：
  - `save_snapshot_and_sync(apps: list[SharedApp]) -> DiffResult`：在单个事务内保存最新快照并比对历史最新快照。
  - `get_family_ranking_stats()`：多表聚合统计计算各成员拥有游戏数、总资产估值（原价总和、现折后总额）。
  - `query_coop_games(member_steamids: list[str])`：计算多位群友共同拥有或家庭库共享份数足够的合作游戏。

### 3.4 `core/diff_engine.py` 增量引擎
- **比对逻辑**：
  - 输入：当前轮询拉取的最新 `SharedApp` 集合 vs 上一次存储的基线快照。
  - 输出：`DiffResult` 对象，包含：
    - `added_apps: list[SharedApp]`（新入库游戏）
    - `removed_apps: list[SharedApp]`（被移出或家庭解绑的游戏）
    - `is_first_run: bool`（若为初次同步，不触发全库轰炸推送，仅落库基线数据）

### 3.5 `core/renderer.py` & `assets/templates/game_card.html`
- **模板设计**：
  - 赛博朋克 / Steam 极简毛玻璃深色风格（Dark Slate UI）。
  - 展示要素：游戏大尺寸封面图、游戏中文译名与英文原名、入库贡献者头像与昵称、当前价格与历史/折后价标签、好评率进度条与 Steam 标签云。
- **渲染逻辑**：
  - `CardRenderer`：基于单例模式管理 Playwright `Browser` 实例，提供 `render_game_card(data: dict) -> bytes`。
  - 包含浏览器自动加载失败时的降级方案（返回格式化文本或内置简易绘图兜底），并支持 Playwright 浏览器未安装提示。

### 3.6 `agent/tools.py` 大模型 Function Calling 工具
注册到 AstrBot Agent 上下文的工具函数：
1. `query_family_games(keyword: str = "", tag: str = "", max_price: float = None)`：按游戏名、类型标签、价格区间模糊检索家庭库，返回游戏名称、好评率与归属者。
2. `get_family_stats()`：返回家庭库全览数据（总游戏数、总价值估算、各成员资产与游戏贡献榜）。
3. `roll_game(tag: str = "", min_rating: int = 0)`：为选择困难症玩家随机抽取符合条件的一款游戏及推荐理由。
4. `check_coop_games(members: list[str])`：查询指定开黑成员可以一起玩的联机游戏。

### 3.7 `main.py` AstrBot 插件入口与指令
- 继承 `Star` 类，使用 `@register` 装饰插件元信息。
- **指令集（Command Handlers）**：
  - `/steam_bind <token> [steamid]`：绑定配置鉴权 Token 与用户 ID。
  - `/steam_check`：手动触发一次家庭库检查与增量变动播报。
  - `/steam_sub` / `/steam_unsub`：当前群或私聊订阅/取消订阅变动广播。
  - `/steam rank`：查看家庭组“义父榜”（各成员资产贡献与总估值排行）。
  - `/steam roll [tag/条件]`：赛博轮盘，随机抽取家庭库内游戏。
  - `/steam coop <@成员...>`：开黑车队雷达，分析多人可联机游戏交集。
- **后台周期任务**：
  - 启动异步后台任务（`asyncio.create_task`），按照 `poll_interval_minutes` 定时轮询。
  - 发现新入库游戏时，通过 `renderer.render_game_card` 渲染出 PNG 图片，并自动向已订阅会话推送图文结果。

---

## 4. 单元测试与验证计划
- 编写 `tests/test_diff.py`：
  - 测试空快照初次同步场景（无新增推送）。
  - 测试新入库单款/多款游戏增量识别。
  - 测试移出游戏识别与幂等更新。
  - 测试非游戏（工具/DLC/Demo）黑名单过滤逻辑。
- 编写 `tests/test_storage.py`：
  - 测试 SQLite 异步建表、快照存取与排行榜聚合统计 SQL 查询。
- 本地 Mock 验证脚本与测试用例可直接通过 `uv run pytest` 执行通过。
