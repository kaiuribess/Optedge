from pathlib import Path
import sys
import json
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from scripts.export_external_paper_track import build_external_orders, export_candidates
from scripts.export_robinhood_agentic_queue import build_robinhood_queue


def _option(**overrides):
    row = {
        "ticker": "AAPL",
        "contract": "AAPL 2026-09-18 C 200",
        "side": "call",
        "strike": 200,
        "expiry": "2026-09-18",
        "mid": 2.5,
        "suggested_contracts": 1,
        "actual_dollars": 250,
        "stop_price": 1.25,
        "target_price": 5.0,
        "confidence": 70,
        "rank_score": 2.0,
        "fused_score": 1.5,
        "trade_status": "Trade",
        "spread_pct": 0.04,
    }
    row.update(overrides)
    return row


def _share(**overrides):
    row = {
        "ticker": "XPEV",
        "spot": 10.0,
        "suggested_dollars": 550,
        "stop_pct": -0.08,
        "target_pct": 0.18,
        "confidence": 72,
        "rank_score": 1.2,
        "share_score": 1.2,
        "trade_status": "Trade",
    }
    row.update(overrides)
    return row


def _future(**overrides):
    row = {
        "symbol": "ES=F",
        "contract": "/MES",
        "direction": "long",
        "entry_price": 5000.0,
        "stop_price": 4950.0,
        "target_price": 5100.0,
        "point_value": 5.0,
        "suggested_contracts": 1,
        "risk_dollars": 250,
        "reward_dollars": 500,
        "confidence": 65,
        "rank_score": 1.4,
        "futures_score": 1.4,
        "trade_status": "Trade",
    }
    row.update(overrides)
    return row


def _export(options=None, shares=None, futures=None, **kwargs):
    return export_candidates(
        options=pd.DataFrame(options or []),
        shares=pd.DataFrame(shares or []),
        futures=pd.DataFrame(futures or []),
        generated_at="2026-05-19T00:00:00+00:00",
        **kwargs,
    )


def test_excludes_zero_contract_options_by_default():
    out = _export(options=[_option(suggested_contracts=0)])
    assert out.empty


def test_excludes_watch_trades_by_default():
    out = _export(options=[_option(trade_status="Watch")])
    assert out.empty


def test_includes_watch_only_with_include_watch():
    out = _export(options=[_option(trade_status="Watch")], include_watch=True)
    assert len(out) == 1
    assert out.loc[0, "trade_status"] == "Watch"


def test_caps_max_new_orders():
    opts = [_option(ticker=f"T{i}", contract=f"T{i} 2026-09-18 C 10", rank_score=10 - i) for i in range(7)]
    out = _export(options=opts, max_new=3, max_options=10)
    assert len(out) == 3


def test_normalizes_options_correctly():
    out = _export(options=[_option()])
    row = out.iloc[0]
    assert row["asset"] == "option"
    assert row["ticker_or_symbol"] == "AAPL"
    assert row["action"] == "BUY_TO_OPEN"
    assert row["direction"] == "long_call"
    assert row["quantity"] == 1
    assert row["option_side"] == "call"
    assert row["entry_price"] == 2.5


def test_normalizes_shares_correctly():
    out = _export(shares=[_share()])
    row = out.iloc[0]
    assert row["asset"] == "share"
    assert row["ticker_or_symbol"] == "XPEV"
    assert row["action"] == "BUY"
    assert row["direction"] == "long"
    assert row["quantity"] == 55
    assert row["stop_price"] == 9.2


def test_normalizes_futures_correctly():
    out = _export(futures=[_future()])
    row = out.iloc[0]
    assert row["asset"] == "futures"
    assert row["ticker_or_symbol"] == "ES=F"
    assert row["action"] == "BUY_TO_OPEN"
    assert row["direction"] == "long"
    assert row["quantity"] == 1
    assert row["contract"] == "/MES"


def test_dry_run_includes_exclusion_reasons():
    out = _export(options=[_option(suggested_contracts=0)], dry_run=True)
    assert len(out) == 1
    assert "suggested_contracts <= 0" in out.loc[0, "reason_excluded"]


