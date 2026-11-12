# 可选 ASF 适配层（SteamFamilyBridge）

这是一个**默认关闭的独立新功能**，不会替换原有的扫码、密码登录或会话续期实现。只有在 AstrBot 插件中显式设置 `enable_asf=true` 时，家庭库同步才使用 ASF 提供的凭据；关闭后仍走原有流程。

## 作用与边界

```text
Steam ← ASF / SteamKit2 已有登录及续期
           │
           └─ SteamFamilyBridge 只读 IPC → AstrBot 家庭库查询与通知
```

- 桥接只读取 ASF **已经持有的短期 Access Token**、SteamID 和账号锁定状态；不导出密码、`shared_secret`、设备授权或 Refresh Token。
- **短期令牌仍是敏感登录凭据**，不是匿名或被强制限制为只读的 Steam API Key。只读的是本桥接接口，不应把令牌分享给其他程序、群聊或公开日志。
- 不执行 ASF 命令，不启动、停止、重登 Bot，也不调用强制续签。令牌缺失或临近到期时等待 ASF 自己完成会话维护。
- ASF 官方 `/Api/Bot` 不序列化 `AccessToken`，所以必须额外安装下方桥接插件，不能直接将该接口当作凭据来源。
- ASF 不可用、密钥错误、账号不匹配时，本插件**不回退**到自己的扫码、密码、Cookie 或 Refresh Token 流程，也不使用旧配置中的 Token 冒充 ASF 凭据。
- ASF 凭据仅保留在 AstrBot 进程内存中，IPC 查询结果的缓存有效期为 60 秒；下次读取失败或关闭适配层时清除，不写入原生凭据配置或 SQLite。SQLite 只保存适配层自己的冷却、暂停、令牌哈希和固定账号信息。
- 每次同步前读取 ASF 状态，最多复用 60 秒内的结果；收到账号 `Lockdown` 或明确的 Steam 安全错误时暂停。状态指令和看板只展示最近一次结果，不是实时账号锁定监测。此功能不能保证 Steam 不触发安全检查，也不能解除 Steam 官方限制。
- 同步时仍由 AstrBot 直接访问 Steam 家庭库 API。请保持 ASF 与 AstrBot 使用可信、稳定的网络出口；本功能并不是把所有 Steam 查询都通过 ASF 代理。

## 1. 编译并安装桥接插件

