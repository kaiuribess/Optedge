import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engines import regsho_threshold


SAMPLE_FILE = """Symbol|Security Name|Market Category|Reg SHO Threshold Flag|Rule 3210|Filler
MOVE|Move Corp Cmn|S|Y|N|
DROP|Drop Corp Cmn|G|N|Y|
WAIT|Wait Corp Cmn|Q|N|N|
20260615230020
"""


class _Response:
    def __init__(self, text: str):
        self.status_code = 200
        self.text = text


class _Session:
    def __init__(self):
        self.urls = []

    def get(self, url, timeout=12):
        self.urls.append((url, timeout))
        if url == regsho_threshold.REGSHO_PAGE_URL:
            return _Response(
                '<a href="/dynamic/symdir/regsho/nasdaqth20260615.txt">Download</a>'
            )
        assert url == "https://www.nasdaqtrader.com/dynamic/symdir/regsho/nasdaqth20260615.txt"
        return _Response(SAMPLE_FILE)


def test_parse_threshold_file_normalizes_flags_and_timestamp():
    df = regsho_threshold.parse_threshold_file(
        SAMPLE_FILE,
        source_url="https://www.nasdaqtrader.com/dynamic/symdir/regsho/nasdaqth20260615.txt",
    )

    assert list(df["symbol"])[:2] == ["MOVE", "DROP"]
    move = df[df["symbol"] == "MOVE"].iloc[0]
    drop = df[df["symbol"] == "DROP"].iloc[0]
    wait = df[df["symbol"] == "WAIT"].iloc[0]
    assert bool(move["is_threshold"]) is True
    assert move["reg_sho_threshold_flag"] == "Y"
    assert move["settlement_risk_score"] == 86
    assert bool(drop["is_threshold"]) is True
    assert drop["rule_3210"] == "Y"
    assert drop["settlement_risk_score"] == 78
    assert bool(wait["is_threshold"]) is False
    assert wait["file_timestamp"] == "20260615230020"
    assert move["source"] == regsho_threshold.SOURCE_NAME


def test_fetch_threshold_list_uses_download_link_and_cache():
    old_cache_get = regsho_threshold.data_provider.cache_get
    old_cache_put = regsho_threshold.data_provider.cache_put
    old_get_session = regsho_threshold.data_provider.get_session
    stored = {}
    session = _Session()

    regsho_threshold.data_provider.cache_get = lambda *args, **kwargs: None
    regsho_threshold.data_provider.cache_put = lambda key, value: stored.update({key: value})
    regsho_threshold.data_provider.get_session = lambda: session
    try:
        df = regsho_threshold.fetch_threshold_list(cache_age=0)
    finally:
        regsho_threshold.data_provider.cache_get = old_cache_get
        regsho_threshold.data_provider.cache_put = old_cache_put
        regsho_threshold.data_provider.get_session = old_get_session

    assert len(df) == 3
    assert session.urls[0] == (regsho_threshold.REGSHO_PAGE_URL, 12)
    assert session.urls[1][0].endswith("/dynamic/symdir/regsho/nasdaqth20260615.txt")
    assert "nasdaq_regsho_threshold:v1" in stored
    assert stored["nasdaq_regsho_threshold:v1"][0]["symbol"] == "MOVE"


if __name__ == "__main__":
    test_parse_threshold_file_normalizes_flags_and_timestamp()
    test_fetch_threshold_list_uses_download_link_and_cache()
    print("2/2 Reg SHO threshold tests passed")
