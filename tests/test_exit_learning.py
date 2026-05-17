import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import exit_learning


def test_default_policy_when_sample_too_small():
    assert not exit_learning.enough_data_for_learning(
        "share",
        pd.DataFrame([{"asset": "share"}]),
        pd.DataFrame([{"asset": "share"}]),
    )
    policy = exit_learning.get_policy_for_asset("share")
    assert policy["close_pressure_threshold"] == 80


def test_thresholds_are_clamped_and_step_limited():
    clamped = exit_learning._clamp_thresholds({
        "watch_pressure_threshold": 10,
        "tighten_pressure_threshold": 10,
        "close_pressure_threshold": 100,
    })
    assert 35 <= clamped["watch_pressure_threshold"] <= 55
    assert 55 <= clamped["tighten_pressure_threshold"] <= 75
    assert 70 <= clamped["close_pressure_threshold"] <= 90
    assert exit_learning._step(80, 50) == 75
    assert exit_learning._step(80, 100) == 85


def test_malformed_policy_falls_back_to_defaults():
    with tempfile.TemporaryDirectory() as td:
        old_file = exit_learning.POLICY_FILE
        exit_learning.POLICY_FILE = Path(td) / "exit_policy.json"
        exit_learning.POLICY_FILE.write_text("{bad")
        try:
            policy = exit_learning.load_exit_policy()
            assert policy["assets"]["option"]["close_pressure_threshold"] == 80
        finally:
            exit_learning.POLICY_FILE = old_file


if __name__ == "__main__":
    test_default_policy_when_sample_too_small()
    test_thresholds_are_clamped_and_step_limited()
    test_malformed_policy_falls_back_to_defaults()
    print("3/3 exit learning tests passed")
