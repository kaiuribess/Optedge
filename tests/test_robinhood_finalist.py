# Purpose: Verify the bounded, exact-contract Robinhood finalist market gate.
from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from optedge.robinhood_finalist import (
    FINALIST_CHECK_SCHEMA,
    FULL_CHAIN_EDGE_SCAN_SCHEMA,
    RobinhoodFinalistCheckError,
    apply_finalist_check_to_sources,
    canonical_digest,
    check_best_option_finalist,
    check_full_chain_option_edges,
    check_top_option_finalists,
    check_top_ticker_option_edges,
)

NOW = datetime(2026, 7, 16, 20, 40, tzinfo=UTC)
CHAIN_ID = "chain-1"
OPTION_ID = "option-1"


def _candidate() -> dict:
    return {
        "asset": "option",
        "symbol": "HYG",
        "ticker_or_symbol": "HYG",
        "action": "BUY_TO_OPEN",
        "order_type": "limit",
        "time_in_force": "day",
        "quantity": 1,
        "contract": "HYG 2026-12-18 P 75",
        "option_side": "put",
        "underlying_type": "equity",
        "strike": 75.0,
        "expiry": "2026-12-18",
        "dte": 155,
        "direction": "long_put",
        "reference_entry_price": 0.485,
        "source_quote_at": (NOW - timedelta(minutes=2)).isoformat(),
        "source_quote_time_basis": "provider_response_received_at",
        "source_bid": 0.48,
        "source_ask": 0.49,
        "source_spread_pct": 0.020619,
        "quote_quality": "free_or_delayed",
        "data_delay": "delayed",
        "max_limit_price": 0.52,
        "stop_price_reference": 0.24,
        "target_price_reference": 0.97,
        "max_allowed_spread_pct": 0.15,
        "execution_profile": "swing_execution",
    }


def _sources(*, entry_allowed: bool = True) -> tuple[dict, dict]:
    candidate = _candidate()
    queue = {
        "schema": "optedge_robinhood_agentic_options_queue_v1",
        "generated_at": (NOW - timedelta(minutes=1)).isoformat(),
        "execution_enabled": False,
        "max_orders_to_submit": 0,
        "does_not_place_orders": True,
        "orders": [copy.deepcopy(candidate)],
    }
    lane = "manual_review_candidates" if entry_allowed else "review_only_entry_candidates"
    cycle = {
        "schema": "optedge_robinhood_agentic_cycle_v1",
        "generated_at": (NOW - timedelta(seconds=45)).isoformat(),
        "auto_submit_allowed": False,
        "does_not_place_orders": True,
        "entry_gate": {
            "new_entries_allowed_after_live_checks": entry_allowed,
        },
        "manual_review_candidates": [],
        "review_only_entry_candidates": [],
    }
    cycle[lane] = [copy.deepcopy(candidate)]
    return queue, cycle


def _write_sources(data_dir: Path, *, entry_allowed: bool = True) -> tuple[dict, dict]:
    import json

    queue, cycle = _sources(entry_allowed=entry_allowed)
    (data_dir / "robinhood_agentic_queue.json").write_text(json.dumps(queue), encoding="utf-8")
    (data_dir / "robinhood_agentic_cycle.json").write_text(json.dumps(cycle), encoding="utf-8")
    return queue, cycle


