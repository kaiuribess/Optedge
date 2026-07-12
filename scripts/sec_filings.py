# Purpose: Retrieve SEC filings and company facts from EDGAR.
"""Free SEC EDGAR recent filings lookup.

Uses SEC's public data.sec.gov submissions API. No API key required.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import data_provider

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{doc}"
SEC_HEADERS = {
    "User-Agent": os.environ.get(
        "SEC_USER_AGENT",
        "Optedge research cockpit contact local@example.com",
    ),
    "Accept": "application/json",
}

IMPORTANT_FORMS = {
    "8-K", "10-Q", "10-K", "S-1", "S-3", "S-8", "424B5", "424B2",
    "DEF 14A", "SC 13D", "SC 13G", "4",
}

FACT_DEFS = {
    "cash": {
        "label": "Cash and equivalents",
        "concepts": [
            ("us-gaap", "CashAndCashEquivalentsAtCarryingValue", "USD"),
            ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "USD"),
            ("us-gaap", "CashAndCashEquivalentsAndShortTermInvestments", "USD"),
        ],
    },
    "assets": {
        "label": "Assets",
        "concepts": [("us-gaap", "Assets", "USD")],
    },
    "liabilities": {
        "label": "Liabilities",
        "concepts": [("us-gaap", "Liabilities", "USD")],
    },
    "equity": {
        "label": "Stockholders equity",
        "concepts": [
            ("us-gaap", "StockholdersEquity", "USD"),
            ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "USD"),
        ],
    },
    "debt": {
        "label": "Debt",
        "concepts": [
            ("us-gaap", "LongTermDebtAndFinanceLeaseObligationsCurrentAndNoncurrent", "USD"),
            ("us-gaap", "LongTermDebtCurrent", "USD"),
            ("us-gaap", "LongTermDebtNoncurrent", "USD"),
            ("us-gaap", "LongTermDebt", "USD"),
        ],
    },
    "revenue": {
        "label": "Revenue",
        "concepts": [
            ("us-gaap", "Revenues", "USD"),
            ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD"),
            ("us-gaap", "SalesRevenueNet", "USD"),
        ],
    },
    "net_income": {
        "label": "Net income",
        "concepts": [("us-gaap", "NetIncomeLoss", "USD")],
    },
    "operating_cash_flow": {
        "label": "Operating cash flow",
        "concepts": [("us-gaap", "NetCashProvidedByUsedInOperatingActivities", "USD")],
    },
    "shares_outstanding": {
        "label": "Shares outstanding",
        "concepts": [
            ("dei", "EntityCommonStockSharesOutstanding", "shares"),
        ],
    },
}


def _sec_get_json(url: str, cache_key: str, max_age_sec: int, timeout: float = 8.0) -> Any:
    cached = data_provider.cache_get(cache_key, max_age_sec=max_age_sec)
    if cached is not None:
        return cached
    session = data_provider.get_session()
    resp = session.get(url, headers=SEC_HEADERS, timeout=timeout)
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"SEC request failed {getattr(resp, 'status_code', 'unknown')}")
    data = resp.json() if hasattr(resp, "json") else json.loads(resp.text)
    data_provider.cache_put(cache_key, data)
    time.sleep(0.12)
    return data


def _ticker_map() -> dict[str, dict[str, Any]]:
    data = _sec_get_json(SEC_TICKERS_URL, "sec_company_tickers:v1", 24 * 3600)
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(data, dict):
        return out
    for item in data.values():
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").upper().strip()
        cik = item.get("cik_str")
        if not ticker or cik is None:
            continue
        out[ticker] = {
            "ticker": ticker,
            "cik": str(cik).zfill(10),
            "name": item.get("title"),
        }
    return out


def _compact_recent_filings(recent: dict[str, Any]) -> list[dict[str, Any]]:
    forms = recent.get("form") or []
    filing_dates = recent.get("filingDate") or []
    report_dates = recent.get("reportDate") or []
    accession_numbers = recent.get("accessionNumber") or []
    primary_docs = recent.get("primaryDocument") or []
    descriptions = recent.get("primaryDocDescription") or []
    rows = []
    for idx, form in enumerate(forms):
        rows.append({
            "form": form,
            "filing_date": filing_dates[idx] if idx < len(filing_dates) else None,
            "report_date": report_dates[idx] if idx < len(report_dates) else None,
            "accession": accession_numbers[idx] if idx < len(accession_numbers) else None,
            "primary_document": primary_docs[idx] if idx < len(primary_docs) else None,
            "description": descriptions[idx] if idx < len(descriptions) else None,
        })
    return rows


def _filing_signal(form: str) -> str:
    f = str(form or "").upper().strip()
    if f in {"S-1", "S-3", "424B5", "424B2"}:
        return "dilution_or_offering_watch"
    if f == "8-K":
        return "material_event_review"
    if f in {"10-Q", "10-K"}:
        return "fundamental_update_review"
    if f in {"SC 13D", "SC 13G"}:
        return "ownership_change_review"
    if f == "4":
        return "insider_activity_review"
    return "filing_review"


def _as_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _latest_fact(companyfacts: dict[str, Any], concepts: list[tuple[str, str, str]]) -> dict[str, Any] | None:
    facts = companyfacts.get("facts", {}) if isinstance(companyfacts, dict) else {}
    candidates = []
    for taxonomy, concept, unit in concepts:
        concept_obj = ((facts.get(taxonomy) or {}).get(concept) or {})
        units = concept_obj.get("units") or {}
        rows = units.get(unit) or []
        for row in rows:
            value = _as_float(row.get("val"))
            if value is None:
                continue
            candidates.append({
                "value": value,
                "period_end": row.get("end"),
                "filed": row.get("filed"),
                "form": row.get("form"),
                "fy": row.get("fy"),
                "fp": row.get("fp"),
                "unit": unit,
                "taxonomy": taxonomy,
                "concept": concept,
            })
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda row: (str(row.get("filed") or ""), str(row.get("period_end") or "")),
        reverse=True,
    )[0]


def _ratio(num: float | None, denom: float | None) -> float | None:
    if num is None or denom is None or denom == 0:
        return None
    return num / denom


def _companyfacts_watch_signals(metrics: dict[str, float | None]) -> list[str]:
    signals = []
    if (metrics.get("liabilities_to_assets") or 0.0) >= 0.85:
        signals.append("high_liabilities_to_assets_watch")
    if (metrics.get("debt_to_assets") or 0.0) >= 0.60:
        signals.append("high_debt_load_watch")
    cash_to_debt = metrics.get("cash_to_debt")
    if cash_to_debt is not None and cash_to_debt < 0.25:
        signals.append("low_cash_vs_debt_watch")
    net_income = metrics.get("net_income")
    if net_income is not None and net_income < 0:
        signals.append("unprofitable_watch")
    op_cf = metrics.get("operating_cash_flow")
    if op_cf is not None and op_cf < 0:
        signals.append("negative_operating_cash_flow_watch")
    return signals


def recent_filings_for_symbol(symbol: str, limit: int = 8) -> dict[str, Any]:
    ticker = str(symbol or "").upper().strip()
    mapping = _ticker_map().get(ticker)
    if not mapping:
        return {
            "symbol": ticker,
            "source": "sec_edgar_submissions",
            "count": 0,
            "rows": [],
            "error": "ticker not found in SEC company_tickers.json",
        }
    cik = str(mapping["cik"])
    data = _sec_get_json(
        SEC_SUBMISSIONS_URL.format(cik=cik),
        f"sec_submissions:{cik}",
        6 * 3600,
    )
    recent = (data or {}).get("filings", {}).get("recent", {}) if isinstance(data, dict) else {}
    rows = []
    cik_int = str(int(cik))
    for row in _compact_recent_filings(recent):
        form = str(row.get("form") or "").upper().strip()
        if form not in IMPORTANT_FORMS:
            continue
        accession = str(row.get("accession") or "")
        doc = str(row.get("primary_document") or "")
        url = None
        if accession and doc:
            url = SEC_ARCHIVE_URL.format(
                cik_int=cik_int,
                acc_clean=accession.replace("-", ""),
                doc=doc,
            )
        rows.append({
            "ticker": ticker,
            "company_name": mapping.get("name"),
            "form": form,
            "filing_date": row.get("filing_date"),
            "report_date": row.get("report_date"),
            "description": row.get("description"),
            "filing_signal": _filing_signal(form),
            "url": url,
        })
        if len(rows) >= limit:
            break
    return {
        "symbol": ticker,
        "cik": cik,
        "company_name": mapping.get("name"),
        "source": "sec_edgar_submissions",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows),
        "rows": rows,
    }


def companyfacts_for_symbol(symbol: str, limit: int = 12) -> dict[str, Any]:
    ticker = str(symbol or "").upper().strip()
    mapping = _ticker_map().get(ticker)
    if not mapping:
        return {
            "symbol": ticker,
            "source": "sec_companyfacts",
            "count": 0,
            "rows": [],
            "metrics": {},
            "watch_signals": [],
            "error": "ticker not found in SEC company_tickers.json",
        }

    cik = str(mapping["cik"])
    data = _sec_get_json(
        SEC_COMPANYFACTS_URL.format(cik=cik),
        f"sec_companyfacts:{cik}",
        12 * 3600,
        timeout=12.0,
    )
    rows = []
    metrics: dict[str, float | None] = {}
    for key, spec in FACT_DEFS.items():
        fact = _latest_fact(data, spec["concepts"])
        if not fact:
            metrics[key] = None
            continue
        metrics[key] = fact["value"]
        rows.append({
            "ticker": ticker,
            "company_name": mapping.get("name"),
            "metric": key,
            "label": spec["label"],
            "value": fact["value"],
            "unit": fact.get("unit"),
            "period_end": fact.get("period_end"),
            "filed": fact.get("filed"),
            "form": fact.get("form"),
            "concept": fact.get("concept"),
        })

    metrics["liabilities_to_assets"] = _ratio(metrics.get("liabilities"), metrics.get("assets"))
    metrics["debt_to_assets"] = _ratio(metrics.get("debt"), metrics.get("assets"))
    metrics["cash_to_debt"] = _ratio(metrics.get("cash"), metrics.get("debt"))
    metrics["net_margin"] = _ratio(metrics.get("net_income"), metrics.get("revenue"))
    metrics["cash_per_share"] = _ratio(metrics.get("cash"), metrics.get("shares_outstanding"))
    watch_signals = _companyfacts_watch_signals(metrics)

    return {
        "symbol": ticker,
        "cik": cik,
        "company_name": mapping.get("name"),
        "source": "sec_companyfacts",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows[:limit]),
        "rows": rows[:limit],
        "metrics": metrics,
        "watch_signals": watch_signals,
    }
