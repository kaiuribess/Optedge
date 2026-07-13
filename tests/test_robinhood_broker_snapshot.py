# Purpose: Test fail-closed broker snapshot normalization.
import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from risk.portfolio import summarize_broker_account_capital_at_risk  # noqa: E402
from scripts.local_cockpit import build_broker_reconciliation  # noqa: E402
from scripts.normalize_robinhood_broker_snapshot import (  # noqa: E402
    RAW_BUNDLE_SCHEMA,
    main,
    normalize_broker_snapshot,
)


def _raw_bundle():
    return {
        "accounts": {
            "accounts": [
                {
                    "account_number": "FAKE123456",
                    "label": "Agentic test",
                    "state": "active",
                    "agentic_allowed": True,
                    "option_level": "option_level_2",
                    "buying_power": "493.21",
                    "unleveraged_buying_power": "450.00",
                }
            ]
        },
        "portfolio": {
            "account_number": "FAKE123456",
            "buying_power": "493.21",
            "unleveraged_buying_power": "450.00",
            "total_value": "510.00",
        },
        "option_positions": {
            "FAKE123456": {
                "results": [
                    {
                        "chain_symbol": "ROBN",
                        "option_type": "call",
                        "strike_price": "35",
                        "expiration_date": "2026-12-18",
                        "quantity": "1",
                        "state": "open",
                        "average_price": "1.25",
                        "mark_price": "1.70",
                        "bid_price": "1.65",
                        "ask_price": "1.75",
                        "option_id": "opt-1",
                    }
                ]
            }
        },
        "equity_positions": [
            {
                "account_number": "FAKE123456",
                "symbol": "HOOD",
                "quantity": "3.5",
                "average_buy_price": "20",
                "current_price": "24",
            }
        ],
        "option_orders": {
            "results": [
                {
                    "account_number": "FAKE123456",
                    "id": "order-1",
                    "chain_symbol": "ROBN",
                    "state": "filled",
                    "side": "buy",
                    "quantity": "1",
                    "price": "1.25",
                }
            ]
        },
    }


def _split_permission_mcp_bundle():
    return {
        "get_accounts": {
            "data": {
                "accounts": [
                    {
                        "account_number": "OPT123456",
                        "type": "margin",
                        "brokerage_account_type": "individual",
                        "is_default": True,
                        "state": "active",
                        "agentic_allowed": False,
                        "option_level": "option_level_2",
                    },
                    {
                        "account_number": "AGT654321",
                        "type": "cash",
                        "brokerage_account_type": "individual",
                        "nickname": "Agentic",
                        "state": "active",
                        "agentic_allowed": True,
                        "option_level": "",
                    },
                ]
            },
            "guide": "MCP account list response",
        },
        "get_portfolio": {
            "account_number": "AGT654321",
            "data": {
                "total_value": "0",
                "cash": "0",
                    "buying_power": {
                        "buying_power": "0.0000",
                        "unleveraged_buying_power": "0.0000",
                        "display_currency": "USD",
                },
            },
        },
    }


def _mcp_v2_bundle():
    """A synthetic bundle matching the current decoded Robinhood MCP result shapes."""
    account_number = "AGT11112222"
    option_id = "option-uuid-1"
    return {
        "schema": RAW_BUNDLE_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "get_accounts": {
            "data": {
                "accounts": [{
                    "account_number": account_number,
                    "rhs_account_number": "RHS99992222",
                    "brokerage_account_type": "individual",
                    "type": "cash",
                    "nickname": "Agentic",
                    "is_default": True,
                    "state": "active",
                    "deactivated": False,
                    "permanently_deactivated": False,
                    "agentic_allowed": True,
                    "option_level": "option_level_2",
                }],
            },
            "guide": "Decoded get_accounts result.",
        },
        "account_snapshots": [{
            "account_number": account_number,
            "get_portfolio": {
                "data": {
                    "buying_power": {
                        "buying_power": "800.00",
                        "unleveraged_buying_power": "650.00",
                        "display_currency": "USD",
                    },
                    "cash": "700.00",
                    "currency": "USD",
                    "equity_value": "1000.00",
                    "options_value": "250.00",
                    "crypto_value": "0.00",
                    "event_contracts_value": "0.00",
                    "fixed_income_value": "0.00",
                    "futures_value": "0.00",
                    "mutual_funds_value": "0.00",
                    "pending_deposits": "0.00",
                    "total_value": "1250.00",
                },
                "guide": "Decoded get_portfolio result.",
            },
            "get_equity_positions": [{
                "data": {"positions": [], "next": None},
                "guide": "Decoded get_equity_positions result.",
            }],
            "get_option_positions": {
                "data": {
                    "positions": [{
                        "average_price": "1.25",
                        "chain_id": "chain-uuid-1",
                        "chain_symbol": "AAPL",
                        "expiration_date": "2027-01-15",
                        "intraday_average_open_price": "0.00",
                        "intraday_quantity": "0.00",
                        "opened_at": "2026-07-10T15:30:00+00:00",
                        "option_id": option_id,
                        "pending_assignment_quantity": "0.00",
                        "pending_buy_quantity": "0.00",
                        "pending_exercise_quantity": "0.00",
                        "pending_expiration_quantity": "0.00",
                        "pending_sell_quantity": "0.00",
                        "quantity": "1.00",
                        "trade_value_multiplier": "100.00",
                        "type": "long",
                    }],
                    "next": None,
                },
                "guide": "Decoded get_option_positions result.",
            },
            # A list permits multiple exact MCP pages while preserving each response shape.
            "get_option_instruments": [{
                "data": {
                    "instruments": [{
                        "chain_id": "chain-uuid-1",
                        "chain_symbol": "AAPL",
                        "expiration_date": "2027-01-15",
                        "id": option_id,
                        "min_ticks": {
                            "above_tick": "0.05",
                            "below_tick": "0.01",
                            "cutoff_price": "3.00",
                        },
                        "sellout_datetime": "2027-01-15T20:30:00+00:00",
                        "state": "active",
                        "strike_price": "200.00",
                        "tradability": "tradable",
                        "type": "call",
                        "underlying_type": "equity",
                    }],
                    "next": None,
                },
                "guide": "Decoded get_option_instruments result.",
            }],
            "get_equity_orders": {
                "data": {"orders": [], "next": None},
                "guide": "Decoded get_equity_orders result.",
            },
            "get_option_orders": {
                "data": {
                    "orders": [{
                        "id": "option-order-1",
                        "chain_id": "chain-uuid-1",
                        "chain_symbol": "AAPL",
                        "created_at": "2026-07-12T15:30:00+00:00",
                        "direction": "debit",
                        "legs": [{
                            "expiration_date": "2027-01-15",
                            "id": "leg-1",
                            "option_id": option_id,
                            "option_type": "call",
                            "position_effect": "open",
                            "ratio_quantity": 1,
                            "side": "buy",
                            "strike_price": "200.00",
                        }],
                        "market_hours": "regular_hours",
                        "pending_quantity": "1.00",
                        "placed_agent": "user",
                        "premium": "100.00",
                        "price": "1.00",
                        "processed_premium": "0.00",
                        "processed_quantity": "0.00",
                        "quantity": "1.00",
                        "state": "queued",
                        "time_in_force": "gfd",
                        "trade_value_multiplier": "100.00",
                        "trigger": "immediate",
                        "type": "limit",
                    }],
                    "next": None,
                },
                "guide": "Decoded get_option_orders result.",
            },
        }],
    }


