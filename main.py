import asyncio
import logging
import re
import time
from pathlib import Path

try:
    import astrbot.api.message_components as Comp
    from astrbot.api import AstrBotConfig
    from astrbot.api.event import AstrMessageEvent, MessageChain, filter
    from astrbot.api.star import Context, Star, register
except ImportError:

    class Star:
        def __init__(self, context):
            self.context = context

    class Context:
        def get_config(self):
            return {}

        async def send_message(self, *args, **kwargs):
            pass

        def register_web_api(self, *args, **kwargs):
            pass

        async def get_current_chat_provider_id(self, *args, **kwargs):
            return None

        async def llm_generate(self, *args, **kwargs):
            return "Mock AI response"

    def register(*args, **kwargs):
        return lambda cls: cls

    class filter:
        @staticmethod
        def command(*args, **kwargs):
            return lambda f: f

        @staticmethod
        def llm_tool(*args, **kwargs):
            return lambda f: f

        @staticmethod
        def permission_type(*args, **kwargs):
            return lambda f: f

        class PermissionType:
            ADMIN = "admin"

    class AstrMessageEvent:
        unified_msg_origin = "test_umo"

        def plain_result(self, text):
            return text

        def image_result(self, url):
            return url

    class MessageChain:
        def __init__(self, *args, **kwargs):
            pass

    class Comp:
        class Plain:
            def __init__(self, text):
                pass

        class Image:
            @staticmethod
            def fromFileSystem(path):
                return path

            @staticmethod
            def fromBytes(b):
                return b

            @staticmethod
            def fromURL(url):
                return url

    AstrBotConfig = dict

try:
    from astrbot.api.web import json_response, request
except ImportError:

    def json_response(data, status_code=200):
        return data

    class request:
        query = {}


try:
    from .agent.tools import SteamAgentTools
    from .config import PluginConfig
    from .core.auth_manager import SteamAuthManager
    from .core.diff_engine import FamilyDiffEngine
    from .core.renderer import CardRenderer
    from .core.steam_api import (
        SteamAuthExpiredError,
        SteamFamilyClient,
        SteamRateLimitError,
        decode_steam_jwt,
    )
    from .core.steam_auth import (
        SteamAuthClient,
        SteamLoginError,
        generate_steam_guard_code,
        steamid_from_refresh_token,
    )
    from .core.storage import FamilyStorage
except (ImportError, ValueError):
    from agent.tools import SteamAgentTools
    from config import PluginConfig
    from core.auth_manager import SteamAuthManager
    from core.diff_engine import FamilyDiffEngine
    from core.renderer import CardRenderer
    from core.steam_api import (
        SteamAuthExpiredError,
        SteamFamilyClient,
        SteamRateLimitError,
        decode_steam_jwt,
    )
    from core.steam_auth import (
        SteamAuthClient,
        SteamLoginError,
        generate_steam_guard_code,
        steamid_from_refresh_token,
    )
    from core.storage import FamilyStorage

logger = logging.getLogger(__name__)

# 单轮推送管线（_drain_pending_notifications）最多处理的事件条数上限。
# 防止历史积压事件过多时一次 drain 长时间占用事件循环；超出部分留待下一轮继续补播。
_DRAIN_MAX_EVENTS = 500

# 降价监控巡检节流：两轮价格巡检的最小间隔（20 小时）与单轮巡检的游戏数上限。
_PRICE_WATCH_MIN_INTERVAL_SECONDS = 20 * 3600
_PRICE_WATCH_MAX_APPS = 120
# 最长轮询退避倍数（连续失败时最多放大到基础间隔的 4 倍）。
_POLL_BACKOFF_MAX_FACTOR = 4


def _compute_poll_interval_secs(base_minutes: int, consecutive_failures: int) -> int:
    """计算下一次轮询等待秒数（自适应退避，纯函数便于单测）。

    - 成功（consecutive_failures=0）→ 基础间隔 base*60 秒；
    - 连续失败 n 次 → base * min(4, 2^n)，即失败越多间隔越长，最长 4 倍；
    - 下限 60 秒，防止配置给出过小值导致高频打 Steam 触发 429。
    """
    base_secs = max(60, int(base_minutes) * 60)
    factor = min(_POLL_BACKOFF_MAX_FACTOR, 2 ** max(0, int(consecutive_failures)))
    return max(60, base_secs * factor)


# ------------------------------------------------------------------
# 异常消息脱敏：Steam API 异常（httpx 的 HTTPStatusError / ConnectError 等）的
# str 里常含完整 `access_token=eyJ...` 查询串或 `steamLoginSecure=...` Cookie 值，
# 若原样进群聊回复 / sync_last_message / 日志会造成凭据外泄，统一在此打码。
# 覆盖 access_token / refresh_token（含 %5F 编码下划线的 URL 变体）/
# steamLoginSecure / cookie 四类键值对；值以 & 空格 逗号 分号 引号为边界截断。
# ------------------------------------------------------------------
_SENSITIVE_PAIR_RE = re.compile(
    r"(?i)\b(access(?:%5F|_)?token|refresh(?:%5F|_)?token|steamloginsecure|cookie)"
    r"\s*[=:]\s*[^&\s,;\"']+"
)


def _sanitize_error_text(text: str) -> str:
    """把任意字符串中的敏感凭据键值对值打码为 ***（字符串级脱敏复用入口）。"""
    return _SENSITIVE_PAIR_RE.sub(r"\1=***", text)


def _sanitize_error(exc: BaseException) -> str:
    """把异常格式化为 "类型名: 消息" 并脱敏其中的敏感凭据（防止 token 泄漏进群聊/日志）。"""
    return _sanitize_error_text(f"{type(exc).__name__}: {exc}")


class _NotifyContext:
    """
    一轮推送管线内共享的上下文：成员昵称表、订阅者列表与统计计数。
    避免每个事件处理函数都携带一长串重复参数。
    """

    def __init__(self, profiles: dict, subscribers: list[str], stats: dict):
        self.profiles = profiles
        self.subscribers = subscribers
        self.stats = stats


def make_cors_response(data: dict | list | str, status_code: int = 200):
    """
    按照 AstrBot 官方标准构建响应。
    当返回 {"status": "ok", "data": ...} 时，bridge.apiGet 会自动直接 resolve 为 data 内容。
    同时添加 Access-Control-Allow-Origin: * 响应头，确保跨域与沙盒环境 100% 畅通。
    """
    payload = (
        {"status": "ok", "data": data, **(data if isinstance(data, dict) else {})}
        if isinstance(data, (dict, list))
        else {"status": "ok", "message": str(data)}
    )

    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS, PUT, DELETE",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Max-Age": "86400",
    }
    try:
        from fastapi.responses import JSONResponse

        return JSONResponse(content=payload, status_code=status_code, headers=headers)
    except Exception:
        pass
    try:
        from starlette.responses import JSONResponse

        return JSONResponse(content=payload, status_code=status_code, headers=headers)
    except Exception:
        pass
    return json_response(payload)


def _get_db_path() -> str:
    """
    获取 AstrBot 规范持久化数据库路径 (位于 data/plugin_data/{plugin_name}/steam_family.db)。
    确保严格存储在 AstrBot 根数据目录 data/ 下，避免更新插件时数据被覆盖。
    """
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        base_data_path = get_astrbot_data_path()
    except Exception:
        base_data_path = Path("data")

    data_dir = Path(base_data_path) / "plugin_data" / "astrbot_plugin_steam_family"
    data_dir.mkdir(parents=True, exist_ok=True)
    target_db = data_dir / "steam_family.db"

    # 自动无缝兼容并迁移旧版位于 data/ 根目录的数据库
    legacy_db = Path("data/steam_family.db")
    if legacy_db.exists() and not target_db.exists():
        try:
            import shutil

            shutil.copy2(legacy_db, target_db)
        except Exception:
            pass
    return str(target_db)


