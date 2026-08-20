import asyncio
import logging
from typing import Any
import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

class SteamAuthExpiredError(Exception):
    """Raised when Steam access token is invalid or expired."""
    pass

class SteamRateLimitError(Exception):
    """Raised when Steam rate limits the requests (HTTP 429)."""
    pass

class FamilyMember(BaseModel):
    steamid: str
    persona_name: str = ""
    avatar_url: str = ""
    role: int = 0  # 1: Adult, 2: Child

class SharedApp(BaseModel):
    appid: int
    name: str
    owner_steamids: list[str] = Field(default_factory=list)
    exclude_reason: int | None = None
    app_type: int | None = None
    is_free: bool = False

class AppDetails(BaseModel):
    appid: int
    name: str
    chinese_name: str | None = None
    header_image: str = ""
    is_free: bool = False
    short_description: str = ""
    initial_price: float = 0.0
    final_price: float = 0.0
    discount_percent: int = 0
    currency: str = "CNY"
    genres: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    positive_rate: int = 0  # 0-100 percent
    review_summary: str = "特别好评"

class SteamFamilyClient:
    def __init__(self, proxy: str = "", timeout: int = 20):
        self.proxy = proxy
        self.timeout = timeout
        self._app_details_cache: dict[int, AppDetails] = {}

    def _get_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            proxy=self.proxy if self.proxy else None,
            timeout=self.timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
            }
        )

    def parse_shared_apps(self, raw_json: dict[str, Any], filter_non_games: bool = True) -> list[SharedApp]:
        response = raw_json.get("response", {})
        raw_apps = response.get("apps", [])
        results: list[SharedApp] = []
        for item in raw_apps:
            appid = item.get("appid")
            name = item.get("name", f"App {appid}")
            owners = [str(o) for o in item.get("owner_steamids", [])]
            exclude_reason = item.get("exclude_reason")
            app_type = item.get("app_type")
            is_free = item.get("is_free", False)

            if filter_non_games:
                if exclude_reason is not None and exclude_reason != 0:
                    continue
                # exclude dedicated servers, tools, sound tracks, demos if flagged
                non_game_keywords = ["Dedicated Server", " Soundtrack", " DLC", " Demo", " Server", " Benchmark"]
                if is_free and any(kw in name for kw in non_game_keywords):
                    continue

            results.append(SharedApp(
                appid=appid,
                name=name,
                owner_steamids=owners,
                exclude_reason=exclude_reason,
                app_type=app_type,
                is_free=is_free
            ))
        return results

    async def get_family_group_id(self, access_token: str, steam_id: str) -> str | None:
        url = "https://api.steampowered.com/IFamilyGroupsService/GetFamilyGroupForUser/v1/"
        params = {"access_token": access_token, "steamid": steam_id}
        
        for attempt in range(3):
            try:
                async with self._get_client() as client:
                    resp = await client.get(url, params=params)
                    if resp.status_code == 401:
                        raise SteamAuthExpiredError("Steam Access Token 已失效")
                    if resp.status_code == 429:
                        if attempt < 2:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        raise SteamRateLimitError("Steam API 请求受限 (429)")
                    resp.raise_for_status()
                    data = resp.json()
                    group_id = data.get("response", {}).get("family_groupid")
                    return str(group_id) if group_id else None
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.NetworkError) as e:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise e
        return None

    async def get_shared_library(self, family_group_id: str, access_token: str, filter_non_games: bool = True) -> list[SharedApp]:
        url = "https://api.steampowered.com/IFamilyGroupsService/GetSharedLibraryApps/v1/"
        params = {
            "access_token": access_token,
            "family_groupid": family_group_id,
            "include_non_shared": "true"
        }
        
        for attempt in range(3):
            try:
                async with self._get_client() as client:
                    resp = await client.get(url, params=params)
                    if resp.status_code == 401:
                        raise SteamAuthExpiredError("Steam Access Token 已失效")
                    if resp.status_code == 429:
                        if attempt < 2:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        raise SteamRateLimitError("Steam API 请求受限 (429)")
                    resp.raise_for_status()
                    data = resp.json()
                    return self.parse_shared_apps(data, filter_non_games=filter_non_games)
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.NetworkError) as e:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise e
        return []

    async def get_app_details(self, appid: int) -> AppDetails | None:
        if appid in self._app_details_cache:
            return self._app_details_cache[appid]

        url = f"https://store.steampowered.com/api/appdetails?appids={appid}&l=schinese&cc=cn"
        async with self._get_client() as client:
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return None
                data = resp.json().get(str(appid), {})
                if not data.get("success"):
                    return None
                info = data.get("data", {})
                price_overview = info.get("price_overview", {})
                initial_price = price_overview.get("initial", 0) / 100.0
                final_price = price_overview.get("final", 0) / 100.0
                discount_percent = price_overview.get("discount_percent", 0)

                genres = [g.get("description", "") for g in info.get("genres", [])]
                categories = [c.get("description", "") for c in info.get("categories", [])]

                details = AppDetails(
                    appid=appid,
                    name=info.get("name", f"App {appid}"),
                    chinese_name=info.get("name"),
                    header_image=info.get("header_image", f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg"),
                    is_free=info.get("is_free", False),
                    short_description=info.get("short_description", ""),
                    initial_price=initial_price,
                    final_price=final_price,
                    discount_percent=discount_percent,
                    genres=genres,
                    categories=categories,
                    positive_rate=88,
                    review_summary="特别好评"
                )
                self._app_details_cache[appid] = details
                return details
            except Exception as e:
                logger.warning(f"Failed to fetch store details for app {appid}: {e}")
                return None
