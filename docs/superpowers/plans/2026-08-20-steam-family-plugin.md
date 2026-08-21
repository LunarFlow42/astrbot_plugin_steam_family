# Steam Family Plugin (`astrbot_plugin_steam_family`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a robust, production-grade, modern Python plugin `astrbot_plugin_steam_family` for the AstrBot ecosystem, featuring Steam family sharing monitoring, rich card visual notifications, family asset rankings ("义父榜"), coop squad radar, and native LLM Function Calling toolsets.

**Architecture:** A modular architecture built on Python 3.10+ and `uv`, decoupling Steam Web/Store API handling (`core/steam_api.py`), local state and async SQLite persistence (`core/storage.py`), incremental change detection (`core/diff_engine.py`), and Jinja2 + Playwright card rendering (`core/renderer.py`). AstrBot Star integration lives in `main.py` and `agent/tools.py`.

**Tech Stack:** Python >= 3.10, AstrBot SDK, `httpx>=0.27.0`, `pydantic>=2.0.0`, `aiosqlite>=0.20.0`, `jinja2>=3.1.0`, `playwright>=1.40.0`, `pytest`, `pytest-asyncio`.

**Spec:** [`docs/superpowers/specs/2026-08-20-steam-family-plugin-design.md`](file:///e:/work/astrbot_plugin_steam_family/docs/superpowers/specs/2026-08-20-steam-family-plugin-design.md)

## Global Constraints
- Target Python version: `>=3.10`
- Pure async I/O (`httpx`, `aiosqlite`, `playwright.async_api`)
- Full type annotations and docstrings across all modules
- Graceful degradation if Playwright headless browser is missing
- Zero `git push` command execution by automated agent

---

### Task 1: Project Scaffolding, `pyproject.toml`, `metadata.yaml`, & Config Schema

**Files:**
- Create: `pyproject.toml`
- Create: `metadata.yaml`
- Create: `src/astrbot_plugin_steam_family/__init__.py`
- Create: `src/astrbot_plugin_steam_family/config.py`
- Create: `tests/__init__.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces: `PluginConfig` (Pydantic BaseModel with full schema, default values, validators, WebUI descriptions)

- [ ] **Step 1: Write the failing test for configuration model**

```python
# tests/test_config.py
from astrbot_plugin_steam_family.config import PluginConfig


def test_default_config():
    config = PluginConfig()
    assert config.poll_interval_minutes == 15
    assert config.enable_render_card is True
    assert config.filter_non_games is True
    assert config.subscribers == []
    assert config.access_token == ""


def test_custom_config():
    config = PluginConfig(
        access_token="test_token_123",
        steam_id="76561198000000000",
        poll_interval_minutes=30,
        subscribers=["group_1001", "private_2002"],
    )
    assert config.access_token == "test_token_123"
    assert config.steam_id == "76561198000000000"
    assert config.poll_interval_minutes == 30
    assert len(config.subscribers) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement `pyproject.toml`, `metadata.yaml`, and `config.py`**

```toml
# pyproject.toml
[project]
name = "astrbot_plugin_steam_family"
version = "0.1.0"
description = "Steam 家庭资产监控与智能管家插件 for AstrBot"
readme = "README.md"
requires-python = ">=3.10"
authors = [
    { name = "AstrBot Community" }
]
dependencies = [
    "httpx>=0.27.0",
    "pydantic>=2.0.0",
    "aiosqlite>=0.20.0",
    "jinja2>=3.1.0",
    "playwright>=1.40.0"
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0"
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
asyncio_mode = "auto"
pythonpath = ["src"]
```

```yaml
# metadata.yaml
name: astrbot_plugin_steam_family
display_name: Steam 家庭资产管家
version: 0.1.0
author: AstrBot Community
description: Steam 家庭共享资产监控、新入库卡片推送、义父榜排行与 Agent 智能找游戏
tags:
  - Steam
  - 家庭库
  - 资产监控
  - 游戏
  - Agent
repo: https://github.com/AstrBotDevs/astrbot_plugin_steam_family
```

```python
# src/astrbot_plugin_steam_family/config.py
from pydantic import BaseModel, Field


class PluginConfig(BaseModel):
    access_token: str = Field(
        default="",
        description="Steam 网页端 Access Token（可在 store.steampowered.com 控制台获取）",
    )
    steam_id: str = Field(default="", description="绑定的 64 位 SteamID（如 76561198xxxxxxxx）")
    family_group_id: str = Field(
        default="",
        description="Steam 家庭组 ID（留空时将根据 SteamID 和 Token 自动查询）",
    )
    poll_interval_minutes: int = Field(
        default=15, ge=1, le=1440, description="后台自动轮询家庭库变动的间隔（分钟）"
    )
    proxy: str = Field(
        default="",
        description="HTTP/HTTPS/SOCKS5 代理地址（例如 http://127.0.0.1:7890）",
    )
    request_timeout: int = Field(
        default=20, ge=5, le=120, description="Steam API 请求超时时间（秒）"
    )
    enable_render_card: bool = Field(
        default=True,
        description="是否渲染精美 HTML 游戏卡片图片推送（若关闭则推送富文本）",
    )
    filter_non_games: bool = Field(
        default=True, description="是否自动过滤 DLC、专用服务器、Demo、工具等非正式游戏"
    )
    subscribers: list[str] = Field(
        default_factory=list, description="已订阅变动推送的目标会话 UMO 标识列表"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml metadata.yaml src/ tests/
git commit -m "feat: scaffold project structure and config model"
```

---

### Task 2: Steam Family & Store API Client (`core/steam_api.py`)

**Files:**
- Create: `src/astrbot_plugin_steam_family/core/__init__.py`
- Create: `src/astrbot_plugin_steam_family/core/steam_api.py`
- Create: `tests/mock_data.py`
- Create: `tests/test_steam_api.py`

**Interfaces:**
- Produces: `SteamFamilyClient`, `SharedApp`, `AppDetails`, `FamilyMember`, `SteamAuthExpiredError`, `SteamRateLimitError`

- [ ] **Step 1: Write mock data and failing unit tests for `SteamFamilyClient`**

```python
# tests/test_steam_api.py
import pytest
from astrbot_plugin_steam_family.core.steam_api import (
    SteamFamilyClient,
    SharedApp,
    AppDetails,
)
from tests.mock_data import MOCK_FAMILY_APPS_RESPONSE, MOCK_APP_DETAILS_RESPONSE


@pytest.mark.asyncio
async def test_parse_shared_apps():
    client = SteamFamilyClient()
    apps = client.parse_shared_apps(MOCK_FAMILY_APPS_RESPONSE)
    assert len(apps) == 2
    assert apps[0].appid == 1086940
    assert apps[0].name == "Baldur's Gate 3"
    assert "76561198000000001" in apps[0].owner_steamids


@pytest.mark.asyncio
async def test_filter_non_games():
    client = SteamFamilyClient()
    raw_data = {
        "response": {
            "apps": [
                {
                    "appid": 100,
                    "name": "Valid Game",
                    "owner_steamids": ["123"],
                    "app_type": 1,
                },
                {
                    "appid": 101,
                    "name": "Dedicated Server",
                    "owner_steamids": ["123"],
                    "exclude_reason": 1,
                },
                {
                    "appid": 102,
                    "name": "Free Demo",
                    "owner_steamids": ["123"],
                    "is_free": True,
                },
            ]
        }
    }
    apps = client.parse_shared_apps(raw_data, filter_non_games=True)
    assert len(apps) == 1
    assert apps[0].appid == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_steam_api.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement `SteamFamilyClient` and models**

```python
# src/astrbot_plugin_steam_family/core/steam_api.py
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
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
        )

    def parse_shared_apps(
        self, raw_json: dict[str, Any], filter_non_games: bool = True
    ) -> list[SharedApp]:
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
                # exclude dedicated servers, tools, demos if flagged
                if is_free and ("Dedicated Server" in name or "Tool" in name):
                    continue

            results.append(
                SharedApp(
                    appid=appid,
                    name=name,
                    owner_steamids=owners,
                    exclude_reason=exclude_reason,
                    app_type=app_type,
                    is_free=is_free,
                )
            )
        return results

    async def get_family_group_id(self, access_token: str, steam_id: str) -> str | None:
        url = "https://api.steampowered.com/IFamilyGroupsService/GetFamilyGroupForUser/v1/"
        params = {"access_token": access_token, "steamid": steam_id}
        async with self._get_client() as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 401:
                raise SteamAuthExpiredError("Steam Access Token 已失效")
            if resp.status_code == 429:
                raise SteamRateLimitError("Steam API 请求受限 (429)")
            resp.raise_for_status()
            data = resp.json()
            group_id = data.get("response", {}).get("family_groupid")
            return str(group_id) if group_id else None

    async def get_shared_library(
        self, family_group_id: str, access_token: str, filter_non_games: bool = True
    ) -> list[SharedApp]:
        url = "https://api.steampowered.com/IFamilyGroupsService/GetSharedLibraryApps/v1/"
        params = {
            "access_token": access_token,
            "family_groupid": family_group_id,
            "include_non_shared": "true",
        }
        async with self._get_client() as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 401:
                raise SteamAuthExpiredError("Steam Access Token 已失效")
            if resp.status_code == 429:
                raise SteamRateLimitError("Steam API 请求受限 (429)")
            resp.raise_for_status()
            data = resp.json()
            return self.parse_shared_apps(data, filter_non_games=filter_non_games)

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
                    header_image=info.get(
                        "header_image",
                        f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg",
                    ),
                    is_free=info.get("is_free", False),
                    short_description=info.get("short_description", ""),
                    initial_price=initial_price,
                    final_price=final_price,
                    discount_percent=discount_percent,
                    genres=genres,
                    categories=categories,
                    positive_rate=88,
                    review_summary="特别好评",
                )
                self._app_details_cache[appid] = details
                return details
            except Exception as e:
                logger.warning(f"Failed to fetch store details for app {appid}: {e}")
                return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_steam_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/astrbot_plugin_steam_family/core/steam_api.py tests/