class _Manager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.quote = {
            "instrument_id": OPTION_ID,
            "ask_price": "0.500000",
            "bid_price": "0.480000",
            "mark_price": "0.490000",
            "open_interest": 75000,
            "volume": 3200,
            "delta": "-0.2400",
            "theta": "-0.0020",
            "gamma": "0.0400",
            "vega": "0.0100",
            "implied_volatility": "0.1800",
            "break_even_price": "74.5000",
            "updated_at": (NOW - timedelta(seconds=3)).isoformat(),
        }
        self.instruments = [
            {
                "id": OPTION_ID,
                "chain_id": CHAIN_ID,
                "chain_symbol": "HYG",
                "underlying_type": "equity",
                "expiration_date": "2026-12-18",
                "strike_price": "75.0000",
                "type": "put",
                "state": "active",
                "tradability": "tradable",
            }
        ]

    def read_tool_input_schema(self, name: str) -> dict:
        if name == "get_option_chains":
            return {
                "type": "object",
                "required": ["underlying_symbol"],
                "properties": {
                    "underlying_symbol": {"type": "string"},
                    "cursor": {"type": "string"},
                },
                "additionalProperties": False,
            }
        if name == "get_option_instruments":
            return {
                "type": "object",
                "required": ["chain_id", "expiration_dates", "strike_price", "type"],
                "properties": {
                    "chain_id": {"type": "string"},
                    "expiration_dates": {"type": "string"},
                    "strike_price": {"type": "string"},
                    "type": {"type": "string"},
                    "state": {"type": "string"},
                    "tradability": {"type": "string"},
                    "cursor": {"type": "string"},
                },
                "additionalProperties": False,
            }
        if name == "get_option_quotes":
            return {
                "type": "object",
                "required": ["ids"],
                "properties": {
                    "ids": {"type": "array", "items": {"type": "string"}},
                    "cursor": {"type": "string"},
                },
                "additionalProperties": False,
            }
        if name == "get_equity_quotes":
            return {
                "type": "object",
                "required": ["symbols"],
                "properties": {
                    "symbols": {"type": "array", "items": {"type": "string"}}
                },
                "additionalProperties": False,
            }
        raise AssertionError(name)

    def call_read_tool(self, name: str, arguments: dict, *, timeout_seconds: float) -> dict:
        assert 0 < timeout_seconds <= 12
        self.calls.append((name, dict(arguments)))
        if name == "get_option_chains":
            return {
                "data": {
                    "chains": [
                        {
                            "id": CHAIN_ID,
                            "symbol": "HYG",
                            "can_open_position": True,
                            "cash_component": None,
                            "expiration_dates": ["2026-12-18"],
                            "trade_value_multiplier": "100.0000",
                            "underlying_instruments": [
                                {"symbol": "HYG", "instrument": "equity-1"}
                            ],
                        }
                    ],
                    "next": None,
                }
            }
        if name == "get_option_instruments":
            return {"data": {"instruments": copy.deepcopy(self.instruments), "next": None}}
        if name == "get_option_quotes":
            return {"data": {"quotes": [{"quote": copy.deepcopy(self.quote)}], "next": None}}
        raise AssertionError(name)


