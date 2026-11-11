import asyncio
import base64
import json
import logging
import time
from typing import Any

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---- Steam 商店接口常量 ----
_APP_DETAILS_URL = "https://store.steampowered.com/api/appdetails"
_APP_REVIEWS_URL = "https://store.steampowered.com/appreviews"

# 批量 appdetails 每个 chunk 的 appid 数上限。
_APP_DETAILS_BATCH_CHUNK_SIZE = 20
# 批量请求尝试携带的字段集合。
_APP_DETAILS_BATCH_FILTERS = "price_overview,basic,genres,categories"
# 降价监控批量预览接口的 filters（实测唯一可用的多 appid 形态）。
_PRICE_BATCH_FILTERS = "price_overview"

# 实测结论（2026-10，直连 store.steampowered.com 校园网环境，无 429/424）：
#   1) appdetails 多 appid（逗号拼接）请求：不带 filters、或 filters=basic / genres /
#      header_image / 多值组合时，一律返回 HTTP 400 + body "null"，Steam 直接拒绝；
#   2) 唯一可用的批量形态是 filters=price_overview：返回多键结构
#      {"1086940": {"success": true, "data": {"price_overview": {...}}}, ...}，
#      但不含 name/genres 等字段，无法直接构造完整 AppDetails；免费游戏该形态下 data 为空数组 []；
#   3) 单 appid 请求（带不带 filters 均可）始终返回完整数据（200）。
# 因此批量获取采用"尝试批量 + 逐个兜底"双路径：先按 chunk 探测批量请求，校验每个返回行
# 是否包含可直接构成完整 AppDetails 的数据（success + 非空 dict + 有 name）；任何一行不满足
# 都走现有 get_app_details 单发兜底。并用进程内探测标记（_batch_probe_done / _batch_supported）
# 记住"本环境批量拿不到完整数据"的结论，避免每次批量调用都浪费一发注定 400 的请求；
# 若未来 Steam 开放批量完整字段，探测会自动恢复批量路径。
_REVIEW_CACHE_TTL = 6 * 60 * 60  # 好评摘要成功结果缓存 6 小时
_REVIEW_CACHE_NEG_TTL = 10 * 60  # 失败/无评价结果短期缓存 10 分钟，避免对 0 评价游戏反复打接口
_REVIEW_CACHE_MISS = object()  # 评价缓存未命中哨兵
# 429 熔断指数退避：首次冷却 60 秒，连续命中按 60 * 2^(count-1) 翻倍
# （60 → 120 → 240 → 300 封顶），避免 Steam 风控未解除期间反复以固定 60 秒
# 全速回压造成锯齿；收到商店/家庭组接口的 2xx 成功响应即归零计数，
# 下一次 429 从基础 60 秒重新起步。
_RATE_LIMIT_COOLDOWN_BASE_SECONDS = 60.0
_RATE_LIMIT_COOLDOWN_MAX_SECONDS = 300.0


def decode_steam_jwt(token: str) -> dict[str, Any] | None:
    """
    纯本地解析 Steam WebAPI JWT Access Token，提取 SteamID64 与过期时间戳（零网络开销）。
    返回结构:
      - valid: bool
      - steamid: str (sub 字段，即 64 位 SteamID)
      - exp: int (过期 UNIX 时间戳)
      - iat: int (签发 UNIX 时间戳)
      - is_expired: bool
      - remaining_seconds: int
      - remaining_hours: float
    """
    if not token or "." not in token:
        return None
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        rem = len(payload_b64) % 4
        if rem > 0:
            payload_b64 += "=" * (4 - rem)
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        data = json.loads(payload_bytes.decode("utf-8"))
        exp = int(data.get("exp", 0))
        sub = str(data.get("sub", ""))
        iat = int(data.get("iat", 0))
        now = int(time.time())
        is_expired = now >= exp
        remaining = max(0, exp - now)
        return {
            "valid": True,
            "steamid": sub,
            "exp": exp,
            "iat": iat,
            "is_expired": is_expired,
            "remaining_seconds": remaining,
            "remaining_hours": round(remaining / 3600.0, 1),
        }
    except Exception:
        return None


class SteamAuthExpiredError(Exception):
    """Raised when Steam Access Token has expired or is invalid."""

    def __init__(self, message: str, eresult: int = 0):
        super().__init__(message)
        self.eresult = eresult


class SteamRateLimitError(Exception):
    """Raised when Steam rate limit (429) is encountered."""


class FamilyMember(BaseModel):
    steamid: str
    role: int = 0
    time_joined: int = 0


class SharedApp(BaseModel):
    appid: int
    name: str = ""
    owner_steamids: list[str] = Field(default_factory=list)
    exclude_reason: int = 0
    app_type: int = 1
    rt_time_acquired: int = 0
    rt_last_played: int = 0
    rt_playtime: int = 0
    img_icon_hash: str = ""


