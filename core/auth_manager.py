"""Steam session reuse and conservative login orchestration.

Inspired by ASF: reuse refresh/guard credentials, serialize authentication,
back off on failures, and stop automation when Steam requires human intervention.
No client implementation can guarantee avoidance of account security restrictions.
"""

import asyncio
import hashlib
import inspect
import json
import logging
import math
import time
from typing import Any, Awaitable, Callable

try:
    from .steam_api import SteamAuthExpiredError, decode_steam_jwt
    from .steam_auth import SteamAuthClient, SteamLoginError, _validate_challenge_url
except (ImportError, ValueError):
    from core.steam_api import SteamAuthExpiredError, decode_steam_jwt
    from core.steam_auth import SteamAuthClient, SteamLoginError, _validate_challenge_url

logger = logging.getLogger(__name__)

# Access Token 有效期约 24 小时：剩余寿命低于该阈值时提前续期
DEFAULT_ACCESS_TOKEN_THRESHOLD_SECS = 3 * 3600
# 失败退避冷却
RT_EXCHANGE_FAIL_COOLDOWN_SECS = 15 * 60
PASSWORD_RELOGIN_FAIL_COOLDOWN_SECS = 30 * 60
COOKIE_FAIL_COOLDOWN_SECS = 15 * 60
LOGIN_RATE_LIMIT_COOLDOWN_SECS = 25 * 60  # ASF 的限流退避下限
LOGIN_MIN_INTERVAL_SECS = 60
MAX_LOGIN_FAILURES = 3
AUTH_STATE_KEY = "steam_auth_state"
_SECURITY_RESULTS = {17, 43, 72, 73, 82, 83, 105}
_HUMAN_REQUIRED_RESULTS = {63, 65, 85, 88, 101}
_REVOKED_RESULTS = {26, 27, 126}