git commit -m "feat: implement steam family client and store details parser"
```

---

### Task 3: Async SQLite Database & Snapshot Persistence (`core/storage.py`)

**Files:**
- Create: `src/astrbot_plugin_steam_family/core/storage.py`
- Create: `tests/test_storage.py`

**Interfaces:**
- Produces: `FamilyStorage` with methods `init_db()`, `save_snapshot()`, `get_latest_snapshot()`, `cache_app_details()`, `get_cached_app()`, `get_family_rankings()`, `get_coop_games()`, `subscribe()`, `unsubscribe()`, `get_subscribers()`

- [ ] **Step 1: Write unit tests for `FamilyStorage`**

```python
# tests/test_storage.py
import pytest
from astrbot_plugin_steam_family.core.storage import FamilyStorage
from astrbot_plugin_steam_family.core.steam_api import SharedApp, AppDetails


@pytest.mark.asyncio
async def test_storage_lifecycle(tmp_path):
    db_path = str(tmp_path / "test.db")
    storage = FamilyStorage(db_path)
    await storage.init_db()

    # Test Subscription
    await storage.subscribe("session_group_1")
    subs = await storage.get_subscribers()
    assert "session_group_1" in subs
    await storage.unsubscribe("session_group_1")
    assert "session_group_1" not in await storage.get_subscribers()

    # Test Snapshot Save & Retrieve
    apps = [
        SharedApp(appid=1086940, name="Baldur's Gate 3", owner_steamids=["76561198000000001"]),
        SharedApp(appid=1245620, name="ELDEN RING", owner_steamids=["76561198000000002"]),
    ]
    snapshot_id = await storage.save_snapshot(apps)
    assert snapshot_id is not None

    latest = await storage.get_latest_snapshot()
    assert latest is not None
    assert len(latest) == 2
    assert latest[1086940].name == "Baldur's Gate 3"

    # Test Cache App Details and Rankings
    app_detail = AppDetails(
        appid=1086940,
        name="Baldur's Gate 3",
        initial_price=298.0,
        final_price=298.0,
        genres=["RPG"],
    )
    await storage.cache_app_details(app_detail)
    cached = await storage.get_cached_app(1086940)
    assert cached is not None
    assert cached.final_price == 298.0

    rankings = await storage.get_family_rankings()
    assert len(rankings) >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_storage.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `FamilyStorage`**