def _write_matching_local_position(data_dir: Path) -> None:
    (data_dir / "open_positions.json").write_text(
        json.dumps([
            {
                "ticker": "ROBN",
                "side": "call",
                "strike": 35,
                "expiry": "2026-12-18",
                "quantity": 1,
                "trade_status": "open",
                "tracking_scope": "broker_linked",
            }
        ]),
        encoding="utf-8",
    )


def _write_v2_matching_local_position(data_dir: Path) -> None:
    (data_dir / "open_positions.json").write_text(json.dumps([{
        "ticker": "AAPL",
        "side": "call",
        "strike": 200,
        "expiry": "2027-01-15",
        "quantity": 1,
        "trade_status": "open",
        "tracking_scope": "broker_linked",
    }]), encoding="utf-8")


def test_normalizes_mcp_bundle_to_cockpit_snapshot():
    snapshot = normalize_broker_snapshot(_raw_bundle(), generated_at="2026-06-24T12:00:00+00:00")

    assert snapshot["schema"] == "optedge_robinhood_broker_snapshot_v1"
    assert snapshot["does_not_place_orders"] is True
    assert snapshot["counts"] == {
        "accounts": 1,
        "equity_positions": 1,
        "equity_orders": 0,
        "missing_option_contracts": 0,
        "option_orders": 1,
        "option_positions": 1,
    }

    account = snapshot["accounts"][0]
    assert "account_number" not in account
    assert account["state"] == "active"
    assert account["agentic_allowed"] is True
    assert account["option_level"] == "option_level_2"
    assert account["buying_power"] == 450.0
    assert account["portfolio"]["total_value"] == 510.0
    assert account["portfolio"]["buying_power"] == 493.21
    assert account["portfolio"]["unleveraged_buying_power"] == 450.0

    option = snapshot["option_positions"][0]
    assert option["symbol"] == "ROBN"
    assert option["option_type"] == "call"
    assert option["strike_price"] == 35.0
    assert option["expiration_date"] == "2026-12-18"
    assert option["current_price"] == 1.7
    assert option["account_agentic_allowed"] is True

    equity = snapshot["equity_positions"][0]
    assert equity["symbol"] == "HOOD"
    assert equity["quantity"] == 3.5
    assert equity["market_value"] == 84.0


def test_v2_normalizes_current_mcp_account_snapshots_and_joins_option_instrument():
    raw = _mcp_v2_bundle()
    snapshot = normalize_broker_snapshot(raw)

    assert snapshot["raw_bundle_schema"] == RAW_BUNDLE_SCHEMA
    assert snapshot["normalization_blockers"] == []
    assert snapshot["counts"] == {
        "accounts": 1,
        "equity_positions": 0,
        "equity_orders": 0,
        "missing_option_contracts": 0,
        "option_orders": 1,
        "option_positions": 1,
    }
    account = snapshot["accounts"][0]
    assert account["account_mask"] == "...2222"
    assert account["state"] == "active"
    assert account["portfolio"]["total_value"] == 1250.0
    assert account["portfolio"]["buying_power"] == 800.0
    assert account["portfolio"]["unleveraged_buying_power"] == 650.0
    assert account["buying_power"] == 650.0

    position = snapshot["option_positions"][0]
    assert position["instrument_id"] == "option-uuid-1"
    assert position["chain_id"] == "chain-uuid-1"
    assert position["symbol"] == "AAPL"
    assert position["option_type"] == "call"
    assert position["position_type"] == "long"
    assert position["strike_price"] == 200.0
    assert position["expiration_date"] == "2027-01-15"
    assert position["trade_value_multiplier"] == 100.0
    assert position["instrument_state"] == "active"
    assert position["tradability"] == "tradable"
    assert position["underlying_type"] == "equity"
    assert position["account_label"] == "Agentic"

    order = snapshot["option_orders"][0]
    assert order["order_id"] == "option-order-1"
    assert order["option_id"] == "option-uuid-1"
    assert order["option_type"] == "call"
    assert order["strike_price"] == 200.0
    assert order["expiration_date"] == "2027-01-15"
    assert order["account_mask"] == "...2222"

    encoded = json.dumps(snapshot, sort_keys=True)
    assert "AGT11112222" not in encoded
    assert "RHS99992222" not in encoded


def test_v2_invalid_option_quantity_blocks_instead_of_becoming_zero_exposure():
    raw = _mcp_v2_bundle()
    raw["account_snapshots"][0]["get_option_orders"]["data"]["orders"] = []
    position = raw["account_snapshots"][0]["get_option_positions"]["data"]["positions"][0]
    position["quantity"] = "not-a-number"

    snapshot = normalize_broker_snapshot(raw)

    assert snapshot["option_positions"][0]["quantity"] is None
    assert snapshot["option_positions"][0]["signed_quantity"] is None
    assert snapshot["option_positions"][0]["position_validation_errors"]
    assert any(
        "option_positions row 1 is unsafe: invalid quantity field(s): quantity"
        in value
        for value in snapshot["normalization_blockers"]
    )
    exposure = summarize_broker_account_capital_at_risk(
        snapshot,
        snapshot["accounts"][0]["account_key"],
    )
    assert exposure["eligible"] is False
    assert "broker snapshot has unresolved normalization blockers" in exposure["blockers"]


def test_v2_contradictory_option_quantity_aliases_block_normalization():
    raw = _mcp_v2_bundle()
    position = raw["account_snapshots"][0]["get_option_positions"]["data"]["positions"][0]
    position["contracts"] = "2.00"

    snapshot = normalize_broker_snapshot(raw)

    assert any(
        "quantity fields disagree: quantity, contracts" in value
        for value in snapshot["normalization_blockers"]
    )


