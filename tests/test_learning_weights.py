# Purpose: Test versioned defaults and isolated learned weights.
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from engines import learning


def _payload(weight: float = 0.42) -> dict:
    return {
        "weights": {"trend": weight},
        "meta": {
            "source": "versioned_default",
            "n_samples": 12,
            "fitted_at": "2026-01-01T00:00:00+00:00",
            "factor_ic": {},
            "decay_flags": [],
        },
    }


def test_versioned_default_is_used_without_mutating_the_repository() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        runtime_dir = root / "runtime"
        default_dir = root / "defaults"
        default_dir.mkdir()
        payload = _payload()
        (default_dir / "options_call.json").write_text(json.dumps(payload), encoding="utf-8")
        old_runtime = learning.WEIGHTS_DIR
        old_defaults = learning.DEFAULT_WEIGHTS_DIR
        learning.WEIGHTS_DIR = runtime_dir
        learning.DEFAULT_WEIGHTS_DIR = default_dir
        try:
            assert learning.load_weights("options_call") == {"trend": 0.42}
            assert learning.load_meta("options_call")["source"] == "versioned_default"
            assert not runtime_dir.exists()
        finally:
            learning.WEIGHTS_DIR = old_runtime
            learning.DEFAULT_WEIGHTS_DIR = old_defaults


def test_cold_start_copies_defaults_to_ignored_runtime_state() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        runtime_dir = root / "runtime"
        default_dir = root / "defaults"
        default_dir.mkdir()
        payload = _payload(0.31)
        (default_dir / "options_call.json").write_text(json.dumps(payload), encoding="utf-8")
        old_runtime = learning.WEIGHTS_DIR
        old_defaults = learning.DEFAULT_WEIGHTS_DIR
        old_buckets = learning.BUCKET_KEYS
        learning.WEIGHTS_DIR = runtime_dir
        learning.DEFAULT_WEIGHTS_DIR = default_dir
        learning.BUCKET_KEYS = ["options_call"]
        try:
            assert learning.initialize_priors() == 1
            saved = json.loads((runtime_dir / "options_call.json").read_text(encoding="utf-8"))
            assert saved == payload
            assert learning.initialize_priors() == 0
        finally:
            learning.WEIGHTS_DIR = old_runtime
            learning.DEFAULT_WEIGHTS_DIR = old_defaults
            learning.BUCKET_KEYS = old_buckets


def test_corrupt_runtime_file_falls_back_to_versioned_default() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        runtime_dir = root / "runtime"
        default_dir = root / "defaults"
        runtime_dir.mkdir()
        default_dir.mkdir()
        (runtime_dir / "options_call.json").write_text("{broken", encoding="utf-8")
        (default_dir / "options_call.json").write_text(
            json.dumps(_payload(0.987)), encoding="utf-8"
        )
        old_runtime = learning.WEIGHTS_DIR
        old_defaults = learning.DEFAULT_WEIGHTS_DIR
        learning.WEIGHTS_DIR = runtime_dir
        learning.DEFAULT_WEIGHTS_DIR = default_dir
        try:
            assert learning.load_weights("options_call") == {"trend": 0.987}
            assert learning.load_meta("options_call")["source"] == "versioned_default"
        finally:
            learning.WEIGHTS_DIR = old_runtime
            learning.DEFAULT_WEIGHTS_DIR = old_defaults
