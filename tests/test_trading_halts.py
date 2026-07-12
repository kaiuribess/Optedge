# Purpose: Test active and resumed trading-halt parsing.
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engines import trading_halts


SAMPLE_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:ndaq="http://www.nasdaqtrader.com/">
  <channel>
    <title>NASDAQTrader.com</title>
    <pubDate>Tue, 16 Jun 2026 18:36:20 GMT</pubDate>
    <item>
      <title>MOVE</title>
      <pubDate>Tue, 16 Jun 2026 14:19:52 GMT</pubDate>
      <ndaq:HaltDate>06/16/2026</ndaq:HaltDate>
      <ndaq:HaltTime>14:19:52.826</ndaq:HaltTime>
      <ndaq:IssueSymbol>MOVE</ndaq:IssueSymbol>
      <ndaq:IssueName>Move Corp Cmn</ndaq:IssueName>
      <ndaq:Market>NASDAQ</ndaq:Market>
      <ndaq:ReasonCode>T1</ndaq:ReasonCode>
      <ndaq:PauseThresholdPrice />
      <ndaq:ResumptionDate></ndaq:ResumptionDate>
      <ndaq:ResumptionQuoteTime></ndaq:ResumptionQuoteTime>
      <ndaq:ResumptionTradeTime></ndaq:ResumptionTradeTime>
    </item>
    <item>
      <title>DROP</title>
      <pubDate>Tue, 16 Jun 2026 15:03:00 GMT</pubDate>
      <ndaq:HaltDate>06/16/2026</ndaq:HaltDate>
      <ndaq:HaltTime>11:03:00</ndaq:HaltTime>
      <ndaq:IssueSymbol>DROP</ndaq:IssueSymbol>
      <ndaq:IssueName>Drop Corp Cmn</ndaq:IssueName>
      <ndaq:Market>NYSE</ndaq:Market>
      <ndaq:ReasonCode>LUDP</ndaq:ReasonCode>
      <ndaq:PauseThresholdPrice>2.10</ndaq:PauseThresholdPrice>
      <ndaq:ResumptionDate>06/16/2026</ndaq:ResumptionDate>
      <ndaq:ResumptionQuoteTime>11:08:00</ndaq:ResumptionQuoteTime>
      <ndaq:ResumptionTradeTime>11:08:00</ndaq:ResumptionTradeTime>
    </item>
  </channel>
</rss>
"""


class _Response:
    status_code = 200
    text = SAMPLE_RSS


class _Session:
    def get(self, url, timeout=10):
        assert url == trading_halts.TRADE_HALTS_RSS_URL
        assert timeout == 10
        return _Response()


def test_parse_trade_halt_rss_normalizes_active_and_resumed_rows():
    df = trading_halts.parse_trade_halt_rss(SAMPLE_RSS)

    assert list(df["symbol"]) == ["MOVE", "DROP"]
    move = df[df["symbol"] == "MOVE"].iloc[0]
    drop = df[df["symbol"] == "DROP"].iloc[0]
    assert bool(move["active_halt"]) is True
    assert move["reason_code"] == "T1"
    assert move["halt_risk_score"] >= 98
    assert move["halted_at"].endswith("-04:00")
    assert bool(drop["active_halt"]) is False
    assert drop["reason_code"] == "LUDP"
    assert drop["pause_threshold_price"] == "2.10"
    assert drop["source"] == trading_halts.SOURCE_NAME


def test_fetch_trade_halts_uses_cache_and_session():
    old_cache_get = trading_halts.data_provider.cache_get
    old_cache_put = trading_halts.data_provider.cache_put
    old_get_session = trading_halts.data_provider.get_session
    stored = {}

    trading_halts.data_provider.cache_get = lambda *args, **kwargs: None
    trading_halts.data_provider.cache_put = lambda key, value: stored.update({key: value})
    trading_halts.data_provider.get_session = lambda: _Session()
    try:
        df = trading_halts.fetch_trade_halts(cache_age=0, max_rows=1)
    finally:
        trading_halts.data_provider.cache_get = old_cache_get
        trading_halts.data_provider.cache_put = old_cache_put
        trading_halts.data_provider.get_session = old_get_session

    assert len(df) == 1
    assert df.iloc[0]["symbol"] == "MOVE"
    assert "nasdaq_trade_halts:rss:v1" in stored
    assert stored["nasdaq_trade_halts:rss:v1"][0]["symbol"] == "MOVE"


if __name__ == "__main__":
    test_parse_trade_halt_rss_normalizes_active_and_resumed_rows()
    test_fetch_trade_halts_uses_cache_and_session()
    print("2/2 trading halt tests passed")
