import json
import logging
import time
from pathlib import Path
from typing import Any

import aiosqlite

try:
    from .steam_api import AppDetails, SharedApp
except (ImportError, ValueError):
    from core.steam_api import AppDetails, SharedApp

logger = logging.getLogger(__name__)


class FamilyStorage:
    """
    Steam 家庭库 SQLite 异步持久化存储引擎。
    负责管理家庭库快照、应用所有权、游戏详情缓存与推送订阅。
    """

    def __init__(self, db_path: str = "data/steam_family.db"):
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # 数据目录权限收紧：库内存有凭据 KV / 浏览记录等敏感数据（POSIX 语义，
        # Windows 上 chmod 无性别语义、失败静默忽略），配合 init_db 的 0o600 文件权限
        try:
            Path(self.db_path).parent.chmod(0o700)
        except OSError:
            pass

    def _get_conn(self):
        return aiosqlite.connect(self.db_path)

    async def init_db(self):
        """初始化 SQLite 数据库表结构。"""
        async with self._get_conn() as db:
            await db.execute("PRAGMA journal_mode = WAL;")
            await db.execute("PRAGMA synchronous = NORMAL;")
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
            await db.execute("""
                CREATE TABLE IF NOT EXISTS member_profiles (
                    steamid TEXT PRIMARY KEY,
                    persona_name TEXT NOT NULL,
                    avatar_url TEXT DEFAULT '',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS credentials_kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS pending_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,            -- 'added' | 'removed' | 'owner_change' | 'price_drop'
                    dedupe_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    retries INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS push_failures (
                    target_id TEXT PRIMARY KEY,
                    fail_count INTEGER NOT NULL DEFAULT 0,
                    last_fail_at REAL
                );
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS playtime_stats (
                    steamid TEXT NOT NULL,
                    appid INTEGER NOT NULL,
                    seconds_played REAL NOT NULL DEFAULT 0,
                    rt_last_played INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (steamid, appid)
                );
            """)
            await db.commit()
        # 库文件权限收紧（首次连接后文件必定存在）：仅属主可读写，防同机其他用户/备份误收集凭据
        try:
            Path(self.db_path).chmod(0o600)
        except OSError:
            pass

    async def subscribe(self, target_id: str):
        """订阅变动推送。"""
        async with self._get_conn() as db:
            await db.execute(
                "INSERT OR IGNORE INTO subscriptions (target_id) VALUES (?)",
                (target_id,),
            )
            await db.commit()

    async def unsubscribe(self, target_id: str):
        """取消订阅变动推送。"""
        async with self._get_conn() as db:
            await db.execute("DELETE FROM subscriptions WHERE target_id = ?", (target_id,))
            await db.commit()

    async def get_subscribers(self) -> list[str]:
        """获取所有已订阅会话列表。"""
        async with (
            self._get_conn() as db,
            db.execute("SELECT target_id FROM subscriptions") as cursor,
        ):
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

    async def save_snapshot(self, apps: list[SharedApp]) -> int:
        """保存全量家庭库快照并整表重建所有权映射。

        app_ownership 在同一事务内先全量 DELETE 再插入本次快照列表，
        确保游戏被移出家庭库后旧映射随之消失（义父榜/资产总值不虚高）。
        家庭库规模小，整表重建的开销可忽略。
        """
        data = [app.model_dump() for app in apps]
        raw_json = json.dumps(data, ensure_ascii=False)
        async with self._get_conn() as db:
            cursor = await db.execute(
                "INSERT INTO library_snapshots (app_count, raw_data) VALUES (?, ?)",
                (len(apps), raw_json),
            )
            snapshot_id = cursor.lastrowid

            # 单事务内整表重建所有权映射：先清空全部旧映射，再插入本次列表
            # （此前仅 INSERT OR IGNORE 从不删除，会导致移出家庭库的游戏残留映射）
            await db.execute("DELETE FROM app_ownership")
            rows = [(app.appid, str(owner)) for app in apps for owner in app.owner_steamids]
            if rows:
                await db.executemany(
                    "INSERT OR IGNORE INTO app_ownership (appid, owner_steamid) VALUES (?, ?)",
                    rows,
                )
            await db.commit()
            return snapshot_id or 0

    async def get_latest_snapshot(self) -> dict[int, SharedApp] | None:
        """获取最新的家庭库快照字典（Key 为 AppID）。"""
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
                app = SharedApp(
                    appid=int(item.get("appid", 0)),
                    name=str(item.get("name", "")),
                    owner_steamids=[
                        str(x)
                        for x in (item.get("ownerSteamids") or item.get("owner_steamids") or [])
                    ],
                    exclude_reason=int(
                        item.get("excludeReason") or item.get("exclude_reason") or 0
                    ),
                    app_type=int(item.get("appType") or item.get("app_type") or 1),
                    rt_time_acquired=int(
                        item.get("rtTimeAcquired") or item.get("rt_time_acquired") or 0
                    ),
                    rt_last_played=int(item.get("rtLastPlayed") or item.get("rt_last_played") or 0),
                    rt_playtime=int(item.get("rtPlaytime") or item.get("rt_playtime") or 0),
                    img_icon_hash=str(item.get("imgIconHash") or item.get("img_icon_hash") or ""),
                )
                result[app.appid] = app
            return result

    async def remove_app_from_latest_snapshot(self, appid: int) -> bool:
        """
        从最新快照中移除指定 AppID（用于测试下次同步时触发新游戏入库推送）。
        """
        async with self._get_conn() as db:
            async with db.execute(
                "SELECT id, raw_data FROM library_snapshots ORDER BY id DESC LIMIT 1"
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return False
                snapshot_id, raw_json = row[0], row[1]
            data = json.loads(raw_json)
            new_data = [item for item in data if item.get("appid") != appid]
            if len(new_data) == len(data):
                return False
            await db.execute(
                "UPDATE library_snapshots SET app_count = ?, raw_data = ? WHERE id = ?",
                (len(new_data), json.dumps(new_data, ensure_ascii=False), snapshot_id),
            )
            await db.execute("DELETE FROM app_ownership WHERE appid = ?", (appid,))
            await db.commit()
            return True

    remove_app_from_snapshot = remove_app_from_latest_snapshot

    async def reset_snapshots(self):
        """重置清空快照数据库（用于测试首次基线建立）。"""
        async with self._get_conn() as db:
            await db.execute("DELETE FROM library_snapshots")
            await db.execute("DELETE FROM app_ownership")
            await db.commit()

    async def cache_app_details(self, details: AppDetails):
        """缓存游戏商店详情数据。"""
        await self.cache_app_details_batch([details])

    async def cache_app_details_batch(self, details_list: list[AppDetails]):
        """批量高速缓存游戏商店详情数据（单事务写入，速度提升百倍）。"""
        if not details_list:
            return
        async with self._get_conn() as db:
            rows = [
                (
                    d.appid,
                    d.name,
                    d.chinese_name or d.name,
                    d.header_image,
                    1 if d.is_free else 0,
                    d.initial_price,
                    d.final_price,
                    d.discount_percent,
                    json.dumps(d.genres, ensure_ascii=False),
                    json.dumps(d.categories, ensure_ascii=False),
                    d.positive_rate,
                    d.review_summary,
                )
                for d in details_list
            ]
            await db.executemany(
                """
                INSERT OR REPLACE INTO apps_cache (
                    appid, name, chinese_name, header_image, is_free,
                    initial_price, final_price, discount_percent,
                    genres, categories, positive_rate, review_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                rows,
            )
            await db.commit()

    # apps_cache 的显式列清单（防御未来加列导致 SELECT * 行位移错位）
    _APPS_CACHE_COLUMNS = (
        "appid, name, chinese_name, header_image, is_free, "
        "initial_price, final_price, discount_percent, "
        "genres, categories, positive_rate, review_summary"
    )

    @classmethod
    def _row_to_app_details(cls, row: tuple) -> AppDetails:
        """把 apps_cache 的行元组转为 AppDetails（列顺序见 _APPS_CACHE_COLUMNS）。"""
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

    async def get_cached_app(self, appid: int) -> AppDetails | None:
        """从缓存中获取单个游戏详情。"""
        async with (
            self._get_conn() as db,
            db.execute(
                f"SELECT {self._APPS_CACHE_COLUMNS} FROM apps_cache WHERE appid = ?", (appid,)
            ) as cursor,
        ):
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_app_details(row)

    async def get_all_cached_apps(self) -> list[AppDetails]:
        """获取所有已缓存的游戏详情。"""
        async with (
            self._get_conn() as db,
            db.execute(f"SELECT {self._APPS_CACHE_COLUMNS} FROM apps_cache") as cursor,
        ):
            rows = await cursor.fetchall()
            return [self._row_to_app_details(row) for row in rows]

    async def update_prices(
        self, appid: int, initial_price: float, final_price: float, discount_percent: int
    ) -> bool:
        """更新 apps_cache 中指定游戏的最新价格（三字段），仅对已缓存过的游戏生效。

        Returns:
            bool: apps_cache 中存在该 appid 且更新成功返回 True；
            从未缓存过的游戏（无匹配行）直接返回 False，调用方据此跳过。
        """
        async with self._get_conn() as db:
            cur = await db.execute(
                """
                UPDATE apps_cache
                SET initial_price = ?, final_price = ?, discount_percent = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE appid = ?
                """,
                (float(initial_price), float(final_price), int(discount_percent), int(appid)),
            )
            await db.commit()
            return bool(cur.rowcount)


    async def save_member_profiles(self, profiles: dict[str, dict[str, Any]]):
        """保存或更新家庭成员资料（真实昵称与头像）。"""
        if not profiles:
            return
        async with self._get_conn() as db:
            rows = [
                (
                    str(sid),
                    info.get("persona_name") or (f"成员_{sid[-4:]}" if len(sid) >= 4 else sid),
                    info.get("avatar_url") or "",
                )
                for sid, info in profiles.items()
                if sid
            ]
            await db.executemany(
                """
                INSERT INTO member_profiles (steamid, persona_name, avatar_url, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(steamid) DO UPDATE SET
                    persona_name = excluded.persona_name,
                    avatar_url = excluded.avatar_url,
                    updated_at = CURRENT_TIMESTAMP
            """,
                rows,
            )
            await db.commit()

    async def get_member_profiles(self) -> dict[str, dict[str, Any]]:
        """获取所有已缓存的家庭成员资料字典（Key 为 SteamID）。"""
        async with (
            self._get_conn() as db,
            db.execute("SELECT steamid, persona_name, avatar_url FROM member_profiles") as cursor,
        ):
            rows = await cursor.fetchall()
            return {
                r[0]: {
                    "steamid": r[0],
                    "persona_name": r[1],
                    "avatar_url": r[2],
                }
                for r in rows
            }

    async def get_family_rankings(self) -> list[dict[str, Any]]:
        """计算家庭成员贡献榜数据（主要按贡献游戏数量降序排列，并挂载真实昵称与头像）。"""
        profiles = await self.get_member_profiles()
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
                ORDER BY game_count DESC, total_initial DESC
            """
            async with db.execute(sql) as cursor:
                rows = await cursor.fetchall()
                rankings = []
                for r in rows:
                    sid = str(r[0])
                    p_info = profiles.get(sid, {})
                    fallback_name = f"成员_{sid[-4:]}" if len(sid) >= 4 else sid
                    rankings.append(
                        {
                            "steamid": sid,
                            "persona_name": p_info.get("persona_name") or fallback_name,
                            "avatar_url": p_info.get("avatar_url") or "",
                            "game_count": r[1],
                            "total_initial_price": round(r[2], 2),
                            "total_current_price": round(r[3], 2),
                        }
                    )
                return rankings

    async def get_coop_games(self, member_steamids: list[str]) -> list[dict[str, Any]]:
        """计算指定多位成员共同拥有的联机游戏。"""
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
        """获取全景 Web 大盘展示所需的全量数据结构。"""
        snapshot = await self.get_latest_snapshot()
        rankings = await self.get_family_rankings()
        cached_apps = await self.get_all_cached_apps()
        cached_map = {a.appid: a for a in cached_apps}
        profiles = await self.get_member_profiles()

        total_initial = sum(r["total_initial_price"] for r in rankings)
        total_final = sum(r["total_current_price"] for r in rankings)
        total_game_count = sum(r["game_count"] for r in rankings)

        all_snapshot_apps = []
        genre_counter: dict[str, int] = {}
        price_counter = {"free": 0, "under50": 0, "50to150": 0, "150to300": 0, "over300": 0}

        if snapshot:
            sorted_apps = sorted(
                snapshot.values(),
                key=lambda a: (int(getattr(a, "rt_time_acquired", 0) or 0), a.appid),
                reverse=True,
            )
            for app in sorted_apps:
                detail = cached_map.get(app.appid)
                init_p = detail.initial_price if detail else 0.0
                final_p = detail.final_price if detail else 0.0
                disc = detail.discount_percent if detail else 0
                pos_rate = detail.positive_rate if detail else 95
                genres = detail.genres if detail and detail.genres else ["Steam 共享"]
                cats = detail.categories if detail and detail.categories else []

                # Count genres
                for g in genres:
                    if g:
                        genre_counter[g] = genre_counter.get(g, 0) + 1

                # Count price ranges
                if detail and detail.is_free or (init_p == 0 and detail is not None):
                    price_counter["free"] += 1
                elif init_p < 50:
                    price_counter["under50"] += 1
                elif init_p <= 150:
                    price_counter["50to150"] += 1
                elif init_p <= 300:
                    price_counter["150to300"] += 1
                else:
                    price_counter["over300"] += 1

                # Map owner steamids to real nicknames
                owner_names = []
                for sid in app.owner_steamids:
                    p = profiles.get(str(sid))
                    if p and p.get("persona_name"):
                        owner_names.append(p["persona_name"])
                    else:
                        owner_names.append(
                            f"成员_{str(sid)[-4:]}" if len(str(sid)) >= 4 else str(sid)
                        )
                owners_display = ", ".join(owner_names) if owner_names else "家庭成员"

                all_snapshot_apps.append(
                    {
                        "appid": app.appid,
                        "name": (
                            detail.chinese_name if detail and detail.chinese_name else app.name
                        )
                        or app.name,
                        "original_name": app.name,
                        "header_image": detail.header_image
                        if detail
                        else f"https://cdn.akamai.steamstatic.com/steam/apps/{app.appid}/header.jpg",
                        "owners": owners_display,
                        "owner_steamids": app.owner_steamids,
                        "initial_price": init_p,
                        "final_price": final_p,
                        "discount_percent": disc,
                        "is_cached": detail is not None,
                        "positive_rate": pos_rate,
                        "review_summary": detail.review_summary if detail else "好评",
                        "genres": genres,
                        "categories": cats,
                        "rt_time_acquired": int(getattr(app, "rt_time_acquired", 0) or 0),
                        "rt_last_played": int(getattr(app, "rt_last_played", 0) or 0),
                        "rt_playtime": int(getattr(app, "rt_playtime", 0) or 0),
                        "img_icon_hash": getattr(app, "img_icon_hash", ""),
                    }
                )

        # Calculate rankings contribution percentage (by contributed game count)
        for r in rankings:
            r["percentage"] = (
                round((r["game_count"] / total_game_count * 100), 1)
                if total_game_count > 0
                else round(100.0 / max(1, len(rankings)), 1)
            )

        top_genres = sorted(genre_counter.items(), key=lambda x: x[1], reverse=True)[:8]
        recent_games = all_snapshot_apps[:12]
        cached_count = len([a for a in all_snapshot_apps if a.get("is_cached")])

        return {
            "total_games": len(snapshot) if snapshot else 0,
            "cached_games_count": cached_count,
            "total_initial_value": round(total_initial, 2),
            "total_current_value": round(total_final, 2),
            "member_count": len(rankings),
            "rankings": rankings,
            "member_profiles": profiles,
            "recent_games": recent_games,
            "all_snapshot_apps": all_snapshot_apps,
            "top_genres": [{"genre": g[0], "count": g[1]} for g in top_genres],
            "price_stats": price_counter,
        }

    # ------------------------------------------------------------------
    # 凭据 KV 持久化 (滚动续期 Token 的二级保险，防止配置保存失败时丢失)
    # ------------------------------------------------------------------
    async def set_kv(self, key: str, value: str):
        """持久化一条凭据键值（如滚动续期后的 refresh_token）。"""
        if key is None or value is None:
            return
        async with self._get_conn() as db:
            await db.execute(
                """
                INSERT INTO credentials_kv (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (str(key), str(value)),
            )
            await db.commit()

    async def get_kv(self, key: str) -> str | None:
        """读取一条凭据键值。"""
        async with (
            self._get_conn() as db,
            db.execute("SELECT value FROM credentials_kv WHERE key = ?", (key,)) as cursor,
        ):
            row = await cursor.fetchone()
            return row[0] if row else None

    async def get_kv_all(self) -> dict[str, str]:
        """读取全部凭据键值。"""
        async with (
            self._get_conn() as db,
            db.execute("SELECT key, value FROM credentials_kv") as cursor,
        ):
            rows = await cursor.fetchall()
            return {str(r[0]): str(r[1]) for r in rows}

    async def delete_kv(self, key: str):
        async with self._get_conn() as db:
            await db.execute("DELETE FROM credentials_kv WHERE key = ?", (key,))
            await db.commit()

    # ------------------------------------------------------------------
    # 待推送通知队列（快照先落库、通知后推送：失败重试、指数退避、防丢失）
    # ------------------------------------------------------------------
    async def enqueue_pending(self, kind: str, payload: dict, dedupe_key: str) -> None:
        """入队一条待推送通知。

        同一 dedupe_key 重复入队时：更新 payload_json 并将 next_attempt_at 归零
        （允许立即重试），但保留原 retries 计数。
        """
        async with self._get_conn() as db:
            await db.execute(
                """
                INSERT INTO pending_notifications (kind, dedupe_key, payload_json, next_attempt_at)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    next_attempt_at = 0
                """,
                (kind, dedupe_key, json.dumps(payload, ensure_ascii=False)),
            )
            await db.commit()

    async def claim_due_pending(self, limit: int = 20) -> list[dict]:
        """领取当前到期（next_attempt_at <= now）的待推送通知，按 id 升序。

        逐行 json.loads 并做毒丸防护：payload_json 损坏的行不会让整批领取炸掉，
        而是按重试退避处理（retries+1；超过上限删除并记录 error 日志），
        其余健康行正常返回。

        并发与可靠性语义（务必了解的前提）：
          - 领取动作只是一条 SELECT 查询，**不会**对行加任何租约/占用标记。
            并发安全的前提是单实例部署 + 调用方用 asyncio 锁串行化
            "claim → 推送 → mark_pending_done" 全程；多实例共享同一 DB 时
            会重复领取同一行，属于不安全用法。
          - at-least-once 语义：若进程在 claim 之后、mark_pending_done 之前
            崩溃，该行仍留在队列中，重启后会被再次领取并重发一次
            （至多重发、不丢消息；调用方应保证通知内容可安全重复展示）。
        """
        async with (
            self._get_conn() as db,
            db.execute(
                "SELECT id, kind, dedupe_key, payload_json, retries FROM pending_notifications "
                "WHERE next_attempt_at <= ? ORDER BY id LIMIT ?",
                (time.time(), limit),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        claimed: list[dict] = []
        poisoned_ids: list[int] = []
        for row in rows:
            try:
                payload = json.loads(row[3])
            except (TypeError, ValueError):
                # 毒丸行：单独退避，不阻断本批其余健康行
                poisoned_ids.append(row[0])
                logger.warning(
                    "pending_notifications payload_json 解析失败，已退避重试: "
                    "id=%s dedupe_key=%s retries=%s",
                    row[0],
                    row[2],
                    row[4],
                )
                continue
            claimed.append({"id": row[0], "kind": row[1], "payload": payload, "retries": row[4]})
        for pid in poisoned_ids:
            # 复用退避逻辑：retries+1，超上限由 bump_pending_retry 删除并记录 error 日志
            await self.bump_pending_retry(pid)
        return claimed

    async def mark_pending_done(self, id: int) -> None:
        """标记通知推送成功完成（直接删除队列行）。"""
        async with self._get_conn() as db:
            await db.execute("DELETE FROM pending_notifications WHERE id = ?", (id,))
            await db.commit()

    async def bump_pending_retry(self, id: int, max_retries: int = 5) -> bool:
        """推送失败后的重试退避处理。

        retries 自增 1；若达到上限（max_retries）则删除该行并返回 False；
        删除前记录 error 日志（kind / dedupe_key / payload_json 截断 200 字），
        便于排查彻底推不出去的通知。
        否则设置指数退避 next_attempt_at = now + min(300 * 2**retries, 3600)
        （retries 为自增后的新值，退避最长 1 小时）并返回 True。
        """
        async with self._get_conn() as db:
            async with db.execute(
                "SELECT retries, kind, dedupe_key, payload_json FROM pending_notifications "
                "WHERE id = ?",
                (id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return False
            new_retries = int(row[0]) + 1
            if new_retries >= max_retries:
                logger.error(
                    "待推送通知重试达上限被丢弃: kind=%s dedupe_key=%s payload=%.200s",
                    row[1],
                    row[2],
                    row[3] or "",
                )
                await db.execute("DELETE FROM pending_notifications WHERE id = ?", (id,))
                await db.commit()
                return False
            delay = min(300 * (2**new_retries), 3600)
            await db.execute(
                "UPDATE pending_notifications SET retries = ?, next_attempt_at = ? WHERE id = ?",
                (new_retries, time.time() + delay, id),
            )
            await db.commit()
            return True

    async def pending_count(self) -> int:
        """当前待推送通知队列长度。"""
        async with (
            self._get_conn() as db,
            db.execute("SELECT COUNT(*) FROM pending_notifications") as cursor,
        ):
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # 快照修剪（防止 library_snapshots 无限膨胀，且最新基线永不丢失）
    # ------------------------------------------------------------------
    async def prune_snapshots(self, keep_min: int = 200, keep_days: int = 30) -> int:
        """修剪历史快照，返回被删除的行数。

        最新的 keep_min 条永远保留；更早的行中，创建时间超过 keep_days 天的才会被删除。
        """
        async with self._get_conn() as db:
            async with db.execute(
                "SELECT MIN(id) FROM (SELECT id FROM library_snapshots ORDER BY id DESC LIMIT ?)",
                (keep_min,),
            ) as cursor:
                row = await cursor.fetchone()
            boundary_id = row[0] if row else None
            if boundary_id is None:
                if keep_min > 0:
                    return 0  # 表为空
                boundary_id = 0  # keep_min<=0：不保留最新若干条，全部按时间规则修剪
            cutoff_seconds = int(time.time() - keep_days * 86400)
            # created_at 由 CURRENT_TIMESTAMP 写入（UTC 文本），用 datetime(?, 'unixepoch')
            # 统一转为同格式文本再做文本比较，避免 REAL 与 TEXT 跨类型比较永远为假的坑。
            cur = await db.execute(
                "DELETE FROM library_snapshots "
                "WHERE id < ? AND created_at < datetime(?, 'unixepoch')",
                (boundary_id, cutoff_seconds),
            )
            deleted = cur.rowcount or 0
            await db.commit()
            return int(deleted)

    # ------------------------------------------------------------------
    # 同步状态观测（基于通用 kv 表 credentials_kv 的薄封装）
    # 常用键：sync_last_success_ts / sync_last_duration_ms / sync_last_message
    # ------------------------------------------------------------------
    @staticmethod
    def _sync_state_key(key: str) -> str:
        """归一化同步状态键（允许调用方省略 sync_ 前缀）。"""
        key = str(key)
        return key if key.startswith("sync_") else f"sync_{key}"

    async def set_sync_state(self, key: str, value: str) -> None:
        """写入一条同步状态键值（键名由调用方给出，如 sync_last_success_ts）。"""
        await self.set_kv(self._sync_state_key(key), str(value))

    async def get_sync_state(self, key: str) -> str | None:
        """读取一条同步状态键值。"""
        return await self.get_kv(self._sync_state_key(key))

    async def get_sync_state_info(self) -> dict[str, Any]:
        """汇总最近一次同步的观测信息（键缺失/非法时返回缺省值）。"""
        last_ts_raw = await self.get_sync_state("last_success_ts")
        last_dur_raw = await self.get_sync_state("last_duration_ms")
        try:
            last_success_ts = float(last_ts_raw) if last_ts_raw is not None else 0.0
        except (TypeError, ValueError):
            last_success_ts = 0.0
        try:
            last_duration_ms = int(float(last_dur_raw)) if last_dur_raw is not None else 0
        except (TypeError, ValueError):
            last_duration_ms = 0
        if last_success_ts > 0:
            last_success_ago_minutes = max(0.0, (time.time() - last_success_ts) / 60.0)
        else:
            last_success_ago_minutes = 0.0
        return {
            "last_success_ts": last_success_ts,
            "last_duration_ms": last_duration_ms,
            "last_success_ago_minutes": last_success_ago_minutes,
        }

    # ------------------------------------------------------------------
    # 推送失败计数（用于自动清理失效订阅者）
    # ------------------------------------------------------------------
    async def record_push_failure(self, target_id: str) -> int:
        """记录一次推送到 target_id 的失败并累加计数，返回累计失败次数。"""
        async with self._get_conn() as db:
            await db.execute(
                """
                INSERT INTO push_failures (target_id, fail_count, last_fail_at)
                VALUES (?, 1, ?)
                ON CONFLICT(target_id) DO UPDATE SET
                    fail_count = fail_count + 1,
                    last_fail_at = excluded.last_fail_at
                """,
                (target_id, time.time()),
            )
            await db.commit()
            async with db.execute(
                "SELECT fail_count FROM push_failures WHERE target_id = ?", (target_id,)
            ) as cursor:
                row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def reset_push_failures(self, target_id: str) -> None:
        """重置（清除）某目标的推送失败计数（推送恢复成功后调用）。"""
        async with self._get_conn() as db:
            await db.execute("DELETE FROM push_failures WHERE target_id = ?", (target_id,))
            await db.commit()

    async def prune_dead_subscribers(
        self, max_failures: int = 5, failure_window_hours: int = 24
    ) -> list[str]:
        """移除推送持续失败的失效订阅者。

        仅当 fail_count >= max_failures 且 last_fail_at 位于最近
        failure_window_hours 小时内才删除（stale 失败可能是目标已恢复但一直没触发
        重发，不应误删）。last_fail_at 为空的行同样保守跳过。
        同时删除 subscriptions 表中的订阅与 push_failures 表中的计数，
        返回被移除的 target_id 列表。
        """
        window_seconds = max(0, int(failure_window_hours)) * 3600
        cutoff = time.time() - window_seconds
        async with self._get_conn() as db:
            async with db.execute(
                "SELECT target_id FROM push_failures "
                "WHERE fail_count >= ? AND last_fail_at IS NOT NULL AND last_fail_at >= ?",
                (max_failures, cutoff),
            ) as cursor:
                rows = await cursor.fetchall()
            dead_ids = [r[0] for r in rows]
            if not dead_ids:
                return []
            placeholders = ",".join("?" * len(dead_ids))
            await db.execute(
                f"DELETE FROM subscriptions WHERE target_id IN ({placeholders})",
                (*dead_ids,),
            )
            await db.execute(
                f"DELETE FROM push_failures WHERE target_id IN ({placeholders})",
                (*dead_ids,),
            )
            await db.commit()
            return dead_ids

    # ------------------------------------------------------------------
    # 游玩时长统计（数据源：IFamilyGroupsService/GetPlaytimeSummary 的 response.entries）
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_entry_keys(entry: dict) -> dict[str, Any]:
        """将 Steam 返回的 entry 键归一化（去下划线并转小写），容忍大小写/命名混用。"""
        return {str(k).replace("_", "").lower(): v for k, v in entry.items()}

    async def refresh_playtime_stats(self, entries: list[dict]) -> int:
        """批量刷新游玩时长统计（单事务 upsert），返回写入条数。

        entries 来自 IFamilyGroupsService/GetPlaytimeSummary 的 response.entries，
        真实字段形状（camelCase，时长单位已是秒、时间为 Unix 时间戳）：
            {"steamid": str, "appid": int,
             "firstPlayed": int, "latestPlayed": int, "secondsPlayed": int}

        键归一化兼容大小写/下划线混用（latestPlayed / latest_played / rt_last_played
        / LastPlayed 等），rt_last_played 列依次取 latestPlayed → rt_last_played →
        firstPlayed（缺前者时的兜底）；seconds_played 直接入库为秒，不做任何换算。

        注意：历史实现误假设"Steam 返回 rt_playtime 单位为分钟，需 ×60 换算成秒"，
        但该接口并不存在 rt_playtime 字段——真实字段 secondsPlayed 的单位已是秒，
        ×60 会把真实数据虚增 60 倍，故该换算分支已删除。
        """
        if not entries:
            return 0
        rows: list[tuple[str, int, float, int]] = []
        for entry in entries:
            if not entry:
                continue
            norm = self._normalize_entry_keys(entry)
            steamid = norm.get("steamid")
            raw_appid = norm.get("appid")
            if not steamid or raw_appid is None:
                continue
            try:
                appid = int(raw_appid)
            except (TypeError, ValueError):
                continue
            # secondsPlayed 直接入库为秒（真实字段单位已是秒）
            seconds_played = 0.0
            if norm.get("secondsplayed") is not None:
                try:
                    seconds_played = float(norm["secondsplayed"])
                except (TypeError, ValueError):
                    seconds_played = 0.0
            # 最后游玩时间：latestPlayed 优先，兼容旧字段 rt_last_played，firstPlayed 兜底
            rt_last_played = 0
            for key in ("latestplayed", "rtlastplayed", "lastplayed", "firstplayed"):
                if norm.get(key) is not None:
                    try:
                        rt_last_played = int(norm[key])
                        break
                    except (TypeError, ValueError):
                        continue
            rows.append((str(steamid), appid, seconds_played, rt_last_played))
        if not rows:
            return 0
        async with self._get_conn() as db:
            await db.executemany(
                """
                INSERT INTO playtime_stats (steamid, appid, seconds_played, rt_last_played, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(steamid, appid) DO UPDATE SET
                    seconds_played = excluded.seconds_played,
                    rt_last_played = excluded.rt_last_played,
                    updated_at = CURRENT_TIMESTAMP
                """,
                rows,
            )
            await db.commit()
        return len(rows)

    async def get_recent_plays(self, limit: int = 10) -> list[dict]:
        """获取最近游玩记录（按最后游玩时间倒序），联表成员资料与游戏详情缓存。"""
        async with (
            self._get_conn() as db,
            db.execute(
                """
                SELECT
                    p.steamid,
                    p.appid,
                    p.seconds_played,
                    p.rt_last_played,
                    COALESCE(NULLIF(a.chinese_name, ''), a.name) AS game_name,
                    a.header_image,
                    m.persona_name,
                    m.avatar_url
                FROM playtime_stats p
                LEFT JOIN member_profiles m ON p.steamid = m.steamid
                LEFT JOIN apps_cache a ON p.appid = a.appid
                ORDER BY p.rt_last_played DESC
                LIMIT ?
                """,
                (limit,),
            ) as cursor,
        ):
            rows = await cursor.fetchall()
        return [
            {
                "steamid": r[0],
                "appid": r[1],
                "game_name": r[4] or f"App {r[1]}",
                "persona_name": r[6] or "",
                "avatar_url": r[7] or "",
                "seconds_played": r[2],
                "rt_last_played": r[3],
                "header_image": r[5] or "",
            }
            for r in rows
        ]
