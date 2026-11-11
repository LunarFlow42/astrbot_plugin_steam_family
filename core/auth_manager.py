"""
Steam "永不掉线" 保活总管 (Keepalive Orchestrator)。

参考手机 Steam App 与 ArchiSteamFarm (ASF) 的保活策略，实现两条互补的保活闭环：

**路线一 —— 账号密码 + shared_secret 全自动重登 (ASF 同款, 100% 永不掉线)**
    当 refresh_token 彻底失效（200 天到期 / Steam 强制下线 / 服务器抽风）时，
    自动用配置的账号密码走完整登录流程，并用本地 shared_secret (HMAC-SHA1)
    直接算出当前 5 位 Steam 令牌验证码自动填入，全程无需人工干预，
    最终拿到一条全新 200 天 refresh_token。
    只有修改 Steam 密码或解绑手机令牌才会失效。

**路线二 —— 伪装 MobileApp 扫码 + renewal_type=1 滚动续期 (无限续杯)**
    平时（99% 的时间）用缓存的 refresh_token 调 GenerateAccessTokenForApp 换取
    access_token，并携带 renewal_type=1：当 Steam 认为当前 refresh_token 剩余寿命
    需要顺延时，会直接在返回体里夹带一条寿命重新重置为 200 天的新 refresh_token，
    本模块会立刻持久化保存。只要插件每天正常轮询（哪怕一天一次），200 天就会
    被无限向后顺延，实现"准永久"在线。

凭据读写通过注入的 get_cred / set_cred 回调完成（由插件主类提供并负责持久化）。
"""

import logging
import time
from typing import Awaitable, Callable

try:
    from .steam_api import decode_steam_jwt
    from .steam_auth import SteamAuthClient, SteamLoginError
except (ImportError, ValueError):
    from core.steam_api import decode_steam_jwt
    from core.steam_auth import SteamAuthClient, SteamLoginError

logger = logging.getLogger(__name__)

# Access Token 有效期约 24 小时：剩余寿命低于该阈值时提前续期
DEFAULT_ACCESS_TOKEN_THRESHOLD_SECS = 3 * 3600
# 失败退避冷却
RT_EXCHANGE_FAIL_COOLDOWN_SECS = 15 * 60
PASSWORD_RELOGIN_FAIL_COOLDOWN_SECS = 30 * 60
COOKIE_FAIL_COOLDOWN_SECS = 15 * 60


