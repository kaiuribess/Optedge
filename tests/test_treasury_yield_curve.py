"""Direct-run tests for the official Treasury yield-curve fallback."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engines import yield_curve_pca


class _FakeResponse:
    status_code = 200

    def __init__(self, text: str):
        self.text = text


class _FakeSession:
    def __init__(self, text: str):
        self.text = text
        self.params_seen = []

    def get(self, url: str, params=None, timeout=None):
        self.params_seen.append(params or {})
        return _FakeResponse(self.text)


def _treasury_xml(rows: int = 25) -> str:
    start = date(2026, 1, 2)
    entries = []
    for idx in range(rows):
        day = start + timedelta(days=idx)
        base = 3.5 + idx * 0.01
        entries.append(f"""
        <entry xmlns="http://www.w3.org/2005/Atom">
          <content type="application/xml">
            <m:properties>
              <d:NEW_DATE m:type="Edm.DateTime">{day.isoformat()}T00:00:00</d:NEW_DATE>
              <d:BC_3MONTH m:type="Edm.Double">{base:.2f}</d:BC_3MONTH>
              <d:BC_6MONTH m:type="Edm.Double">{base + 0.02:.2f}</d:BC_6MONTH>
              <d:BC_1YEAR m:type="Edm.Double">{base + 0.04:.2f}</d:BC_1YEAR>
              <d:BC_2YEAR m:type="Edm.Double">{base + 0.07:.2f}</d:BC_2YEAR>
              <d:BC_5YEAR m:type="Edm.Double">{base + 0.12:.2f}</d:BC_5YEAR>
              <d:BC_10YEAR m:type="Edm.Double">{base + 0.18:.2f}</d:BC_10YEAR>
              <d:BC_30YEAR m:type="Edm.Double">{base + 0.25:.2f}</d:BC_30YEAR>
            </m:properties>
          </content>
        </entry>
        """)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices" '
        'xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">'
        + "\n".join(entries)
        + "</feed>"
    )


def test_treasury_xml_parser_maps_required_tenors():
    rows = yield_curve_pca._parse_treasury_xml_curve(_treasury_xml(rows=1))
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-01-02"
    assert rows[0]["3m"] == 3.5
    assert rows[0]["10y"] == 3.68
    assert rows[0]["30y"] == 3.75


def test_compute_pca_uses_treasury_xml_when_fred_is_empty():
    session = _FakeSession(_treasury_xml(rows=30))
    old_fred_series = yield_curve_pca._fred_series_history
    old_cache_get = yield_curve_pca.data_provider.cache_get
    old_cache_put = yield_curve_pca.data_provider.cache_put
    old_get_session = yield_curve_pca.data_provider.get_session
    try:
        yield_curve_pca._fred_series_history = lambda *args, **kwargs: []
        yield_curve_pca.data_provider.cache_get = lambda *args, **kwargs: None
        yield_curve_pca.data_provider.cache_put = lambda *args, **kwargs: None
        yield_curve_pca.data_provider.get_session = lambda: session

        state = yield_curve_pca.compute_pca_factors()
    finally:
        yield_curve_pca._fred_series_history = old_fred_series
        yield_curve_pca.data_provider.cache_get = old_cache_get
        yield_curve_pca.data_provider.cache_put = old_cache_put
        yield_curve_pca.data_provider.get_session = old_get_session

    assert state["curve_source"] == "treasury_xml"
    assert state["n_obs"] == 30
    assert state["ten_year"] > state["two_year"]
    assert session.params_seen[0]["data"] == "daily_treasury_yield_curve"


if __name__ == "__main__":
    test_treasury_xml_parser_maps_required_tenors()
    test_compute_pca_uses_treasury_xml_when_fred_is_empty()
    print("2/2 treasury yield curve tests passed")
