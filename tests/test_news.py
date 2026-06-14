"""Direct-run tests for the no-key news RSS engine."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engines import news


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeSession:
    def __init__(self, google_text: str | None = None, yahoo_text: str | None = None):
        self.google_text = google_text
        self.yahoo_text = yahoo_text
        self.urls: list[str] = []

    def get(self, url: str, headers=None, timeout=None):
        self.urls.append(url)
        if "news.google.com" in url:
            if self.google_text is None:
                return _FakeResponse(500, "")
            return _FakeResponse(200, self.google_text)
        if "feeds.finance.yahoo.com" in url:
            if self.yahoo_text is None:
                return _FakeResponse(500, "")
            return _FakeResponse(200, self.yahoo_text)
        return _FakeResponse(404, "")


def _rss_xml(*titles: str) -> str:
    items = "\n".join(
        f"""
        <item>
          <title>{title}</title>
          <link>https://example.test/{idx}</link>
          <pubDate>Sun, 14 Jun 2026 18:00:00 GMT</pubDate>
        </item>
        """
        for idx, title in enumerate(titles, start=1)
    )
    return f"<rss><channel>{items}</channel></rss>"


def _with_fake_provider(session: _FakeSession, func):
    old_cache_get = news.data_provider.cache_get
    old_cache_put = news.data_provider.cache_put
    old_get_session = news.data_provider.get_session
    old_time = news.time.time
    stored = {}
    fixed_now = datetime(2026, 6, 14, 19, 0, tzinfo=timezone.utc).timestamp()
    try:
        news.data_provider.cache_get = lambda *args, **kwargs: None
        news.data_provider.cache_put = lambda key, value: stored.update({key: value})
        news.data_provider.get_session = lambda: session
        news.time.time = lambda: fixed_now
        return func(stored)
    finally:
        news.data_provider.cache_get = old_cache_get
        news.data_provider.cache_put = old_cache_put
        news.data_provider.get_session = old_get_session
        news.time.time = old_time


def test_yahoo_rss_fallback_when_google_is_blocked():
    session = _FakeSession(google_text=None, yahoo_text=_rss_xml("Apple raises guidance"))

    def run(stored):
        items = news._fetch_rss("AAPL")
        assert len(items) == 1
        assert items[0]["provider"] == "yahoo_finance_rss"
        assert "news:v2:AAPL" in stored
        assert any("news.google.com" in url for url in session.urls)
        assert any("feeds.finance.yahoo.com" in url for url in session.urls)

    _with_fake_provider(session, run)


def test_news_score_includes_combined_source_metadata():
    session = _FakeSession(
        google_text=_rss_xml("Microsoft demand improves"),
        yahoo_text=_rss_xml("Microsoft wins new AI deal"),
    )

    def run(_stored):
        row = news._score_ticker("MSFT")
        assert row["n_24h"] == 2
        assert row["news_provider_count"] == 2
        assert row["news_source"] == "google_news+yahoo_finance_rss"
        assert row["top_headline"]

    _with_fake_provider(session, run)


if __name__ == "__main__":
    test_yahoo_rss_fallback_when_google_is_blocked()
    test_news_score_includes_combined_source_metadata()
    print("2/2 news tests passed")
