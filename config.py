from pydantic import BaseModel, Field

class PluginConfig(BaseModel):
    access_token: str = Field(
        default="",
        description="Steam 网页端 Access Token（通过直达链接获取）"
    )
    cookie: str = Field(
        default="",
        description="Steam 网页端 Cookie (或 steamLoginSecure)，填入后可全自动无感刷新 Token 永久保活"
    )
    steam_id: str = Field(
        default="",
        description="绑定的 64 位 SteamID"
    )
    family_group_id: str = Field(
        default="",
        description="Steam 家庭组 ID（留空时将自动获取）"
    )
    poll_interval_minutes: int = Field(
        default=15,
        ge=1,
        le=1440,
        description="后台自动轮询间隔（分钟）"
    )
    enable_dashboard_server: bool = Field(
        default=True,
        description="是否启用独立 Web 资产全景大盘服务"
    )
    dashboard_host: str = Field(
        default="0.0.0.0",
        description="看板监听主机（0.0.0.0 允许远程访问，127.0.0.1 仅限本机）"
    )
    dashboard_port: int = Field(
        default=6188,
        ge=1,
        le=65535,
        description="看板监听端口"
    )
    proxy: str = Field(
        default="",
        description="网络代理地址（如 http://127.0.0.1:7890）"
    )
    request_timeout: int = Field(
        default=20,
        ge=5,
        le=120,
        description="网络请求超时时间（秒）"
    )
    enable_render_card: bool = Field(
        default=True,
        description="是否启用 Playwright 卡片渲染（若未安装则自动降级为文本）"
    )
    filter_non_games: bool = Field(
        default=True,
        description="是否自动过滤非游戏应用（如工具、专用服务端、原声带）"
    )
    subscribers: list[str] = Field(
        default_factory=list,
        description="订阅变动推送的目标会话 UMO 列表"
    )
