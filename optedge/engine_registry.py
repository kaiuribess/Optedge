# Purpose: Preserve informational engine-name metadata for compatibility.
"""Informational engine-name metadata retained for compatibility.

The current orchestrator owns engine dispatch and runtime status. These lists
are not an orchestration source of truth, do not prove that an engine ran, and
must not be treated as market evidence or model-promotion input.
"""
from __future__ import annotations

REGISTRY_ROLE = "informational_compatibility_metadata"
ORCHESTRATION_AUTHORITY = False
EVIDENCE_SOURCE = False

CORE_ENGINES = [
    "macro",
    "mispricing",
    "sentiment",
    "fundamentals",
    "insider",
    "news",
    "earnings",
    "value",
    "futures",
]

RESEARCH_ENGINES = [
    "congress",
    "social",
    "analyst",
    "sector_rs",
    "dark_pool",
    "fda",
    "sector_flow",
    "technicals",
    "short_int",
    "cot",
    "thirteen_f",
    "vix_term",
    "eia",
    "wasde",
    "buybacks",
    "gtrends",
    "form_144",
    "whisper",
    "hyperliquid",
    "twitter",
    "r_options",
    "yield_curve",
    "credit_spread",
    "cluster_buys",
]

KEY_ENGINE_SET = frozenset(CORE_ENGINES)


def all_engines() -> list[str]:
    """Return the historical status-name order without asserting availability."""
    return [*CORE_ENGINES, *RESEARCH_ENGINES]