当前桥接 **v0.2.0** 已针对 [**ASF 7.0.0.8（.NET 11）**](https://github.com/JustArchiNET/ArchiSteamFarm/releases/tag/7.0.0.8) 的官方程序集编译并通过离线测试，已适配 ASF 7 异步插件接口末尾新增的 `CancellationToken`。使用 **.NET 11 SDK**，并指定对应版本 ASF 的安装目录（必须包含 `ArchiSteamFarm.dll`、`SteamKit2.dll` 和 `System.Composition.AttributedModel.dll`）：

> **7.0.0.8 是 ASF 预发布版**，上游提醒可能包含未修复问题。本适配不等于建议所有用户升级运行中的 ASF；如果选择此版本，先备份 ASF 配置，并按上游发布说明处理运行时及插件兼容性。

在本仓库根目录执行：

```bash
dotnet build integrations/asf/SteamFamilyBridge/SteamFamilyBridge.csproj \
  --configuration Release \
  -p:ASFDirectory=/absolute/path/to/ASF-7.0.0.8
```

产物为 `integrations/asf/SteamFamilyBridge/bin/Release/net11.0/SteamFamilyBridge.dll`。

将**这一份 DLL**手动放入 ASF 的 `plugins/SteamFamilyBridge/` 目录，再重启 ASF。不要把编译目录中的 ASF 本体、测试程序或其他依赖复制成第二套 ASF；桥接使用目标 ASF 自带的程序集。

ASF 7 与 ASF 6 的插件接口不兼容：之前针对 ASF 6 / .NET 10 构建的桥接 DLL 不能直接用于 ASF 7，必须重新编译并替换。本版本产物也不适用于 ASF 6。其他 ASF 7 版本仍需针对其程序集验证，不承诺所有未来版本兼容。

ASF `generic` 版本运行时也需要 .NET 11；官方系统专用版本自带运行时，但**编译本桥接仍需 .NET 11 SDK**。此次升级仅涉及可选桥接的接口、构建和测试，Python IPC 协议仍为 `ProtocolVersion=1`，现有 AstrBot `asf_*` 配置无需改变；原有认证、同步及通知功能未因版本升级而调整。

## 2. 在 ASF 中显式启用

在 ASF 的全局 `config/ASF.json` 中**合并**以下配置，不要覆盖你的其他设置：

```json
{
  "IPC": true,
  "IPCPassword": "your-existing-strong-ipc-password",
  "SteamFamilyBridge": {
    "Enabled": true,
    "Key": "replace-with-a-random-64-character-secret",
    "Bots": ["main"]
  }
}
```

- `Bots` 是确切的 ASF Bot 名称白名单，不是 Steam 登录账号名；不接受 `ASF` 的全选语义、通配符、范围或多个名字拼接。
- `Key` 应是随机生成的 32–256 个可打印 ASCII 字符，不含空格。可以在可信机器本地生成，例如 `uv run --no-project python -c "import secrets; print(secrets.token_hex(32))"`，不要把生成的密钥贴到群聊或 Issue。
- 桥接要求 ASF 本身已配置 `IPCPassword`，即使使用回环地址也不能省略。两把密钥通过不同的请求头发送：ASF 的 `Authentication` 和桥接的 `X-Steam-Family-Key`，不放入 URL。
- **IPCPassword 本身拥有 ASF IPC 的管理权限**，独立桥接密钥不会将它变成只读凭据。应限制 IPC 的监听、网络访问、配置文件及反向代理日志权限，不能将 IPC 直接暴露公网。

默认建议 IPC 仅监听回环 `http://127.0.0.1:1242`。同机连接可用 HTTP；非回环连接必须使用有效证书的 HTTPS，或通过 SSH 隧道映射到 AstrBot 所在机器的回环端口。不要关闭 TLS 验证。ASF 的 IPC 监听配置参见[官方 IPC 文档](https://github.com/JustArchiNET/ArchiSteamFarm/wiki/IPC)。

桥接接口：

```text
GET /Api/SteamFamily/main/AccessToken
```

该路径位于 `/Api` 下，受 ASF 原有 IPC 认证中间件保护；桥接额外校验独立密钥、Bot 白名单和连接安全性，并返回 `Cache-Control: no-store, private`。它没有写入接口。

## 3. 配置 AstrBot 插件

在 AstrBot 的插件配置中设置：

| 字段 | 示例 / 说明 |
|---|---|
| `enable_asf` | `true`，显式启用新功能 |
| `asf_ipc_url` | `http://127.0.0.1:1242`，或受保护的 HTTPS 地址 |
| `asf_bot_name` | `main`，与 ASF 白名单一致 |
| `asf_ipc_password` | ASF 的 IPCPassword，不是 Steam 密码 |
| `asf_bridge_key` | 与 `SteamFamilyBridge.Key` 一致 |
| `asf_request_timeout` | 默认 `10` 秒 |
| `steam_id` | 建议填写目标 Bot 的 17 位 SteamID，用于核对账号 |

无需给 AstrBot 填 Steam 密码、`shared_secret` 或 Refresh Token。已有的原生配置与凭据不会自动删除；开启 ASF 时它们不作为同步凭据来源，关闭 ASF 后原有功能仍可使用。

`steam_id` 留空时，适配层会固定第一次成功读取的 ASF 账号；若同名 Bot 后续切换账号，会拒绝凭据，而不是静默改绑。建议显式填写 SteamID。已有快照不会被自动清空；有意改为另一个家庭组时，请先确认配置，再手动处理本地基线，避免跨家庭对比造成批量通知。

保存并重载插件后使用 `/steam_check` 同步。适配层不会自动在 ASF 中登录，所以目标 Bot 应已经通过 ASF 自己的方式正常登录。

新增管理员指令：

- `/steam_asf`：查看适配层状态，不请求 IPC 令牌，不执行 ASF 命令。
- `/steam_asf_resume`：在 ASF/Steam 官方渠道处理账号或 IPC 问题后，私聊解除适配层的**本地暂停**。不发起登录，也不能提前绕过冷却。

开启 ASF 时 `/steam_qr`、`/steam_relogin`、`/steam_refresh` 会提示去 ASF 处理，而不会触发原生认证。关闭 ASF 时这些命令保持原有行为。

## 故障处理

| 状态 | 行为与处理 |
|---|---|
| 桥接未安装、未启用或 Bot 不在白名单 | 暂停读取，检查 DLL 和 `ASF.json` |
| IPC/桥接密钥被拒绝 | 至少冷却 1 小时并暂停，防止错误密钥反复请求导致 ASF IPC 封禁；配置正确后恢复 |
| Bot 离线或令牌临近到期 | 短期冷却，等待 ASF 自身处理；不启动/重登 Bot |
| ASF/Steam 报告账号安全限制 | 持久化暂停，同步停止；先在官方渠道处理，再使用恢复指令 |
| Steam 返回 401/403 | 最多重新读取 ASF 当前令牌一次；若仍是同一被拒令牌，等待 ASF 换发，不反复打 Steam |
| IPC 返回 429 | 遵守 `Retry-After`，强制同步不绕过冷却 |
| Steam 返回 429 或认证节流 | ASF 模式至少退避 25 分钟，不回退到其他登录方式 |

修改密钥不会清除同一 ASF 来源的账号安全暂停或冷却。适配层与原生认证使用不同的状态键，恢复 ASF 暂停不会重置原生认证状态。

## 离线测试

Python 测试不需要运行 ASF：

```bash
uv run python -m pytest tests/test_asf_adapter.py
```

桥接插件测试需要 ASF 安装目录中的程序集，但**不会构造 Bot 或连接 Steam**。已使用 ASF 7.0.0.8 官方程序集通过 42 项断言，包括程序集版本、新版插件初始化/取消接口、默认关闭行为、令牌元数据校验、真实 MVC 路由和 ASF IPC 认证中间件。测试只启动临时回环 HTTP 监听器，不访问运行中的 ASF：

```bash
dotnet run --project integrations/asf/SteamFamilyBridge.Tests/SteamFamilyBridge.Tests.csproj \
  --configuration Release \
  -p:ASFDirectory=/absolute/path/to/ASF-7.0.0.8
```

本适配层只提供源码和构建说明，没有自动部署脚本，也不会修改你的 ASF 或 AstrBot 运行目录。
