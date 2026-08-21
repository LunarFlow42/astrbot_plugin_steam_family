# 🎮 astrbot_plugin_steam_family (Steam 家庭资产管家)

<p align="center">
  <img src="https://raw.githubusercontent.com/AstrBotDevs/AstrBot/master/docs/public/logo_prod.png" width="120" alt="AstrBot" />
</p>

<p align="center">
  <b>基于 AstrBot 框架的现代化 Steam 家庭库资产监控与智能管家插件</b><br>
  ⚡ 自动增量监控 | 🎨 赛博卡片推送 | 👑 义父资产排行 | 🎲 赛博轮盘 | 🚀 车队雷达 | 🌐 独立 Web 大盘 | 🤖 LLM 自然语言找游戏
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white" alt="Python Version" />
  <img src="https://img.shields.io/badge/AstrBot-Plugin%20v4-FF6B6B?style=flat" alt="AstrBot Plugin" />
  <img src="https://img.shields.io/badge/Ruff-Formatted-black?style=flat" alt="Ruff Formatted" />
  <img src="https://img.shields.io/badge/License-MIT-green?style=flat" alt="License" />
</p>

---

## ✨ 核心特性

- 🔄 **全异步秒级增量监控**：基于 Steam 官方新版家庭组 API (`IFamilyGroupsService`)，全异步定时轮询，自适应指数退避，精准识别新入库与移出游戏。
- 🎨 **Playwright 赛博朋克卡片**：Jinja2 + 现代化 Steam 深色玻璃拟物风格，自动抓取并渲染游戏中文名、封面、折扣标签、好评率与入库义父信息。
- 👑 **家庭“义父榜”资产大盘**：多维统计成员游戏数、原价总额与折后现值，一键生成谁是赛博慈善家、谁是白嫖理财大师。
- 🎲 **今天玩什么（赛博轮盘）**：支持按类型标签、好评率从全库随机抽选游戏并给出推荐理由，治愈群友选择困难症。
- 🚀 **开黑车队雷达**：一键计算指定多位开黑成员共同拥有的多人合作游戏交集，聚会开黑不撞车。
- 📊 **Web 全景大盘与远程访问**：内置 Web 看板服务与 `_conf_schema.json`，支持一键切换 `0.0.0.0` 远程/局域网浏览器访问，直观查看全量家庭资产。
- 🤖 **原生 AI Agent 工具调用（LLM Function Calling）**：支持群友使用自然语言与机器人聊天找游戏（*“帮我找个 50 块以下画风治愈的解谜游戏”*）。
- 🛡️ **双模鉴权与自动永久保活**：支持直达链接获取 Token，亦可填入 Cookie 由插件在后台自动静默刷新 Token，永久免维护。

---

## 📦 插件安装

### 方式一：AstrBot WebUI 插件市场 / Git 安装（推荐）
1. 打开 AstrBot Web 控制台，进入 **「插件管理」->「安装插件」**；
2. 在 Git 仓库地址中输入：
   ```text
   https://github.com/LunarFlow42/astrbot_plugin_steam_family
   ```
3. 点击安装，AstrBot 会**全自动解析 `requirements.txt` 完成全部依赖安装**，无需手动执行任何命令。

### 方式二：手动克隆安装
在 AstrBot 根目录下的 `data/plugins/` 目录执行：
```bash
git clone https://github.com/LunarFlow42/astrbot_plugin_steam_family.git data/plugins/astrbot_plugin_steam_family
```

---

## 🚀 30 秒极速上手与凭据获取

