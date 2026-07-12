# Purpose: Test keyless public FRED history parsing.
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engines import fred_public


class _FakeResponse:
    status_code = 200
    text = (
        "observation_date,DGS10\n"
        "2026-05-28,4.45\n"
        "2026-05-29,.\n"
        "2026-06-01,4.50\n"
    )


class _FakeSession:
    def get(self, url, params=None, timeout=15):
        assert url == fred_public.FRED_CSV_URL
        assert params["id"] == "DGS10"
        assert "cosd" in params
        assert timeout == 15
        return _FakeResponse()


class _MonkeyPatch:
    def __init__(self):
        self._changes = []

    def setattr(self, obj, name, value):
        self._changes.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, value in reversed(self._changes):
            setattr(obj, name, value)


def test_fred_csv_history_parses_keyless_public_csv(monkeypatch=None):
    own_patch = monkeypatch is None
    monkeypatch = monkeypatch or _MonkeyPatch()
    stored = {}
    monkeypatch.setattr(fred_public.data_provider, "cache_get", lambda *args, **kwargs: None)
    monkeypatch.setattr(fred_public.data_provider, "cache_put", lambda key, value: stored.update({key: value}))
    monkeypatch.setattr(fred_public.data_provider, "get_session", lambda: _FakeSession())

    try:
        rows = fred_public.fred_csv_history("DGS10", days=5)
    finally:
        if own_patch:
            monkeypatch.undo()

    assert rows == [
        {"date": "2026-06-01", "value": 4.50},
        {"date": "2026-05-28", "value": 4.45},
    ]
    assert stored


if __name__ == "__main__":
    test_fred_csv_history_parses_keyless_public_csv()
    print("1/1 FRED public tests passed")
