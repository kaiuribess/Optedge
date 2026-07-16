# Purpose: Test read-only Robinhood quote caching.
"""Tests for the read-only Robinhood interactive research bridge."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import robinhood_research_bridge as bridge  # noqa: E402, I001


ASOF = datetime(2026, 7, 10, 21, 0, tzinfo=UTC)


def _equity_record(collected_at: str = "2026-07-10T20:55:00Z") -> dict:
    return {
        "request_id": "equity:AAPL",
        "query": "Apple",
        "symbol": "AAPL",
        "collected_at": collected_at,
        "equity_quote": {
            "quote": {
                "symbol": "AAPL",
                "last_trade_price": "210.00",
                "venue_last_trade_time": "2026-07-10T20:00:00Z",
                "last_non_reg_trade_price": "211.25",
                "venue_last_non_reg_trade_time": "2026-07-10T20:54:00Z",
                "bid_price": "211.20",
                "ask_price": "211.30",
                "state": "active",
            },
            "close": {"date": "2026-07-09", "price": "208.50", "interpolated": False},
        },
        "fundamentals": {
            "symbol": "AAPL",
            "market_cap": "3200000000000",
            "pe_ratio": "31.5",
            "pb_ratio": "42.0",
            "sector": "Electronic Technology",
            "industry": "Telecommunications Equipment",
            "volume": "50000000",
            "average_volume_30_days": "48000000",
            "high_52_weeks": "220",
            "low_52_weeks": "165",
            "float": "15000000000",
        },
        "earnings": [
            {
                "symbol": "AAPL",
                "year": 2026,
                "quarter": 3,
                "eps": {"estimate": "1.65", "actual": None},
                "report": {"date": "2026-07-30", "timing": "pm", "verified": True},
            }
        ],
        "equity_history": {
            "symbol": "AAPL",
            "bars": [
                {
                    "begins_at": f"2026-06-{day:02d}T00:00:00Z",
                    "close_price": str(180 + day),
                    "interpolated": False,
                }
                for day in range(1, 26)
            ],
        },
    }


def _option_record() -> dict:
    record = _equity_record()
    record.update(
        {
            "request_id": "option:AAPL|2026-12-18|call|220",
            "option_request": {
                "asset": "option",
                "ticker": "AAPL",
                "expiry": "2026-12-18",
                "side": "call",
                "strike": 220,
            },
            "option_contracts": [
                {
                    "instrument": {
                        "id": "opt-aapl-220",
                        "chain_symbol": "AAPL",
                        "expiration_date": "2026-12-18",
                        "strike_price": "220.0000",
                        "type": "call",
                        "state": "active",
                        "tradability": "tradable",
                        "sellout_datetime": "2026-12-18T19:30:00Z",
                    },
                    "quote": {
                        "quote": {
                            "instrument_id": "opt-aapl-220",
                            "mark_price": "9.00",
                            "bid_price": "8.90",
                            "ask_price": "9.10",
                            "bid_size": 12,
                            "ask_size": 8,
                            "volume": 1400,
                            "open_interest": 22000,
                            "implied_volatility": "0.31",
                            "delta": "0.52",
                            "gamma": "0.015",
                            "theta": "-0.04",
                            "vega": "0.22",
                            "chance_of_profit_long": "0.44",
                            "break_even_price": "229.00",
                            "low_fill_rate_buy_price": "8.95",
                            "high_fill_rate_buy_price": "9.08",
                            "updated_at": "2026-07-10T20:54:30Z",
                        },
                        "close": {"date": "2026-07-09", "price": "8.50", "interpolated": False},
                    },
                }
            ],
        }
    )
    return record


def test_equity_queue_writes_bounded_read_only_artifacts():
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)
        queued = bridge.queue_request("Apple", "AAPL", data_dir=data_dir, asof=ASOF)
        packet = json.loads((data_dir / bridge.REQUESTS_PATH.name).read_text(encoding="utf-8"))
        assert queued["request_id"] == "equity:AAPL"
        assert queued["status"] == "pending"
        assert packet["read_only"] is True
        assert packet["pending_count"] == 1
        assert "get_equity_quotes" in packet["requests"][0]["tools"]
        assert "get_option_quotes" not in packet["requests"][0]["tools"]
        assert (data_dir / bridge.PROMPT_PATH.name).exists()
        assert (data_dir / bridge.COVERAGE_PATH.name).exists()


def test_fresh_cache_satisfies_matching_request():
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)
        bridge._write_json(
            data_dir / bridge.SNAPSHOT_PATH.name,
            {
                "schema": bridge.SNAPSHOT_SCHEMA,
                "records": [_equity_record()],
            },
        )
        queued = bridge.queue_request("AAPL", "AAPL", data_dir=data_dir, asof=ASOF)
        assert queued["status"] == "satisfied"
        assert queued["cache_freshness"] == "fresh"


def test_equity_normalization_uses_newer_extended_quote_and_earnings():
    row = bridge.flatten_equity_record(_equity_record(), asof=ASOF)
    assert row["current_price"] == 211.25
    assert row["price_session"] == "extended"
    assert row["official_close"] == 208.5
    assert row["pe_ratio"] == 31.5
    assert row["next_earnings_date"] == "2026-07-30"
    assert row["days_to_earnings"] == 20
    assert row["snapshot_freshness"] == "fresh"
    assert row["broker_history_rows"] == 25
    assert row["broker_ret_20d"] is not None


def test_option_normalization_preserves_exact_liquidity_and_greeks():
    record = _option_record()
    row = bridge.flatten_option_contract(
        record["option_contracts"][0],
        record["collected_at"],
        asof=ASOF,
    )
    assert row["instrument_id"] == "opt-aapl-220"
    assert row["mark_price"] == 9.0
    assert row["spread_pct"] is not None and row["spread_pct"] < 0.03
    assert row["open_interest"] == 22000
    assert row["volume"] == 1400
    assert row["delta"] == 0.52
    assert row["implied_volatility"] == 0.31


def test_option_freshness_uses_upstream_quote_time_not_collection_time():
    record = _option_record()
    record["collected_at"] = ASOF.isoformat()
    record["option_contracts"][0]["quote"]["quote"]["updated_at"] = (
        ASOF - timedelta(hours=3)
    ).isoformat()
    row = bridge.flatten_option_contract(
        record["option_contracts"][0],
        record["collected_at"],
        asof=ASOF,
    )
    assert row["collection_age_min"] == 0
    assert row["quote_age_min"] == 180
    assert row["snapshot_freshness"] == "stale"


def test_lookup_sections_filters_to_exact_requested_contract():
    other = _option_record()
    other["request_id"] = "option:AAPL|2026-12-18|put|180"
    other["option_request"] = {
        "asset": "option",
        "ticker": "AAPL",
        "expiry": "2026-12-18",
        "side": "put",
        "strike": 180,
    }
    other["option_contracts"][0]["instrument"].update({"type": "put", "strike_price": "180"})
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)
        bridge._write_json(
            data_dir / bridge.SNAPSHOT_PATH.name,
            {
                "schema": bridge.SNAPSHOT_SCHEMA,
                "records": [_option_record(), other],
            },
        )
        sections = bridge.lookup_sections(
            "AAPL",
            {
                "asset": "option",
                "ticker": "AAPL",
                "expiry": "2026-12-18",
                "side": "call",
                "strike": 220,
            },
            data_dir=data_dir,
            asof=ASOF,
        )
        assert len(sections["robinhood_research"]) == 1
        assert len(sections["robinhood_option_quotes"]) == 1
        assert sections["robinhood_option_quotes"][0]["side"] == "call"
        assert sections["robinhood_option_quotes"][0]["strike"] == 220


def test_snapshot_merge_preserves_existing_records():
    existing = {"records": [_equity_record()]}
    incoming = {"records": [_option_record()]}
    merged = bridge.merge_snapshot_payload(existing, incoming, asof=ASOF)
    assert len(merged["records"]) == 2
    assert {row["request_id"] for row in merged["records"]} == {
        "equity:AAPL",
        "option:AAPL|2026-12-18|call|220",
    }


if __name__ == "__main__":
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"robinhood research bridge tests passed ({len(tests)})")
