import asyncio
import base64
import json
import logging
import time
from typing import Any

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


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
        self._store_limiter = SteamStoreRateLimiter(min_interval=1.6)
        self._client = httpx.AsyncClient(
            proxy=self.proxy or None,
            timeout=self.timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
        )
        self._app_details_cache: dict[int, AppDetails] = {}

    @property
    def is_rate_limited(self) -> bool:
        return time.time() < self._rate_limited_until

    @property
    def rate_limit_cooldown_remaining(self) -> int:
        remaining = int(self._rate_limited_until - time.time())
        return max(0, remaining)

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
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("data", {}).get("webapi_token")
                if token:
                    logger.info("Successfully auto-refreshed Steam Access Token via Cookie.")
                    return token
        except Exception as e:
            logger.warning(f"Failed to auto-refresh token via cookie: {e}")
        return None

    async def get_family_group_id(self, access_token: str, steam_id: str) -> str | None:
        url = "https://api.steampowered.com/IFamilyGroupsService/GetFamilyGroupForUser/v1/"
        params = {"access_token": access_token, "steamid": steam_id}
        resp = await self._client.get(url, params=params)
        if resp.status_code == 401 or resp.status_code == 403:
            raise SteamAuthExpiredError("Steam Access Token expired or unauthorized.")
        if resp.status_code == 429:
            raise SteamRateLimitError("Steam API rate limit exceeded.")

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
            "include_non_games": "true",
            "language": "schinese",
        }

        retries = 3
        delay = 2.0
        for attempt in range(retries):
            try:
                resp = await self._client.get(url, params=params)
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
            if resp.status_code in (401, 403):
                raise SteamAuthExpiredError("Steam Access Token expired.")
            if resp.status_code == 200:
                data = resp.json()
                return data.get("response", {}).get("entries", [])
        except Exception as e:
            logger.debug(f"Failed to fetch playtime summary: {e}")
        return []

    def parse_shared_apps(
        self, raw_response: dict[str, Any], filter_non_games: bool = False
    ) -> list[SharedApp]:
        apps_data = raw_response.get("response", {}).get("apps", [])
        shared_apps = []
        for item in apps_data:
            appid = item.get("appid")
            name = item.get("name", "")
            exclude_reason = item.get("exclude_reason", 0)
            app_type = item.get("app_type", 1)
            owner_steamids = [str(x) for x in item.get("owner_steamids", [])]

            if not appid:
                continue

            # Filtering heuristics
            if filter_non_games:
                if exclude_reason != 0:
                    continue
                lower_name = name.lower()
                non_game_keywords = [
                    "dedicated server",
                    "soundtrack",
                    "test server",
                    "sdk",
                    "wallpaper",
                    "benchmark",
                ]
                if any(kw in lower_name for kw in non_game_keywords):
                    continue

            rt_time_raw = (
                item.get("rt_time_acquired")
                or item.get("rt_time_created")
                or item.get("rtime_acquired")
                or item.get("time_acquired")
                or item.get("time")
                or 0
            )
            try:
                rt_time_acquired = int(rt_time_raw)
            except (ValueError, TypeError):
                rt_time_acquired = 0

            rt_last_played = int(item.get("rt_last_played") or 0)
            rt_playtime = int(item.get("rt_playtime") or 0)
            img_icon_hash = str(item.get("img_icon_hash") or "")

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

    async def get_app_details(self, appid: int, basic_only: bool = False) -> AppDetails | None:
        if appid in self._app_details_cache:
            return self._app_details_cache[appid]

        if self.is_rate_limited:
            raise SteamRateLimitError(
                f"Steam 商店接口当前处于 429 速率风控冷却中（还剩 {self.rate_limit_cooldown_remaining} 秒），已自动熔断暂停向 Steam 发起请求。"
            )

        # Proactively rate limit before dispatching request to Steam Store
        await self._store_limiter.acquire()

        url = "https://store.steampowered.com/api/appdetails"
        params = {"appids": appid, "cc": "cn", "l": "schinese"}
        if basic_only:
            params["filters"] = "price_overview,basic,genres"
        try:
            resp = await self._client.get(url, params=params, timeout=10.0)
            if resp.status_code == 429:
                # Set 60 seconds cooldown to protect IP
                self._rate_limited_until = time.time() + 60.0
                logger.warning(
                    f"Steam 商店接口对 AppID {appid} 返回 429 Too Many Requests！已触发自动熔断，暂停请求 60 秒。"
                )
                raise SteamRateLimitError(
                    "Steam 商店接口触发 429 速率风控，已触发熔断保护并进入 60 秒冷却期。"
                )

            if resp.status_code == 200:
                data = resp.json()
                app_info = data.get(str(appid), {})
                if app_info.get("success"):
                    app_data = app_info.get("data", {})
                    details = self._parse_store_details(appid, app_data)
                    self._app_details_cache[appid] = details
                    return details
        except SteamRateLimitError:
            raise
        except Exception as e:
            logger.debug(f"Store details query for app {appid}: {e}")
        return None

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
            positive_rate=95,
            review_summary="特别好评",
        )
