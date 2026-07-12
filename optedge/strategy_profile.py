"""Canonical strategy policy for discovery and swing-execution review.

Discovery deliberately uses a shorter option-DTE window to find and score
market ideas.  Swing execution is a separate, stricter handoff policy used
when an idea is prepared for broker review.  Keeping both profiles explicit
prevents the scanner's discovery window from being mistaken for an execution
requirement.

Changing a value in this module changes strategy behavior and therefore
requires a new ``strategy_version`` plus validation evidence.
"""
from __future__ import annotations

from dataclasses import dataclass


strategy_version = "2026.07-policy-v2"
STRATEGY_VERSION = strategy_version


# Discovery policy: preserves the main scanner's existing filters.
DISCOVERY_OPTION_MIN_DTE = 14
DISCOVERY_OPTION_MAX_DTE = 60
DISCOVERY_MAX_OPTION_SPREAD_PCT = 0.15
DISCOVERY_MIN_OPEN_INTEREST = 100
DISCOVERY_MIN_DAILY_VOLUME = 25
DISCOVERY_MIN_OPTION_PRICE = 0.10


# Swing-execution review policy: preserves queue and cockpit defaults.
SWING_EXECUTION_OPTION_MIN_DTE = 90
SWING_EXECUTION_OPTION_MAX_DTE: int | None = None
SWING_EXECUTION_MAX_OPTION_SPREAD_PCT = 0.15
SWING_EXECUTION_DEFAULT_ACCOUNT_BUDGET = 500.0
SWING_EXECUTION_MAX_ORDERS = 2
SWING_EXECUTION_MAX_CANDIDATES = 5
SWING_EXECUTION_MIN_CONFIDENCE = 55.0
SWING_EXECUTION_LIMIT_BUFFER_PCT = 0.08
SWING_EXECUTION_TOTAL_PREMIUM_BUDGET_FRACTION = 0.50
SWING_EXECUTION_MAX_TOTAL_PREMIUM = 250.0
SWING_EXECUTION_ORDER_PREMIUM_BUDGET_FRACTION = 0.30
SWING_EXECUTION_MAX_PREMIUM_PER_ORDER = 150.0
SWING_EXECUTION_OPTION_UNDERLYING_TYPE = "equity"
UNSUPPORTED_INDEX_OPTION_ROOTS = frozenset({
    "DJX", "MRUT", "NANOS", "NDX", "NDXP", "NQX", "OEX", "RUT", "RUTW",
    "SPX", "SPXW", "VIX", "VIXW", "XEO", "XSP",
})


def is_known_index_option_symbol(value: object) -> bool:
    """Return whether a symbol is an index/index-option root excluded from v2 review."""
    symbol = str(value or "").strip().upper()
    return symbol.startswith("^") or symbol in UNSUPPORTED_INDEX_OPTION_ROOTS


# Freshness policy used by the local cockpit and execution-review packets.
SNAPSHOT_FRESH_MINUTES = 90.0
SNAPSHOT_STALE_MINUTES = 360.0
EXECUTION_PACKET_FRESH_MINUTES = 45.0
EXECUTION_PACKET_STALE_MINUTES = 90.0


@dataclass(frozen=True, slots=True)
class DiscoveryStrategyProfile:
    """Filters used to discover and rank option ideas."""

    name: str
    strategy_version: str
    option_min_dte: int
    option_max_dte: int
    max_option_spread_pct: float
    min_open_interest: int
    min_daily_volume: int
    min_option_price: float


@dataclass(frozen=True, slots=True)
class SwingExecutionStrategyProfile:
    """Requirements used before an idea becomes a broker-review candidate."""

    name: str
    strategy_version: str
    option_min_dte: int
    option_max_dte: int | None
    max_option_spread_pct: float
    snapshot_fresh_minutes: float
    snapshot_stale_minutes: float
    execution_packet_fresh_minutes: float
    execution_packet_stale_minutes: float
    default_account_budget: float
    max_orders: int
    max_candidates: int
    min_confidence: float
    limit_buffer_pct: float
    total_premium_budget_fraction: float
    max_total_premium: float
    order_premium_budget_fraction: float
    max_premium_per_order: float


DISCOVERY_PROFILE = DiscoveryStrategyProfile(
    name="discovery",
    strategy_version=strategy_version,
    option_min_dte=DISCOVERY_OPTION_MIN_DTE,
    option_max_dte=DISCOVERY_OPTION_MAX_DTE,
    max_option_spread_pct=DISCOVERY_MAX_OPTION_SPREAD_PCT,
    min_open_interest=DISCOVERY_MIN_OPEN_INTEREST,
    min_daily_volume=DISCOVERY_MIN_DAILY_VOLUME,
    min_option_price=DISCOVERY_MIN_OPTION_PRICE,
)


SWING_EXECUTION_PROFILE = SwingExecutionStrategyProfile(
    name="swing_execution",
    strategy_version=strategy_version,
    option_min_dte=SWING_EXECUTION_OPTION_MIN_DTE,
    option_max_dte=SWING_EXECUTION_OPTION_MAX_DTE,
    max_option_spread_pct=SWING_EXECUTION_MAX_OPTION_SPREAD_PCT,
    snapshot_fresh_minutes=SNAPSHOT_FRESH_MINUTES,
    snapshot_stale_minutes=SNAPSHOT_STALE_MINUTES,
    execution_packet_fresh_minutes=EXECUTION_PACKET_FRESH_MINUTES,
    execution_packet_stale_minutes=EXECUTION_PACKET_STALE_MINUTES,
    default_account_budget=SWING_EXECUTION_DEFAULT_ACCOUNT_BUDGET,
    max_orders=SWING_EXECUTION_MAX_ORDERS,
    max_candidates=SWING_EXECUTION_MAX_CANDIDATES,
    min_confidence=SWING_EXECUTION_MIN_CONFIDENCE,
    limit_buffer_pct=SWING_EXECUTION_LIMIT_BUFFER_PCT,
    total_premium_budget_fraction=SWING_EXECUTION_TOTAL_PREMIUM_BUDGET_FRACTION,
    max_total_premium=SWING_EXECUTION_MAX_TOTAL_PREMIUM,
    order_premium_budget_fraction=SWING_EXECUTION_ORDER_PREMIUM_BUDGET_FRACTION,
    max_premium_per_order=SWING_EXECUTION_MAX_PREMIUM_PER_ORDER,
)
