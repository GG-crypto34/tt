import asyncio
import logging
import re
import time
from pathlib import Path

from app.cleanup import cleanup
from app.config import Settings
from app.db import Database
from app.models import Source, SourceIdentifier, TikTokProvider, Video
from app.ranking import FILM_WORDS, Growth, eligible, growth, language_relevance, trend_score
from app.telegram import Messenger, TelegramRejected

log = logging.getLogger(__name__)


def caption(video: Video, velocity: Growth, score: float, source: Source, now: float) -> str:
    source_text = "Источник: не удалось определить"
    if source.title:
        label = "Источник (по описанию)" if source.confidence >= 0.8 else "Возможно"
        source_text = f"{label}: {source.title[:100]}\nУверенность: {source.confidence:.0%}"
        if "tmdb" in source.method:
            source_text += "\nКаталог: TMDB (themoviedb.org)"
    age = max(0, int((now - video.published_at) / 60))
    m = video.metrics
    return (
        f"🔥 Быстро растёт\n\n{source_text}\n\n👁 Просмотры: {m.views:,}\n"
        f"📈 Рост: +{velocity.views:,.0f} просмотров/ч"
        f"{' (предварительно)' if not velocity.measured else ''}\n"
        f"❤️ Лайки: {m.likes:,}\n❤️ Рост лайков: +{velocity.likes:,.0f}/ч\n"
        f"🔁 Репосты: {m.shares:,}\n💬 Комментарии: {m.comments:,}\n\n"
        f"🕐 Возраст: {age // 60} ч {age % 60} мин\n👤 Автор: @{video.author[:50]}\n"
        f"👥 Подписчиков: {video.followers if video.followers is not None else 'неизвестно'}\n"
        f"⭐ Trend Score: {score:.1f}/100\n\n🔗 Оригинал: {video.url}"
    )