def test_v2_invalid_equity_quantity_and_option_pending_fields_block_normalization():
    invalid_equity = _mcp_v2_bundle()
    invalid_equity["account_snapshots"][0]["get_equity_positions"][0]["data"][
        "positions"
    ] = [{
        "symbol": "AAPL",
        "quantity": "unknown",
        "current_price": "200.00",
    }]
    equity_snapshot = normalize_broker_snapshot(invalid_equity)
    assert any(
        "equity_positions row 1 is unsafe: invalid quantity field(s): quantity"
        in value
        for value in equity_snapshot["normalization_blockers"]
    )

    invalid_pending = _mcp_v2_bundle()
    option_position = invalid_pending["account_snapshots"][0]["get_option_positions"][
        "data"
    ]["positions"][0]
    option_position["pending_assignment_quantity"] = "unknown"
    pending_snapshot = normalize_broker_snapshot(invalid_pending)
    assert any(
        "invalid pending quantity field(s): pending_assignment_quantity" in value
        for value in pending_snapshot["normalization_blockers"]
    )


def test_v2_invalid_critical_position_prices_values_and_multiplier_block():
    invalid_mark = _mcp_v2_bundle()
    option_position = invalid_mark["account_snapshots"][0]["get_option_positions"][
        "data"
    ]["positions"][0]
    option_position["mark_price"] = "unknown"
    mark_snapshot = normalize_broker_snapshot(invalid_mark)
    assert any(
        "invalid current mark field(s): mark_price" in value
        for value in mark_snapshot["normalization_blockers"]
    )

    invalid_multiplier = _mcp_v2_bundle()
    option_position = invalid_multiplier["account_snapshots"][0][
        "get_option_positions"
    ]["data"]["positions"][0]
    option_position["trade_value_multiplier"] = "unknown"
    multiplier_snapshot = normalize_broker_snapshot(invalid_multiplier)
    assert any(
        "invalid trade-value multiplier field(s): trade_value_multiplier" in value
        for value in multiplier_snapshot["normalization_blockers"]
    )

    invalid_equity_price = _mcp_v2_bundle()
    invalid_equity_price["account_snapshots"][0]["get_equity_positions"][0][
        "data"
    ]["positions"] = [{
        "symbol": "AAPL",
        "quantity": "1",
        "current_price": "unknown",
        "market_value": "200.00",
    }]
    equity_price_snapshot = normalize_broker_snapshot(invalid_equity_price)
    assert any(
        "invalid current price field(s): current_price" in value
        for value in equity_price_snapshot["normalization_blockers"]
    )

    invalid_equity_value = _mcp_v2_bundle()
    invalid_equity_value["account_snapshots"][0]["get_equity_positions"][0][
        "data"
    ]["positions"] = [{
        "symbol": "AAPL",
        "quantity": "1",
        "current_price": "200.00",
        "market_value": "unknown",
    }]
    equity_value_snapshot = normalize_broker_snapshot(invalid_equity_value)
    assert any(
        "invalid market value field(s): market_value" in value
        for value in equity_value_snapshot["normalization_blockers"]
    )


def test_v2_valid_price_and_multiplier_alternatives_remain_compatible():
    raw = _mcp_v2_bundle()
    option_position = raw["account_snapshots"][0]["get_option_positions"]["data"][
        "positions"
    ][0]
    option_position["current_price"] = "1.20"
    option_position["mark_price"] = "1.25"
    option_position["trade_value_multiplier"] = None
    option_position["multiplier"] = "100"

    snapshot = normalize_broker_snapshot(raw)

    assert snapshot["normalization_blockers"] == []
    assert snapshot["option_positions"][0]["current_price"] == 1.25
    assert snapshot["option_positions"][0]["trade_value_multiplier"] == 100.0


def test_v2_contradictory_multiplier_aliases_block_normalization():
    raw = _mcp_v2_bundle()
    option_position = raw["account_snapshots"][0]["get_option_positions"]["data"][
        "positions"
    ][0]
    option_position["multiplier"] = "50"

    snapshot = normalize_broker_snapshot(raw)

    assert any(
        "trade-value multiplier fields disagree" in value
        for value in snapshot["normalization_blockers"]
    )


def test_v2_unknown_or_contradictory_position_type_aliases_block():
    invalid_equity_type = _mcp_v2_bundle()
    invalid_equity_type["account_snapshots"][0]["get_equity_positions"][0][
        "data"
    ]["positions"] = [{
        "symbol": "AAPL",
        "quantity": "1",
        "position_type": "unknown",
        "current_price": "200.00",
    }]
    equity_snapshot = normalize_broker_snapshot(invalid_equity_type)
    assert any(
        "invalid position-type field(s): position_type" in value
        for value in equity_snapshot["normalization_blockers"]
    )

    contradictory_option_type = _mcp_v2_bundle()
    option_position = contradictory_option_type["account_snapshots"][0][
        "get_option_positions"
    ]["data"]["positions"][0]
    option_position["position_type"] = "long"
    option_position["type"] = "short"
    option_snapshot = normalize_broker_snapshot(contradictory_option_type)
    assert any(
        "position-type fields disagree: position_type, type" in value
        for value in option_snapshot["normalization_blockers"]
    )


def test_v2_option_signed_quantity_must_reconcile_with_explicit_direction():
    negative_long = _mcp_v2_bundle()
    option_position = negative_long["account_snapshots"][0]["get_option_positions"][
        "data"
    ]["positions"][0]
    option_position["signed_quantity"] = "-1"
    option_position["type"] = "long"
    negative_long_snapshot = normalize_broker_snapshot(negative_long)
    assert any(
        "signed quantity contradicts explicit position type" in value
        for value in negative_long_snapshot["normalization_blockers"]
    )
    assert negative_long_snapshot["option_positions"][0]["quantity"] is None

    negative_unsigned_long = _mcp_v2_bundle()
    option_position = negative_unsigned_long["account_snapshots"][0][
        "get_option_positions"
    ]["data"]["positions"][0]
    option_position["quantity"] = "-1"
    option_position["type"] = "long"
    negative_unsigned_snapshot = normalize_broker_snapshot(negative_unsigned_long)
    assert any(
        "negative quantity contradicts explicit long position type" in value
        for value in negative_unsigned_snapshot["normalization_blockers"]
    )

    positive_short = _mcp_v2_bundle()
    option_position = positive_short["account_snapshots"][0]["get_option_positions"][
        "data"
    ]["positions"][0]
    option_position["signed_quantity"] = "1"
    option_position["type"] = "short"
    positive_short_snapshot = normalize_broker_snapshot(positive_short)
    assert any(
        "signed quantity contradicts explicit position type" in value
        for value in positive_short_snapshot["normalization_blockers"]
    )

    legitimate_unsigned_short = _mcp_v2_bundle()
    option_position = legitimate_unsigned_short["account_snapshots"][0][
        "get_option_positions"
    ]["data"]["positions"][0]
    option_position["type"] = "short"
    unsigned_short_snapshot = normalize_broker_snapshot(legitimate_unsigned_short)
    assert unsigned_short_snapshot["normalization_blockers"] == []
    assert unsigned_short_snapshot["option_positions"][0]["signed_quantity"] == -1.0


