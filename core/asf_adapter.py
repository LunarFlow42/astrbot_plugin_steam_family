"""Opt-in ASF credential source; never starts a Steam login or exports refresh tokens.

The companion ASF plugin exposes one authenticated, read-only endpoint. Credentials
stay in memory here; only IPC cooldown/pause metadata is persisted. A failure must
not fall back to native authentication or use a stale token from plugin config.
"""

import asyncio
import base64
import hashlib
import ipaddress
import json
import math
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlsplit

import httpx

from .steam_api import decode_steam_jwt

ASF_STATE_KEY = "asf_adapter_state"
_IPC_CACHE_SECONDS = 60
_FAILURE_COOLDOWN_SECONDS = 60
_IPC_AUTH_FAILURE_COOLDOWN_SECONDS = 3600
_STEAM_RATE_LIMIT_COOLDOWN_SECONDS = 25 * 60
_MIN_TOKEN_TTL_SECONDS = 60
_ACCOUNT_LOCKDOWN_FLAG = 1 << 23
_SECURITY_RESULTS = {17, 43, 72, 73, 82, 83, 105}


def asf_state_key(url: str, bot_name: str) -> str:
    """Separate state per ASF source so replacing a provider cannot overwrite it."""
    source_id = hashlib.sha256(json.dumps([url.rstrip("/"), bot_name]).encode()).hexdigest()
    return f"{ASF_STATE_KEY}:{source_id}"


class AsfAdapterError(Exception):
    """A deliberately sanitized error; never include response bodies or secrets."""

    def __init__(self, message: str, *, code: str = "ipc_error", retry_after: float = 0):
        super().__init__(message)
        self.code = code
        self.retry_after = max(0, retry_after) if math.isfinite(retry_after) else 0


def _validate_bot_name(name: str) -> str:
    if (
        not name
        or len(name) > 128
        or ".." in name
        or name == "."
        or any(not (c.isalnum() or c in "_.-") for c in name)
    ):
        raise AsfAdapterError(
            "请填写一个确切的 ASF Bot 名称，不支持通配符、范围或多个 Bot", code="configuration"
        )
    return name


def _validate_ipc_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (ValueError, TypeError) as exc:
        raise AsfAdapterError("ASF IPC 地址格式无效", code="configuration") from exc
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 0 < port < 65536)
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
        or "\\" in value
        or "%" in parsed.netloc + parsed.path
        or any(segment in (".", "..") for segment in parsed.path.split("/"))
    ):
        raise AsfAdapterError(
            "ASF IPC 地址必须是无账号、查询参数和片段的 HTTP(S) 服务地址", code="configuration"
        )
    loopback = parsed.hostname.lower() == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme == "http" and not loopback:
        raise AsfAdapterError(
            "非回环 ASF IPC 必须使用验证证书的 HTTPS；本地可使用 SSH 隧道", code="configuration"
        )
    return value.rstrip("/")


def _retry_after(response: httpx.Response) -> float:
    raw = response.headers.get("retry-after", "0")
    try:
        seconds = float(raw)
    except (ValueError, TypeError):
        try:
            seconds = parsedate_to_datetime(raw).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            seconds = 0
    return max(0, seconds) if math.isfinite(seconds) else 0


@dataclass(frozen=True)
class AsfCredential:
    steam_id: str
    access_token: str = field(repr=False)
    expires_at: int


