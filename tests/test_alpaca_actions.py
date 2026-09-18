"""Corporate-action parsing, and the Alpaca request plumbing around it."""

from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest
from app.core.timeutils import UTC
from app.sources import alpaca, alpaca_actions
from app.sources.retry import RetryPolicy, SourceError

DAY = date(2026, 3, 10)
NOW = datetime(2026, 3, 10, 20, 10, tzinfo=UTC)
POLICY = RetryPolicy(
    max_retries=1,
    base_seconds=0.01,
    max_seconds=0.01,
    timeout_seconds=5.0,
    connect_timeout_seconds=2.0,
)
CREDS = alpaca.AlpacaCredentials(key_id="AKTESTKEY1234", secret_key="secret-value")


# --- credentials -------------------------------------------------------------


def test_credentials_repr_is_redacted():
    rendered = f"{CREDS!r} {CREDS}"
    assert "secret-value" not in rendered
    assert "AKTESTKEY1234" not in rendered
    assert "1234" in rendered  # enough to tell two keys apart


def test_headers_carry_both_keys():
    headers = CREDS.headers()
    assert headers["APCA-API-KEY-ID"] == "AKTESTKEY1234"
    assert headers["APCA-API-SECRET-KEY"] == "secret-value"


# --- parsing -----------------------------------------------------------------


def test_reverse_split_ratio_is_new_over_old():
    action = alpaca_actions.parse_action(
        "reverse_split",
        {"symbol": "ABCD", "ex_date": "2026-03-10", "old_rate": 10, "new_rate": 1},
    )
    assert action.ratio == pytest.approx(0.1)
    assert action.is_reverse_split


def test_forward_split_ratio_is_above_one():
    action = alpaca_actions.parse_action(
        "forward_split",
        {"symbol": "EFGH", "ex_date": "2026-03-10", "old_rate": 1, "new_rate": 3},
    )
    assert action.ratio == pytest.approx(3.0)
    assert not action.is_reverse_split


def test_unusable_rates_leave_the_ratio_none(caplog):
    with caplog.at_level("WARNING"):
        action = alpaca_actions.parse_action(
            "reverse_split",
            {"symbol": "ABCD", "ex_date": "2026-03-10", "old_rate": "many", "new_rate": 1},
        )
    assert action.ratio is None
    assert "Unusable split rates" in caplog.text


def test_symbol_change_carries_both_symbols():
    action = alpaca_actions.parse_action(
        "name_change",
        {"ex_date": "2026-03-10", "old_symbol": "OLDX", "new_symbol": "NEWX"},
    )
    assert action.action_type == "symbol_change"
    assert (action.old_symbol, action.new_symbol) == ("OLDX", "NEWX")


def test_an_unknown_action_type_is_kept_under_its_own_name():
    """An unmapped type is still evidence something happened to that ticker."""
    action = alpaca_actions.parse_action("spinoff", {"symbol": "ABCD", "ex_date": "2026-03-10"})
    assert action.action_type == "spinoff"


def test_a_record_without_a_date_is_rejected():
    with pytest.raises(ValueError, match="effective date"):
        alpaca_actions.parse_action("reverse_split", {"symbol": "ABCD"})


def test_a_record_without_a_symbol_is_rejected():
    with pytest.raises(ValueError, match="symbol"):
        alpaca_actions.parse_action("reverse_split", {"ex_date": "2026-03-10"})


def test_one_bad_record_does_not_discard_the_rest():
    payload = {
        "corporate_actions": {
            "reverse_splits": [
                {"symbol": "ABCD", "ex_date": "2026-03-10", "old_rate": 10, "new_rate": 1},
                {"symbol": "BROKEN"},
            ]
        }
    }
    result = alpaca_actions.parse_payload(payload)
    assert len(result.actions) == 1
    assert result.unparsable == 1
    assert result.errors


def test_lake_rows_carry_the_effective_date():
    actions = alpaca_actions.mock_actions(DAY).actions
    rows = alpaca_actions.action_rows(actions, DAY, now=NOW)
    assert rows[0]["effective_date"] == DAY
    assert rows[0]["action_type"] == "reverse_split"
    assert rows[0]["ratio"] == pytest.approx(0.1)


def test_default_window_reaches_into_the_future():
    """Knowing about tomorrow's split before it happens is the whole point."""
    start, end = alpaca_actions.default_window(DAY)
    assert start < DAY < end
    assert (end - DAY).days == 5


# --- HTTP plumbing -----------------------------------------------------------


def client_returning(*responses: httpx.Response) -> httpx.Client:
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return queue.pop(0)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_actions_parses_a_single_page():
    payload = {
        "corporate_actions": {
            "reverse_splits": [
                {"symbol": "ABCD", "ex_date": "2026-03-10", "old_rate": 10, "new_rate": 1}
            ]
        }
    }
    with client_returning(httpx.Response(200, json=payload)) as client:
        result = alpaca_actions.fetch_actions(
            CREDS, start=DAY, end=DAY, policy=POLICY, client=client
        )
    assert [a.ticker for a in result.actions] == ["ABCD"]


def test_fetch_actions_follows_the_page_cursor():
    first = {
        "corporate_actions": {"reverse_splits": [{"symbol": "A", "ex_date": "2026-03-10"}]},
        "next_page_token": "abc",
    }
    second = {"corporate_actions": {"reverse_splits": [{"symbol": "B", "ex_date": "2026-03-10"}]}}
    with client_returning(
        httpx.Response(200, json=first), httpx.Response(200, json=second)
    ) as client:
        result = alpaca_actions.fetch_actions(
            CREDS, start=DAY, end=DAY, policy=POLICY, client=client
        )
    assert {a.ticker for a in result.actions} == {"A", "B"}


def test_a_server_error_is_retried_then_raises():
    responses = [httpx.Response(503) for _ in range(POLICY.max_retries + 1)]
    with client_returning(*responses) as client, pytest.raises(SourceError) as excinfo:
        alpaca_actions.fetch_actions(CREDS, start=DAY, end=DAY, policy=POLICY, client=client)
    assert excinfo.value.transient is True


def test_an_auth_error_is_not_retried():
    with client_returning(httpx.Response(401)) as client, pytest.raises(SourceError) as excinfo:
        alpaca_actions.fetch_actions(CREDS, start=DAY, end=DAY, policy=POLICY, client=client)
    assert excinfo.value.transient is False


def test_an_inverted_window_is_rejected():
    with pytest.raises(ValueError, match="precedes"):
        alpaca_actions.fetch_actions(
            CREDS, start=DAY, end=DAY.replace(day=9), policy=POLICY, client=None
        )


def test_mock_actions_include_a_reverse_split_and_a_rename():
    actions = alpaca_actions.mock_actions(DAY).actions
    assert any(a.is_reverse_split for a in actions)
    assert any(a.action_type == "symbol_change" for a in actions)