class TrendService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        provider: TikTokProvider,
        messenger: Messenger,
        identifier: SourceIdentifier,
    ):
        self.settings, self.db, self.provider = settings, db, provider
        self.messenger, self.identifier = messenger, identifier
        self.scan_lock = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()
        self.work_lock = asyncio.Lock()
        self.active_media: set[Path] = set()
        settings.media_dir.mkdir(parents=True, exist_ok=True)

    def cleanup(self, now: float) -> None:
        cleanup(self.db, self.settings, now, self.active_media)

    def ranked(self, now: float, users: list[int]) -> list[tuple[Video, Growth, float]]:
        result = []
        for video in self.db.candidates(now, self.settings.max_video_age_hours):
            snapshots = self.db.snapshots(video.id)
            if not eligible(video, now, self.settings):
                continue
            # Do not deliver hours-old counters when a provider outage prevents refresh.
            if not snapshots or now - snapshots[-1][0] > self.settings.scan_interval_minutes * 120:
                continue
            if all(self.db.delivered(user_id, video.id) for user_id in users):
                continue
            velocity = growth(video, snapshots, now)
            score = trend_score(video, velocity, now, self.settings)
            self.db.save_score(video.id, score)
            result.append((video, velocity, score))
        return sorted(result, key=lambda item: (-item[2], item[0].id))

    async def scan(self, now: float | None = None) -> bool:
        if self.scan_lock.locked() or not self.db.enabled:
            return False
        async with self.scan_lock, self.work_lock:
            try:
                async with asyncio.timeout(self.settings.scan_timeout_seconds):
                    await self._scan(now)
            except TimeoutError:
                self.db.set_state("discovery_available", "0")
                self.db.set_state(
                    "provider_health", "Scan превысил общий timeout; повтор на следующем цикле"
                )
                log.warning("scan_timeout budget=%s", self.settings.scan_timeout_seconds)
            return True

    async def _scan(self, now: float | None = None) -> None:
        if not self.db.enabled:
            return
        started = now if now is not None else time.time()
        self.db.set_state("last_scan_attempt", str(started))
        self.db.expire_queries(started)
        queries = list(
            dict.fromkeys([*self.settings.base_queries, *self.db.active_queries(started)])
        )
        found: dict[str, Video] = {}
        observed_at: dict[str, float] = {}
        failures = 0
        successes = 0
        seen: set[str] = set()
        rejected = {"age": 0, "topic": 0, "language": 0}
        deadline = asyncio.get_running_loop().time() + self.settings.scan_timeout_seconds
        budget_limited = False
        log.info("scan_started queries=%s", queries)
        try:
            if hasattr(self.provider, "begin_scan"):
                await self.provider.begin_scan()
            for query in queries:
                if not self.db.enabled:
                    return
                if (
                    successes + failures
                    and deadline - asyncio.get_running_loop().time()
                    < self.settings.search_query_timeout + 10
                ):
                    budget_limited = True
                    break
                try:
                    async with asyncio.timeout(self.settings.search_query_timeout + 1):
                        items = await self.provider.search(query, self.settings.search_limit)
                    successes += 1
                    new_good = False
                    for video in items:
                        captured = now if now is not None else time.time()
                        age = (captured - video.published_at) / 3600
                        if not 0 <= age <= self.settings.max_video_age_hours:
                            if video.id not in seen:
                                rejected["age"] += 1
                            seen.add(video.id)
                            continue
                        seen.add(video.id)
                        if video.id not in found and len(found) >= self.settings.max_candidates:
                            continue
                        found[video.id] = video
                        observed_at[video.id] = captured
                        query_matches = not query.startswith("#") or query[1:].lower() in {
                            tag.lower().lstrip("#") for tag in video.hashtags
                        }
                        new_good |= eligible(video, started, self.settings) and query_matches
                    if new_good:
                        self.db.query_success(query, started, self.settings.query_ttl_hours)
                except Exception as error:
                    failures += 1
                    log.warning("search_failed query=%s type=%s", query, type(error).__name__)
                await asyncio.sleep(self.settings.request_delay_seconds)
            # Refresh tracked videos even if they have disappeared from search results.
            tracked = self.db.candidates(started, self.settings.max_video_age_hours)
            for video in tracked:
                if not self.db.enabled:
                    return
                if video.id in found or len(found) >= self.settings.max_candidates:
                    continue
                if (
                    deadline - asyncio.get_running_loop().time()
                    < self.settings.request_timeout * 2 + 10
                ):
                    budget_limited = True
                    break
                try:
                    async with asyncio.timeout(self.settings.request_timeout * 2):
                        found[video.id] = await self.provider.metadata(video.url)
                        observed_at[video.id] = now if now is not None else time.time()
                except Exception as error:
                    log.warning(
                        "metric_refresh_failed video=%s type=%s", video.id, type(error).__name__
                    )
                await asyncio.sleep(self.settings.request_delay_seconds)
            accepted = 0
            for video in found.values():
                captured = observed_at[video.id]
                age = (captured - video.published_at) / 3600
                if not 0 <= age <= self.settings.max_video_age_hours:
                    rejected["age"] += 1
                    continue
                if not FILM_WORDS.search(video.caption + " " + " ".join(video.hashtags)):
                    rejected["topic"] += 1
                    continue
                language = language_relevance(video)
                if language < self.settings.language_threshold:
                    rejected["language"] += 1
                    continue
                # Track relevant subthreshold videos; they can cross MIN_VIEWS next scan.
                self.db.save_video(video, captured, language)
                velocity = growth(video, self.db.snapshots(video.id), captured)
                self.db.save_score(video.id, trend_score(video, velocity, captured, self.settings))
                accepted += 1
                if eligible(video, captured, self.settings):
                    self._record_tags(video, captured)
            if not self.db.enabled:
                return
            self.db.conn.execute(
                "DELETE FROM videos WHERE id NOT IN (SELECT id FROM videos "
                "ORDER BY updated_at DESC,latest_trend_score DESC LIMIT ?)",
                (self.settings.max_candidates,),
            )
            self.db.set_state("last_scan_at", str(started))
            available = successes > 0 and getattr(self.provider, "search_available", True)
            self.db.set_state("discovery_available", "1" if available else "0")
            self.db.set_state("scan_found", str(len(seen)))
            self.db.set_state("scan_rejected_age", str(rejected["age"]))
            self.db.set_state("scan_accepted", str(accepted))
            self.db.set_state("search_pages", str(getattr(self.provider, "search_pages", 0)))
            depth = getattr(self.provider, "depth_health", "")
            if budget_limited:
                depth += "; сбор остановлен по бюджету времени scan"
            self.db.set_state("search_depth", depth.lstrip("; "))
            limited = budget_limited or getattr(self.provider, "search_limited", False)
            self.db.set_state("discovery_limited", "1" if limited else "0")
            health = getattr(self.provider, "health", "ok")
            self.db.set_state(
                "provider_health",
                health if not failures else f"{health}; ошибок запросов: {failures}",
            )
            await self._propose_queries(started)
            log.info(
                "scan_complete raw=%d accepted=%d filtered=%s failures=%d",
                len(seen),
                accepted,
                rejected,
                failures,
            )
        except Exception:
            self.db.set_state("discovery_available", "0")
            self.db.set_state("provider_health", "Ошибка scan; подробности в журнале")
            log.exception("scan_failed")
        finally:
            if hasattr(self.provider, "end_scan"):
                await self.provider.end_scan()

    def _record_tags(self, video: Video, now: float) -> None:
        base = {q.lower().lstrip("#") for q in self.settings.base_queries}
        for tag in set(video.hashtags):
            tag = tag.lower().lstrip("#")
            if tag in base or not re.search(r"[а-яё]", tag) or not re.fullmatch(r"\w{2,50}", tag):
                continue
            self.db.conn.execute(
                "INSERT INTO hashtag_evidence VALUES (?,?,?) "
                "ON CONFLICT(tag,video_id) DO UPDATE SET observed_at=excluded.observed_at",
                (tag, video.id, now),
            )

    async def _propose_queries(self, now: float) -> None:
        popular = self.db.rows(
            "SELECT tag FROM hashtag_evidence WHERE observed_at>? "
            "GROUP BY tag HAVING COUNT(DISTINCT video_id)>=? LIMIT 5",
            (now - 86400, self.settings.query_threshold),
        )
        for row in popular:
            self.db.propose("#" + row["tag"], now, self.settings.query_cooldown_days)
        # Keep unsent proposals durable across a restart or transient Telegram failure.
        for query in self.db.rows("SELECT * FROM dynamic_queries WHERE status='pending'"):
            for user in self.db.rows("SELECT id FROM users"):
                uid = user["id"]
                if self.db.rows(
                    "SELECT 1 FROM query_messages WHERE query_id=? AND user_id=?",
                    (query["id"], uid),
                ):
                    continue
                markup = {
                    "inline_keyboard": [
                        [
                            {"text": "✅ Добавить", "callback_data": f"q:{query['id']}:yes"},
                            {"text": "❌ Отклонить", "callback_data": f"q:{query['id']}:no"},
                        ]
                    ]
                }
                try:
                    mid = await self.messenger.send_text(
                        uid, f"Предлагаю добавить поисковый запрос: {query['query']}", markup
                    )
                    self.db.conn.execute(
                        "INSERT OR IGNORE INTO query_messages VALUES (?,?,?)",
                        (query["id"], uid, mid),
                    )
                except Exception as error:
                    log.warning(
                        "query_notification_failed user=%d type=%s", uid, type(error).__name__
                    )

    async def decide_query(self, query_id: int, user_id: int, approve: bool) -> bool:
        won = self.db.decide_query(
            query_id, user_id, approve, time.time(), self.settings.query_ttl_hours
        )
        if won:
            row = self.db.rows("SELECT query FROM dynamic_queries WHERE id=?", (query_id,))[0]
            text = f"{row['query']}: {'добавлен на 24 часа' if approve else 'отклонён'}"
            for message in self.db.rows(
                "SELECT * FROM query_messages WHERE query_id=?", (query_id,)
            ):
                try:
                    await self.messenger.edit_text(message["user_id"], message["message_id"], text)
                except Exception as error:
                    log.warning("query_message_edit_failed type=%s", type(error).__name__)
            log.info("query_decided id=%d approved=%s user=%d", query_id, approve, user_id)
        return won

    async def _prepare(self, video: Video, now: float) -> tuple[Path | None, Source]:
        path = (self.settings.media_dir / f"{video.id}.mp4").resolve()
        # Provider IDs are data, never trusted as paths.
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", video.id):
            raise ValueError("Некорректный video ID")
        self.active_media.add(path)
        available = (
            path.exists()
            and now - path.stat().st_mtime
            < self.settings.media_retention_hours * 3600
            - self.settings.download_timeout
            - self.settings.source_timeout
            - 60
        )
        if not available:
            path.unlink(missing_ok=True)
            try:
                async with asyncio.timeout(self.settings.download_timeout):
                    await self.provider.download(video, path)
                available = True
            except Exception as error:
                log.warning("download_failed video=%s type=%s", video.id, type(error).__name__)
        source = self.db.source(video.id)
        if source is None:
            if video.subtitle_tracks and hasattr(self.provider, "get_subtitles"):
                try:
                    video.subtitles = await self.provider.get_subtitles(video)
                except Exception as error:
                    log.warning("subtitle_fetch_failed type=%s", type(error).__name__)
            try:
                async with asyncio.timeout(self.settings.source_timeout):
                    source = await self.identifier.identify(video, path if available else None)
            except Exception as error:
                log.warning("source_failed video=%s type=%s", video.id, type(error).__name__)
                source = Source()
            self.db.save_source(video.id, source)
        return path if available else None, source

    async def broadcast(self, user_id: int | None = None, now: float | None = None) -> None:
        if not self.db.enabled:
            return
        async with self.delivery_lock, self.work_lock:
            if not self.db.enabled:
                return
            timestamp = now if now is not None else time.time()
            users = (
                [user_id]
                if user_id is not None and self.db.user(user_id)
                else [r["id"] for r in self.db.rows("SELECT id FROM users WHERE mailing_enabled=1")]
                if user_id is None
                else []
            )
            if not users:
                return
            selected = self.ranked(timestamp, users)[:3]
            log.info(
                "broadcast_started users=%d selected=%s", len(users), [v.id for v, _, _ in selected]
            )
            for uid in users:
                if not any(not self.db.delivered(uid, v.id) for v, _, _ in selected):
                    try:
                        discovery = self.db.state("discovery_available", "unknown")
                        if discovery == "0":
                            empty_text = (
                                "Автоматический поиск TikTok недоступен. "
                                "Подборку сейчас сформировать не удалось; "
                                "бот повторит поиск на следующем цикле."
                            )
                        elif discovery == "unknown":
                            empty_text = "Первый автоматический поиск ещё не завершён."
                        else:
                            empty_text = "За последний цикл новых подходящих видео не найдено."
                        await self.messenger.send_text(uid, empty_text)
                    except Exception as error:
                        log.warning(
                            "empty_delivery_failed user=%d type=%s", uid, type(error).__name__
                        )
            try:
                for video, velocity, score in selected:
                    if not self.db.enabled:
                        return
                    path, source = await self._prepare(video, timestamp)
                    text = caption(video, velocity, score, source, timestamp)
                    for uid in users:
                        if not self.db.enabled or not self.db.user(uid):
                            continue
                        if user_id is None and not self.db.user(uid)["mailing_enabled"]:
                            continue
                        kind = "manual" if user_id is not None else "scheduled"
                        if not self.db.reserve_delivery(uid, video.id, time.time(), kind):
                            continue
                        try:
                            if path:
                                try:
                                    mid = await self.messenger.send_video(uid, path, text)
                                except TelegramRejected as error:
                                    if error.code != 400:
                                        raise
                                    mid = await self.messenger.send_text(
                                        uid, text + "\nMP4 отклонён Telegram; доступна ссылка."
                                    )
                            else:
                                mid = await self.messenger.send_text(
                                    uid, text + "\nНе удалось загрузить MP4; доступна ссылка."
                                )
                            self.db.finish_delivery(uid, video.id, mid)
                            log.info("delivery_complete user=%d video=%s", uid, video.id)
                        except TelegramRejected as error:
                            self.db.release_delivery(uid, video.id)
                            if error.code == 403:
                                self.db.pause(uid, True)
                            log.warning("delivery_rejected user=%d code=%d", uid, error.code)
                        except Exception as error:
                            # Preserve ambiguous reservations to prevent duplicates on restart.
                            log.warning(
                                "delivery_uncertain user=%d video=%s type=%s",
                                uid,
                                video.id,
                                type(error).__name__,
                            )
            finally:
                self.active_media.clear()
                if hasattr(self.provider, "end_scan"):
                    await self.provider.end_scan()
            log.info("broadcast_complete")
