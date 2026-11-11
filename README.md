# 🎮 astrbot_plugin_steam_family (Steam 家庭资产管家)

<p align="center">
  <img src="https://raw.githubusercontent.com/AstrBotDevs/AstrBot/master/docs/public/logo_prod.png" width="120" alt="AstrBot" />
</p>

<p align="center">
  <b>基于 AstrBot 框架的现代化 Steam 家庭库资产监控与智能管家插件</b><br>
  ⚡ 官方口径秒级增量监控 | 🎨 原生赛博卡片推送 | 👑 义父资产排行 | 🎲 赛博轮盘 | 🚀 车队雷达 | 🖥️ 原生内置 WebUI | 🤖 1000+ 全库 LLM 智能找游戏
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white" alt="Python Version" />
  <img src="https://img.shields.io/badge/AstrBot-Plugin%20v4-FF6B6B?style=flat" alt="AstrBot Plugin" />
  <img src="https://img.shields.io/badge/Ruff-Formatted-black?style=flat" alt="Ruff Formatted" />
  <img src="https://img.shields.io/badge/License-MIT-green?style=flat" alt="License" />
</p>

---

## ✨ 核心特性

- 🔄 **官方口径秒级增量监控**：基于 Steam 官方新版家庭组 API (`IFamilyGroupsService`)，精准对齐 Steam 客户端分类（Included / Excluded / Free / Hidden），识别新入库、移出与共享属性变化。
- 🛡️ **防刷屏智能聚合安全阀**：单次大量入库/移出/状态变动时（>5款）自动聚合成清爽的一句话汇总通知，绝不疯狂发卡片轰炸群聊。
- 🎨 **无边框赛博卡片渲染**：基于 AstrBot 原生 `html_render` 与 Jinja2 现代化 Steam 深色玻璃拟物风格，自动去除外部黑边，极速渲染中文游戏名、海报封面、真实中文好评率与真实入库义父昵称。
- 👑 **家庭“义父榜”资产大盘**：多维统计成员游戏数、原价总额与折后现值，一键生成谁是赛博慈善家、谁是白嫖理财大师。
- 📉 **家庭库降价监控**：每 20 小时自动巡检一次家庭库游戏的商店实时价格，检测到降价或折扣加深即向群聊推送降价喜报。
- 🎲 **今天玩什么（赛博轮盘）**：支持按类型标签、好评率从全库随机抽选游戏并给出推荐理由，治愈群友选择困难症。
- 🚀 **开黑车队雷达**：一键计算指定多位开黑成员共同拥有的多人合作游戏交集，聚会开黑不撞车。
- 🖥️ **AstrBot 原生内置 WebUI（Plugin Pages）**：无需额外放行任何端口！直接在 AstrBot 管理面板点击插件卡片上的 **「🖥️ 打开插件UI界面」** 图标，即可呼出全景大盘与可视化数据库测试中心。
- 🤖 **1000+ 款全库 LLM 智能荐游（/steam_ai）**：大模型拥有全库 1000+ 款游戏全景视野，支持自然语言深度交互（*“找个能叫上好兄弟无脑突突突割草的解压游戏”*），杜绝“误把库内游戏当库外”的幻觉。
- 🔑 **"永不掉线" 全自动保活体系**：支持 `/steam_qr` 扫码免密登录（200 天长效令牌 + `renewal_type=1` 每日滚动无限续杯），进阶可填入账号密码 + `shared_secret`（2FA 秘钥）实现本地算码全自动重登（ASF 同款，100% 永不掉线）。

---

## 📦 插件安装

### 方式一：AstrBot Web 控制台 Git 安装（推荐）
1. 打开 AstrBot Web 控制台，进入 **「插件管理」->「安装插件」**；
2. 在 Git 仓库地址中输入：
   ```text
   https://github.com/LunarFlow42/astrbot_plugin_steam_family
   ```
3. 点击安装，AstrBot 会**全自动解析依赖完成安装**，无需手动执行任何命令。

### 方式二：克隆源码到插件目录（或创建软链接）
在 AstrBot 根目录下的 `data/plugins/` 目录执行：
```bash
git clone https://github.com/LunarFlow42/astrbot_plugin_steam_family.git data/plugins/astrbot_plugin_steam_family
```

---

## 🚀 登录与保活指引

本插件支持两种免密与自动保活方案，**无需在 WebUI 页面寻找繁琐的登录按钮**：

### 方案 1：聊天发送指令扫码登录（最推荐，一步到位）
1. 在与机器人私聊或群聊中发送：
   ```text
   /steam_qr
   ```
2. 机器人会直接在聊天框发送 **手机 Steam App 的专属登录二维码与链接**；
3. 用手机 Steam App 扫码并确认登录，插件即可自动拿到 200 天长效 Token，并**自动激活每日滚动续期（无限续杯）**。

