# 🎮 astrbot_plugin_steam_family (Steam 家庭资产管家)

<p align="center">
  <img src="https://raw.githubusercontent.com/AstrBotDevs/AstrBot/master/docs/public/logo_prod.png" width="120" alt="AstrBot" />
</p>

<p align="center">
  <b>基于 AstrBot 框架的现代化 Steam 家庭库资产监控与智能管家插件</b><br>
  ⚡ 自动增量监控 | 🎨 赛博卡片推送 | 👑 义父资产排行 | 🎲 赛博轮盘 | 🚀 车队雷达 | 🖥️ 原生内置 WebUI | 🤖 LLM 自然语言找游戏
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
- 🎨 **原生赛博朋克卡片渲染**：基于 AstrBot 原生 `html_render` 与 Jinja2 现代化 Steam 深色玻璃拟物风格，免装任何重型浏览器依赖，极速渲染游戏中文名、海报封面、好评率与入库义父信息。
- 👑 **家庭“义父榜”资产大盘**：多维统计成员游戏数、原价总额与折后现值，一键生成谁是赛博慈善家、谁是白嫖理财大师。
- 🎲 **今天玩什么（赛博轮盘）**：支持按类型标签、好评率从全库随机抽选游戏并给出推荐理由，治愈群友选择困难症。
- 🚀 **开黑车队雷达**：一键计算指定多位开黑成员共同拥有的多人合作游戏交集，聚会开黑不撞车。
- 🖥️ **AstrBot 原生内置 WebUI（Plugin Pages）**：无需额外手动开启或放行任何端口！直接在 AstrBot 管理面板点击插件卡片上的 **「🖥️ 打开插件UI界面」** 图标，即可一键呼出全景大盘与可视化数据库测试中心。
- 🤖 **原生 AI Agent 工具调用（LLM Function Calling）**：支持群友使用自然语言与机器人聊天找游戏（*“帮我找个 50 块以下画风治愈的解谜游戏”*）。
- 🛡️ **"永不掉线" 全自动保活**：支持 `/steam_qr` 扫码登录（200 天令牌 + `renewal_type=1` 每日滚动续期无限续杯），进阶可填入账号密码 + `shared_secret`（2FA 秘钥）实现断线后本地算码全自动重登（ASF 同款，100% 永不掉线），另有 Cookie 静默刷新兜底。

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

### 3. 更简单的方式：手机扫一次码（任选）
不想折腾 Token？直接在聊天里发送 **`/steam_qr`**，用手机 Steam App 扫一下二维码并确认，插件即可拿到与手机 App 完全同级、200 天寿命的 `refresh_token` + `access_token`，并**自动开启每日滚动续期**。从此无需再手动更新任何 Token！

---

## 🛡️ "永不掉线" 全自动保活体系（v0.2.0 新增）

手机 Steam App 可以几年不掉线，靠的是 **滚动续期（Rolling Renewal）** 与 **本地 2FA 秘钥（`shared_secret`）**。本插件完整复刻了这两条机制，双路线保活：

| 路线 | 需要准备 | 保活效果 | 什么情况会掉线 |
|---|---|---|---|
| **路线一：2FA 秘钥全自动重登**（ASF 同款） | Steam 账号名 + 密码 + `shared_secret` | **100% 永不掉线** | 只有你主动改 Steam 密码 / 解绑手机令牌 |
| **路线二：扫码 + 滚动续期**（无限续杯） | 手机扫一次码（`/steam_qr`） | **准永久**（每天运行即无限顺延 200 天） | 插件停机超半年，或在手机端"注销所有其他设备" |

### 路线二：伪装手机 App 扫码 + `renewal_type=1` 滚动续期（默认推荐）
- 扫码会话以 `k_EAuthTokenPlatformType_MobileApp`（手机端）发起，令牌 audience 为 `['web','mobile']`，比网页端更抗风控；
- 插件每次登录前都会用缓存的 `refresh_token` 调用 `GenerateAccessTokenForApp` 兑换新 Access Token，并携带 **`renewal_type=1`**：一旦 Steam 判定当前 200 天令牌剩余寿命需要顺延，会直接在返回体中夹带一条**寿命重置为 200 天的全新 `refresh_token`**，插件立即持久化保存，实现"无限续杯"；
- 新令牌会自动回写 AstrBot 配置 + 插件 SQLite KV 双备份，重启后依然有效。