class _FullChainManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.instruments = [
            {
                "id": "call-1",
                "chain_id": CHAIN_ID,
                "chain_symbol": "HYG",
                "underlying_type": "equity",
                "expiration_date": "2026-12-18",
                "strike_price": "70",
                "type": "call",
                "state": "active",
                "tradability": "tradable",
            },
            {
                "id": "put-1",
                "chain_id": CHAIN_ID,
                "chain_symbol": "HYG",
                "underlying_type": "equity",
                "expiration_date": "2026-12-18",
                "strike_price": "80",
                "type": "put",
                "state": "active",
                "tradability": "tradable",
            },
            {
                "id": "dead-1",
                "chain_id": CHAIN_ID,
                "chain_symbol": "HYG",
                "underlying_type": "equity",
                "expiration_date": "2026-12-18",
                "strike_price": "75",
                "type": "call",
                "state": "active",
                "tradability": "tradable",
            },
        ]
        self.quotes = {
            "call-1": {
                "instrument_id": "call-1",
                "ask_price": "0.50",
                "bid_price": "0.48",
                "mark_price": "0.49",
                "open_interest": 1200,
                "volume": 90,
                "delta": "0.70",
                "implied_volatility": "0.22",
                "updated_at": (NOW - timedelta(seconds=2)).isoformat(),
            },
            "put-1": {
                "instrument_id": "put-1",
                "ask_price": "0.50",
                "bid_price": "0.48",
                "mark_price": "0.49",
                "open_interest": 1100,
                "volume": 80,
                "delta": "-0.70",
                "implied_volatility": "0.23",
                "updated_at": (NOW - timedelta(seconds=2)).isoformat(),
            },
            "dead-1": {
                "instrument_id": "dead-1",
                "ask_price": "1.00",
                "bid_price": "0.99",
                "mark_price": "0.995",
                "open_interest": 2,
                "volume": 0,
                "delta": "0.65",
                "implied_volatility": "0.20",
                "updated_at": (NOW - timedelta(seconds=2)).isoformat(),
            },
        }

    def read_tool_input_schema(self, name: str) -> dict:
        if name == "get_option_chains":
            return {
                "type": "object",
                "required": ["underlying_symbol"],
                "properties": {"underlying_symbol": {"type": "string"}},
                "additionalProperties": False,
            }
        if name == "get_option_instruments":
            return {
                "type": "object",
                "required": ["chain_id", "expiration_dates"],
                "properties": {
                    "chain_id": {"type": "string"},
                    "expiration_dates": {"type": "array", "items": {"type": "string"}},
                    "type": {"type": "string"},
                    "state": {"type": "string"},
                    "tradability": {"type": "string"},
                },
                "additionalProperties": False,
            }
        if name == "get_option_quotes":
            return {
                "type": "object",
                "required": ["ids"],
                "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
                "additionalProperties": False,
            }
        if name == "get_equity_quotes":
            return {
                "type": "object",
                "required": ["symbols"],
                "properties": {
                    "symbols": {"type": "array", "items": {"type": "string"}}
                },
                "additionalProperties": False,
            }
        raise AssertionError(name)

    def call_read_tool(self, name: str, arguments: dict, *, timeout_seconds: float) -> dict:
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "get_option_chains":
            return {
                "data": {
                    "chains": [
                        {
                            "id": CHAIN_ID,
                            "symbol": "HYG",
                            "can_open_position": True,
                            "cash_component": None,
                            "expiration_dates": ["2026-12-18"],
                            "trade_value_multiplier": "100",
                            "underlying_instruments": [
                                {"symbol": "HYG", "instrument": "equity-1"}
                            ],
                        }
                    ],
                    "next": None,
                }
            }
        if name == "get_option_instruments":
            return {"data": {"instruments": copy.deepcopy(self.instruments), "next": None}}
        if name == "get_option_quotes":
            ids = arguments["ids"]
            return {
                "data": {
                    "quotes": [
                        {"quote": copy.deepcopy(self.quotes[option_id])}
                        for option_id in ids
                    ],
                    "next": None,
                }
            }
        if name == "get_equity_quotes":
            return {
                "data": {
                    "results": [
                        {
                            "quote": {
                                "symbol": symbol,
                                "bid_price": "74.95",
                                "ask_price": "75.05",
                                "state": "active",
                            }
                        }
                        for symbol in arguments["symbols"]
                    ]
                }
            }
        raise AssertionError(name)

def test_happy_path_checks_only_exact_finalist_and_returns_loadable_live_plan(tmp_path: Path):
    _write_sources(tmp_path)
    manager = _Manager()
    report = check_best_option_finalist(
        manager,
        data_dir=tmp_path,
        now=NOW,
        write=False,
    )
    assert report["schema"] == FINALIST_CHECK_SCHEMA
    assert report["status"] == "passed"
    assert report["market_check_passed"] is True
    assert report["ready_for_manual_review"] is True
    assert report["candidate"]["label"] == "HYG 2026-12-18 P 75"
    assert report["contract"]["option_id"] == OPTION_ID
    assert report["quote"]["spread_fraction"] == pytest.approx(0.040816, abs=1e-6)
    assert report["quote"]["age_seconds"] == 3
    assert report["planner_candidate"]["plan_ready"] is True
    assert report["planner_candidate"]["entry_price"] == 0.5
    assert [name for name, _ in manager.calls] == [
        "get_option_chains",
        "get_option_instruments",
        "get_option_quotes",
    ]
    assert manager.calls[0][1] == {"underlying_symbol": "HYG"}
    assert manager.calls[1][1]["chain_id"] == CHAIN_ID
    assert manager.calls[2][1] == {"ids": [OPTION_ID]}
    assert report["does_not_place_orders"] is True
    assert report["does_not_preview_orders"] is True


def test_top_ten_check_is_bounded_and_preserves_ranked_candidate_identity(tmp_path: Path):
    import json

    queue, cycle = _sources()
    queue["orders"] = []
    cycle["manual_review_candidates"] = []
    for index in range(12):
        candidate = _candidate()
        candidate["max_limit_price"] = 0.52 + index / 100
        candidate["contract"] = f"HYG 2026-12-18 P 75 rank {index + 1}"
        queue["orders"].append(copy.deepcopy(candidate))
        cycle["manual_review_candidates"].append(copy.deepcopy(candidate))
    (tmp_path / "robinhood_agentic_queue.json").write_text(json.dumps(queue), encoding="utf-8")
    (tmp_path / "robinhood_agentic_cycle.json").write_text(json.dumps(cycle), encoding="utf-8")

    manager = _Manager()
    batch = check_top_option_finalists(manager, data_dir=tmp_path, limit=99, now=NOW)
    assert batch["candidate_count"] == 10
    assert batch["requested_limit"] == 10
    assert [row["candidate_index"] for row in batch["reports"]] == list(range(10))
    assert batch["one_shot"] is True
    assert batch["does_not_place_orders"] is True
    assert len(manager.calls) == 30
    saved = json.loads((tmp_path / "robinhood_finalist_batch.json").read_text())
    assert saved["artifact_digest_sha256"] == batch["artifact_digest_sha256"]