def test_v2_duplicate_or_blank_account_identities_block_normalization():
    duplicate = _mcp_v2_bundle()
    account_rows = duplicate["get_accounts"]["data"]["accounts"]
    account_rows.append(dict(account_rows[0]))
    duplicate_snapshot = normalize_broker_snapshot(duplicate)
    assert any(
        "get_accounts capture contains duplicate account identities" in value
        for value in duplicate_snapshot["normalization_blockers"]
    )

    blank = _mcp_v2_bundle()
    blank_account = blank["get_accounts"]["data"]["accounts"][0]
    blank_account["account_number"] = ""
    blank_account["rhs_account_number"] = ""
    blank_snapshot = normalize_broker_snapshot(blank)
    assert any(
        "row(s) without a stable account identity" in value
        for value in blank_snapshot["normalization_blockers"]
    )


def test_v2_multi_account_unscoped_reads_fail_closed_instead_of_guessing_account():
    raw = _mcp_v2_bundle()
    raw["get_accounts"]["data"]["accounts"].append({
        "account_number": "OPT33334444",
        "rhs_account_number": "RHS99994444",
        "brokerage_account_type": "individual",
        "type": "margin",
        "nickname": "Options",
        "is_default": False,
        "state": "active",
        "deactivated": False,
        "permanently_deactivated": False,
        "agentic_allowed": False,
        "option_level": "option_level_2",
    })
    scoped = raw.pop("account_snapshots")[0]
    raw["get_portfolio"] = scoped["get_portfolio"]
    raw["get_option_positions"] = scoped["get_option_positions"]
    raw["get_option_instruments"] = scoped["get_option_instruments"]
    raw["get_option_orders"] = scoped["get_option_orders"]

    snapshot = normalize_broker_snapshot(raw)

    assert snapshot["counts"]["accounts"] == 3
    assert any("requires non-empty account_snapshots" in value for value in snapshot["normalization_blockers"])
    assert any("portfolio rows are not account-scoped" in value for value in snapshot["normalization_blockers"])
    assert any("option_positions rows are not account-scoped" in value for value in snapshot["normalization_blockers"])
    unscoped = next(row for row in snapshot["accounts"] if row["account_mask"] == "...oped")
    assert unscoped["state"] == ""
    assert unscoped["agentic_allowed"] is False
    assert unscoped["portfolio"]["total_value"] == 1250.0
    assert len(unscoped["option_positions"]) == 1
    assert all(not row["portfolio"] for row in snapshot["accounts"] if row is not unscoped)

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(snapshot), encoding="utf-8",
        )
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        report = build_broker_reconciliation(data_dir)

    assert report["status"] == "mismatch"
    assert any("requires non-empty account_snapshots" in value for value in report["warnings"])


def test_v2_open_option_without_instrument_metadata_is_a_blocking_mismatch():
    raw = _mcp_v2_bundle()
    del raw["account_snapshots"][0]["get_option_instruments"]

    snapshot = normalize_broker_snapshot(raw)
    position = snapshot["option_positions"][0]

    assert snapshot["counts"]["missing_option_contracts"] == 1
    assert position["symbol"] == "AAPL"
    assert position["expiration_date"] == "2027-01-15"
    assert position["option_type"] == ""
    assert position["strike_price"] is None
    assert any("lack exact instrument metadata" in value for value in snapshot["normalization_blockers"])

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(snapshot), encoding="utf-8",
        )
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        report = build_broker_reconciliation(data_dir)

    assert report["status"] == "mismatch"
    assert report["missing_contract_fields_count"] == 1
    assert any("lack exact instrument metadata" in value for value in report["warnings"])


def test_v2_missing_required_scoped_read_blocks_reconciliation_readiness():
    raw = _mcp_v2_bundle()
    del raw["account_snapshots"][0]["get_equity_orders"]

    snapshot = normalize_broker_snapshot(raw)

    assert any(
        "missing required scoped read section(s): get_equity_orders" in value
        for value in snapshot["normalization_blockers"]
    )

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(snapshot), encoding="utf-8",
        )
        _write_v2_matching_local_position(data_dir)
        report = build_broker_reconciliation(data_dir)

    assert report["matched_count"] == 1
    assert report["status"] == "mismatch"
    assert report["normalization_ready"] is False
    assert report["normalization_blocker_count"] >= 1
    assert report["agentic_option_ready"] is False
    assert report["agentic_readiness_status"] == "capture_incomplete"
    assert report["funded_agentic_option_count"] == 0
    assert any("get_equity_orders" in value for value in report["normalization_blockers"])


def test_v2_nonnull_pagination_cursor_blocks_reconciliation_readiness():
    raw = _mcp_v2_bundle()
    raw["account_snapshots"][0]["get_option_orders"]["data"]["next"] = (
        "https://broker.invalid/options/orders?cursor=more"
    )

    snapshot = normalize_broker_snapshot(raw)

    assert any(
        "get_option_orders capture is incomplete: final data.next is non-null" in value
        for value in snapshot["normalization_blockers"]
    )

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(snapshot), encoding="utf-8",
        )
        _write_v2_matching_local_position(data_dir)
        report = build_broker_reconciliation(data_dir)

    assert report["matched_count"] == 1
    assert report["status"] == "mismatch"
    assert report["normalization_ready"] is False
    assert report["normalization_blocker_count"] >= 1
    assert report["agentic_option_ready"] is False
    assert report["agentic_readiness_status"] == "capture_incomplete"
    assert report["funded_agentic_option_count"] == 0
    assert any("final data.next is non-null" in value for value in report["normalization_blockers"])


def test_v2_complete_ordered_two_page_capture_is_accepted():
    raw = _mcp_v2_bundle()
    scope = raw["account_snapshots"][0]
    first_page = scope["get_option_orders"]
    first_page["data"]["next"] = "https://broker.invalid/options/orders?cursor=page-two"
    scope["get_option_orders"] = [
        first_page,
        {
            "request": {"cursor": "page-two"},
            "data": {"orders": [], "next": None},
            "guide": "Decoded final get_option_orders page.",
        },
    ]

    snapshot = normalize_broker_snapshot(raw)

    assert snapshot["normalization_blockers"] == []
    assert snapshot["counts"]["option_orders"] == 1
    assert snapshot["option_orders"][0]["order_id"] == "option-order-1"