### 路线一：账号密码 + `shared_secret` 全自动重登（100% 永生兜底）
当 `refresh_token` 彻底失效（200 天到期 / Steam 抽风强制踢下线）时，插件自动执行 ASF 同款流程：
1. 调用 `IAuthenticationService/GetPasswordRSAPublicKey/v1` 获取 RSA 公钥，**本地** PKCS#1 v1.5 加密密码（纯 Python 实现，密码不出明文）；
2. `BeginAuthSessionViaCredentials/v1` 发起登录；
3. 填入配置的 `shared_secret`，本地 HMAC-SHA1 **直接算出当前 5 位 Steam 令牌验证码**（完全不需要看手机）；
4. `UpdateAuthSessionWithSteamGuardCode/v1` 自动填码，`PollAuthSessionStatus` 领取全新 200 天 `refresh_token`。

> **如何获取 `shared_secret`？**
> - 令牌绑在 **ASF / SDA** 上：打开 `.maFile` 即可见 `"shared_secret": "xxxxxx="`；
> - 绑在**已 Root 的安卓机**（Magisk/KernelSU）上：明文保存在 `/data/data/com.valvesoftware.android.steam.community/files/Steamguard-你的SteamID` 中。

> ⚠️ 所有保活流程均带失败退避冷却（滚动续期 15 分钟 / 密码重登 30 分钟 / Cookie 15 分钟），不会对 Steam 疯狂重试触发风控。

---

## 🔔 监控可靠性（0.3.0）

家庭库变动监控在此版本补齐了完整的运营与观测能力，重试不丢事件、失败可退避、状态可观测：

- **单飞锁（Single-flight）**：聊天指令 / WebUI 同步按钮 / 后台轮询三入口并发触发时自动合并为一次执行，从源头消灭重复拉取与重复推送竞态。
- **事件重试队列**：所有变动事件先持久化到 `pending_notifications` 表再播报；推送失败按指数退避（最长 1 小时）自动重试，进程重启后补播，绝不丢通知。
- **移出与归属变更播报**：新游戏入库卡片之外，游戏被移出家庭库（含"曾归属义父"）、归属义父增减、退款/共享状态变化均有独立文案播报；移出数量异常时会触发安全阀静默处理，防止刷屏。
- **批量商店详情**：新入库游戏批量拉取 `price_overview,basic,genres,categories`（chunk ≤ 20，实测仅该形态多 appid 可用），单发兜底 + 429 自动熔断。
- **真实好评率**：卡片好评率来自 `appreviews` 真实评价摘要（好评如潮/特别好评简述 + 精确百分比），无评价数据时中性展示，绝不虚构。
- **📉 家庭库降价监控**：每 20 小时自动巡检一次家庭库游戏的商店实时价格（单轮最多 120 款，只对比本地已有价格记录的游戏），检测到**降价或折扣加深**即向订阅会话推送降价提醒，并同步刷新看板估值缓存。

  降价提醒示例输出：
  ```text
  📉 降价提醒：《Hades II》 ¥199.0 → ¥99.0 (折扣 50%)
  ```
- **游玩统计接线**：接入此前未使用的 `GetPlaytimeSummary` 接口，WebUI 大盘新增"🎮 最近游玩"面板（成员、游戏、总时长、最后游玩时间），游玩数据获取失败不影响检查主流程。
- **快照修剪**：历史快照自动修剪（最新 200 条永保 + 超过 30 天的删除），防止数据库无限膨胀。
- **自适应轮询退避**：连续检查失败时轮询间隔按 `base * min(4, 2^n)` 指数倍增（最长 4 倍），成功后自动恢复，避免对故障状态的 Steam 疯狂重试。
- **同步状态观测**：最近一次成功同步时间 / 耗时 / 结果消息 / 失败标记持久化在 `sync_*` 观测键中，WebUI 顶部实时显示"🕘 最后同步 X 分钟前"徽标。

---

## ⚙️ 配置与绑定

### 方式 A：WebUI 可视化配置（推荐）
在 AstrBot Web 控制台进入「插件管理」->「插件配置」找到 `Steam 家庭资产管家`，填写以下核心配置：
- **`access_token`**：粘贴上述获取到的 `webapi_token` 字符串。
- **`steam_id`**：填写你的 17 位 Steam 64 位数字 ID。
- **`enable_price_watch`**（默认开启）：家庭库游戏降价监控，每 20 小时自动巡检一次并在降价/折扣加深时推送提醒。
- **`cookie`**（可选·保活兜底）：填入 Steam 网页端 `steamLoginSecure` Cookie，作为保活链路的最终兜底。
- **全自动保活（可选但强烈推荐）**：
  - 路线二：聊天发送 `/steam_qr` 扫码即可，无需任何手动配置；
  - 路线一：填写 **`account_name`（账号名）+ `password`（密码）+ `shared_secret`（2FA 秘钥）**，达成 100% 永不掉线；