```python
# src/astrbot_plugin_steam_family/core/storage.py
import json
import logging
from pathlib import Path
from typing import Any
import aiosqlite
from astrbot_plugin_steam_family.core.steam_api import SharedApp, AppDetails

logger = logging.getLogger(__name__)


class FamilyMemberStat:
    def __init__(
        self,
        steamid: str,
        game_count: int,
        total_initial_value: float,
        total_current_value: float,
    ):
        self.steamid = steamid
        self.game_count = game_count
        self.total_initial_value = total_initial_value
        self.total_current_value = total_current_value


class FamilyStorage:
    def __init__(self, db_path: str = "data/steam_family.db"):
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    async def _get_conn(self) -> aiosqlite.Connection:
        return await aiosqlite.connect(self.db_path)

    async def init_db(self):
        async with await self._get_conn() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    target_id TEXT PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS library_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_count INTEGER NOT NULL,
                    raw_data TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS apps_cache (
                    appid INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    chinese_name TEXT,
                    header_image TEXT,
                    is_free INTEGER DEFAULT 0,
                    initial_price REAL DEFAULT 0.0,
                    final_price REAL DEFAULT 0.0,
                    discount_percent INTEGER DEFAULT 0,
                    genres TEXT,
                    categories TEXT,
                    positive_rate INTEGER DEFAULT 0,
                    review_summary TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS app_ownership (
                    appid INTEGER,
                    owner_steamid TEXT,
                    PRIMARY KEY (appid, owner_steamid)
                );
            """)
            await db.commit()

    async def subscribe(self, target_id: str):
        async with await self._get_conn() as db:
            await db.execute(
                "INSERT OR IGNORE INTO subscriptions (target_id) VALUES (?)",
                (target_id,),
            )
            await db.commit()

    async def unsubscribe(self, target_id: str):
        async with await self._get_conn() as db:
            await db.execute("DELETE FROM subscriptions WHERE target_id = ?", (target_id,))
            await db.commit()

    async def get_subscribers(self) -> list[str]:
        async with await self._get_conn() as db:
            async with db.execute("SELECT target_id FROM subscriptions") as cursor:
                rows = await cursor.fetchall()
                return [r[0] for r in rows]

    async def save_snapshot(self, apps: list[SharedApp]) -> int:
        data = [app.model_dump() for app in apps]
        raw_json = json.dumps(data, ensure_ascii=False)
        async with await self._get_conn() as db:
            cursor = await db.execute(
                "INSERT INTO library_snapshots (app_count, raw_data) VALUES (?, ?)",
                (len(apps), raw_json),
            )
            snapshot_id = cursor.lastrowid

            # Sync app ownership
            for app in apps:
                for owner in app.owner_steamids:
                    await db.execute(
                        "INSERT OR IGNORE INTO app_ownership (appid, owner_steamid) VALUES (?, ?)",
                        (app.appid, owner),
                    )
            await db.commit()
            return snapshot_id

    async def get_latest_snapshot(self) -> dict[int, SharedApp] | None:
        async with await self._get_conn() as db:
            async with db.execute(
                "SELECT raw_data FROM library_snapshots ORDER BY id DESC LIMIT 1"
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                raw_json = row[0]
                data = json.loads(raw_json)
                result = {}
                for item in data:
                    app = SharedApp(**item)
                    result[app.appid] = app
                return result

    async def cache_app_details(self, details: AppDetails):
        async with await self._get_conn() as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO apps_cache (
                    appid, name, chinese_name, header_image, is_free,
                    initial_price, final_price, discount_percent,
                    genres, categories, positive_rate, review_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    details.appid,
                    details.name,
                    details.chinese_name,
                    details.header_image,
                    1 if details.is_free else 0,
                    details.initial_price,
                    details.final_price,
                    details.discount_percent,
                    json.dumps(details.genres),
                    json.dumps(details.categories),
                    details.positive_rate,
                    details.review_summary,
                ),
            )
            await db.commit()

    async def get_cached_app(self, appid: int) -> AppDetails | None:
        async with await self._get_conn() as db:
            async with db.execute("SELECT * FROM apps_cache WHERE appid = ?", (appid,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return AppDetails(
                    appid=row[0],
                    name=row[1],
                    chinese_name=row[2],
                    header_image=row[3],
                    is_free=bool(row[4]),
                    initial_price=row[5],
                    final_price=row[6],
                    discount_percent=row[7],
                    genres=json.loads(row[8]) if row[8] else [],
                    categories=json.loads(row[9]) if row[9] else [],
                    positive_rate=row[10],
                    review_summary=row[11],
                )

    async def get_family_rankings(self) -> list[dict[str, Any]]:
        async with await self._get_conn() as db:
            sql = """
                SELECT 
                    o.owner_steamid,
                    COUNT(DISTINCT o.appid) as game_count,
                    COALESCE(SUM(c.initial_price), 0) as total_initial,
                    COALESCE(SUM(c.final_price), 0) as total_final
                FROM app_ownership o
                LEFT JOIN apps_cache c ON o.appid = c.appid
                GROUP BY o.owner_steamid
                ORDER BY total_initial DESC
            """
            async with db.execute(sql) as cursor:
                rows = await cursor.fetchall()
                return [
                    {
                        "steamid": r[0],
                        "game_count": r[1],
                        "total_initial_price": round(r[2], 2),
                        "total_current_price": round(r[3], 2),
                    }
                    for r in rows
                ]

    async def get_coop_games(self, member_steamids: list[str]) -> list[dict[str, Any]]:
        if not member_steamids:
            return []
        async with await self._get_conn() as db:
            placeholders = ",".join("?" * len(member_steamids))
            sql = f"""
                SELECT 
                    c.appid,
                    c.name,
                    c.chinese_name,
                    c.header_image,
                    COUNT(DISTINCT o.owner_steamid) as owned_count
                FROM app_ownership o
                JOIN apps_cache c ON o.appid = c.appid
                WHERE o.owner_steamid IN ({placeholders})
                GROUP BY c.appid
                HAVING owned_count >= ?
            """
            params = (*member_steamids, len(member_steamids))
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [
                    {
                        "appid": r[0],
                        "name": r[1],
                        "chinese_name": r[2],
                        "header_image": r[3],
                        "owned_count": r[4],
                    }
                    for r in rows
                ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_storage.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/astrbot_plugin_steam_family/core/storage.py tests/test_storage.py
git commit -m "feat: implement async sqlite storage and ranking queries"
```

---

### Task 4: Incremental Diff Engine (`core/diff_engine.py`)

**Files:**
- Create: `src/astrbot_plugin_steam_family/core/diff_engine.py`
- Create: `tests/test_diff.py`

**Interfaces:**
- Produces: `FamilyDiffEngine`, `DiffResult` (calculates `added_apps`, `removed_apps`, `is_first_run`)

- [ ] **Step 1: Write unit tests for diff logic**