class AppDetails(BaseModel):
    appid: int
    name: str
    chinese_name: str | None = None
    header_image: str = ""
    is_free: bool = False
    initial_price: float = 0.0
    final_price: float = 0.0
    discount_percent: int = 0
    genres: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    positive_rate: int = 0
    review_summary: str = ""


class SteamStoreRateLimiter:
    """
    Steam 商店接口主动令牌桶速率限制器。
    Steam Store API (store.steampowered.com/api/appdetails) 的官方未公开阈值为 200次 / 5分钟 (即约 40次/分钟)。
    本限制器强制请求间距 >= 1.6 秒 (约 37次/分钟)，在发包前主动拦截限速，彻底从源头杜绝触发 429 风控与防爬拦截。
    """

    def __init__(self, min_interval: float = 1.6):
        self.min_interval = min_interval
        self._last_time: float = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_time
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)
            self._last_time = asyncio.get_event_loop().time()


class SteamFamilyClient:
    def __init__(self, proxy: str = "", timeout: int = 20):
        self.proxy = proxy
        self.timeout = timeout
        self._rate_limited_until: float = 0.0
        # 连续 429 计数（指数退避用的实例状态）：命中真实 429 时 +1，
        # 收到商店/家庭组接口的 2xx 响应时归零（见 _enter_rate_limit_cooldown /
        # _reset_429_streak_on_2xx）。
        self._consecutive_429_count: int = 0
        self._store_limiter = SteamStoreRateLimiter(min_interval=1.6)
        self._client = httpx.AsyncClient(
            proxy=self.proxy or None,
            timeout=self.timeout,
            # 与认证客户端保持同一显式网络配置；不意外读取环境中的另一条代理。
            trust_env=False,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
        )
        self._app_details_cache: dict[int, AppDetails] = {}
        # 评价摘要 TTL 缓存: {appid: (写入时间戳, (positive_rate, review_desc) | None)}
        self._review_summary_cache: dict[int, tuple[float, tuple[int, str] | None]] = {}
        # 商店多 appid 批量请求可用性探测标记（见模块顶部实测结论注释）
        self._batch_probe_done: bool = False
        self._batch_supported: bool = False

    @property
    def is_rate_limited(self) -> bool:
        return time.time() < self._rate_limited_until

    @property
    def rate_limit_cooldown_remaining(self) -> int:
        remaining = int(self._rate_limited_until - time.time())
        return max(0, remaining)

    def _enter_rate_limit_cooldown(self) -> float:
        """命中真实 429：连续计数 +1，并按指数退避设置熔断冷却窗口。

        冷却时长 = 60 * 2^(count-1)，封顶 300 秒（连续序列：60 → 120 → 240 → 300）。
        返回本次设置的冷却时长（秒），供日志与错误文案展示。
        """
        self._consecutive_429_count += 1
        cooldown = min(
            _RATE_LIMIT_COOLDOWN_BASE_SECONDS * (2 ** (self._consecutive_429_count - 1)),
            _RATE_LIMIT_COOLDOWN_MAX_SECONDS,
        )
        self._rate_limited_until = time.time() + cooldown
        return cooldown

    def _reset_429_streak_on_2xx(self) -> None:
        """收到商店/家庭组接口的 2xx 成功响应：连续 429 计数归零。

        2xx 是 Steam 风控已放行的直接证据，归零后下一次 429 从基础 60 秒
        重新起步，避免恢复后残留过长的历史退避冷却。
        """
        self._consecutive_429_count = 0

    async def close(self):
        await self._client.aclose()

    async def refresh_token_via_cookie(self, cookie: str) -> str | None:
        """
        通过 Steam 网页端 Cookie (或 steamLoginSecure) 静默请求 pointssummary 接口获取最新 webapi_token。
        """
        if not cookie:
            return None
        try:
            cookie_header = (
                cookie if "steamLoginSecure" in cookie else f"steamLoginSecure={cookie.strip()}"
            )
            url = "https://store.steampowered.com/pointssummary/ajaxgetasyncconfig"
            resp = await self._client.get(url, headers={"Cookie": cookie_header})
            self._check_auth_result(resp)
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("data", {}).get("webapi_token")
                if token:
                    logger.info("Successfully auto-refreshed Steam Access Token via Cookie.")
                    return token
        except SteamAuthExpiredError:
            raise
        except Exception as e:
            logger.warning(f"Failed to auto-refresh token via cookie: {e}")
        return None

    async def get_family_group_id(self, access_token: str, steam_id: str) -> str | None:
        url = "https://api.steampowered.com/IFamilyGroupsService/GetFamilyGroupForUser/v1/"
        params = {"access_token": access_token, "steamid": steam_id}
        resp = await self._client.get(url, params=params)
        self._check_auth_result(resp)
        if resp.status_code == 401 or resp.status_code == 403:
            raise SteamAuthExpiredError("Steam Access Token expired or unauthorized.")
        if resp.status_code == 429:
            raise SteamRateLimitError("Steam API rate limit exceeded.")
        if 200 <= resp.status_code < 300:
            # 家庭组接口 2xx 成功：视为风控放行信号，连续 429 计数归零
            self._reset_429_streak_on_2xx()

        data = resp.json()
        family_groupid = data.get("response", {}).get("family_groupid")
        return str(family_groupid) if family_groupid else None

    async def get_shared_library(
        self, family_group_id: str, access_token: str, filter_non_games: bool = True
    ) -> list[SharedApp]:
        url = "https://api.steampowered.com/IFamilyGroupsService/GetSharedLibraryApps/v1/"
        params = {
            "family_groupid": family_group_id,
            "access_token": access_token,
            "include_own": "true",
            "include_excluded": "true",
            "include_non_games": "false" if filter_non_games else "true",
            "language": "schinese",
        }

        retries = 3
        delay = 2.0
        for attempt in range(retries):
            try:
                resp = await self._client.get(url, params=params)
                self._check_auth_result(resp)
                if resp.status_code in (401, 403):
                    raise SteamAuthExpiredError("Steam Access Token expired.")
                if resp.status_code == 429:
                    if attempt == retries - 1:
                        raise SteamRateLimitError("Steam rate limit exceeded (429).")
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue

                resp.raise_for_status()
                data = resp.json()
                # 家庭组接口 2xx 成功：视为风控放行信号，连续 429 计数归零
                self._reset_429_streak_on_2xx()
                return self.parse_shared_apps(data, filter_non_games=filter_non_games)
            except (httpx.RequestError, httpx.HTTPStatusError) as e:
                if attempt == retries - 1:
                    raise e
                await asyncio.sleep(delay)
                delay *= 2
        return []

    async def get_playtime_summary(
        self, family_group_id: str, access_token: str
    ) -> list[dict[str, Any]]:
        """
        获取家庭组成员在各游戏上的总游玩时长与最新游玩记录 (IFamilyGroupsService/GetPlaytimeSummary)。
        """
        url = "https://api.steampowered.com/IFamilyGroupsService/GetPlaytimeSummary/v1/"
        params = {
            "family_groupid": family_group_id,
            "access_token": access_token,
        }
        try:
            resp = await self._client.get(url, params=params)
            self._check_auth_result(resp)
            if resp.status_code in (401, 403):
                raise SteamAuthExpiredError("Steam Access Token expired.")
            if resp.status_code == 200:
                # 家庭组接口 2xx 成功：视为风控放行信号，连续 429 计数归零
                self._reset_429_streak_on_2xx()
                data = resp.json()
                return data.get("response", {}).get("entries", [])
        except SteamAuthExpiredError:
            raise
        except Exception as e:
            logger.debug(f"Failed to fetch playtime summary: {e}")
        return []

    @staticmethod
    def _check_auth_result(resp: httpx.Response) -> None:
        # 保留 Steam 明确返回的限制代码，不能一概当成普通过期后自动重登。
        try:
            eresult = int(resp.headers.get("x-eresult", 1))
        except (TypeError, ValueError):
            return
        if eresult in {5, 15, 17, 26, 27, 43, 72, 73, 82, 83, 85, 87, 105, 126}:
            raise SteamAuthExpiredError("Steam 拒绝当前授权或返回安全限制", eresult=eresult)

    async def get_player_summaries(
        self, steamids: list[str], access_token: str = ""
    ) -> dict[str, dict[str, Any]]:
        """
        获取 Steam 用户的个人公开资料（真实用户昵称 persona_name 与头像 avatar_url）。
        优先通过 IPlayerService/GetPlayerLinkDetails/v1/ 使用 Access Token 批量拉取。
        """
        if not steamids:
            return {}

        results: dict[str, dict[str, Any]] = {}
        unique_ids = list(set(str(sid) for sid in steamids if str(sid).isdigit()))
        if not unique_ids:
            return results

        # 1. Primary: IPlayerService/GetPlayerLinkDetails/v1/ with access_token
        if access_token:
            url = "https://api.steampowered.com/IPlayerService/GetPlayerLinkDetails/v1/"
            params: dict[str, Any] = {"access_token": access_token}
            for i, sid in enumerate(unique_ids):
                params[f"steamids[{i}]"] = sid
            try:
                resp = await self._client.get(url, params=params, timeout=10.0)
                if resp.status_code == 200:
                    data = resp.json()
                    accounts = data.get("response", {}).get("accounts", [])
                    for acc in accounts:
                        pub = acc.get("public_data") or acc.get("publicData") or {}
                        sid = str(pub.get("steamid", ""))
                        if not sid:
                            continue
                        name = pub.get("persona_name") or pub.get("personaName") or ""
                        avatar_raw = pub.get("sha_digest_avatar") or pub.get("shaDigestAvatar")
                        avatar_url = ""
                        if avatar_raw:
                            try:
                                if isinstance(avatar_raw, str):
                                    try:
                                        raw_bytes = base64.b64decode(avatar_raw)
                                        avatar_hex = raw_bytes.hex()
                                    except Exception:
                                        avatar_hex = avatar_raw
                                elif isinstance(avatar_raw, bytes):
                                    avatar_hex = avatar_raw.hex()
                                else:
                                    avatar_hex = ""
                                if avatar_hex:
                                    avatar_url = (
                                        f"https://avatars.steamstatic.com/{avatar_hex}_full.jpg"
                                    )
                            except Exception:
                                pass
                        results[sid] = {
                            "steamid": sid,
                            "persona_name": name,
                            "avatar_url": avatar_url,
                        }
            except Exception as e:
                logger.debug(f"Failed to query GetPlayerLinkDetails: {e}")

        # 2. Fallback for missing steamids via Steam Community Profile XML
        missing_ids = [
            sid for sid in unique_ids if sid not in results or not results[sid].get("persona_name")
        ]
        for sid in missing_ids[:6]:
            try:
                xml_url = f"https://steamcommunity.com/profiles/{sid}/?xml=1"
                resp = await self._client.get(xml_url, timeout=6.0)
                if resp.status_code == 200 and "<steamID>" in resp.text:
                    import re

                    name_match = re.search(r"<steamID><!\[CDATA\[(.*?)\]\]></steamID>", resp.text)
                    avatar_match = re.search(
                        r"<avatarFull><!\[CDATA\[(.*?)\]\]></avatarFull>", resp.text
                    )
                    name = name_match.group(1) if name_match else ""
                    avatar = avatar_match.group(1) if avatar_match else ""
                    if name:
                        results[sid] = {
                            "steamid": sid,
                            "persona_name": name,
                            "avatar_url": avatar,
                        }
            except Exception as e:
                logger.debug(f"Fallback community XML query for {sid} failed: {e}")

        return results

    def parse_shared_apps(
        self, raw_response: dict[str, Any], filter_non_games: bool = False
    ) -> list[SharedApp]:
        apps_data = raw_response.get("response", {}).get("apps", [])
        shared_apps = []
        for item in apps_data:
            appid = item.get("appid")
            name = item.get("name", "")
            exclude_reason = int(item.get("exclude_reason") if item.get("exclude_reason") is not None else item.get("excludeReason", 0))
            app_type = int(item.get("app_type") if item.get("app_type") is not None else item.get("appType", 1))
            owner_steamids = [
                str(x)
                for x in (item.get("owner_steamids") or item.get("ownerSteamids") or [])
            ]

            if not appid:
                continue

            # Filtering heuristics: 当 filter_non_games 为 True 时，严格对齐 Steam 客户端 Included Games
            if filter_non_games:
                if exclude_reason != 0:
                    continue
                lower_name = name.lower()
                non_game_keywords = [
                    "dedicated server",
                    "soundtrack",
                    "sdk",
                    "wallpaper",
                    "benchmark",
                ]
                if any(kw in lower_name for kw in non_game_keywords):
                    continue

            try:
                rt_time_acquired = int(item.get("rt_time_acquired") if item.get("rt_time_acquired") is not None else item.get("rtTimeAcquired", 0))
            except (ValueError, TypeError):
                rt_time_acquired = 0

            rt_last_played = int(item.get("rt_last_played") if item.get("rt_last_played") is not None else item.get("rtLastPlayed", 0))
            rt_playtime = int(item.get("rt_playtime") if item.get("rt_playtime") is not None else item.get("rtPlaytime", 0))
            img_icon_hash = str(item.get("img_icon_hash") or item.get("imgIconHash", ""))

            shared_apps.append(
                SharedApp(
                    appid=appid,
                    name=name,
                    owner_steamids=owner_steamids,
                    exclude_reason=exclude_reason,
                    app_type=app_type,
                    rt_time_acquired=rt_time_acquired,
                    rt_last_played=rt_last_played,
                    rt_playtime=rt_playtime,
                    img_icon_hash=img_icon_hash,
                )
            )
        return shared_apps

    async def get_app_details(
        self, appid: int, basic_only: bool = False, include_review: bool = False
    ) -> AppDetails | None:
        """获取单个游戏的商店详情。

        Args:
            appid: Steam AppID。
            basic_only: 为 True 时仅请求价格/基础/标签字段（filters 精简请求）。
            include_review: 为 True 时在详情获取成功（或缓存命中缺评价数据）后追加调用
                get_review_summary，将真实好评率与评价简述写入返回的 AppDetails；
                评价查询失败不影响主流程，返回的 AppDetails 评价字段保持缺省。

        Returns:
            AppDetails | None: 解析失败/网络错误返回 None；正处于 429 熔断冷却期时
            抛出 SteamRateLimitError（保持既有语义）。
        """
        if appid in self._app_details_cache:
            cached = self._app_details_cache[appid]
            if include_review and not cached.review_summary:
                # 缓存中的详情缺评价数据时按需补全（原地更新同时惠及内存缓存）
                review = await self.get_review_summary(appid)
                if review:
                    cached.positive_rate, cached.review_summary = review
            return cached

        if self.is_rate_limited:
            raise SteamRateLimitError(
                f"Steam 商店接口当前处于 429 速率风控冷却中（还剩 {self.rate_limit_cooldown_remaining} 秒），已自动熔断暂停向 Steam 发起请求。"
            )

        # Proactively rate limit before dispatching request to Steam Store
        await self._store_limiter.acquire()

        url = _APP_DETAILS_URL
        params = {"appids": appid, "cc": "cn", "l": "schinese"}
        if basic_only:
            params["filters"] = "price_overview,basic,genres"
        try:
            resp = await self._client.get(url, params=params, timeout=10.0)
            if resp.status_code == 429:
                # 指数退避熔断：连续 429 冷却 60 → 120 → 240 → 300（封顶）秒
                cooldown = self._enter_rate_limit_cooldown()
                logger.warning(
                    f"Steam 商店接口对 AppID {appid} 返回 429 Too Many Requests！"
                    f"已触发自动熔断，暂停请求 {cooldown:.0f} 秒。"
                )
                raise SteamRateLimitError(
                    f"Steam 商店接口触发 429 速率风控，已触发熔断保护并进入 {cooldown:.0f} 秒冷却期。"
                )

            if resp.status_code == 200:
                # 商店接口 2xx 成功：连续 429 计数归零
                self._reset_429_streak_on_2xx()
                data = resp.json()
                app_info = data.get(str(appid), {})
                if app_info.get("success"):
                    app_data = app_info.get("data", {})
                    if isinstance(app_data, dict):
                        details = self._parse_store_details(appid, app_data)
                        if include_review:
                            review = await self.get_review_summary(appid)
                            if review:
                                details.positive_rate, details.review_summary = review
                        self._app_details_cache[appid] = details
                        return details
        except SteamRateLimitError:
            raise
        except Exception as e:
            logger.debug(f"Store details query for app {appid}: {e}")
        return None

    async def get_app_details_batch(self, appids: list[int]) -> dict[int, AppDetails]:
        """批量获取多个游戏的商店详情（先去重 + 剔除缓存命中，再按 chunk 尝试批量）。

        与单个版 get_app_details 的关键语义差异（务必注意）：
          - 调用时若已处于 429 熔断冷却期（is_rate_limited 为 True），本方法**不发起任何
            网络请求也不抛错**，直接返回内存缓存命中部分（可能为空 dict）；
          - 默认路径下本方法不抛错：单个 id 抓取/解析失败只表现为结果 dict 中缺少该键。

        工作流程：
          1. 入参去重并剔除已命中 self._app_details_cache 的 id；
          2. 按 chunk（≤ _APP_DETAILS_BATCH_CHUNK_SIZE 个 id）尝试多 appid 批量请求
             （仍受 _store_limiter 限流）。实测多数环境下仅 filters=price_overview 可用、
             拿不到完整数据（见模块顶部注释），此时自动降级为逐个 get_app_details 单发兜底，
             并记住探测结论；
          3. 兜底单发期间若命中内存缓存则跳过；批量/单发请求过程中如收到真实 429 响应，
             与单发一致地设置 _rate_limited_until 熔断并 raise SteamRateLimitError；
          4. 成功结果写入内存缓存后合并返回。

        Returns:
            dict[int, AppDetails]: {appid: AppDetails}，至少包含缓存命中项。
        """
        results: dict[int, AppDetails] = {}
        unique_ids: list[int] = []
        seen: set[int] = set()
        for appid in appids:
            try:
                aid = int(appid)
            except (TypeError, ValueError):
                continue
            if aid in seen:
                continue
            seen.add(aid)
            cached = self._app_details_cache.get(aid)
            if cached is not None:
                results[aid] = cached
            else:
                unique_ids.append(aid)

        # 熔断期间只回缓存，不打网络请求（与单个版抛 SteamRateLimitError 的语义不同）
        if not unique_ids or self.is_rate_limited:
            return results

        # 1) 批量路径：仅在尚未探测过、或已探测为可用时尝试
        if not self._batch_probe_done or self._batch_supported:
            fallback_ids: list[int] = []
            for i in range(0, len(unique_ids), _APP_DETAILS_BATCH_CHUNK_SIZE):
                chunk = unique_ids[i : i + _APP_DETAILS_BATCH_CHUNK_SIZE]
                # 探测结论一旦否定，后续 chunk 不再尝试批量
                if self._batch_probe_done and not self._batch_supported:
                    fallback_ids.extend(chunk)
                    continue
                fetched = await self._fetch_app_details_chunk(chunk)
                results.update(fetched)
                # 批量响应缺失/无效的 id 交给单发兜底
                fallback_ids.extend(aid for aid in chunk if aid not in fetched)
            unique_ids = fallback_ids

        # 2) 单发兜底（其间命中内存缓存就跳过）
        for aid in unique_ids:
            if aid in self._app_details_cache:
                results[aid] = self._app_details_cache[aid]
                continue
            details = await self.get_app_details(aid)
            if details is not None:
                results[aid] = details
        return results

    async def _fetch_app_details_chunk(self, appids: list[int]) -> dict[int, AppDetails]:
        """尝试一次多 appid 批量 appdetails 请求，返回 {appid: AppDetails}（仅含解析成功项）。

        - 429：按指数退避设置熔断冷却（60 → 120 → 240 → 300 秒封顶）并 raise
          SteamRateLimitError（与单发一致）；
        - 400 / 响应非多键 dict / 网络错误：返回空 dict，由调用方对该 chunk 单发兜底；
          其中 400（Steam 结构性拒绝批量）会把进程内探测标记置为批量不可用；
        - 单行 success=False / data 为空数组 / 缺 name：视为该 id 无效，交给调用方单发兜底；
          整个 chunk 无任何有效行时同样置为批量不可用。
        """
        results: dict[int, AppDetails] = {}
        # 批量请求同样走商店限流令牌桶
        await self._store_limiter.acquire()
        params = {
            "appids": ",".join(str(aid) for aid in appids),
            "cc": "cn",
            "l": "schinese",
            "filters": _APP_DETAILS_BATCH_FILTERS,
        }
        try:
            resp = await self._client.get(_APP_DETAILS_URL, params=params, timeout=10.0)
            if resp.status_code == 429:
                # 指数退避熔断：连续 429 冷却逐次翻倍（封顶 300 秒），与单发一致
                cooldown = self._enter_rate_limit_cooldown()
                logger.warning(
                    f"Steam 商店批量接口返回 429 Too Many Requests！"
                    f"已触发自动熔断，暂停请求 {cooldown:.0f} 秒。"
                )
                raise SteamRateLimitError(
                    f"Steam 商店接口触发 429 速率风控，已触发熔断保护并进入 {cooldown:.0f} 秒冷却期。"
                )
            if resp.status_code != 200:
                # 实测：多 appid 携带不被支持的 filters 组合时返回 400 + body null
                self._mark_batch_unsupported()
                return {}
            # 批量接口 2xx 成功：连续 429 计数归零
            self._reset_429_streak_on_2xx()
            data = resp.json()
            if not isinstance(data, dict):
                self._mark_batch_unsupported()
                return {}
        except SteamRateLimitError:
            raise
        except Exception as e:
            # 网络抖动等瞬时错误：本次批量全部走单发兜底，但不改变探测结论
            logger.debug(f"Store batch details query failed ({len(appids)} ids): {e}")
            return {}

        for aid in appids:
            entry = data.get(str(aid))
            if not isinstance(entry, dict):
                continue
            app_data = entry.get("data")
            if not entry.get("success") or not isinstance(app_data, dict) or not app_data.get("name"):
                # 缺该 id / success=False / data 为空数组（免费游戏 price_overview 形态）/
                # 缺 name（无法构成完整 AppDetails）→ 该 id 无效，交给单发兜底
                continue
            details = self._parse_store_details(aid, app_data)
            self._app_details_cache[aid] = details
            results[aid] = details

        if not results:
            # 整个 chunk 都没有有效行：本环境批量拿不到完整数据，永久降级单发
            self._mark_batch_unsupported()
        return results

    def _mark_batch_unsupported(self) -> None:
        """记录进程内探测结论：商店接口多 appid 批量拿不到完整数据，后续直接单发兜底。"""
        if not self._batch_probe_done:
            logger.info(
                "Steam 商店接口多 appid 批量请求不可用（实测仅 filters=price_overview 可用且缺 name），"
                "后续自动降级为逐个单发兜底。"
            )
        self._batch_probe_done = True
        self._batch_supported = False

    async def get_price_overview_batch(
        self, appids: list[int]
    ) -> dict[int, tuple[float, float, int]]:
        """批量获取多个游戏的商店价格预览（降价 monitoring 专用轻量接口）。

        请求形态（curl 实测确认，2026-10）：
          GET https://store.steampowered.com/api/appdetails
              ?appids=105600,220&cc=cn&l=schinese&filters=price_overview
          返回 {appid: {"success": true, "data": {"price_overview": {"initial": int, "final": int,
          "discount_percent": int, ...}}}}，价格单位为"分"（initial=4200 → ¥42.00）；
          免费游戏该形态下 data 为空数组 []。

        语义约定：
          - 入参去重后按 chunk（≤ _APP_DETAILS_BATCH_CHUNK_SIZE 个 id）请求，
            每次请求前都经过 _store_limiter 令牌桶限流；
          - 入口即处于 429 熔断冷却期时，不打任何请求直接返回空 dict
            （与 get_app_details_batch 的"熔断期只回缓存"语义一致）；
          - 某个 chunk 收到真实 429 响应：设置 _rate_limited_until 熔断并
            raise SteamRateLimitError（与单发/批量详情语义一致）；
          - 某个 id 缺键 / success=False / data 非预期结构（含空数组）：
            结果 dict 中直接缺少该键，不抛错；
          - 网络抖动等瞬时异常：该 chunk 记 debug 日志后整体缺失，不抛错。

        Returns:
            dict[int, tuple[float, float, int]]: {appid: (initial_price, final_price, discount_percent)}
            （价格已 /100.0 转为元）。
        """
        results: dict[int, tuple[float, float, int]] = {}
        # 去重，保持原顺序
        seen: set[int] = set()
        unique_ids: list[int] = []
        for appid in appids:
            try:
                aid = int(appid)
            except (TypeError, ValueError):
                continue
            if aid > 0 and aid not in seen:
                seen.add(aid)
                unique_ids.append(aid)

        if not unique_ids:
            return results
        # 熔断冷却期内：静默返回空结果（不打请求、不抛错，与批量详情语义一致）
        if self.is_rate_limited:
            logger.debug("价格预览批量查询在熔断冷却期内，本轮直接跳过。")
            return results

        for i in range(0, len(unique_ids), _APP_DETAILS_BATCH_CHUNK_SIZE):
            chunk = unique_ids[i : i + _APP_DETAILS_BATCH_CHUNK_SIZE]
            # 商店接口共用令牌桶限流
            await self._store_limiter.acquire()
            params = {
                "appids": ",".join(str(aid) for aid in chunk),
                "cc": "cn",
                "l": "schinese",
                "filters": _PRICE_BATCH_FILTERS,
            }
            try:
                resp = await self._client.get(_APP_DETAILS_URL, params=params, timeout=10.0)
                if resp.status_code == 429:
                    # 指数退避熔断：连续 429 冷却逐次翻倍（封顶 300 秒），与单发/批量详情一致
                    cooldown = self._enter_rate_limit_cooldown()
                    logger.warning(
                        f"Steam 商店价格预览批量接口返回 429！"
                        f"已触发自动熔断，暂停请求 {cooldown:.0f} 秒。"
                    )
                    raise SteamRateLimitError(
                        f"Steam 商店接口触发 429 速率风控，已触发熔断保护并进入 {cooldown:.0f} 秒冷却期。"
                    )
                if resp.status_code != 200:
                    logger.debug(f"价格预览批量查询返回非 200 状态（{resp.status_code}），本 chunk 跳过。")
                    continue
                # 价格接口 2xx 成功：连续 429 计数归零
                self._reset_429_streak_on_2xx()
                data = resp.json()
                if not isinstance(data, dict):
                    logger.debug("价格预览批量查询返回非 dict 结构，本 chunk 跳过。")
                    continue
            except SteamRateLimitError:
                raise
            except Exception as e:
                # 网络抖动等瞬时错误：本 chunk 结果整体缺失，不影响调用方其余逻辑
                logger.debug(f"价格预览批量查询失败（{len(chunk)} 个 id）: {e}")
                continue

            for aid in chunk:
                entry = data.get(str(aid))
                if not isinstance(entry, dict) or not entry.get("success"):
                    continue
                app_data = entry.get("data")
                # 免费游戏 price_overview 形态下 data 为空数组 []，直接缺键
                if not isinstance(app_data, dict):
                    continue
                overview = app_data.get("price_overview")
                if not isinstance(overview, dict):
                    continue
                try:
                    initial = float(overview.get("initial", 0) or 0) / 100.0
                    final = float(overview.get("final", 0) or 0) / 100.0
                    discount = int(overview.get("discount_percent", 0) or 0)
                except (TypeError, ValueError):
                    continue
                results[aid] = (initial, final, discount)
        return results

    async def get_review_summary(self, appid: int) -> tuple[int, str] | None:
        """获取指定游戏的 Steam 用户评价摘要（真实好评率与评价简述）。

        请求: GET https://store.steampowered.com/appreviews/{appid}?json=1&language=schinese&l=schinese&purchase_type=all&num_per_page=0
        （language 与 l 同时携带：实测仅 language=schinese 时 review_score_desc 仍为英文，
        补上 l=schinese 才会返回中文评价简述）
        解析响应中的 query_summary: review_score_desc / total_reviews / total_positive，
        positive_rate = round(100 * total_positive / total_reviews)。

        Returns:
            tuple[int, str] | None: (positive_rate 0~100, review_score_desc)。
            total_reviews==0、其他解析失败、网络错误或 429 时一律返回 None（不抛错）；
            429 时同时触发指数退避熔断（连续 429 冷却 60 → 120 → 240 → 300 秒封顶，
            详见 _enter_rate_limit_cooldown）。
            成功结果缓存 6 小时；None 结果短期缓存 10 分钟，避免重复打接口。
        """
        cached = self._review_cache_lookup(appid)
        if cached is not _REVIEW_CACHE_MISS:
            return cached

        if self.is_rate_limited:
            return None

        # 评价接口同属商店域名，共用商店限流令牌桶
        await self._store_limiter.acquire()
        url = f"{_APP_REVIEWS_URL}/{appid}"
        params = {
            "json": "1",
            "language": "schinese",
            "l": "schinese",
            "purchase_type": "all",
            "num_per_page": "0",
        }
        try:
            resp = await self._client.get(url, params=params, timeout=10.0)
            if resp.status_code == 429:
                # 指数退避熔断：连续 429 冷却逐次翻倍（封顶 300 秒）
                cooldown = self._enter_rate_limit_cooldown()
                logger.warning(
                    f"Steam 评价接口对 AppID {appid} 返回 429，已触发熔断，暂停请求 {cooldown:.0f} 秒。"
                )
                return None

            if resp.status_code == 200:
                # 评价接口 2xx 成功：连续 429 计数归零
                self._reset_429_streak_on_2xx()
                try:
                    data = resp.json()
                    summary = data.get("query_summary") or {}
                    total_reviews = int(summary.get("total_reviews", 0) or 0)
                    if total_reviews > 0:
                        total_positive = int(summary.get("total_positive", 0) or 0)
                        positive_rate = round(100.0 * total_positive / total_reviews)
                        positive_rate = max(0, min(100, positive_rate))
                        review_desc = str(summary.get("review_score_desc", "") or "")
                        value: tuple[int, str] = (positive_rate, review_desc)
                        self._review_summary_cache[appid] = (time.time(), value)
                        return value
                    # 明确 0 评价
                    self._review_summary_cache[appid] = (time.time(), None)
                    return None
                except Exception:
                    pass

            # 2. 备用兜底路径：部分地区/网络或新游戏下 appreviews 接口会 302 重定向到首页，
            # 此时直接抓取商店页面 HTML 解析真实评测摘要与好评率百分比
            try:
                page_url = f"https://store.steampowered.com/app/{appid}/"
                page_resp = await self._client.get(
                    page_url,
                    params={"l": "schinese"},
                    cookies={
                        "birthtime": "568022401",
                        "wants_mature_content": "1",
                        "mature_content": "1",
                        "lastagecheckage": "1-January-1990",
                    },
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"
                        )
                    },
                    follow_redirects=True,
                    timeout=10.0,
                )
                if page_resp.status_code == 200:
                    import re

                    m = re.search(
                        r'class="game_review_summary[^>]*>([^<]+)</span>\s*\([^)]*?中的\s*(\d+)%\)',
                        page_resp.text,
                        re.DOTALL,
                    )
                    if m:
                        review_desc = m.group(1).strip()
                        positive_rate = max(0, min(100, int(m.group(2))))
                        value = (positive_rate, review_desc)
                        self._review_summary_cache[appid] = (time.time(), value)
                        return value
            except Exception as ex:
                logger.debug(f"Store page review fallback for app {appid}: {ex}")

            self._review_summary_cache[appid] = (time.time(), None)
            return None
        except Exception as e:
            logger.debug(f"Review summary query for app {appid}: {e}")
            self._review_summary_cache[appid] = (time.time(), None)
            return None

    def _review_cache_lookup(self, appid: int) -> tuple[int, str] | None | object:
        """查询评价摘要 TTL 缓存；未命中（含已过期）返回 _REVIEW_CACHE_MISS 哨兵。"""
        entry = self._review_summary_cache.get(appid)
        if entry is None:
            return _REVIEW_CACHE_MISS
        fetched_at, value = entry
        ttl = _REVIEW_CACHE_NEG_TTL if value is None else _REVIEW_CACHE_TTL
        if time.time() - fetched_at < ttl:
            return value
        self._review_summary_cache.pop(appid, None)
        return _REVIEW_CACHE_MISS

    def _parse_store_details(self, appid: int, app_data: dict[str, Any]) -> AppDetails:
        name = app_data.get("name", "")
        header_image = app_data.get("header_image", "")
        is_free = app_data.get("is_free", False)

        price_overview = app_data.get("price_overview", {})
        initial_price = price_overview.get("initial", 0) / 100.0 if price_overview else 0.0
        final_price = price_overview.get("final", 0) / 100.0 if price_overview else 0.0
        discount_percent = price_overview.get("discount_percent", 0) if price_overview else 0

        genres = [
            g.get("description", "") for g in app_data.get("genres", []) if g.get("description")
        ]
        categories = [
            c.get("description", "") for c in app_data.get("categories", []) if c.get("description")
        ]

        return AppDetails(
            appid=appid,
            name=name,
            chinese_name=name,
            header_image=header_image,
            is_free=is_free,
            initial_price=initial_price,
            final_price=final_price,
            discount_percent=discount_percent,
            genres=genres,
            categories=categories,
            # 评价字段保持中性缺省（0 / 空串）：真实好评率由 include_review=get_review_summary
            # 按需补全，不再在无评价数据时硬编码 "95 / 特别好评" 这类假数据误导用户。
            positive_rate=0,
            review_summary="",
        )
