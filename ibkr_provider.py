"""Optional read-only IBKR market-data bridge.

Optedge does not store IBKR credentials. Log into TWS or IB Gateway yourself
and enable read-only socket API access; this module connects to localhost and
requests quotes from that already-authenticated session.
"""
from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, Optional

import sys

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("optedge.ibkr")

_IB = None
_OPTION = None
_DISABLED_REASON: Optional[str] = None


def _config(name: str, default: Any) -> Any:
    env_name = f"OPTEDGE_{name}"
    if env_name in os.environ:
        return os.environ[env_name]
    try:
        import config

        return getattr(config, name, default)
    except Exception:
        return default


def is_enabled() -> bool:
    value = _config("IBKR_ENABLED", False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def disabled_reason() -> Optional[str]:
    return _DISABLED_REASON


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _load_ib_insync():
    global _OPTION
    try:
        from ib_insync import IB, Option
    except Exception as exc:
        raise RuntimeError("ib_insync is not installed; run `pip install ib_insync`") from exc
    _OPTION = Option
    return IB


def _connection():
    global _IB, _DISABLED_REASON
    if not is_enabled():
        _DISABLED_REASON = "disabled"
        return None
    if _DISABLED_REASON:
        return None
    if _IB is not None and _IB.isConnected():
        return _IB
    try:
        IB = _load_ib_insync()
        host = str(_config("IBKR_HOST", "127.0.0.1"))
        port = int(_config("IBKR_PORT", 7497))
        client_id = int(_config("IBKR_CLIENT_ID", 77))
        timeout = float(_config("IBKR_CONNECT_TIMEOUT", 4))
        ib = IB()
        ib.connect(host, port, clientId=client_id, timeout=timeout, readonly=True)
        _IB = ib
        log.info("IBKR market data connected on %s:%s clientId=%s", host, port, client_id)
        return _IB
    except Exception as exc:
        _DISABLED_REASON = str(exc)
        log.warning("IBKR market data unavailable; falling back to public sources: %s", exc)
        return None


def option_contract_params(position: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ticker = str(position.get("ticker") or "").strip().upper()
    expiry = str(position.get("expiry") or "").strip().replace("-", "")
    side = str(position.get("side") or "").strip().lower()
    strike = _safe_float(position.get("strike"))
    if not ticker or not expiry or strike is None:
        return None
    if side not in {"call", "put"}:
        return None
    return {
        "symbol": ticker,
        "lastTradeDateOrContractMonth": expiry,
        "strike": float(strike),
        "right": "C" if side == "call" else "P",
        "exchange": "SMART",
        "currency": "USD",
    }


def quote_option_position(position: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return top-of-book quote/mid for an open option position."""
    ib = _connection()
    if ib is None:
        return None
    params = option_contract_params(position)
    if not params or _OPTION is None:
        return None
    try:
        contract = _OPTION(**params)
        qualified = ib.qualifyContracts(contract)
        if qualified:
            contract = qualified[0]
        ticker = ib.reqMktData(contract, "", False, False)
        ib.sleep(float(_config("IBKR_QUOTE_TIMEOUT", 1.5)))
        bid = _safe_float(getattr(ticker, "bid", None))
        ask = _safe_float(getattr(ticker, "ask", None))
        last = _safe_float(getattr(ticker, "last", None))
        close = _safe_float(getattr(ticker, "close", None))
        market = _safe_float(ticker.marketPrice()) if hasattr(ticker, "marketPrice") else None
        try:
            ib.cancelMktData(contract)
        except Exception:
            pass
        mid = None
        if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
            mid = (bid + ask) / 2.0
        elif market is not None and market > 0:
            mid = market
        elif last is not None and last > 0:
            mid = last
        elif close is not None and close > 0:
            mid = close
        if mid is None or mid <= 0:
            return None
        return {
            "mid": float(mid),
            "bid": bid,
            "ask": ask,
            "last": last,
            "close": close,
            "source": "ibkr",
            "conId": getattr(contract, "conId", None),
        }
    except Exception as exc:
        log.debug("IBKR option quote failed for %s: %s", params, exc)
        return None


def disconnect() -> None:
    global _IB
    try:
        if _IB is not None and _IB.isConnected():
            _IB.disconnect()
    except Exception:
        pass
    _IB = None