def test_market_check_can_pass_while_local_optedge_gate_still_blocks_review(tmp_path: Path):
    _write_sources(tmp_path, entry_allowed=False)
    report = check_best_option_finalist(_Manager(), data_dir=tmp_path, now=NOW, write=False)
    assert report["market_check_passed"] is True
    assert report["ready_for_manual_review"] is False
    assert report["candidate_lane"] == "review_only_entry_candidates"
    assert report["local_entry_gate_allowed"] is False


def test_ten_ticker_research_scan_reports_missing_tickers_and_never_promotes(tmp_path: Path):
    manager = _Manager()
    contract = {
        "symbol": "HYG",
        "side": "put",
        "underlying_type": "equity",
        "strike": 75.0,
        "expiry": "2026-12-18",
        "bid": 0.48,
        "ask": 0.49,
        "mid": 0.485,
        "after_cost_edge_pct": 0.12,
        "execution_profile": "swing_execution",
        "chain_source": "cboe",
        "quote_quality": "free_or_delayed",
    }
    result = check_top_ticker_option_edges(
        manager,
        data_dir=tmp_path,
        ticker_candidates=[
            {"symbol": "HYG", "source": "swing scout", "score": 88},
            {"symbol": "AAPL", "source": "swing scout", "score": 84},
        ],
        contract_rows=[contract],
        now=NOW,
    )
    assert result["ticker_count"] == 2
    assert result["contract_candidate_count"] == 1
    assert result["market_passed_count"] == 1
    assert result["live_edge_count"] == 1
    assert result["review_ready_count"] == 0
    assert result["reports"][0]["candidate_lane"] == "ticker_research_scan"
    assert result["reports"][0]["ready_for_manual_review"] is False
    assert result["reports"][1]["status"] == "no_contract"
    assert result["does_not_promote_candidates"] is True
    assert result["does_not_place_orders"] is True
    assert (tmp_path / "robinhood_ticker_edge_scan.json").exists()


def test_full_chain_scan_prices_every_hard_filter_survivor_and_rechecks_finalists(
    tmp_path: Path,
):
    manager = _FullChainManager()
    result = check_full_chain_option_edges(
        manager,
        data_dir=tmp_path,
        ticker_candidates=[{"symbol": "HYG", "source": "normal Optedge", "score": 82}],
        pricing_context={
            "HYG": {
                "spot": 75,
                "fair_vol": 0.30,
                "fair_vol_source": "top_options_test.parquet",
                "option_side": "call",
                "confidence": 80,
                "research_guard_status": "passed",
            }
        },
        now=NOW,
    )

    assert result["schema"] == FULL_CHAIN_EDGE_SCAN_SCHEMA
    assert result["ticker_count"] == 1
    assert result["total_instruments"] == 3
    assert result["total_option_quotes"] == 3
    assert result["hard_filter_survivor_count"] == 2
    assert result["rechecked_finalist_count"] == 2
    assert result["decision"] == "research_candidates_available"
    assert result["conservative_positive_count"] == 1
    assert result["does_not_promote_candidates"] is True
    assert result["does_not_place_orders"] is True
    assert result["no_trade_is_valid"] is True
    assert result["rejection_counts"]["daily volume below 10"] == 1
    call = next(row for row in result["results"] if row["option_type"] == "call")
    put = next(row for row in result["results"] if row["option_type"] == "put")
    assert call["exact_recheck_passed"] is True
    assert call["conservative_positive_edge"] is True
    assert call["spot_source"] == "robinhood_equity_quote"
    assert call["theoretical_ev_pct"] > 0
    assert call["after_cost_ev_pct"] > 0
    assert call["ev_lower_bound_pct"] > 0
    assert set(call["theoretical_models"]) == {
        "black_scholes",
        "crr",
        "bjerksund_stensland",
        "low",
        "high",
    }
    assert 0 < call["theoretical_profit_probability"] < 1
    assert put["thesis_aligned"] is False
    assert put["conservative_positive_edge"] is False
    quote_calls = [arguments for name, arguments in manager.calls if name == "get_option_quotes"]
    assert len(quote_calls) == 2
    assert set(quote_calls[0]["ids"]) == {"call-1", "put-1", "dead-1"}
    assert set(quote_calls[1]["ids"]) == {"call-1", "put-1"}
    assert (tmp_path / "robinhood_full_chain_edge_scan.json").exists()


