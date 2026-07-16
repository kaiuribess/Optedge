# Purpose: Verify the bounded, exact-contract Robinhood finalist market gate.
from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from optedge.robinhood_finalist import (
    FINALIST_CHECK_SCHEMA,
    RobinhoodFinalistCheckError,
    apply_finalist_check_to_sources,
    canonical_digest,
    check_best_option_finalist,
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
    (data_dir / "robinhood_agentic_queue.json").write_text(
        json.dumps(queue), encoding="utf-8"
    )
    (data_dir / "robinhood_agentic_cycle.json").write_text(
        json.dumps(cycle), encoding="utf-8"
    )
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
        self.instruments = [{
            "id": OPTION_ID,
            "chain_id": CHAIN_ID,
            "chain_symbol": "HYG",
            "underlying_type": "equity",
            "expiration_date": "2026-12-18",
            "strike_price": "75.0000",
            "type": "put",
            "state": "active",
            "tradability": "tradable",
        }]

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
        raise AssertionError(name)

    def call_read_tool(self, name: str, arguments: dict, *, timeout_seconds: float) -> dict:
        assert 0 < timeout_seconds <= 12
        self.calls.append((name, dict(arguments)))
        if name == "get_option_chains":
            return {"data": {"chains": [{
                "id": CHAIN_ID,
                "symbol": "HYG",
                "can_open_position": True,
                "cash_component": None,
                "expiration_dates": ["2026-12-18"],
                "trade_value_multiplier": "100.0000",
                "underlying_instruments": [{"symbol": "HYG", "instrument": "equity-1"}],
            }], "next": None}}
        if name == "get_option_instruments":
            return {"data": {"instruments": copy.deepcopy(self.instruments), "next": None}}
        if name == "get_option_quotes":
            return {"data": {"quotes": [{"quote": copy.deepcopy(self.quote)}], "next": None}}
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
        "get_option_chains", "get_option_instruments", "get_option_quotes"
    ]
    assert manager.calls[0][1] == {"underlying_symbol": "HYG"}
    assert manager.calls[1][1]["chain_id"] == CHAIN_ID
    assert manager.calls[2][1] == {"ids": [OPTION_ID]}
    assert report["does_not_place_orders"] is True
    assert report["does_not_preview_orders"] is True


def test_market_check_can_pass_while_local_optedge_gate_still_blocks_review(tmp_path: Path):
    _write_sources(tmp_path, entry_allowed=False)
    report = check_best_option_finalist(
        _Manager(), data_dir=tmp_path, now=NOW, write=False
    )
    assert report["market_check_passed"] is True
    assert report["ready_for_manual_review"] is False
    assert report["candidate_lane"] == "review_only_entry_candidates"
    assert report["local_entry_gate_allowed"] is False


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
                result["data"]["chains"][0]["underlying_instruments"] = [
                    {"symbol": None}
                ]
            return result

    report = check_best_option_finalist(
        UnboundUnderlyingManager(),
        data_dir=tmp_path,
        now=NOW,
        write=False,
    )
    assert report["market_check_passed"] is False
    assert any(
        "exact equity underlying" in blocker.lower()
        for blocker in report["blockers"]
    )
    assert report["broker_read_calls"] == [
        "get_option_chains",
        "get_option_instruments",
    ]


def test_wide_stale_expensive_and_illiquid_quote_fails_closed(tmp_path: Path):
    _write_sources(tmp_path)
    manager = _Manager()
    manager.quote.update({
        "bid_price": "0.10",
        "ask_price": "0.60",
        "open_interest": 5,
        "volume": 0,
        "updated_at": (NOW - timedelta(minutes=5)).isoformat(),
    })
    report = check_best_option_finalist(
        manager, data_dir=tmp_path, now=NOW, write=False
    )
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
    report = check_best_option_finalist(
        manager, data_dir=tmp_path, now=NOW, write=False
    )
    assert report["market_check_passed"] is False
    assert any("exactly one" in blocker.lower() for blocker in report["blockers"])
    assert [name for name, _ in manager.calls] == [
        "get_option_chains", "get_option_instruments"
    ]


def test_live_check_overlay_is_bound_to_unchanged_queue_and_cycle(tmp_path: Path):
    queue, cycle = _write_sources(tmp_path)
    report = check_best_option_finalist(
        _Manager(), data_dir=tmp_path, now=NOW, write=False
    )
    plan = {"order": {
        "symbol": "HYG",
        "option_type": "put",
        "strike": 75,
        "expiry": "2026-12-18",
        "underlying_type": "equity",
    }}
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
        check_best_option_finalist(
            manager, data_dir=tmp_path, now=NOW, write=False
        )
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
