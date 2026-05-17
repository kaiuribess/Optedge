"""Async HTTP fan-out helper for engines.

Wraps aiohttp + httpx (whichever is available) to enable concurrent fetches
within a single engine with a per-engine SLA timeout. Falls back to
ThreadPoolExecutor if neither async lib is installed.

Usage:
    from async_http import gather_get
    results = gather_get(["https://api.foo/a", "https://api.foo/b"], timeout=10)
    # results = [{"url": "...", "ok": True, "status": 200, "text": "..."}, ...]
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Dict, List, Optional

log = logging.getLogger("optedge.async_http")


def _aiohttp_available() -> bool:
    try:
        import aiohttp  # noqa
        return True
    except ImportError:
        return False


def _httpx_available() -> bool:
    try:
        import httpx  # noqa
        return True
    except ImportError:
        return False


async def _aio_fetch(session, url: str, timeout: int, headers: Optional[Dict] = None):
    import aiohttp
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout),
                                headers=headers or {}) as resp:
            text = await resp.text()
            return {"url": url, "ok": True, "status": resp.status, "text": text}
    except Exception as e:
        return {"url": url, "ok": False, "status": 0, "error": str(e)[:200], "text": ""}


async def _aio_gather(urls: List[str], timeout: int, headers: Optional[Dict]):
    import aiohttp
    connector = aiohttp.TCPConnector(limit=20, force_close=False, enable_cleanup_closed=True)
    async with aiohttp.ClientSession(connector=connector) as sess:
        tasks = [_aio_fetch(sess, u, timeout, headers) for u in urls]
        return await asyncio.gather(*tasks, return_exceptions=False)


def _httpx_gather(urls: List[str], timeout: int, headers: Optional[Dict]):
    import httpx
    out = []
    transport = httpx.HTTPTransport(retries=1, http2=True)
    with httpx.Client(transport=transport, timeout=timeout, http2=True,
                       headers=headers or {}, limits=httpx.Limits(max_connections=20)) as client:
        # httpx Client is sync but with HTTP/2 multiplexing — still fast
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=16) as ex:
            futs = {ex.submit(client.get, u): u for u in urls}
            for fut in as_completed(futs):
                u = futs[fut]
                try:
                    r = fut.result()
                    out.append({"url": u, "ok": True, "status": r.status_code, "text": r.text})
                except Exception as e:
                    out.append({"url": u, "ok": False, "status": 0,
                                "error": str(e)[:200], "text": ""})
    return out


def _fallback_gather(urls: List[str], timeout: int, headers: Optional[Dict]):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import data_provider
    sess = data_provider.get_session()
    out = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(sess.get, u, timeout=timeout, **({"headers": headers} if headers else {})): u for u in urls}
        for fut in as_completed(futs):
            u = futs[fut]
            try:
                r = fut.result()
                out.append({"url": u, "ok": True, "status": r.status_code, "text": r.text})
            except Exception as e:
                out.append({"url": u, "ok": False, "status": 0,
                            "error": str(e)[:200], "text": ""})
    return out


def gather_get(urls: List[str], timeout: int = 15,
               headers: Optional[Dict] = None,
               mode: str = "auto") -> List[Dict]:
    """Fetch N URLs concurrently. Prefer aiohttp -> httpx -> ThreadPool fallback.

    Returns list of dicts: {url, ok, status, text} (and possibly 'error').
    """
    if not urls:
        return []
    t0 = time.time()
    if mode == "auto":
        if _aiohttp_available():
            mode = "aiohttp"
        elif _httpx_available():
            mode = "httpx"
        else:
            mode = "fallback"
    try:
        if mode == "aiohttp":
            out = asyncio.run(_aio_gather(urls, timeout, headers))
        elif mode == "httpx":
            out = _httpx_gather(urls, timeout, headers)
        else:
            out = _fallback_gather(urls, timeout, headers)
    except Exception as e:
        log.warning("async http %s failed: %s — fallback", mode, e)
        out = _fallback_gather(urls, timeout, headers)
    elapsed = time.time() - t0
    n_ok = sum(1 for r in out if r["ok"])
    log.debug("async_http: %d URLs in %.2fs, %d ok (%s)", len(urls), elapsed, n_ok, mode)
    return out