def test_full_chain_scan_keeps_theoretical_iv_proxy_out_of_conservative_lane(tmp_path: Path):
    result = check_full_chain_option_edges(
        _FullChainManager(),
        data_dir=tmp_path,
        ticker_candidates=[{"symbol": "HYG", "source": "normal Optedge", "score": 82}],
        pricing_context={
            "HYG": {"spot": 75, "option_side": "call", "confidence": 80}
        },
        now=NOW,
        write=False,
    )
    assert result["decision"] == "no_trade"
    assert result["conservative_positive_count"] == 0
    assert result["theoretical_positive_count"] >= 1
    assert all(row["fair_vol_independent"] is False for row in result["results"])
    assert all(row["conservative_positive_edge"] is False for row in result["results"])


def test_full_chain_scan_keeps_stale_quotes_visible_only_as_theoretical_research(
    tmp_path: Path,
):
    manager = _FullChainManager()
    for quote in manager.quotes.values():
        quote["updated_at"] = (NOW - timedelta(hours=2)).isoformat()
    result = check_full_chain_option_edges(
        manager,
        data_dir=tmp_path,
        ticker_candidates=[{"symbol": "HYG"}],
        pricing_context={
            "HYG": {
                "spot": 75,
                "fair_vol": 0.30,
                "option_side": "call",
                "confidence": 80,
                "research_guard_status": "passed",
            }
        },
        now=NOW,
        write=False,
    )
    assert result["decision"] == "no_trade"
    assert result["hard_filter_survivor_count"] == 2
    assert result["theoretical_positive_count"] >= 1
    assert result["conservative_positive_count"] == 0
    assert all(row["quote_is_fresh"] is False for row in result["results"])
    assert all(row["exact_recheck_passed"] is False for row in result["results"])


def test_full_chain_scan_fails_closed_when_live_schema_requires_a_strike(tmp_path: Path):
    class StrikeRequiredManager(_FullChainManager):
        def read_tool_input_schema(self, name: str) -> dict:
            schema = super().read_tool_input_schema(name)
            if name == "get_option_instruments":
                schema["required"].append("strike_price")
                schema["properties"]["strike_price"] = {"type": "string"}
            return schema

    result = check_full_chain_option_edges(
        StrikeRequiredManager(),
        data_dir=tmp_path,
        ticker_candidates=[{"symbol": "HYG"}],
        pricing_context={"HYG": {"spot": 75, "fair_vol": 0.30, "option_side": "call"}},
        now=NOW,
        write=False,
    )
    assert result["decision"] == "no_trade"
    assert result["ticker_summaries"][0]["error_code"] == "full_chain_schema_requires_strike"
    assert result["total_option_quotes"] == 0


def test_exact_chain_symbol_binds_one_stable_underlying_reference(tmp_path: Path):
    _write_sources(tmp_path)

    class IdentifierOnlyUnderlyingManager(_Manager):
        def call_read_tool(
            self,
            name: str,
            arguments: dict,
            *,
            timeout_seconds: float,
        ) -> dict:
            result = super().call_read_tool(
                name,
                arguments,
                timeout_seconds=timeout_seconds,
            )
            if name == "get_option_chains":
                underlying = result["data"]["chains"][0]["underlying_instruments"][0]
                underlying["symbol"] = None
                underlying["instrument"] = "equity-reference-1"
            return result

    report = check_best_option_finalist(
        IdentifierOnlyUnderlyingManager(),
        data_dir=tmp_path,
        now=NOW,
        write=False,
    )
    assert report["market_check_passed"] is True
    assert report["contract"]["chain_symbol"] == "HYG"