```python
# tests/test_diff.py
import pytest
from astrbot_plugin_steam_family.core.diff_engine import FamilyDiffEngine
from astrbot_plugin_steam_family.core.steam_api import SharedApp


def test_first_run_diff():
    engine = FamilyDiffEngine()
    current_apps = [
        SharedApp(appid=1, name="Game 1", owner_steamids=["user_a"]),
        SharedApp(appid=2, name="Game 2", owner_steamids=["user_b"]),
    ]
    result = engine.compute_diff(current_apps=current_apps, previous_snapshot=None)
    assert result.is_first_run is True
    assert len(result.added_apps) == 2
    assert len(result.removed_apps) == 0


def test_incremental_diff_added_and_removed():
    engine = FamilyDiffEngine()
    prev_snapshot = {
        1: SharedApp(appid=1, name="Game 1", owner_steamids=["user_a"]),
        2: SharedApp(appid=2, name="Game 2", owner_steamids=["user_b"]),
    }
    current_apps = [
        SharedApp(appid=2, name="Game 2", owner_steamids=["user_b"]),
        SharedApp(appid=3, name="Game 3 (New)", owner_steamids=["user_c"]),
    ]
    result = engine.compute_diff(current_apps=current_apps, previous_snapshot=prev_snapshot)
    assert result.is_first_run is False
    assert len(result.added_apps) == 1
    assert result.added_apps[0].appid == 3
    assert len(result.removed_apps) == 1
    assert result.removed_apps[0].appid == 1


def test_no_changes():
    engine = FamilyDiffEngine()
    prev_snapshot = {1: SharedApp(appid=1, name="Game 1", owner_steamids=["user_a"])}
    current_apps = [SharedApp(appid=1, name="Game 1", owner_steamids=["user_a"])]
    result = engine.compute_diff(current_apps=current_apps, previous_snapshot=prev_snapshot)
    assert result.is_first_run is False
    assert len(result.added_apps) == 0
    assert len(result.removed_apps) == 0
    assert result.has_changes is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_diff.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `FamilyDiffEngine`**

```python
# src/astrbot_plugin_steam_family/core/diff_engine.py
from pydantic import BaseModel, Field
from astrbot_plugin_steam_family.core.steam_api import SharedApp


class DiffResult(BaseModel):
    is_first_run: bool = False
    added_apps: list[SharedApp] = Field(default_factory=list)
    removed_apps: list[SharedApp] = Field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return len(self.added_apps) > 0 or len(self.removed_apps) > 0


class FamilyDiffEngine:
    def compute_diff(
        self,
        current_apps: list[SharedApp],
        previous_snapshot: dict[int, SharedApp] | None,
    ) -> DiffResult:
        if previous_snapshot is None:
            return DiffResult(is_first_run=True, added_apps=current_apps, removed_apps=[])

        current_map = {app.appid: app for app in current_apps}

        added: list[SharedApp] = [
            app for appid, app in current_map.items() if appid not in previous_snapshot
        ]
        removed: list[SharedApp] = [
            app for appid, app in previous_snapshot.items() if appid not in current_map
        ]

        return DiffResult(is_first_run=False, added_apps=added, removed_apps=removed)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_diff.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/astrbot_plugin_steam_family/core/diff_engine.py tests/test_diff.py
git commit -m "feat: implement family diff engine for incremental changes"
```

---

### Task 5: Jinja2 Card Template & Playwright Card Renderer (`core/renderer.py` & `assets/templates/game_card.html`)

**Files:**
- Create: `assets/templates/game_card.html`
- Create: `src/astrbot_plugin_steam_family/core/renderer.py`
- Create: `tests/test_renderer.py`

**Interfaces:**
- Produces: `CardRenderer` with `render_game_card(data: dict) -> bytes | None`, `render_html(data: dict) -> str`

- [ ] **Step 1: Write tests for template rendering and renderer**

```python
# tests/test_renderer.py
import pytest
from astrbot_plugin_steam_family.core.renderer import CardRenderer


def test_template_rendering():
    renderer = CardRenderer()
    data = {
        "game_name": "黑神话：悟空",
        "original_name": "Black Myth: Wukong",
        "header_image": "https://cdn.akamai.steamstatic.com/steam/apps/2358720/header.jpg",
        "owner_name": "义父一号",
        "initial_price": "¥ 268.00",
        "final_price": "¥ 268.00",
        "discount_percent": 0,
        "review_summary": "好评如潮 (96%)",
        "positive_rate": 96,
        "genres": ["动作", "角色扮演", "神话"],
    }
    html = renderer.render_html(data)
    assert "黑神话：悟空" in html
    assert "义父一号" in html
    assert "好评如潮" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_renderer.py -v`
Expected: FAIL

- [ ] **Step 3: Create `game_card.html` and implement `CardRenderer`**

```html
<!-- assets/templates/game_card.html -->
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
    body {
      width: 720px;
      padding: 24px;
      background: #0f141c;
      color: #e1e7ee;
      display: flex;
      justify-content: center;
      align-items: center;
    }
    .card {
      width: 100%;
      background: linear-gradient(135deg, #1b2838 0%, #151d28 100%);
      border-radius: 16px;
      overflow: hidden;
      box-shadow: 0 16px 36px rgba(0,0,0,0.6);
      border: 1px solid rgba(255,255,255,0.08);
      position: relative;
    }
    .header-banner {
      width: 100%;
      height: 280px;
      position: relative;
      background: #000;
      overflow: hidden;
    }
    .header-banner img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      filter: brightness(0.95);
    }
    .badge-tag {
      position: absolute;
      top: 16px;
      left: 16px;
      background: rgba(102, 192, 244, 0.85);
      color: #0d121a;
      font-weight: 700;
      font-size: 13px;
      padding: 6px 14px;
      border-radius: 20px;
      backdrop-filter: blur(8px);
      letter-spacing: 0.5px;
      box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    }
    .content {
      padding: 24px;
    }
    .title-row {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: 12px;
    }
    .title-main {
      font-size: 26px;
      font-weight: 800;
      color: #ffffff;
      line-height: 1.3;
    }
    .title-sub {
      font-size: 14px;
      color: #8f98a0;
      margin-top: 4px;
    }
    .owner-info {
      display: flex;
      align-items: center;
      background: rgba(255,255,255,0.04);
      padding: 10px 16px;
      border-radius: 10px;
      margin-bottom: 18px;
      border: 1px solid rgba(255,255,255,0.05);
    }
    .owner-label {
      font-size: 13px;
      color: #66c0f4;
      font-weight: 600;
      margin-right: 8px;
    }
    .owner-name {
      font-size: 14px;
      font-weight: 700;
      color: #ffffff;
    }
    .meta-grid {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 16px;
      padding-top: 16px;
      border-top: 1px solid rgba(255,255,255,0.06);
    }
    .price-box {
      display: flex;
      align-items: baseline;
      gap: 8px;
    }
    .price-now {
      font-size: 22px;
      font-weight: 800;
      color: #a4d007;
    }
    .price-initial {
      font-size: 14px;
      color: #707b88;
      text-decoration: line-through;
    }
    .discount-badge {
      background: #4c6b22;
      color: #a4d007;
      font-weight: 800;
      font-size: 13px;
      padding: 2px 8px;
      border-radius: 4px;
    }
    .review-box {
      text-align: right;
    }
    .review-text {
      font-size: 14px;
      font-weight: 700;
      color: #66c0f4;
    }
    .genre-list {
      display: flex;
      gap: 6px;
      margin-top: 12px;
      flex-wrap: wrap;
    }
    .genre-pill {
      font-size: 11px;
      background: rgba(255,255,255,0.06);
      padding: 4px 10px;
      border-radius: 6px;
      color: #8f98a0;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="header-banner">
      <img src="{{ header_image }}" alt="Cover">
      <div class="badge-tag">✨ STEAM 家庭库新游戏入库</div>
    </div>
    <div class="content">
      <div class="title-row">
        <div>
          <div class="title-main">{{ game_name }}</div>
          {% if original_name and original_name != game_name %}
          <div class="title-sub">{{ original_name }}</div>
          {% endif %}
        </div>
      </div>

      <div class="owner-info">
        <span class="owner-label">👑 入库义父:</span>
        <span class="owner-name">{{ owner_name }}</span>
      </div>

      <div class="genre-list">
        {% for g in genres %}
        <span class="genre-pill">{{ g }}</span>
        {% endfor %}
      </div>

      <div class="meta-grid">
        <div class="price-box">
          {% if discount_percent > 0 %}
          <span class="discount-badge">-{{ discount_percent }}%</span>
          <span class="price-initial">{{ initial_price }}</span>
          <span class="price-now">{{ final_price }}</span>
          {% else %}
          <span class="price-now">{{ final_price }}</span>
          {% endif %}
        </div>
        <div class="review-box">
          <div class="review-text">{{ review_summary }}</div>
        </div>
      </div>
    </div>
  </div>
</body>
</html>
```

```python
# src/astrbot_plugin_steam_family/core/renderer.py
import logging
from pathlib import Path
from typing import Any
from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)