- **`proxy`**：若服务器位于中国大陆无法直连 Steam API，可配置代理地址（如 `http://127.0.0.1:7890`）。

### 方式 B：聊天指令快捷配置
在机器人所在的聊天中发送：
```text
/steam_qr              # 手机扫码登录（推荐，一步到位）
/steam_bind <Token> <SteamID>
/steam_cred <账号名> <密码> <shared_secret>   # 路线一全自动重登凭据
/steam_sub
/steam_check
```

---

## 🖥️ WebUI 内置全景大盘与测试中心 (Plugin Pages)

进入 AstrBot Web 管理控制台的「插件管理」页面：
1. 找到 **`Steam 家庭资产管家`** 插件卡片；
2. 点击卡片上的 **「🖥️ 打开插件UI界面」** 图标按钮；
3. 即可在 AstrBot 控制台内部直接打开内置大盘：
   - ⚡ **一键手动同步检查**：网页端实时触发数据拉取；
   - 👑 **义父资产贡献榜**：多维度排名与贡献估值；
   - ✨ **家庭库游戏封面预览**：精美海报与好评率一览；
   - 🛠️ **本地数据库调试中心**：支持可视化搜索快照，**一键移出某款游戏以快速测试新游戏入库推送**！

---

## 📖 完整指令速查表

| 指令 | 说明 | 兼容别名 | 使用示例 |
|---|---|---|---|
| `/steam_qr` | 📱 扫码登录：获取 200 天令牌并自动开启滚动续期 | `/steam qr`, `/steam扫码` | `/steam_qr` |
| `/steam_relogin` | 🔐 账号密码+2FA秘钥 全自动重登（ASF 同款） | `/steam relogin`, `/steam重登` | `/steam_relogin` |
| `/steam_refresh` | 🛢️ 手动执行一次 Refresh Token 滚动续期 | `/steam refresh`, `/steam续期` | `/steam_refresh` |
| `/steam_code` | 🔑 显示当前 5 位 Steam 令牌动态验证码 | `/steam code`, `/steam令牌` | `/steam_code` |
| `/steam_cred <账号> <密码> [秘钥]` | 保存路线一全自动重登凭据 | `/steam cred`, `/steam凭据` | `/steam_cred myacct Pass! cWFh...` |
| `/steam_check` | 手动触发一次家庭库变动检查与同步 | `/steam check`, `/steam同步`, `/家庭库检查` | `/steam check` |
| `/steam_sub` | 订阅当前会话的家庭库新游戏变动推送 | `/steam sub`, `/steam订阅` | `/steam sub` |
| `/steam_unsub` | 取消当前会话的变动推送订阅 | `/steam unsub`, `/steam退订` | `/steam unsub` |
| `/steam_rank` | 查看家庭组“义父”资产贡献排行榜 | `/steam rank`, `/义父榜`, `/steam排行` | `/steam rank` |
| `/steam_roll [标签]` | 赛博轮盘：随机抽取家庭库内游戏 | `/steam roll`, `/今天玩什么`, `/steam抽奖` | `/steam roll RPG` |
| `/steam_coop <SteamID...>` | 开黑车队雷达：计算多人共有联机游戏 | `/steam coop`, `/开黑雷达`, `/steam联机` | `/steam coop 76561198... 76561198...` |
| `/steam_dashboard` | 查看 Web 看板开启与使用指引 | `/steam dashboard`, `/steam大盘`, `/steam看板` | `/steam dashboard` |
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

## ❓ 常见问题与说明 (FAQ)

### 一、关于“怎么登录？为什么 UI 没有登录按钮？”

**登录方式有两种，不需要在 UI 点复杂的按钮：**

#### 方式 1：私聊或群聊发指令 `/steam_qr`（最推荐，免密扫码）
1. 在机器人私聊或群里直接发送：
   ```text
   /steam_qr
   ```
2. 机器人会直接在聊天框发送 **手机 Steam App 的专属登录二维码与链接**；
3. 用手机 Steam 扫码并确认登录，插件就会自动拿到 200 天长效 Token 并激活每日滚动自动续期（无限续杯）。

#### 方式 2：在 AstrBot Web 控制台配置（路线一：账号密码 + 2FA 秘钥）
1. 进入 AstrBot 管理控制台 -> **「插件管理」** -> 找到 **`Steam 家庭资产管家`** 卡片；
2. 点击卡片上的 **⚙️ 配置（小齿轮）**；
3. 填入：
   - `account_name`：Steam 账号名
   - `password`：Steam 密码
   - `shared_secret`：从 `.maFile` / ASF 提取的 Base64 令牌秘钥
   - `steam_id`：你的 17 位 SteamID