def test_blank_underlying_symbol_without_stable_reference_stays_blocked(tmp_path: Path):
    _write_sources(tmp_path)

    class UnboundUnderlyingManager(_Manager):
        def call_read_tool(
            self,
            name: str,
            arguments: dict,
            *,
            timeout_seconds: float,
        ) -> dict:
            result = super().call_read_tool(
                name,
                arguments,
                timeout_seconds=timeout_seconds,
            )
            if name == "get_option_chains":
                result["data"]["chains"][0]["underlying_instruments"] = [{"symbol": None}]
            return result

    report = check_best_option_finalist(
        UnboundUnderlyingManager(),
        data_dir=tmp_path,
        now=NOW,
        write=False,
    )
    assert report["market_check_passed"] is False
    assert any("exact equity underlying" in blocker.lower() for blocker in report["blockers"])
    assert report["broker_read_calls"] == [
        "get_option_chains",
        "get_option_instruments",
    ]


def test_wide_stale_expensive_and_illiquid_quote_fails_closed(tmp_path: Path):
    _write_sources(tmp_path)
    manager = _Manager()
    manager.quote.update(
        {
            "bid_price": "0.10",
            "ask_price": "0.60",
            "open_interest": 5,
            "volume": 0,
            "updated_at": (NOW - timedelta(minutes=5)).isoformat(),
        }
    )
    report = check_best_option_finalist(manager, data_dir=tmp_path, now=NOW, write=False)
    assert report["status"] == "blocked"
    assert report["market_check_passed"] is False
    assert report["planner_candidate"]["plan_ready"] is False
    rendered = " ".join(report["blockers"]).lower()
    assert "stale" in rendered
    assert "spread" in rendered
    assert "limit cap" in rendered
    assert "open interest" in rendered
    assert "volume" in rendered


def test_ambiguous_instrument_blocks_before_quote_call(tmp_path: Path):
    _write_sources(tmp_path)
    manager = _Manager()
    manager.instruments.append(copy.deepcopy(manager.instruments[0]))
    manager.instruments[1]["id"] = "option-2"
    report = check_best_option_finalist(manager, data_dir=tmp_path, now=NOW, write=False)
    assert report["market_check_passed"] is False
    assert any("exactly one" in blocker.lower() for blocker in report["blockers"])
    assert [name for name, _ in manager.calls] == ["get_option_chains", "get_option_instruments"]


def test_live_check_overlay_is_bound_to_unchanged_queue_and_cycle(tmp_path: Path):
    queue, cycle = _write_sources(tmp_path)
    report = check_best_option_finalist(_Manager(), data_dir=tmp_path, now=NOW, write=False)
    plan = {
        "order": {
            "symbol": "HYG",
            "option_type": "put",
            "strike": 75,
            "expiry": "2026-12-18",
            "underlying_type": "equity",
        }
    }
    live_cycle, live_queue, status = apply_finalist_check_to_sources(
        plan, cycle, queue, report, now=NOW
    )
    assert status["applied"] is True
    live_row = live_queue["orders"][0]
    assert live_row["source_quote_time_basis"] == "broker_exchange_quote_updated_at"
    assert live_row["source_ask"] == 0.5
    assert live_row["quote_quality"] == "live_broker"
    assert live_cycle["manual_review_candidates"][0] == live_row

    changed_queue = copy.deepcopy(queue)
    changed_queue["orders"][0]["max_limit_price"] = 0.53
    _, unchanged, changed_status = apply_finalist_check_to_sources(
        plan, cycle, changed_queue, report, now=NOW
    )
    assert changed_status["applied"] is False
    assert changed_status["reason"] == "finalist_check_source_changed"
    assert canonical_digest(unchanged) == canonical_digest(changed_queue)


def test_unknown_required_schema_field_blocks_without_guessing_or_calling(tmp_path: Path):
    _write_sources(tmp_path)

    class ChangedManager(_Manager):
        def read_tool_input_schema(self, name: str) -> dict:
            schema = super().read_tool_input_schema(name)
            if name == "get_option_chains":
                schema["required"].append("new_required_field")
                schema["properties"]["new_required_field"] = {"type": "string"}
            return schema

    manager = ChangedManager()
    with pytest.raises(RobinhoodFinalistCheckError) as caught:
        check_best_option_finalist(manager, data_dir=tmp_path, now=NOW, write=False)
    assert caught.value.code == "tool_schema_changed"
    assert manager.calls == []


