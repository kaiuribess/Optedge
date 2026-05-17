"""Risk-based futures sizing with micro-contract preference."""
from __future__ import annotations

import math
from typing import Any, Dict

import pandas as pd

FUTURES_SPECS: Dict[str, Dict[str, Any]] = {
    "ES=F": {"contract": "/ES", "micro_contract": "/MES", "point_value": 50, "micro_point_value": 5, "spec_confidence": "high"},
    "NQ=F": {"contract": "/NQ", "micro_contract": "/MNQ", "point_value": 20, "micro_point_value": 2, "spec_confidence": "high"},
    "YM=F": {"contract": "/YM", "micro_contract": "/MYM", "point_value": 5, "micro_point_value": 0.5, "spec_confidence": "high"},
    "RTY=F": {"contract": "/RTY", "micro_contract": "/M2K", "point_value": 50, "micro_point_value": 5, "spec_confidence": "high"},
    "GC=F": {"contract": "/GC", "micro_contract": "/MGC", "point_value": 100, "micro_point_value": 10, "spec_confidence": "high"},
    "SI=F": {"contract": "/SI", "micro_contract": None, "point_value": 5000, "micro_point_value": None, "spec_confidence": "low"},
    "CL=F": {"contract": "/CL", "micro_contract": "/MCL", "point_value": 1000, "micro_point_value": 100, "spec_confidence": "high"},
    "NG=F": {"contract": "/NG", "micro_contract": None, "point_value": 10000, "micro_point_value": None, "spec_confidence": "low"},
    "ZB=F": {"contract": "/ZB", "micro_contract": None, "point_value": 1000, "micro_point_value": None, "spec_confidence": "low"},
    "ZN=F": {"contract": "/ZN", "micro_contract": None, "point_value": 1000, "micro_point_value": None, "spec_confidence": "low"},
    "DX=F": {"contract": "/DX", "micro_contract": None, "point_value": 1000, "micro_point_value": None, "spec_confidence": "low"},
    "BTC=F": {"contract": "/BTC", "micro_contract": "/MBT", "point_value": 5, "micro_point_value": 0.1, "spec_confidence": "low"},
    "ETH=F": {"contract": "/ETH", "micro_contract": "/MET", "point_value": 50, "micro_point_value": 0.1, "spec_confidence": "low"},
}


def _atr_like(row: pd.Series) -> float:
    spot = float(row.get("spot") or row.get("entry") or 0)
    hv20 = row.get("hv20")
    try:
        hv = float(hv20)
        if hv > 0:
            return max(spot * hv / math.sqrt(252), spot * 0.003)
    except Exception:
        pass
    for key in ("ret_5d", "ret_20d"):
        try:
            r = abs(float(row.get(key) or 0))
            if r > 0:
                return max(spot * r / 2.0, spot * 0.003)
        except Exception:
            pass
    return max(spot * 0.01, 0.01)


def compute_futures_ev_and_sizing(row: pd.Series, bankroll: float = 10000,
                                  aggressive: bool = False) -> Dict[str, Any]:
    score = float(row.get("futures_score") or 0)
    threshold = 0.30
    if score > threshold:
        direction = "long"
    elif score < -threshold:
        direction = "short"
    else:
        direction = "watch"
    entry = float(row.get("spot") or row.get("entry") or 0)
    spec = FUTURES_SPECS.get(str(row.get("symbol") or ""), {})
    atr = _atr_like(row)
    stop_dist = 1.25 * atr
    target_dist = 2.50 * atr
    if direction == "short":
        stop = entry + stop_dist
        target = entry - target_dist
    else:
        stop = entry - stop_dist
        target = entry + target_dist
    risk_budget = bankroll * (0.01 if aggressive else 0.005)
    full_pv = spec.get("point_value")
    micro_pv = spec.get("micro_point_value")

    using_micro = False
    pv = full_pv
    contract = spec.get("contract")
    risk_per = abs(entry - stop) * float(pv or 0)
    contracts = math.floor(risk_budget / risk_per) if risk_per > 0 else 0
    if contracts < 1 and micro_pv:
        using_micro = True
        pv = micro_pv
        contract = spec.get("micro_contract")
        risk_per = abs(entry - stop) * float(pv)
        contracts = math.floor(risk_budget / risk_per) if risk_per > 0 else 0
    actionable = direction in {"long", "short"} and contracts > 0 and entry > 0 and pv
    reward = abs(target - entry) * float(pv or 0) * max(contracts, 1)
    risk = risk_per * max(contracts, 1) if risk_per else 0
    return {
        "asset": "futures",
        "direction": direction,
        "contract": contract,
        "micro_contract": spec.get("micro_contract"),
        "point_value": float(pv or 0),
        "using_micro": using_micro,
        "entry_price": entry,
        "atr_estimate": atr,
        "stop_price": stop,
        "target_price": target,
        "risk_dollars": risk,
        "reward_dollars": reward,
        "reward_risk_ratio": reward / risk if risk > 0 else None,
        "suggested_contracts": int(contracts),
        "suggested_dollars_risk": risk_per * contracts if risk_per else 0,
        "trade_status": "Trade" if actionable else ("Watch" if direction in {"long", "short"} else "Skip"),
        "is_actionable": bool(actionable),
        "spec_confidence": spec.get("spec_confidence", "low"),
    }


def add_sizing_to_futures(df: pd.DataFrame, bankroll: float = 10000,
                          aggressive: bool = False) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    rows = [compute_futures_ev_and_sizing(r, bankroll=bankroll, aggressive=aggressive)
            for _, r in out.iterrows()]
    return pd.concat([out.reset_index(drop=True), pd.DataFrame(rows)], axis=1)