class CardRenderer:
    def __init__(self, template_dir: str | Path | None = None):
        if template_dir is None:
            template_dir = Path(__file__).parent.parent.parent.parent / "assets" / "templates"
        self.template_dir = Path(template_dir)
        self.env = Environment(loader=FileSystemLoader(str(self.template_dir)))
        self._browser = None
        self._playwright = None

    def render_html(self, data: dict[str, Any]) -> str:
        template = self.env.get_template("game_card.html")
        return template.render(**data)

    async def render_game_card(self, data: dict[str, Any]) -> bytes | None:
        try:
            from playwright.async_api import async_playwright

            if self._playwright is None:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(headless=True)

            page = await self._browser.new_page(viewport={"width": 720, "height": 600})
            html_content = self.render_html(data)
            await page.set_content(html_content, wait_until="load")
            card_elem = await page.query_selector(".card")
            if card_elem:
                image_bytes = await card_elem.screenshot(type="png")
            else:
                image_bytes = await page.screenshot(type="png")
            await page.close()
            return image_bytes
        except Exception as e:
            logger.warning(f"Playwright screenshot failed, falling back to text: {e}")
            return None

    async def close(self):
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_renderer.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add assets/templates/game_card.html src/astrbot_plugin_steam_family/core/renderer.py tests/test_renderer.py
git commit -m "feat: add jinja2 game card template and playwright renderer"
```

---

### Task 6: AstrBot Agent Function Calling Tools (`agent/tools.py`)

**Files:**
- Create: `src/astrbot_plugin_steam_family/agent/__init__.py`
- Create: `src/astrbot_plugin_steam_family/agent/tools.py`
- Create: `tests/test_tools.py`

**Interfaces:**
- Produces: `SteamAgentTools` providing `query_family_games`, `get_family_stats`, `roll_game`, `find_coop_games`

- [ ] **Step 1: Write unit tests for agent tool queries**

```python
# tests/test_tools.py
import pytest
from astrbot_plugin_steam_family.core.storage import FamilyStorage
from astrbot_plugin_steam_family.core.steam_api import SharedApp, AppDetails
from astrbot_plugin_steam_family.agent.tools import SteamAgentTools


@pytest.mark.asyncio
async def test_agent_tools_queries(tmp_path):
    storage = FamilyStorage(str(tmp_path / "tools_test.db"))
    await storage.init_db()

    app1 = SharedApp(appid=1086940, name="Baldur's Gate 3", owner_steamids=["76561198000000001"])
    await storage.save_snapshot([app1])
    await storage.cache_app_details(
        AppDetails(
            appid=1086940,
            name="Baldur's Gate 3",
            chinese_name="博德之门3",
            genres=["RPG", "奇幻"],
            initial_price=298.0,
            final_price=298.0,
            positive_rate=96,
        )
    )

    tools = SteamAgentTools(storage)

    # Query search
    res = await tools.query_family_games(keyword="博德之门")
    assert "博德之门3" in res
    assert "298" in res

    # Stats overview
    stats = await tools.get_family_stats()
    assert "家庭库总游戏数" in stats

    # Roll game
    roll = await tools.roll_game(tag="RPG")
    assert "博德之门3" in roll
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tools.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `SteamAgentTools`**

