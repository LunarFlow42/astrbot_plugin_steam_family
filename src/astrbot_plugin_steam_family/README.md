# 🎮 astrbot_plugin_steam_family (Steam 家庭资产管家)

<p align="center">
  <img src="https://raw.githubusercontent.com/AstrBotDevs/AstrBot/master/docs/public/logo_prod.png" width="120" alt="AstrBot" />
</p>

<p align="center">
  <b>基于 AstrBot 框架的现代化 Steam 家庭库资产监控与智能管家插件</b><br>
  ⚡ 自动增量监控 | 🎨 赛博卡片推送 | 👑 义父资产排行 | 🎲 赛博轮盘 | 🚀 车队雷达 | 🤖 LLM 自然语言找游戏
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white" alt="Python Version" />
  <img src="https://img.shields.io/badge/AstrBot-Plugin-FF6B6B?style=flat" alt="AstrBot Plugin" />
  <img src="https://img.shields.io/badge/License-MIT-green?style=flat" alt="License" />
</p>

---

## ✨ 核心特性

- 🔄 **全异步秒级增量监控**：基于 Steam 官方新版家庭组 API (`IFamilyGroupsService`)，全异步定时轮询，自适应指数退避，精准识别新入库与移出游戏。
- 🎨 **Playwright 赛博朋克卡片**：Jinja2 + 现代化 Steam 深色玻璃拟物风格，自动抓取并渲染游戏中文名、封面、折扣标签、好评率与入库义父信息。
- 👑 **家庭“义父榜”资产大盘**：多维统计成员游戏数、原价总额与折后现值，一键生成谁是赛博慈善家、谁是白嫖理财大师。
- 🎲 **今天玩什么（赛博轮盘）**：支持按类型标签、好评率从全库随机抽选游戏并给出推荐理由，治愈群友选择困难症。
- 🚀 **开黑车队雷达**：一键计算指定多位开黑成员共同拥有的多人合作游戏交集，聚会开黑不撞车。
- 📊 **WebUI 可视化控制台与资产大盘**：内置 `_conf_schema.json`，在 AstrBot WebUI 后台即可一键可视化配置 Token、轮询间隔与推送开关。
- 🤖 **原生 AI Agent 工具调用（LLM Function Calling）**：支持群友使用自然语言与机器人聊天找游戏（*“帮我找个 50 块以下画风治愈的解谜游戏”*）。

---

## 🚀 30 秒极速上手指南

### 1. 安装插件
在 AstrBot 插件市场搜索 `astrbot_plugin_steam_family` 点击安装，或在插件目录克隆代码：
```bash
cd data/plugins
git clone https://github.com/AstrBotDevs/astrbot_plugin_steam_family.git
```

### 2. 获取 Steam Access Token（30 秒获取）
1. 在电脑浏览器打开并登录 [Steam 商店主页](https://store.steampowered.com/)；
2. 按 `F12` 键打开浏览器开发者工具，切换到 **控制台 (Console)**；
3. 粘贴以下代码并回车：
```javascript
console.log("你的 Steam Access Token:\n" + JSON.parse(document.getElementById("application_config").getAttribute("data-loyalty_webapi_token")));
```
4. 复制输出的长字符串即为你的 `access_token`。

### 3. 配置与绑定
- **方式 A（推荐 · WebUI 可视化配置）**：
  打开 AstrBot Web 控制台（默认 `http://localhost:6180`），进入「插件管理」->「插件配置」找到 `astrbot_plugin_steam_family`，填入 `access_token` 和你的 17 位 `steam_id` 保存即可。
- **方式 B（聊天指令配置）**：
  在聊天群或私聊中向机器人发送：
  ```text
  /steam_bind <你的Token> <你的SteamID>
  /steam_sub
  /steam_check
  ```

---

## 📖 指令速查表

| 指令 | 说明 | 使用示例 |
|---|---|---|
| `/steam_bind <token> [steamid]` | 绑定 Steam 鉴权凭据 | `/steam_bind eyJhb... 76561198000000001` |
| `/steam_check` | 手动触发一次家庭库变动检查与播报 | `/steam_check` |
| `/steam_sub` | 订阅当前会话的家庭库新游戏推送 | `/steam_sub` |
| `/steam_unsub` | 取消当前会话的推送订阅 | `/steam_unsub` |
| `/steam rank` | 查看家庭组“义父”资产贡献排行榜 | `/steam rank` |
| `/steam roll [标签]` | 赛博轮盘：随机抽取家庭库内游戏 | `/steam roll RPG` 或 `/steam roll 动作` |
| `/steam coop <@SteamID...>` | 开黑车队雷达：计算多人共有游戏 | `/steam coop 76561198... 76561198...` |

---

## 🤖 AI Agent 智能对话示例

本插件深度接入 AstrBot Function Calling，无需死记指令，直接在群内用自然语言提问：

- 🗣️ **群友**：*“帮我看看家庭库里有没有评分高于 90% 的肉鸽或者卡牌游戏？”*
  - 🤖 **AstrBot (Agent)**：*自动调用 `query_family_games` 工具，并以幽默口吻推荐《杀戮尖塔》《黑帝斯》及入库拥有者。*
- 🗣️ **群友**：*“我们家庭库现在总共值多少钱？谁贡献最多？”*
  - 🤖 **AstrBot (Agent)**：*自动调用 `get_family_stats` 工具，播报当前库总原值与大义父排行。*
- 🗣️ **群友**：*“今晚我俩想联机，推荐个游戏呗”*
  - 🤖 **AstrBot (Agent)**：*自动调用 `roll_family_game` 推荐多人联机佳作。*

---

## 🧪 开发者与测试

本项目使用现代 `uv` 工程管理，可在本地一键运行完整单元测试与仿真：

```bash
# 运行全部单元测试
uv run --with pytest --with pytest-asyncio pytest -v

# 运行全链路端到端仿真脚本（生成卡片与大盘预览）
uv run python scripts/demo_mock_run.py
```

---

## 📄 开源许可证

本项目基于 [MIT License](LICENSE) 开源。
