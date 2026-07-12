# Purpose: Test discovery and swing-execution policy profiles.
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
import scripts.export_robinhood_agentic_queue as robinhood_queue
import scripts.local_cockpit as local_cockpit
from optedge.strategy_profile import (
    DISCOVERY_PROFILE,
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


if __name__ == "__main__":
    test_discovery_profile_preserves_scanner_policy()
    test_swing_execution_profile_preserves_queue_and_cockpit_policy()
    print("2/2 strategy profile tests passed")
