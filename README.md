# 🎮 astrbot_plugin_steam_family (Steam 家庭资产管家)

<p align="center">
  <img src="https://raw.githubusercontent.com/AstrBotDevs/AstrBot/master/docs/public/logo_prod.png" width="120" alt="AstrBot" />
</p>

<p align="center">
  <b>基于 AstrBot 框架的现代化 Steam 家庭库资产监控与智能管家插件</b><br>
  ⚡ 官方口径秒级增量监控 | 🎨 原生赛博卡片推送 | 👑 义父资产排行 | 🎲 赛博轮盘 | 🚀 车队雷达 | 🖥️ 原生内置 WebUI | 🤖 全库 LLM 智能找游戏
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white" alt="Python Version" />
  <img src="https://img.shields.io/badge/AstrBot-Plugin%20v4-FF6B6B?style=flat" alt="AstrBot Plugin" />
  <img src="https://img.shields.io/badge/Ruff-Formatted-black?style=flat" alt="Ruff Formatted" />
  <img src="https://img.shields.io/badge/License-MIT-green?style=flat" alt="License" />
</p>

---

> ### ⚠️ 账号安全与风控限制特别声明（必读）
>
> 1. **不存在绝对“免封/免风控”的技术方案**：无论采用原生扫码、密码登录、Token 提取，还是 **ASF 托管复用**，均涉及通过第三方网络/设备与 Steam 通信。**任何非官方官方客户端直接交互的行为均无法规避 Steam 官方的动态风控模型。**
> 2. **异地登录与代理网络风险**：若机器人部署在云服务器（机房出口 IP）、异地 VPS、公开代理或频繁漂移的出口节点，极易触发 Steam 的**设备限制（RestrictedDevice）、异地保护锁（AccountLockedDown）或登录节流（RateLimitExceeded）**。强烈建议使用常驻本地出口或专线连接。
> 3. **ASF 托管的风险边界**：ASF 托管模式减少了第三方设备独立登录的会话数，属于更稳健的短效凭据复用方案，**但这不代表 Steam API 查询是匿名或被白名单豁免的**。若账号本身已被标记、频繁并发拉取或网络出口异常，Steam 依然可能实施账号保护限制。
> 4. **发生风控限制时的处理**：一旦账号触发安全限制或警报，**切勿频繁重试、频繁切换代理节点或连续扫码/重登**，应立即停用自动同步，并在个人常用网络环境下通过 Steam 官方客户端或网页端进行人工核实与安全解除。

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
- 🤖 **全库 LLM 智能荐游（/steam_ai）**：大模型拥有家庭全库游戏全景视野，支持自然语言深度交互（*“找个能叫上好兄弟无脑突突突割草的解压游戏”*），杜绝“误把库内游戏当库外”的幻觉。
- 🔑 **授权复用与保守重登**：支持私聊 `/steam_qr` 登录，优先复用已有 Refresh Token / Cookie；参照 ASF 的错误处理思路保存设备授权、限制登录频率、在安全限制时暂停。自动密码重登默认关闭。
- 🧩 **可选 ASF 托管（推荐，默认关闭）**：通过配套只读桥接插件取得 ASF 已持有的短期 Access Token，由 ASF 接管繁琐的登录与会话维持，实现真正零密码、零 2FA 储存。参见 [ASF 适配层配置指南](integrations/asf/README.md)。

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

## 🚀 登录与凭据配置

本插件支持多种凭据来源。登录、续期及凭据管理指令仅限 AstrBot 管理员；二维码和密码操作请务必在**私聊或后台插件配置页**进行。

### 推荐方案：由 ASF 托管授权（最平稳安全）

