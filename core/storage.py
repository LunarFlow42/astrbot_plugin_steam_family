import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

try:
    from .steam_api import AppDetails, SharedApp
except (ImportError, ValueError):
    from core.steam_api import AppDetails, SharedApp

logger = logging.getLogger(__name__)


class FamilyStorage:
    def __init__(self, db_path: str = "data/steam_family.db"):
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def _get_conn(self):
        return aiosqlite.connect(self.db_path)

    async def init_db(self):
        async with self._get_conn() as db:
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
        async with self._get_conn() as db:
            await db.execute(
                "INSERT OR IGNORE INTO subscriptions (target_id) VALUES (?)",
                (target_id,),
            )
            await db.commit()

    async def unsubscribe(self, target_id: str):
        async with self._get_conn() as db:
            await db.execute("DELETE FROM subscriptions WHERE target_id = ?", (target_id,))
            await db.commit()

    async def get_subscribers(self) -> list[str]:
        async with self._get_conn() as db:
            async with db.execute("SELECT target_id FROM subscriptions") as cursor:
                rows = await cursor.fetchall()
                return [r[0] for r in rows]

    async def save_snapshot(self, apps: list[SharedApp]) -> int:
        data = [app.model_dump() for app in apps]
        raw_json = json.dumps(data, ensure_ascii=False)
        async with self._get_conn() as db:
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
                        (app.appid, str(owner)),
                    )
            await db.commit()
            return snapshot_id or 0

    async def get_latest_snapshot(self) -> dict[int, SharedApp] | None:
        async with (
            self._get_conn() as db,
            db.execute("SELECT raw_data FROM library_snapshots ORDER BY id DESC LIMIT 1") as cursor,
        ):
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
        async with self._get_conn() as db:
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
                    json.dumps(details.genres, ensure_ascii=False),
                    json.dumps(details.categories, ensure_ascii=False),
                    details.positive_rate,
                    details.review_summary,
                ),
            )
            await db.commit()

    async def get_cached_app(self, appid: int) -> AppDetails | None:
        async with (
            self._get_conn() as db,
            db.execute("SELECT * FROM apps_cache WHERE appid = ?", (appid,)) as cursor,
        ):
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

    async def get_all_cached_apps(self) -> list[AppDetails]:
        async with self._get_conn() as db:
            async with db.execute("SELECT * FROM apps_cache") as cursor:
                rows = await cursor.fetchall()
                return [
                    AppDetails(
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
                    for row in rows
                ]

    async def get_family_rankings(self) -> list[dict[str, Any]]:
        async with self._get_conn() as db:
            sql = """
                SELECT
                    o.owner_steamid,
                    COUNT(DISTINCT o.appid) as game_count,
                    COALESCE(SUM(c.initial_price), 0) as total_initial,
                    COALESCE(SUM(c.final_price), 0) as total_final
                FROM app_ownership o
                LEFT JOIN apps_cache c ON o.appid = c.appid
                GROUP BY o.owner_steamid
                ORDER BY total_initial DESC, game_count DESC
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
        async with self._get_conn() as db:
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

    async def get_dashboard_data(self) -> dict[str, Any]:
        snapshot = await self.get_latest_snapshot()
        rankings = await self.get_family_rankings()
        cached_apps = await self.get_all_cached_apps()

        total_initial = sum(r["total_initial_price"] for r in rankings)
        total_final = sum(r["total_current_price"] for r in rankings)

        recent_games = []
        if cached_apps:
            recent_games = [
                {
                    "appid": a.appid,
                    "name": a.chinese_name or a.name,
                    "header_image": a.header_image,
                    "final_price": a.final_price,
                    "positive_rate": a.positive_rate,
                    "genres": a.genres,
                }
                for a in cached_apps[:12]
            ]
        elif snapshot:
            # Fallback when details are still caching
            recent_games = [
                {
                    "appid": app.appid,
                    "name": app.name,
                    "header_image": f"https://cdn.akamai.steamstatic.com/steam/apps/{app.appid}/header.jpg",
                    "final_price": 0.0,
                    "positive_rate": 95,
                    "genres": ["Steam 共享"],
                }
                for app in list(snapshot.values())[:12]
            ]

        return {
            "total_games": len(snapshot) if snapshot else 0,
            "total_initial_value": round(total_initial, 2),
            "total_current_value": round(total_final, 2),
            "member_count": len(rankings),
            "rankings": rankings,
            "recent_games": recent_games,
        }
