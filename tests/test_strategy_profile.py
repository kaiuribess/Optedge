# Purpose: Test discovery and swing-execution policy profiles.
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import scripts.export_robinhood_agentic_queue as robinhood_queue  # noqa: E402
import scripts.local_cockpit as local_cockpit  # noqa: E402
from optedge.strategy_profile import (  # noqa: E402
    DISCOVERY_PROFILE,
    LEAPS_EVIDENCE_LANE,
    LEAPS_SWING_PROFILE,
    STRATEGY_VERSION,
    SWING_EXECUTION_PROFILE,
    strategy_version,
)


def test_discovery_profile_preserves_scanner_policy():
    assert strategy_version == STRATEGY_VERSION
    assert DISCOVERY_PROFILE.name == "discovery"
    assert DISCOVERY_PROFILE.strategy_version == STRATEGY_VERSION
    assert DISCOVERY_PROFILE.option_min_dte == 14
    assert DISCOVERY_PROFILE.option_max_dte == 60
    assert DISCOVERY_PROFILE.max_option_spread_pct == 0.15
    assert DISCOVERY_PROFILE.min_open_interest == 100
    assert DISCOVERY_PROFILE.min_daily_volume == 25
    assert DISCOVERY_PROFILE.min_option_price == 0.10
    assert config.MIN_DTE == DISCOVERY_PROFILE.option_min_dte
    assert config.MAX_DTE == DISCOVERY_PROFILE.option_max_dte
    assert config.MAX_BID_ASK_SPREAD_PCT == DISCOVERY_PROFILE.max_option_spread_pct
    assert config.MIN_OPEN_INTEREST == DISCOVERY_PROFILE.min_open_interest
    assert config.MIN_DAILY_VOLUME == DISCOVERY_PROFILE.min_daily_volume
    assert config.MIN_OPTION_PRICE == DISCOVERY_PROFILE.min_option_price


def test_swing_execution_profile_preserves_queue_and_cockpit_policy():
    assert SWING_EXECUTION_PROFILE.name == "swing_execution"
    assert SWING_EXECUTION_PROFILE.strategy_version == STRATEGY_VERSION
    assert SWING_EXECUTION_PROFILE.option_min_dte == 90
    assert SWING_EXECUTION_PROFILE.option_max_dte is None
    assert SWING_EXECUTION_PROFILE.max_option_spread_pct == 0.15
    assert robinhood_queue.DEFAULT_MIN_DTE == SWING_EXECUTION_PROFILE.option_min_dte
    assert (
        robinhood_queue.DEFAULT_MAX_SPREAD_PCT
        == SWING_EXECUTION_PROFILE.max_option_spread_pct
    )
    assert robinhood_queue.DEFAULT_MAX_ORDERS == SWING_EXECUTION_PROFILE.max_orders
    assert robinhood_queue.DEFAULT_MAX_CANDIDATES == SWING_EXECUTION_PROFILE.max_candidates
    assert (
        robinhood_queue.DEFAULT_ACCOUNT_BUDGET
        == SWING_EXECUTION_PROFILE.default_account_budget
    )
    assert robinhood_queue.DEFAULT_MIN_CONFIDENCE == SWING_EXECUTION_PROFILE.min_confidence
    assert robinhood_queue.DEFAULT_LIMIT_BUFFER_PCT == SWING_EXECUTION_PROFILE.limit_buffer_pct
    assert (
        robinhood_queue._default_max_total_premium(500.0)
        == SWING_EXECUTION_PROFILE.max_total_premium
    )
    assert (
        robinhood_queue._default_max_premium_per_order(500.0)
        == SWING_EXECUTION_PROFILE.max_premium_per_order
    )
    assert local_cockpit.MIN_SWING_OPTION_DTE == SWING_EXECUTION_PROFILE.option_min_dte
    assert (
        local_cockpit.FRESH_SNAPSHOT_MINUTES
        == SWING_EXECUTION_PROFILE.snapshot_fresh_minutes
    )
    assert (
        local_cockpit.STALE_SNAPSHOT_MINUTES
        == SWING_EXECUTION_PROFILE.snapshot_stale_minutes
    )
    assert (
        local_cockpit.AGENTIC_FRESH_MINUTES
        == SWING_EXECUTION_PROFILE.execution_packet_fresh_minutes
    )
    assert (
        local_cockpit.AGENTIC_STALE_MINUTES
        == SWING_EXECUTION_PROFILE.execution_packet_stale_minutes
    )


def test_leaps_swing_profile_is_true_leaps_with_shorter_manual_hold_policy():
    assert LEAPS_SWING_PROFILE.name == "leaps_swing"
    assert LEAPS_SWING_PROFILE.evidence_lane == LEAPS_EVIDENCE_LANE
    assert LEAPS_EVIDENCE_LANE == "option_leaps_swing"
    assert LEAPS_SWING_PROFILE.option_min_dte == 365
    assert LEAPS_SWING_PROFILE.option_max_dte == 900
    assert LEAPS_SWING_PROFILE.preferred_min_dte == 365
    assert LEAPS_SWING_PROFILE.preferred_max_dte == 730
    assert LEAPS_SWING_PROFILE.review_sessions == (3, 5, 10)
    assert LEAPS_SWING_PROFILE.default_hold_sessions == 10
    assert LEAPS_SWING_PROFILE.max_hold_sessions == 20
    assert LEAPS_SWING_PROFILE.min_abs_delta == 0.55
    assert LEAPS_SWING_PROFILE.max_abs_delta == 0.80
    assert LEAPS_SWING_PROFILE.max_spread_pct == 0.10
    assert LEAPS_SWING_PROFILE.min_open_interest == 250
    assert LEAPS_SWING_PROFILE.preferred_open_interest == 500
    assert LEAPS_SWING_PROFILE.min_daily_volume == 10
    assert LEAPS_SWING_PROFILE.min_confidence == 65
    assert LEAPS_SWING_PROFILE.after_cost_edge_must_be_positive is True
    assert LEAPS_SWING_PROFILE.max_quote_age_seconds == 120
    assert LEAPS_SWING_PROFILE.stop_loss_fraction == 0.25
    assert LEAPS_SWING_PROFILE.target_gain_fraction == 0.35
    assert LEAPS_SWING_PROFILE.breakeven_review_trigger_fraction == 0.20
    assert LEAPS_SWING_PROFILE.manual_management_only is True


def test_leaps_profile_does_not_mutate_existing_swing_defaults():
    assert SWING_EXECUTION_PROFILE.option_min_dte == 90
    assert SWING_EXECUTION_PROFILE.option_max_dte is None
    assert SWING_EXECUTION_PROFILE.max_option_spread_pct == 0.15
    assert SWING_EXECUTION_PROFILE.strategy_version == STRATEGY_VERSION


if __name__ == "__main__":
    test_discovery_profile_preserves_scanner_policy()
    test_swing_execution_profile_preserves_queue_and_cockpit_policy()
    test_leaps_swing_profile_is_true_leaps_with_shorter_manual_hold_policy()
    test_leaps_profile_does_not_mutate_existing_swing_defaults()
    print("4/4 strategy profile tests passed")
