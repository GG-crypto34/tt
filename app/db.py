import json
import logging
import sqlite3
from pathlib import Path

from app.models import Metrics, Source, Video

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '', authenticated_at REAL NOT NULL,
 mailing_enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS videos (
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, url TEXT NOT NULL, published_at REAL NOT NULL,
 discovered_at REAL NOT NULL, updated_at REAL NOT NULL, language_score REAL NOT NULL,
 latest_trend_score REAL NOT NULL DEFAULT 0, metadata TEXT NOT NULL, source TEXT);
CREATE INDEX IF NOT EXISTS videos_age ON videos(published_at);
CREATE TABLE IF NOT EXISTS metric_snapshots (
 video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE, captured_at REAL NOT NULL,
 views INTEGER NOT NULL, likes INTEGER NOT NULL, shares INTEGER NOT NULL, comments INTEGER NOT NULL,
 PRIMARY KEY(video_id,captured_at));
CREATE TABLE IF NOT EXISTS deliveries (
 user_id INTEGER NOT NULL, video_id TEXT NOT NULL, delivered_at REAL NOT NULL,
 delivery_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', message_id INTEGER,
 PRIMARY KEY(user_id,video_id));
CREATE TABLE IF NOT EXISTS dynamic_queries (
 id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT UNIQUE NOT NULL, discovered_at REAL NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', decided_by INTEGER, decided_at REAL,
 expires_at REAL, last_success_at REAL);
CREATE TABLE IF NOT EXISTS query_messages (
 query_id INTEGER NOT NULL, user_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
 PRIMARY KEY(query_id,user_id));
CREATE TABLE IF NOT EXISTS hashtag_evidence (
 tag TEXT NOT NULL, video_id TEXT NOT NULL, observed_at REAL NOT NULL,
 PRIMARY KEY(tag,video_id));
CREATE TABLE IF NOT EXISTS service_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS auth_attempts (
 user_id INTEGER PRIMARY KEY, failures INTEGER NOT NULL, window_start REAL NOT NULL);
