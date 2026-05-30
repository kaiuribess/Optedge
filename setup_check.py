"""Optedge — interactive setup & health check.

Run BEFORE your first `python3 run.py` to verify all data sources work.

Tests each data source independently and reports a green/yellow/red status,
with a clear remediation note if anything fails. Saves a `.optedge_status.json`
so `run.py` can auto-fall-back to working sources.
"""
from __future__ import annotations
import json
import logging
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
STATUS_FILE = ROOT / ".optedge_status.json"


GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"


def banner(text: str):
    print(f"\n{BOLD}{text}{RESET}")
    print("─" * len(text))


def ok(msg: str):
    print(f"  {GREEN}✓{RESET} {msg}")


def warn(msg: str):
    print(f"  {YELLOW}!{RESET} {msg}")


def fail(msg: str, hint: str = ""):
    print(f"  {RED}✗{RESET} {msg}")
    if hint:
        print(f"    {DIM}{hint}{RESET}")


def check_python() -> bool:
    banner("Python version")
    v = sys.version_info
    if v >= (3, 9):
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
        return True
    fail(f"Python {v.major}.{v.minor} — need ≥ 3.9",
         "Install Python 3.9+ (e.g. `brew install python@3.11` on macOS)")
    return False


def check_packages() -> bool:
    banner("Required packages")
    needed = [
        ("yfinance", "yfinance"),
        ("pandas", "pandas"),
        ("numpy", "numpy"),
        ("scipy", "scipy"),
        ("requests", "requests"),
        ("vaderSentiment", "vaderSentiment.vaderSentiment"),
        ("pyarrow", "pyarrow"),
        ("scikit-learn", "sklearn"),
    ]
    optional = [("curl_cffi", "curl_cffi"), ("ib_insync", "ib_insync")]
    all_good = True
    for name, importable in needed:
        try:
            __import__(importable)
            ok(f"{name}")
        except ImportError:
            fail(f"{name} not installed", f"pip install {name}")
            all_good = False
    for name, importable in optional:
        try:
            __import__(importable)
            ok(f"{name} (optional, recommended)")
        except ImportError:
            warn(f"{name} not installed — yfinance will work but may be more rate-limited")
    return all_good


def check_yfinance() -> tuple[bool, str]:
    banner("Yahoo Finance (yfinance) — prices, options, fundamentals, macro")
    try:
        import yfinance as yf
    except ImportError:
        fail("yfinance not installed")
        return False, "not installed"

    # Try a simple history fetch
    try:
        # Try with curl_cffi session if available (more reliable)
        try:
            from curl_cffi import requests as creq
            session = creq.Session(impersonate="chrome120")
            tk = yf.Ticker("AAPL", session=session)
        except ImportError:
            tk = yf.Ticker("AAPL")
        h = tk.history(period="5d")
        if h.empty:
            fail("AAPL history returned empty",
                 "Yahoo may be rate-limiting your IP. Wait 5 min and retry, or "
                 "set up Polygon.io (free tier) — see README.")
            return False, "empty"
        last = float(h["Close"].iloc[-1])
        ok(f"AAPL last close: ${last:.2f}")

        # Try options chain
        time.sleep(1)
        opts = tk.options
        if not opts:
            warn("AAPL options expirations empty — options engine will be impaired")
            return True, "history-only"
        ok(f"AAPL options expirations available: {len(opts)} (first: {opts[0]})")

        # Try one chain
        time.sleep(1)
        chain = tk.option_chain(opts[0])
        if chain.calls.empty:
            warn("Options chain returned empty calls")
            return True, "history-only"
        ok(f"AAPL chain: {len(chain.calls)} calls, {len(chain.puts)} puts")
        return True, "full"
    except Exception as e:
        msg = str(e)[:120]
        if "rate" in msg.lower() or "429" in msg:
            fail(f"Yahoo rate-limited this IP: {msg}",
                 "Common from datacenter/VPN IPs. Try a residential connection, "
                 "wait 30 min, or use Polygon.io free tier — see README.")
        else:
            fail(f"yfinance error: {msg}",
                 "Check internet connection and try again.")
        return False, "blocked"


