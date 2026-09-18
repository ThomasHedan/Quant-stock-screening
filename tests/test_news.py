"""News freshness, the point-in-time cache and the pillar-3 interaction."""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest
from app.core import news
from app.core.news import Freshness, NewsCache
from app.core.timeutils import ET, UTC
from app.core.types import NewsItem

AS_OF = datetime(2026, 3, 10, 12, 5, tzinfo=UTC)  # 08:05 ET


def item(
    minutes_old: float,
    *,
    news_id: str = "n1",
    symbols: tuple[str, ...] = ("ABCD",),
    latency_s: float = 2.0,
) -> NewsItem:
    created = AS_OF - timedelta(minutes=minutes_old)
    return NewsItem(
        news_id=news_id,
        symbols=symbols,
        headline="Positive topline results",
        source="benzinga",
        url=None,
        created_at=created,
        received_at=created + timedelta(seconds=latency_s),
    )


# --- freshness ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (1, Freshness.FRESH),
        (15, Freshness.FRESH),  # inclusive boundary
        (15.1, Freshness.TODAY),
        (None, Freshness.NONE),
    ],
)
def test_classification_by_age(age, expected):
    subject = None if age is None else item(age)
    assert news.classify(subject, as_of=AS_OF, fresh_minutes=15) is expected


def test_future_dated_news_is_stale_not_fresh(caplog):
    """A clock skew on the feed side must not manufacture a catalyst."""
    with caplog.at_level("WARNING"):
        verdict = news.classify(item(-5), as_of=AS_OF, fresh_minutes=15)
    assert verdict is Freshness.STALE
    assert "future" in caplog.text


def test_yesterdays_afternoon_news_counts_as_today_premarket():
    """A 16:30 announcement is the catalyst for this morning's gap."""
    yesterday_1630 = datetime(2026, 3, 9, 16, 30, tzinfo=ET).astimezone(UTC)
    age = (AS_OF - yesterday_1630).total_seconds() / 60
    assert news.classify(item(age), as_of=AS_OF, fresh_minutes=15) is Freshness.TODAY


def test_the_day_before_yesterday_is_stale():
    old = datetime(2026, 3, 8, 16, 30, tzinfo=ET).astimezone(UTC)
    age = (AS_OF - old).total_seconds() / 60
    assert news.classify(item(age), as_of=AS_OF, fresh_minutes=15) is Freshness.STALE


def test_session_news_start_switches_at_the_regular_open():
    premarket = datetime(2026, 3, 10, 8, 5, tzinfo=ET).astimezone(UTC)
    regular = datetime(2026, 3, 10, 10, 0, tzinfo=ET).astimezone(UTC)
    kwargs = {"day_start": time(4, 0), "prev_day_start": time(16, 0)}
    start_pre = news.session_news_start(premarket, **kwargs)
    start_reg = news.session_news_start(regular, **kwargs)
    assert (start_pre.astimezone(ET).day, start_pre.astimezone(ET).hour) == (9, 16)
    assert (start_reg.astimezone(ET).day, start_reg.astimezone(ET).hour) == (10, 4)


def test_postmarket_news_window_starts_at_0400_today():
    """After the open, yesterday afternoon is no longer today's catalyst."""
    postmarket = datetime(2026, 3, 10, 16, 5, tzinfo=ET).astimezone(UTC)
    yesterday_1630 = datetime(2026, 3, 9, 16, 30, tzinfo=ET).astimezone(UTC)
    stale = NewsItem(
        news_id="y",
        symbols=("ABCD",),
        headline="Yesterday",
        source="benzinga",
        url=None,
        created_at=yesterday_1630,
        received_at=yesterday_1630,
    )
    assert news.classify(stale, as_of=postmarket, fresh_minutes=15) is Freshness.STALE


# --- the cache ---------------------------------------------------------------


def test_add_and_retrieve():
    cache = NewsCache()
    assert cache.add(item(2))
    assert cache.latest_for("ABCD", as_of=AS_OF) is not None
    assert cache.size == 1