def test_v2_followup_page_without_cursor_linkage_is_blocked():
    raw = _mcp_v2_bundle()
    scope = raw["account_snapshots"][0]
    first_page = scope["get_option_orders"]
    first_page["data"]["next"] = "https://broker.invalid/options/orders?cursor=expected-page"
    scope["get_option_orders"] = [
        first_page,
        {
            "data": {"orders": [], "next": None},
            "guide": "An unrelated terminal page without request cursor evidence.",
        },
    ]

    snapshot = normalize_broker_snapshot(raw)

    assert any(
        "follow-up page is missing request.cursor linkage metadata" in value
        for value in snapshot["normalization_blockers"]
    )


def test_v2_one_page_paginated_read_requires_explicit_terminal_next():
    raw = _mcp_v2_bundle()
    del raw["account_snapshots"][0]["get_option_positions"]["data"]["next"]

    snapshot = normalize_broker_snapshot(raw)

    assert any(
        "get_option_positions capture is incomplete: page 1 is missing explicit data.next"
        in value
        for value in snapshot["normalization_blockers"]
    )


def test_v2_final_paginated_page_requires_explicit_terminal_next():
    raw = _mcp_v2_bundle()
    scope = raw["account_snapshots"][0]
    first_page = scope["get_option_orders"]
    first_page["data"]["next"] = "https://broker.invalid/options/orders?cursor=page-two"
    scope["get_option_orders"] = [
        first_page,
        {
            "request": {"cursor": "page-two"},
            "data": {"orders": []},
            "guide": "Decoded final page with an omitted next field.",
        },
    ]

    snapshot = normalize_broker_snapshot(raw)

    assert any(
        "get_option_orders capture is incomplete: page 2 is missing explicit data.next"
        in value
        for value in snapshot["normalization_blockers"]
    )


def test_v2_required_reads_reject_guide_only_and_wrong_collection_shapes():
    cases = [
        (
            lambda raw: raw.__setitem__("get_accounts", {"guide": "capture failed"}),
            "get_accounts capture has an invalid decoded shape",
        ),
        (
            lambda raw: raw["account_snapshots"][0].__setitem__(
                "get_portfolio", {"guide": "capture failed"}
            ),
            "get_portfolio capture has an invalid decoded shape",
        ),
        (
            lambda raw: raw["account_snapshots"][0].__setitem__(
                "get_option_positions", {"guide": "capture failed"}
            ),
            "get_option_positions capture has an invalid decoded shape",
        ),
        (
            lambda raw: raw["account_snapshots"][0].__setitem__(
                "get_equity_orders",
                {"data": {"positions": [], "next": None}},
            ),
            "get_equity_orders capture has an invalid decoded shape",
        ),
        (
            lambda raw: raw["account_snapshots"][0].__setitem__(
                "get_option_orders",
                {"data": {"orders": [None], "next": None}},
            ),
            "data.orders contains a non-object entry",
        ),
    ]
    for mutate, expected in cases:
        raw = _mcp_v2_bundle()
        mutate(raw)

        snapshot = normalize_broker_snapshot(raw)

        assert any(expected in value for value in snapshot["normalization_blockers"])


def test_v2_rejects_account_scoped_reads_placed_at_top_level():
    raw = _mcp_v2_bundle()
    scope = raw["account_snapshots"][0]
    top_level_position = json.loads(json.dumps(scope["get_option_positions"]))
    scope["get_option_positions"] = {"data": {"positions": [], "next": None}}
    raw["get_option_positions"] = top_level_position

    snapshot = normalize_broker_snapshot(raw)

    assert snapshot["counts"]["option_positions"] == 0
    assert any(
        "V2 account-scoped read section(s) must not appear at the top level"
        in value
        and "get_option_positions" in value
        for value in snapshot["normalization_blockers"]
    )

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(snapshot), encoding="utf-8",
        )
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        report = build_broker_reconciliation(data_dir)

    assert report["execution_capture_ready"] is False
    assert report["agentic_option_ready"] is False
    assert report["agentic_readiness_status"] == "capture_incomplete"


def test_reconciliation_detects_aggregate_quantity_mismatch():
    raw = _raw_bundle()
    raw["option_positions"]["FAKE123456"]["results"][0]["quantity"] = "10"
    snapshot = normalize_broker_snapshot(raw, generated_at=datetime.now(UTC).isoformat())

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(snapshot), encoding="utf-8",
        )
        _write_matching_local_position(data_dir)
        report = build_broker_reconciliation(data_dir)

    assert report["status"] == "mismatch"
    assert report["matched_count"] == 0
    assert report["quantity_mismatch_count"] == 1
    assert report["rows"][0]["status"] == "quantity_mismatch"
    assert report["rows"][0]["signed_quantity"] == 10.0
    assert report["rows"][0]["local_signed_quantity"] == 1.0


def test_reconciliation_detects_long_short_type_mismatch():
    raw = _raw_bundle()
    raw["option_positions"]["FAKE123456"]["results"][0]["position_type"] = "short"
    snapshot = normalize_broker_snapshot(raw, generated_at=datetime.now(UTC).isoformat())

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(snapshot), encoding="utf-8",
        )
        _write_matching_local_position(data_dir)
        report = build_broker_reconciliation(data_dir)

    assert report["status"] == "mismatch"
    assert report["matched_count"] == 0
    assert report["type_mismatch_count"] == 1
    assert report["rows"][0]["status"] == "position_type_mismatch"
    assert report["rows"][0]["position_type"] == "short"
    assert report["rows"][0]["local_position_type"] == "long"


def test_reconciliation_assigns_safe_fallback_key_when_option_position_has_no_account_key():
    snapshot = normalize_broker_snapshot(
        _raw_bundle(),
        generated_at=datetime.now(UTC).isoformat(),
    )
    account = snapshot["accounts"][0]
    account.pop("account_key", None)
    account["option_positions"][0].pop("account_key", None)
    # Exercise the nested-account fallback path directly instead of allowing the
    # duplicated flattened row to provide its already-normalized account key.
    snapshot["option_positions"] = []

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(snapshot),
            encoding="utf-8",
        )
        _write_matching_local_position(data_dir)

        report = build_broker_reconciliation(data_dir)

    assert report["broker_option_count"] == 1
    assert report["matched_count"] == 1
    assert report["rows"][0]["account_key"] == "snapshot_account_001"


