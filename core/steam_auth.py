"""Steam authentication WebAPI client.

Use the published authentication protobuf schema, keep a consistent device profile,
and reuse server-issued guard/refresh tokens. ASF uses SteamKit2's authenticated CM
transport; this HTTP implementation does NOT emulate that transport and cannot
guarantee exemption from Steam account security checks.
"""

import asyncio
import base64
import hashlib
import hmac
import io
import logging
import math
import platform
import secrets
import socket
import struct
import time
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx
import qrcode

from .steam_auth_proto import decode_response, encode_request

try:
    from .steam_api import decode_steam_jwt
except (ImportError, ValueError):
    from core.steam_api import decode_steam_jwt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 协议枚举常量 (与 SteamKit / node-steam-session 对齐)
# ---------------------------------------------------------------------------
# EAuthTokenPlatformType
PLATFORM_TYPE_STEAM_CLIENT = 1
PLATFORM_TYPE_WEB_BROWSER = 2
PLATFORM_TYPE_MOBILE_APP = 3

# ERefreshTokenPlatformType (access token aud audience)
AUDIENCE_MOBILE = "mobile"
AUDIENCE_WEB = "web"

# EAuthSessionGuardType
GUARD_TYPE_UNKNOWN = 0
GUARD_TYPE_NONE = 1
GUARD_TYPE_EMAIL_CODE = 2
GUARD_TYPE_DEVICE_CODE = 3
GUARD_TYPE_DEVICE_CONFIRMATION = 4
GUARD_TYPE_EMAIL_CONFIRMATION = 5
GUARD_TYPE_MACHINE_TOKEN = 6
GUARD_TYPE_LEGACY_MACHINE_AUTH = 7

GUARD_TYPE_NAMES = {
    GUARD_TYPE_NONE: "无需额外验证",
    GUARD_TYPE_EMAIL_CODE: "邮件验证码",
    GUARD_TYPE_DEVICE_CODE: "手机令牌验证码",
    GUARD_TYPE_MACHINE_TOKEN: "机器令牌",
    GUARD_TYPE_DEVICE_CONFIRMATION: "手机确认弹窗",
    GUARD_TYPE_EMAIL_CONFIRMATION: "邮件确认",
    GUARD_TYPE_LEGACY_MACHINE_AUTH: "旧版机器授权",
    GUARD_TYPE_UNKNOWN: "未知",
}

# ETokenRenewalType
RENEWAL_TYPE_NONE = 0
RENEWAL_TYPE_ALLOW = 1

# ESessionPersistence
PERSISTENCE_PERSISTENT = 1
PERSISTENCE_EPHEMERAL = 0

# EResult (常用子集)
EResult_OK = 1
ERESULT_MESSAGES = {
    2: "常规失败 (Fail)",
    3: "无法连接到 Steam 网络 (NoConnection)",
    5: "账号或密码错误 (InvalidPassword)",
    8: "请求参数无效 (InvalidParam)",
    15: "拒绝访问 (AccessDenied)",
    16: "请求超时 (Timeout)",
    17: "账号被封禁 (Banned)",
    20: "Steam 服务不可用 (ServiceUnavailable)",
    26: "授权已撤销 (Revoked)",
    27: "授权已过期 (Expired)",
    43: "账号被停用 (AccountDisabled)",
    63: "需要 Steam Guard 验证 (AccountLogonDenied)",
    65: "登录验证码错误 (InvalidLoginAuthCode)",
    72: "IP 登录受限 (IPLoginRestrictionFailed)",
    73: "账号已被安全锁定 (AccountLockedDown)",
    82: "设备受限 (RestrictedDevice)",
    83: "地区受限 (RegionLocked)",
    84: "请求过于频繁被限流 (RateLimitExceeded)",
    85: "需要手机令牌验证 (AccountLoginDeniedNeedTwoFactor)",
    87: "登录被节流 (AccountLoginDeniedThrottle)",
    88: "Steam 令牌验证码错误 (TwoFactorCodeMismatch)",
    101: "需要人工完成验证码 (NeedCaptcha)",
    105: "IP 被封禁 (IPBanned)",
}

# Steam Guard 官方字符表
STEAM_GUARD_ALPHABET = "23456789BCDFGHJKMNPQRTVWXY"

_API_BASE = "https://api.steampowered.com/IAuthenticationService"

# TOTP 对时接口（免费、无需任何参数）
_QUERY_TIME_URL = "https://api.steampowered.com/ITwoFactorService/QueryTime/v1/"