class AsfIpcClient:
    """Read a single allowlisted bot's already-issued short-lived token over IPC."""

    def __init__(
        self, url: str, bot_name: str, ipc_password: str, bridge_key: str, timeout: int = 10
    ):
        self.url = _validate_ipc_url(url)
        self.bot_name = _validate_bot_name(bot_name)
        if not ipc_password or not 32 <= len(bridge_key) <= 256:
            raise AsfAdapterError(
                "ASF 模式需要 IPCPassword 和至少 32 字符的独立桥接密钥", code="configuration"
            )
        if any(not 33 <= ord(c) <= 126 for c in ipc_password + bridge_key):
            raise AsfAdapterError(
                "ASF 认证密钥必须是无空格的可打印 ASCII 字符", code="configuration"
            )
        self._client = httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "Authentication": ipc_password,
                "X-Steam-Family-Key": bridge_key,
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_credential(self, expected_steam_id: str = "") -> AsfCredential:
        endpoint = f"{self.url}/Api/SteamFamily/{quote(self.bot_name, safe='')}/AccessToken"
        try:
            response = await self._client.get(endpoint)
        except httpx.RequestError as exc:
            raise AsfAdapterError("无法连接 ASF IPC，请检查服务或证书；未回退到原生登录") from exc
        if response.status_code in (401, 403):
            raise AsfAdapterError(
                "ASF IPC/桥接密钥被拒绝，已停止重复请求；请检查配置和 ASF IPC 封禁状态",
                code="ipc_auth",
                retry_after=max(_IPC_AUTH_FAILURE_COOLDOWN_SECONDS, _retry_after(response)),
            )
        if response.status_code == 429:
            raise AsfAdapterError(
                "ASF IPC 请求被限流，请等待冷却",
                code="rate_limit",
                retry_after=_retry_after(response),
            )
        if 300 <= response.status_code < 400:
            raise AsfAdapterError("ASF IPC 返回重定向，已拒绝转发密钥", code="configuration")
        if response.status_code == 404:
            raise AsfAdapterError(
                "ASF 桥接未安装/未启用，或 Bot 未列入允许名单", code="configuration"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AsfAdapterError("ASF IPC 返回了无效 JSON，未使用返回内容") from exc
        if not isinstance(payload, dict):
            raise AsfAdapterError("ASF IPC 响应结构无效")
        error_code = payload.get("ErrorCode")
        if error_code == "account_locked":
            raise AsfAdapterError(
                "ASF 报告账号安全锁定，已暂停获取凭据和同步", code="account_locked"
            )
        if error_code == "bot_offline":
            raise AsfAdapterError(
                "ASF Bot 尚未登录，请在 ASF 中处理；插件不会启动或重登 Bot", code="bot_offline"
            )
        if error_code == "token_unavailable":
            raise AsfAdapterError(
                "ASF 尚无可用 Access Token，请等待 ASF 自身完成续签", code="token_unavailable"
            )
        if response.status_code != 200 or payload.get("Success") is not True:
            raise AsfAdapterError("ASF 桥接请求未成功；未读取凭据或执行登录")
        result = payload.get("Result")
        if (
            not isinstance(result, dict)
            or type(result.get("ProtocolVersion")) is not int
            or result["ProtocolVersion"] != 1
        ):
            raise AsfAdapterError("ASF 桥接协议不兼容", code="configuration")
        if result.get("BotName") != self.bot_name:
            raise AsfAdapterError("ASF 返回的 Bot 名称不匹配，已拒绝凭据", code="identity")
        flags = result.get("AccountFlags")
        if not isinstance(flags, int) or isinstance(flags, bool) or not 0 <= flags <= 0xFFFFFFFF:
            raise AsfAdapterError("ASF 未返回可靠的账号状态，已拒绝凭据")
        if result.get("IsAccountLocked") is True or flags & _ACCOUNT_LOCKDOWN_FLAG:
            raise AsfAdapterError(
                "ASF 报告账号安全锁定，已暂停获取凭据和同步", code="account_locked"
            )
        if result.get("IsAccountLocked") is not False:
            raise AsfAdapterError("ASF 账号锁定状态未知，已拒绝凭据")
        if result.get("IsConnectedAndLoggedOn") is not True:
            raise AsfAdapterError("ASF Bot 尚未登录，请在 ASF 中处理", code="bot_offline")
        steam_id = result.get("SteamID")
        if (
            not isinstance(steam_id, str)
            or len(steam_id) != 17
            or not steam_id.isascii()
            or not steam_id.isdigit()
        ):
            raise AsfAdapterError("ASF 返回了无效 SteamID，已拒绝凭据", code="identity")
        if expected_steam_id and steam_id != expected_steam_id:
            raise AsfAdapterError(
                "ASF Bot 与插件配置的 SteamID 不匹配，未覆盖原凭据", code="identity"
            )
        token = result.get("AccessToken")
        info = decode_steam_jwt(token) if isinstance(token, str) else None
        if not info or info.get("steamid") != steam_id:
            raise AsfAdapterError("ASF Access Token 与 Bot 账号不匹配，已拒绝凭据", code="identity")
        if info.get("remaining_seconds", 0) <= _MIN_TOKEN_TTL_SECONDS:
            raise AsfAdapterError(
                "ASF Access Token 已到期或即将到期，请等待 ASF 续签", code="token_unavailable"
            )
        # Platform hints are unverified; reject refresh tokens rather than handling
        # them. ASF owns that long-lived authority and must not share it here.
        try:
            if len(token.split(".")) != 3:
                raise ValueError("JWT must have three segments")
            segment = token.split(".")[1]
            token_data = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
            audience = token_data.get("aud", [])
            if (
                not isinstance(audience, list)
                or any(not isinstance(a, str) for a in audience)
                or "web" not in audience
            ):
                raise ValueError("access token must have web audience")
            if any(a in audience for a in ("renew", "derive", "machine")):
                raise AsfAdapterError(
                    "ASF 桥接返回了非短期 Access Token，已拒绝读取", code="identity"
                )
        except (ValueError, TypeError, IndexError) as exc:
            raise AsfAdapterError("ASF Access Token 结构无效，已拒绝读取") from exc
        if type(result.get("ExpiresAt")) is not int or result["ExpiresAt"] != info["exp"]:
            raise AsfAdapterError("ASF 桥接有效期与令牌不符，已拒绝读取")
        return AsfCredential(steam_id, token, info["exp"])


class AsfSessionProvider:
    """Independent, opt-in source with short caching and persistent failure guards."""

    def __init__(
        self,
        url: str,
        bot_name: str,
        ipc_password: str,
        bridge_key: str,
        expected_steam_id: str = "",
        timeout: int = 10,
        load_state: Callable[[], Awaitable[str | None]] | None = None,
        save_state: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.bot_name = bot_name
        self._settings_id = hashlib.sha256(
            json.dumps([url, bot_name, ipc_password, bridge_key, expected_steam_id]).encode()
        ).hexdigest()
        self._source_id = asf_state_key(url, bot_name).split(":", 1)[1]
        self._expected_steam_id = expected_steam_id
        self._bound_steam_id = expected_steam_id
        self._client: AsfIpcClient | None = None
        self._config_error = ""
        try:
            self._client = AsfIpcClient(url, bot_name, ipc_password, bridge_key, timeout)
        except AsfAdapterError as exc:
            self._config_error = str(exc)
        self._credential: AsfCredential | None = None
        self._lock = asyncio.Lock()
        self._load_state = load_state
        self._save_state = save_state
        self._state_loaded = load_state is None
        self._cooldown_until = 0.0
        self._security_blocked = False
        self._paused = False
        self._rejected_token_hash = ""
        self._last_fetched_at = 0.0
        self._pinned_account_mismatch = False
        self._fetch_generation = 0
        self._closed = False
        self.last_note = self._config_error or "ASF 模式已配置，尚未获取凭据"

    @property
    def access_token(self) -> str:
        return self._credential.access_token if self._credential else ""

    @property
    def steam_id(self) -> str:
        return self._credential.steam_id if self._credential else self._bound_steam_id

    @property
    def security_blocked(self) -> bool:
        return self._security_blocked

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @property
    def cooldown_remaining(self) -> int:
        return max(0, math.ceil(self._cooldown_until - time.time()))

    def describe_mode(self) -> str:
        if self._config_error:
            return "⚠️ ASF 托管配置尚未完成"
        if self._security_blocked:
            return "🛑 ASF 报告账号安全限制，同步已暂停"
        if self._paused:
            return "⏸️ ASF IPC 配置/身份验证失败，已暂停"
        return "🧩 ASF 授权托管（插件不发起 Steam 登录）"

    def public_status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "bot_name": self.bot_name,
            "security_blocked": self._security_blocked,
            "paused": self._paused or bool(self._config_error),
            "cooldown_remaining": self.cooldown_remaining,
            "last_fetched_at": self._last_fetched_at,
            "message": self.last_note,
        }

    async def get_status(self) -> dict[str, Any]:
        """Read persistent local status without asking ASF for a credential."""
        async with self._lock:
            await self._ensure_state_loaded()
            return self.public_status()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            self._credential = None
            if self._client:
                await self._client.close()

    async def _ensure_state_loaded(self) -> None:
        if self._state_loaded:
            return
        raw = await self._load_state() if self._load_state else ""
        try:
            state = json.loads(raw) if raw else {}
            if not isinstance(state, dict):
                raise ValueError("state must be a dict")
            if state.get("source_id") == self._source_id:
                until = float(state.get("cooldown_until", 0))
                if not math.isfinite(until):
                    raise ValueError("bad cooldown")
                self._cooldown_until = until
                for key in ("security_blocked", "paused"):
                    if key in state and type(state[key]) is not bool:
                        raise ValueError("bad pause flag")
                self._security_blocked = state.get("security_blocked") is True
                self._paused = (
                    state.get("paused") is True and state.get("settings_id") == self._settings_id
                )
                rejected_hash = state.get("rejected_token_hash", "")
                if not isinstance(rejected_hash, str) or (
                    rejected_hash
                    and (
                        len(rejected_hash) != 64
                        or any(c not in "0123456789abcdef" for c in rejected_hash)
                    )
                ):
                    raise ValueError("bad rejected-token hash")
                self._rejected_token_hash = rejected_hash
                bound = state.get("bound_steam_id", self._bound_steam_id)
                if not isinstance(bound, str) or (
                    bound and (len(bound) != 17 or not bound.isascii() or not bound.isdigit())
                ):
                    raise ValueError("bad pinned account")
                self._bound_steam_id = bound
                if self._expected_steam_id and bound and self._expected_steam_id != bound:
                    self._paused = True
                    self._pinned_account_mismatch = True
                    self.last_note = "ASF 固定账号与新的 SteamID 配置不匹配，请检查配置"
                elif self._security_blocked or self._paused:
                    self.last_note = "ASF 适配层已暂停，请检查账号/配置后由管理员恢复"
        except (ValueError, TypeError):
            self._paused = True
            self.last_note = "ASF 安全状态损坏，已暂停，请检查后手动恢复"
        self._state_loaded = True

    async def _persist_state(self) -> None:
        if self._save_state:
            try:
                await self._save_state(
                    json.dumps(
                        {
                            "source_id": self._source_id,
                            "settings_id": self._settings_id,
                            "cooldown_until": self._cooldown_until,
                            "security_blocked": self._security_blocked,
                            "paused": self._paused,
                            "rejected_token_hash": self._rejected_token_hash,
                            "bound_steam_id": self._bound_steam_id,
                        }
                    )
                )
            except Exception as exc:
                self._credential = None
                self._paused = True
                self.last_note = "无法保存 ASF 安全状态，已暂停同步，请检查存储后恢复"
                raise AsfAdapterError(self.last_note, code="storage") from exc

    async def ensure_valid_access_token(self, force: bool = False) -> bool:
        requested_generation = self._fetch_generation
        async with self._lock:
            await self._ensure_state_loaded()
            if self._closed:
                self.last_note = "ASF 适配层已关闭，未发起 IPC 请求"
                return False
            if self._config_error:
                self.last_note = self._config_error
                return False
            if self._paused or self._security_blocked:
                return False
            if self.cooldown_remaining:
                self.last_note = (
                    f"ASF 适配层冷却剩 {self.cooldown_remaining} 秒，未触发任何原生登录"
                )
                return False
            now = time.time()
            if (
                (not force or self._fetch_generation != requested_generation)
                and self._credential
                and self._credential.expires_at - now > _MIN_TOKEN_TTL_SECONDS
                and now - self._last_fetched_at < _IPC_CACHE_SECONDS
            ):
                return True
            # 先撤下旧令牌，任何请求/验证意外都不能继续暴露先前缓存。
            self._credential = None
            try:
                credential = await self._client.fetch_credential(self._bound_steam_id)
                digest = hashlib.sha256(credential.access_token.encode()).hexdigest()
                if digest == self._rejected_token_hash:
                    raise AsfAdapterError(
                        "ASF 仍返回被 Steam 拒绝的旧令牌，请等待 ASF 自身续签",
                        code="token_unavailable",
                    )
            except AsfAdapterError as exc:
                self._credential = None
                self.last_note = str(exc)
                self._security_blocked = exc.code == "account_locked"
                self._paused = exc.code in {"ipc_auth", "configuration", "identity"}
                self._cooldown_until = time.time() + max(_FAILURE_COOLDOWN_SECONDS, exc.retry_after)
                await self._persist_state()
                return False
            self._credential = credential
            self._bound_steam_id = credential.steam_id
            self._last_fetched_at = now
            self._fetch_generation += 1
            self._cooldown_until = 0
            self.last_note = "已读取 ASF 当前 Access Token；未启动 Bot、未请求登录或强制续期"
            await self._persist_state()
            return True

    async def record_api_auth_failure(self, eresult: int = 0) -> None:
        async with self._lock:
            await self._ensure_state_loaded()
            if self.access_token and eresult not in _SECURITY_RESULTS | {84, 87}:
                self._rejected_token_hash = hashlib.sha256(self.access_token.encode()).hexdigest()
            self._credential = None
            if eresult in _SECURITY_RESULTS:
                self._security_blocked = True
                self.last_note = "Steam 返回明确安全限制，ASF 适配层已暂停，请先处理官方账号提示"
            elif eresult in (84, 87):
                self._cooldown_until = time.time() + _STEAM_RATE_LIMIT_COOLDOWN_SECONDS
                self.last_note = "Steam 返回限流，ASF 模式暂停读取凭据，不回退到原生登录"
            else:
                self.last_note = "Steam 拒绝 ASF 当前令牌，等待 ASF 换发；不会触发原生登录"
            await self._persist_state()

    async def resume_after_review(self) -> str:
        async with self._lock:
            await self._ensure_state_loaded()
            if self.cooldown_remaining:
                return f"⏳ ASF 冷却仍有 {self.cooldown_remaining} 秒，不能提前恢复"
            if self._config_error:
                return f"⚠️ {self._config_error}"
            if self._pinned_account_mismatch:
                return (
                    "⚠️ ASF 固定账号与 SteamID 配置不匹配，请先修正配置；恢复暂停不会自动改绑账号。"
                )
            self._paused = False
            self._security_blocked = False
            self.last_note = "ASF 本地暂停已解除；未发起 IPC 请求、Steam 登录或续期"
            await self._persist_state()
            return "✅ ASF 适配层本地暂停已解除；不会解除 Steam 官方限制，也未发起登录。请先确认 ASF Bot 已正常登录。"