def test_same_label_accounts_keep_distinct_stable_keys_and_never_dedupe():
    raw = _mcp_v2_bundle()
    second_account_number = "AGT55556666"
    second_account = json.loads(json.dumps(raw["get_accounts"]["data"]["accounts"][0]))
    second_account["account_number"] = second_account_number
    second_account["rhs_account_number"] = "RHS99996666"
    second_account["is_default"] = False
    second_account["nickname"] = "Agentic"
    raw["get_accounts"]["data"]["accounts"].append(second_account)
    second_scope = json.loads(json.dumps(raw["account_snapshots"][0]))
    second_scope["account_number"] = second_account_number
    raw["account_snapshots"].append(second_scope)

    snapshot = normalize_broker_snapshot(raw)
    account_keys = {row["account_key"] for row in snapshot["accounts"]}
    position_keys = {row["account_key"] for row in snapshot["option_positions"]}

    assert len(account_keys) == 2
    assert all(key.startswith("acct_") for key in account_keys)
    assert position_keys == account_keys
    assert "AGT11112222" not in json.dumps(snapshot, sort_keys=True)
    assert second_account_number not in json.dumps(snapshot, sort_keys=True)

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(snapshot), encoding="utf-8",
        )
        _write_v2_matching_local_position(data_dir)
        report = build_broker_reconciliation(data_dir)

    assert report["broker_option_count"] == 2
    assert report["matched_count"] == 0
    assert report["account_scope_mismatch_count"] == 2
    assert {row["account_key"] for row in report["rows"] if row["source"] == "broker"} == account_keys


def test_v2_readiness_requires_authoritative_total_and_both_buying_power_fields():
    mutations = (
        lambda portfolio: portfolio.pop("total_value"),
        lambda portfolio: portfolio["buying_power"].pop("buying_power"),
        lambda portfolio: portfolio["buying_power"].pop("unleveraged_buying_power"),
    )
    for mutate in mutations:
        raw = _mcp_v2_bundle()
        portfolio = raw["account_snapshots"][0]["get_portfolio"]["data"]
        mutate(portfolio)
        snapshot = normalize_broker_snapshot(raw)

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "robinhood_broker_snapshot.json").write_text(
                json.dumps(snapshot), encoding="utf-8",
            )
            _write_v2_matching_local_position(data_dir)
            report = build_broker_reconciliation(data_dir)

        readiness = report["account_readiness_rows"][0]
        assert readiness["portfolio_ready"] is False
        assert readiness["funded"] is False
        assert readiness["status"] == "missing_portfolio"
        assert report["agentic_option_ready"] is False
        assert report["funded_agentic_option_count"] == 0
        if "total_value" not in portfolio:
            assert readiness["account_equity"] is None


def test_snapshot_redacts_account_numbers_and_raw_portfolio_secrets():
    raw = _raw_bundle()
    raw["accounts"]["accounts"][0]["portfolio"] = {
        "equity": "510.00",
        "access_token": "never-persist-this-account-secret",
    }
    raw["portfolio"]["refresh_token"] = "never-persist-this-portfolio-secret"
    raw["portfolio"]["account_url"] = "https://broker.invalid/accounts/FAKE123456"

    snapshot = normalize_broker_snapshot(raw, generated_at="2026-06-24T12:00:00+00:00")
    encoded = json.dumps(snapshot, sort_keys=True)

    assert "FAKE123456" not in encoded
    assert "never-persist" not in encoded
    assert "account_number" not in encoded
    assert snapshot["accounts"][0]["portfolio"] == {
        "total_value": 510.0,
        "buying_power": 493.21,
        "unleveraged_buying_power": 450.0,
    }

    snapshot["accounts"][0]["account_number"] = "FAKE123456"
    snapshot["accounts"][0]["portfolio"]["access_token"] = "never-persist-again"
    sanitized_again = normalize_broker_snapshot(snapshot)
    assert "FAKE123456" not in json.dumps(sanitized_again, sort_keys=True)
    assert "never-persist-again" not in json.dumps(sanitized_again, sort_keys=True)


def test_legacy_snapshot_remains_visible_but_cannot_authorize_agentic_review():
    snapshot = normalize_broker_snapshot(
        _raw_bundle(),
        generated_at=datetime.now(UTC).isoformat(),
    )

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(snapshot), encoding="utf-8",
        )
        _write_matching_local_position(data_dir)
        report = build_broker_reconciliation(data_dir)

    assert report["broker_option_count"] == 1
    assert report["matched_count"] == 1
    assert report["snapshot_schema"] == "optedge_robinhood_broker_snapshot_v1"
    assert report["raw_bundle_schema"] == "legacy_flexible_bundle"
    assert report["execution_capture_ready"] is False
    assert report["agentic_option_ready"] is False
    assert report["agentic_readiness_status"] == "capture_untrusted"


def test_missing_source_timestamp_is_not_replaced_by_normalization_time():
    snapshot = normalize_broker_snapshot(_raw_bundle())

    assert snapshot["generated_at"] is None
    assert snapshot["normalized_at"]

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "robinhood_broker_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
        _write_matching_local_position(data_dir)

        report = build_broker_reconciliation(data_dir)

    assert report["snapshot_age_minutes"] is None
    assert report["status"] == "review"
    assert any("timestamp is missing" in warning for warning in report["warnings"])


def test_stale_source_timestamp_keeps_reconciliation_out_of_synced_state():
    stale_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    snapshot = normalize_broker_snapshot(_raw_bundle(), generated_at=stale_at)

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "robinhood_broker_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
        _write_matching_local_position(data_dir)

        report = build_broker_reconciliation(data_dir)

    assert report["snapshot_age_minutes"] >= 100
    assert report["status"] == "review"
    assert any("snapshot is stale" in warning.lower() for warning in report["warnings"])


def test_zero_buying_power_is_not_replaced_by_positive_cash():
    raw = _raw_bundle()
    raw["accounts"]["accounts"][0]["buying_power"] = "0"
    raw["accounts"]["accounts"][0]["unleveraged_buying_power"] = "0"
    raw["portfolio"] = {
        "account_number": "FAKE123456",
        "buying_power": "0.0000",
        "unleveraged_buying_power": "0.0000",
        "total_value": "510.00",
        "cash": "250.00",
        "cash_available_for_withdrawal": "250.00",
    }

    snapshot = normalize_broker_snapshot(raw, generated_at=datetime.now(UTC).isoformat())
    account = snapshot["accounts"][0]

    assert account["buying_power"] == 0.0
    assert account["portfolio"]["buying_power"] == 0.0
    assert account["portfolio"]["unleveraged_buying_power"] == 0.0
    assert account["portfolio"]["cash"] == 250.0