class SteamAuthManager:
    def __init__(
        self,
        auth: SteamAuthClient,
        get_cred: Callable[[str], str],
        set_cred: Callable[[str, str], None],
        cookie_refresh_fn: Callable[[str], Awaitable[str | None]] | None = None,
        notify_fn: Callable[[str], Awaitable[None]] | None = None,
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

        self._rt_fail_cooldown_until: float = 0.0
        self._relogin_fail_cooldown_until: float = 0.0
        self._cookie_fail_cooldown_until: float = 0.0
        self.last_keepalive_note: str = ""
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
        """路线一可用性：账号 + 密码 + shared_secret 齐备（共享密钥缺省时启动登录会失败并给出明确提示）。"""
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

    # ------------------------------------------------------------------
    # 状态描述（供 WebUI / 指令展示）
    # ------------------------------------------------------------------
    def describe_mode(self) -> str:
        if self.has_password_login and self.has_shared_secret:
            return "🛡️ 2FA秘钥全自动重登 (ASF同款·永不掉线)"
        if self.has_password_login:
            return "⚠️ 密码已填但缺 shared_secret，重登需人工过验证"
        if self.refresh_token:
            return "🔄 Refresh Token 滚动续期 (无限续杯)"
        if self._get_cred("cookie"):
            return "🍪 Cookie 静默续签"
        return "❓ 未配置任何保活方式"

    def refresh_token_days_left(self) -> float:
        """Refresh Token 剩余寿命（天）。Steam 令牌初始寿命约 200 天。"""
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
        用 refresh_token 兑换全新 access_token；renewal_type=1 时 Steam 会在
        剩余寿命不足时下发全新 200 天 refresh_token，这里负责立刻持久化。
        返回 True 表示成功拿到了可用的 access_token。
        """
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
        用 refresh_token 兑换全新 access_token；renewal_type=1 时 Steam 会在
        剩余寿命不足时下发全新 200 天 refresh_token，这里负责立刻持久化。
        返回 True 表示成功拿到了可用的 access_token。
        """
        refresh_token = self.refresh_token
        if not refresh_token:
            return False
        if time.time() < self._rt_fail_cooldown_until and not force_new:
            return False
        try:
            result = await self.auth.refresh_tokens(
                refresh_token, steamid=self.steam_id, renewal=True
            )
        except SteamLoginError as e:
            logger.warning(f"Refresh Token 续期失败: {e}")
            self._rt_fail_cooldown_until = time.time() + RT_EXCHANGE_FAIL_COOLDOWN_SECS
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

        # Steam 下发了顺延 200 天的新 refresh_token -> 立刻覆盖保存（无限续杯的关键！）
        # 注意顺序：无论如何先持久化新 RT，之后再判断 access_token 是否可用，
        # 避免因 AT 缺失抛错/失败路径把 200 天新 RT 丢掉。
        if result.get("refresh_token"):
            self._set_cred("refresh_token", result["refresh_token"])
            logger.info(
                "Steam 已下发全新 Refresh Token (寿命重置约 200 天)，已自动持久化。滚动续期成功。"
            )
        if result.get("steamid") and not self.steam_id:
            self._set_cred("steam_id", result["steamid"])
        access_token = result.get("access_token", "") or ""
        if not access_token:
            # 边缘情况：Steam 只重置了 refresh_token 寿命但未返回 access_token。
            # 新 RT 已保住，下一轮 ensure_valid_access_token 会用新 RT 重新兑换。
            self.last_keepalive_note = (
                "Steam 未返回可用 Access Token（新 Refresh Token 已持久化，稍后自动重试兑换）"
            )
            logger.warning(self.last_keepalive_note)
            return False
        self._set_cred("access_token", access_token)
        self._rt_fail_cooldown_until = 0.0
        self.last_keepalive_note = f"滚动续期成功 (Access Token 剩余 {self.access_token_hours_left()}h)"
        return True

    # ------------------------------------------------------------------
    # 路线一：账号密码 + shared_secret 全自动重登
    # ------------------------------------------------------------------
    async def relogin_with_password(self) -> str:
        """
        账号密码 + shared_secret 走完整登录流程重置一切令牌（互斥：与扫码/续期会话串行）。
        返回面向用户的结果文案。
        """
        if self._ops_busy:
            return "⏳ 已有登录 / 续期会话正在进行（如二维码扫码确认等待中），请稍后再试"
        self._ops_busy = True
        try:
            return await self._relogin_with_password_impl()
        finally:
            self._ops_busy = False

    async def _relogin_with_password_impl(self) -> str:
        """账号密码 + shared_secret 完整重登链路的实际执行体（由互斥壳调用）。"""
        account_name = self._get_cred("account_name") or ""
        password = self._get_cred("password") or ""
        shared_secret = self._get_cred("shared_secret") or ""
        if not account_name or not password:
            return (
                "⚠️ 未启用路线一保活：请先在插件配置中填写「Steam 账号名」和「登录密码」"
                "（建议连同 shared_secret 一并填写以实现全自动过锁）"
            )
        if time.time() < self._relogin_fail_cooldown_until:
            remaining = int(self._relogin_fail_cooldown_until - time.time())
            return f"⏳ 上次自动重登失败处于退避冷却中，请 {remaining // 60} 分钟后再试"

        try:
            tokens = await self.auth.login_with_password(
                account_name, password, shared_secret=shared_secret
            )
        except SteamLoginError as e:
            self._relogin_fail_cooldown_until = (
                time.time() + PASSWORD_RELOGIN_FAIL_COOLDOWN_SECS
            )
            self.last_keepalive_note = f"密码重登失败: {e}"
            logger.warning(f"Password relogin failed: {e}")
            return f"❌ 账号密码自动重登失败：{e}"

        self._set_cred("access_token", tokens["access_token"])
        self._set_cred("refresh_token", tokens["refresh_token"])
        if tokens.get("account_name") and not account_name:
            self._set_cred("account_name", tokens["account_name"])
        if tokens.get("steamid") and not self.steam_id:
            self._set_cred("steam_id", tokens["steamid"])
        self._relogin_fail_cooldown_until = 0.0
        self._rt_fail_cooldown_until = 0.0
        self.last_keepalive_note = "路线一重登成功，已换发全新 200 天凭据"
        days = self.refresh_token_days_left()
        logger.info("Password relogin succeeded; fresh refresh_token persisted.")
        return (
            f"✅ 全自动密码重登成功！已获取全新 Refresh Token (寿命约 {days} 天)，"
            "整条保活链路已重置。"
        )

    # ------------------------------------------------------------------
    # 兜底：旧版 Cookie 静默刷新
    # ------------------------------------------------------------------
    async def refresh_via_cookie(self) -> bool:
        cookie = self._get_cred("cookie") or ""
        if not cookie or not self._cookie_refresh_fn:
            return False
        if time.time() < self._cookie_fail_cooldown_until:
            return False
        try:
            token = await self._cookie_refresh_fn(cookie)
        except Exception as e:
            logger.debug(f"Cookie refresh error: {e}")
            token = None
        if token:
            self._set_cred("access_token", token)
            self._cookie_fail_cooldown_until = 0.0
            self.last_keepalive_note = "Cookie 静默刷新成功"
            return True
        self._cookie_fail_cooldown_until = time.time() + COOKIE_FAIL_COOLDOWN_SECS
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
        3. 路线一: 账号密码 + shared_secret 全自动重登
        4. 兜底: Cookie 静默刷新
        返回 True 表示当前 access_token 可用。
        """
        access_token = self.access_token
        info = decode_steam_jwt(access_token or "")

        if access_token and info and not info.get("is_expired") and not force:
            remaining = int(info.get("remaining_seconds", 0))
            if remaining > min_remaining_secs:
                return True
            self.last_keepalive_note = f"Access Token 仅剩 {info.get('remaining_hours')}h，开始自动续期..."
            logger.info(self.last_keepalive_note)

        if not access_token:
            self.last_keepalive_note = "Access Token 缺失，尝试自动获取..."

        # 1) 路线二: refresh_token 滚动续期（最常用、零风险）
        if self.refresh_token and await self.exchange_refresh_token(force_new=force):
            return True

        # 2) 路线一: 账号密码 + shared_secret 全自动重登（ASF 同款兜底）
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
        # 密码未配置或重登失败时继续降级

        # 3) 兜底: Cookie 静默刷新
        if await self.refresh_via_cookie():
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
        **路线二扫码登录**：伪装手机 Steam App 发起扫码会话，
        把二维码（图片优先、链接兜底）发给用户，轮询直到手机确认。
        成功后持久化 refresh_token (200 天) / access_token，滚动续期即刻生效。
        互斥：会话存续期间（最长 timeout_seconds）阻塞其他登录/续期链路。
        """
        if self._ops_busy:
            return "⏳ 已有登录 / 续期会话正在进行（如密码重登流程中），请稍后再试"
        self._ops_busy = True
        try:
            return await self._run_qr_login_impl(
                send_text=send_text, send_image=send_image, timeout_seconds=timeout_seconds
            )
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
            return "❌ 创建 Steam 扫码会话失败，请稍后重试。"

        if send_text:
            await send_text(
                "📱 请使用手机 Steam App 扫描二维码并确认登录（3 分钟内有效）：\n"
                f"{challenge_url}\n\n"
                "扫码确认后，本插件将拿到与手机 App 同级的 200 天令牌，"
                "并自动开启滚动续期（无限续杯）。"
            )
        if send_image:
            qr_png = await self.auth.fetch_qr_image(challenge_url)
            if qr_png:
                await send_image(qr_png)
            else:
                await send_text("⚠️ 二维码图片渲染失败，请直接复制上方链接到浏览器打开后再扫码。")

        async def on_challenge_rotated(new_url: str) -> None:
            """
            二维码被 Steam 轮换时的回调：重新渲染并推送新二维码与链接（send_* None 安全）。
            过期二维码必须重发，否则用户扫旧码永远完成不了确认。
            """
            if send_text:
                await send_text(
                    f"♻️ 二维码已被 Steam 轮换更新，旧码作废，请扫描新的二维码：\n{new_url}"
                )
            if send_image:
                qr_png = await self.auth.fetch_qr_image(new_url)
                if qr_png:
                    await send_image(qr_png)
                else:
                    await send_text(
                        "⚠️ 新二维码图片渲染失败，请直接复制上方链接到浏览器打开后再扫码。"
                    )

        try:
            tokens = await self.auth.wait_for_session_tokens(
                client_id,
                request_id,
                poll_interval=poll_interval,
                timeout=timeout_seconds,
                challenge_url=challenge_url,
                on_challenge_rotated=on_challenge_rotated,
            )
        except SteamLoginError as e:
            return f"❌ 扫码登录未完成：{e}"

        self._set_cred("access_token", tokens["access_token"])
        self._set_cred("refresh_token", tokens["refresh_token"])
        if tokens.get("account_name"):
            self._set_cred("account_name", tokens["account_name"])
        if tokens.get("steamid") and not self.steam_id:
            self._set_cred("steam_id", tokens["steamid"])
        self._rt_fail_cooldown_until = 0.0
        self._relogin_fail_cooldown_until = 0.0

        days = self.refresh_token_days_left()
        steam_id = self.steam_id
        totp_hint = ""
        if not self.has_shared_secret:
            totp_hint = (
                "\n💡 提示：若希望达到 100% 永不掉线（连 Steam 强制踢下线都能自愈），"
                "可在配置中补填 shared_secret（2FA 秘钥），启用账号密码全自动重登兜底。"
            )
        return (
            f"✅ Steam 扫码登录成功！\n"
            f"👤 账号: {tokens.get('account_name') or steam_id or '未知'}\n"
            f"🎫 Refresh Token 寿命: {days} 天 (滚动续期已激活，之后无需再扫码)\n"
            f"⏰ Access Token 已自动兑换，正在刷新家庭库数据...{totp_hint}"
        )
