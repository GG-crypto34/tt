import math
import re
from dataclasses import dataclass

from app.config import Settings
from app.models import Metrics, Video

FILM_WORDS = re.compile(r"фильм|сериал|кино|нарезк|сцен[ауы]|эпизод|movie|series", re.I)
RUSSIAN = re.compile(r"[а-яё]", re.I)


def language_relevance(video: Video) -> float:
    """Evidence score, not a statistical probability. No media download required."""
    caption = re.sub(r"#\S+", "", video.caption)
    letters = re.findall(r"[^\W\d_]", caption, re.UNICODE)
    ratio = len(RUSSIAN.findall(caption)) / max(1, len(letters))
    score = min(0.65, ratio * 0.65)
    if any(RUSSIAN.search(tag) for tag in video.hashtags):
        score += 0.45
    if RUSSIAN.search(video.subtitles) or any(
        track.get("language", "").lower().startswith("ru") for track in video.subtitle_tracks
    ):
        score += 0.5
    return min(1.0, score)


def eligible(video: Video, now: float, settings: Settings) -> bool:
    age = (now - video.published_at) / 3600
    return (
        0 <= age <= settings.max_video_age_hours
        and video.metrics.views >= settings.min_views
        and bool(FILM_WORDS.search(video.caption + " " + " ".join(video.hashtags)))
        and language_relevance(video) >= settings.language_threshold
    )


@dataclass
class Growth:
    views: float
    likes: float
    shares: float
    comments: float
    confidence: float
    measured: bool


def growth(video: Video, snapshots: list[tuple[float, Metrics]], now: float) -> Growth:
    """Latest independent observation; skip <60s intervals and clip negative counters."""
    current_time, current = snapshots[-1] if snapshots else (now, video.metrics)
    previous = next((s for s in reversed(snapshots[:-1]) if current_time - s[0] >= 60), None)
    if previous:
        hours = (current_time - previous[0]) / 3600
        rates = [
            max(0, getattr(current, k) - getattr(previous[1], k)) / hours
            for k in ("views", "likes", "shares", "comments")
        ]
        return Growth(*rates, confidence=1.0, measured=True)
    hours = max(0.25, (current_time - video.published_at) / 3600)
    return Growth(
        *(max(0, getattr(current, k)) / hours for k in ("views", "likes", "shares", "comments")),
        confidence=0.55,
        measured=False,
    )


def trend_score(video: Video, velocity: Growth, now: float, settings: Settings) -> float:
    """Fixed log scales prevent huge outliers changing other candidates' scores.

    A 100k views/hour or 10k likes/hour reaches the clipped velocity maximum.
    Rates use bounded denominators. Engagement tempers both velocity terms.
    First observations receive a .55 confidence multiplier on growth; missing
    follower counts get neutral breakout, rather than pretending to be a tiny account.
    """
    m = video.metrics

    def logscale(x: float, cap: float) -> float:
        return min(1.0, math.log1p(max(0, x)) / math.log1p(cap))

    engagement = min(1.0, (m.likes + m.shares + m.comments) / max(1, m.views) / 0.1)
    values = {
        "views": logscale(velocity.views, 100000) * velocity.confidence * (0.8 + 0.2 * engagement),
        "likes": logscale(velocity.likes, 10000) * velocity.confidence * (0.8 + 0.2 * engagement),
        "shares": min(1.0, max(0, m.shares) / max(1, m.views) / 0.03),
        "comments": min(1.0, max(0, m.comments) / max(1, m.views) / 0.01),
        "freshness": max(0.0, 1 - (now - video.published_at) / 3600 / settings.max_video_age_hours),
        "breakout": logscale(m.views / max(100, video.followers), 100)
        if video.followers is not None
        else 0.5,
    }
    weights = settings.ranking_weights
    return round(100 * sum(values[k] * w for k, w in weights.items()) / sum(weights.values()), 2)