def test_duplicate_ids_are_ignored():
    """The WS and the REST backfill overlap by design after a reconnect."""
    cache = NewsCache()
    cache.add(item(2))
    assert not cache.add(item(2))
    assert cache.size == 1


def test_lookup_is_case_insensitive_on_the_ticker():
    cache = NewsCache()
    cache.add(item(2, symbols=("abcd",)))
    assert cache.latest_for("ABCD", as_of=AS_OF) is not None


def test_an_article_from_the_future_of_the_query_is_invisible():
    """A backfill inserting an 08:02 article must not be visible at 08:01."""
    cache = NewsCache()
    cache.add(item(-1, news_id="later"))  # created one minute after AS_OF
    assert cache.latest_for("ABCD", as_of=AS_OF) is None


def test_latest_picks_the_most_recent_visible_article():
    cache = NewsCache()
    cache.add(item(40, news_id="old"))
    cache.add(item(3, news_id="new"))
    latest = cache.latest_for("ABCD", as_of=AS_OF)
    assert latest is not None
    assert latest.news_id == "new"


def test_fresh_for_applies_the_freshness_rule():
    cache = NewsCache()
    cache.add(item(40, news_id="stale"))
    assert cache.fresh_for("ABCD", as_of=AS_OF, fresh_minutes=15) is None
    cache.add(item(5, news_id="fresh"))
    fresh = cache.fresh_for("ABCD", as_of=AS_OF, fresh_minutes=15)
    assert fresh is not None
    assert fresh.news_id == "fresh"


def test_first_for_finds_the_days_opening_headline():
    cache = NewsCache()
    cache.add(item(200, news_id="first"))
    cache.add(item(5, news_id="second"))
    first = cache.first_for("ABCD", on=AS_OF)
    assert first is not None
    assert first.news_id == "first"


def test_multi_symbol_articles_are_indexed_under_each():
    cache = NewsCache()
    cache.add(item(2, symbols=("ABCD", "EFGH")))
    assert cache.symbols() == frozenset({"ABCD", "EFGH"})
    assert cache.size == 1


def test_pruning_bounds_the_cache():
    cache = NewsCache()
    cache.add(item(300, news_id="old"))
    cache.add(item(5, news_id="recent"))
    dropped = cache.prune_before(AS_OF - timedelta(hours=1))
    assert dropped == 1
    assert cache.size == 1


def test_pruning_everything_empties_the_index():
    cache = NewsCache()
    cache.add(item(5))
    assert cache.prune_before(AS_OF) == 1
    assert cache.symbols() == frozenset()


# --- latency and rows --------------------------------------------------------


def test_feed_latency_is_measured_from_created_to_received():
    assert news.feed_latency_seconds(item(5, latency_s=12.0)) == pytest.approx(12.0)


def test_negative_latency_is_reported_not_clamped():
    """A feed clock disagreeing with ours belongs on the dashboard."""
    assert news.feed_latency_seconds(item(5, latency_s=-30.0)) == pytest.approx(-30.0)


def test_news_row_keeps_updated_at_but_freshness_never_reads_it():
    subject = NewsItem(
        news_id="n1",
        symbols=("ABCD",),
        headline="Revised headline",
        source="benzinga",
        url=None,
        created_at=AS_OF - timedelta(hours=6),
        received_at=AS_OF - timedelta(hours=6),
        updated_at=AS_OF,  # revised just now
    )
    row = news.news_row(subject, now=AS_OF)
    assert row["updated_at_utc"] == AS_OF
    # ... and the revision does not make it fresh.
    assert news.classify(subject, as_of=AS_OF, fresh_minutes=15) is Freshness.TODAY


def test_news_row_dates_by_et_trading_date():
    evening = datetime(2026, 1, 6, 19, 30, tzinfo=ET).astimezone(UTC)
    subject = NewsItem(
        news_id="n1",
        symbols=("ABCD",),
        headline="After hours",
        source="benzinga",
        url=None,
        created_at=evening,
        received_at=evening,
    )
    assert str(news.news_row(subject, now=evening)["date"]) == "2026-01-06"