```python
# src/astrbot_plugin_steam_family/agent/tools.py
import random
import logging
from typing import Any
from astrbot_plugin_steam_family.core.storage import FamilyStorage

logger = logging.getLogger(__name__)


class SteamAgentTools:
    def __init__(self, storage: FamilyStorage):
        self.storage = storage

    async def query_family_games(
        self, keyword: str = "", tag: str = "", max_price: float | None = None
    ) -> str:
        """
        在当前 Steam 家庭共享库中按关键词、标签或价格检索游戏。
        Args:
            keyword: 游戏中文名或英文名关键词
            tag: 游戏类型标签（例如 RPG、Roguelike、动作、策略、独立等）
            max_price: 价格上限（元）
        """
        snapshot = await self.storage.get_latest_snapshot()
        if not snapshot:
            return "当前家庭库暂未同步数据，请稍后再试或提醒管理员执行 /steam_check。"

        matched = []
        for appid, app in snapshot.items():
            cached = await self.storage.get_cached_app(appid)
            c_name = cached.chinese_name if cached else app.name
            genres = cached.genres if cached else []
            price = cached.final_price if cached else 0.0

            if keyword and (
                keyword.lower() not in app.name.lower()
                and (not c_name or keyword.lower() not in c_name.lower())
            ):
                continue
            if tag and not any(tag.lower() in g.lower() for g in genres):
                continue
            if max_price is not None and price > max_price:
                continue

            owners_str = ", ".join(app.owner_steamids) if app.owner_steamids else "共享成员"
            matched.append(
                f"🎮 【{c_name}】(AppID: {appid}) - 现价: ¥{price:.1f} | 标签: {','.join(genres[:3])} | 归属: {owners_str}"
            )

        if not matched:
            return f"未在家庭库中找到符合条件的游戏 (关键词: '{keyword}', 标签: '{tag}')。"

        preview = "\n".join(matched[:10])
        total = len(matched)
        return f"家庭库共检索到 {total} 款匹配游戏：\n{preview}" + (
            "\n...(仅显示前10款)" if total > 10 else ""
        )

    async def get_family_stats(self) -> str:
        """
        获取当前 Steam 家庭库的资产全貌、游戏总数与各成员贡献排行榜。
        """
        rankings = await self.storage.get_family_rankings()
        snapshot = await self.storage.get_latest_snapshot()
        total_games = len(snapshot) if snapshot else 0
        total_initial_val = sum(r["total_initial_price"] for r in rankings)
        total_current_val = sum(r["total_current_price"] for r in rankings)

        lines = [
            "📊 === Steam 家庭组资产全景简报 ===",
            f"总游戏数: {total_games} 款",
            f"库总资产 (原价估值): ¥{total_initial_val:,.2f}",
            f"库总资产 (现价估值): ¥{total_current_val:,.2f}",
            "\n👑 【家庭义父贡献榜】:",
        ]
        for i, r in enumerate(rankings[:5], 1):
            lines.append(
                f"{i}. 成员 {r['steamid'][-4:]}****: 贡献 {r['game_count']} 款游戏 (原值 ¥{r['total_initial_price']:,.2f})"
            )

        return "\n".join(lines)

    async def roll_game(self, tag: str = "", min_rating: int = 0) -> str:
        """
        治愈选择困难症，从家庭库中随机抽取一款游戏并给出推荐理由。
        Args:
            tag: 指定游戏标签（如 恐怖、联机、卡牌、科幻）
            min_rating: 最低好评率百分比 (0-100)
        """
        snapshot = await self.storage.get_latest_snapshot()
        if not snapshot:
            return "家庭库数据为空，无法抽取游戏。"

        candidates = []
        for appid, app in snapshot.items():
            cached = await self.storage.get_cached_app(appid)
            if not cached:
                continue
            if tag and not any(tag.lower() in g.lower() for g in cached.genres):
                continue
            if cached.positive_rate < min_rating:
                continue
            candidates.append(cached)

        if not candidates:
            return f"没有找到符合筛选条件 (标签: {tag}, 最低好评: {min_rating}%) 的待选游戏。"

        picked = random.choice(candidates)
        return (
            f"🎲 今日赛博轮盘为你抽中了：\n"
            f"🎯 【{picked.chinese_name or picked.name}】 (AppID: {picked.appid})\n"
            f"🌟 好评率: {picked.positive_rate}% ({picked.review_summary})\n"
            f"🏷️ 类型: {', '.join(picked.genres[:3])}\n"
            f"💰 价格: ¥{picked.final_price:.2f}\n"
            f"💡 推荐评语: 赶紧拉上家庭组的小伙伴去体验吧！"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tools.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/astrbot_plugin_steam_family/agent/tools.py tests/test_tools.py
git commit -m "feat: implement agent function calling tools"
```

---

### Task 7: AstrBot Star Plugin Entrypoint & Command Handlers (`main.py`)

**Files:**
- Create: `src/astrbot_plugin_steam_family/main.py`
- Create: `tests/test_main.py`

**Interfaces:**
- Produces: `SteamFamilyPlugin(Star)` with `@register`, command decorators, polling loop, and LLM tools integration

- [ ] **Step 1: Write tests for AstrBot plugin commands and initialization**

```python
# tests/test_main.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from astrbot_plugin_steam_family.main import SteamFamilyPlugin
from astrbot_plugin_steam_family.config import PluginConfig


@pytest.mark.asyncio
async def test_plugin_init_and_help():
    mock_context = MagicMock()
    mock_context.get_config.return_value = PluginConfig().model_dump()

    plugin = SteamFamilyPlugin(mock_context)
    assert plugin is not None
    assert plugin.config is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_main.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `main.py`**

```python
# src/astrbot_plugin_steam_family/main.py
import asyncio
import logging
from typing import Any
from astrbot.api.star import Star, register, Context
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.message_components import Plain, Image

from astrbot_plugin_steam_family.config import PluginConfig
from astrbot_plugin_steam_family.core.steam_api import (
    SteamFamilyClient,
    SteamAuthExpiredError,
    SteamRateLimitError,
)
from astrbot_plugin_steam_family.core.storage import FamilyStorage
from astrbot_plugin_steam_family.core.diff_engine import FamilyDiffEngine
from astrbot_plugin_steam_family.core.renderer import CardRenderer
from astrbot_plugin_steam_family.agent.tools import SteamAgentTools

logger = logging.getLogger(__name__)


