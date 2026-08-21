import asyncio
import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


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


class SteamFamilyClient:
    def __init__(self, proxy: str = "", timeout: int = 20):
        self.proxy = proxy
        self.timeout = timeout
        self._client = httpx.AsyncClient(
            proxy=self.proxy or None,
            timeout=self.timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
        )
        self._app_details_cache: dict[int, AppDetails] = {}

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

            rt_time_acquired = item.get("rt_time_acquired", 0)
            shared_apps.append(
                SharedApp(
                    appid=appid,
                    name=name,
                    owner_steamids=owner_steamids,
                    exclude_reason=exclude_reason,
                    app_type=app_type,
                    rt_time_acquired=rt_time_acquired,
                )
            )
        return shared_apps

    async def get_app_details(self, appid: int, basic_only: bool = False) -> AppDetails | None:
        if appid in self._app_details_cache:
            return self._app_details_cache[appid]

        url = "https://store.steampowered.com/api/appdetails"
        params = {"appids": appid, "cc": "cn", "l": "schinese"}
        if basic_only:
            params["filters"] = "price_overview,basic,genres"
        try:
            resp = await self._client.get(url, params=params, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                app_info = data.get(str(appid), {})
                if app_info.get("success"):
                    app_data = app_info.get("data", {})
                    details = self._parse_store_details(appid, app_data)
                    self._app_details_cache[appid] = details
                    return details
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