### 1. 获取 64 位 SteamID（1 秒直达）
👉 **直达链接**：点击登录 [Steam 账户明细页 (store.steampowered.com/account)](https://store.steampowered.com/account/)  
> 页面最上方直接显示：**`Steam ID: 76561198xxxxxxxx`**，复制这 **17 位纯数字** 即可。

---

### 2. 获取 Steam Access Token（点击直达链接免 F12）

👉 **官方直达接口**：在已登录 Steam 的浏览器中直接点击打开：  
🔗 **[https://store.steampowered.com/pointssummary/ajaxgetasyncconfig](https://store.steampowered.com/pointssummary/ajaxgetasyncconfig)**

页面会直接以 JSON 格式输出：
```json
{
  "success": 1,
  "data": {
    "webapi_token": "eyJhbGciOiJ..."
  }
}
```
> 📌 **操作**：直接复制 `"webapi_token"` 冒号后面的那串以 **`ey`** 开头的长字符串即可！无需开控制台，点开就能直接复制。

---

## ⚙️ 配置与绑定

### 方式 A：WebUI 可视化配置（推荐）
在 AstrBot Web 控制台进入「插件管理」->「插件配置」找到 `Steam 家庭资产管家`，填写以下核心配置：
- **`access_token`**：粘贴上述获取到的 `webapi_token` 字符串。
- **`steam_id`**：填写你的 17 位 Steam 64 位数字 ID。
- **`cookie`**（可选·永久保活）：若填入 Steam 网页端 `steamLoginSecure` Cookie，插件将在 Token 过期时全自动静默刷新，永久保活免维护！
- **`dashboard_host`**：默认 `0.0.0.0`（支持远程/局域网访问）或 `127.0.0.1`（仅本机）。
- **`dashboard_port`**：默认 `6188`。
- **`proxy`**：若服务器位于中国大陆无法直连 Steam API，可配置代理地址（如 `http://127.0.0.1:7890`）。

### 方式 B：聊天指令快捷配置
在机器人所在的聊天中发送：
```text
/steam_bind <你的Token> <你的SteamID>
/steam_sub
/steam_check
```

---

## 📖 完整指令速查表

| 指令 | 说明 | 兼容别名 | 使用示例 |
|---|---|---|---|
| `/steam_check` | 手动触发一次家庭库变动检查与同步 | `/steam check`, `/steam同步`, `/家庭库检查` | `/steam check` |
| `/steam_sub` | 订阅当前会话的家庭库新游戏变动推送 | `/steam sub`, `/steam订阅` | `/steam sub` |
| `/steam_unsub` | 取消当前会话的变动推送订阅 | `/steam unsub`, `/steam退订` | `/steam unsub` |
| `/steam_rank` | 查看家庭组“义父”资产贡献排行榜 | `/steam rank`, `/义父榜`, `/steam排行` | `/steam rank` |
| `/steam_roll [标签]` | 赛博轮盘：随机抽取家庭库内游戏 | `/steam roll`, `/今天玩什么`, `/steam抽奖` | `/steam roll RPG` |
| `/steam_coop <SteamID...>` | 开黑车队雷达：计算多人共有联机游戏 | `/steam coop`, `/开黑雷达`, `/steam联机` | `/steam coop 76561198... 76561198...` |
| `/steam_dashboard` | 获取 Web 资产全景看板直达访问链接 | `/steam dashboard`, `/steam大盘`, `/steam看板` | `/steam dashboard` |
| `/steam_bind <token> [steamid]` | 绑定/更新 Steam 鉴权凭据 | `/steam bind`, `/steam绑定` | `/steam_bind eyJhb... 76561198...` |
| `/steam_help` | 查看插件指令与功能帮助 | `/steam help`, `/steam`, `/steam帮助` | `/steam help` |

---

## 🤖 AI Agent 智能对话示例

- 🗣️ **群友**：*“帮我看看家庭库里有没有评分高于 90% 的肉鸽或者卡牌游戏？”*
  - 🤖 **AstrBot (Agent)**：*自动调用 `query_family_games` 工具，并以幽默口吻推荐《杀戮尖塔》《黑帝斯》及拥有者。*
- 🗣️ **群友**：*“我们家庭库现在总共值多少钱？谁贡献最多？”*
  - 🤖 **AstrBot (Agent)**：*自动调用 `get_family_stats` 工具，播报当前库总原值与大义父排行。*
- 🗣️ **群友**：*“今晚我俩想联机，推荐个游戏呗”*
  - 🤖 **AstrBot (Agent)**：*自动调用 `roll_family_game` 推荐多人联机佳作。*

---

## 💡 致谢与灵感来源 (Acknowledgements & Inspiration)

本项目在开发过程中借鉴了开源社区优秀项目的创意与实现思路，特此致谢：
- 🌟 **[AstrBot 框架](https://github.com/AstrBotDevs/AstrBot)**：强大的跨平台 LLM 机器人与插件开发框架。
- 🌟 **Steam 逆向工程社区（SteamRE / SteamDB 等）**：提供了对 Valve 新版 `IFamilyGroupsService` API 及 Token 提取机制的基础协议逆向支持。
- 🌟 **开源社区同类项目（如 [koishi-steam-family-bot](https://github.com/kt-fun/koishi-steam-family-bot)）**：提供了群聊家庭库监控的场景实践与设计参考。
- 🌟 **Steam 官方 Web API (`IFamilyGroupsService`)**：提供家庭组底层数据支持。

---

## 📄 开源许可证 (License)

本项目基于 [MIT License](LICENSE) 开源。请在使用、修改或衍生本项目代码时保留原作者版权及许可声明。