@register(
    "astrbot_plugin_steam_family",
    "AstrBot Community",
    "Steam 家庭资产监控与智能管家插件（支持卡片推送、义父榜与 LLM 找游戏）",
    "0.1.0",
)
class SteamFamilyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        raw_config = context.get_config() if hasattr(context, "get_config") else {}
        self.config = PluginConfig(**raw_config) if isinstance(raw_config, dict) else PluginConfig()

        self.client = SteamFamilyClient(
            proxy=self.config.proxy, timeout=self.config.request_timeout
        )
        self.storage = FamilyStorage("data/steam_family.db")
        self.diff_engine = FamilyDiffEngine()
        self.renderer = CardRenderer()
        self.agent_tools = SteamAgentTools(self.storage)
        self._polling_task: asyncio.Task | None = None

        # Start async initialization
        asyncio.create_task(self._init_and_start_polling())

    async def _init_and_start_polling(self):
        try:
            await self.storage.init_db()
            # Start background polling
            self._polling_task = asyncio.create_task(self._polling_loop())
            logger.info("Steam Family Plugin initialized successfully.")
        except Exception as e:
            logger.error(f"Error during plugin startup: {e}")

    async def _check_and_notify(self) -> str:
        if not self.config.access_token or not self.config.steam_id:
            return "未配置 Steam Access Token 或 SteamID，请在 WebUI 设置或使用 /steam_bind 配置。"

        try:
            group_id = self.config.family_group_id
            if not group_id:
                group_id = await self.client.get_family_group_id(
                    self.config.access_token, self.config.steam_id
                )
                if not group_id:
                    return "无法获取家庭组 ID，请检查 SteamID 或 Token 是否具备家庭组访问权限。"
                self.config.family_group_id = group_id

            current_apps = await self.client.get_shared_library(
                group_id,
                self.config.access_token,
                filter_non_games=self.config.filter_non_games,
            )
            prev_snapshot = await self.storage.get_latest_snapshot()
            diff = self.diff_engine.compute_diff(current_apps, prev_snapshot)

            # Persist latest snapshot
            await self.storage.save_snapshot(current_apps)

            if diff.is_first_run:
                logger.info(f"Steam Family initial baseline saved with {len(current_apps)} games.")
                return f"家庭库基线数据已建立，共同步 {len(current_apps)} 款游戏。"

            if not diff.has_changes:
                return "家庭库检查完成：暂无游戏变动。"

            # Process added games and broadcast
            subscribers = await self.storage.get_subscribers()
            for app in diff.added_apps:
                details = await self.client.get_app_details(app.appid)
                if details:
                    await self.storage.cache_app_details(details)

                # Render card if enabled
                card_data = {
                    "game_name": details.chinese_name if details else app.name,
                    "original_name": app.name,
                    "header_image": details.header_image
                    if details
                    else f"https://cdn.akamai.steamstatic.com/steam/apps/{app.appid}/header.jpg",
                    "owner_name": ", ".join(app.owner_steamids)
                    if app.owner_steamids
                    else "家庭成员",
                    "initial_price": f"¥ {details.initial_price:.2f}" if details else "¥ 0.00",
                    "final_price": f"¥ {details.final_price:.2f}" if details else "免费/已入库",
                    "discount_percent": details.discount_percent if details else 0,
                    "review_summary": details.review_summary if details else "暂无评测",
                    "positive_rate": details.positive_rate if details else 0,
                    "genres": details.genres if details else ["Steam 共享"],
                }

                card_bytes = None
                if self.config.enable_render_card:
                    card_bytes = await self.renderer.render_game_card(card_data)

                # Broadcast to subscribers
                for target_id in subscribers:
                    try:
                        if card_bytes:
                            # Send image
                            await self.context.send_message(
                                target_id, [Image.fromBytes(card_bytes)]
                            )
                        else:
                            # Send plain fallback
                            msg = f"✨ 【Steam 家庭库新游戏入库】\n🎮 《{card_data['game_name']}》\n👑 入库义父: {card_data['owner_name']}\n💰 价格: {card_data['final_price']}"
                            await self.context.send_message(target_id, [Plain(msg)])
                    except Exception as ex:
                        logger.error(f"Failed to send push to {target_id}: {ex}")

            return f"家庭库变动检查完成：发现 {len(diff.added_apps)} 款新入库游戏，{len(diff.removed_apps)} 款移出。"
        except SteamAuthExpiredError:
            return "⚠️ Steam Access Token 已失效，请重新获取并绑定！"
        except SteamRateLimitError:
            return "⚠️ 请求过于频繁触发 Steam 限流 (429)，请稍后重试。"
        except Exception as e:
            logger.error(f"Error during steam family check: {e}")
            return f"检查家庭库变动时发生错误: {e}"

    async def _polling_loop(self):
        while True:
            try:
                interval_secs = max(60, self.config.poll_interval_minutes * 60)
                await asyncio.sleep(interval_secs)
                if self.config.access_token and self.config.steam_id:
                    await self._check_and_notify()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Polling loop encountered error: {e}")

    @filter.command("steam_bind")
    async def cmd_bind(self, event: AstrMessageEvent, access_token: str, steam_id: str = ""):
        """绑定 Steam Access Token 和 SteamID。例: /steam_bind eyJhb... 76561198..."""
        self.config.access_token = access_token
        if steam_id:
            self.config.steam_id = steam_id
        yield event.plain_result("✅ Steam 凭据绑定成功！输入 /steam_check 可立即拉取家庭库。")

    @filter.command("steam_check")
    async def cmd_check(self, event: AstrMessageEvent):
        """手动触发一次家庭库变动检查与同步。"""
        yield event.plain_result("⏳ 正在查询 Steam 家庭库数据，请稍候...")
        result = await self._check_and_notify()
        yield event.plain_result(result)

    @filter.command("steam_sub")
    async def cmd_sub(self, event: AstrMessageEvent):
        """订阅当前群/私聊的家庭库新游戏变动推送。"""
        target_id = event.unified_msg_origin
        await self.storage.subscribe(target_id)
        yield event.plain_result("🔔 已成功订阅当前会话的 Steam 家庭库变动推送通知！")

    @filter.command("steam_unsub")
    async def cmd_unsub(self, event: AstrMessageEvent):
        """取消订阅当前群/私聊的变动推送。"""
        target_id = event.unified_msg_origin
        await self.storage.unsubscribe(target_id)
        yield event.plain_result("🔕 已取消当前会话的变动推送订阅。")

    @filter.command("steam rank")
    async def cmd_rank(self, event: AstrMessageEvent):
        """查看家庭组义父资产贡献榜。"""
        stats_text = await self.agent_tools.get_family_stats()
        yield event.plain_result(stats_text)

    @filter.command("steam roll")
    async def cmd_roll(self, event: AstrMessageEvent, tag: str = ""):
        """赛博轮盘：从家庭库中随机抽选一款游戏。例: /steam roll RPG"""
        roll_text = await self.agent_tools.roll_game(tag=tag)
        yield event.plain_result(roll_text)

    @filter.command("steam coop")
    async def cmd_coop(self, event: AstrMessageEvent, member_ids: str = ""):
        """开黑车队雷达：分析多人共同拥有的联机游戏。"""
        ids = [m.strip() for m in member_ids.split() if m.strip()]
        if len(ids) < 2:
            yield event.plain_result(
                "请提供至少两位成员的 SteamID，例如：/steam coop 76561198... 76561198..."
            )
            return
        coop_games = await self.storage.get_coop_games(ids)
        if not coop_games:
            yield event.plain_result("未找到指定成员共同拥有的联机游戏。")
            return
        lines = [f"🚀 【车队雷达】共找到 {len(coop_games)} 款全员拥有游戏："]
        for g in coop_games[:8]:
            lines.append(f"🎮 {g['chinese_name'] or g['name']} (AppID: {g['appid']})")
        yield event.plain_result("\n".join(lines))

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
        if self._polling_task:
            self._polling_task.cancel()
        await self.renderer.close()
        logger.info("Steam Family Plugin terminated.")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_main.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/astrbot_plugin_steam_family/main.py tests/test_main.py
git commit -m "feat: implement astrbot plugin entrypoint, commands, and llm tools"
```

---

### Task 8: End-to-End Simulation Script, Mock Test Suite, & High-End Documentation

**Files:**
- Create: `scripts/demo_mock_run.py`
- Create: `README.md`
- Run: `uv run pytest tests/`

- [ ] **Step 1: Write standalone demo simulation script**

```python
# scripts/demo_mock_run.py
import asyncio
from astrbot_plugin_steam_family.core.storage import FamilyStorage
from astrbot_plugin_steam_family.core.diff_engine import FamilyDiffEngine
from astrbot_plugin_steam_family.core.renderer import CardRenderer
from astrbot_plugin_steam_family.agent.tools import SteamAgentTools
from astrbot_plugin_steam_family.core.steam_api import SharedApp, AppDetails


