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
- 🔑 **授权复用与保守重登**：支持私聊 `/steam_qr` 登录，优先复用已有 Refresh Token / Cookie；参照 ASF 的错误处理思路保存设备授权、限制登录频率、在安全限制时暂停。自动密码重登默认关闭，不承诺固定令牌寿命或免受 Steam 风控。

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

> **账号安全提醒**：扫码和密码登录都会创建真实的授权会话。新设备、代理/机房出口、地区变化等可能触发 Steam 安全检查。ASF、浏览器和本插件都不能保证“零风险”。账号出现疑似被盗/安全限制时，应先停用插件的登录功能，在 [Steam 官方支持](https://help.steampowered.com/) 核查处理，不要连续扫码、换节点或重复输入密码。

本插件提供以下登录方式。登录、续期及凭据管理指令仅限 AstrBot 管理员；二维码和密码操作请在私聊或插件配置页进行。

### 方案 1：手动绑定已有 Access Token（不在插件中发起设备登录）
在已经登录 Steam 的浏览器中打开 [Steam 配置接口](https://store.steampowered.com/pointssummary/ajaxgetasyncconfig)，将 `data.webapi_token` 填入插件配置的 `access_token`，并填写 `steam_id`。也可以私聊 `/steam_bind <Token> [SteamID]`。

此方式不在插件中调用扫码/密码登录接口，但 Token 仍是敏感凭据，不等于匿名访问或“绝对安全”；过期后需重新绑定，或使用已有会话续签。

### 方案 2：管理员私聊扫码登录
1. 在与机器人私聊中发送：
   ```text
   /steam_qr
   ```
2. 机器人会在本地生成二维码并发送官方 `https://s.team/q/...` 链接，不再将登录链接发送到第三方二维码服务；
3. 手机 Steam App 扫码后，务必核对设备名称和登录位置。只有确认是本人发起且账号无安全限制时才批准；
4. 新登录默认使用 WebBrowser 平台及固定设备名称，成功后保存 Refresh Token / Access Token。网页令牌通过 Steam `finalizelogin` 复用会话；已有旧移动端令牌仍兼容 `GenerateAccessTokenForApp`。是否续期以及令牌有效期由 Steam 决定。

### 方案 3：配置账号密码和 2FA 密钥（可选）
1. 进入 AstrBot 管理控制台 -> **「插件管理」** -> 找到 **`Steam 家庭资产管家`** 卡片；
2. 点击卡片上的 **⚙️ 配置（小齿轮）**；
3. 填入：
   - `account_name`：Steam 账号名
   - `password`：Steam 密码
   - `shared_secret`：从 `.maFile` / ASF 提取的 Base64 令牌秘钥
   - `steam_id`：你的 17 位 SteamID（可在 [Steam 账户明细页](https://store.steampowered.com/account/) 查看）
4. 保存配置不会立即登录；私聊 `/steam_relogin` 可手动验证。需要手机/邮件确认时按 Steam 提示操作；需要手动填写的邮件验证码请改用扫码流程。
5. `enable_auto_relogin` **默认关闭**。显式开启后，仅在旧授权确认过期/撤销时才尝试密码重登；临时网络错误、拒绝访问、限流不会立即升级为新设备登录。密码或验证码被拒绝、需人工确认时暂停自动重登。

#### 认证安全机制与边界

- **固定身份**：使用持久化 `auth_device_name`（默认宿主机名）和真实宿主系统类型，不随机伪造 Windows 主机/手机型号；保持可信且稳定的网络出口。
- **显式网络配置**：认证和家庭库 HTTP 请求统一使用插件 `proxy`，不自动读取环境变量代理；透明代理/TUN 仍由宿主网络决定。请确保登录与后续请求走同一个可信出口。
- **记住授权**：保存 Steam 返回的 `new_guard_data`，后续仅对同一账号复用 `guard_data`。这是 Steam 签发的授权，不是伪造硬件 ID；Steam 不保证所有平台都返回此字段。
- **串行与退避**：扫码、密码、Refresh Token 和 Cookie 刷新互斥；新登录至少间隔 60 秒，认证限流至少等待 25 分钟，并遵守更长的 `Retry-After`。冷却及失败计数持久化，重启和强制刷新不会绕过。
- **安全暂停**：遇到 `AccountLockedDown`、`RestrictedDevice`、IP 登录限制等显式安全错误时停止认证与同步。管理员在官方渠道处理后，可私聊 `/steam_auth_resume` 解除**本地暂停**，该命令不会解除 Steam 官方限制，也不会发起新登录。
- **并非完整 ASF 客户端**：ASF 使用 SteamKit2 已认证的 CM 连接。本插件使用 HTTP WebAPI，不能仅更换 `platform_type` 或请求头就模拟 ASF；不支持直接复用 SteamClient 平台令牌的 CM 登录/续签。

> 升级前已显式配置的 `enable_auto_relogin=true` 会保留，请自行检查是否需要关闭。所有认证测试均使用假凭据和模拟响应；未拿受限账号做实际登录验证。

#### 凭据存储

密码、`shared_secret`、Token 与设备授权会以明文保存在 AstrBot 配置或插件 SQLite KV 中。请限制 `data/config/` 和 `data/plugin_data/astrbot_plugin_steam_family/` 的访问及备份权限。不要将配置文件、登录二维码或令牌贴到群聊、Issue 或日志中。

---

## 📖 完整指令速查表

| 指令 | 说明 | 兼容别名 | 使用示例 |
|---|---|---|---|
| `/steam_qr` | 📱 管理员私聊扫码登录，核对设备和位置 | `/steam qr`, `/steam扫码` | `/steam_qr` |
| `/steam_ai <需求>` | 🤖 全库大模型智能导购荐游（杜绝库外幻觉） | `/steam ai`, `/steam推荐` | `/steam_ai 推荐两款联机割草神作` |
| `/steam_check` | 立即手动触发一次家庭库变动检查与同步 | `/steam check`, `/steam同步`, `/家庭库检查` | `/steam check` |
| `/steam_sub` | 订阅当前会话的家庭库新游戏变动推送 | `/steam sub`, `/steam订阅` | `/steam sub` |
| `/steam_unsub` | 取消当前会话的变动推送订阅 | `/steam unsub`, `/steam退订` | `/steam unsub` |
| `/steam_rank` | 查看家庭组“义父”资产贡献排行榜（显示真实昵称） | `/steam rank`, `/义父榜`, `/steam排行` | `/steam rank` |
| `/steam_roll [标签]` | 赛博轮盘：随机抽取家庭库内游戏 | `/steam roll`, `/今天玩什么`, `/steam抽奖` | `/steam roll 肉鸽` |
| `/steam_coop <SteamID...>` | 开黑车队雷达：计算多人共有联机游戏 | `/steam coop`, `/开黑雷达`, `/steam联机` | `/steam coop 76561198... 76561198...` |
| `/steam_code` | 🔑 实时计算当前 5 位 Steam 令牌动态验证码（私聊有效） | `/steam code`, `/steam令牌` | `/steam_code` |
| `/steam_refresh` | 🛢️ 手动执行一次 Refresh Token 滚动续期 | `/steam refresh`, `/steam续期` | `/steam_refresh` |
| `/steam_relogin` | 🔐 管理员私聊密码登录，支持等待手机确认 | `/steam relogin`, `/steam重登` | `/steam_relogin` |
| `/steam_auth_resume` | 安全限制处理后恢复本地暂停，不触发登录 | `/steam auth_resume`, `/steam恢复认证` | `/steam_auth_resume` |
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

认证协议和会话处理参考：[ASF](https://github.com/JustArchiNET/ArchiSteamFarm)、[SteamKit2](https://github.com/SteamRE/SteamKit)、[node-steam-session](https://github.com/DoctorMcKay/node-steam-session)、[SteamDatabase/Protobufs](https://github.com/SteamDatabase/Protobufs)。本项目不是 Valve 官方客户端，也不声称具备绕过账号安全检测的能力。