def test_option_order_normalizes_execution_side_and_exact_contract_identity():
    raw = _raw_bundle()
    raw["option_orders"]["results"] = [
        {
            "account_number": "FAKE123456",
            "id": "working-order-1",
            "chain_symbol": "ROBN",
            "state": "queued",
            "side": "buy",
            "quantity": "1",
            "pending_quantity": "1",
            "processed_quantity": "0",
            "price": "1.25",
            "legs": [
                {
                    "side": "buy",
                    "position_effect": "open",
                    "option_type": "call",
                    "expiration_date": "2026-12-18",
                    "strike_price": "35",
                    "option_id": "opt-1",
                }
            ],
        }
    ]
    snapshot = normalize_broker_snapshot(raw, generated_at=datetime.now(UTC).isoformat())
    order = snapshot["option_orders"][0]

    assert order["side"] == "buy"
    assert order["position_effect"] == "open"
    assert order["option_type"] == "call"
    assert order["expiration_date"] == "2026-12-18"
    assert order["strike_price"] == 35.0
    assert order["option_id"] == "opt-1"
    assert order["pending_quantity"] == "1"
    assert order["processed_quantity"] == "0"

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "robinhood_broker_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
        _write_matching_local_position(data_dir)
        report = build_broker_reconciliation(data_dir)

    assert report["working_option_order_count"] == 1
    assert report["unresolved_working_option_order_count"] == 0
    assert report["working_option_order_contract_keys"] == ["ROBN|call|2026-12-18|35"]


def test_nonterminal_multi_leg_order_is_unresolved_instead_of_dropping_later_leg():
    raw = _raw_bundle()
    raw["option_orders"]["results"] = [{
        "account_number": "FAKE123456",
        "id": "working-spread-1",
        "chain_symbol": "AAPL",
        "state": "queued",
        "quantity": "1",
        "price": "0.50",
        "legs": [
            {
                "side": "sell",
                "position_effect": "open",
                "option_type": "call",
                "expiration_date": "2027-01-15",
                "strike_price": "210",
                "option_id": "spread-short-leg",
            },
            {
                "side": "buy",
                "position_effect": "open",
                "option_type": "call",
                "expiration_date": "2027-01-15",
                "strike_price": "200",
                "option_id": "planned-long-call-second-leg",
            },
        ],
    }]

    snapshot = normalize_broker_snapshot(raw, generated_at=datetime.now(UTC).isoformat())
    order = snapshot["option_orders"][0]

    assert order["leg_count"] == 2
    assert order["contract_identity_status"] == "unresolved_multi_leg"
    assert order["option_type"] == ""
    assert order["expiration_date"] == ""
    assert order["strike_price"] is None
    assert order["option_id"] == ""

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(snapshot), encoding="utf-8",
        )
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        report = build_broker_reconciliation(data_dir)

    assert report["working_option_order_count"] == 1
    assert report["unresolved_working_option_order_count"] == 1
    assert report["working_option_order_contract_keys"] == []
    assert report["status"] == "mismatch"


def test_nonterminal_order_with_valid_leg_plus_malformed_leg_is_unresolved():
    raw = _raw_bundle()
    raw["option_orders"]["results"] = [{
        "account_number": "FAKE123456",
        "id": "malformed-working-order",
        "chain_symbol": "AAPL",
        "state": "queued",
        "quantity": "1",
        "legs": [
            {
                "side": "buy",
                "position_effect": "open",
                "option_type": "call",
                "expiration_date": "2027-01-15",
                "strike_price": "200",
                "option_id": "apparently-valid-leg",
            },
            None,
        ],
    }]

    snapshot = normalize_broker_snapshot(raw, generated_at=datetime.now(UTC).isoformat())
    order = snapshot["option_orders"][0]

    assert order["leg_count"] == 2
    assert order["contract_identity_status"] == "unresolved_malformed_legs"
    assert order["option_type"] == ""
    assert order["expiration_date"] == ""
    assert order["strike_price"] is None

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(snapshot), encoding="utf-8",
        )
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        report = build_broker_reconciliation(data_dir)

    assert report["unresolved_working_option_order_count"] == 1
    assert report["working_option_order_contract_keys"] == []


def test_nonterminal_option_order_without_contract_identity_fails_closed():
    raw = _raw_bundle()
    raw["option_orders"]["results"] = [
        {
            "account_number": "FAKE123456",
            "id": "unknown-working-order",
            "state": "mystery_pending_state",
            "side": "buy",
            "quantity": "1",
            "price": "1.25",
        }
    ]
    snapshot = normalize_broker_snapshot(raw, generated_at=datetime.now(UTC).isoformat())

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "robinhood_broker_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
        _write_matching_local_position(data_dir)
        report = build_broker_reconciliation(data_dir)

    assert report["working_option_order_count"] == 1
    assert report["unresolved_working_option_order_count"] == 1
    assert report["status"] == "mismatch"
    assert any("cannot be matched to an exact contract" in warning for warning in report["warnings"])


def test_normalized_snapshot_feeds_broker_reconciliation():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        snapshot = normalize_broker_snapshot(_raw_bundle(), generated_at="2026-06-24T12:00:00+00:00")
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(snapshot),
            encoding="utf-8",
        )
        (data_dir / "open_positions.json").write_text(
            json.dumps([
                {
                    "ticker": "ROBN",
                    "side": "call",
                    "strike": 35,
                    "expiry": "2026-12-18",
                    "quantity": 1,
                    "trade_status": "open",
                    "tracking_scope": "broker_linked",
                }
            ]),
            encoding="utf-8",
        )

        report = build_broker_reconciliation(data_dir)

        assert report["snapshot_exists"] is True
        assert report["broker_option_count"] == 1
        assert report["matched_count"] == 1
        assert report["broker_only_count"] == 0
        assert report["agentic_option_ready"] is False
        assert report["agentic_readiness_status"] == "capture_untrusted"
        assert report["execution_capture_ready"] is False
        assert report["rows"][0]["status"] == "matched"


def test_paper_only_overlap_does_not_count_as_live_broker_sync():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        snapshot = normalize_broker_snapshot(
            _raw_bundle(),
            generated_at="2026-06-24T12:00:00+00:00",
        )
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(snapshot),
            encoding="utf-8",
        )
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "agentic_paper_positions.json").write_text(json.dumps([{
            "status": "open",
            "symbol": "ROBN",
            "option_side": "call",
            "strike": 35,
            "expiry": "2026-12-18",
            "quantity": 1,
        }]), encoding="utf-8")

        report = build_broker_reconciliation(data_dir)

        assert report["status"] == "mismatch"
        assert report["matched_count"] == 0
        assert report["broker_only_count"] == 1
        assert report["rows"][0]["status"] == "broker_only_paper_overlap"
        assert report["rows"][0]["local_match"] == "paper_reference_only"