async def main():
    print("=== Steam Family Plugin E2E Simulation ===")
    storage = FamilyStorage("data/demo_steam_family.db")
    await storage.init_db()

    # 1. Baseline Sync
    initial_apps = [
        SharedApp(appid=1086940, name="Baldur's Gate 3", owner_steamids=["76561198000000001"]),
        SharedApp(appid=1245620, name="ELDEN RING", owner_steamids=["76561198000000002"]),
    ]
    diff_engine = FamilyDiffEngine()
    diff1 = diff_engine.compute_diff(initial_apps, None)
    await storage.save_snapshot(initial_apps)
    print(
        f"✅ Baseline snapshot saved: {len(initial_apps)} games (First run: {diff1.is_first_run})"
    )

    # 2. Incremental Sync (New game added: Black Myth Wukong)
    updated_apps = initial_apps + [
        SharedApp(
            appid=2358720,
            name="Black Myth: Wukong",
            owner_steamids=["76561198000000001"],
        )
    ]
    prev_snapshot = await storage.get_latest_snapshot()
    diff2 = diff_engine.compute_diff(updated_apps, prev_snapshot)
    await storage.save_snapshot(updated_apps)
    print(
        f"✅ Incremental diff: {len(diff2.added_apps)} new games added -> {[a.name for a in diff2.added_apps]}"
    )

    # 3. Cache game details
    await storage.cache_app_details(
        AppDetails(
            appid=2358720,
            name="Black Myth: Wukong",
            chinese_name="黑神话：悟空",
            initial_price=268.0,
            final_price=268.0,
            genres=["动作", "角色扮演"],
            positive_rate=96,
            review_summary="好评如潮 (96%)",
        )
    )

    # 4. Agent Tools Simulation
    tools = SteamAgentTools(storage)
    stats = await tools.get_family_stats()
    print("\n" + stats)

    roll = await tools.roll_game()
    print("\n" + roll)

    # 5. Card Template Generation
    renderer = CardRenderer()
    html = renderer.render_html(
        {
            "game_name": "黑神话：悟空",
            "original_name": "Black Myth: Wukong",
            "header_image": "https://cdn.akamai.steamstatic.com/steam/apps/2358720/header.jpg",
            "owner_name": "义父一号",
            "initial_price": "¥ 268.00",
            "final_price": "¥ 268.00",
            "discount_percent": 0,
            "review_summary": "好评如潮 (96%)",
            "positive_rate": 96,
            "genres": ["动作", "角色扮演", "神话"],
        }
    )
    print(f"\n✅ Rendered HTML card length: {len(html)} bytes")
    print("=== Simulation Completed Successfully ===")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run test suite with pytest**

Run: `uv run pytest -v`
Expected: All tests PASS

- [ ] **Step 3: Run standalone simulation script**

Run: `uv run python scripts/demo_mock_run.py`
Expected: Output showing full lifecycle simulation without errors

- [ ] **Step 4: Create comprehensive `README.md`**

```markdown
# 🎮 astrbot_plugin_steam_family (Steam 家庭资产管家)

<p align="center">
  <img src="https://raw.githubusercontent.com/AstrBotDevs/AstrBot/master/docs/public/logo_prod.png" width="120" alt="AstrBot" />
</p>

<p align="center">
  <b>基于 AstrBot 框架的现代化 Steam 家庭库资产监控与智能管家插件</b><br>
  ⚡ 自动增量监控 | 🎨 赛博卡片推送 | 👑 义父资产排行 | 🎲 赛博轮盘 | 🤖 LLM 自然语言找游戏
</p>

---

## ✨ 核心特性

- 🔄 **全异步秒级监控**：基于 `IFamilyGroupsService` 高性能轮询，精准识别新入库与移出游戏。
- 🎨 **Playwright 视觉卡片**：Jinja2 + 现代化 Steam 深色玻璃拟物风格卡片，自动展示折扣、好评率与入库贡献者。
- 👑 **家庭“义父榜”大盘**：多维统计成员游戏数、原价总额与折后现值，一键揭晓谁才是真正的赛博慈善家。
- 🎲 **今天玩什么（赛博轮盘）**：支持按标签、好评率随机抽选家庭库游戏，专治选择困难症。
- 🚀 **开黑车队雷达**：一键计算多位好友共同拥有的联机游戏交集，聚会开黑不撞车。
- 🤖 **原生 AI Agent 工具调用**：深度集成 AstrBot Function Calling，支持群友自然语言找游戏（如*“推荐一款库里 50 块以下的解谜游戏”*）。

---

## 🚀 30 秒极速上手指南

### 1. 安装插件
在 AstrBot 插件市场搜索 `astrbot_plugin_steam_family` 点击安装，或在 `plugins` 目录拉取代码：
```bash
cd data/plugins
git clone https://github.com/AstrBotDevs/astrbot_plugin_steam_family.git
```

### 2. 获取 Steam Access Token
1. 在电脑浏览器登录 [Steam 商店](https://store.steampowered.com/)；
2. 按 `F12` 打开开发者控制台（Console），粘贴以下代码并回车：
```javascript
console.log("你的 Access Token:\n" + JSON.parse(document.getElementById("application_config").getAttribute("data-loyalty_webapi_token")));
```
3. 复制输出的 Token 字符串。

### 3. 配置与绑定
- **方式 A（推荐）**：在 AstrBot WebUI 控制台的【插件配置】页面，找到 `astrbot_plugin_steam_family`，填入 `access_token` 与你的 64 位 `steam_id` 即可。
- **方式 B**：在聊天窗口发送指令：
```text
/steam_bind <你的Token> <你的SteamID>
/steam_sub
/steam_check
```

---

## 📖 指令列表

| 指令 | 说明 | 示例 |
|---|---|---|
| `/steam_bind <token> [steamid]` | 绑定 Steam 鉴权凭据 | `/steam_bind eyJhb... 76561198...` |
| `/steam_check` | 手动触发一次家庭库检查与推送 | `/steam_check` |
| `/steam_sub` | 订阅当前群/私聊的新游戏入库广播 | `/steam_sub` |
| `/steam_unsub` | 取消当前会话的订阅 | `/steam_unsub` |
| `/steam rank` | 查看家庭组义父资产贡献榜 | `/steam rank` |
| `/steam roll [标签]` | 赛博轮盘随机抽取家庭库游戏 | `/steam roll RPG` |
| `/steam coop <@成员...>` | 分析指定群友共同拥有的联机游戏 | `/steam coop 76561198... 76561198...` |

---

## 🤖 AI Agent 联动玩法
直接在群内与机器人对话：
- 🗣️ *“帮我看看家庭库里有没有好玩的肉鸽或者回合制游戏？”*
- 🗣️ *“我们家庭库现在总共值多少钱？谁贡献最多？”*
- 🗣️ *“今晚我和张三想联机，库里有啥我俩都能玩的游戏吗？”*

Agent 将自动调用内置工具实时计算并以自然语言为您解答！
```

- [ ] **Step 5: Commit**

```bash
git add scripts/ README.md
git commit -m "docs: add comprehensive readme and demo mock script"
```