如果你本地已运行 [ArchiSteamFarm (ASF)](https://github.com/JustArchiNET/ArchiSteamFarm)，推荐使用此方案。
- **免存密码**：AstrBot 不持有任何密码、2FA 密钥或长期 Refresh Token。
- **只读提取**：仅通过配套插件 [`SteamFamilyBridge`](integrations/asf/README.md) 只读读取 ASF 内存中已有的短期 Access Token。
- **永不越权**：不代理、不发起重登、不向 ASF 注入命令；发生错误时立即暂停，绝不悄悄回退到扫码或密码登录。

**配置指引**：
1. 参照 [ASF 适配层详细指南](integrations/asf/README.md) 部署 `SteamFamilyBridge` 并配置独立通讯密钥与 Bot 白名单；
2. 在 AstrBot 插件配置中将 `enable_asf` 设为 `true`，并填入 IPC 地址与桥接密钥；
3. 后续同步和监控将全自动复用 ASF 现成令牌。

---

### 原生方案 1：手动绑定已有 Access Token（零额外会话）
在已经登录 Steam 的浏览器中打开 [Steam 配置接口](https://store.steampowered.com/pointssummary/ajaxgetasyncconfig)，将 `data.webapi_token` 填入插件配置的 `access_token`，并填写 `steam_id`。也可以私聊 `/steam_bind <Token> [SteamID]`。

- **优势**：不在服务器/容器内发起新的认证流程，避免异地设备授权风险。
- **缺点**：Token 过期后必须手动重新抓取绑定。

---

### 原生方案 2：管理员私聊扫码登录
1. 在与机器人私聊中发送：
   ```text
   /steam_qr
   ```
2. 机器人会在本地生成二维码并发送官方 `https://s.team/q/...` 链接（不经由任何第三方打码/转码服务）；
3. 手机 Steam App 扫码批准后保存会话。**扫码前请务必仔细核对 Steam App 提示的登录地点与设备名称**！

---

### 原生方案 3：账号密码配置与自动重登（高风险，默认关闭）
- `enable_auto_relogin` **默认必须为关闭状态**。
- 若主动开启密码自动重登，在网络不稳定、IP 漂移或 Steam 服务器波动时可能导致连续重登失败，**极易引发 Steam 账号风控管制（锁定或冷却锁定）**。
- 密码和 `shared_secret` 均以明文形式留存于本地文件或 SQLite，务必严格限制运行环境的安全权限。

#### 认证安全防御机制
- **身份固定**：采用固定的宿主机名识别标识，杜绝伪造随机设备指纹。
- **显式代理出口**：严格仅使用插件设定的代理出口，避免请求在不同节点跳跃。
- **频率惩罚与退避**：遭遇限流时强制暂停至少 25 分钟并记录持久化冷却，禁止暴力重试。
- **风控熔断**：捕获到 `AccountLockedDown`、`RestrictedDevice` 时强制熔断暂停，保护账号免遭进一步处置。

---

## 📖 完整指令速查表

| 指令 | 说明 | 兼容别名 | 使用示例 |
|---|---|---|---|
| `/steam_check` | 立即手动触发一次家庭库变动检查与同步 | `/steam check`, `/steam同步`, `/家庭库检查` | `/steam check` |
| `/steam_rank` | 查看家庭组“义父”资产贡献排行榜（显示真实昵称） | `/steam rank`, `/义父榜`, `/steam排行` | `/steam rank` |
| `/steam_roll [标签]` | 赛博轮盘：随机抽取家庭库内游戏 | `/steam roll`, `/今天玩什么`, `/steam抽奖` | `/steam roll 肉鸽` |
| `/steam_coop <SteamID...>` | 开黑车队雷达：计算多人共有联机游戏 | `/steam coop`, `/开黑雷达`, `/steam联机` | `/steam coop 76561198... 76561198...` |
| `/steam_ai <需求>` | 🤖 全库大模型智能导购荐游（杜绝库外幻觉） | `/steam ai`, `/steam推荐` | `/steam_ai 推荐两款联机割草神作` |
| `/steam_sub` | 订阅当前会话的家庭库新游戏变动推送 | `/steam sub`, `/steam订阅` | `/steam sub` |
| `/steam_unsub` | 取消当前会话的变动推送订阅 | `/steam unsub`, `/steam退订` | `/steam unsub` |
| `/steam_asf` | 查看当前 ASF 托管模式运行与冷却状态 | `/steam asf`, `/steamASF` | `/steam_asf` |
| `/steam_asf_resume` | 处理完毕后私聊恢复 ASF 适配层的本地暂停 | `/steam asf_resume`, `/steamASF恢复` | `/steam_asf_resume` |
| `/steam_dashboard` | 查看 Web 大盘面板使用入口指引 | `/steam dashboard`, `/steam大盘` | `/steam dashboard` |
| `/steam_help` | 查看插件指令与功能帮助 | `/steam help`, `/steam`, `/steam帮助` | `/steam help` |
| `/steam_qr` | 📱 管理员私聊扫码登录 | `/steam qr`, `/steam扫码` | `/steam_qr` |
| `/steam_refresh` | 🛢️ 手动执行一次 Refresh Token 滚动续期 | `/steam refresh`, `/steam续期` | `/steam_refresh` |
| `/steam_relogin` | 🔐 管理员私聊密码登录，支持等待手机确认 | `/steam relogin`, `/steam重登` | `/steam_relogin` |
| `/steam_auth_resume` | 安全限制处理后恢复本地暂停，不触发登录 | `/steam auth_resume`, `/steam恢复认证` | `/steam_auth_resume` |
| `/steam_bind <Token> [SteamID]` | 手动绑定/更新 Access Token | `/steam bind`, `/steam绑定` | `/steam_bind eyJhb... 76561198...` |
| `/steam_code` | 🔑 实时计算当前 5 位 Steam 令牌动态验证码（私聊） | `/steam code`, `/steam令牌` | `/steam_code` |
| `/steam_cred <账号> <密码> [秘钥]` | 保存原生保活凭据（私聊有效） | `/steam cred`, `/steam凭据` | `/steam_cred myacct Pass! cWFh...` |

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
- **Token 占用极低**：纯游戏名文本非常紧凑，按常规家庭库测算，**每 1000 款游戏仅占用约 1.1 万 ~ 1.4 万 Token**（仅占现代主流模型 128k~1000k 上下文窗口的不到 10%，调用成本极低）；
- **建议**：如果你的家庭库游戏规模达到数千款以上，建议配置支持较大上下文窗口的模型（如 DeepSeek-V3 / Qwen / GPT-4o / Claude 3.5 / Gemini 等），即可畅享无死角的全库智能游戏推荐。

---

## 📄 开源许可证 (License)

本项目基于 [MIT License](LICENSE) 开源。

认证协议和会话处理参考：[ASF](https://github.com/JustArchiNET/ArchiSteamFarm)、[SteamKit2](https://github.com/SteamRE/SteamKit)、[node-steam-session](https://github.com/DoctorMcKay/node-steam-session)、[SteamDatabase/Protobufs](https://github.com/SteamDatabase/Protobufs)。

**免责声明**：本项目为开源学习与自动化工具，并非 Valve / Steam 官方产品。用户需自行对账号安全行为与配置负责，任何因异地代理、高频重试或多处并发使用触发的官方风控及账号限制，本项目及维护者不承担责任。