def test_reconciliation_flags_split_agentic_and_options_permissions():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        snapshot = normalize_broker_snapshot(
            _split_permission_mcp_bundle(),
            generated_at="2026-06-24T12:00:00+00:00",
        )
        (data_dir / "robinhood_broker_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "agentic_paper_positions.json").write_text("[]", encoding="utf-8")

        report = build_broker_reconciliation(data_dir)

        assert snapshot["counts"]["accounts"] == 2
        assert snapshot["accounts"][0]["option_level"] == "option_level_2"
        assert snapshot["accounts"][1]["agentic_allowed"] is True
        assert report["agentic_option_ready"] is False
        assert report["agentic_readiness_status"] == "capture_untrusted"
        assert report["agentic_readiness_label"] == "Trusted V2 broker capture required"
        assert report["agentic_account_count"] == 1
        assert report["option_ready_account_count"] == 1
        assert len(report["account_readiness_rows"]) == 2
        assert report["execution_capture_ready"] is False
        assert any("optedge_robinhood_mcp_read_bundle_v2" in warning for warning in report["warnings"])


def test_reconciliation_rejects_truthy_string_agentic_permission():
    snapshot = normalize_broker_snapshot(
        _mcp_v2_bundle(),
        generated_at=datetime.now(UTC).isoformat(),
    )
    snapshot["accounts"][0]["agentic_allowed"] = "false"

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        report = build_broker_reconciliation(
            data_dir,
            snapshot_override=snapshot,
        )

    assert report["agentic_account_count"] == 0
    assert report["agentic_option_ready"] is False
    assert report["agentic_readiness_status"] != "ready"
    assert report["account_readiness_rows"][0]["agentic_allowed"] is False


def test_boolean_portfolio_money_cannot_authorize_a_funded_account():
    raw = _mcp_v2_bundle()
    portfolio = raw["account_snapshots"][0]["get_portfolio"]["data"]
    portfolio["total_value"] = True
    portfolio["buying_power"] = {
        "buying_power": True,
        "unleveraged_buying_power": True,
    }
    snapshot = normalize_broker_snapshot(
        raw,
        generated_at=datetime.now(UTC).isoformat(),
    )

    normalized_portfolio = snapshot["accounts"][0]["portfolio"]
    assert normalized_portfolio.get("total_value") is None
    assert normalized_portfolio.get("buying_power") is None
    assert normalized_portfolio.get("unleveraged_buying_power") is None

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        report = build_broker_reconciliation(
            data_dir,
            snapshot_override=snapshot,
        )

    readiness = report["account_readiness_rows"][0]
    assert readiness["account_equity"] is None
    assert readiness["buying_power"] is None
    assert readiness["funded"] is False
    assert report["funded_agentic_option_count"] == 0
    assert report["agentic_option_ready"] is False


def test_reconciliation_does_not_treat_inactive_agentic_options_account_as_ready():
    raw = _raw_bundle()
    raw["accounts"]["accounts"][0]["state"] = "deactivated"
    snapshot = normalize_broker_snapshot(raw, generated_at=datetime.now(UTC).isoformat())

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(snapshot), encoding="utf-8",
        )
        _write_matching_local_position(data_dir)
        report = build_broker_reconciliation(data_dir)

    assert report["agentic_option_ready"] is False
    assert report["funded_agentic_option_count"] == 0
    assert report["agentic_readiness_status"] == "capture_untrusted"
    assert report["execution_capture_ready"] is False
    assert report["account_readiness_rows"][0]["active"] is False
    assert report["account_readiness_rows"][0]["account_equity"] == 510.0
    assert report["account_readiness_rows"][0]["buying_power"] == 450.0


def test_cli_writes_snapshot_and_dry_run_does_not_write():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        raw_path = root / "raw.json"
        out_path = root / "robinhood_broker_snapshot.json"
        dry_path = root / "dry.json"
        raw_path.write_text(json.dumps(_raw_bundle()), encoding="utf-8")

        assert main(["--input", str(raw_path), "--output", str(dry_path), "--dry-run"]) == 0
        assert not dry_path.exists()

        assert main(["--input", str(raw_path), "--output", str(out_path)]) == 0
        saved = json.loads(out_path.read_text(encoding="utf-8"))
        assert saved["counts"]["option_positions"] == 1
        assert saved["accounts"][0]["account_mask"] == "...3456"


if __name__ == "__main__":
    test_normalizes_mcp_bundle_to_cockpit_snapshot()
    test_v2_normalizes_current_mcp_account_snapshots_and_joins_option_instrument()
    test_v2_multi_account_unscoped_reads_fail_closed_instead_of_guessing_account()
    test_v2_open_option_without_instrument_metadata_is_a_blocking_mismatch()
    test_v2_missing_required_scoped_read_blocks_reconciliation_readiness()
    test_v2_nonnull_pagination_cursor_blocks_reconciliation_readiness()
    test_v2_complete_ordered_two_page_capture_is_accepted()
    test_v2_followup_page_without_cursor_linkage_is_blocked()
    test_v2_one_page_paginated_read_requires_explicit_terminal_next()
    test_v2_final_paginated_page_requires_explicit_terminal_next()
    test_v2_required_reads_reject_guide_only_and_wrong_collection_shapes()
    test_v2_rejects_account_scoped_reads_placed_at_top_level()
    test_reconciliation_detects_aggregate_quantity_mismatch()
    test_reconciliation_detects_long_short_type_mismatch()
    test_reconciliation_assigns_safe_fallback_key_when_option_position_has_no_account_key()
    test_same_label_accounts_keep_distinct_stable_keys_and_never_dedupe()
    test_v2_readiness_requires_authoritative_total_and_both_buying_power_fields()
    test_snapshot_redacts_account_numbers_and_raw_portfolio_secrets()
    test_legacy_snapshot_remains_visible_but_cannot_authorize_agentic_review()
    test_missing_source_timestamp_is_not_replaced_by_normalization_time()
    test_stale_source_timestamp_keeps_reconciliation_out_of_synced_state()
    test_zero_buying_power_is_not_replaced_by_positive_cash()
    test_option_order_normalizes_execution_side_and_exact_contract_identity()
    test_nonterminal_multi_leg_order_is_unresolved_instead_of_dropping_later_leg()
    test_nonterminal_order_with_valid_leg_plus_malformed_leg_is_unresolved()
    test_nonterminal_option_order_without_contract_identity_fails_closed()
    test_normalized_snapshot_feeds_broker_reconciliation()
    test_paper_only_overlap_does_not_count_as_live_broker_sync()
    test_reconciliation_flags_split_agentic_and_options_permissions()
    test_reconciliation_does_not_treat_inactive_agentic_options_account_as_ready()
    test_cli_writes_snapshot_and_dry_run_does_not_write()
    print("31/31 robinhood broker snapshot tests passed")
