from pydantic import BaseModel, Field

class PluginConfig(BaseModel):
    access_token: str = Field(
        default="",
        description="Steam 网页端 Access Token（可在 store.steampowered.com 控制台获取）"
    )
    steam_id: str = Field(
        default="",
        description="绑定的 64 位 SteamID（如 76561198xxxxxxxx）"
    )
    family_group_id: str = Field(
        default="",
        description="Steam 家庭组 ID（留空时将根据 SteamID 和 Token 自动查询）"
    )
    poll_interval_minutes: int = Field(
        default=15,
        ge=1,
        le=1440,
        description="后台自动轮询家庭库变动的间隔（分钟）"
    )
    proxy: str = Field(
        default="",
        description="HTTP/HTTPS/SOCKS5 代理地址（例如 http://127.0.0.1:7890）"
    )
    request_timeout: int = Field(
        default=20,
        ge=5,
        le=120,
        description="Steam API 请求超时时间（秒）"
    )
    enable_render_card: bool = Field(
        default=True,
        description="是否渲染精美 HTML 游戏卡片图片推送（若关闭则推送富文本）"
    )
    filter_non_games: bool = Field(
        default=True,
        description="是否自动过滤 DLC、专用服务器、Demo、工具等非正式游戏"
    )
    subscribers: list[str] = Field(
        default_factory=list,
        description="已订阅变动推送的目标会话 UMO 标识列表"
    )
