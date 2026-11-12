from pydantic import BaseModel, Field


class PluginConfig(BaseModel):
    access_token: str = Field(
        default="",
        description="Steam 网页端 Access Token（通过直达链接获取）",
    )
    cookie: str = Field(
        default="",
        description="Steam 网页端 Cookie (或 steamLoginSecure)，仅在有效期间用于复用登录会话",
    )
    steam_id: str = Field(default="", description="绑定的 64 位 SteamID")
    account_name: str = Field(
        default="",
        description="Steam 账号名（路线一·全自动重登保活用）",
    )
    password: str = Field(
        default="",
        description="Steam 登录密码（路线一·搭配 shared_secret 实现密码全自动重登）",
    )
    shared_secret: str = Field(
        default="",
        description="Steam 令牌 2FA 秘钥 shared_secret（Base64 格式）",
    )
    refresh_token: str = Field(
        default="",
        description="Steam Refresh Token（插件自动保存/复用，寿命与可续期性由 Steam 决定）",
    )
    enable_auto_relogin: bool = Field(
        default=False,
        description="显式允许自动密码重登（默认关闭；需账号密码+2FA秘钥，可能触发设备/异地安全检查）",
    )
    auth_device_name: str = Field(
        default="", description="固定认证设备名称（留空使用宿主机名；不要频繁更换）"
    )
    enable_asf: bool = Field(
        default=False,
        description="可选 ASF 托管模式（默认关闭；启用时凭据仅来自 ASF，不回退到原生登录）",
    )
    asf_ipc_url: str = Field(
        default="http://127.0.0.1:1242",
        description="ASF IPC 服务地址（HTTP 仅限回环；远程必须 HTTPS 或 SSH 隧道）",
    )
    asf_bot_name: str = Field(
        default="", description="ASF 中确切的 Bot 名称（不是 Steam 登录账号名）"
    )
    asf_ipc_password: str = Field(
        default="", repr=False, description="ASF config/ASF.json 中的 IPCPassword（不是 Steam 密码）"
    )
    asf_bridge_key: str = Field(
        default="", repr=False, description="ASF config/ASF.json 中为 SteamFamilyBridge.Key 配置的独立密钥（至少 32 字符）"
    )
    asf_request_timeout: int = Field(default=10, ge=3, le=60, description="ASF IPC 请求超时（秒）")
    family_group_id: str = Field(default="", description="Steam 家庭组 ID（留空时将自动获取）")
    poll_interval_minutes: int = Field(
        default=15, ge=1, le=1440, description="后台自动轮询间隔（分钟）"
    )
    enable_price_watch: bool = Field(
        default=True,
        description="是否启用家庭库游戏降价监控（每 20 小时巡检一次，单轮最多 120 款）",
    )
    proxy: str = Field(default="", description="网络代理地址（如 http://127.0.0.1:7890）")
    request_timeout: int = Field(default=20, ge=5, le=120, description="网络请求超时时间（秒）")
    enable_render_card: bool = Field(
        default=True,
        description="是否启用 Playwright 卡片渲染（若未安装则自动降级为文本）",
    )
    filter_non_games: bool = Field(
        default=True,
        description="是否自动过滤非游戏应用（如工具、专用服务端、原声带）",
    )
    broadcast_free_games: bool = Field(
        default=False,
        description="是否播报免费入库的游戏（默认关闭，仅在有人购买付费游戏时推送播报）",
    )
    subscribers: list[str] = Field(
        default_factory=list, description="订阅变动推送的目标会话 UMO 列表"
    )