# 旧移动端授权仅用于兼容已有 mobile audience 的 Refresh Token。
MOBILE_APP_UA = "okhttp/4.9.2"
MOBILE_APP_COOKIES = "mobileClient=android; mobileClientVersion=777777 3.10.3"
_BROWSER_PLATFORM = {
    "Windows": "Windows NT 10.0; Win64; x64",
    "Darwin": "Macintosh; Intel Mac OS X 10_15_7",
}.get(platform.system(), "X11; Linux x86_64")
BROWSER_UA = (
    f"Mozilla/5.0 ({_BROWSER_PLATFORM}) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class SteamLoginError(Exception):
    """Steam 登录/续期流程失败。"""

    def __init__(
        self,
        message: str,
        eresult: int = 0,
        *,
        retry_after: float = 0.0,
        requires_confirmation: bool = False,
    ):
        if eresult and eresult != EResult_OK:
            reason = ERESULT_MESSAGES.get(eresult)
            if reason:
                message = f"{message} (EResult {eresult}: {reason})"
            else:
                message = f"{message} (EResult {eresult})"
        super().__init__(message)
        self.eresult = eresult
        self.retry_after = max(0.0, retry_after) if math.isfinite(retry_after) else 0.0
        self.requires_confirmation = requires_confirmation


def generate_steam_guard_code_for_time(shared_secret: str, unix_timestamp: float) -> str:
    """
    利用 shared_secret 在本地计算指定时刻的 5 位 Steam 2FA 验证码。
    与手机 Steam App / SDA / ASF 完全同算法：HMAC-SHA1 + 30 秒时间步长 + Steam 字符表取模。
    """
    try:
        secret_bytes = base64.b64decode(shared_secret, validate=True)
    except (ValueError, TypeError) as exc:
        raise SteamLoginError("shared_secret 必须是有效的 Base64 密钥") from exc
    if not secret_bytes:
        raise SteamLoginError("shared_secret 不能为空")
    time_step = int(float(unix_timestamp) // 30)
    msg = struct.pack(">Q", time_step)
    hmac_hash = hmac.new(secret_bytes, msg, hashlib.sha1).digest()
    offset = hmac_hash[19] & 0x0F
    code_int = struct.unpack(">I", hmac_hash[offset : offset + 4])[0] & 0x7FFFFFFF
    code_chars = []
    for _ in range(5):
        code_chars.append(STEAM_GUARD_ALPHABET[code_int % len(STEAM_GUARD_ALPHABET)])
        code_int //= len(STEAM_GUARD_ALPHABET)
    return "".join(code_chars)


def generate_steam_guard_code(shared_secret: str, offset_seconds: int = 0) -> str:
    """生成当前时间的 Steam 令牌 5 位验证码；offset_seconds 可偏移时间窗（如 -30 表示上一窗）。"""
    return generate_steam_guard_code_for_time(shared_secret, time.time() + float(offset_seconds))


def encrypt_password_rsa(password: str, publickey_mod: str, publickey_exp: str) -> str:
    """
    使用 Steam 下发的 RSA 公钥对密码做 PKCS#1 v1.5 (type 2) 加密，返回 Base64 密文。
    纯 Python 实现（与 node-bignumber / ValvePython 生态等标准做法一致），无需引入任何 RSA 第三方库。
    """
    modulus = int(publickey_mod, 16)
    exponent = int(publickey_exp, 16)
    key_len = (modulus.bit_length() + 7) // 8
    plaintext = password.encode("utf-8")
    if len(plaintext) > key_len - 11:
        raise SteamLoginError("密码过长，无法使用当前 RSA 公钥加密")

    # PKCS#1 v1.5 type 2: 0x00 0x02 || random non-zero padding || 0x00 || message
    pad_len = key_len - len(plaintext) - 3
    padding = bytearray()
    while len(padding) < pad_len:
        byte = secrets.token_bytes(1)
        if byte != b"\x00":
            padding += byte
    padded = b"\x00\x02" + bytes(padding) + b"\x00" + plaintext

    cipher_int = pow(int.from_bytes(padded, "big"), exponent, modulus)
    return base64.b64encode(cipher_int.to_bytes(key_len, "big")).decode("utf-8")


def steamid_from_refresh_token(refresh_token: str) -> str:
    """从 Refresh Token 的 JWT 声明中解析出 64 位 SteamID (sub claim)。"""
    info = decode_steam_jwt(refresh_token or "")
    if info and info.get("steamid"):
        return str(info["steamid"])
    return ""


def _token_audiences(token: str) -> set[str]:
    """Unverified JWT hints, used for transport selection only (never authorization)."""
    import json

    try:
        segment = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        audiences = payload.get("aud", [])
        return {audiences} if isinstance(audiences, str) else set(audiences)
    except (ValueError, TypeError, IndexError, AttributeError):
        return set()


def _host_os_type() -> int:
    # SteamKit EOSType: use actual host family, not a fabricated Windows device.
    return {"Linux": -203, "Darwin": -102, "Windows": 0}.get(platform.system(), -1)


def _retry_after_seconds(resp: httpx.Response) -> float:
    raw = resp.headers.get("retry-after", "0")
    try:
        value = float(raw)
    except (ValueError, TypeError):
        try:
            value = parsedate_to_datetime(raw).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            value = 0.0
    return max(0.0, value) if math.isfinite(value) else 0.0


def _raise_web_auth_http_error(response: httpx.Response, action: str) -> None:
    """网页认证也保留 x-eresult / Retry-After，不能把账号锁定折叠成网络错误。"""
    raw_result = response.headers.get("x-eresult")
    if raw_result is not None:
        try:
            result = int(raw_result)
        except ValueError as exc:
            raise SteamLoginError(f"{action}: 无效 x-eresult") from exc
        if result != EResult_OK:
            raise SteamLoginError(
                action, eresult=result, retry_after=_retry_after_seconds(response)
            )
    if response.status_code == 429:
        raise SteamLoginError(action, eresult=84, retry_after=_retry_after_seconds(response))
    if response.status_code in (401, 403):
        raise SteamLoginError(action, eresult=15)
    if response.status_code != 200:
        raise SteamLoginError(f"{action} (HTTP {response.status_code})")


def _validate_challenge_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "s.team"
        or not parsed.path.startswith("/q/")
        or parsed.username
        or parsed.password
        or parsed.netloc.lower() not in ("s.team", "s.team:443")
    ):
        raise SteamLoginError("Steam 返回了非官方扫码链接，已拒绝展示")


class SteamAuthClient:
    """Web authentication client; default to a browser profile for HTTP transport."""

    def __init__(
        self,
        proxy: str = "",
        timeout: int = 20,
        platform_type: int = PLATFORM_TYPE_WEB_BROWSER,
        device_name: str = "",
    ):
        if platform_type == PLATFORM_TYPE_STEAM_CLIENT:
            raise ValueError("SteamClient 模式需要已认证的 CM 连接，不能用 HTTP WebAPI 模拟 ASF")
        if platform_type not in (PLATFORM_TYPE_WEB_BROWSER, PLATFORM_TYPE_MOBILE_APP):
            raise ValueError("Unsupported Steam authentication platform")
        self.platform_type = platform_type
        self.device_name = device_name or f"{socket.gethostname()} (AstrBot Steam Family)"
        # 本地与 Steam 服务器的时间偏移（秒）；None=尚未对时，登录时对一次并按实例缓存复用
        self._server_time_offset: int | None = None
        headers = {"Accept": "application/json, application/octet-stream", "User-Agent": BROWSER_UA}
        if platform_type == PLATFORM_TYPE_MOBILE_APP:
            headers["User-Agent"] = MOBILE_APP_UA
            headers["Cookie"] = MOBILE_APP_COOKIES
        else:
            headers.update(
                {
                    "Origin": "https://steamcommunity.com",
                    "Referer": "https://steamcommunity.com/",
                }
            )
        self._client = httpx.AsyncClient(
            proxy=proxy or None,
            timeout=timeout,
            headers=headers,
            trust_env=False,
            follow_redirects=False,
        )

    @property
    def device_details(self) -> dict[str, Any]:
        details = {
            "device_friendly_name": self.device_name,
            "platform_type": self.platform_type,
            "os_type": _host_os_type(),
        }
        return details

    @property
    def website_id(self) -> str:
        return "Mobile" if self.platform_type == PLATFORM_TYPE_MOBILE_APP else "Community"

    async def close(self):
        await self._client.aclose()

    # ------------------------------------------------------------------
    # HTTP 基础封装
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_response(resp: httpx.Response, action: str, method: str = "") -> dict[str, Any]:
        """
        解析统一 Web API 响应（fail-closed）。

        任何无法确认为 EResult.OK 的形态一律抛出 SteamLoginError，绝不静默降级为
        "成功空结果"（旧版在 HTTP 429/5xx、HTML 错误页、x-eresult 不可解析时会
        fail-open 成功空 dict，导致上层把故障误判为"轮询待确认"无限空转）：
          - HTTP >= 400 一律失败（429 统一映射为 EResult 84 限流）；
          - 响应体不是合法 JSON（如 HTML 错误页 / 空响应体）→ "响应体异常"；
          - x-eresult 头存在但无法解析为整数 → 抛错而非按成功处理；
          - x-eresult != 1 → 按官方 EResult 错误码抛错。
        唯一放行场景：HTTP 2xx + 合法 JSON（含 {"response": {}} 空响应，即 QR
        轮询待确认态）+ x-eresult=1。
        """
        # 官方安全错误优先于 HTTP 状态，避免 403 + AccountLockedDown 被误当普通过期。
        eresult_raw = resp.headers.get("x-eresult")
        eresult: int | None = None
        if eresult_raw is not None:
            try:
                eresult = int(eresult_raw)
            except (TypeError, ValueError):
                raise SteamLoginError(f"{action}: x-eresult 响应头异常，无法解析")
        if eresult is not None and eresult != EResult_OK:
            raise SteamLoginError(
                f"{action} 失败", eresult=eresult, retry_after=_retry_after_seconds(resp)
            )

        # 不跟随认证重定向，也不把缺少成功头的空结果当成待确认。
        if not 200 <= resp.status_code < 300:
            if resp.status_code == 429:
                raise SteamLoginError(
                    f"{action}: 请求过于频繁，被 Steam 限流 (HTTP 429)",
                    eresult=84,
                    retry_after=_retry_after_seconds(resp),
                )
            if resp.status_code in (401, 403):
                raise SteamLoginError(
                    f"{action}: Steam 拒绝访问 (HTTP {resp.status_code})", eresult=15
                )
            raise SteamLoginError(f"{action}: Steam 服务异常 (HTTP {resp.status_code})")

        if eresult is None:
            raise SteamLoginError(f"{action}: 响应体异常，缺少 x-eresult 成功标记")

        content_type = str(resp.headers.get("content-type", "")).split(";", 1)[0].strip().lower()
        if method and content_type in ("application/octet-stream", "application/x-protobuf"):
            try:
                return decode_response(method, resp.content)
            except Exception as exc:
                raise SteamLoginError(f"{action}: Protobuf 响应体异常") from exc
        try:
            payload = resp.json()
        except Exception as e:
            raise SteamLoginError(
                f"{action}: 响应体异常，无法解析为 JSON ({e.__class__.__name__})"
            ) from e
        if isinstance(payload, dict) and isinstance(payload.get("response"), dict):
            return payload["response"]
        raise SteamLoginError(
            f"{action}: 响应体异常，JSON 结构不符合预期 ({type(payload).__name__})"
        )

    async def _get(self, path: str, params: dict[str, Any], action: str) -> dict[str, Any]:
        method = path.strip("/").split("/")[0]
        query = {"input_protobuf_encoded": encode_request(method, params)}
        if self.platform_type == PLATFORM_TYPE_MOBILE_APP:
            query["origin"] = "SteamMobile"
        try:
            resp = await self._client.get(url=f"{_API_BASE}{path}", params=query)
        except httpx.RequestError as e:
            raise SteamLoginError(f"{action}: 网络请求异常 ({e.__class__.__name__})") from e
        return self._parse_response(resp, action, method)

    async def _post(self, path: str, data: dict[str, Any], action: str) -> dict[str, Any]:
        method = path.strip("/").split("/")[0]
        try:
            encoded = encode_request(method, data)
        except (ValueError, TypeError) as exc:
            raise SteamLoginError(f"{action}: 认证参数无法编码为 Protobuf") from exc
        try:
            resp = await self._client.post(
                url=f"{_API_BASE}{path}",
                files={"input_protobuf_encoded": (None, encoded)},
            )
        except httpx.RequestError as e:
            raise SteamLoginError(f"{action}: 网络请求异常 ({e.__class__.__name__})") from e
        return self._parse_response(resp, action, method)

    # ------------------------------------------------------------------
    # TOTP 对时 (ITwoFactorService/QueryTime)
    # ------------------------------------------------------------------
    async def query_server_time(self) -> int | None:
        """
        查询 Steam 服务器时间（POST ITwoFactorService/QueryTime/v1，免费接口、无需参数，
        响应 response.server_time）。Steam Guard 验证码是 30 秒时间窗的 TOTP，
        本地时钟偏移会直接导致验证码被拒 (EResult 88)，生成验证码前必须对时。
        返回 server_time（UNIX 秒）；网络失败 / 响应异常时返回 None（调用方按 offset=0 降级）。
        """
        try:
            resp = await self._client.post(_QUERY_TIME_URL, data={})
            payload = resp.json()
        except Exception as e:
            logger.debug(f"QueryTime request failed: {type(e).__name__}")
            return None
        try:
            server_time = int((payload.get("response") or {}).get("server_time", 0) or 0)
        except Exception as e:
            logger.debug(f"QueryTime response invalid: {e}")
            return None
        return server_time if server_time > 0 else None

    async def _resolve_time_offset(self) -> int:
        """
        获取本地与 Steam 服务器的时钟偏移（offset = server_time - 本地时间）。
        验证码被拒时清除缓存，下一次由用户决定是否重试；不猜测相邻时间窗。
        """
        if self._server_time_offset is not None:
            return self._server_time_offset
        offset = 0
        server_time = await self.query_server_time()
        if server_time:
            offset = int(server_time) - int(time.time())
            if offset:
                logger.info(
                    f"Steam 服务器对时完成：本地时钟偏移 {offset:+d} 秒，验证码将按服务器时间生成"
                )
        else:
            logger.warning("Steam 服务器对时失败 (QueryTime 不可达)，按 offset=0 降级生成验证码")
        self._server_time_offset = offset
        return offset

    # ------------------------------------------------------------------
    # 路线一：账号密码 + shared_secret
    # ------------------------------------------------------------------
    async def get_password_rsa_public_key(self, account_name: str) -> dict[str, Any]:
        """获取用于加密密码的 RSA 公钥 (publickey_mod / publickey_exp / timestamp)。"""
        return await self._get(
            "/GetPasswordRSAPublicKey/v1/",
            params={"account_name": account_name},
            action="获取 RSA 公钥",
        )

    async def begin_session_with_credentials(
        self,
        account_name: str,
        password: str,
        persistence: int = PERSISTENCE_PERSISTENT,
        guard_data: str = "",
    ) -> dict[str, Any]:
        """RSA 加密密码并创建登录会话，返回 client_id / request_id / steamid / 验证方式列表。"""
        if self.platform_type != PLATFORM_TYPE_WEB_BROWSER:
            raise SteamLoginError("仅支持网页端新登录；旧移动端令牌只能用于兼容续签")
        if persistence not in (PERSISTENCE_PERSISTENT, PERSISTENCE_EPHEMERAL):
            raise SteamLoginError("无效会话持久化设置")
        rsa_key = await self.get_password_rsa_public_key(account_name)
        publickey_mod = rsa_key.get("publickey_mod", "")
        publickey_exp = rsa_key.get("publickey_exp", "")
        timestamp = rsa_key.get("timestamp", 0)
        if not publickey_mod or not publickey_exp:
            raise SteamLoginError(f"账号 {account_name} 获取 RSA 公钥失败：Steam 返回内容为空")

        encrypted_password = encrypt_password_rsa(password, publickey_mod, publickey_exp)
        data: dict[str, Any] = {
            "account_name": account_name,
            "encrypted_password": encrypted_password,
            "encryption_timestamp": int(timestamp),
            "remember_login": persistence == PERSISTENCE_PERSISTENT,
            "persistence": persistence,
            "website_id": self.website_id,
            "device_details": self.device_details,
        }
        if guard_data:
            data["guard_data"] = guard_data
        return await self._post("/BeginAuthSessionViaCredentials/v1/", data=data, action="凭据登录")

    async def submit_steam_guard_code(
        self, client_id: str, steamid: str, code: str, code_type: int = GUARD_TYPE_DEVICE_CODE
    ) -> None:
        """向登录会话提交 Steam 令牌验证码 / 邮件验证码。"""
        result = await self._post(
            "/UpdateAuthSessionWithSteamGuardCode/v1/",
            data={
                "client_id": str(client_id),
                "steamid": str(steamid),
                "code": str(code).strip().upper(),
                "code_type": code_type,
            },
            action="提交 Steam 令牌验证码",
        )
        if result.get("agreement_session_url"):
            raise SteamLoginError("Steam 要求人工确认服务协议，请在官方客户端完成")

    async def poll_auth_session(self, client_id: str, request_id: str) -> dict[str, Any]:
        """轮询登录会话状态；确认通过后返回 refresh_token / access_token / account_name。"""
        return await self._post(
            "/PollAuthSessionStatus/v1/",
            data={"client_id": str(client_id), "request_id": str(request_id)},
            action="轮询登录状态",
        )

    async def login_with_password(
        self,
        account_name: str,
        password: str,
        shared_secret: str = "",
        poll_timeout: float = 60.0,
        guard_data: str = "",
        allow_interactive: bool = False,
        on_confirmation_required: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """
        **路线一完整流程**：账号密码 + 本地计算 Steam 令牌验证码全自动登录。
        成功返回 {"access_token", "refresh_token", "steamid", "account_name"}。
        """
        if not account_name or not password:
            raise SteamLoginError("请先在插件配置中填写 Steam 账号名与登录密码")

        if shared_secret:
            # 在发起账号登录前验证本地配置，避免无效 secret 触发多余登录。
            generate_steam_guard_code_for_time(shared_secret, time.time())
        kwargs = {"guard_data": guard_data} if guard_data else {}
        session = await self.begin_session_with_credentials(account_name, password, **kwargs)
        client_id = session.get("client_id", "")
        request_id = session.get("request_id", "")
        steamid = str(session.get("steamid", "") or "")
        confirmations = session.get("allowed_confirmations", []) or []
        poll_interval = float(session.get("interval", 5.0) or 5.0)
        if not client_id or not request_id or not steamid:
            raise SteamLoginError("Steam 未返回完整登录会话，已停止本次登录")
        if session.get("agreement_session_url"):
            raise SteamLoginError("Steam 要求人工确认服务协议，请在官方客户端完成")
        confirm_types = [int(c.get("confirmation_type", 0)) for c in confirmations if c]

        needs_steam_guard_code = GUARD_TYPE_DEVICE_CODE in confirm_types
        needs_email_code = GUARD_TYPE_EMAIL_CODE in confirm_types

        can_wait_for_confirmation = bool(
            GUARD_TYPE_DEVICE_CONFIRMATION in confirm_types
            or GUARD_TYPE_EMAIL_CONFIRMATION in confirm_types
        )
        if GUARD_TYPE_NONE in confirm_types:
            pass
        elif needs_steam_guard_code and shared_secret:
            # 对时：TOTP 验证码锚定服务器时间生成，避免本地时钟偏移导致 88 拒绝
            offset = await self._resolve_time_offset()
            code = generate_steam_guard_code_for_time(shared_secret, time.time() + offset)
            try:
                # 每次登录会话只提交一个对时后的验证码；错误时重新对时后由用户决定重试。
                await self.submit_steam_guard_code(client_id, steamid, code)
            except SteamLoginError as exc:
                if exc.eresult == 88:
                    self._server_time_offset = None
                raise
        elif guard_data and GUARD_TYPE_MACHINE_TOKEN in confirm_types:
            if self.platform_type != PLATFORM_TYPE_WEB_BROWSER:
                raise SteamLoginError(
                    "此平台无法通过网页机器令牌验证，请手动登录", requires_confirmation=True
                )
            await self._check_browser_machine_auth(client_id, steamid, guard_data)
        elif allow_interactive and can_wait_for_confirmation:
            if on_confirmation_required:
                await on_confirmation_required(
                    "请在 Steam 官方手机 App / 邮件中确认本次登录；核对设备名称和位置，"
                    "若不是你发起的登录请拒绝。"
                )
        elif needs_steam_guard_code:
            raise SteamLoginError(
                "该账号需要手机令牌验证码但未配置 shared_secret，请手动登录",
                requires_confirmation=True,
            )
        elif can_wait_for_confirmation:
            raise SteamLoginError(
                "该账号需要人工手机确认弹窗/邮件确认，自动重登已暂停",
                requires_confirmation=True,
            )
        elif needs_email_code or GUARD_TYPE_MACHINE_TOKEN in confirm_types:
            raise SteamLoginError(
                "该账号需要人工邮件验证码或重新验证设备，请使用扫码登录",
                requires_confirmation=True,
            )
        else:
            raise SteamLoginError("Steam 返回了未知验证方式，已停止自动尝试")

        return await self.wait_for_session_tokens(
            client_id,
            request_id,
            poll_interval=poll_interval,
            timeout=poll_timeout,
            fallback_steamid=steamid,
        )

    async def _check_browser_machine_auth(
        self, client_id: str, steamid: str, guard_data: str
    ) -> None:
        """WebBrowser 的记住设备验证（node-steam-session checkdevice 路径）。"""
        try:
            response = await self._client.post(
                "https://login.steampowered.com/jwt/checkdevice",
                files={"clientid": (None, str(client_id)), "steamid": (None, steamid)},
                headers={"Cookie": f"steamMachineAuth{steamid}={guard_data}"},
            )
            _raise_web_auth_http_error(response, "设备授权验证失败")
            payload = response.json()
            if not isinstance(payload, dict):
                raise SteamLoginError("设备授权验证返回无效响应")
            result = int(payload.get("result", 0))
            if result != EResult_OK:
                raise SteamLoginError(
                    "设备授权需要重新验证，请使用扫码登录",
                    eresult=result,
                    requires_confirmation=True,
                )
        except httpx.RequestError as exc:
            raise SteamLoginError(f"设备授权验证网络异常 ({type(exc).__name__})") from exc
        except (ValueError, TypeError) as exc:
            raise SteamLoginError("设备授权验证响应解析失败") from exc

    # ------------------------------------------------------------------
    # 路线二：扫码登录
    # ------------------------------------------------------------------
    async def begin_qr_session(self) -> dict[str, Any]:
        """
        创建扫码登录会话，返回:
        client_id / request_id / challenge_url / interval / version
        """
        if self.platform_type != PLATFORM_TYPE_WEB_BROWSER:
            raise SteamLoginError("仅支持网页端新登录；旧移动端令牌只能用于兼容续签")
        return await self._post(
            "/BeginAuthSessionViaQR/v1/",
            data={
                "device_details": self.device_details,
                "website_id": self.website_id,
            },
            action="创建扫码登录会话",
        )

    async def wait_for_session_tokens(
        self,
        client_id: str,
        request_id: str,
        poll_interval: float = 5.0,
        timeout: float = 180.0,
        on_poll: Callable[[int], Awaitable[None]] | None = None,
        fallback_steamid: str = "",
        challenge_url: str = "",
        on_challenge_rotated: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """
        轮询等待 Steam 下发令牌（扫码确认 / 验证码通过后成立）。
        返回 dict: refresh_token / access_token / account_name / steamid。

        :param challenge_url: 当前已知扫码链接（来自 BeginAuthSession），用于对比检测二维码轮换。
        :param on_challenge_rotated: (可选) 二维码被 Steam 轮换 (PollAuthSessionStatus 返回
            全新 new_challenge_url) 时回调通知，None 安全。过期二维码必须重新展示，
            否则用户扫旧码永远无法确认。
        """
        if not math.isfinite(timeout) or timeout <= 0:
            raise SteamLoginError("登录等待超时必须是正数")
        deadline = time.monotonic() + timeout
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            poll_interval = 5.0
        interval = max(1.0, poll_interval)
        poll_count = 0
        last_challenge_url = challenge_url or ""
        guard_data = ""
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                # Steam 无响应时，也必须在总超时边界内结束，取消会向底层 HTTP 请求传播。
                result = await asyncio.wait_for(
                    self.poll_auth_session(client_id, request_id), timeout=remaining
                )
            except asyncio.TimeoutError as exc:
                raise SteamLoginError("等待用户确认/验证超时") from exc
            new_client_id = str(result.get("new_client_id", "") or "")
            if new_client_id and new_client_id != "0":
                client_id = new_client_id
            guard_data = str(result.get("new_guard_data", "") or guard_data)
            if result.get("agreement_session_url"):
                raise SteamLoginError("Steam 要求人工确认服务协议，请在官方客户端完成")
            # 二维码轮换检测：Steam 会在二维码过期时在轮询响应中下发全新 challenge_url
            new_challenge_url = str(result.get("new_challenge_url", "") or "")
            if new_challenge_url and new_challenge_url != last_challenge_url:
                _validate_challenge_url(new_challenge_url)
                logger.info("二维码已被 Steam 轮换，通知调用方重新展示（链接不写入日志）")
                last_challenge_url = new_challenge_url
                if on_challenge_rotated:
                    try:
                        await on_challenge_rotated(new_challenge_url)
                    except Exception as e:
                        logger.warning(f"on_challenge_rotated 回调执行失败: {type(e).__name__}")
            refresh_token = result.get("refresh_token", "") or ""
            access_token = result.get("access_token", "") or ""
            account_name = result.get("account_name", "") or ""
            if refresh_token:
                steamid = steamid_from_refresh_token(refresh_token) or str(fallback_steamid or "")
                return {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "account_name": account_name,
                    "steamid": steamid,
                    "new_guard_data": guard_data,
                }
            poll_count += 1
            if on_poll:
                await on_poll(poll_count)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(interval, remaining))
        raise SteamLoginError("等待用户确认/验证超时", eresult=0)

    # ------------------------------------------------------------------
    # Refresh Token 会话复用与续签
    # ------------------------------------------------------------------
    async def refresh_tokens(
        self,
        refresh_token: str,
        steamid: str = "",
        renewal: bool = True,
    ) -> dict[str, Any]:
        """
        用 refresh_token 兑换 access_token；移动端走 GenerateAccessTokenForApp，
        网页端通过 finalizelogin 复用现有会话。SteamClient 令牌需已认证的 CM 连接。
        renewal=True 仅请求允许续期，是否下发新令牌以及寿命由 Steam 决定。
        steamid 为空时自动从 refresh_token 的 JWT 声明中解析。
        """
        if not refresh_token:
            raise SteamLoginError("缺少 refresh_token，无法续期")
        audiences = _token_audiences(refresh_token)
        effective_steamid = steamid or steamid_from_refresh_token(refresh_token)
        if not effective_steamid:
            raise SteamLoginError("Refresh Token 缺少有效 SteamID，已停止兑换")
        if not str(effective_steamid).isdigit() or not 0 < int(effective_steamid) < 2**64:
            raise SteamLoginError("Refresh Token 的 SteamID 无效，已停止兑换")
        token_steamid = steamid_from_refresh_token(refresh_token)
        if token_steamid and token_steamid != effective_steamid:
            raise SteamLoginError("Refresh Token 与配置的 SteamID 不匹配，已停止兑换")
        if "client" in audiences and "mobile" not in audiences:
            raise SteamLoginError("SteamClient Refresh Token 需要已认证的 CM 连接，请使用 ASF")
        if "mobile" not in audiences:
            if audiences and "web" in audiences:
                return await self._refresh_browser_session(refresh_token, effective_steamid)
            raise SteamLoginError("无法识别 Refresh Token 平台，已停止自动兑换")
        data: dict[str, Any] = {
            "refresh_token": refresh_token,
            "renewal_type": RENEWAL_TYPE_ALLOW if renewal else RENEWAL_TYPE_NONE,
        }
        if effective_steamid:
            data["steamid"] = str(effective_steamid)
        response = await self._post(
            "/GenerateAccessTokenForApp/v1/", data=data, action="兑换 Access Token"
        )
        access_token = response.get("access_token", "") or ""
        new_refresh_token = response.get("refresh_token", "") or ""
        # 边缘情况：Steam 只下发新 refresh_token 而没给 access_token。
        # 此时不能抛错丢弃新 RT，必须先返回给调用方持久化，
        # access_token 缺失由调用方记录并稍后重试兑换。
        if not access_token and not new_refresh_token:
            raise SteamLoginError("Steam 未返回 access_token", eresult=0)
        return {
            "access_token": access_token,
            "refresh_token": new_refresh_token,
            "steamid": effective_steamid,
        }

    async def _refresh_browser_session(self, refresh_token: str, steamid: str) -> dict[str, Any]:
        """复用网页 Refresh Token，不发起密码登录或新增扫码会话。

        node-steam-session 的 WebBrowser 路径使用 finalizelogin + transfer_info。
        转移目标只允许 Steam 官方白名单，绝不把凭据送到任意返回 URL。
        """
        headers = {
            "User-Agent": BROWSER_UA,
            "Origin": "https://steamcommunity.com",
            "Referer": "https://steamcommunity.com/",
        }
        try:
            response = await self._client.post(
                "https://login.steampowered.com/jwt/finalizelogin",
                files={
                    "nonce": (None, refresh_token),
                    "sessionid": (None, secrets.token_hex(12)),
                    "redir": (None, "https://steamcommunity.com/login/home/?goto="),
                },
                headers=headers,
            )
            _raise_web_auth_http_error(response, "网页会话续签失败")
            payload = response.json()
            if not isinstance(payload, dict):
                raise SteamLoginError("网页会话续签返回了无效响应")
            if payload.get("error"):
                raise SteamLoginError("网页会话续签失败", eresult=int(payload["error"]))
            transfers = payload.get("transfer_info")
            if not isinstance(transfers, list) or not transfers:
                raise SteamLoginError("Steam 未返回网页会话转移信息")
            if str(payload.get("steamID", steamid)) != steamid:
                raise SteamLoginError("网页会话 SteamID 不匹配，已拒绝更新凭据")

            validated_transfers = []
            for transfer in transfers:
                if not isinstance(transfer, dict):
                    raise SteamLoginError("Steam 返回了无效的会话转移信息")
                url = str(transfer.get("url", ""))
                parsed = urlparse(url)
                if (
                    parsed.scheme != "https"
                    or parsed.hostname
                    not in {"steamcommunity.com", "store.steampowered.com", "help.steampowered.com"}
                    or parsed.username
                    or parsed.password
                    or parsed.netloc.lower()
                    not in {
                        "steamcommunity.com",
                        "steamcommunity.com:443",
                        "store.steampowered.com",
                        "store.steampowered.com:443",
                        "help.steampowered.com",
                        "help.steampowered.com:443",
                    }
                    or parsed.path != "/login/settoken"
                    or parsed.query
                    or parsed.fragment
                ):
                    raise SteamLoginError("Steam 返回了非官方会话转移目标，已拒绝发送凭据")
                params = transfer.get("params")
                if not isinstance(params, dict):
                    raise SteamLoginError("Steam 会话转移参数无效")
                validated_transfers.append((url, params))
            for url, params in validated_transfers:
                transfer_resp = await self._client.post(
                    url,
                    files={
                        key: (None, str(value))
                        for key, value in {**params, "steamID": steamid}.items()
                    },
                    headers=headers,
                )
                _raise_web_auth_http_error(transfer_resp, "Steam 会话转移失败")
                transfer_data = transfer_resp.json()
                if not isinstance(transfer_data, dict):
                    raise SteamLoginError("Steam 会话转移响应无效")
                result = int(transfer_data.get("result", EResult_OK))
                if result != EResult_OK:
                    raise SteamLoginError("Steam 会话转移失败", eresult=result)
                login_cookie = transfer_resp.cookies.get("steamLoginSecure")
                if login_cookie:
                    from urllib.parse import unquote

                    cookie_steamid, separator, access_token = unquote(login_cookie).partition("||")
                    if separator and cookie_steamid == steamid and access_token:
                        return {
                            "access_token": access_token,
                            "refresh_token": "",
                            "steamid": steamid,
                        }
            raise SteamLoginError("Steam 未返回可用的网页登录凭据")
        except httpx.RequestError as exc:
            raise SteamLoginError(f"网页会话续签网络异常 ({type(exc).__name__})") from exc
        except (ValueError, TypeError, AttributeError) as exc:
            raise SteamLoginError("网页会话续签响应解析失败") from exc

    async def fetch_qr_image(self, challenge_url: str) -> bytes | None:
        """在本地生成二维码，不再把一次性登录链接发送给第三方服务。"""
        try:
            _validate_challenge_url(challenge_url)
            image = qrcode.make(challenge_url, box_size=8, border=4)
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()
        except Exception as e:
            logger.debug(f"Local QR generation failed: {type(e).__name__}")
        return None
