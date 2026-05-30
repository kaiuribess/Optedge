import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ibkr_provider


def test_option_contract_params_call():
    params = ibkr_provider.option_contract_params({
        "ticker": "AAPL",
        "expiry": "2026-06-18",
        "side": "call",
        "strike": 200,
    })
    assert params == {
        "symbol": "AAPL",
        "lastTradeDateOrContractMonth": "20260618",
        "strike": 200.0,
        "right": "C",
        "exchange": "SMART",
        "currency": "USD",
    }


def test_option_contract_params_put():
    params = ibkr_provider.option_contract_params({
        "ticker": "spy",
        "expiry": "2026-06-18",
        "side": "put",
        "strike": "450",
    })
    assert params["symbol"] == "SPY"
    assert params["right"] == "P"
    assert params["strike"] == 450.0


def test_option_contract_params_rejects_missing_fields():
    assert ibkr_provider.option_contract_params({"ticker": "AAPL", "side": "call"}) is None


if __name__ == "__main__":
    test_option_contract_params_call()
    test_option_contract_params_put()
    test_option_contract_params_rejects_missing_fields()
    print("3/3 IBKR provider tests passed")
