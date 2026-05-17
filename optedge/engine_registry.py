"""Engine registry metadata for documentation and future orchestration cleanup."""
from __future__ import annotations

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
    return [*CORE_ENGINES, *RESEARCH_ENGINES]
