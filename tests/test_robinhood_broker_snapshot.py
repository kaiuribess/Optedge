import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.local_cockpit import build_broker_reconciliation  # noqa: E402
from scripts.normalize_robinhood_broker_snapshot import (  # noqa: E402
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
                    "agentic_allowed": True,
                    "option_level": "option_level_2",
                }
            ]
        },
        "portfolio": {
            "account_number": "FAKE123456",
            "buying_power": "493.21",
            "total_equity": "510.00",
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


def test_normalizes_mcp_bundle_to_cockpit_snapshot():
    snapshot = normalize_broker_snapshot(_raw_bundle(), generated_at="2026-06-24T12:00:00+00:00")

    assert snapshot["schema"] == "optedge_robinhood_broker_snapshot_v1"
    assert snapshot["does_not_place_orders"] is True
    assert snapshot["counts"] == {
        "accounts": 1,
        "equity_positions": 1,
        "equity_orders": 0,
        "option_orders": 1,
        "option_positions": 1,
    }

    account = snapshot["accounts"][0]
    assert account["agentic_allowed"] is True
    assert account["option_level"] == "option_level_2"
    assert account["buying_power"] == 493.21

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
                }
            ]),
            encoding="utf-8",
        )

        report = build_broker_reconciliation(data_dir)

        assert report["snapshot_exists"] is True
        assert report["broker_option_count"] == 1
        assert report["matched_count"] == 1
        assert report["broker_only_count"] == 0
        assert report["agentic_option_ready"] is True
        assert report["rows"][0]["status"] == "matched"


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
    test_normalized_snapshot_feeds_broker_reconciliation()
    test_cli_writes_snapshot_and_dry_run_does_not_write()
    print("3/3 robinhood broker snapshot tests passed")