### 方案 2：在 AstrBot Web 控制台配置（路线一：账号密码 + 2FA 秘钥）
1. 进入 AstrBot 管理控制台 -> **「插件管理」** -> 找到 **`Steam 家庭资产管家`** 卡片；
2. 点击卡片上的 **⚙️ 配置（小齿轮）**；
3. 填入：
   - `account_name`：Steam 账号名
   - `password`：Steam 密码
   - `shared_secret`：从 `.maFile` / ASF 提取的 Base64 令牌秘钥
   - `steam_id`：你的 17 位 SteamID（可在 [Steam 账户明细页](https://store.steampowered.com/account/) 查看）
4. 保存配置后，机器人在每次 Token 失效或被踢下线时，都会在后台自动算出 5 位动态验证码重新登录（ASF 同款，100% 永生）。

---

## 📖 完整指令速查表

| 指令 | 说明 | 兼容别名 | 使用示例 |
|---|---|---|---|
| `/steam_qr` | 📱 扫码登录：获取 200 天令牌并自动开启滚动续期 | `/steam qr`, `/steam扫码` | `/steam_qr` |
| `/steam_ai <需求>` | 🤖 全库大模型智能导购荐游（杜绝库外幻觉） | `/steam ai`, `/steam推荐` | `/steam_ai 推荐两款联机割草神作` |
| `/steam_check` | 立即手动触发一次家庭库变动检查与同步 | `/steam check`, `/steam同步`, `/家庭库检查` | `/steam check` |
| `/steam_sub` | 订阅当前会话的家庭库新游戏变动推送 | `/steam sub`, `/steam订阅` | `/steam sub` |
| `/steam_unsub` | 取消当前会话的变动推送订阅 | `/steam unsub`, `/steam退订` | `/steam unsub` |
| `/steam_rank` | 查看家庭组“义父”资产贡献排行榜（显示真实昵称） | `/steam rank`, `/义父榜`, `/steam排行` | `/steam rank` |
| `/steam_roll [标签]` | 赛博轮盘：随机抽取家庭库内游戏 | `/steam roll`, `/今天玩什么`, `/steam抽奖` | `/steam roll 肉鸽` |
| `/steam_coop <SteamID...>` | 开黑车队雷达：计算多人共有联机游戏 | `/steam coop`, `/开黑雷达`, `/steam联机` | `/steam coop 76561198... 76561198...` |
| `/steam_code` | 🔑 实时计算当前 5 位 Steam 令牌动态验证码（私聊有效） | `/steam code`, `/steam令牌` | `/steam_code` |
| `/steam_refresh` | 🛢️ 手动执行一次 Refresh Token 滚动续期 | `/steam refresh`, `/steam续期` | `/steam_refresh` |
| `/steam_relogin` | 🔐 立即触发账号密码+2FA秘钥重登 | `/steam relogin`, `/steam重登` | `/steam_relogin` |
| `/steam_cred <账号> <密码> [秘钥]` | 保存路线一保活凭据（私聊有效） | `/steam cred`, `/steam凭据` | `/steam_cred myacct Pass! cWFh...` |
| `/steam_bind <Token> [SteamID]` | 手动绑定/更新 Access Token | `/steam bind`, `/steam绑定` | `/steam_bind eyJhb... 76561198...` |
| `/steam_dashboard` | 查看 Web 看板开启与使用指引 | `/steam dashboard`, `/steam大盘` | `/steam dashboard` |
| `/steam_help` | 查看插件指令与功能帮助 | `/steam help`, `/steam`, `/steam帮助` | `/steam help` |

---

## ❓ 常见问题与对账说明 (FAQ)

### 1. 为什么 Steam 客户端显示的数目和插件完全一致？
插件底层请求严格对齐了 Steam 客户端的家庭共享协议字段：
- **Included Games (可共享)**：`app_type=1` 且 `exclude_reason=0`（已排除开发工具、专用服务器等），数量与官方客户端完全一致；
- **Excluded Games (已排除)**：因号主设为私密、发行商不支持或许可证原因被排除的游戏；
- **Free Games (免费游戏)**：Dota2、CS2、TF2 等免费游玩项目；
- **Hidden (隐藏)**：号主本人在客户端右键“隐藏/设为私密”的游戏。

### 2. 大量入库或首次同步时会疯狂刷屏吗？
不会！插件内置了**防刷屏智能聚合安全阀**：
- 当单次新增超过 5 款时，会自动聚合成一条批量入库摘要（例如包含《xxx》《yyy》等共 N 款），不逐条发图；
- 当检测到归属人或状态批量更新（>5款）时，同样自动聚合，保持群聊整洁清爽。

### 3. 超大库存家庭组（数千款游戏）使用 `/steam_ai` 会消耗很多 Token 吗？
为了彻底杜绝大模型“误把库内游戏当库外”的幻觉，插件会将当前家庭库的**完整游戏名清单全量提供给大模型**：
- **Token 占用极低**：纯游戏名文本非常紧凑，**1000 款游戏仅占用约 1.1 万 ~ 1.4 万 Token**（仅占现代主流模型 128k~1000k 上下文窗口的不到 10%，调用成本仅约一两分钱）；
- **建议**：如果你的家庭库游戏规模达到数千款以上，建议配置支持较大上下文窗口的模型（如 DeepSeek-V3 / Qwen / GPT-4o / Claude 3.5 / Gemini 等），即可畅享无死角的全库智能游戏推荐。

---

## 📄 开源许可证 (License)

本项目基于 [MIT License](LICENSE) 开源。