def test_excludes_short_dated_options_by_default():
    out = _export(options=[_option(expiry="2026-06-18", contract="AAPL 2026-06-18 C 200")], dry_run=True)
    assert len(out) == 1
    assert "dte below 90" in out.loc[0, "reason_excluded"]


def test_query_filters_to_matching_ticker_or_contract():
    out = _export(
        options=[
            _option(ticker="AAPL", contract="AAPL 2026-09-18 C 200"),
            _option(ticker="MSFT", contract="MSFT 2026-09-18 C 500", rank_score=5.0),
        ],
        shares=[_share(ticker="NVDA", rank_score=10.0)],
        query="AAPL 20260918 C 200",
        max_new=5,
        max_options=5,
        max_shares=5,
    )
    assert len(out) == 1
    assert out.loc[0, "ticker_or_symbol"] == "AAPL"


def test_build_external_orders_includes_chain_shortlist_candidates():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        generated_at = datetime.now(timezone.utc).isoformat()
        (data_dir / "option_chain_shortlist.json").write_text(json.dumps({
            "generated_at": generated_at,
            "rows": [{
                "generated_at": generated_at,
                "symbol": "AAPL",
                "contract_query": "AAPL 2027-01-15 C 220",
                "side": "call",
                "expiry": "2027-01-15",
                "strike": 220.0,
                "dte": 216,
                "mid": 1.20,
                "premium_dollars": 120.0,
                "spread_pct": 0.04,
                "openInterest": 1200,
                "contract_grade": "A",
                "readiness_label": "ready",
                "readiness_score": 91,
                "contract_quality_score": 94,
                "chain_source": "cboe",
                "quote_quality": "free_or_delayed",
            }],
        }), encoding="utf-8")

        out = build_external_orders(data_dir, asset="option", query="AAPL", max_options=3)

    assert len(out) == 1
    row = out.iloc[0]
    assert row["ticker_or_symbol"] == "AAPL"
    assert row["contract"] == "AAPL 2027-01-15 C 220"
    assert row["quantity"] == 1
    assert row["entry_price"] == 1.2
    assert row["stop_price"] == 0.6
    assert row["target_price"] == 2.4
    assert "chain shortlist" in row["reason_selected"]
    assert "chain-shortlist" in row["notes"]


def test_robinhood_queue_uses_chain_shortlist_when_no_top_options_exist():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        generated_at = datetime.now(timezone.utc).isoformat()
        (data_dir / "option_chain_shortlist.json").write_text(json.dumps({
            "generated_at": generated_at,
            "rows": [{
                "generated_at": generated_at,
                "symbol": "MSFT",
                "contract_query": "MSFT 2027-01-15 C 500",
                "side": "call",
                "expiry": "2027-01-15",
                "strike": 500.0,
                "dte": 216,
                "mid": 1.10,
                "premium_dollars": 110.0,
                "spread_pct": 0.03,
                "openInterest": 1500,
                "contract_grade": "A",
                "readiness_label": "ready",
                "readiness_score": 92,
                "contract_quality_score": 95,
                "chain_source": "cboe",
                "quote_quality": "free_or_delayed",
            }],
        }), encoding="utf-8")

        queue = build_robinhood_queue(
            data_dir,
            account_budget=500,
            max_orders=2,
            max_candidates=3,
            min_dte=180,
            min_confidence=55,
            query="MSFT",
        )

    assert queue["status"] == "ready"
    assert len(queue["orders"]) == 1
    assert queue["orders"][0]["symbol"] == "MSFT"
    assert queue["orders"][0]["max_limit_price"] >= 1.1


if __name__ == "__main__":
    test_excludes_zero_contract_options_by_default()
    test_excludes_watch_trades_by_default()
    test_includes_watch_only_with_include_watch()
    test_caps_max_new_orders()
    test_normalizes_options_correctly()
    test_normalizes_shares_correctly()
    test_normalizes_futures_correctly()
    test_dry_run_includes_exclusion_reasons()
    test_excludes_short_dated_options_by_default()
    test_query_filters_to_matching_ticker_or_contract()
    test_build_external_orders_includes_chain_shortlist_candidates()
    test_robinhood_queue_uses_chain_shortlist_when_no_top_options_exist()
    print("12/12 external paper track tests passed")