4. 保存配置后，机器人在每次 Token 失效或被踢下线时，都会在后台自动算出 5 位动态验证码重新登录，完全免人工操作。

---

### 二、关于“删除游戏提示无法连接到后端接口”的修复

#### 根本原因定位
1. **参数提取遗漏**：AstrBot 前端通过 Bridge 发起 POST 请求时，参数并未放入原始 `*args` 或 `query` 中，而是绑定在 AstrBot 的标准请求代理对象 `request.json()` 中。之前后端在判断参数时漏掉了这一异步读取分支，导致后端接收不到 `appid`，抛出了 400 错误。
2. 前端看板在捕获到 400 失败时给出了通用的 *“无法连接到后端接口”* 提示。

#### 修复与同步
已修复 `main.py` 的 `web_api_remove_app`，使其优先从 `astrbot.api.web.request.json()` 提取 JSON 体中的 `appid`，并同步更新到了 AstrBot 生产运行目录：
- 测试通过 Starlette / AstrBot Dashboard Bridge 标准路由模拟：
  ```http
  POST /api/v1/plugins/extensions/astrbot_plugin_steam_family/db/remove_app
  Body: {"appid": 1086940}
  -> 响应状态码 200/404 正常交互，参数解析 100% 成功！
  ```

#### 立即生效步骤
在 AstrBot 管理面板的「插件管理」页面中，点击本插件右上角的 **「🔄 重载插件」**（或重启 AstrBot），然后再去看板的测试管理中心删除游戏即可正常操作。

---

## 💡 致谢与灵感来源 (Acknowledgements & Inspiration)

本项目在开发过程中借鉴了开源社区优秀项目的创意与实现思路，特此致谢：
- 🌟 **[AstrBot 框架](https://github.com/AstrBotDevs/AstrBot)**：强大的跨平台 LLM 机器人与插件开发框架。
- 🌟 **Steam 逆向工程社区（SteamRE / SteamDB 等）**：提供了对 Valve 新版 `IFamilyGroupsService` API 及 Token 提取机制的基础协议逆向支持。
- 🌟 **[ArchiSteam Farm (ASF)](https://github.com/JustArchiNET/ArchiSteamFarm) / [node-steam-session](https://github.com/DoctorMcKay/node-steam-session)**：本插件"永不掉线"保活体系（`IAuthenticationService` 登录协议、`renewal_type` 滚动续期、SteamGuard 算码）的协议参考与灵感来源。
- 🌟 **开源社区同类项目（如 [koishi-steam-family-bot](https://github.com/kt-fun/koishi-steam-family-bot)）**：提供了群聊家庭库监控的场景实践与设计参考。
- 🌟 **Steam 官方 Web API (`IFamilyGroupsService`)**：提供家庭组底层数据支持。

---

## 📡 数据出站与外部依赖披露（隐私透明化）

本插件运行时会与以下外部服务通信，部署前请确认网络策略与隐私预期：

| 域名 | 用途 | 发送内容 |
|---|---|---|
| `api.steampowered.com` | 家庭组/鉴权/登录协议 | 账号名、RSA 加密后的密码（不见明文）、令牌 |
| `store.steampowered.com` | 游戏详情/评价/价格 | AppID 查询；仅在配置了 Cookie 时携带 Cookie |
| `steamcommunity.com` | 昵称/头像 XML 兜底 | SteamID |
| `*.steamstatic.com` | 头像与封面图 | 仅图片拉取 |
| `api.qrserver.com` | 扫码登录二维码渲染 | 一次性 s.team 登录链接（失败时自动降级为文字链接） |
| `t2i.soulter.top` | （AstrBot 框架行为）卡片 html_render 截图 | 模板与卡片数据（可在框架内改用本地渲染） |

另请注意：**账号密码 / `shared_secret` / 各类令牌会以明文形式**存储在 AstrBot 配置文件与插件 SQLite 数据库（`data/plugin_data/astrbot_plugin_steam_family/`，目录权限已自动收紧为 `700`、库文件 `600`）中。请确保宿主机目录访问可控；如需更高强度，可对 `data/` 目录启用磁盘加密。

---

## 📄 开源许可证 (License)

本项目基于 [MIT License](LICENSE) 开源。请在使用、修改或衍生本项目代码时保留原作者版权及许可声明。