def test_missing_next_is_terminal_only_when_live_schema_forbids_cursor(tmp_path: Path):
    _write_sources(tmp_path)

    class NonPaginatedManager(_Manager):
        def read_tool_input_schema(self, name: str) -> dict:
            schema = super().read_tool_input_schema(name)
            if name in {"get_option_chains", "get_option_quotes"}:
                schema["properties"].pop("cursor")
            if name == "get_option_quotes":
                schema["required"] = ["instrument_ids"]
                schema["properties"].pop("ids")
                schema["properties"]["instrument_ids"] = {
                    "type": "array",
                    "items": {"type": "string"},
                }
            return schema

        def call_read_tool(
            self,
            name: str,
            arguments: dict,
            *,
            timeout_seconds: float,
        ) -> dict:
            result = super().call_read_tool(
                name,
                arguments,
                timeout_seconds=timeout_seconds,
            )
            if name in {
                "get_option_chains",
                "get_option_instruments",
                "get_option_quotes",
            }:
                result["data"].pop("next")
            if name == "get_option_quotes":
                result["data"]["results"] = result["data"].pop("quotes")
            return result

    manager = NonPaginatedManager()
    report = check_best_option_finalist(
        manager,
        data_dir=tmp_path,
        now=NOW,
        write=False,
    )
    assert report["market_check_passed"] is True
    assert manager.calls[-1] == (
        "get_option_quotes",
        {"instrument_ids": [OPTION_ID]},
    )


def test_quote_response_with_both_supported_envelopes_blocks_as_ambiguous(tmp_path: Path):
    _write_sources(tmp_path)

    class AmbiguousQuoteEnvelopeManager(_Manager):
        def call_read_tool(
            self,
            name: str,
            arguments: dict,
            *,
            timeout_seconds: float,
        ) -> dict:
            result = super().call_read_tool(
                name,
                arguments,
                timeout_seconds=timeout_seconds,
            )
            if name == "get_option_quotes":
                result["data"]["results"] = copy.deepcopy(result["data"]["quotes"])
            return result

    with pytest.raises(RobinhoodFinalistCheckError) as caught:
        check_best_option_finalist(
            AmbiguousQuoteEnvelopeManager(),
            data_dir=tmp_path,
            now=NOW,
            write=False,
        )
    assert caught.value.code == "tool_result_shape_changed"


def test_missing_next_still_blocks_when_live_schema_declares_cursor(tmp_path: Path):
    _write_sources(tmp_path)

    class UnprovenPaginatedManager(_Manager):
        def call_read_tool(
            self,
            name: str,
            arguments: dict,
            *,
            timeout_seconds: float,
        ) -> dict:
            result = super().call_read_tool(
                name,
                arguments,
                timeout_seconds=timeout_seconds,
            )
            if name == "get_option_chains":
                result["data"].pop("next")
            return result

    manager = UnprovenPaginatedManager()
    with pytest.raises(RobinhoodFinalistCheckError) as caught:
        check_best_option_finalist(
            manager,
            data_dir=tmp_path,
            now=NOW,
            write=False,
        )
    assert caught.value.code == "pagination_proof_missing"
    assert [name for name, _arguments in manager.calls] == [
        "get_option_chains",
    ]


def test_cursorless_exact_instrument_page_must_match_every_frozen_filter(tmp_path: Path):
    _write_sources(tmp_path)

    class ScopeMismatchManager(_Manager):
        def call_read_tool(
            self,
            name: str,
            arguments: dict,
            *,
            timeout_seconds: float,
        ) -> dict:
            result = super().call_read_tool(
                name,
                arguments,
                timeout_seconds=timeout_seconds,
            )
            if name == "get_option_instruments":
                result["data"]["instruments"][0]["chain_id"] = "unexpected-chain"
                result["data"].pop("next")
            return result

    manager = ScopeMismatchManager()
    with pytest.raises(RobinhoodFinalistCheckError) as caught:
        check_best_option_finalist(
            manager,
            data_dir=tmp_path,
            now=NOW,
            write=False,
        )
    assert caught.value.code == "pagination_proof_missing"
    assert [name for name, _arguments in manager.calls] == [
        "get_option_chains",
        "get_option_instruments",
    ]