class SteamAuthManager:
    def __init__(
        self,
        auth: SteamAuthClient,
        get_cred: Callable[[str], str],
        set_cred: Callable[[str, str], Awaitable[None] | None],
        cookie_refresh_fn: Callable[[str], Awaitable[str | None]] | None = None,
        notify_fn: Callable[[str], Awaitable[None]] | None = None,
        load_state_fn: Callable[[], Awaitable[str | None]] | None = None,
    ):
        """
        :param auth: SteamAuthClient 实例
        :param get_cred: 读取凭据 (key -> value)，key 与 PluginConfig 字段同名
        :param set_cred: 写入并持久化凭据
        :param cookie_refresh_fn: (可选) 旧版 Cookie 静默刷新能力，用于最终兜底
        :param notify_fn: (可选) 自动向订阅会话广播保活事件
        """
        self.auth = auth
        self._get_cred = get_cred
        self._set_cred = set_cred
        self._cookie_refresh_fn = cookie_refresh_fn
        self._notify_fn = notify_fn
        self._load_state_fn = load_state_fn
        self._state_loaded = load_state_fn is None
        self._state_lock = asyncio.Lock()

        self._rt_fail_cooldown_until: float = 0.0
        self._relogin_fail_cooldown_until: float = 0.0
        self._cookie_fail_cooldown_until: float = 0.0
        self.last_keepalive_note: str = ""
        self._auth_cooldown_until = 0.0
        self._login_next_allowed_at = 0.0
        self._login_failures = 0
        self._automatic_login_paused = False
        self._security_blocked = False
        self._rt_invalid_fingerprint = ""
        self._pause_reason = ""
        # 登录/续期互斥标志（事件循环单线程内 check-then-set，无 await 悬窗）：
        # 防止并发触发多条完整登录链路（如 QR 扫码等待期间又有人发起密码重登）
        self._ops_busy: bool = False

    # ------------------------------------------------------------------
    # 凭据便捷访问
    # ------------------------------------------------------------------
    def set_clients(
        self,
        auth: SteamAuthClient,
        cookie_refresh_fn: Callable[[str], Awaitable[str | None]] | None = None,
    ):
        """网络层重建（如代理变更）后替换底层认证客户端。"""
        self.auth = auth
        if cookie_refresh_fn is not None:
            self._cookie_refresh_fn = cookie_refresh_fn

    @property
    def access_token(self) -> str:
        return self._get_cred("access_token") or ""

    @property
    def refresh_token(self) -> str:
        return self._get_cred("refresh_token") or ""

    @property
    def steam_id(self) -> str:
        return self._get_cred("steam_id") or ""

    @property
    def has_password_login(self) -> bool:
        """是否配置了账号与密码；自动重登另要求 shared_secret 和显式启用。"""
        return bool(self._get_cred("account_name")) and bool(self._get_cred("password"))

    @property
    def has_shared_secret(self) -> bool:
        return bool(self._get_cred("shared_secret"))

    @property
    def auto_relogin_enabled(self) -> bool:
        """是否允许插件自动发起账号密码重登（路线一）。"""
        value = self._get_cred("enable_auto_relogin")
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)

    @property
    def security_blocked(self) -> bool:
        return self._security_blocked

    @property
    def automatic_login_paused(self) -> bool:
        return self._automatic_login_paused

    @property
    def auth_cooldown_remaining(self) -> int:
        return max(0, math.ceil(self._auth_cooldown_until - time.time()))

    def restore_state(self, raw: str) -> None:
        """重启恢复安全暂停/退避；状态中没有任何密码或完整令牌。"""
        if not raw:
            return
        try:
            state = json.loads(raw)
            if not isinstance(state, dict):
                raise ValueError("auth state is not a dict")
            for key, attr in (
                ("auth_cooldown_until", "_auth_cooldown_until"),
                ("login_next_allowed_at", "_login_next_allowed_at"),
                ("rt_fail_cooldown_until", "_rt_fail_cooldown_until"),
                ("relogin_fail_cooldown_until", "_relogin_fail_cooldown_until"),
                ("cookie_fail_cooldown_until", "_cookie_fail_cooldown_until"),
            ):
                value = float(state.get(key, 0))
                if not math.isfinite(value):
                    raise ValueError("non-finite cooldown")
                setattr(self, attr, max(0.0, value))
            for key in ("automatic_login_paused", "security_blocked"):
                if key in state and not isinstance(state[key], bool):
                    raise ValueError("invalid security flag")
            self._login_failures = max(0, int(state.get("login_failures", 0)))
            self._automatic_login_paused = state.get("automatic_login_paused") is True
            self._security_blocked = state.get("security_blocked") is True
            self._rt_invalid_fingerprint = str(state.get("rt_invalid_fingerprint", ""))
            self._pause_reason = str(state.get("pause_reason", ""))
            if self._pause_reason:
                self.last_keepalive_note = self._pause_reason
        except (ValueError, TypeError, OverflowError):
            self._security_blocked = True
            self._automatic_login_paused = True
            self._pause_reason = "认证安全状态损坏，已暂停自动操作，请管理员检查后恢复"
            self.last_keepalive_note = self._pause_reason
            logger.warning(self._pause_reason)

    async def ensure_state_loaded(self) -> None:
        """包括启动期间收到的手动指令，都必须先读安全状态再发网络请求。"""
        if self._state_loaded:
            return
        async with self._state_lock:
            if not self._state_loaded:
                self.restore_state(await self._load_state_fn() or "")
                self._state_loaded = True

    async def _save_credential(self, key: str, value: str) -> None:
        result = self._set_cred(key, value)
        if inspect.isawaitable(result):
            await result

    async def _persist_state(self) -> None:
        await self._save_credential(
            AUTH_STATE_KEY,
            json.dumps(
                {
                    "auth_cooldown_until": self._auth_cooldown_until,
                    "login_next_allowed_at": self._login_next_allowed_at,
                    "rt_fail_cooldown_until": self._rt_fail_cooldown_until,
                    "relogin_fail_cooldown_until": self._relogin_fail_cooldown_until,
                    "cookie_fail_cooldown_until": self._cookie_fail_cooldown_until,
                    "login_failures": self._login_failures,
                    "automatic_login_paused": self._automatic_login_paused,
                    "security_blocked": self._security_blocked,
                    "rt_invalid_fingerprint": self._rt_invalid_fingerprint,
                    "pause_reason": self._pause_reason,
                }
            ),
        )

    def _auth_block_reason(self, *, new_login: bool = False, interactive: bool = False) -> str:
        if self._security_blocked:
            return "Steam 已返回账号/IP/设备安全限制，已暂停认证。请先通过 Steam 官方支持处理，不要反复登录"
        if self.auth_cooldown_remaining:
            return f"Steam 认证正在限流退避，请 {self.auth_cooldown_remaining} 秒后再试"
        if new_login:
            if self._automatic_login_paused and not interactive:
                return self._pause_reason or "自动重登已暂停，请手动确认账号安全"
            remaining = math.ceil(self._login_next_allowed_at - time.time())
            if remaining > 0:
                return f"登录请求有最小间隔限制，请 {remaining} 秒后再试"
        return ""

    async def resume_after_security_review(self) -> str:
        """人工恢复只清除安全暂停，绝不绕过限流期限或自动触发一次新登录。"""
        await self.ensure_state_loaded()
        if self._ops_busy:
            return "⏳ 有认证操作正在进行，暂不能恢复安全状态"
        if self.auth_cooldown_remaining:
            return f"⏳ Steam 限流冷却仍有 {self.auth_cooldown_remaining} 秒，不能提前恢复"
        self._ops_busy = True
        try:
            self._security_blocked = False
            self._automatic_login_paused = False
            self._pause_reason = ""
            self._login_failures = 0
            await self._persist_state()
        finally:
            self._ops_busy = False
        self.last_keepalive_note = "管理员已确认处理 Steam 安全限制，本地暂停已解除；未自动发起登录"
        return "✅ 本地认证暂停已解除（不会解除 Steam 官方限制，也未发起新登录）。仅在官方账号已恢复且网络出口可信时，再手动登录。"

    async def _record_failure(self, exc: SteamLoginError, *, login: bool = False) -> None:
        if exc.eresult in _SECURITY_RESULTS:
            self._security_blocked = True
            self._automatic_login_paused = True
            self._pause_reason = (
                f"Steam 安全限制 (EResult {exc.eresult})，认证已暂停，请联系 Steam 官方支持"
            )
        elif exc.eresult in (84, 87):
            self._auth_cooldown_until = max(
                self._auth_cooldown_until,
                time.time() + max(LOGIN_RATE_LIMIT_COOLDOWN_SECS, exc.retry_after),
            )
        elif login:
            self._login_failures += 1
            if (
                exc.requires_confirmation
                or exc.eresult in _HUMAN_REQUIRED_RESULTS
                or exc.eresult in (5, 15)
                or self._login_failures >= MAX_LOGIN_FAILURES
            ):
                self._automatic_login_paused = True
                self._pause_reason = (
                    "登录凭据被拒绝或需要人工确认，已暂停自动重登，请检查配置与账号安全"
                )
        await self._persist_state()

    async def record_api_auth_failure(self, eresult: int) -> None:
        """业务 API 的显式安全/限流代码与登录端共享同一暂停状态。"""
        await self.ensure_state_loaded()
        if eresult in _SECURITY_RESULTS or eresult in (84, 87):
            await self._record_failure(SteamLoginError("Steam 业务接口拒绝授权", eresult=eresult))

    async def _login_succeeded(self) -> None:
        self._login_failures = 0
        self._automatic_login_paused = False
        self._pause_reason = ""
        self._rt_invalid_fingerprint = ""
        self._relogin_fail_cooldown_until = 0.0
        self._rt_fail_cooldown_until = 0.0
        await self._persist_state()

    @staticmethod
    def _token_fingerprint(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _validate_token_account(self, tokens: dict[str, Any], expected_name: str = "") -> str:
        ids = {str(tokens["steamid"])} if tokens.get("steamid") else set()
        for key in ("access_token", "refresh_token"):
            info = decode_steam_jwt(tokens.get(key, ""))
            if info and info.get("steamid"):
                ids.add(str(info["steamid"]))
        if len(ids) > 1 or (self.steam_id and ids and ids != {self.steam_id}):
            raise SteamLoginError("登录账号 SteamID 不匹配，未覆盖原有凭据，请检查扫描的账号")
        name = str(tokens.get("account_name", ""))
        if expected_name and name and expected_name.casefold() != name.casefold():
            raise SteamLoginError("登录账号名不匹配，未覆盖原有凭据")
        return next(iter(ids), self.steam_id)

    async def _save_session(self, tokens: dict[str, Any], expected_name: str = "") -> bool:
        steamid = self._validate_token_account(tokens, expected_name)
        if not steamid:
            raise SteamLoginError("无法确认登录账号 SteamID，未覆盖原有凭据")
        if not tokens.get("refresh_token"):
            raise SteamLoginError("Steam 未返回 Refresh Token，未覆盖原有凭据")
        # 保存顺序重要：即使 Steam 此次不返回 AT，也保住 RT / new_guard_data。
        await self._save_credential("refresh_token", tokens["refresh_token"])
        await self._save_credential("steam_refresh_account_id", steamid)
        if tokens.get("new_guard_data"):
            await self._save_credential("steam_guard_data", tokens["new_guard_data"])
            if expected_name or tokens.get("account_name"):
                await self._save_credential(
                    "steam_guard_account", expected_name or tokens["account_name"]
                )
        if steamid and not self.steam_id:
            await self._save_credential("steam_id", steamid)
        if tokens.get("account_name"):
            await self._save_credential("account_name", tokens["account_name"])
        if tokens.get("access_token"):
            await self._save_credential("access_token", tokens["access_token"])
            return True
        return False

    # ------------------------------------------------------------------
    # 状态描述（供 WebUI / 指令展示）
    # ------------------------------------------------------------------
    def describe_mode(self) -> str:
        if self.security_blocked:
            return "🛑 Steam 安全限制，认证已暂停"
        if self.auth_cooldown_remaining:
            return "⏳ Steam 认证限流退避中"
        if self.automatic_login_paused:
            return "⏸️ 自动重登已暂停，等待人工确认"
        if self.has_password_login and self.has_shared_secret:
            return (
                "🔐 2FA 自动重登（已启用）" if self.auto_relogin_enabled else "🔐 密码重登仅限手动"
            )
        if self.has_password_login:
            return "⚠️ 密码已填但缺 shared_secret，重登需人工过验证"
        if self.refresh_token:
            return "🔄 已有 Refresh Token 会话复用"
        if self._get_cred("cookie"):
            return "🍪 Cookie 静默续签"
        return "❓ 未配置任何保活方式"

    def refresh_token_days_left(self) -> float:
        """从未验签的 JWT 提示计算剩余天数，不代表服务端保证仍有效。"""
        info = decode_steam_jwt(self.refresh_token or "")
        if info:
            return round(info.get("remaining_seconds", 0) / 86400.0, 1)
        return 0.0

    def access_token_hours_left(self) -> float:
        info = decode_steam_jwt(self.access_token or "")
        if info:
            return float(info.get("remaining_hours", 0.0))
        return 0.0

    async def _notify(self, text: str):
        if self._notify_fn:
            try:
                await self._notify_fn(text)
            except Exception as e:
                logger.debug(f"Keepalive notify failed: {e}")

    # ------------------------------------------------------------------
    # 路线二：滚动续期
    # ------------------------------------------------------------------
    async def exchange_refresh_token(self, force_new: bool = False) -> bool:
        """
        复用 refresh_token 兑换 access_token；若 Steam 下发新 RT 立即持久化。
        是否续期及实际寿命由 Steam 决定。
        返回 True 表示成功拿到了可用的 access_token。
        """
        await self.ensure_state_loaded()
        reason = self._auth_block_reason()
        if reason:
            self.last_keepalive_note = reason
            return False
        if self._ops_busy:
            # 如 QR 扫码确认会话进行中：不并行打登录类接口，交由 cookie 兜底
            self.last_keepalive_note = "已有登录/续期会话进行中，跳过本次 Refresh Token 兑换"
            return False
        self._ops_busy = True
        try:
            return await self._exchange_refresh_token_impl(force_new=force_new)
        finally:
            self._ops_busy = False

    async def _exchange_refresh_token_impl(self, force_new: bool = False) -> bool:
        """
        复用 refresh_token 兑换 access_token，并保存 Steam 下发的轮换凭据。
        返回 True 表示成功拿到了可用的 access_token。
        """
        refresh_token = self.refresh_token
        if not refresh_token:
            return False
        rt_info = decode_steam_jwt(refresh_token)
        if rt_info and rt_info.get("is_expired"):
            self._rt_invalid_fingerprint = self._token_fingerprint(refresh_token)
            self.last_keepalive_note = "Refresh Token 已到期，不再向 Steam 重复兑换"
            await self._persist_state()
            return False
        if time.time() < self._rt_fail_cooldown_until:
            self.last_keepalive_note = "Refresh Token 兑换处于退避冷却，强制刷新也不会绕过冷却"
            return False
        fingerprint = self._token_fingerprint(refresh_token)
        if fingerprint == self._rt_invalid_fingerprint:
            self.last_keepalive_note = "已确认该 Refresh Token 被撤销/过期，不再反复兑换"
            return False
        try:
            reason = self._auth_block_reason()
            if reason:
                self.last_keepalive_note = reason
                return False
            result = await self.auth.refresh_tokens(
                refresh_token, steamid=self.steam_id, renewal=True
            )
        except SteamLoginError as e:
            logger.warning(f"Refresh Token 续期失败: {e}")
            self._rt_fail_cooldown_until = time.time() + RT_EXCHANGE_FAIL_COOLDOWN_SECS
            if e.eresult in _REVOKED_RESULTS:
                self._rt_invalid_fingerprint = fingerprint
            await self._record_failure(e)
            if self.security_blocked or self.auth_cooldown_remaining:
                self.last_keepalive_note = self._auth_block_reason()
                return False
            # 兜底价值判断：若当前 Access Token 的 JWT 显示仍有效（剩余 > 20 分钟），
            # 立即返回 True——续期暂失败但当前凭据够用，不触发昂贵的账号密码重登
            # （AT 真过期才允许走密码重登链路）。
            # 注意仅在自动保活路径（force_new=False）生效：force_new=True 意味着
            # 刚收到 Steam 401 或用户手动续期，此时 "JWT 未过期" 不可信，照常返回失败。
            at_info = decode_steam_jwt(self.access_token or "")
            if (
                not force_new
                and at_info
                and not at_info.get("is_expired")
                and int(at_info.get("remaining_seconds", 0) or 0) > 20 * 60
            ):
                self.last_keepalive_note = (
                    f"续期暂失败（{e}）但当前 Access Token 仍可用"
                    f"（剩余 {self.access_token_hours_left()}h），暂不触发密码重登"
                )
                logger.info(self.last_keepalive_note)
                return True
            self.last_keepalive_note = f"Refresh Token 续期失败: {e}"
            return False

        try:
            self._validate_token_account(result)
        except SteamLoginError as exc:
            self.last_keepalive_note = str(exc)
            self._rt_fail_cooldown_until = time.time() + RT_EXCHANGE_FAIL_COOLDOWN_SECS
            await self._persist_state()
            return False
        # Steam 下发新 refresh_token -> 立刻保存；旧 RT 可能立即失效。
        # 注意顺序：无论如何先持久化新 RT，之后再判断 access_token 是否可用，
        # 避免因 AT 缺失而丢弃新 RT。
        if result.get("refresh_token"):
            await self._save_credential("refresh_token", result["refresh_token"])
            account_id = self._validate_token_account(result)
            if account_id:
                await self._save_credential("steam_refresh_account_id", account_id)
            logger.info("Steam 已下发新 Refresh Token，已持久化；实际有效期由 Steam 决定。")
        if result.get("steamid") and not self.steam_id:
            await self._save_credential("steam_id", result["steamid"])
        access_token = result.get("access_token", "") or ""
        if not access_token:
            # 边缘情况：Steam 只重置了 refresh_token 寿命但未返回 access_token。
            # 新 RT 已保住，下一轮 ensure_valid_access_token 会用新 RT 重新兑换。
            self.last_keepalive_note = (
                "Steam 未返回可用 Access Token（新 Refresh Token 已持久化，稍后自动重试兑换）"
            )
            logger.warning(self.last_keepalive_note)
            self._rt_fail_cooldown_until = time.time() + RT_EXCHANGE_FAIL_COOLDOWN_SECS
            await self._persist_state()
            return False
        await self._save_credential("access_token", access_token)
        self._rt_fail_cooldown_until = 0.0
        self._rt_invalid_fingerprint = ""
        await self._persist_state()
        self.last_keepalive_note = (
            f"滚动续期成功 (Access Token 剩余 {self.access_token_hours_left()}h)"
        )
        return True

    # ------------------------------------------------------------------
    # 路线一：账号密码 + shared_secret 全自动重登
    # ------------------------------------------------------------------
    async def relogin_with_password(
        self,
        interactive: bool = False,
        on_confirmation_required: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """
        账号密码 + shared_secret 走完整登录流程重置一切令牌（互斥：与扫码/续期会话串行）。
        返回面向用户的结果文案。
        """
        await self.ensure_state_loaded()
        reason = self._auth_block_reason(new_login=True, interactive=interactive)
        if reason:
            self.last_keepalive_note = reason
            return f"⚠️ {reason}"
        if self._ops_busy:
            return "⏳ 已有登录 / 续期会话正在进行（如二维码扫码确认等待中），请稍后再试"
        self._ops_busy = True
        try:
            return await self._relogin_with_password_impl(interactive, on_confirmation_required)
        finally:
            self._ops_busy = False

    async def _relogin_with_password_impl(
        self,
        interactive: bool = False,
        on_confirmation_required: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """账号密码 + shared_secret 完整重登链路的实际执行体（由互斥壳调用）。"""
        account_name = self._get_cred("account_name") or ""
        password = self._get_cred("password") or ""
        shared_secret = self._get_cred("shared_secret") or ""
        if not account_name or not password:
            return (
                "⚠️ 未启用路线一保活：请先在插件配置中填写「Steam 账号名」和「登录密码」"
                "（shared_secret 可生成 2FA 验证码，但不能绕过账号安全限制）"
            )
        if time.time() < self._relogin_fail_cooldown_until:
            remaining = int(self._relogin_fail_cooldown_until - time.time())
            return f"⏳ 上次自动重登失败处于退避冷却中，请 {remaining // 60} 分钟后再试"

        self._login_next_allowed_at = time.time() + LOGIN_MIN_INTERVAL_SECS
        await self._persist_state()
        try:
            kwargs: dict[str, Any] = {"shared_secret": shared_secret}
            guard_account = self._get_cred("steam_guard_account") or ""
            guard_data = self._get_cred("steam_guard_data") or ""
            if guard_data and guard_account.casefold() == account_name.casefold():
                kwargs["guard_data"] = guard_data
            if interactive:
                kwargs["allow_interactive"] = True
                kwargs["on_confirmation_required"] = on_confirmation_required
            tokens = await self.auth.login_with_password(account_name, password, **kwargs)
            has_access = await self._save_session(tokens, account_name)
        except SteamLoginError as e:
            self._relogin_fail_cooldown_until = time.time() + PASSWORD_RELOGIN_FAIL_COOLDOWN_SECS
            await self._record_failure(e, login=True)
            self.last_keepalive_note = f"密码重登失败: {e}"
            logger.warning(f"Password relogin failed: {e}")
            return f"❌ 账号密码自动重登失败：{e}"

        await self._login_succeeded()
        if not has_access:
            has_access = await self._exchange_refresh_token_impl()
        if not has_access:
            return "⚠️ Steam 登录已确认，Refresh Token 已保存，但未能兑换 Access Token；请等待冷却后重试续期，不要重复登录"
        self.last_keepalive_note = "密码登录成功，凭据已保存"
        days = self.refresh_token_days_left()
        logger.info("Password relogin succeeded; fresh refresh_token persisted.")
        return f"✅ 密码登录成功！Refresh Token 剩余约 {days} 天，凭据已保存。"

    # ------------------------------------------------------------------
    # 兜底：旧版 Cookie 静默刷新
    # ------------------------------------------------------------------
    async def refresh_via_cookie(self) -> bool:
        await self.ensure_state_loaded()
        reason = self._auth_block_reason()
        if reason:
            self.last_keepalive_note = reason
            return False
        if self._ops_busy:
            self.last_keepalive_note = "已有登录/续期会话进行中，跳过 Cookie 刷新"
            return False
        self._ops_busy = True
        try:
            return await self._refresh_via_cookie_impl()
        finally:
            self._ops_busy = False

    async def _refresh_via_cookie_impl(self) -> bool:
        cookie = self._get_cred("cookie") or ""
        if not cookie or not self._cookie_refresh_fn:
            return False
        if time.time() < self._cookie_fail_cooldown_until:
            return False
        try:
            token = await self._cookie_refresh_fn(cookie)
        except SteamAuthExpiredError as exc:
            self._cookie_fail_cooldown_until = time.time() + COOKIE_FAIL_COOLDOWN_SECS
            await self._record_failure(SteamLoginError("Cookie 授权被拒绝", eresult=exc.eresult))
            self.last_keepalive_note = self._auth_block_reason() or "Cookie 授权被 Steam 拒绝"
            return False
        except Exception as e:
            logger.debug(f"Cookie refresh error: {type(e).__name__}")
            token = None
        if token:
            try:
                self._validate_token_account({"access_token": token})
            except SteamLoginError as exc:
                self.last_keepalive_note = str(exc)
                self._cookie_fail_cooldown_until = time.time() + COOKIE_FAIL_COOLDOWN_SECS
                await self._persist_state()
                return False
            await self._save_credential("access_token", token)
            self._cookie_fail_cooldown_until = 0.0
            await self._persist_state()
            self.last_keepalive_note = "Cookie 静默刷新成功"
            return True
        self._cookie_fail_cooldown_until = time.time() + COOKIE_FAIL_COOLDOWN_SECS
        await self._persist_state()
        self.last_keepalive_note = "Cookie 静默刷新失败（Cookie 可能已过期）"
        return False

    # ------------------------------------------------------------------
    # 统一入口：确保 access_token 可用 (任何 API 调用前执行)
    # ------------------------------------------------------------------
    async def ensure_valid_access_token(
        self,
        min_remaining_secs: int = DEFAULT_ACCESS_TOKEN_THRESHOLD_SECS,
        force: bool = False,
    ) -> bool:
        """
        确保 config 中的 access_token 可用：
        1. Token 足够新鲜 -> 直接返回（零网络开销）
        2. 路线二: refresh_token 滚动续期
        3. Cookie 复用
        4. 显式开启且旧授权确认失效后才进行密码重登
        返回 True 表示当前 access_token 可用。
        """
        await self.ensure_state_loaded()
        access_token = self.access_token
        if self.security_blocked:
            self.last_keepalive_note = self._auth_block_reason()
            return False
        info = decode_steam_jwt(access_token or "")

        if access_token and info and not info.get("is_expired") and not force:
            remaining = int(info.get("remaining_seconds", 0))
            if remaining > min_remaining_secs:
                return True
            self.last_keepalive_note = (
                f"Access Token 仅剩 {info.get('remaining_hours')}h，开始自动续期..."
            )
            logger.info(self.last_keepalive_note)

        if not access_token:
            self.last_keepalive_note = "Access Token 缺失，尝试自动获取..."

        if self._ops_busy:
            self.last_keepalive_note = "已有认证操作进行中，本次不发起第二条登录链路"
            return False
        if (
            self.auto_relogin_enabled
            and self.has_password_login
            and not self.has_shared_secret
            and not self.refresh_token
            and not self._get_cred("cookie")
        ):
            self.last_keepalive_note = "已配置密码但缺 shared_secret，自动重登不可用"
            return False

        # 1) 复用已有 Refresh Token
        if self.refresh_token and await self.exchange_refresh_token(force_new=force):
            return True

        if self.security_blocked or self.auth_cooldown_remaining:
            self.last_keepalive_note = self._auth_block_reason()
            return False
        # 2) 优先复用 Cookie，绝不因为临时网络/限流失败升级成密码登录。
        if await self.refresh_via_cookie():
            return True
        if self.security_blocked or self.auth_cooldown_remaining:
            return False
        if access_token and info and not info.get("is_expired") and not force:
            # 只有 AT 且即将到期时，也不要提前创建新设备登录。
            self.last_keepalive_note = (
                "会话续签暂未完成，但当前 Access Token 尚未到期，不触发新登录"
            )
            return True
        if self.refresh_token:
            rt_info = decode_steam_jwt(self.refresh_token)
            definitively_expired = bool(rt_info and rt_info.get("is_expired"))
            if (
                not definitively_expired
                and self._token_fingerprint(self.refresh_token) != self._rt_invalid_fingerprint
            ):
                self.last_keepalive_note = (
                    "会话续签暂未完成，不因网络错误/拒绝访问创建新设备登录；"
                    "请检查网络或等待冷却，必要时手动登录"
                )
                return False

        # 3) 仅显式开启且旧授权已确认失效时，才允许自动密码重登。
        # 防御：自动重登要求密码与 shared_secret 同时具备——只有密码没有秘钥时
        # 本地无法算 2FA 验证码，自动化流程会卡死在 Steam 令牌验证环节，故跳过
        # 并明确记录原因；手动 /steam_relogin 路径不受此限制（可等待用户人工过锁）。
        if self.auto_relogin_enabled and self.has_password_login:
            if not self.has_shared_secret:
                self.last_keepalive_note = "已配置密码但缺 shared_secret，自动重登不可用"
            else:
                result = await self.relogin_with_password()
                if result.startswith("✅"):
                    try:
                        await self._notify(
                            result + "\n（插件已自动完成 Steam 全自动重登，无需人工处理）"
                        )
                    except Exception:
                        pass
                    return True
        logger.warning(f"Token 保活未能完成: {self.last_keepalive_note}")
        return False

    # ------------------------------------------------------------------
    # 路线二初始绑定：扫码登录
    # ------------------------------------------------------------------
    async def run_qr_login(
        self,
        send_text: Callable[[str], Awaitable[None]] | None = None,
        send_image: Callable[[bytes], Awaitable[None]] | None = None,
        timeout_seconds: float = 180.0,
    ) -> str:
        """
        扫码登录：在固定的浏览器设备身份下发起扫码会话，
        把二维码（图片优先、链接兜底）发给用户，轮询直到手机确认。
        成功后持久化 refresh_token / access_token，令牌寿命由 Steam 决定。
        互斥：会话存续期间（最长 timeout_seconds）阻塞其他登录/续期链路。
        """
        await self.ensure_state_loaded()
        reason = self._auth_block_reason(new_login=True, interactive=True)
        if reason:
            self.last_keepalive_note = reason
            return f"⚠️ {reason}"
        if self._ops_busy:
            return "⏳ 已有登录 / 续期会话正在进行（如密码重登流程中），请稍后再试"
        self._ops_busy = True
        try:
            self._login_next_allowed_at = time.time() + LOGIN_MIN_INTERVAL_SECS
            await self._persist_state()
            return await self._run_qr_login_impl(
                send_text=send_text, send_image=send_image, timeout_seconds=timeout_seconds
            )
        except SteamLoginError as exc:
            await self._record_failure(exc, login=True)
            self.last_keepalive_note = f"扫码登录未完成: {exc}"
            return f"❌ {self.last_keepalive_note}"
        finally:
            self._ops_busy = False

    async def _run_qr_login_impl(
        self,
        send_text: Callable[[str], Awaitable[None]] | None = None,
        send_image: Callable[[bytes], Awaitable[None]] | None = None,
        timeout_seconds: float = 180.0,
    ) -> str:
        """扫码登录实际执行体（由互斥壳调用）。"""
        session = await self.auth.begin_qr_session()
        challenge_url = session.get("challenge_url", "") or ""
        client_id = str(session.get("client_id", "") or "")
        request_id = str(session.get("request_id", "") or "")
        poll_interval = float(session.get("interval", 5.0) or 5.0)
        if not challenge_url or not client_id or not request_id:
            raise SteamLoginError("创建 Steam 扫码会话失败，响应缺少必要字段")
        _validate_challenge_url(challenge_url)

        if send_text:
            await send_text(
                "📱 请使用手机 Steam App 扫描二维码并确认登录（3 分钟内有效）：\n"
                f"{challenge_url}\n\n"
                f"设备名称：{self.auth.device_name}\n"
                "这是一次真实设备登录。请核对 Steam App 显示的位置和设备；"
                "如果账号已有安全限制或不是你发起的登录，请拒绝并联系 Steam 官方支持。"
            )
        if send_image:
            qr_png = await self.auth.fetch_qr_image(challenge_url)
            if qr_png:
                await send_image(qr_png)
            elif send_text:
                await send_text("⚠️ 二维码图片渲染失败，请直接复制上方链接到浏览器打开后再扫码。")

        async def on_challenge_rotated(new_url: str) -> None:
            """
            二维码被 Steam 轮换时的回调：重新渲染并推送新二维码与链接（send_* None 安全）。
            过期二维码必须重发，否则用户扫旧码永远完成不了确认。
            """
            if send_text:
                _validate_challenge_url(new_url)
                await send_text(
                    f"♻️ 二维码已被 Steam 轮换更新，旧码作废，请扫描新的二维码：\n{new_url}"
                )
            if send_image:
                qr_png = await self.auth.fetch_qr_image(new_url)
                if qr_png:
                    await send_image(qr_png)
                elif send_text:
                    await send_text(
                        "⚠️ 新二维码图片渲染失败，请直接复制上方链接到浏览器打开后再扫码。"
                    )

        tokens = await self.auth.wait_for_session_tokens(
            client_id,
            request_id,
            poll_interval=poll_interval,
            timeout=timeout_seconds,
            challenge_url=challenge_url,
            on_challenge_rotated=on_challenge_rotated,
        )
        has_access = await self._save_session(tokens, self._get_cred("account_name") or "")
        await self._login_succeeded()
        if not has_access:
            has_access = await self._exchange_refresh_token_impl()
        if not has_access:
            return "⚠️ Steam 扫码确认成功，Refresh Token 已保存，暂未获取 Access Token；请等待冷却后续期，不要反复扫码"

        days = self.refresh_token_days_left()
        steam_id = self.steam_id
        return (
            f"✅ Steam 扫码登录成功！\n"
            f"👤 账号: {tokens.get('account_name') or steam_id or '未知'}\n"
            f"🎫 Refresh Token 剩余: {days} 天（是否继续续期由 Steam 决定）\n"
            "⏰ 凭据已保存，正在刷新家庭库数据；新设备/异地登录仍可能触发 Steam 安全检查。"
        )
