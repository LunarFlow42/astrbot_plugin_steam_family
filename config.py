from pydantic import BaseModel, Field


class PluginConfig(BaseModel):
    access_token: str = Field(
        default="",
        description="Steam 网页端 Access Token（通过直达链接获取）",
    )
    cookie: str = Field(
        default="",
        description="Steam 网页端 Cookie (或 steamLoginSecure)，填入后可全自动无感刷新 Token 永久保活",
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
        description="Steam Refresh Token（200 天，插件自动滚动续期并回写，一般无需手动填写）",
    )
    enable_auto_relogin: bool = Field(
        default=True,
        description="是否启用断线全自动重登（路线一：账号密码+2FA秘钥，需填写 account_name/password/shared_secret）",
    )
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