@register(
    "astrbot_plugin_steam_family",
    "LunarFlow42",
    "Steam 家庭资产监控与智能管家插件（支持卡片推送、义父榜与 LLM 找游戏）",
    "0.3.0",
)
class SteamFamilyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        raw_config = config if isinstance(config, (dict, AstrBotConfig)) else {}
        if not raw_config and hasattr(context, "get_config"):
            try:
                raw_config = context.get_config()
            except Exception:
                raw_config = {}

        self.raw_astr_config = config if isinstance(config, AstrBotConfig) else None
        self.config = (
            PluginConfig(**raw_config)
            if isinstance(raw_config, dict) and raw_config
            else PluginConfig()
        )

        self.client = SteamFamilyClient(
            proxy=self.config.proxy, timeout=self.config.request_timeout
        )
        self.storage = FamilyStorage(_get_db_path())
        self.diff_engine = FamilyDiffEngine()
        self.renderer = CardRenderer()
        self.agent_tools = SteamAgentTools(self.storage)
        self._polling_task: asyncio.Task | None = None
        # 后台任务登记表：全插件所有后台任务统一经 _spawn 创建并登记在集合中，
        # 任务结束时自动移除；terminate() 时逐一 cancel 并 gather 回收，杜绝泄漏。
        self._bg_tasks: set[asyncio.Task] = set()

        # --- 可靠管线：单飞锁 + 统一收款/播报管线 ---
        # 单飞锁：cmd_check / web_api_sync / 轮询三个同步入口的并发保护。
        self._sync_lock = asyncio.Lock()
        # 进行中的同步任务：并发调用合并复用其结果，从源头消灭重复拉取与重复推送竞态。
        self._sync_task: asyncio.Task | None = None
        # 推送管线锁：保证同进程内多处触发（检查完成/轮询/启动补播）串行领取待推送队列，
        # 避免同一事件被并发 claim 造成重复推送。
        self._drain_lock = asyncio.Lock()
        # 受控消息协议下的最近一次检查成功与否（None=未知/未执行到分类器）。
        self._last_check_success: bool | None = None

        # 会话复用与保守重登：默认不自动创建密码登录会话。
        self._runtime_credentials: dict[str, str] = {}
        self.auth = SteamAuthClient(
            proxy=self.config.proxy,
            timeout=self.config.request_timeout,
            device_name=self.config.auth_device_name,
        )
        self.auth_manager = SteamAuthManager(
            auth=self.auth,
            get_cred=lambda name: self._runtime_credentials.get(
                name, getattr(self.config, name, "")
            ),
            set_cred=self._persist_auth_credential,
            cookie_refresh_fn=self.client.refresh_token_via_cookie,
            notify_fn=self._broadcast_notify,
            load_state_fn=lambda: self.storage.get_kv("steam_auth_state"),
        )

        # Register AstrBot Web APIs for embedded Plugin Pages
        self._register_astrbot_web_apis()

        # Start async initialization
        self._spawn(self._init_and_start_polling())

    def _spawn(self, coro) -> asyncio.Task:
        """统一后台任务入口：创建任务并登记到 _bg_tasks 集合，结束后自动移除。

        全插件的 fire-and-forget 任务（启动补播 drain、首轮检查、命令成功后的补
        check、降价巡检、凭据 KV 落库等）都必须经此入口创建，确保 terminate()
        能统一 cancel 全部后台任务并 await 回收，避免插件卸载/重载后残留游离
        任务继续访问已关闭的 httpx 客户端与 SQLite 连接。
        """
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _register_astrbot_web_apis(self):
        """Register Web APIs per official AstrBot Plugin Pages standard."""
        if hasattr(self.context, "register_web_api"):
            routes = [
                # Official standard relative routes
                ("data", self.web_api_get_data, ["GET", "OPTIONS"]),
                ("sync", self.web_api_sync, ["POST", "OPTIONS"]),
                ("db/remove_app", self.web_api_remove_app, ["POST", "OPTIONS"]),
                ("remove_app", self.web_api_remove_app, ["POST", "OPTIONS"]),
                ("db/reset_snapshots", self.web_api_reset_snapshots, ["POST", "OPTIONS"]),
                ("reset_snapshots", self.web_api_reset_snapshots, ["POST", "OPTIONS"]),
                ("db/reset", self.web_api_reset_snapshots, ["POST", "OPTIONS"]),
                ("reset", self.web_api_reset_snapshots, ["POST", "OPTIONS"]),
                # Absolute & alias routes
                ("/data", self.web_api_get_data, ["GET", "OPTIONS"]),
                ("/api/data", self.web_api_get_data, ["GET", "OPTIONS"]),
                ("/astrbot_plugin_steam_family/data", self.web_api_get_data, ["GET", "OPTIONS"]),
                (
                    "/astrbot_plugin_steam_family/api/data",
                    self.web_api_get_data,
                    ["GET", "OPTIONS"],
                ),
                ("/sync", self.web_api_sync, ["POST", "OPTIONS"]),
                ("/api/sync", self.web_api_sync, ["POST", "OPTIONS"]),
                (
                    "/astrbot_plugin_steam_family/sync",
                    self.web_api_sync,
                    ["POST", "OPTIONS"],
                ),
                (
                    "/astrbot_plugin_steam_family/api/sync",
                    self.web_api_sync,
                    ["POST", "OPTIONS"],
                ),
                ("/db/remove_app", self.web_api_remove_app, ["POST", "OPTIONS"]),
                ("/remove_app", self.web_api_remove_app, ["POST", "OPTIONS"]),
                ("/api/db/remove_app", self.web_api_remove_app, ["POST", "OPTIONS"]),
                (
                    "/astrbot_plugin_steam_family/db/remove_app",
                    self.web_api_remove_app,
                    ["POST", "OPTIONS"],
                ),
                (
                    "/astrbot_plugin_steam_family/api/db/remove_app",
                    self.web_api_remove_app,
                    ["POST", "OPTIONS"],
                ),
                ("/db/reset_snapshots", self.web_api_reset_snapshots, ["POST", "OPTIONS"]),
                ("/reset_snapshots", self.web_api_reset_snapshots, ["POST", "OPTIONS"]),
                (
                    "/api/db/reset_snapshots",
                    self.web_api_reset_snapshots,
                    ["POST", "OPTIONS"],
                ),
                (
                    "/astrbot_plugin_steam_family/db/reset_snapshots",
                    self.web_api_reset_snapshots,
                    ["POST", "OPTIONS"],
                ),
                (
                    "/astrbot_plugin_steam_family/api/db/reset_snapshots",
                    self.web_api_reset_snapshots,
                    ["POST", "OPTIONS"],
                ),
            ]
            for route_path, handler, methods in routes:
                try:
                    self.context.register_web_api(
                        route_path,
                        handler,
                        methods,
                        f"Steam Family API: {route_path}",
                    )
                except Exception as e:
                    logger.debug(f"Route registration notice for {route_path}: {e}")
            logger.info("Successfully registered AstrBot Web APIs for Plugin Pages.")

    async def web_api_get_data(self, *args, **kwargs):
        data = await self.storage.get_dashboard_data()
        data["is_rate_limited"] = self.client.is_rate_limited
        data["rate_limit_cooldown_remaining"] = self.client.rate_limit_cooldown_remaining

        # Live JWT credential status + 全自动保活状态
        jwt_info = decode_steam_jwt(self.config.access_token)
        data["token_info"] = {
            "valid": bool(jwt_info and not jwt_info.get("is_expired")),
            "is_expired": bool(jwt_info.get("is_expired", True)) if jwt_info else True,
            "remaining_hours": jwt_info.get("remaining_hours", 0.0) if jwt_info else 0.0,
            "steamid": jwt_info.get("steamid", "") if jwt_info else "",
            "has_cookie_autorenew": bool(self.config.cookie),
            # Cookie 失败冷却状态：读取 auth_manager 在 Cookie 静默刷新失败时写入的
            # 冷却截止时间（原字段 _cookie_refresh_cooldown_until 从不更新，恒为 False）
            "cookie_cooling": self.auth_manager._cookie_fail_cooldown_until > time.time(),
            "keepalive_mode": self.auth_manager.describe_mode(),
            "has_refresh_token": bool(self.config.refresh_token),
            "refresh_token_days_left": self.auth_manager.refresh_token_days_left(),
            "has_2fa_autologin": bool(
                self.auth_manager.auto_relogin_enabled
                and not self.auth_manager.automatic_login_paused
                and self.config.account_name
                and self.config.password
                and self.config.shared_secret
            ),
            "keepalive_note": self.auth_manager.last_keepalive_note,
            "auth_security_blocked": self.auth_manager.security_blocked,
            "auth_cooldown_remaining": self.auth_manager.auth_cooldown_remaining,
            "automatic_login_paused": self.auth_manager.automatic_login_paused,
            "auth_device_name": self.auth.device_name,
        }

        # 同步状态观测（0.3.0）：最近一次成功同步时间/耗时/结果消息 + 最近游玩面板数据
        try:
            sync_info = await self.storage.get_sync_state_info()
            sync_msg = await self.storage.get_sync_state("last_message")
            sync_failed = await self.storage.get_sync_state("last_status_failed")
            sync_info["last_message"] = str(sync_msg) if sync_msg else ""
            # last_status_failed: "1"=最近一次检查失败 / "0"=成功 / None=从未执行
            sync_info["failed"] = str(sync_failed) == "1"
            data["sync_info"] = sync_info
        except Exception as ex:
            logger.debug(f"Failed to collect sync state info: {ex}")
        try:
            data["recent_plays"] = await self.storage.get_recent_plays(8)
        except Exception as ex:
            logger.debug(f"Failed to collect recent plays: {ex}")
        return make_cors_response(data)

    async def web_api_sync(self, *args, **kwargs):
        msg = await self._check_and_notify()
        # _check_and_notify_impl 内部已按受控消息协议维护 _last_check_success；
        # None（未执行到分类器，理论不可达）按失败处理，消息仍为本次执行结果。
        success = bool(self._last_check_success)
        return make_cors_response({"success": success, "message": msg})

    async def web_api_remove_app(self, *args, **kwargs):
        appid = 0

        # 优先从 AstrBot 官方上下文代理 request 读取（异步/同步 JSON 及 query 均支持）
        if hasattr(request, "json"):
            try:
                req_json = request.json()
                if asyncio.iscoroutine(req_json):
                    req_json = await req_json
                if isinstance(req_json, dict) and "appid" in req_json:
                    val = req_json["appid"]
                    if str(val).isdigit() or isinstance(val, int):
                        appid = int(val)
            except Exception:
                pass
        if appid == 0 and hasattr(request, "query"):
            try:
                val = request.query.get("appid", 0) if hasattr(request.query, "get") else 0
                if str(val).isdigit():
                    appid = int(val)
            except Exception:
                pass

        for arg in args:
            if hasattr(arg, "json"):
                try:
                    import inspect

                    if inspect.iscoroutinefunction(arg.json):
                        body = await arg.json()
                    else:
                        body = arg.json()
                    if isinstance(body, dict) and "appid" in body:
                        appid = int(body["appid"])
                except Exception:
                    pass
            if not appid and hasattr(arg, "query_params"):
                val = arg.query_params.get("appid", "0")
                if str(val).isdigit():
                    appid = int(val)
            if not appid and isinstance(arg, dict):
                val = arg.get("appid", 0)
                if str(val).isdigit() or isinstance(val, int):
                    appid = int(val)
            if not appid and hasattr(arg, "data") and isinstance(arg.data, dict):
                val = arg.data.get("appid", 0)
                if str(val).isdigit() or isinstance(val, int):
                    appid = int(val)
        if appid == 0 and "appid" in kwargs:
            val = kwargs["appid"]
            appid = int(val) if str(val).isdigit() else 0

        if not appid:
            return make_cors_response(
                {"success": False, "message": "Missing or invalid appid parameter"},
                status_code=400,
            )

        success = await self.storage.remove_app_from_latest_snapshot(appid)
        if success:
            return make_cors_response(
                {
                    "success": True,
                    "message": f"AppID {appid} has been removed from local snapshot baseline. Next sync check will detect it as a newly added game and broadcast it.",
                }
            )
        return make_cors_response(
            {"success": False, "message": f"AppID {appid} not found in latest snapshot baseline."},
            status_code=404,
        )

    async def web_api_reset_snapshots(self, *args, **kwargs):
        await self.storage.reset_snapshots()
        return make_cors_response(
            {
                "success": True,
                "message": "Snapshot baseline has been reset successfully.",
            }
        )

    def _refresh_config(self):
        """Dynamically reload config from AstrBotConfig or context."""
        raw = None
        if self.raw_astr_config is not None:
            raw = dict(self.raw_astr_config)
        elif hasattr(self.context, "get_config"):
            try:
                raw = self.context.get_config()
            except Exception:
                raw = None

        if isinstance(raw, dict) and raw:
            # 运行时轮换的凭据优先（防止回读旧配置导致刚续期的 refresh_token 丢失）
            raw = dict(raw)
            runtime = dict(self._runtime_credentials)
            # 令牌轮换不能被旧配置覆盖，但设备名称允许管理员显式修改。
            if raw.get("auth_device_name"):
                runtime.pop("auth_device_name", None)
            raw.update(runtime)
            old_proxy = self.client.proxy
            old_timeout = self.client.timeout
            old_device_name = self.config.auth_device_name
            self.config = PluginConfig(**raw)
            if self.auth_manager._ops_busy:
                # 登录/续期会话期间保持客户端与出口不变，新配置下次再应用。
                self.config.proxy = old_proxy
                self.config.request_timeout = old_timeout
                self.config.auth_device_name = old_device_name
                return
            if old_proxy != self.config.proxy or old_timeout != self.config.request_timeout:
                old_client = self.client
                self.client = SteamFamilyClient(
                    proxy=self.config.proxy,
                    timeout=self.config.request_timeout,
                )
                self._spawn(old_client.close())
            if (
                old_proxy != self.config.proxy
                or old_timeout != self.config.request_timeout
                or old_device_name != self.config.auth_device_name
            ):
                old_auth = self.auth
                self.auth = SteamAuthClient(
                    proxy=self.config.proxy,
                    timeout=self.config.request_timeout,
                    device_name=self.config.auth_device_name,
                )
                self.auth_manager.set_clients(self.auth, self.client.refresh_token_via_cookie)
                self._spawn(old_auth.close())

    def _save_credentials(self, key: str, value: str):
        """
        持久化滚动续期 / 自动重登过程中轮换的凭据（refresh_token / access_token 等）：
        1. 运行时内存覆盖 —— 防止 _refresh_config 回读旧配置造成刚轮换的 Token 丢失；
        2. 写回 AstrBotConfig 并 save_config()；
        3. 写入 SQLite credentials_kv 作为二级保险。
        """
        value = str(value or "")
        self._runtime_credentials[key] = value
        try:
            if hasattr(self.config, key):
                setattr(self.config, key, value)
        except Exception:
            pass
        if self.raw_astr_config is not None:
            try:
                if key in PluginConfig.model_fields:
                    self.raw_astr_config[key] = value
                    if hasattr(self.raw_astr_config, "save_config"):
                        self.raw_astr_config.save_config()
            except Exception as e:
                logger.debug(f"Persist credential '{key}' into AstrBotConfig failed: {e}")
        try:
            self._spawn(self.storage.set_kv(key, value))
        except Exception as e:
            logger.debug(f"Persist credential '{key}' into KV storage failed: {e}")

    async def _persist_auth_credential(self, key: str, value: str) -> None:
        """认证轮换/安全状态必须先落库完成，再允许下一次网络操作。"""
        value = str(value or "")
        await self.storage.set_kv(key, value)
        self._runtime_credentials[key] = value
        if key in PluginConfig.model_fields:
            setattr(self.config, key, value)
            if self.raw_astr_config is not None:
                self.raw_astr_config[key] = value
                try:
                    if hasattr(self.raw_astr_config, "save_config"):
                        self.raw_astr_config.save_config()
                except Exception as exc:
                    logger.warning(
                        f"AstrBot 配置保存失败，认证凭据已保存至 KV ({type(exc).__name__})"
                    )

    async def _broadcast_notify(self, text: str):
        """向所有订阅会话广播保活事件（如全自动重登成功）。"""
        try:
            targets = await self.storage.get_subscribers()
            for target_id in targets:
                try:
                    chain = MessageChain([Comp.Plain(text)])
                    await self.context.send_message(target_id, chain)
                except Exception as ex:
                    logger.error(f"Failed to send keepalive notice to {target_id}: {ex}")
        except Exception as e:
            logger.debug(f"Broadcast notify failed: {e}")

    async def _restore_credentials(self):
        """
        启动时凭据恢复：
        1. 配置缺失 refresh_token / access_token 时，从 SQLite KV 二级备份恢复；
        2. 未配置 steam_id 时，自动从 refresh_token / access_token 的 JWT 声明中推导。
        """
        try:
            for key in ("refresh_token", "access_token"):
                value = await self.storage.get_kv(key)
                configured = getattr(self.config, key, "")
                saved_info = decode_steam_jwt(value or "")
                configured_info = decode_steam_jwt(configured or "")
                persisted_account_id = await self.storage.get_kv("steam_refresh_account_id")
                saved_matches_account = not self.config.steam_id or (
                    saved_info and saved_info.get("steamid") == self.config.steam_id
                )
                # Steam 会撤销被轮换掉的旧 RT；KV 更晚签发时不得回读过时配置覆盖它。
                saved_is_newer = bool(
                    saved_info
                    and configured_info
                    and saved_info.get("steamid") == configured_info.get("steamid")
                    and saved_info.get("iat", 0) > configured_info.get("iat", 0)
                )
                persisted_rt_authoritative = bool(
                    key == "refresh_token"
                    and persisted_account_id
                    and saved_info
                    and saved_info.get("steamid") == persisted_account_id
                    and (
                        not configured_info
                        or configured_info.get("steamid") == persisted_account_id
                    )
                )
                if (
                    value
                    and saved_matches_account
                    and (not configured or saved_is_newer or persisted_rt_authoritative)
                ):
                    self._runtime_credentials[key] = value
                    if hasattr(self.config, key):
                        setattr(self.config, key, value)
                    logger.info(f"Restored credential '{key}' from KV storage.")
            for key in ("steam_guard_data", "steam_guard_account"):
                value = await self.storage.get_kv(key)
                if value:
                    self._runtime_credentials[key] = value
            await self.auth_manager.ensure_state_loaded()
            if not self.config.auth_device_name:
                saved_name = await self.storage.get_kv("auth_device_name")
                await self._persist_auth_credential(
                    "auth_device_name", saved_name or self.auth.device_name
                )
                self.auth.device_name = self.config.auth_device_name
        except Exception as e:
            # 安全状态读取失败时停止初始化，不以空白安全状态继续登录。
            logger.error(f"Restore credentials/security state failed: {type(e).__name__}")
            raise
        if not self.config.steam_id:
            steam_id = steamid_from_refresh_token(self.config.refresh_token or "")
            if not steam_id and self.config.access_token:
                info = decode_steam_jwt(self.config.access_token)
                steam_id = (info or {}).get("steamid", "")
            if steam_id:
                self._save_credentials("steam_id", str(steam_id))
                logger.info(f"Auto-derived SteamID {steam_id} from token JWT.")

    async def _init_and_start_polling(self):
        try:
            await self.storage.init_db()
            self._refresh_config()
            await self._restore_credentials()

            # 重启补播：pending_notifications 队列中的历史未发事件（如上次进程崩溃前
            # 已完成快照落库但未播报的变动）由统一收款/播报管线一次性补偿推送。
            self._spawn(self._drain_pending_notifications())

            # 自动保活: access_token 缺失但配置了 refresh_token / 密码重登时，先尝试补齐。
            if not self.config.access_token and (
                self.config.refresh_token
                or self.config.cookie
                or (self.config.account_name and self.config.password)
            ):
                try:
                    await self.auth_manager.ensure_valid_access_token(force=False)
                except Exception as e:
                    logger.warning(f"Startup token bootstrap failed: {e}")

            # Execute immediate initial sync if credentials are configured
            has_credentials = bool(
                self.config.access_token
                or self.config.cookie
                or self.config.refresh_token
                or (self.config.account_name and self.config.password)
            )
            if has_credentials and self.config.steam_id:
                self._spawn(self._check_and_notify())

            # Start background polling loop
            self._polling_task = self._spawn(self._polling_loop())
            logger.info("Steam Family Plugin initialized successfully.")
        except Exception as e:
            logger.error(f"Error during plugin startup: {e}")

    async def _render_card_image(self, card_data: dict) -> str | None:
        """利用 AstrBot 原生 self.html_render 渲染高质量游戏卡片。"""
        if not self.config.enable_render_card:
            return None

        if hasattr(self, "html_render"):
            try:
                tmpl = self.renderer.get_template_source("game_card.html")
                if tmpl:
                    url = await self.html_render(
                        tmpl,
                        card_data,
                        options={"type": "png", "omit_background": True},
                    )
                    return url
            except Exception as ex:
                logger.debug(f"AstrBot Star.html_render exception: {ex}")

        return None

    async def _check_and_notify(self) -> str:
        """单飞薄壳：持有 _sync_lock，并发调用合并为一次执行。

        cmd_check / web_api_sync / 轮询三个入口可能同时触发检查；这里保证
        正在进行中的同步不被打断也不重复执行，后来者等待并直接复用本次结果
        （并发三次仅执行一次 impl），从源头消灭并发导致的重复拉取与重复推送竞态。
        """
        async with self._sync_lock:
            # 已有正在执行的同步任务：直接复用，不再重复执行 impl
            if self._sync_task is not None and not self._sync_task.done():
                task = self._sync_task
            else:
                task = self._spawn(self._check_and_notify_impl())
                self._sync_task = task
        # asyncio.shield：某个等待者被取消（如 web 客户端断连）时，只取消该等待者
        # 自身的 await，不得把所有等待者共享的 impl 任务连带取消——否则其他等待者
        # 也会收到 CancelledError，且同步中途夭折。
        return await asyncio.shield(task)

    async def _check_and_notify_impl(self) -> str:
        """执行一次家庭库同步，并按"受控消息协议"维护 _last_check_success。

        受控消息协议：_check_and_notify_core 返回的面向用户消息若以 ❌ / ⚠️ 开头，
        即代表本次检查未成功完成（凭据缺失/失效、限流、异常等）；其余视为成功。
        web_api_sync 依赖 _last_check_success 判定，不再自行解析消息前缀。

        收尾时同步写入 sync_* 观测键（成功时间戳/耗时/结果消息/失败标记），
        并在成功路径末尾异步触发一轮降价监控巡检（不阻塞本次同步返回）。
        """
        started_at = time.monotonic()
        msg = await self._check_and_notify_core()
        # 受控消息协议分类器：在最终返回前统一维护结构化成功标记
        self._last_check_success = not msg.startswith(("❌", "⚠️"))
        ok = self._last_check_success

        # ---- 同步状态观测（供看板"最后同步 X 分钟前"徽标与运维排查）----
        try:
            await self.storage.set_sync_state(
                "sync_last_duration_ms", str(int((time.monotonic() - started_at) * 1000))
            )
            # 结果消息压平换行并截断到 200 字内留痕（失败时即为失败原因；
            # 落库前统一脱敏，防止异常消息里的 access_token 等凭据经看板外泄）
            await self.storage.set_sync_state(
                "sync_last_message", _sanitize_error_text(msg).replace("\n", " ")[:200]
            )
            await self.storage.set_sync_state("sync_last_status_failed", "0" if ok else "1")
            if ok:
                await self.storage.set_sync_state("sync_last_success_ts", str(int(time.time())))
        except Exception as ex:
            logger.debug(f"Failed to persist sync state: {ex}")

        # ---- 降价监控：成功路径末尾异步触发价格巡检（后台任务，不阻塞同步返回）----
        if ok:
            try:
                self._spawn(self._price_watch_sweep())
            except Exception as ex:
                logger.debug(f"Failed to schedule price watch sweep: {ex}")
        return msg

    async def _recover_after_auth_expired(self, error: SteamAuthExpiredError | None = None) -> bool:
        """Steam 401/403 后先检查显式安全错误，再尝试复用授权（不绕过冷却）。

        get_shared_library / get_family_group_id 等遇 SteamAuthExpiredError 时共用，
        返回 True 表示 access_token 已被强制刷新，调用方可安全重试原请求一次。
        """
        logger.info("Steam 返回 401/403，触发强制保活链路后重试...")
        try:
            if error is not None:
                await self.auth_manager.record_api_auth_failure(error.eresult)
            return await self.auth_manager.ensure_valid_access_token(force=True)
        except Exception as e:
            logger.error(f"Force keepalive after 401 failed: {_sanitize_error(e)}")
            return False

    def _auth_failure_message(self) -> str:
        note = self.auth_manager.last_keepalive_note
        return (
            f"⚠️ Steam 凭据未被服务端接受，本次未能恢复：{note or '请检查网络、授权或账号安全提示'}。"
            "请先处理 Steam 官方安全提示并等待冷却，不要连续扫码或密码登录。"
        )

    async def _check_and_notify_core(self) -> str:
        self._refresh_config()
        try:
            await self.auth_manager.ensure_state_loaded()
        except Exception as exc:
            return f"⚠️ 无法读取认证安全状态，已停止本次 Steam 请求：{_sanitize_error(exc)}"
        if self.auth_manager.security_blocked:
            return "⚠️ Steam 已返回账号/IP/设备安全限制，插件已暂停 Steam 请求；请先通过 Steam 官方支持处理账号限制"
        logger.info(
            f"Checking Steam Family. Token present: {bool(self.config.access_token)}, "
            f"RefreshToken present: {bool(self.config.refresh_token)}, "
            f"Cookie present: {bool(self.config.cookie)}, "
            f"SteamID: {self.config.steam_id}"
        )

        # 1. 认证预检：优先复用已有授权，安全限制时停止。
        #    Access Token 新鲜 -> 零开销直接通过；
        #    即将过期/已过期 -> Refresh Token / Cookie 复用；
        #    仅显式启用且旧授权确认失效后进行密码重登。
        try:
            alive = await self.auth_manager.ensure_valid_access_token()
            if not alive:
                logger.warning(f"Token 保活预检未完成: {self.auth_manager.last_keepalive_note}")
        except SteamLoginError as e:
            logger.warning(f"Token 保活预检异常: {e}")
        except Exception as e:
            logger.error(f"Token 保活预检异常: {_sanitize_error(e)}", exc_info=True)

        if self.auth_manager.security_blocked:
            return "⚠️ Steam 返回账号/IP/设备安全限制，认证及同步已暂停；请通过 Steam 官方支持处理，不要反复扫码或密码登录"

        jwt_info = decode_steam_jwt(self.config.access_token)

        # 2. Token 仍缺失时给出针对性指引
        if not self.config.access_token:
            return self._auth_failure_message()

        # SteamID 缺失时自动从 Access Token JWT 声明或 Refresh Token 声明推导
        if not self.config.steam_id and jwt_info and jwt_info.get("steamid"):
            self.config.steam_id = jwt_info["steamid"]
            logger.info(
                f"Automatically extracted SteamID {self.config.steam_id} from Access Token JWT."
            )
        if not self.config.steam_id:
            derived = steamid_from_refresh_token(self.config.refresh_token or "")
            if derived:
                self.config.steam_id = derived
                logger.info(f"Automatically derived SteamID {derived} from Refresh Token JWT.")

        if not self.config.steam_id:
            return "⚠️ 未检测到配置：未填写 17 位 SteamID，请在 WebUI 中填入你的 Steam 64 位 ID。"

        try:
            group_id = self.config.family_group_id
            if not group_id:
                logger.info(f"Fetching family group id for SteamID {self.config.steam_id}...")
                try:
                    group_id = await self.client.get_family_group_id(
                        self.config.access_token, self.config.steam_id
                    )
                except SteamAuthExpiredError as exc:
                    # 与 get_shared_library 相同的 401 恢复链：强制保活后重试一次，
                    # 避免 Token 恰好过期时直接落到底部兜底文案而错过自动恢复。
                    recovered = await self._recover_after_auth_expired(exc)
                    if not recovered:
                        return self._auth_failure_message()
                    group_id = await self.client.get_family_group_id(
                        self.config.access_token, self.config.steam_id
                    )
                if not group_id:
                    return f"⚠️ 无法获取家庭组 ID。该账号 ({self.config.steam_id}) 可能未加入任何 Steam 家庭组，或 Token 权限不足。"
                self.config.family_group_id = group_id
                logger.info(f"Detected family group id: {group_id}")

            logger.info(f"Fetching shared library apps for group {group_id}...")
            try:
                current_apps = await self.client.get_shared_library(
                    group_id,
                    self.config.access_token,
                    filter_non_games=self.config.filter_non_games,
                )
            except SteamAuthExpiredError as exc:
                # 先检查安全限制，复用现有授权后最多重试一次。
                recovered = await self._recover_after_auth_expired(exc)
                if recovered:
                    current_apps = await self.client.get_shared_library(
                        group_id,
                        self.config.access_token,
                        filter_non_games=self.config.filter_non_games,
                    )
                else:
                    return self._auth_failure_message()

            if not current_apps:
                return f"⚠️ 成功连接到 Steam 家庭组 (ID: {group_id})，但获取到的共享游戏列表为空（可能家庭组内无共享游戏或被 Steam 过滤）。"

            # 游玩统计接线（0.3.0）：GetPlaytimeSummary 此前从未在生产链路被调用，
            # 这里在成功拿到 current_apps 后顺手刷新成员游玩时长与最近游玩记录
            # （try/except 记 debug，任何失败都不阻塞检查主流程）。
            try:
                playtime_entries = await self.client.get_playtime_summary(
                    group_id, self.config.access_token
                )
                if playtime_entries:
                    await self.storage.refresh_playtime_stats(playtime_entries)
            except SteamAuthExpiredError as exc:
                await self.auth_manager.record_api_auth_failure(exc.eresult)
                if self.auth_manager.security_blocked:
                    return self._auth_failure_message()
            except Exception as ex:
                logger.debug(f"Failed to refresh playtime stats: {ex}")

            # 3. Synchronize member profiles & real nicknames
            all_owners = list(
                {
                    str(sid)
                    for app in current_apps
                    for sid in app.owner_steamids
                    if str(sid).isdigit()
                }
            )
            if all_owners:
                try:
                    profiles = await self.client.get_player_summaries(
                        all_owners, self.config.access_token
                    )
                    if profiles:
                        await self.storage.save_member_profiles(profiles)
                except Exception as ex:
                    logger.debug(f"Failed to refresh member profiles: {ex}")

            # member_profiles 昵称解析已下沉到推送管线（_drain_pending_notifications）内统一获取
            prev_snapshot = await self.storage.get_latest_snapshot()
            diff = self.diff_engine.compute_diff(current_apps, prev_snapshot)

            # Persist latest snapshot（先落库：基线永远以持久化为准，播报失败不会丢变动）
            await self.storage.save_snapshot(current_apps)

            if diff.is_first_run:
                logger.info(f"Steam Family initial baseline saved with {len(current_apps)} games.")
                return f"🎉 首次同步成功！已成功建立家庭库基线数据，共同步【{len(current_apps)}】款游戏！\n📊 实时大盘已生成，可在 WebUI 查看最新全景大盘与义父榜。"

            if not diff.has_changes:
                return (
                    f"✅ 家庭库检查完成：当前库内共有 {len(current_apps)} 款游戏，暂无新游戏变动。"
                )

            # 变动事件统一入队（快照已落库，播报交给 pending_notifications 可靠队列）：
            # 引入防刷屏聚合安全阀：
            # 1. 新增游戏（added）：超过 5 款时静默入队但不单独狂发卡片，而是聚合成一条汇总消息；
            # 2. 移出游戏（removed）：超过 10 款（或占总数 15%）时跳过单条播报；
            # 3. 归属变化（owner_changes）与状态变化（status_changes）：超过 5 条时聚合汇总，绝不单条疯狂轰炸。
            added_total = len(diff.added_apps)
            large_added = added_total > 5
            added_enqueued = 0

            if large_added:
                # 超过 5 款新增，不再逐条发卡片轰炸群聊，聚合为一条汇总事件
                names_preview = "、".join(f"《{a.name}》" for a in diff.added_apps[:5])
                summary_text = (
                    f"📦 【Steam 家庭库批量入库】检测到大量游戏新入库（共 {added_total} 款）！\n"
                    f"包含 {names_preview} 等，已自动同步更新至大盘，不再逐条发卡片刷屏。"
                )
                await self.storage.enqueue_pending(
                    "summary_notice",
                    {"text": summary_text},
                    dedupe_key=f"summary_added:{time.time():.0f}",
                )
                added_enqueued = 1
                logger.info(f"新增 {added_total} 款游戏，已触发防刷屏聚合汇总播报。")
            else:
                for app in diff.added_apps:
                    await self.storage.enqueue_pending(
                        "added",
                        {
                            "appid": app.appid,
                            "name": app.name,
                            "owner_steamids": list(app.owner_steamids),
                        },
                        dedupe_key=f"added:{app.appid}",
                    )
                    added_enqueued += 1

            # 大规模移出安全阀：removed 数量异常多时（例如过滤开关切换 / 家庭组大变动），
            # 超过 10 款时聚合成一条汇总播报，避免疯狂刷屏。
            removed_total = len(diff.removed_apps)
            large_removal = removed_total > 10
            removed_enqueued = 0
            if large_removal:
                summary_text = (
                    f"🛡️ 【Steam 家庭库批量变动】检测到 {removed_total} 款游戏移出/状态变更，"
                    "已自动更新基线，跳过逐条播报。"
                )
                await self.storage.enqueue_pending(
                    "summary_notice",
                    {"text": summary_text},
                    dedupe_key=f"summary_removed:{time.time():.0f}",
                )
                removed_enqueued = 1
                logger.warning(f"检测到大规模移出 {removed_total} 款，已聚合为汇总消息播报。")
            else:
                for app in diff.removed_apps:
                    prev_app = prev_snapshot.get(app.appid) if prev_snapshot else None
                    if prev_app and int(prev_app.exclude_reason or 0) != 0:
                        logger.debug(
                            f"App {app.appid}《{app.name}》移出前已是排除态"
                            f"（exclude_reason={prev_app.exclude_reason}），忽略伪移出事件"
                        )
                        continue
                    prev_owners = (
                        list(prev_app.owner_steamids) if prev_app else list(app.owner_steamids)
                    )
                    await self.storage.enqueue_pending(
                        "removed",
                        {
                            "appid": app.appid,
                            "name": app.name,
                            "owner_steamids": prev_owners,
                        },
                        dedupe_key=f"removed:{app.appid}",
                    )
                    removed_enqueued += 1

            owner_changes_total = len(diff.owner_changes)
            if owner_changes_total > 5:
                # 归属变动超过 5 条（常见于修复 bug 或初次识别所有权时），聚合为一条汇总，绝不连续单条刷屏
                summary_text = f"🔁 【Steam 家庭库归属变动】检测到 {owner_changes_total} 款游戏的归属人更新，已自动刷新义父榜资产！"
                await self.storage.enqueue_pending(
                    "summary_notice",
                    {"text": summary_text},
                    dedupe_key=f"summary_owner:{time.time():.0f}",
                )
                logger.info(f"归属人变动 {owner_changes_total} 款，已聚合为单条汇总。")
            else:
                for oc in diff.owner_changes:
                    await self.storage.enqueue_pending(
                        "owner_change",
                        {
                            "appid": oc.app.appid,
                            "name": oc.app.name,
                            "added_owners": list(oc.added_owners),
                            "removed_owners": list(oc.removed_owners),
                        },
                        dedupe_key=f"owner:{oc.app.appid}",
                    )

            status_changes_total = len(diff.status_changes)
            if status_changes_total > 5:
                summary_text = f"⚠️ 【Steam 家庭库状态变动】检测到 {status_changes_total} 款游戏状态发生变更，已同步更新。"
                await self.storage.enqueue_pending(
                    "summary_notice",
                    {"text": summary_text},
                    dedupe_key=f"summary_status:{time.time():.0f}",
                )
                logger.info(f"状态变动 {status_changes_total} 款，已聚合为单条汇总。")
            else:
                for sc in diff.status_changes:
                    await self.storage.enqueue_pending(
                        "status_change",
                        {
                            "appid": sc.app.appid,
                            "name": sc.app.name,
                            "field": sc.field,
                            "old": sc.old,
                            "new": sc.new,
                        },
                        dedupe_key=f"status:{sc.app.appid}:{sc.field}",
                    )

            queued_count = (
                added_enqueued
                + removed_enqueued
                + (1 if owner_changes_total > 5 else owner_changes_total)
                + (1 if status_changes_total > 5 else status_changes_total)
            )

            # 入队完成后立即触发一次推送管线（失败事件留在队列中按指数退避自动重试）
            drain_stats = await self._drain_pending_notifications()

            # ------------------------------------------------------------------
            # 组装面向用户的返回消息：基于"已入队 N 条事件 + 管线处理结果"，
            # 前缀语义须遵守受控消息协议（❌/⚠️ 开头 = 本次检查未成功）。
            # ------------------------------------------------------------------
            lines: list[str] = []
            if large_removal:
                # 注意：此处为"信息性提示"（检查本身成功），禁用 ⚠️ 前缀以免被
                # 受控消息协议分类器误判为同步失败（会触发无谓轮询退避 + 看板红标）。
                lines.append(f"🛡️ 检测到大规模移出（{removed_total} 款），已跳过播报仅更新基线。")
            if queued_count > 0:
                detail = (
                    f"✅ 家庭库变动检查完成：已入队 {queued_count} 条变动事件"
                    f"（新增 {added_enqueued} 款 / 移出 {removed_enqueued} 款 / "
                    f"归属变动 {len(diff.owner_changes)} 条 / 状态变化 {len(diff.status_changes)} 条）。\n"
                    f"📨 本次推送管线：成功发送 {drain_stats['success']} 条"
                )
                if drain_stats["failed"]:
                    detail += f"，{drain_stats['failed']} 条将在退避后自动重试"
                if drain_stats.get("abandoned"):
                    detail += f"，{drain_stats['abandoned']} 条连续失败达上限已放弃（详见日志）"
                if drain_stats["filtered"]:
                    # filtered 与 stats 口径一致：免费过滤 / 未知类型 / 过期事件均不计入"成功发送"
                    detail += f"，{drain_stats['filtered']} 条已静默消解（免费游戏/过期事件）"
                processed_now = (
                    drain_stats["success"]
                    + drain_stats["filtered"]
                    + drain_stats["failed"]
                    + drain_stats.get("abandoned", 0)
                )
                if processed_now == 0:
                    # 撞上并行进行中的 drain（轮询/启动补播抢先清空队列）：
                    # 事件已被处理，本条消息若说"成功发送 0 条"会误导用户，改为如实说明。
                    detail += "\nℹ️ 事件已由并行进行中的推送管线接管处理，本条消息未重复推送。"
                detail += "。"
                lines.append(detail)
            else:
                lines.append(f"✅ 家庭库检查完成：基线已更新为 {len(current_apps)} 款游戏。")
            return "\n".join(lines)
        except SteamAuthExpiredError as exc:
            await self.auth_manager.record_api_auth_failure(exc.eresult)
            return self._auth_failure_message()
        except SteamRateLimitError:
            return "⚠️ 请求过于频繁触发 Steam 限流 (429)，请稍后重试。"
        except Exception as e:
            # 兜底路径必须脱敏：httpx 异常 str 里含完整 access_token=eyJ...，
            # 原样返回即泄漏进群聊，因此日志与返回消息统一使用打码后的文本。
            reason = _sanitize_error(e)
            logger.error(f"Error during steam family check: {reason}", exc_info=True)
            return (
                f"❌ 检查家庭库失败: {reason}。"
                "若在中国大陆服务器运行，请检查网络是否能直连 Steam API 或在 WebUI 配置代理 (proxy)。"
            )

    # ------------------------------------------------------------------
    # 统一收款/播报管线：领取 pending_notifications 待推送队列并逐条分发播报。
    # ------------------------------------------------------------------
    async def _drain_pending_notifications(self, batch_limit: int = 50) -> dict[str, int]:
        """统一收款/播报管线：把队列中的待推送事件真正播报出去（核心"欠账补播"器）。

        设计要点（消灭丢通知 / 重复推送竞态的核心）：
          1. 变动事件先持久化于 pending_notifications 表，本方法按到期顺序 claim 领取；
             单条事件渲染/详情/enrich 全程 try/except 包裹，失败只 bump_pending_retry
             （指数退避 300*2^n、封顶 3600s），绝不让异常逃出炉子，也绝不丢事件；
          2. 全程持有 _drain_lock：检查完成 / 轮询 / 启动补播多处并发触发时串行领取，
             避免同一事件被并发 claim 导致重复推送；
          3. 对每个订阅者维护推送失败计数：失败 record_push_failure、成功 reset 后，
             收尾统一 prune_dead_subscribers 清理彻底失联的订阅者。

        Returns:
            dict: {"success": 成功播报条数, "failed": 待重试条数,
                   "filtered": 静默消解条数（免费过滤 / 未知类型 / 过期事件，不计入成功）,
                   "abandoned": 连续失败达重试上限被放弃的条数（死信已记日志）}
        """
        stats = {"success": 0, "failed": 0, "filtered": 0, "abandoned": 0}
        try:
            async with self._drain_lock:
                profiles = await self.storage.get_member_profiles()
                subscribers = await self.storage.get_subscribers()
                if not subscribers:
                    # 无订阅者时不消费队列：不 claim、不 mark done、不动退避状态，
                    # 事件完整保留给未来的订阅者（防止空订阅期间把积压清空）。
                    logger.warning(
                        "当前没有任何订阅者，本轮推送管线跳过（积压事件保留，待有人订阅后补播）。"
                    )
                    return stats
                ctx = _NotifyContext(profiles, subscribers, stats)
                processed = 0
                while processed < _DRAIN_MAX_EVENTS:
                    events = await self.storage.claim_due_pending(batch_limit)
                    if not events:
                        break

                    # 批量预取：本轮 added 事件统一走 get_app_details_batch（批量拿详情；
                    # 熔断期只回缓存不抛错；单个失败表现为结果缺键，由事件处理单发兜底）
                    added_events = [e for e in events if e.get("kind") == "added"]
                    batch_details: dict[int, object] = {}
                    failed_event_ids: set[int] = set()
                    if added_events:
                        collect_ids = [
                            int(e["payload"].get("appid", 0) or 0)
                            for e in added_events
                            if isinstance(e.get("payload"), dict)
                        ]
                        collect_ids = [aid for aid in collect_ids if aid > 0]
                        try:
                            if collect_ids:
                                batch_details = await self.client.get_app_details_batch(collect_ids)
                        except Exception as ex:
                            # 批量路径抛错（如单发兜底遇真实 429 熔断）：本轮 added 全部退避重试；
                            # bump 返回 False 表示已达重试上限被删除（死信已记日志），须如实计入 abandoned
                            logger.warning(
                                f"批量获取商店详情失败，{len(added_events)} 条入库事件将退避重试: {ex}"
                            )
                            for e in added_events:
                                if await self.storage.bump_pending_retry(e["id"]):
                                    stats["failed"] += 1
                                else:
                                    stats["abandoned"] += 1
                                failed_event_ids.add(e["id"])
                                processed += 1
                            added_events = []

                    # 过时事件对账基准（方案B）：storage 不支持按 dedupe_key 删除待处理行，
                    # 无法在入队时级联删除同 appid 的对方事件，改为消费前统一对账最新快照
                    # （详见 _process_pending_event）。本批含 added/removed 时取一次快照，
                    # 获取失败（None）则跳过对账按原语义播报。
                    reconcile_snapshot = None
                    kinds_in_batch = {str(e.get("kind", "")) for e in events}
                    if "added" in kinds_in_batch or "removed" in kinds_in_batch:
                        try:
                            reconcile_snapshot = await self.storage.get_latest_snapshot()
                        except Exception as ex:
                            logger.debug(f"drain 取对账快照失败（跳过对账播报）: {ex}")

                    for ev in events:
                        ev_id = ev.get("id")
                        if ev_id in failed_event_ids:
                            continue
                        try:
                            result = await self._process_pending_event(
                                ev, batch_details, ctx, reconcile_snapshot
                            )
                        except Exception as ex:
                            logger.error(
                                f"处理待推送事件 #{ev_id} ({ev.get('kind')}) 异常: {_sanitize_error(ex)}",
                                exc_info=True,
                            )
                            result = "retry"
                        if result == "done":
                            await self.storage.mark_pending_done(ev_id)
                            stats["success"] += 1
                        elif result == "filtered":
                            # 事件按规则静默消解（免费过滤/未知类型/过期事件）：不再重试，
                            # 但不计入 success，保证 stats 口径里"成功发送"只统计真实播报。
                            await self.storage.mark_pending_done(ev_id)
                            stats["filtered"] += 1
                        else:
                            # "retry"（或兼容任何非 done/filtered 返回值）一律退避重试；
                            # bump 返回 False = 已达重试上限、事件被删除（删除前死信已记 error 日志），
                            # 统计与文案必须如实区分"将重试"与"已放弃"。
                            if await self.storage.bump_pending_retry(ev_id):
                                stats["failed"] += 1
                            else:
                                stats["abandoned"] += 1
                                logger.warning(
                                    f"待推送事件 #{ev_id} ({ev.get('kind')}) 连续失败已达上限，"
                                    f"已放弃投递（死信已记录）"
                                )
                            processed += 1

                # 收尾：清理推送失败计数达到上限的失效订阅者（记录被清理列表日志）
                try:
                    pruned = await self.storage.prune_dead_subscribers()
                    if pruned:
                        logger.info(f"已自动清理推送连续失败达上限的失效订阅者: {pruned}")
                except Exception as ex:
                    logger.debug(f"prune_dead_subscribers failed: {ex}")
                return stats
        except Exception as ex:
            # 管线级兜底：任何意外异常都不允许让后台任务崩溃
            logger.error(
                f"Drain pending notifications failed: {_sanitize_error(ex)}", exc_info=True
            )
            return stats

    async def _process_pending_event(
        self, event: dict, batch_details: dict, ctx: _NotifyContext, reconcile_snapshot=None
    ) -> str:
        """按 kind 分发单条待推送事件（三态返回，供 drain 精确记账）。

        Args:
            reconcile_snapshot: 本批事件统一的最新快照对账基准（None=跳过对账）。

        Returns:
            "done": 事件正常播报完成，计入 success；
            "filtered": 事件按规则静默消解（免费过滤/未知类型/过期事件），计入 filtered；
            "retry": 处理失败，退避重试。
        """
        kind = str(event.get("kind", ""))
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        appid = int(payload.get("appid", 0) or 0)
        name = str(payload.get("name") or "") or (f"App {appid}" if appid else "未知应用")

        # 未知类型：直接消解，避免毒丸事件无限重试；payload 截断到 200 字防日志刷屏
        if kind not in (
            "added",
            "removed",
            "owner_change",
            "status_change",
            "price_drop",
            "summary_notice",
        ):
            logger.warning(
                f"收到未知类型待推送事件 kind={kind}，跳过播报。payload={str(payload)[:200]}"
            )
            return "filtered"

        if kind == "summary_notice":
            text = str(payload.get("text", ""))
            if text:
                await self._send_text_to_subscribers(text, ctx)
            return "done"

        # ------------------------------------------------------------------
        # 过时事件对账（方案B）：storage 不提供按 dedupe_key 删除待处理行的能力，
        # 无法在入队 added:{appid} 时级联删除未发送的 removed:{appid}（反之亦然），
        # 因此改为消费前对账最新快照：
        #   - "移出"事件：若该 appid 已重新在库（先移出又入库的竞态），此"移出"已过时，
        #     静默消解，避免用户先收到"移出"紧接着又收到"入库"的自相矛盾播报；
        #   - "入库"事件：若最新快照中已无该 appid（入队后又被移出），同理静默消解，
        #     避免播报"新游戏入库"后又立刻"移出"。
        # 快照缺失（从未同步过）时无法对账，按原语义照常播报。
        # ------------------------------------------------------------------
        if reconcile_snapshot is not None and kind == "removed" and appid in reconcile_snapshot:
            logger.info(f"过时移出事件已静默消解：《{name}》(AppID {appid}) 已重新在库。")
            return "filtered"
        if reconcile_snapshot is not None and kind == "added" and appid not in reconcile_snapshot:
            logger.info(f"过时入库事件已静默消解：《{name}》(AppID {appid}) 已不在最新快照中。")
            return "filtered"

        if kind == "added":
            return await self._notify_added_event(appid, name, payload, batch_details, ctx)
        if kind == "removed":
            return await self._notify_removed_event(appid, name, payload, ctx)
        if kind == "owner_change":
            return await self._notify_owner_change_event(appid, name, payload, ctx)
        if kind == "status_change":
            return await self._notify_status_change_event(appid, name, payload, ctx)
        return await self._notify_price_drop_event(appid, name, payload, ctx)

    @staticmethod
    def _resolve_member_name(steamid: str, profiles: dict) -> str:
        """按 SteamID 解析成员昵称（member_profiles 命中用真实昵称，否则"成员_后四位"兜底）。"""
        p = profiles.get(str(steamid))
        if p and p.get("persona_name"):
            return str(p["persona_name"])
        sid = str(steamid)
        return f"成员_{sid[-4:]}" if len(sid) >= 4 else sid

    async def _send_text_to_subscribers(self, text: str, ctx: _NotifyContext) -> int:
        """把纯文本消息推送给全部订阅者，并按目标维护推送成功/失败计数。返回成功送达数。"""
        return await self._send_chain_to_subscribers(MessageChain([Comp.Plain(text)]), ctx)

    async def _send_chain_to_subscribers(self, chain, ctx: _NotifyContext) -> int:
        """把消息链推送给全部订阅者：成功 reset_push_failures，失败 record_push_failure。"""
        delivered = 0
        for target_id in ctx.subscribers:
            try:
                if hasattr(self.context, "send_message"):
                    await self.context.send_message(target_id, chain)
                await self.storage.reset_push_failures(target_id)
                delivered += 1
            except Exception as ex:
                logger.error(f"Failed to send push to {target_id}: {ex}")
                try:
                    await self.storage.record_push_failure(target_id)
                except Exception as inner_ex:
                    logger.debug(f"record_push_failure({target_id}) failed: {inner_ex}")
        return delivered

    async def _notify_added_event(
        self, appid: int, name: str, payload: dict, batch_details: dict, ctx: _NotifyContext
    ) -> str:
        """播报"新游戏入库"事件：批量详情优先、单个兜底；免费过滤宁缺毋滥，未知详情宁多勿漏。

        Returns: "done"（正常播报）/ "filtered"（免费游戏按设置静默忽略）。
        """
        details = batch_details.get(appid) if batch_details else None
        if details is None:
            # 批量结果缺键（该 id 单发失败）→ 单发兜底；include_review=True 补真实好评率与评价简述
            details = await self.client.get_app_details(appid, include_review=True)

        if details is not None:
            # 回填 apps_cache（Web 大盘价格/评价数据依赖），失败不影响播报
            try:
                await self.storage.cache_app_details(details)
            except Exception as ex:
                logger.debug(f"cache_app_details({appid}) failed: {ex}")
            # 真实评价数据按需补全：批量路径不带评价字段（无评价数据时保持中性缺省，不虚构）
            if not details.review_summary:
                try:
                    review = await self.client.get_review_summary(appid)
                    if review:
                        details.positive_rate, details.review_summary = review
                except Exception as ex:
                    logger.debug(f"get_review_summary({appid}) failed: {ex}")

        # ---- 免费过滤（收紧后语义）----
        # 仅当商店详情明确 is_free=True 才按免费过滤。0/0 报价且 is_free=False 的游戏
        # （未发售 / 商店暂无报价）并非免费游戏，照常播报，防止被静默吞掉；
        # details 为 None（商店查询失败）同样视为未知 → 宁多勿漏，照常播报。
        if details is not None and details.is_free is True and not self.config.broadcast_free_games:
            logger.info(f"App 《{name}》(AppID: {appid}) 为免费游戏，根据设置已自动忽略入库播报。")
            return "filtered"  # 事件按设置静默消费完毕（filtered 计数由 drain 管线统一记账）

        # 解析真实昵称
        owner_sids = payload.get("owner_steamids") or []
        owners_display = (
            "、".join(self._resolve_member_name(sid, ctx.profiles) for sid in owner_sids)
            if owner_sids
            else "家庭成员"
        )

        # 价格展示：details 拿到时按真实报价展示；details=None（商店查询失败）时
        # 价格未知，不虚构 "¥ 0.00 / 免费" 误导用户以为游戏免费入库。
        if details is not None:
            initial_price_display = (
                f"¥ {details.initial_price:.2f}" if details.initial_price > 0 else "¥ 0.00"
            )
            final_price_display = (
                f"¥ {details.final_price:.2f}" if details.final_price > 0 else "免费/已入库"
            )
        else:
            initial_price_display = "价格未知"
            final_price_display = "价格未知（商店查询失败）"

        # Render card（无评价数据时中性展示：好评率 0、摘要"暂无评价"，绝不虚构）
        card_data = {
            "appid": appid,
            "game_name": (details.chinese_name if details and details.chinese_name else name)
            or name,
            "original_name": name,
            "header_image": (details.header_image if details else "")
            or f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg",
            "owner_name": owners_display,
            "initial_price": initial_price_display,
            "final_price": final_price_display,
            "discount_percent": details.discount_percent if details else 0,
            "review_summary": (details.review_summary if details else "") or "暂无评价",
            "positive_rate": details.positive_rate if details else 0,
            "genres": (details.genres if details and details.genres else None) or ["Steam 共享"],
        }

        rendered_image = await self._render_card_image(card_data)
        if rendered_image:
            chain = MessageChain([Comp.Image.fromURL(rendered_image)])
        else:
            text = (
                f"✨ 【Steam 家庭库新游戏入库】\n"
                f"🎮 《{card_data['game_name']}》\n"
                f"👑 入库义父: {owners_display}\n"
                f"💰 价格: {card_data['final_price']}\n"
                f"🔗 商店: https://store.steampowered.com/app/{appid}"
            )
            chain = MessageChain([Comp.Plain(text)])
        await self._send_chain_to_subscribers(chain, ctx)
        return "done"

    async def _notify_removed_event(
        self, appid: int, name: str, payload: dict, ctx: _NotifyContext
    ) -> bool:
        """播报"移出家庭共享库"事件（纯文字：商店链接 + 曾归属昵称）。"""
        prev_owner_sids = payload.get("owner_steamids") or []
        owners_display = (
            "、".join(self._resolve_member_name(sid, ctx.profiles) for sid in prev_owner_sids)
            if prev_owner_sids
            else "家庭成员"
        )
        text = (
            f"🚪 【Steam 家庭库移出】《{name}》(AppID {appid}) 已离开家庭共享库\n"
            f"🔗 商店: https://store.steampowered.com/app/{appid}\n"
            f"👥 曾归属: {owners_display}"
        )
        await self._send_text_to_subscribers(text, ctx)
        return "done"

    async def _notify_owner_change_event(
        self, appid: int, name: str, payload: dict, ctx: _NotifyContext
    ) -> bool:
        """播报"归属变动"事件：新增义父 / 失去义父。"""
        added_names = [
            self._resolve_member_name(sid, ctx.profiles)
            for sid in (payload.get("added_owners") or [])
        ]
        lost_names = [
            self._resolve_member_name(sid, ctx.profiles)
            for sid in (payload.get("removed_owners") or [])
        ]
        parts = []
        if added_names:
            parts.append(f"新增义父 {'、'.join(added_names)}")
        if lost_names:
            parts.append(f"失去义父 {'、'.join(lost_names)}")
        text = f"🔁 《{name}》归属变动：{' / '.join(parts) if parts else '无归属变化'}"
        await self._send_text_to_subscribers(text, ctx)
        return "done"

    async def _notify_status_change_event(
        self, appid: int, name: str, payload: dict, ctx: _NotifyContext
    ) -> bool:
        """播报"状态/共享属性变化"事件（支持中文人类友好提示）。"""
        field = str(payload.get("field", ""))
        old_val = payload.get("old", 0)
        new_val = payload.get("new", 0)

        # 针对 exclude_reason（家庭共享属性）做专门的人类友好解读
        if field == "exclude_reason":
            reason_map = {
                0: "支持家庭共享（正常在库）",
                1: "号主设置为私密/仅自己可见",
                2: "开发者不支持共享",
                3: "发行商已退出家庭共享",
                4: "区域限制不可共享",
                5: "账号封禁或受限",
                6: "免费周末/临时许可到期",
                7: "需要第三方启动器（如 EA/育碧）不支持共享",
                8: "原许可证失效或已退款",
            }
            old_desc = reason_map.get(int(old_val), f"受限({old_val})")
            new_desc = reason_map.get(int(new_val), f"受限({new_val})")

            if int(old_val) != 0 and int(new_val) == 0:
                # 原来不可共享，现在新增/解锁了家庭共享！
                text = (
                    f"🎉 【Steam 共享属性解锁】《{name}》(AppID {appid})\n"
                    f"✨ 发行商或号主已开放家庭共享权限，大家现在可以一起游玩了！\n"
                    f"🔗 商店: https://store.steampowered.com/app/{appid}"
                )
            elif int(old_val) == 0 and int(new_val) != 0:
                # 原来可以共享，现在被去除了家庭共享！
                text = (
                    f"🚫 【Steam 共享属性被去除】《{name}》(AppID {appid})\n"
                    f"⚠️ 该游戏已被移出家庭共享库！\n"
                    f"📌 原因: {new_desc}\n"
                    f"🔗 商店: https://store.steampowered.com/app/{appid}"
                )
            else:
                text = (
                    f"⚠️ 【Steam 共享状态变更】《{name}》(AppID {appid})\n"
                    f"变动: {old_desc} → {new_desc}"
                )
        elif field == "app_type":
            type_map = {1: "游戏", 2: "软件", 4: "DLC", 8: "Demo", 16: "视频"}
            old_desc = type_map.get(int(old_val), str(old_val))
            new_desc = type_map.get(int(new_val), str(new_val))
            text = f"ℹ️ 《{name}》类型变更: {old_desc} → {new_desc}"
        else:
            text = f"⚠️ 《{name}》状态变化 {field}: {old_val} → {new_val}"

        await self._send_text_to_subscribers(text, ctx)
        return "done"

    async def _notify_price_drop_event(
        self, appid: int, name: str, payload: dict, ctx: _NotifyContext
    ) -> bool:
        """播报"降价提醒"事件（kind 骨架，payload 字段由后续价格监控代理写入）。"""
        old_price = payload.get("old_price", payload.get("old", "?"))
        new_price = payload.get("new_price", payload.get("new", "?"))
        discount = payload.get("discount_percent", payload.get("discount", 0))
        text = f"📉 降价提醒：《{name}》 ¥{old_price} → ¥{new_price} (折扣 {discount}%)"
        await self._send_text_to_subscribers(text, ctx)
        return "done"

    # ------------------------------------------------------------------
    # 家庭库游戏降价监控（价格巡检）：每 20 小时一轮、单轮最多 120 款，
    # 对比 apps_cache 已缓存价格与商店实时 price_overview，降价/折扣加深即入队播报。
    # ------------------------------------------------------------------
    async def _price_watch_sweep(self) -> None:
        """家庭库游戏降价监控巡检（由 _check_and_notify_impl 成功路径异步触发）。

        流程：
          1. enable_price_watch 关闭时直接 return；
          2. 以 sync_price_watch_last_ts 节流：距上次巡检不足 20 小时直接 return
             （时间戳在巡检开头先行落库，中途崩溃/重启也不会立即重复扫全库）；
          3. 顺手调用 storage.prune_snapshots() 修剪历史快照（与检查流程二选一，不重复）；
          4. 取最新快照中的 appids（上限 120 款/轮，多余的下一轮再扫），
             并只对比 apps_cache 已有记录的游戏（从未缓存过的游戏直接跳过）；
          5. get_price_overview_batch 拉取实时价格；判定降价：
             old.final_price > 0 且 new_final > 0 且（new_final < old.final_price
             或 new_discount > old.discount_percent）→ 入队 price_drop 事件
             （payload 键名与 _notify_price_drop_event 的解析逻辑匹配）；
          6. 无论是否降价，成功拿到新价格都 update_prices 刷新缓存；
          7. 单个游戏的异常只记 debug，不影响整轮扫描。
        """
        try:
            if not self.config.enable_price_watch:
                return

            # 节流：20 小时内已巡检过则直接跳过（键写入 sync_ 前缀 kv 表）
            last_ts_raw = await self.storage.get_sync_state("price_watch_last_ts")
            try:
                last_ts = float(last_ts_raw) if last_ts_raw else 0.0
            except (TypeError, ValueError):
                last_ts = 0.0
            now = time.time()
            if last_ts > 0 and (now - last_ts) < _PRICE_WATCH_MIN_INTERVAL_SECONDS:
                return
            # 先落时间戳再干活，保证"每 20 小时最多扫一轮"语义
            await self.storage.set_sync_state("price_watch_last_ts", str(int(now)))

            # 顺手修剪历史快照，防止 library_snapshots 无限膨胀
            try:
                await self.storage.prune_snapshots()
            except Exception as ex:
                logger.debug(f"prune_snapshots in price watch sweep failed: {ex}")

            snapshot = await self.storage.get_latest_snapshot()
            if not snapshot:
                return
            # 用 get_all_cached_apps 一次性建 map（storage 只有单条 get_cached_app，
            # 逐条循环取会放大 DB 往返次数）
            cached_map = {a.appid: a for a in await self.storage.get_all_cached_apps()}
            # 只监控 apps_cache 已有记录的游戏，按 appid 排序后基于 kv 游标做确定性轮转：
            # 每轮只扫 _PRICE_WATCH_MAX_APPS 款，游标（price_watch_cursor_index，复用
            # set_sync_state/get_sync_state 落库）记录下一轮起点，扫到队尾回绕起点，
            # 保证库内所有游戏都会被周期性覆盖，而不是永远只扫前 120 款。
            watch_ids_all = sorted(aid for aid in snapshot.keys() if aid in cached_map)
            if not watch_ids_all:
                return
            cursor_raw = await self.storage.get_sync_state("price_watch_cursor_index")
            try:
                cursor = int(cursor_raw) if cursor_raw else 0
            except (TypeError, ValueError):
                cursor = 0
            total = len(watch_ids_all)
            if cursor < 0 or cursor >= total:
                cursor = 0  # 快照变化/游标非法（如库内游戏数变少）→ 安全回绕到起点
            window = watch_ids_all[cursor : cursor + _PRICE_WATCH_MAX_APPS]
            if cursor > 0 and len(window) < _PRICE_WATCH_MAX_APPS:
                # 尾部不足一批时回绕补齐开头一段，保证每轮尽量用满单轮巡检配额
                window += watch_ids_all[: _PRICE_WATCH_MAX_APPS - len(window)]
            watch_ids = window
            # 推进游标并落库（下一轮从新位置继续轮转）
            await self.storage.set_sync_state(
                "price_watch_cursor_index", str((cursor + _PRICE_WATCH_MAX_APPS) % total)
            )

            prices = await self.client.get_price_overview_batch(watch_ids)
            for appid in watch_ids:
                try:
                    new_price = prices.get(appid)
                    if new_price is None:
                        # 免费游戏（data=[]）/ 该 id 请求失败：结果缺键，直接跳过
                        continue
                    new_initial, new_final, new_discount = new_price
                    old = cached_map[appid]
                    # 降价判定：old 有正价且（新价更低 或 折扣加深）
                    is_drop = (
                        old.final_price > 0
                        and new_final > 0
                        and (new_final < old.final_price or new_discount > old.discount_percent)
                    )
                    if is_drop:
                        name = old.chinese_name or old.name or f"App {appid}"
                        logger.info(
                            f"降价监控：《{name}》(AppID {appid}) "
                            f"¥{old.final_price:.2f} → ¥{new_final:.2f}（折扣 {new_discount}%）"
                        )
                        await self.storage.enqueue_pending(
                            "price_drop",
                            {
                                "appid": appid,
                                "name": name,
                                "old_price": round(float(old.final_price), 2),
                                "new_price": round(float(new_final), 2),
                                "discount_percent": int(new_discount),
                            },
                            dedupe_key=f"price_drop:{appid}:{int(new_final * 100)}",
                        )
                    # 无论是否降价都刷新缓存价格（保持看板估值与商店同步）
                    await self.storage.update_prices(
                        appid, new_initial, new_final, int(new_discount)
                    )
                except Exception as ex:
                    # 单个游戏的异常不影响整轮扫描
                    logger.debug(f"价格监控处理 AppID {appid} 失败: {ex}")
        except SteamRateLimitError as ex:
            logger.warning(f"降价监控巡检触发 429 熔断，本轮中止: {ex}")
        except Exception as ex:
            logger.debug(f"降价监控巡检中止: {ex}")

    async def _polling_loop(self):
        # 连续失败计数：成功归零；失败时按 _compute_poll_interval_secs 指数退避拉长间隔
        consecutive_failures = 0
        while True:
            try:
                # 首次等待不受影响（consecutive_failures=0 → 基础间隔）
                interval_secs = _compute_poll_interval_secs(
                    self.config.poll_interval_minutes, consecutive_failures
                )
                if consecutive_failures > 0:
                    logger.warning(
                        f"家庭库检查已连续失败 {consecutive_failures} 次，"
                        f"轮询间隔自适应退避至 {interval_secs} 秒。"
                    )
                await asyncio.sleep(interval_secs)

                # 每轮同步先检查凭据；复用、续签能力及有效期由 Steam 决定。
                has_credentials = bool(
                    self.config.access_token
                    or self.config.refresh_token
                    or self.config.cookie
                    or (self.config.account_name and self.config.password)
                )
                if has_credentials:
                    await self._check_and_notify()
                    # 依据受控消息协议维护的结构化成功标记更新退避计数
                    if self._last_check_success is False:
                        consecutive_failures += 1
                    elif self._last_check_success:
                        consecutive_failures = 0

                # 每轮顺手清一次推送队列：补播退避到期 / 上次失败的历史事件
                await self._drain_pending_notifications()
            except asyncio.CancelledError:
                break
            except Exception as e:
                # 意外异常（如 drain 抛错）同样计一次失败，触发下轮退避
                consecutive_failures += 1
                logger.error(f"Polling loop encountered error: {_sanitize_error(e)}")

    @staticmethod
    def _is_group_chat(event) -> bool:
        """防御性判断当前会话是否群聊（敏感凭据类命令的群聊拒绝用）。

        不同适配器的事件实现不一：优先 event.get_group_id()，回退 event.group_id
        属性；无法确认私聊时拒绝敏感凭据操作，不能以异常默认放行。
        """
        try:
            get_group_id = getattr(event, "get_group_id", None)
            if callable(get_group_id):
                return bool(get_group_id())
            if hasattr(event, "group_id"):
                return bool(event.group_id)
            return True
        except Exception:
            return True

    @filter.command("steam_bind", alias={"steam bind", "steam绑定"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_bind(self, event: AstrMessageEvent, access_token: str, steam_id: str = ""):
        """绑定 Steam Access Token 和 SteamID。例: /steam_bind eyJhb... 76561198..."""
        # 防御：access_token 属等值凭据，禁止在群聊中明文提交（聊天记录会永久留痕）
        if self._is_group_chat(event):
            yield event.plain_result("⚠️ 敏感凭据操作请勿在群聊执行，请私聊我或在 WebUI 配置。")
            return
        self._save_credentials("access_token", access_token)
        if steam_id:
            self._save_credentials("steam_id", steam_id)
        yield event.plain_result("✅ Steam 凭据绑定成功！正在立即拉取家庭库并更新大盘数据...")
        self._spawn(self._check_and_notify())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam_auth_resume", alias={"steam auth_resume", "steam恢复认证"})
    async def cmd_auth_resume(self, event: AstrMessageEvent):
        """管理员已在 Steam 官方渠道处理安全限制后，显式恢复本地暂停状态。"""
        if self._is_group_chat(event):
            yield event.plain_result(
                "⚠️ 请私聊管理员指令 /steam_auth_resume，并先处理 Steam 官方账号限制。"
            )
            return
        result = await self.auth_manager.resume_after_security_review()
        yield event.plain_result(result)

    @filter.command("steam_qr", alias={"steam qr", "steam扫码"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_qr_login(self, event: AstrMessageEvent):
        """扫码登录：固定浏览器设备身份，必须核对 Steam 官方 App 的位置与设备。"""
        if self._is_group_chat(event):
            yield event.plain_result(
                "⚠️ 登录二维码属于敏感授权，请私聊使用 /steam_qr，不要在群里登录。"
            )
            return
        umo = getattr(event, "unified_msg_origin", "test_umo")

        async def send_text(text: str):
            try:
                await self.context.send_message(umo, MessageChain([Comp.Plain(text)]))
            except Exception as ex:
                logger.error(f"Failed to send QR text: {ex}")

        async def send_image(data: bytes):
            try:
                await self.context.send_message(umo, MessageChain([Comp.Image.fromBytes(data)]))
            except Exception as ex:
                logger.error(f"Failed to send QR image: {ex}")
                await send_text("🖼️ 二维码图片发送失败，请直接复制上方链接到浏览器打开后扫码。")

        yield event.plain_result(
            "⏳ 正在创建 Steam 扫码会话；请核对设备和位置，账号受限时不要再次登录。"
        )
        try:
            result = await self.auth_manager.run_qr_login(
                send_text=send_text, send_image=send_image
            )
        except SteamLoginError as e:
            result = f"❌ 扫码登录失败: {e}"
        except Exception as e:
            logger.error(f"QR login error: {_sanitize_error(e)}", exc_info=True)
            result = f"❌ 扫码登录异常: {_sanitize_error(e)}"
        yield event.plain_result(result)
        if result.startswith("✅"):
            self._spawn(self._check_and_notify())

    @filter.command("steam_relogin", alias={"steam relogin", "steam重登"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_relogin(self, event: AstrMessageEvent):
        """手动密码登录：优先记住设备授权，需要手机确认时等待用户处理。"""
        if self._is_group_chat(event):
            yield event.plain_result("⚠️ 登录属于敏感凭据操作，请私聊使用 /steam_relogin。")
            return
        yield event.plain_result(
            "⏳ 正在登录：RSA 加密 → 复用设备授权 → Steam Guard 验证；请核对设备位置。"
        )

        async def notify_confirmation(text: str):
            await self.context.send_message(
                event.unified_msg_origin, MessageChain([Comp.Plain(text)])
            )

        result = await self.auth_manager.relogin_with_password(
            interactive=True, on_confirmation_required=notify_confirmation
        )
        yield event.plain_result(result)
        if result.startswith("✅"):
            self._spawn(self._check_and_notify())

    @filter.command("steam_refresh", alias={"steam refresh", "steam续期"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_refresh(self, event: AstrMessageEvent):
        """手动请求复用已有 Refresh Token，不能绕过冷却或账号安全暂停。"""
        yield event.plain_result("⏳ 正在使用 Refresh Token 兑换 Access Token (renewal_type=1)...")
        if not self.config.refresh_token:
            yield event.plain_result(
                "⚠️ 尚无 Refresh Token。确认账号没有安全限制后，可由管理员私聊 /steam_qr 登录。"
            )
            return
        success = False
        try:
            success = await self.auth_manager.exchange_refresh_token(force_new=True)
        except SteamLoginError as e:
            self.auth_manager.last_keepalive_note = str(e)
        rt_days = self.auth_manager.refresh_token_days_left()
        at_hours = self.auth_manager.access_token_hours_left()
        if success:
            yield event.plain_result(
                "✅ 滚动续期完成！\n"
                f"🎫 Refresh Token 剩余寿命: {rt_days} 天（续签是否成功、是否延长由 Steam 决定）\n"
                f"⏰ Access Token 剩余: {at_hours} 小时"
            )
        else:
            yield event.plain_result(
                f"❌ 滚动续期失败: {self.auth_manager.last_keepalive_note}\n"
                "请先检查账号安全提示并等待冷却；不要在限流或账号受限期间反复登录。"
            )

    @filter.command("steam_code", alias={"steam code", "steam令牌"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_code(self, event: AstrMessageEvent):
        """🔑 实时计算当前 5 位 Steam 令牌验证码（需配置 shared_secret，等于手机令牌）。"""
        # 防御：令牌验证码属敏感凭据，群聊中拒绝展示
        if self._is_group_chat(event):
            yield event.plain_result("⚠️ 敏感凭据操作请勿在群聊执行，请私聊我或在 WebUI 配置。")
            return
        if not self.config.shared_secret:
            yield event.plain_result(
                "⚠️ 未配置 shared_secret，无法本地算码。\n"
                "可从 .maFile / ASF 配置 / 已 Root 安卓的 Steamguard 文件中提取，填入插件配置后即可使用。"
            )
            return
        try:
            code = generate_steam_guard_code(self.config.shared_secret)
            secs = 30 - int(time.time()) % 30
            yield event.plain_result(f"🔑 当前 Steam 令牌验证码: {code}\n⏱ {secs} 秒后自动刷新")
        except Exception as e:
            yield event.plain_result(
                f"❌ 计算验证码失败（shared_secret 格式可能不正确，应为 Base64）: {e}"
            )

    @filter.command("steam_cred", alias={"steam cred", "steam凭据"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_cred(
        self,
        event: AstrMessageEvent,
        account_name: str = "",
        password: str = "",
        shared_secret: str = "",
    ):
        """🔐 保存路线一保活凭据。例: /steam_cred 账号名 密码 cWFhYmJjY2RkZA==（shared_secret 可选，但不填将无法自动过锁）"""
        # 防御：账号/密码/shared_secret 属等值凭据，禁止在群聊中明文提交
        if self._is_group_chat(event):
            yield event.plain_result("⚠️ 敏感凭据操作请勿在群聊执行，请私聊我或在 WebUI 配置。")
            return
        if not account_name or not password:
            yield event.plain_result(
                "用法: /steam_cred <账号名> <密码> [shared_secret]\n"
                "💡 共享密钥建议从 .maFile / ASF / Root 手机提取（Base64 格式）。\n"
                "⚠️ 聊天记录会留下明文密码，强烈建议改在 WebUI 配置页填写！"
            )
            return
        self._save_credentials("account_name", account_name)
        self._save_credentials("password", password)
        if shared_secret:
            self._save_credentials("shared_secret", shared_secret)
        yield event.plain_result(
            "✅ 路线一保活凭据已保存（账号+密码"
            + ("+2FA秘钥" if shared_secret else "，未填秘钥")
            + "）。此操作只保存配置，不会立即创建新登录设备；需要验证时请私聊 /steam_relogin。"
        )

    @filter.command("steam_check", alias={"steam check", "steam同步", "家庭库检查"})
    async def cmd_check(self, event: AstrMessageEvent):
        """手动触发一次家庭库变动检查与同步。"""
        yield event.plain_result("⏳ 正在向 Steam 请求家庭库数据，请稍候...")
        result = await self._check_and_notify()
        yield event.plain_result(result)

    @filter.command("steam_sub", alias={"steam sub", "steam订阅"})
    async def cmd_sub(self, event: AstrMessageEvent):
        """订阅当前群/私聊的家庭库新游戏变动推送。"""
        target_id = getattr(event, "unified_msg_origin", str(event))
        await self.storage.subscribe(target_id)
        yield event.plain_result("🔔 已成功订阅当前会话的 Steam 家庭库变动推送通知！")

    @filter.command("steam_unsub", alias={"steam unsub", "steam退订"})
    async def cmd_unsub(self, event: AstrMessageEvent):
        """取消订阅当前群/私聊的变动推送。"""
        target_id = getattr(event, "unified_msg_origin", str(event))
        await self.storage.unsubscribe(target_id)
        yield event.plain_result("🔕 已取消当前会话的变动推送订阅。")

    @filter.command("steam_rank", alias={"steam rank", "义父榜", "steam排行"})
    async def cmd_rank(self, event: AstrMessageEvent):
        """查看家庭组义父资产贡献榜。"""
        stats_text = await self.agent_tools.get_family_stats()
        yield event.plain_result(stats_text)

    @filter.command("steam_roll", alias={"steam roll", "今天玩什么", "steam抽奖"})
    async def cmd_roll(self, event: AstrMessageEvent, tag: str = ""):
        """赛博轮盘：从家庭库中随机抽选一款游戏。例: /steam_roll RPG"""
        roll_text = await self.agent_tools.roll_game(tag=tag)
        yield event.plain_result(roll_text)

    @filter.command("steam_coop", alias={"steam coop", "开黑雷达", "steam联机"})
    async def cmd_coop(self, event: AstrMessageEvent, member_ids: str = ""):
        """开黑车队雷达：分析多人共同拥有的联机游戏。"""
        ids = [m.strip() for m in member_ids.split() if m.strip()]
        if len(ids) < 2:
            yield event.plain_result(
                "请提供至少两位成员的 SteamID，例如：/steam_coop 76561198... 76561198..."
            )
            return
        result = await self.agent_tools.find_coop_games(ids)
        yield event.plain_result(result)

    @filter.command("steam_ai", alias={"steam ai", "steam推荐", "steam找游戏"})
    async def cmd_ai(self, event: AstrMessageEvent, query: str = ""):
        """利用 AstrBot 原生 LLM 进行智能家庭库游戏推荐与专家问答。例: /steam_ai 推荐两款50元内治愈系解谜"""
        if not query.strip():
            yield event.plain_result(
                "💡 请提供你想询问的游戏需求，例如：/steam_ai 推荐两款评分高于90分的肉鸽游戏"
            )
            return

        umo = getattr(event, "unified_msg_origin", None)
        yield event.plain_result("🤔 Steam 家庭管家正在调取库内数据并思考中，请稍候...")
        try:
            all_cached = await self.storage.get_all_cached_apps()

            # 1. 生成全库游戏名清单（全库 1000+ 款游戏名一次性全量喂给大模型）
            # 这彻底杜绝大模型“误以为某游戏不在家庭库中”的幻觉！
            all_game_names = set()
            for a in all_cached:
                if a.chinese_name:
                    all_game_names.add(a.chinese_name)
                if a.name:
                    all_game_names.add(a.name)
            all_names_catalog = "、".join(sorted(all_game_names))

            # 2. 依托基模强大认知，提取直观匹配词，并补充高分代表游戏
            raw_tokens = [
                t.lower() for t in query.replace("，", " ").replace("。", " ").split() if len(t) > 1
            ]
            matched_apps = []
            other_apps = []
            for a in all_cached:
                text_to_search = f"{a.chinese_name or ''} {a.name} {' '.join(a.genres)} {' '.join(a.categories)} {a.review_summary}".lower()
                score = sum(1 for term in raw_tokens if term in text_to_search)
                if score > 0:
                    matched_apps.append((score, a))
                else:
                    other_apps.append(a)

            matched_apps.sort(key=lambda x: (x[0], x[1].positive_rate), reverse=True)
            top_candidates = [x[1] for x in matched_apps[:40]]
            other_apps.sort(key=lambda x: x.positive_rate, reverse=True)
            candidates = top_candidates + other_apps[: max(0, 50 - len(top_candidates))]

            detailed_summaries = [
                f"• 《{a.chinese_name or a.name}》(原名:{a.name}, 现价:¥{a.final_price:.1f}, 好评率:{a.positive_rate}%, 评价:{a.review_summary or '暂无'}, 标签:{','.join(a.genres[:4])})"
                for a in candidates
            ]
            detailed_context = (
                "\n".join(detailed_summaries) if detailed_summaries else "暂无游戏缓存数据"
            )

            system_prompt = (
                "你是一个幽默风趣、见多识广的 Steam 资深游戏专家与家庭库管家。\n"
                "【极高优先级原则·必须严格执行】：\n"
                "1. 下方的【家庭库完整拥有游戏全名单】和【重点游戏详情】中包含的所有游戏，都是当前家庭库**已经购买并拥有的真实游戏**！\n"
                "2. 只要一个游戏（无论中文名或英文名）出现在【家庭库完整拥有游戏全名单】或【重点游戏详情】中（例如《Warhammer: Vermintide 2》《HELLDIVERS™ 2》等），它就**绝对是家庭库已有游戏**！严禁将其归为'库外'或'未入库'！\n"
                "3. 推荐时请大力夸赞家庭库里的神作储备，优先推荐库内已有的神作！\n\n"
                f"【家庭库完整拥有游戏全名单（总计 {len(all_game_names)} 款，以下全部已拥有）】：\n"
                f"{all_names_catalog}\n\n"
                f"【根据需求重点筛选的游戏详情参考】：\n"
                f"{detailed_context}"
            )

            if (
                umo
                and hasattr(self.context, "get_current_chat_provider_id")
                and hasattr(self.context, "llm_generate")
            ):
                prov_id = await self.context.get_current_chat_provider_id(umo)
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=prov_id,
                    prompt=query,
                    system_prompt=system_prompt,
                )
                reply_text = getattr(llm_resp, "completion_text", str(llm_resp))
                yield event.plain_result(reply_text)
            else:
                reply_text = await self.agent_tools.query_family_games(keyword=query)
                yield event.plain_result(reply_text)
        except Exception as ex:
            logger.error(f"Error in steam_ai command: {ex}")
            yield event.plain_result(f"❌ AI 推荐思考出现异常: {ex}")

    @filter.command(
        "steam_dashboard",
        alias={"steam dashboard", "steam大盘", "steam看板"},
    )
    async def cmd_dashboard(self, event: AstrMessageEvent):
        """获取 Steam 家庭资产全景 Web 看板访问指引。"""
        yield event.plain_result(
            "📊 Steam 家庭资产全景看板已原生集成至 AstrBot！\n"
            "👉 请在 AstrBot Web 管理面板「插件管理」中找到本插件卡片，点击「🖥️ 打开插件UI界面」图标即可一键开启内置全景大盘！"
        )

    @filter.command("steam_help", alias={"steam help", "steam", "steam帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看 Steam 家庭管家所有支持的指令列表。"""
        help_msg = (
            "🎮 【Steam 家庭资产管家指令列表】\n"
            "🔄 登录与会话复用（管理员操作）:\n"
            "• /steam_qr - 📱 私聊扫码登录（核对设备与位置，不保证固定令牌寿命）\n"
            "• /steam_relogin - 🔐 私聊密码登录，Steam 需要人工确认时请处理提示\n"
            "• /steam_auth_resume - 🛑 处理官方账号限制后恢复本地认证暂停（不发起登录）\n"
            "• /steam_refresh - 🛢️ 手动滚动续期一次 Refresh Token\n"
            "• /steam_code - 🔑 显示当前 5 位 Steam 令牌动态验证码\n"
            "• /steam_cred <账号> <密码> [秘钥] - 保存全自动重登凭据\n"
            "📊 家庭库:\n"
            "• /steam_check 或 /steam check - 立即同步检查家庭库变动\n"
            "• /steam_sub 或 /steam sub - 开启当前群的新游戏入库卡片推送\n"
            "• /steam_unsub - 取消当前群变动推送\n"
            "• /steam_rank 或 /steam rank - 查看家庭组义父资产贡献榜\n"
            "• /steam_roll [标签] - 赛博轮盘随机抽取游戏\n"
            "• /steam_coop <SteamID...> - 开黑车队雷达 (计算共有游戏)\n"
            "• /steam_ai <需求> - 专属 LLM 游戏专家智能推荐\n"
            "• /steam_dashboard - 查看 Web 看板开启指引\n"
            "• /steam_bind <Token> <SteamID> - 重新绑定鉴权凭据"
        )
        yield event.plain_result(help_msg)

    # --- Agent LLM Tools ---
    @filter.llm_tool(name="query_family_games")
    async def tool_query_family_games(
        self, event: AstrMessageEvent, keyword: str = "", tag: str = ""
    ):
        """检索 Steam 家庭库中的游戏列表与归属者。 Args: keyword(string): 游戏名关键词, tag(string): 类型标签"""
        return await self.agent_tools.query_family_games(keyword=keyword, tag=tag)

    @filter.llm_tool(name="get_family_stats")
    async def tool_get_family_stats(self, event: AstrMessageEvent):
        """获取 Steam 家庭库资产全貌、游戏总数与成员贡献排行。"""
        return await self.agent_tools.get_family_stats()

    @filter.llm_tool(name="roll_family_game")
    async def tool_roll_game(self, event: AstrMessageEvent, tag: str = ""):
        """从 Steam 家庭库中随机抽选一款推荐游戏。 Args: tag(string): 筛选标签"""
        return await self.agent_tools.roll_game(tag=tag)

    async def terminate(self):
        # 统一回收全部后台任务：取消经 _spawn 登记的所有任务（轮询、补播 drain、
        # 巡检、命令补 check 等），并 await 真正退出，杜绝插件卸载/重载后的任务泄漏。
        tasks = list(self._bg_tasks)
        if self._polling_task is not None and self._polling_task not in tasks:
            tasks.append(self._polling_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._bg_tasks.clear()
        self._polling_task = None
        try:
            await self.auth.close()
        except Exception:
            pass
        try:
            await self.client.close()
        except Exception:
            pass
        logger.info("Steam Family Plugin terminated.")