def check_reddit() -> tuple[bool, str]:
    banner("Reddit — sentiment data (no auth required)")
    try:
        import requests
        r = requests.get(
            "https://www.reddit.com/r/wallstreetbets/new.json?limit=5",
            headers={"User-Agent": "optedge-research/0.1"},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            n = len(data.get("data", {}).get("children", []))
            ok(f"r/wallstreetbets returned {n} posts")
            return True, "ok"
        elif r.status_code == 429:
            fail("Reddit rate-limited (429)",
                 "Wait 5 min and retry. Reddit's free JSON has aggressive rate limits.")
            return False, "rate_limited"
        elif r.status_code == 403:
            fail("Reddit blocked this IP (403)",
                 "Common from datacenter IPs. Sentiment will be skipped automatically; "
                 "system still runs without it. Use --skip-sentiment to silence the warning.")
            return False, "blocked"
        else:
            fail(f"Reddit returned HTTP {r.status_code}",
                 "System still runs; sentiment will be skipped.")
            return False, f"http_{r.status_code}"
    except Exception as e:
        fail(f"Reddit error: {str(e)[:100]}",
             "System still runs; sentiment will be skipped.")
        return False, "error"


def check_sec() -> tuple[bool, str]:
    banner("SEC EDGAR — insider Form 4 data (no auth required)")
    try:
        import requests
        r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": "optedge-research/0.1 (research@optedge.local)"},
            timeout=15,
        )
        if r.status_code == 200:
            n = len(r.json())
            ok(f"EDGAR ticker map: {n:,} companies")
            # Try fetching a sample submission
            time.sleep(0.2)
            r2 = requests.get(
                "https://data.sec.gov/submissions/CIK0000320193.json",  # AAPL
                headers={"User-Agent": "optedge-research/0.1 (research@optedge.local)"},
                timeout=15,
            )
            if r2.status_code == 200:
                ok("Form 4 submissions endpoint reachable")
                return True, "ok"
            warn(f"submissions endpoint returned {r2.status_code} — insider engine may be impaired")
            return True, "partial"
        fail(f"EDGAR ticker map returned HTTP {r.status_code}",
             "SEC sometimes throttles. Wait a minute and retry.")
        return False, f"http_{r.status_code}"
    except Exception as e:
        fail(f"EDGAR error: {str(e)[:100]}")
        return False, "error"


def check_macro() -> tuple[bool, str]:
    banner("Macro — VIX / yields (uses yfinance)")
    try:
        import yfinance as yf
        try:
            from curl_cffi import requests as creq
            session = creq.Session(impersonate="chrome120")
            vix_tk = yf.Ticker("^VIX", session=session)
        except ImportError:
            vix_tk = yf.Ticker("^VIX")
        h = vix_tk.history(period="5d")
        if h.empty:
            warn("VIX returned empty — macro engine will use defaults")
            return False, "empty"
        ok(f"VIX last close: {float(h['Close'].iloc[-1]):.2f}")
        return True, "ok"
    except Exception as e:
        warn(f"macro check failed: {str(e)[:100]}")
        return False, "error"


def maybe_setup_fred() -> str:
    banner("FRED API key — optional (richer macro data)")
    existing = os.environ.get("FRED_API_KEY")
    if existing:
        ok(f"FRED_API_KEY already set ({existing[:6]}…)")
        return existing
    print(f"  {DIM}FRED is optional. Adds CPI, unemployment, and more macro series.{RESET}")
    print(f"  {DIM}Get a free key in 30s: https://fredaccount.stlouisfed.org/apikey{RESET}")
    try:
        ans = input("  Have a FRED key now? Paste it (or press Enter to skip): ").strip()
    except EOFError:
        ans = ""
    if ans:
        ok(f"Saved FRED_API_KEY for this session.")
        os.environ["FRED_API_KEY"] = ans
        print(f"    {DIM}To make permanent, add to your shell profile:{RESET}")
        print(f"    {DIM}  export FRED_API_KEY='{ans}'{RESET}")
        return ans
    warn("FRED skipped — macro engine will run on yfinance VIX/yields only")
    return ""


def main():
    print(f"{BOLD}╭─────────────────────────────────╮{RESET}")
    print(f"{BOLD}│  Optedge — setup health check   │{RESET}")
    print(f"{BOLD}╰─────────────────────────────────╯{RESET}")

    py_ok = check_python()
    pkg_ok = check_packages()
    if not (py_ok and pkg_ok):
        print(f"\n{RED}Critical setup issue. Fix the items above and re-run.{RESET}")
        return 1

    yf_ok, yf_state = check_yfinance()
    reddit_ok, reddit_state = check_reddit()
    sec_ok, sec_state = check_sec()
    macro_ok, macro_state = check_macro()
    fred = maybe_setup_fred() if sys.stdin.isatty() else os.environ.get("FRED_API_KEY", "")

    status = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "yfinance": {"ok": yf_ok, "state": yf_state},
        "reddit":   {"ok": reddit_ok, "state": reddit_state},
        "sec":      {"ok": sec_ok, "state": sec_state},
        "macro":    {"ok": macro_ok, "state": macro_state},
        "fred":     {"ok": bool(fred), "state": "ok" if fred else "no_key"},
    }
    STATUS_FILE.write_text(json.dumps(status, indent=2))

    banner("Summary")
    if yf_ok and yf_state == "full":
        ok("Live mode is fully operational. Run: python3 run.py")
    elif yf_ok:
        warn("Live mode partially working — options chain may be impaired.")
        print(f"    {DIM}Run: python3 run.py — system will degrade gracefully.{RESET}")
    else:
        fail("Live mode unavailable from this network/IP.",
             "Run `python3 run.py --demo` to use synthetic data, or "
             "set up Polygon.io free tier (see README) and run with --polygon.")

    if not reddit_ok:
        print(f"    {DIM}Sentiment: will be skipped automatically. Use --skip-sentiment to silence.{RESET}")
    if not sec_ok:
        print(f"    {DIM}Insider: will be empty. The other 4 engines still produce signals.{RESET}")

    print(f"\n{DIM}Status saved to: {STATUS_FILE}{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