CREATE TABLE IF NOT EXISTS broadcast_slots (slot TEXT PRIMARY KEY, claimed_at REAL NOT NULL);
PRAGMA user_version=1;
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        if self.conn.execute("PRAGMA user_version").fetchone()[0] > 1:
            raise ValueError("База создана более новой версией приложения")
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT OR IGNORE INTO service_state VALUES ('global_enabled','1')")
        log.info("database_initialized version=1")

    def close(self) -> None:
        self.conn.close()

    def rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def state(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM service_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO service_state VALUES (?,?) ON CONFLICT(key) "
            "DO UPDATE SET value=excluded.value",
            (key, value),
        )

    @property
    def enabled(self) -> bool:
        return self.state("global_enabled", "1") == "1"

    def user(self, user_id: int):
        return self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    def authorize(self, user_id: int, username: str, now: float) -> None:
        self.conn.execute(
            "INSERT INTO users(id,username,authenticated_at,created_at) VALUES "
            "(?,?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username, "
            "authenticated_at=excluded.authenticated_at",
            (user_id, username, now, now),
        )
        self.conn.execute("DELETE FROM auth_attempts WHERE user_id=?", (user_id,))

    def pause(self, user_id: int, paused: bool) -> None:
        self.conn.execute("UPDATE users SET mailing_enabled=? WHERE id=?", (not paused, user_id))

    def logout(self, user_id: int) -> None:
        self.conn.execute("DELETE FROM users WHERE id=?", (user_id,))

    def auth_blocked(self, user_id: int, now: float) -> bool:
        row = self.conn.execute(
            "SELECT * FROM auth_attempts WHERE user_id=?", (user_id,)
        ).fetchone()
        return bool(row and now - row["window_start"] < 900 and row["failures"] >= 5)

    def auth_failure(self, user_id: int, now: float) -> None:
        self.conn.execute(
            "INSERT INTO auth_attempts VALUES (?,1,?) ON CONFLICT(user_id) "
            "DO UPDATE SET failures=CASE WHEN ?-window_start>=900 THEN 1 "
            "ELSE failures+1 END, window_start=CASE WHEN ?-window_start>=900 "
            "THEN ? ELSE window_start END",
            (user_id, now, now, now, now),
        )

    def save_video(self, video: Video, now: float, language: float) -> None:
        self.conn.execute(
            "INSERT INTO videos(id,provider,url,published_at,discovered_at,updated_at,"
            "language_score,metadata) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) "
            "DO UPDATE SET metadata=excluded.metadata,updated_at=excluded.updated_at,"
            "language_score=excluded.language_score,published_at=excluded.published_at",
            (
                video.id,
                video.provider,
                video.url,
                video.published_at,
                now,
                now,
                language,
                json.dumps(video.to_dict(), ensure_ascii=False),
            ),
        )
        m = video.metrics
        self.conn.execute(
            "INSERT OR IGNORE INTO metric_snapshots VALUES (?,?,?,?,?,?)",
            (video.id, now, m.views, m.likes, m.shares, m.comments),
        )

    def snapshots(self, video_id: str) -> list[tuple[float, Metrics]]:
        return [
            (r["captured_at"], Metrics(r["views"], r["likes"], r["shares"], r["comments"]))
            for r in self.rows(
                "SELECT * FROM metric_snapshots WHERE video_id=? ORDER BY captured_at", (video_id,)
            )
        ]

    def candidates(self, now: float, max_age: float) -> list[Video]:
        return [
            Video.from_dict(json.loads(r["metadata"]))
            for r in self.rows(
                "SELECT metadata FROM videos WHERE published_at>=? AND published_at<=?",
                (now - max_age * 3600, now),
            )
        ]

    def save_score(self, video_id: str, score: float) -> None:
        self.conn.execute("UPDATE videos SET latest_trend_score=? WHERE id=?", (score, video_id))

    def source(self, video_id: str) -> Source | None:
        row = self.conn.execute("SELECT source FROM videos WHERE id=?", (video_id,)).fetchone()
        return Source(**json.loads(row[0])) if row and row[0] else None

    def save_source(self, video_id: str, source: Source) -> None:
        from dataclasses import asdict

        self.conn.execute(
            "UPDATE videos SET source=? WHERE id=?",
            (json.dumps(asdict(source), ensure_ascii=False), video_id),
        )

    def delivered(self, user_id: int, video_id: str) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM deliveries WHERE user_id=? AND video_id=?", (user_id, video_id)
            ).fetchone()
        )

    def reserve_delivery(self, user_id: int, video_id: str, now: float, kind: str) -> bool:
        return (
            self.conn.execute(
                "INSERT OR IGNORE INTO deliveries "
                "(user_id,video_id,delivered_at,delivery_type) VALUES (?,?,?,?)",
                (user_id, video_id, now, kind),
            ).rowcount
            == 1
        )

    def finish_delivery(self, user_id: int, video_id: str, message_id: int) -> None:
        self.conn.execute(
            "UPDATE deliveries SET status='sent',message_id=? WHERE user_id=? AND video_id=?",
            (message_id, user_id, video_id),
        )

    def release_delivery(self, user_id: int, video_id: str) -> None:
        self.conn.execute(
            "DELETE FROM deliveries WHERE user_id=? AND video_id=? AND status='pending'",
            (user_id, video_id),
        )

    def claim_slot(self, slot: str, now: float) -> bool:
        return (
            self.conn.execute(
                "INSERT OR IGNORE INTO broadcast_slots VALUES (?,?)", (slot, now)
            ).rowcount
            == 1
        )

    def active_queries(self, now: float) -> list[str]:
        return [
            r[0]
            for r in self.rows(
                "SELECT query FROM dynamic_queries WHERE status='approved' "
                "AND expires_at>? ORDER BY id",
                (now,),
            )
        ]

    def query_success(self, query: str, now: float, ttl: float) -> None:
        self.conn.execute(
            "UPDATE dynamic_queries SET last_success_at=?,expires_at=? "
            "WHERE query=? AND status='approved' AND expires_at>?",
            (now, now + ttl * 3600, query, now),
        )

    def propose(self, tag: str, now: float, cooldown: int) -> int | None:
        row = self.conn.execute("SELECT * FROM dynamic_queries WHERE query=?", (tag,)).fetchone()
        if row:
            if row["status"] in ("pending", "approved"):
                return None
            if now - (row["decided_at"] or row["discovered_at"]) < cooldown * 86400:
                return None
            self.conn.execute("DELETE FROM query_messages WHERE query_id=?", (row["id"],))
            # New ID means old buttons cannot decide a newly proposed generation.
            self.conn.execute("DELETE FROM dynamic_queries WHERE id=?", (row["id"],))
        return self.conn.execute(
            "INSERT INTO dynamic_queries(query,discovered_at) VALUES (?,?)", (tag, now)
        ).lastrowid

    def decide_query(
        self, query_id: int, user_id: int, approve: bool, now: float, ttl: float
    ) -> bool:
        if not self.user(user_id):
            return False
        # A single conditional UPDATE makes concurrent first-decision-wins atomic.
        return (
            self.conn.execute(
                "UPDATE dynamic_queries SET status=?,decided_by=?,decided_at=?,"
                "expires_at=? WHERE id=? AND status='pending'",
                (
                    "approved" if approve else "rejected",
                    user_id,
                    now,
                    now + ttl * 3600 if approve else None,
                    query_id,
                ),
            ).rowcount
            == 1
        )

    def expire_queries(self, now: float) -> int:
        return self.conn.execute(
            "UPDATE dynamic_queries SET status='expired',decided_at=? "
            "WHERE status='approved' AND expires_at<=?",
            (now, now),
        ).rowcount
