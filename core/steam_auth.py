"""
Steam "永不掉线" 认证协议支持模块。

实现 Steam 新版 IAuthenticationService 登录协议（与手机 Steam App / ASF 同源）：

**路线一（账号密码 + shared_secret 自动重登，ASF 同款）**:
    1. IAuthenticationService/GetPasswordRSAPublicKey/v1  获取 RSA 公钥
    2. 本地 PKCS#1 v1.5 加密密码（纯 Python 实现，零第三方依赖）
    3. IAuthenticationService/BeginAuthSessionViaCredentials/v1  发起登录会话
    4. 本地 HMAC-SHA1 计算当前 5 位 Steam 令牌验证码（无需看手机）
    5. IAuthenticationService/UpdateAuthSessionWithSteamGuardCode/v1  自动填码
    6. IAuthenticationService/PollAuthSessionStatus/v1  领取全新 200 天 refresh_token

**路线二（扫码 + 滚动续期）**:
    1. IAuthenticationService/BeginAuthSessionViaQR/v1  (platform_type=MobileApp 伪装手机端)
    2. IAuthenticationService/PollAuthSessionStatus/v1  轮询等待手机扫码确认
    3. IAuthenticationService/GenerateAccessTokenForApp/v1  (renewal_type=1 滚动续期,
       Steam 会在 refresh_token 剩余寿命不足时直接下发全新 200 天 refresh_token)

协议编码说明：本插件统一使用 plain HTTP form 绑定访问统一 Web API
（GET 用 query 参数 / POST 用 form 字段，字段名与 proto 字段一致），
响应为 {"response": {...}} JSON，错误通过 x-eresult 响应头传递。
以上绑定方式均已对 api.steampowered.com 实测验证。
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import struct
import time
from typing import Any, Awaitable, Callable

import httpx

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
GUARD_TYPE_MACHINE_TOKEN = 4
GUARD_TYPE_DEVICE_CONFIRMATION = 6
GUARD_TYPE_EMAIL_CONFIRMATION = 7

GUARD_TYPE_NAMES = {
    GUARD_TYPE_NONE: "无需额外验证",
    GUARD_TYPE_EMAIL_CODE: "邮件验证码",
    GUARD_TYPE_DEVICE_CODE: "手机令牌验证码",
    GUARD_TYPE_MACHINE_TOKEN: "机器令牌",
    GUARD_TYPE_DEVICE_CONFIRMATION: "手机确认弹窗",
    GUARD_TYPE_EMAIL_CONFIRMATION: "邮件确认",
    GUARD_TYPE_UNKNOWN: "未知",
}

# ETokenRenewalType
RENEWAL_TYPE_NONE = 0
RENEWAL_TYPE_ALLOW = 1

# ESessionPersistence
PERSISTENCE_PERSISTENT = 1
PERSISTENCE_EPHEMERAL = 2

# EResult (常用子集)
EResult_OK = 1
ERESULT_MESSAGES = {
    2: "常规失败 (Fail)",
    3: "无法连接到 Steam 网络 (NoConnection)",
    5: "账号或密码错误 (InvalidPassword)",
    15: "请求参数无效 (InvalidParam)",
    16: "登录被拒绝 (AccountLoginDenied)",
    65: "本 IP 被 Steam 限制 (IPLoginFailed)",
    83: "登录被风控拦截 (AccessDenied)",
    84: "请求过于频繁被限流 (RateLimitExceeded)",
    85: "登录被风控节流 (AccountLoginDeniedThrottle)",
    88: "Steam 令牌验证码错误 (TwoFactorCodeMismatch)",
}

# Steam Guard 字母表 (剔除易混淆的 0/1/8/9 之外的字符)
STEAM_GUARD_ALPHABET = "23456789BCDFGHJKMNPQRTVWXY"

_API_BASE = "https://api.steampowered.com/IAuthenticationService"

# TOTP 对时接口（免费、无需任何参数）
_QUERY_TIME_URL = "https://api.steampowered.com/ITwoFactorService/QueryTime/v1/"

# 伪装为手机 Steam App 的请求指纹（node-steam-session 同款）
MOBILE_APP_UA = "okhttp/4.9.2"
MOBILE_APP_COOKIES = "mobileClient=android; mobileClientVersion=777777 3.10.3"


class SteamLoginError(Exception):
    """Steam 登录/续期流程失败。"""

    def __init__(self, message: str, eresult: int = 0):
        if eresult and eresult != EResult_OK:
            reason = ERESULT_MESSAGES.get(eresult)
            if reason:
                message = f"{message} (EResult {eresult}: {reason})"
            else:
                message = f"{message} (EResult {eresult})"
        super().__init__(message)
        self.eresult = eresult


def generate_steam_guard_code_for_time(shared_secret: str, unix_timestamp: float) -> str:
    """
    利用 shared_secret 在本地计算指定时刻的 5 位 Steam 2FA 验证码。
    与手机 Steam App / SDA / ASF 完全同算法：HMAC-SHA1 + 30 秒时间步长 + Steam 字符表取模。
    """
    secret_bytes = base64.b64decode(shared_secret)
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
    return generate_steam_guard_code_for_time(
        shared_secret, time.time() + float(offset_seconds)
    )


def encrypt_password_rsa(
    password: str, publickey_mod: str, publickey_exp: str
) -> str:
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


class SteamAuthClient:
    """
    Steam 认证协议客户端（独立于业务 API 客户端 SteamFamilyClient）。
    默认伪装为 MobileApp 平台（k_EAuthTokenPlatformType_MobileApp），
    令牌 audience 为 ['web','mobile']，对风控最友好，滚动续期更稳。
    """

    def __init__(
        self,
        proxy: str = "",
        timeout: int = 20,
        platform_type: int = PLATFORM_TYPE_MOBILE_APP,
        device_name: str = "AstrBot-MiniApp",
    ):
        self.platform_type = platform_type
        self.device_name = device_name
        # 本地与 Steam 服务器的时间偏移（秒）；None=尚未对时，登录时对一次并按实例缓存复用
        self._server_time_offset: int | None = None
        headers = {"User-Agent": MOBILE_APP_UA}
        if platform_type == PLATFORM_TYPE_MOBILE_APP:
            headers["Cookie"] = MOBILE_APP_COOKIES
        elif platform_type == PLATFORM_TYPE_WEB_BROWSER:
            headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        else:  # SteamClient
            headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Valve Steam Client/default/1665786434 "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/85.0.4183.121 Safari/537.36"
            )
        self._client = httpx.AsyncClient(
            proxy=proxy or None,
            timeout=timeout,
            headers=headers,
        )

    async def close(self):
        await self._client.aclose()

    # ------------------------------------------------------------------
    # HTTP 基础封装
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_response(resp: httpx.Response, action: str) -> dict[str, Any]:
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
        # 1) HTTP 状态码兜底：>= 400 一律按失败处理
        if resp.status_code >= 400:
            if resp.status_code == 429:
                raise SteamLoginError(
                    f"{action}: 请求过于频繁，被 Steam 限流 (HTTP 429)", eresult=84
                )
            if resp.status_code in (401, 403):
                raise SteamLoginError(f"{action}: Steam 拒绝访问 (HTTP {resp.status_code})")
            raise SteamLoginError(f"{action}: Steam 服务异常 (HTTP {resp.status_code})")

        # 2) x-eresult 头解析：存在但 int() 失败属于异常形态，按失败处理而非误判成功
        eresult_raw = resp.headers.get("x-eresult")
        eresult: int | None = None
        if eresult_raw is not None:
            try:
                eresult = int(eresult_raw)
            except (TypeError, ValueError):
                raise SteamLoginError(
                    f"{action}: x-eresult 响应头异常 (值 {eresult_raw!r} 无法解析)"
                )

        # 3) Steam 显式报错时优先保留官方 EResult 错误码（即使响应体也解析失败）
        if eresult is not None and eresult != EResult_OK:
            raise SteamLoginError(f"{action} 失败", eresult=eresult)

        # 4) 响应体必须为合法 JSON（fail-closed，不再吞掉 HTML 错误页 / 空响应体）
        try:
            payload = resp.json()
        except Exception as e:
            raise SteamLoginError(
                f"{action}: 响应体异常，无法解析为 JSON ({e.__class__.__name__})"
            ) from e
        if isinstance(payload, dict):
            return payload.get("response") or {}
        raise SteamLoginError(
            f"{action}: 响应体异常，JSON 结构不符合预期 ({type(payload).__name__})"
        )

    async def _get(self, path: str, params: dict[str, Any], action: str) -> dict[str, Any]:
        try:
            resp = await self._client.get(url=f"{_API_BASE}{path}", params=params)
        except httpx.RequestError as e:
            raise SteamLoginError(f"{action}: 网络请求异常 ({e.__class__.__name__})") from e
        return self._parse_response(resp, action)

    async def _post(self, path: str, data: dict[str, Any], action: str) -> dict[str, Any]:
        try:
            resp = await self._client.post(url=f"{_API_BASE}{path}", data=data)
        except httpx.RequestError as e:
            raise SteamLoginError(f"{action}: 网络请求异常 ({e.__class__.__name__})") from e
        return self._parse_response(resp, action)

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
            logger.debug(f"QueryTime request failed: {e}")
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
        对时结果按实例缓存供后续登录复用；查询失败按 offset=0 降级（不阻塞登录，
        验证码被拒后还有 +30/−30 秒窗口重试兜底）。
        """
        if self._server_time_offset is not None:
            return self._server_time_offset
        offset = 0
        server_time = await self.query_server_time()
        if server_time:
            offset = int(server_time) - int(time.time())
            if offset:
                logger.info(f"Steam 服务器对时完成：本地时钟偏移 {offset:+d} 秒，验证码将按服务器时间生成")
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
    ) -> dict[str, Any]:
        """RSA 加密密码并创建登录会话，返回 client_id / request_id / steamid / 验证方式列表。"""
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
            "encryption_timestamp": str(int(timestamp)),
            "remember_login": "true",
            "persistence": str(persistence),
            "platform_type": str(self.platform_type),
            "device_friendly_name": self.device_name,
        }
        if self.platform_type == PLATFORM_TYPE_MOBILE_APP:
            data["website_id"] = "Mobile"
        elif self.platform_type == PLATFORM_TYPE_WEB_BROWSER:
            data["website_id"] = "Community"
        return await self._post(
            "/BeginAuthSessionViaCredentials/v1/", data=data, action="凭据登录"
        )

    async def submit_steam_guard_code(
        self, client_id: str, steamid: str, code: str, code_type: int = GUARD_TYPE_DEVICE_CODE
    ) -> None:
        """向登录会话提交 Steam 令牌验证码 / 邮件验证码。"""
        await self._post(
            "/UpdateAuthSessionWithSteamGuardCode/v1/",
            data={
                "client_id": str(client_id),
                "steamid": str(steamid),
                "code": str(code).strip().upper(),
                "code_type": str(code_type),
            },
            action="提交 Steam 令牌验证码",
        )

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
    ) -> dict[str, Any]:
        """
        **路线一完整流程**：账号密码 + 本地计算 Steam 令牌验证码全自动登录。
        成功返回 {"access_token", "refresh_token", "steamid", "account_name"}。
        """
        if not account_name or not password:
            raise SteamLoginError("请先在插件配置中填写 Steam 账号名与登录密码")

        session = await self.begin_session_with_credentials(account_name, password)
        client_id = session.get("client_id", "")
        request_id = session.get("request_id", "")
        steamid = str(session.get("steamid", "") or "")
        confirmations = session.get("allowed_confirmations", []) or []
        poll_interval = float(session.get("interval", 5.0) or 5.0)
        confirm_types = [int(c.get("confirmation_type", 0)) for c in confirmations if c]

        needs_steam_guard_code = GUARD_TYPE_DEVICE_CODE in confirm_types
        needs_email_code = GUARD_TYPE_EMAIL_CODE in confirm_types

        # 仅"手机确认弹窗"（DeviceConfirmation）保护：这类确认必须人工在手机端点击，
        # 干等轮询只会白白耗满超时（且撞多了触发 84/85 风控），直接快败并说明原因。
        if (
            GUARD_TYPE_DEVICE_CONFIRMATION in confirm_types
            and GUARD_TYPE_DEVICE_CODE not in confirm_types
            and GUARD_TYPE_EMAIL_CODE not in confirm_types
        ):
            raise SteamLoginError(
                "该账号使用手机确认弹窗保护，需要人工在手机端点击确认，此账号无法全自动登录"
            )

        if needs_steam_guard_code or (not confirm_types and shared_secret):
            # 手机令牌验证码：用 shared_secret 本地 HMAC-SHA1 直接算出，
            # 完全不需要看手机。若验证码因跨窗被拒 (EResult 88)，依次换
            # +30 / −30 秒偏移重试（Steam 侧 max_attempts=3，总尝试不超过 3 次，
            # 撞多了会触发 84/85 风控节流）。
            if not shared_secret and needs_steam_guard_code:
                raise SteamLoginError(
                    "该账号已开启手机令牌，但未配置 shared_secret，无法本地算码自动登录；"
                    "请从 maFile / ASF / 已 Root 安卓的 Steamguard 文件中提取并填入配置"
                )
            # 对时：TOTP 验证码锚定服务器时间生成，避免本地时钟偏移导致 88 拒绝
            offset = await self._resolve_time_offset()
            code_anchor = time.time() + float(offset)
            for attempt, anchor in enumerate(
                (code_anchor, code_anchor + 30.0, code_anchor - 30.0), start=1
            ):
                code = generate_steam_guard_code_for_time(shared_secret, anchor)
                try:
                    await self.submit_steam_guard_code(client_id, steamid, code)
                    break
                except SteamLoginError as e:
                    if e.eresult != 88 or attempt >= 3:
                        raise
                    logger.warning(
                        f"Steam Guard 验证码被拒 (EResult 88, 第 {attempt}/3 次)，"
                        "换相邻时间窗重试..."
                    )
        elif needs_email_code:
            raise SteamLoginError(
                "该账号触发了邮件验证码 (EmailCode) 保护，无法自动过验证；"
                "请为账号开启手机令牌并填入共享密钥，走手机验证码全自动重登"
            )
        # 其余情况 (无需验证码 / 仅机器令牌确认) 直接轮询领取令牌

        return await self.wait_for_session_tokens(
            client_id,
            request_id,
            poll_interval=poll_interval,
            timeout=poll_timeout,
            fallback_steamid=steamid,
        )

    # ------------------------------------------------------------------
    # 路线二：扫码登录
    # ------------------------------------------------------------------
    async def begin_qr_session(self) -> dict[str, Any]:
        """
        创建扫码登录会话（伪装 MobileApp 平台），返回:
        client_id / request_id / challenge_url / interval / version
        """
        return await self._post(
            "/BeginAuthSessionViaQR/v1/",
            data={
                "device_friendly_name": self.device_name,
                "platform_type": str(self.platform_type),
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
        deadline = time.time() + timeout
        poll_count = 0
        last_challenge_url = challenge_url or ""
        while time.time() < deadline:
            result = await self.poll_auth_session(client_id, request_id)
            # 二维码轮换检测：Steam 会在二维码过期时在轮询响应中下发全新 challenge_url
            new_challenge_url = str(result.get("new_challenge_url", "") or "")
            if new_challenge_url and new_challenge_url != last_challenge_url:
                logger.info(f"二维码已被 Steam 轮换 (新链接: {new_challenge_url})，通知调用方重新展示")
                last_challenge_url = new_challenge_url
                if on_challenge_rotated:
                    try:
                        await on_challenge_rotated(new_challenge_url)
                    except Exception as e:
                        logger.warning(f"on_challenge_rotated 回调执行失败: {e}")
            refresh_token = result.get("refresh_token", "") or ""
            access_token = result.get("access_token", "") or ""
            account_name = result.get("account_name", "") or ""
            if refresh_token and access_token:
                steamid = steamid_from_refresh_token(refresh_token) or str(
                    fallback_steamid or ""
                )
                return {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "account_name": account_name,
                    "steamid": steamid,
                }
            poll_count += 1
            if on_poll:
                await on_poll(poll_count)
            await asyncio.sleep(max(1.0, poll_interval))
        raise SteamLoginError("等待用户确认/验证超时", eresult=0)

    # ------------------------------------------------------------------
    # 路线二：滚动续期 (无限续杯核心)
    # ------------------------------------------------------------------
    async def refresh_tokens(
        self,
        refresh_token: str,
        steamid: str = "",
        renewal: bool = True,
    ) -> dict[str, Any]:
        """
        用 refresh_token 兑换 access_token。
        renewal=True 时携带 renewal_type=1，Steam 会在 refresh_token 剩余寿命不足时
        在返回体中夹带一条寿命重置为 200 天的新 refresh_token，调用方必须持久化保存！
        steamid 为空时自动从 refresh_token 的 JWT 声明中解析。
        """
        if not refresh_token:
            raise SteamLoginError("缺少 refresh_token，无法续期")
        data: dict[str, Any] = {
            "refresh_token": refresh_token,
            "renewal_type": str(RENEWAL_TYPE_ALLOW if renewal else RENEWAL_TYPE_NONE),
        }
        effective_steamid = steamid or steamid_from_refresh_token(refresh_token)
        if effective_steamid:
            data["steamid"] = str(effective_steamid)
        response = await self._post(
            "/GenerateAccessTokenForApp/v1/", data=data, action="兑换 Access Token"
        )
        access_token = response.get("access_token", "") or ""
        new_refresh_token = response.get("refresh_token", "") or ""
        # 边缘情况：Steam 只顺延了下发新 refresh_token (寿命重置 200 天) 而没给 access_token。
        # 此时绝不能直接抛错把 200 天新 RT 丢掉，必须先把它返回给调用方持久化保命，
        # access_token 缺失由调用方记录并稍后重试兑换。
        if not access_token and not new_refresh_token:
            raise SteamLoginError("Steam 未返回 access_token", eresult=0)
        return {
            "access_token": access_token,
            "refresh_token": new_refresh_token,
            "steamid": effective_steamid,
        }

    async def fetch_qr_image(self, challenge_url: str) -> bytes | None:
        """把 s.team 扫码链接渲染为二维码 PNG（使用公共 QR API，失败返回 None 降级为文字链接）。"""
        try:
            resp = await self._client.get(
                "https://api.qrserver.com/v1/create-qr-code/",
                params={"size": "380x380", "margin": "10", "data": challenge_url},
                timeout=15.0,
            )
            if resp.status_code == 200 and resp.content[:4] == b"\x89PNG":
                return resp.content
        except Exception as e:
            logger.debug(f"QR image fetch failed: {e}")
        return None
