import logging
import shutil
from pathlib import Path

from app.config import Settings
from app.db import Database

log = logging.getLogger(__name__)


def cleanup(db: Database, settings: Settings, now: float, active: set[Path] | None = None) -> None:
    active = active or set()
    db.conn.execute(
        "DELETE FROM metric_snapshots WHERE captured_at<?",
        (now - settings.metrics_retention_hours * 3600,),
    )
    db.conn.execute("DELETE FROM videos WHERE published_at<?", (now - 48 * 3600,))
    db.conn.execute(
        "DELETE FROM deliveries WHERE delivered_at<?",
        (now - settings.delivery_retention_days * 86400,),
    )
    db.conn.execute("DELETE FROM hashtag_evidence WHERE observed_at<?", (now - 24 * 3600,))
    db.conn.execute("DELETE FROM broadcast_slots WHERE claimed_at<?", (now - 30 * 86400,))
    db.conn.execute("DELETE FROM auth_attempts WHERE window_start<?", (now - 86400,))
    db.expire_queries(now)
    db.conn.execute(
        "DELETE FROM query_messages WHERE query_id IN "
        "(SELECT id FROM dynamic_queries WHERE status!='pending' AND decided_at<?)",
        (now - settings.query_cooldown_days * 86400,),
    )
    db.conn.execute(
        "DELETE FROM dynamic_queries WHERE status!='pending' AND decided_at<?",
        (now - settings.query_cooldown_days * 86400,),
    )
    db.conn.execute(
        "UPDATE dynamic_queries SET status='expired',decided_at=? "
        "WHERE status='pending' AND discovered_at<?",
        (now, now - 86400),
    )
    root = settings.media_dir.resolve()
    if root.exists():
        for path in root.iterdir():
            if path.is_symlink() or path in active:
                continue
            # Files outside this dedicated media directory are never traversed/deleted.
            if path.resolve().parent != root:
                continue
            if not (
                path.suffix in (".mp4", ".part", ".tmp")
                or (path.is_dir() and path.name.startswith("ocr-"))
            ):
                continue
            # Safety margin for the cleanup scheduler's one-minute polling interval.
            ttl = (
                max(0, settings.media_retention_hours * 3600 - 60)
                if path.suffix == ".mp4"
                else 1800
            )
            if now - path.stat().st_mtime >= ttl:
                if path.is_file():
                    path.unlink()
                elif path.is_dir() and path.name.startswith("ocr-"):
                    shutil.rmtree(path)
                log.info("media_removed name=%s", path.name)
    log.info("cleanup_complete")
