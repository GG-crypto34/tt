import copy
import math
import time

import pytest

from app.models import Metrics
from app.ranking import eligible, growth, language_relevance, trend_score


def test_first_snapshot_and_measured_velocity(system):
    video = system.provider.videos[0]
    now = time.time()
    first = growth(video, [(now, Metrics(30000, 3000, 300, 100))], now)
    assert not first.measured and first.confidence < 1
    snapshots = [(now - 900, Metrics(10000, 1000, 100, 10)), (now, Metrics(20000, 1500, 150, 20))]
    measured = growth(video, snapshots, now)
    assert measured.measured and measured.views == 40000 and measured.likes == 2000


def test_negative_counters_and_small_interval(system):
    video = system.provider.videos[0]
    now = time.time()
    assert growth(video, [(now - 900, Metrics(30000)), (now, Metrics(20000))], now).views == 0
    assert not growth(video, [(now - 1, Metrics(10000)), (now, Metrics(20000))], now).measured
    assert (
        growth(
            video, [(now - 900, Metrics()), (now - 1, Metrics(10000)), (now, Metrics(20000))], now
        ).views
        == 80000
    )


@pytest.mark.parametrize(
    "age,views,result",
    [
        (24, 10000, True),
        (24.01, 10000, False),
        (-1, 10000, False),
        (1, 9999, False),
        (1, 10000, True),
    ],
)
def test_age_and_views_filter(system, age, views, result):
    now = time.time()
    video = copy.deepcopy(system.provider.videos[0])
    video.published_at = now - age * 3600
    video.metrics.views = views
    assert eligible(video, now, system.settings) is result


def test_russian_relevance_and_topic(system):
    video = copy.deepcopy(system.provider.videos[0])
    assert language_relevance(video) >= 0.45
    video.caption, video.hashtags = "Movie scene", []
    assert language_relevance(video) == 0
    video.subtitles = "Русский текст субтитров"
    assert language_relevance(video) >= 0.45
    video.caption = "Рецепт вкусного пирога"
    assert not eligible(video, time.time(), system.settings)


def test_velocity_beats_absolute_popularity_and_outliers(system):
    now = time.time()
    slow = copy.deepcopy(system.provider.videos[0])
    fast = copy.deepcopy(slow)
    slow.metrics = Metrics(1000000, 10000, 1000, 500)
    fast.metrics = Metrics(100000, 10000, 1000, 500)
    slow_growth = growth(slow, [(now - 900, Metrics(999900, 9990)), (now, slow.metrics)], now)
    fast_growth = growth(fast, [(now - 900, Metrics(75000, 8000)), (now, fast.metrics)], now)
    assert trend_score(fast, fast_growth, now, system.settings) > trend_score(
        slow, slow_growth, now, system.settings
    )
    fast_growth.views = 10**30
    score = trend_score(fast, fast_growth, now, system.settings)
    assert math.isfinite(score) and 0 <= score <= 100


def test_zero_values_and_weights(system):
    video = copy.deepcopy(system.provider.videos[0])
    video.metrics = Metrics()
    score = trend_score(video, growth(video, [], time.time()), time.time(), system.settings)
    assert 0 <= score <= 100
    system.settings.ranking_weights = {k: 0 for k in system.settings.ranking_weights}
    with pytest.raises(ValueError, match="RANKING_WEIGHTS"):
        system.settings.validate(live=False)
