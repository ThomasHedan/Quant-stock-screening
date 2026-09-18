"""Research access: read-only queries, the row cap and the holdout guard."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from app import research
from app.core.timeutils import UTC
from app.storage import lake

NOW = datetime(2026, 3, 20, 12, 0, tzinfo=UTC)
LIMITS = research.ResearchLimits(timeout_seconds=10, row_cap=100, holdout_fraction=0.30)


@pytest.fixture
def populated(tmp_path: Path) -> Path:
    """A small lake with ten days of evaluations."""
    root = tmp_path / "lake"
    writer = lake.LakeWriter(root=root, source="tradingview")
    for offset in range(10):
        day = date(2026, 3, 2) + timedelta(days=offset)
        for index in range(5):
            writer.append(
                "evaluations",
                day,
                {
                    "ticker": f"T{index}",
                    "date": day,
                    "poll_ts_utc": NOW,
                    "window_start_utc": NOW,
                    "price": 5.0 + index,
                    "gap_pct": 10.0 * index,
                    "rvol": 2.0 * index,
                    "float_confidence": "low" if index == 0 else "high",
                    "pillar_1_status": "pass",
                    "pillar_2_status": "fail",
                    "pillar_3_status": "fail",
                    "pillar_4_status": "pass",
                    "pillar_5_status": "unknown",
                    "pillars_passed": 2,
                    "pillars_unknown": 1,
                    "tier": "none",
                    "written_at_utc": NOW,
                },
            )
    writer.flush(now=NOW)
    return root


# --- holdout -----------------------------------------------------------------


def test_cutoff_holds_back_the_most_recent_fraction():
    days = tuple(date(2026, 3, 1) + timedelta(days=i) for i in range(10))
    assert research.holdout_cutoff(days, 0.30) == date(2026, 3, 8)


def test_cutoff_moves_forward_as_history_accumulates():
    short = tuple(date(2026, 3, 1) + timedelta(days=i) for i in range(10))
    longer = tuple(date(2026, 3, 1) + timedelta(days=i) for i in range(30))
    assert research.holdout_cutoff(longer, 0.30) > research.holdout_cutoff(short, 0.30)


def test_no_history_means_no_cutoff():
    assert research.holdout_cutoff((), 0.30) is None


def test_an_impossible_fraction_is_rejected():
    with pytest.raises(ValueError, match="holdout_fraction"):
        research.holdout_cutoff((date(2026, 3, 1),), 1.5)


def test_a_query_naming_a_holdout_date_is_refused():
    with pytest.raises(research.QueryRejectedError, match="holdout"):
        research.check_query(
            "SELECT * FROM evaluations WHERE date = '2026-03-09'", cutoff=date(2026, 3, 8)
        )


def test_a_query_on_earlier_dates_is_allowed():
    research.check_query(
        "SELECT * FROM evaluations WHERE date < '2026-03-05'", cutoff=date(2026, 3, 8)
    )


def test_an_unfiltered_query_is_allowed():
    """Most exploratory queries are aggregate; refusing them makes the page useless."""
    research.check_query("SELECT count(*) FROM evaluations", cutoff=date(2026, 3, 8))


def test_the_override_is_logged(caplog):
    with caplog.at_level("WARNING"):
        research.check_query(
            "SELECT * FROM evaluations WHERE date = '2026-03-09'",
            cutoff=date(2026, 3, 8),
            allow_holdout=True,
        )
    assert "Holdout override" in caplog.text


# --- read-only enforcement ---------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM evaluations",
        "DROP VIEW evaluations",
        "INSERT INTO evaluations VALUES (1)",
        "COPY evaluations TO 'out.csv'",
        "INSTALL httpfs",
        "UPDATE evaluations SET tier = 'A'",
    ],
)
def test_write_statements_are_refused(sql):
    with pytest.raises(research.QueryRejectedError):
        research.check_query(sql, cutoff=None)


def test_a_non_select_is_refused():
    with pytest.raises(research.QueryRejectedError, match="SELECT"):
        research.check_query("SHOW TABLES", cutoff=None)


def test_a_cte_is_allowed():
    research.check_query("WITH x AS (SELECT 1) SELECT * FROM x", cutoff=None)


def test_an_empty_query_is_refused():
    with pytest.raises(research.QueryRejectedError, match="empty"):
        research.check_query("   ", cutoff=None)


# --- running -----------------------------------------------------------------


def test_a_query_runs_against_the_lake(populated):
    columns, rows = research.run_query(
        populated,
        "SELECT ticker, count(*) AS n FROM evaluations GROUP BY 1 ORDER BY 1",
        limits=LIMITS,
        cutoff=None,
    )
    assert columns == ["ticker", "n"]
    assert len(rows) == 5
    assert rows[0][1] == 10


def test_results_are_capped(populated):
    limits = research.ResearchLimits(timeout_seconds=10, row_cap=7, holdout_fraction=0.3)
    _columns, rows = research.run_query(
        populated, "SELECT * FROM evaluations", limits=limits, cutoff=None
    )
    assert len(rows) == 7


def test_an_empty_lake_is_reported_clearly(tmp_path: Path):
    with pytest.raises(research.QueryRejectedError, match="empty"):
        research.run_query(tmp_path / "nothing", "SELECT 1", limits=LIMITS, cutoff=None)


def test_hive_partitioning_lets_a_query_filter_on_date(populated):
    _columns, rows = research.run_query(
        populated,
        "SELECT count(*) FROM evaluations WHERE date = '2026-03-03'",
        limits=LIMITS,
        cutoff=None,
    )
    assert rows[0][0] == 5


# --- presentation ------------------------------------------------------------


def test_table_stats_cover_every_declared_table(populated):
    stats = {stat.name: stat for stat in research.table_stats(populated)}
    assert stats["evaluations"].days == 10
    assert stats["evaluations"].size_bytes > 0
    assert stats["runners"].days == 0  # declared but not yet written


def test_reweight_note_states_the_multiplier():
    note = research.reweight_note(10)
    assert "10%" in note
    assert "10x" in note


def test_csv_export_includes_the_header():
    csv_text = research.to_csv(["a", "b"], [(1, 2), (3, 4)])
    assert csv_text.splitlines()[0] == "a,b"
    assert csv_text.splitlines()[1] == "1,2"


def test_starter_queries_lead_with_descriptives():
    titles = list(research.starter_queries())
    assert "When do moves actually start?" in titles[0]


def test_describe_cutoff_without_history():
    assert "No history" in research.describe_cutoff(None, now=NOW)


def test_describe_cutoff_names_the_date():
    text = research.describe_cutoff(date(2026, 3, 8), now=NOW)
    assert "2026-03-08" in text
    assert "override" in text
