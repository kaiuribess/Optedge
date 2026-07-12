# Purpose: Test memory-first performance caching.
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider


def test_ram_cache_serves_values_before_disk_roundtrip():
    old_enabled = data_provider.RAM_CACHE_ENABLED
    old_max = data_provider.RAM_CACHE_MAX_ITEMS
    data_provider.configure_ram_cache(enabled=True, max_items=100)
    key = "test:ram-cache:serves-values-before-disk"
    value = {"ok": True, "rows": [1, 2, 3]}
    try:
        data_provider.cache_put(key, value)
        stats = data_provider.cache_stats()
        assert stats["ram_cache_enabled"] is True
        assert stats["ram_cache_items"] >= 1
        assert data_provider.cache_get(key, max_age_sec=3600) == value
    finally:
        data_provider.configure_ram_cache(enabled=old_enabled, max_items=old_max)


if __name__ == "__main__":
    test_ram_cache_serves_values_before_disk_roundtrip()
    print("1/1 performance cache tests passed")
