"""Optedge — orchestrator. Long-only options + small-cap shares + futures + value plays.

Engines run in parallel. WSB trending tickers added at runtime. Each engine
internally parallelizes per-ticker work.

First run? Run `python setup_check.py` first to verify data sources.
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# CRITICAL: print something immediately so silent failures are obvious.
print("Optedge starting…", flush=True)

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from config import (UNIVERSE, UNIVERSE_OPTIONS, UNIVERSE_SHARES, ASOF,
                        TOP_N_CALLS, TOP_N_PUTS, TOP_N_SHARES,
                        TOP_N_VALUE, TOP_N_FUTURES,
                        WSB_TRENDING_TOP_N, WSB_TRENDING_MIN_MENTIONS,
                        ENGINE_CONCURRENT)
    import config as _config
    # v20: optional config keys with defaults (in case user runs over an
    # old config.py that doesn't have them yet)
    ENGINE_SLA_SECONDS = getattr(_config, "ENGINE_SLA_SECONDS", {})
    UNIVERSE_PREFILTER_TOP_N = getattr(_config, "UNIVERSE_PREFILTER_TOP_N", 300)
    UNIVERSE_PREFILTER_ENABLED = getattr(_config, "UNIVERSE_PREFILTER_ENABLED", True)
    HEDGE_DELTA_THRESHOLD = getattr(_config, "HEDGE_DELTA_THRESHOLD", 5000.0)
    DRAWDOWN_BREAKER_ENABLED = getattr(_config, "DRAWDOWN_BREAKER_ENABLED", True)
    from engines import mispricing, sentiment, fundamentals, insider, macro
    from engines import wsb_trending, news, earnings, value, futures, congress, social, analyst
    from fusion import rank as fusion_rank
    from backtest import track as backtest_track
    from backtest import predictor as bt_predictor
    from backtest import sizing as bt_sizing
    from dashboard import build as dash_build
    import data_provider
    # v20: telemetry + universe filter (best-effort imports)
    try:
        from telemetry import perf as _perf
        from telemetry import cache_stats as _cache_stats
        _cache_stats.install_hooks()
    except Exception as _telem_err:
        _perf = None
        _cache_stats = None
    try:
        import universe_filter as _ufilter
    except Exception:
        _ufilter = None

    # If config_runtime.py exists (auto-retrain output), override SIGNAL_WEIGHTS
    _runtime_weights = bt_predictor.load_runtime_weights()
    if _runtime_weights:
        _config.SIGNAL_WEIGHTS = _runtime_weights
        # Also patch the imported module so fusion sees the override
        import fusion.rank as _fr_module
        _fr_module.SIGNAL_WEIGHTS = _runtime_weights
        print(f"  [auto-retrain] using runtime SIGNAL_WEIGHTS: top factor "
              f"= {max(_runtime_weights, key=_runtime_weights.get)} "
              f"({_runtime_weights[max(_runtime_weights, key=_runtime_weights.get)]:.2f})", flush=True)
except Exception as e:
    print(f"\nIMPORT ERROR — Optedge can't start.\n{e}\n", flush=True)
    traceback.print_exc()
    sys.exit(2)


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # yfinance is very chatty when tickers are delisted or are ETFs (no fundamentals).
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    logging.getLogger("peewee").setLevel(logging.CRITICAL)


def _to_df(v):
    """Coerce engine result to DataFrame. Engines may return None on failure.

    Note: do NOT use `v or pd.DataFrame()` — pandas refuses to evaluate
    DataFrame truthiness ('The truth value of a DataFrame is ambiguous').
    """
    return v if isinstance(v, pd.DataFrame) else pd.DataFrame()


def auto_detect_mode(args) -> tuple:
    """Read .optedge_status.json (if present) and return (use_demo, skip_flags)."""
    status = data_provider.status()
    if not status:
        return args.demo, {}

    yf_ok = status.get("yfinance", {}).get("ok", False)
    reddit_ok = status.get("reddit", {}).get("ok", False)
    sec_ok = status.get("sec", {}).get("ok", False)

    skip = {}
    use_demo = args.demo
    if not yf_ok and not args.demo:
        print("\n" + "=" * 70)
        print("  yfinance is not working from your network (per setup_check.py).")
        print("  Falling back to DEMO MODE.")
        print("=" * 70 + "\n")
        use_demo = True
    if not reddit_ok and not use_demo:
        skip["sentiment"] = True
        skip["wsb_trending"] = True
    if not sec_ok and not use_demo:
        skip["insider"] = True
    return use_demo, skip


def run_engines_concurrent(universe_options, universe_all, skip_sentiment,
                           skip_insider, skip_fund, skip_news=False, skip_earnings=False,
                           skip_value=False, skip_futures=False, skip_congress=False,
                           skip_social=False, skip_analyst=False,
                           universe_heavy=None, skip_v20=None):
    """Dispatch all ticker-driven engines in parallel.

    v20: universe_heavy is the pre-filtered subset for slow per-ticker engines.
    universe_all is used for broadcast engines that just project hardcoded
    sector signals onto any ticker passed.
    """
    log = logging.getLogger("optedge")
    log.info("dispatching engines concurrently …")
    # If pre-filter not provided, treat heavy = all
    universe_heavy = universe_heavy or universe_all
    skip_v20 = skip_v20 or {}
    def _v20_on(name):
        return not skip_v20.get(name, False)

    tasks = {}
    tasks["macro"] = lambda: macro.run()
    tasks["mispricing"] = lambda: mispricing.run(universe_options)
    if not skip_sentiment:
        tasks["sentiment"] = lambda: sentiment.run(universe_heavy)
    if not skip_fund:
        tasks["fundamentals"] = lambda: fundamentals.run(universe_heavy)
    if not skip_insider:
        tasks["insider"] = lambda: insider.run(universe_heavy)
    if not skip_news:
        tasks["news"] = lambda: news.run(universe_heavy)
    if not skip_earnings:
        tasks["earnings"] = lambda: earnings.run(universe_heavy)
    if not skip_value:
        tasks["value"] = lambda: value.run(universe_heavy)
    if not skip_futures:
        tasks["futures"] = lambda: futures.run()
    if not skip_congress:
        tasks["congress"] = lambda: congress.run(universe_heavy)
    if not skip_social:
        tasks["social"] = lambda: social.run(universe_heavy)
    if not skip_analyst:
        tasks["analyst"] = lambda: analyst.run(universe_heavy)
    # v17 new engines — heavy engines get pre-filtered universe, broadcast
    # engines get full universe
    try:
        from engines import sector_rs as _sector_rs_mod
        tasks["sector_rs"] = lambda: _sector_rs_mod.run(universe_heavy)
    except Exception:
        pass
    try:
        from engines import dark_pool as _dark_pool_mod
        tasks["dark_pool"] = lambda: _dark_pool_mod.run(universe_heavy)
    except Exception:
        pass
    try:
        from engines import fda_calendar as _fda_mod
        tasks["fda"] = lambda: _fda_mod.run(universe_heavy)
    except Exception:
        pass
    try:
        from engines import sector_etf_flow as _flow_mod
        def _sector_flow_task():
            sf = _flow_mod.run()
            return _flow_mod.per_ticker_score(universe_all, sf)  # broadcast = full
        tasks["sector_flow"] = _sector_flow_task
    except Exception:
        pass
    try:
        from engines import technicals as _tech_mod
        tasks["technicals"] = lambda: _tech_mod.run(universe_heavy)
    except Exception:
        pass
    try:
        from engines import short_interest as _short_int_mod
        tasks["short_int"] = lambda: _short_int_mod.run(universe_heavy)
    except Exception:
        pass
    # v20 — Tier B new engines (broadcast: full universe; heavy: pre-filtered)
    if _v20_on("cot"):
        try:
            from engines import cot as _cot_mod
            tasks["cot"] = lambda: _cot_mod.run(universe_all)
        except Exception: pass
    if _v20_on("thirteen_f"):
        try:
            from engines import thirteen_f as _13f_mod
            tasks["thirteen_f"] = lambda: _13f_mod.run(universe_heavy)
        except Exception: pass
    if _v20_on("vix_term"):
        try:
            from engines import vix_term as _vt_mod
            tasks["vix_term"] = lambda: _vt_mod.run(universe_all)
        except Exception: pass
    if _v20_on("eia"):
        try:
            from engines import eia as _eia_mod
            tasks["eia"] = lambda: _eia_mod.run(universe_all)
        except Exception: pass
    if _v20_on("wasde"):
        try:
            from engines import wasde as _wasde_mod
            tasks["wasde"] = lambda: _wasde_mod.run(universe_all)
        except Exception: pass
    if _v20_on("buybacks"):
        try:
            from engines import buybacks as _bb_mod
            tasks["buybacks"] = lambda: _bb_mod.run(universe_heavy)
        except Exception: pass
    if _v20_on("gtrends"):
        try:
            from engines import google_trends as _gt_mod
            tasks["gtrends"] = lambda: _gt_mod.run(universe_heavy)
        except Exception: pass
    if _v20_on("form_144"):
        try:
            from engines import form_144 as _f144_mod
            tasks["form_144"] = lambda: _f144_mod.run(universe_heavy)
        except Exception: pass
    if _v20_on("whisper"):
        try:
            from engines import whisper as _whisper_mod
            tasks["whisper"] = lambda: _whisper_mod.run(universe_heavy)
        except Exception: pass
    if _v20_on("hyperliquid"):
        try:
            from engines import hyperliquid as _hl_mod
            tasks["hyperliquid"] = lambda: _hl_mod.run(universe_all)
        except Exception: pass
    # v20 — Tier C new engines
    if _v20_on("twitter"):
        try:
            from engines import nitter as _twitter_mod
            tasks["twitter"] = lambda: _twitter_mod.run(universe_heavy)
        except Exception: pass
    if _v20_on("r_options"):
        try:
            from engines import r_options as _ropt_mod
            tasks["r_options"] = lambda: _ropt_mod.run(universe_heavy)
        except Exception: pass
    # v20 — Tier D new engines (broadcast — hardcoded sector buckets)
    if _v20_on("yield_curve"):
        try:
            from engines import yield_curve_pca as _ycp_mod
            tasks["yield_curve"] = lambda: _ycp_mod.run(universe_all)
        except Exception: pass
    if _v20_on("credit_spread"):
        try:
            from engines import credit_spread as _cs_mod
            tasks["credit_spread"] = lambda: _cs_mod.run(universe_all)
        except Exception: pass

    results = {}
    timings = {}
    SLA_MAP = ENGINE_SLA_SECONDS

    def _runner(name, fn):
        """Wrap engine call with telemetry tracking."""
        import time as _t
        t0 = _t.time()
        try:
            r = fn()
            elapsed = _t.time() - t0
            rows = 0
            try:
                # Engines that return DataFrames -> len() is correct.
                # Engines that return dicts (e.g. mispricing -> {contracts, summary})
                # should report the contracts row count, not len(dict)==2.
                if isinstance(r, dict):
                    if "contracts" in r and hasattr(r["contracts"], "__len__"):
                        rows = len(r["contracts"])
                    elif "rows" in r and hasattr(r["rows"], "__len__"):
                        rows = len(r["rows"])
                    else:
                        rows = sum(len(v) for v in r.values()
                                    if hasattr(v, "__len__"))
                elif r is not None and hasattr(r, "__len__"):
                    rows = len(r)
            except Exception:
                pass
            timings[name] = {"elapsed": elapsed, "rows": rows, "ok": True}
            # Best-effort telemetry write
            if _perf:
                try:
                    with _perf.track(name) as t:
                        t.elapsed = elapsed
                        t.set_rows(rows)
                except Exception:
                    pass
            return r
        except Exception as e:
            timings[name] = {"elapsed": _t.time() - t0, "rows": 0, "ok": False,
                              "error": str(e)[:200]}
            raise

    with ThreadPoolExecutor(max_workers=max(8, len(tasks))) as ex:
        future_map = {ex.submit(_runner, name, fn): name for name, fn in tasks.items()}
        for fut in as_completed(future_map):
            name = future_map[fut]
            sla = SLA_MAP.get(name, 300)
            try:
                results[name] = fut.result(timeout=sla)
                t = timings.get(name, {})
                log.info("✓ %s engine completed (%.1fs, %d rows)", name,
                         t.get("elapsed", 0), t.get("rows", 0))
            except Exception as e:
                log.error("✗ %s engine failed/timeout: %s", name, str(e)[:200])
                results[name] = None

    results["_timings"] = timings
    return results


def main():
    ap = argparse.ArgumentParser(description="Optedge — long-only options/shares/futures/value ranker")
    ap.add_argument("--universe", nargs="*", default=None,
                    help="Override universe (default: config.UNIVERSE + WSB trending)")
    ap.add_argument("--max-calls", type=int, default=TOP_N_CALLS)
    ap.add_argument("--max-puts", type=int, default=TOP_N_PUTS)
    ap.add_argument("--max-shares", type=int, default=TOP_N_SHARES)
    ap.add_argument("--max-value", type=int, default=TOP_N_VALUE)
    ap.add_argument("--max-futures", type=int, default=TOP_N_FUTURES)
    ap.add_argument("--skip-sentiment", action="store_true")
    ap.add_argument("--skip-insider", action="store_true")
    ap.add_argument("--skip-fundamentals", action="store_true")
    ap.add_argument("--skip-wsb", action="store_true")
    ap.add_argument("--skip-news", action="store_true")
    ap.add_argument("--skip-earnings", action="store_true")
    ap.add_argument("--skip-value", action="store_true")
    ap.add_argument("--skip-futures", action="store_true")
    ap.add_argument("--skip-congress", action="store_true")
    ap.add_argument("--skip-social", action="store_true",
                    help="Skip StockTwits + Trump Truth Social engine")
    ap.add_argument("--skip-analyst", action="store_true",
                    help="Skip Finnhub analyst recommendations engine")
    # v20.1 — per-engine skip flags for the v20 new factor set
    ap.add_argument("--skip-cot", action="store_true",
                    help="Skip CFTC Commitments of Traders engine (v20)")
    ap.add_argument("--skip-13f", action="store_true",
                    help="Skip SEC 13F smart-money engine (v20)")
    ap.add_argument("--skip-vix-term", action="store_true",
                    help="Skip CBOE VIX term-structure engine (v20)")
    ap.add_argument("--skip-eia", action="store_true",
                    help="Skip EIA petroleum/natgas inventory engine (v20)")
    ap.add_argument("--skip-wasde", action="store_true",
                    help="Skip USDA WASDE ag engine (v20)")
    ap.add_argument("--skip-buybacks", action="store_true",
                    help="Skip SEC 8-K buyback scanner (v20)")
    ap.add_argument("--skip-gtrends", action="store_true",
                    help="Skip Google Trends engine (v20)")
    ap.add_argument("--skip-form-144", action="store_true",
                    help="Skip SEC Form 144 pre-sale engine (v20)")
    ap.add_argument("--skip-whisper", action="store_true",
                    help="Skip earnings whisper engine (v20)")
    ap.add_argument("--skip-hyperliquid", action="store_true",
                    help="Skip Hyperliquid perp OI engine (v20)")
    ap.add_argument("--skip-twitter", action="store_true",
                    help="Skip Twitter/Apewisdom engine (v20)")
    ap.add_argument("--skip-r-options", action="store_true",
                    help="Skip r/options sticky engine (v20)")
    ap.add_argument("--skip-yield-curve", action="store_true",
                    help="Skip FRED yield-curve PCA engine (v20)")
    ap.add_argument("--skip-credit-spread", action="store_true",
                    help="Skip FRED IG/HY credit spread engine (v20)")
    ap.add_argument("--skip-finbert", action="store_true",
                    help="Skip FinBERT sentiment engine (v20.3, GPU-aware, optional)")
    ap.add_argument("--fast-insider", action="store_true",
                    help="Insider engine: count-only mode (skip XML parsing, ~5x faster)")
    ap.add_argument("--demo", action="store_true",
                    help="Force synthetic data (sandbox / first-look mode)")
    ap.add_argument("--no-auto-detect", action="store_true")
    ap.add_argument("--sequential", action="store_true",
                    help="Run engines sequentially (debug mode)")
    ap.add_argument("--forward", action="store_true",
                    help="Forward test only — replay logged signals with current prices")
    ap.add_argument("--backtest", action="store_true",
                    help="Historical backtest — IC analysis on 7/30/60/90d forward returns")
    ap.add_argument("--bankroll", type=float, default=10000,
                    help="Account size used for Kelly position sizing (default $10K)")
    ap.add_argument("--aggressive", action="store_true",
                    help="½ Kelly + 10%% per option / 15%% per share caps (vs default ¼ Kelly + 5/8%%)")
    ap.add_argument("--loop", type=int, metavar="MINUTES",
                    help="Run continuously, sleeping N minutes between iterations")
    ap.add_argument("--no-open", action="store_true",
                    help="Don't auto-open the dashboard in browser when done")
    ap.add_argument("--quiet", action="store_true",
                    help="Reduce log verbosity (only WARNING and above)")
    ap.add_argument("--minimal", action="store_true",
                    help="Lightweight preset: skip slow engines (wsb, news, congress, social, analyst). ~30s runs.")
    ap.add_argument("--out-dir", default=str(ROOT / "data"))
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    # --quiet overrides log_level
    setup_logging("WARNING" if args.quiet else args.log_level)
    log = logging.getLogger("optedge")
    t_start = datetime.now()
    run_asof = datetime.now(timezone.utc)

    # --minimal preset: stack of skip flags for fast iteration
    if args.minimal:
        args.skip_wsb = True
        args.skip_news = True
        args.skip_congress = True
        args.skip_social = True
        args.skip_analyst = True
        args.fast_insider = True

    # Standalone modes — forward / backtest only
    if args.forward:
        from backtest.forward import run_forward_test, _load_all_logs
        log.info("== FORWARD TEST ==")
        result = run_forward_test()
        if result["signals"].empty:
            sigs = _load_all_logs()
            if sigs.empty:
                print("\nNo logged signals yet — run `python run.py` first.")
                print("Each daily run logs its top picks to logs/signals_*.parquet")
                print("automatically. After 1+ runs, --forward will replay them.")
            else:
                print(f"\nFound {len(sigs)} logged signals across "
                      f"{len(set(sigs['log_time'].astype(str)))} runs, but couldn't")
                print("fetch current prices to re-price them. Common causes:")
                print("  - yfinance is rate-limited from your IP")
                print("  - You're offline")
                print("  - All logged tickers are now delisted")
                print("Try again in 15 min, or run from a residential IP.")
            return 0
        ovr = result["overall"]
        print(f"\n=== FORWARD TEST RESULTS ===")
        print(f"  Signals tracked:    {ovr['n_signals']}")
        print(f"  Win rate:           {ovr['win_rate']*100:.1f}%")
        print(f"  Avg P&L:            {ovr['avg_pnl_pct']*100:+.2f}%")
        print(f"  Median P&L:         {ovr['median_pnl_pct']*100:+.2f}%")
        print(f"  Best winner:        {ovr['best']*100:+.1f}%")
        print(f"  Worst loser:        {ovr['worst']*100:+.1f}%")
        if not result["by_confidence"].empty:
            print("\n  By confidence bucket:")
            for _, r in result["by_confidence"].iterrows():
                print(f"    {r['bucket']:<14} n={int(r['n']):>4}  win={r['win_rate']*100:>5.1f}%  avg P&L={r['avg_pnl']*100:+.2f}%")
        if not result["by_type"].empty:
            print("\n  By signal type:")
            for _, r in result["by_type"].iterrows():
                print(f"    {r['type']:<6}  n={int(r['n']):>4}  win={r['win_rate']*100:>5.1f}%  avg P&L={r['avg_pnl']*100:+.2f}%")
        # Save
        out_dir = Path(args.out_dir); out_dir.mkdir(exist_ok=True)
        asof_tag = run_asof.strftime("%Y%m%d_%H%M%S")
        result["signals"].to_parquet(out_dir / f"forward_test_{asof_tag}.parquet", index=False)
        return 0

    if args.backtest:
        from backtest.historical import run_historical_backtest
        log.info("== HISTORICAL BACKTEST ==")
        # We need factor scores. Run the live engines (or demo) to get them.
        if args.demo or auto_detect_mode(args)[0]:
            from demo_data import (synthetic_fundamentals, synthetic_sentiment,
                                    synthetic_insider, synthetic_value)
            log.info("(using synthetic factor scores for backtest)")
            fund_df = synthetic_fundamentals(UNIVERSE)
            sent_df = synthetic_sentiment(UNIVERSE)
            ins_df = synthetic_insider(UNIVERSE)
            val_df = synthetic_value(UNIVERSE)
        else:
            log.info("Computing live factor scores …")
            fund_df = fundamentals.run(UNIVERSE)
            sent_df = sentiment.run(UNIVERSE)
            ins_df = insider.run(UNIVERSE)
            val_df = value.run(UNIVERSE)
        result = run_historical_backtest(
            UNIVERSE,
            factor_dfs={
                "value_score": val_df,
                "fund_score": fund_df,
                "sentiment_delta": sent_df,
                "insider_score": ins_df,
            },
        )
        ic = result["ic"]
        rets = result["returns"]
        if ic.empty:
            if rets.empty:
                print("\nBacktest produced no usable data.")
                print("Couldn't fetch historical prices for any ticker. Most likely:")
                print("  - yfinance is rate-limited from your IP (try in 15 min)")
                print("  - You're offline")
                print("  - You're running from a datacenter / VPN IP")
            else:
                print(f"\nBacktest fetched {len(rets)} return rows but no IC computed.")
                print("This usually means factor scores didn't merge in.")
                print("Run without --demo for live factor scores.")
            return 0
        print("\n=== HISTORICAL BACKTEST — Information Coefficient ===")
        print("(IC > 0.05 is meaningful, > 0.10 is strong)")
        for h in [7, 30, 60, 90]:
            sub = ic[ic["horizon_days"] == h].sort_values("ic", ascending=False)
            if sub.empty:
                continue
            print(f"\n  {h}-day horizon:")
            for _, r in sub.iterrows():
                print(f"    {r['factor']:<20}  IC={r['ic']:>+.3f}  Q5-Q1 spread={r['spread']*100:>+5.1f}%  n={int(r['n'])}")
        out_dir = Path(args.out_dir); out_dir.mkdir(exist_ok=True)
        asof_tag = run_asof.strftime("%Y%m%d_%H%M%S")
        ic.to_parquet(out_dir / f"backtest_ic_{asof_tag}.parquet", index=False)
        result["returns"].to_parquet(out_dir / f"backtest_returns_{asof_tag}.parquet", index=False)
        return 0

    if not args.no_auto_detect:
        use_demo, auto_skip = auto_detect_mode(args)
    else:
        use_demo, auto_skip = args.demo, {}
    skip_sentiment = args.skip_sentiment or auto_skip.get("sentiment", False)
    skip_insider = args.skip_insider or auto_skip.get("insider", False)
    skip_wsb = args.skip_wsb or auto_skip.get("wsb_trending", False) or use_demo

    if args.fast_insider:
        import config
        config.INSIDER_FAST_MODE = True

    universe_static = args.universe or UNIVERSE
    universe_options_static = args.universe or UNIVERSE_OPTIONS

    trending = []
    trending_meta = []   # list of {ticker, score, mentions, ups}
    if not skip_wsb and not args.universe:
        log.info("== WSB trending discovery ==")
        try:
            trending_meta = wsb_trending.get_trending_with_metadata(
                valid_universe=universe_static, top_n=WSB_TRENDING_TOP_N,
                min_mentions=WSB_TRENDING_MIN_MENTIONS,
            )
            trending = [t["ticker"] for t in trending_meta]
            if trending:
                log.info("added %d trending tickers from WSB (top: %s)",
                         len(trending),
                         ", ".join(f"{t['ticker']}({t['mentions']})" for t in trending_meta[:5]))
        except Exception as e:
            log.warning("wsb trending failed: %s", e)

    universe_options = list(dict.fromkeys(universe_options_static + trending))
    universe_shares = UNIVERSE_SHARES
    universe_all = list(dict.fromkeys(universe_static + trending))
    log.info("universe: options=%d  shares=%d  all=%d  (WSB added: %d)",
             len(universe_options), len(universe_shares), len(universe_all), len(trending))

    # v20 Tier A — Universe pre-filter for slow per-ticker engines.
    # The full universe goes to engines that don't fan out per-ticker (macro,
    # vix_term, sector_flow, futures). The pre-filtered subset goes to
    # everything per-ticker (mispricing chains, insider, news, value, etc.).
    universe_heavy = universe_all
    if UNIVERSE_PREFILTER_ENABLED and _ufilter is not None and len(universe_all) > UNIVERSE_PREFILTER_TOP_N:
        try:
            universe_heavy = _ufilter.filter_for_heavy_engines(
                universe_all, top_n=UNIVERSE_PREFILTER_TOP_N,
                include_trending=trending, include_priors=True,
            )
            log.info("universe pre-filter: heavy engines see %d of %d tickers",
                     len(universe_heavy), len(universe_all))
        except Exception as e:
            log.warning("universe pre-filter failed: %s — using full", e)
            universe_heavy = universe_all
    # universe_options stays full (chain pulls are critical for any options pick)
    # universe_all for shares is also kept full so we don't shrink the share pool

    news_df = pd.DataFrame()
    earn_df = pd.DataFrame()
    value_df = pd.DataFrame()
    futures_df = pd.DataFrame()
    congress_df = pd.DataFrame()
    social_df = pd.DataFrame()
    analyst_df = pd.DataFrame()

    if use_demo:
        from demo_data import (synthetic_mispricing, synthetic_sentiment,
                                synthetic_fundamentals, synthetic_insider, synthetic_macro,
                                synthetic_news, synthetic_earnings,
                                synthetic_value, synthetic_futures, synthetic_congress,
                                synthetic_social, synthetic_analyst)
        log.info("== DEMO/HYBRID MODE ==")
        macro_state = synthetic_macro()
        mp = synthetic_mispricing(universe_options, run_asof)
        contracts = mp["contracts"]; summary = mp["summary"]
        sent_df = pd.DataFrame() if skip_sentiment else synthetic_sentiment(universe_all)
        fund_df = pd.DataFrame() if args.skip_fundamentals else synthetic_fundamentals(universe_all)
        news_df = pd.DataFrame() if args.skip_news else synthetic_news(universe_all)
        earn_df = pd.DataFrame() if args.skip_earnings else synthetic_earnings(universe_all)
        value_df = pd.DataFrame() if args.skip_value else synthetic_value(universe_all)
        futures_df = pd.DataFrame() if args.skip_futures else synthetic_futures()
        # Congress: try LIVE first (works if Stock Watcher S3 is reachable), else synthetic
        if not args.skip_congress:
            try:
                live_congress = congress.run(universe_all)
                congress_df = live_congress if not live_congress.empty else synthetic_congress(universe_all)
            except Exception as e:
                log.warning("live congress failed: %s — using synthetic", e)
                congress_df = synthetic_congress(universe_all)
        # Social: try LIVE first (StockTwits usually works), else synthetic
        if not args.skip_social:
            try:
                live_social = social.run(universe_all)
                social_df = live_social if not live_social.empty else synthetic_social(universe_all)
            except Exception as e:
                log.warning("live social failed: %s — using synthetic", e)
                social_df = synthetic_social(universe_all)
        # Analyst: try LIVE Finnhub first, else synthetic
        if not args.skip_analyst:
            try:
                live_analyst = analyst.run(universe_all)
                analyst_df = live_analyst if not live_analyst.empty else synthetic_analyst(universe_all)
            except Exception as e:
                log.warning("live analyst failed: %s — using synthetic", e)
                analyst_df = synthetic_analyst(universe_all)
        # If SEC works, use real insider data even in demo mode
        status = data_provider.status()
        if not skip_insider and status.get("sec", {}).get("ok", False):
            log.info("== Insider (LIVE — SEC EDGAR works from this IP) ==")
            try:
                ins_df = insider.run(universe_all)
            except Exception as e:
                log.warning("live insider failed: %s — using synthetic", e)
                ins_df = synthetic_insider(universe_all)
        else:
            ins_df = pd.DataFrame() if skip_insider else synthetic_insider(universe_all)
    else:
        log.info("== LIVE DATA ==")
        if ENGINE_CONCURRENT and not args.sequential:
            skip_v20 = {
                "cot": args.skip_cot, "thirteen_f": args.skip_13f,
                "vix_term": args.skip_vix_term, "eia": args.skip_eia,
                "wasde": args.skip_wasde, "buybacks": args.skip_buybacks,
                "gtrends": args.skip_gtrends, "form_144": args.skip_form_144,
                "whisper": args.skip_whisper, "hyperliquid": args.skip_hyperliquid,
                "twitter": args.skip_twitter, "r_options": args.skip_r_options,
                "yield_curve": args.skip_yield_curve,
                "credit_spread": args.skip_credit_spread,
            }
            results = run_engines_concurrent(
                universe_options, universe_all,
                skip_sentiment, skip_insider, args.skip_fundamentals,
                skip_news=args.skip_news, skip_earnings=args.skip_earnings,
                skip_value=args.skip_value, skip_futures=args.skip_futures,
                skip_congress=args.skip_congress,
                skip_social=args.skip_social,
                skip_analyst=args.skip_analyst,
                universe_heavy=universe_heavy,
                skip_v20=skip_v20,
            )
            macro_state = results.get("macro") or {"regime": "neutral", "macro_tilt": 0.0}
            mp = results.get("mispricing") or {"contracts": pd.DataFrame(), "summary": pd.DataFrame()}
            contracts = _to_df(mp.get("contracts"))
            summary = _to_df(mp.get("summary"))
            sent_df = _to_df(results.get("sentiment"))
            fund_df = _to_df(results.get("fundamentals"))
            ins_df = _to_df(results.get("insider"))
            news_df = _to_df(results.get("news"))
            earn_df = _to_df(results.get("earnings"))
            value_df = _to_df(results.get("value"))
            futures_df = _to_df(results.get("futures"))
            congress_df = _to_df(results.get("congress"))
            social_df = _to_df(results.get("social"))
            analyst_df = _to_df(results.get("analyst"))
        else:
            log.info("== Macro =="); macro_state = macro.run()
            mp = mispricing.run(universe_options)
            contracts = mp["contracts"]; summary = mp["summary"]
            sent_df = pd.DataFrame() if skip_sentiment else sentiment.run(universe_all)
            fund_df = pd.DataFrame() if args.skip_fundamentals else fundamentals.run(universe_all)
            ins_df = pd.DataFrame() if skip_insider else insider.run(universe_all)
            news_df = pd.DataFrame() if args.skip_news else news.run(universe_all)
            earn_df = pd.DataFrame() if args.skip_earnings else earnings.run(universe_all)
            value_df = pd.DataFrame() if args.skip_value else value.run(universe_all)
            futures_df = pd.DataFrame() if args.skip_futures else futures.run()

        if contracts.empty:
            log.error("No option contracts returned. Yahoo is likely rate-limited from this IP. "
                      "Falling back to demo for THIS run.")
            from demo_data import (synthetic_mispricing, synthetic_sentiment,
                                    synthetic_fundamentals, synthetic_insider, synthetic_macro,
                                    synthetic_news, synthetic_earnings,
                                    synthetic_value, synthetic_futures)
            use_demo = True
            macro_state = synthetic_macro()
            mp = synthetic_mispricing(universe_options, run_asof)
            contracts = mp["contracts"]; summary = mp["summary"]
            if sent_df.empty: sent_df = synthetic_sentiment(universe_all)
            if fund_df.empty: fund_df = synthetic_fundamentals(universe_all)
            if ins_df.empty: ins_df = synthetic_insider(universe_all)
            if news_df.empty: news_df = synthetic_news(universe_all)
            if earn_df.empty: earn_df = synthetic_earnings(universe_all)
            if value_df.empty: value_df = synthetic_value(universe_all)
            if futures_df.empty: futures_df = synthetic_futures()

    # v20.3: optional FinBERT pass on news headlines (auto-detects GPU, falls
    # back to CPU torch, no-ops if torch/transformers not installed).
    finbert_df = pd.DataFrame()
    if not getattr(args, "skip_finbert", False) and not news_df.empty:
        try:
            from engines import finbert as _finbert
            finbert_df = _finbert.run(news_df)
        except Exception as e:
            log.debug("finbert engine skipped: %s", e)

    log.info("rows: contracts=%d sent=%d fund=%d ins=%d news=%d earn=%d value=%d futures=%d congress=%d social=%d analyst=%d finbert=%d",
             len(contracts), len(sent_df), len(fund_df), len(ins_df),
             len(news_df), len(earn_df), len(value_df), len(futures_df),
             len(congress_df), len(social_df), len(analyst_df), len(finbert_df))

    # v20.1 — Thin-chains warning: yfinance got rate-limited if very few
    # contracts emerged. Tell the user so they don't think v20 broke options.
    if len(contracts) > 0 and len(contracts) < 50:
        log.warning("THIN CHAIN COUNT: only %d option contracts cleared filters. "
                    "Yfinance is likely rate-limited from your IP. Try: "
                    "(1) wait 15 min and re-run, (2) use a residential VPN, "
                    "(3) reduce universe with --universe AAPL MSFT NVDA, or "
                    "(4) run with --loop 60 (longer gap = less rate-limit pressure).",
                    len(contracts))

    # Auto-retrain BEFORE fusion so this run uses fresh predictor coefs.
    # Forward-test data + cached IC fold into the predictor here.
    try:
        from backtest.forward import run_forward_test
        ft_pre = run_forward_test()
        forward_signals_pre = ft_pre["signals"] if not ft_pre["signals"].empty else None
    except Exception:
        forward_signals_pre = None
    try:
        ic_df_pre = bt_predictor.load_cached_ic()
        if ic_df_pre is not None or forward_signals_pre is not None:
            coef_payload = bt_predictor.fit_return_predictor(forward_signals_pre, ic_df_pre)
            new_w = bt_predictor.update_runtime_weights(forward_signals_pre, ic_df_pre)
            if new_w:
                log.info("auto-retrain (pre-fusion): top weight = %s",
                         max(new_w, key=new_w.get))
            top_coef = max(coef_payload["coefs"].items(), key=lambda kv: abs(kv[1]))
            log.info("predictor: source=%s top=%s=%+.4f",
                     coef_payload["meta"].get("source"), top_coef[0], top_coef[1])
    except Exception as e:
        log.debug("pre-fusion retrain skipped: %s", e)

    # v20.3: refit per-model ensemble weights from realized mid moves
    try:
        from backtest import model_accuracy as bt_model_acc
        new_weights = bt_model_acc.refit_weights()
        if new_weights:
            log.info("model-ensemble auto-refit: regimes updated = %s",
                     list(new_weights.keys()))
    except Exception as e:
        log.debug("model-accuracy refit skipped: %s", e)

    # v17: derive UOA from already-fetched chains (free, no extra network)
    try:
        from engines import uoa as _uoa_mod
        uoa_df = _uoa_mod.derive_from_contracts(contracts)
    except Exception as e:
        log.debug("uoa derivation skipped: %s", e)
        uoa_df = pd.DataFrame()
    # v19: derive Put/Call + IV-surface anomalies from chains (also free)
    try:
        from engines import put_call as _pc_mod
        put_call_df = _pc_mod.derive_from_contracts(contracts)
    except Exception as e:
        log.debug("put/call derivation skipped: %s", e)
        put_call_df = pd.DataFrame()
    try:
        from engines import iv_surface as _ivs_mod
        iv_surface_df = _ivs_mod.derive_from_contracts(contracts)
    except Exception as e:
        log.debug("iv_surface derivation skipped: %s", e)
        iv_surface_df = pd.DataFrame()
    # Pull v17+ new factors from concurrent results (if we ran live, demo skips)
    try:
        sector_rs_df = _to_df(results.get("sector_rs"))
        dark_pool_df = _to_df(results.get("dark_pool"))
        fda_df = _to_df(results.get("fda"))
        sector_flow_df = _to_df(results.get("sector_flow"))
        technicals_df = _to_df(results.get("technicals"))
        short_int_df = _to_df(results.get("short_int"))
    except NameError:
        sector_rs_df = pd.DataFrame()
        dark_pool_df = pd.DataFrame()
        fda_df = pd.DataFrame()
        sector_flow_df = pd.DataFrame()
        technicals_df = pd.DataFrame()
        short_int_df = pd.DataFrame()

    # v20 — pull NEW factor results
    try:
        cot_df = _to_df(results.get("cot"))
        thirteen_f_df = _to_df(results.get("thirteen_f"))
        vix_term_df = _to_df(results.get("vix_term"))
        eia_df = _to_df(results.get("eia"))
        wasde_df = _to_df(results.get("wasde"))
        buybacks_df = _to_df(results.get("buybacks"))
        gtrends_df = _to_df(results.get("gtrends"))
        form_144_df = _to_df(results.get("form_144"))
        whisper_df = _to_df(results.get("whisper"))
        hyperliquid_df = _to_df(results.get("hyperliquid"))
        twitter_df = _to_df(results.get("twitter"))
        r_options_df = _to_df(results.get("r_options"))
        yield_curve_df = _to_df(results.get("yield_curve"))
        credit_spread_df = _to_df(results.get("credit_spread"))
        engine_timings = results.get("_timings", {}) if isinstance(results, dict) else {}
    except NameError:
        cot_df = thirteen_f_df = vix_term_df = eia_df = wasde_df = pd.DataFrame()
        buybacks_df = gtrends_df = form_144_df = whisper_df = pd.DataFrame()
        hyperliquid_df = twitter_df = r_options_df = pd.DataFrame()
        yield_curve_df = credit_spread_df = pd.DataFrame()
        engine_timings = {}

    # v20 Tier C — derive cluster_buys from insider engine output (post-process)
    try:
        from engines import cluster_buys as _cb_mod
        cluster_buys_df = _cb_mod.derive_from_insider(ins_df)
    except Exception as e:
        log.debug("cluster_buys derivation skipped: %s", e)
        cluster_buys_df = pd.DataFrame()

    # v20.1 — Empty-engine diagnostic: list which v20 engines returned 0 rows
    # with the most likely reason.
    EMPTY_REASONS = {
        "cot":          "v20.2: CFTC Socrata + legacy TXT both unreachable (network?)",
        "thirteen_f":   "no smart-money deltas in universe overlap (or SEC fetch failed)",
        "vix_term":     "VIX futures unavailable",
        "eia":          "v20.2: EIA v2 API + ir.eia.gov HTML fallback both empty (residential IP block?)",
        "wasde":        "no current WASDE catalyst within window (normal — only 1 release/month)",
        "buybacks":     "no 8-K repurchase mentions in universe overlap",
        "gtrends":      "v20.2: pytrends + Wikipedia pageviews fallback both returned no momentum",
        "form_144":     "no Form 144 filings overlap with universe (30d window)",
        "whisper":      "v20.2: Finnhub + yfinance targetMeanPrice both empty, or no tickers in -2d..+14d earnings window",
        "hyperliquid":  "Hyperliquid API unreachable",
        "twitter":      "Apewisdom + Nitter mirrors both down",
        "r_options":    "no r/options sticky mentions in universe",
        "yield_curve":  "FRED_API_KEY missing or curve series unavailable",
        "credit_spread":"FRED_API_KEY missing or HY/IG series unavailable",
        "cluster_buys": "no insider triple-buys in last 90 days",
    }
    v20_df_map = {
        "cot": cot_df, "thirteen_f": thirteen_f_df, "vix_term": vix_term_df,
        "eia": eia_df, "wasde": wasde_df, "buybacks": buybacks_df,
        "gtrends": gtrends_df, "form_144": form_144_df, "whisper": whisper_df,
        "hyperliquid": hyperliquid_df, "twitter": twitter_df,
        "r_options": r_options_df, "yield_curve": yield_curve_df,
        "credit_spread": credit_spread_df, "cluster_buys": cluster_buys_df,
    }
    empty_engines = []
    for name, df in v20_df_map.items():
        if df is None or (hasattr(df, "empty") and df.empty):
            empty_engines.append({"name": name, "reason": EMPTY_REASONS.get(name, "")})
    if empty_engines:
        log.info("v20 empty engines (%d): %s",
                 len(empty_engines),
                 ", ".join(e["name"] for e in empty_engines))

    log.info("== Fusion ==")
    ranked_opts = fusion_rank.fuse_options(contracts, summary, sent_df, fund_df, ins_df,
                                           macro_state, news=news_df, earnings=earn_df,
                                           value=value_df, congress=congress_df,
                                           social=social_df, analyst=analyst_df,
                                           uoa=uoa_df, sector_rs=sector_rs_df,
                                           dark_pool=dark_pool_df, fda=fda_df,
                                           sector_flow=sector_flow_df,
                                           technicals=technicals_df,
                                           short_int=short_int_df,
                                           put_call=put_call_df,
                                           iv_surface=iv_surface_df,
                                           # v20 new factors
                                           cot=cot_df, thirteen_f=thirteen_f_df,
                                           vix_term=vix_term_df, eia=eia_df,
                                           wasde=wasde_df, buybacks=buybacks_df,
                                           gtrends=gtrends_df, form_144=form_144_df,
                                           whisper=whisper_df, hyperliquid=hyperliquid_df,
                                           twitter=twitter_df, r_options=r_options_df,
                                           yield_curve=yield_curve_df,
                                           credit_spread=credit_spread_df,
                                           cluster_buys=cluster_buys_df)

    # Apply return predictions to ranked options
    coefs = bt_predictor.load_predictor_coefs()
    has_predictor = any(abs(v) > 1e-6 for v in coefs.values())
    if has_predictor:
        ranked_opts = bt_predictor.add_predictions_to_options(ranked_opts, coefs)

    # v20 — Drawdown circuit breaker
    drawdown_mult = 1.0
    breaker_state = None
    if DRAWDOWN_BREAKER_ENABLED:
        try:
            from backtest.drawdown_breaker import compute_breaker_state
            breaker_state = compute_breaker_state(window_days=14)
            drawdown_mult = breaker_state["multiplier"]
            if drawdown_mult != 1.0:
                log.info("Drawdown breaker: %s (mult=%.2f)",
                         breaker_state["verdict"], drawdown_mult)
        except Exception as e:
            log.debug("drawdown breaker skipped: %s", e)

    # Add EV + Kelly sizing across the full ranked option universe before
    # selecting the top board, so final ideas reflect both the research score
    # and whether the sizing stack can actually support a position.
    ranked_opts = bt_sizing.add_sizing_to_options(ranked_opts, bankroll=args.bankroll,
                                                   aggressive=args.aggressive,
                                                   drawdown_mult=drawdown_mult)
    ranked_opts = bt_sizing.sort_for_trade_selection(ranked_opts, asset="option")
    option_candidates = ranked_opts
    if "trade_status" in ranked_opts.columns:
        option_candidates = ranked_opts[ranked_opts["trade_status"] != "Skip"]
    top_opts = fusion_rank.top_options(option_candidates, max_calls=args.max_calls,
                                       max_puts=args.max_puts)

    # v20 — Aggregate portfolio Greeks
    portfolio_greeks = {}
    hedge_sugg = None
    try:
        from backtest.portfolio_greeks import aggregate_portfolio_greeks, hedge_suggestion
        portfolio_greeks = aggregate_portfolio_greeks(top_opts)
        hedge_sugg = hedge_suggestion(portfolio_greeks, threshold=HEDGE_DELTA_THRESHOLD)
        if hedge_sugg:
            log.info("Hedge suggestion: %s", hedge_sugg["suggestion"])
    except Exception as e:
        log.debug("greeks aggregation skipped: %s", e)
    calls = top_opts[top_opts["side"] == "call"].reset_index(drop=True) if not top_opts.empty else pd.DataFrame()
    puts = top_opts[top_opts["side"] == "put"].reset_index(drop=True) if not top_opts.empty else pd.DataFrame()

    excluded = set(top_opts["ticker"].tolist()) if not top_opts.empty else set()
    ranked_shares = fusion_rank.fuse_shares(universe_shares, sent_df, fund_df, ins_df, macro_state,
                                             excluded_tickers=excluded,
                                             news=news_df, earnings=earn_df, value=value_df,
                                             congress=congress_df)
    if has_predictor:
        ranked_shares = bt_predictor.add_predictions_to_shares(ranked_shares, coefs)
    ranked_shares = bt_sizing.add_sizing_to_shares(ranked_shares, bankroll=args.bankroll,
                                                    aggressive=args.aggressive,
                                                    drawdown_mult=drawdown_mult)
    ranked_shares = bt_sizing.sort_for_trade_selection(ranked_shares, asset="shares")
    share_candidates = ranked_shares
    if "trade_status" in ranked_shares.columns:
        share_candidates = ranked_shares[ranked_shares["trade_status"] != "Skip"]
    top_sh = share_candidates.head(args.max_shares).reset_index(drop=True)

    # Top value plays — independent ranking on the value engine's score
    top_value = pd.DataFrame()
    if not value_df.empty and "value_score" in value_df.columns:
        # Merge in fund + insider + news for richer cards
        v = value_df.copy()
        if not fund_df.empty:
            v = v.merge(fund_df[["ticker", "classification", "earnings_date", "market_cap"]],
                        on="ticker", how="left", suffixes=("", "_f"))
        if not news_df.empty:
            v = v.merge(news_df[["ticker", "n_24h", "top_headline", "news_delta"]],
                        on="ticker", how="left")
        if not ins_df.empty:
            v = v.merge(ins_df[["ticker", "insider_score", "n_buys", "n_sells"]],
                        on="ticker", how="left")
        v = v[v["value_score"] > 0.3].sort_values("value_score", ascending=False)
        top_value = v.head(args.max_value).reset_index(drop=True)

    # Top futures plays
    top_fut = pd.DataFrame()
    if not futures_df.empty and "futures_score" in futures_df.columns:
        # Bullish + bearish split
        bullish = futures_df[futures_df["futures_score"] > 0.3].sort_values("futures_score", ascending=False)
        bearish = futures_df[futures_df["futures_score"] < -0.3].sort_values("futures_score")
        top_fut = pd.concat([bullish.head(args.max_futures // 2),
                             bearish.head(args.max_futures // 2)], ignore_index=True)

    log.info("top calls: %d  top puts: %d  top shares: %d  top value: %d  top futures: %d",
             len(calls), len(puts), len(top_sh), len(top_value), len(top_fut))

    out_dir = Path(args.out_dir); out_dir.mkdir(exist_ok=True)
    asof_tag = run_asof.strftime("%Y%m%d_%H%M%S")

    def _save(df, name):
        if df is not None and not df.empty:
            df.to_parquet(out_dir / f"{name}_{asof_tag}.parquet", index=False)

    _save(ranked_opts, "ranked_options")
    _save(top_opts, "top_options")
    _save(top_sh, "top_shares")
    _save(top_value, "top_value")
    _save(top_fut, "top_futures")
    _save(contracts, "contracts")
    _save(sent_df, "sentiment")
    _save(fund_df, "fundamentals")
    _save(ins_df, "insider")
    _save(news_df, "news")
    _save(earn_df, "earnings")
    _save(value_df, "value")
    _save(futures_df, "futures")
    _save(congress_df, "congress")
    _save(social_df, "social")
    _save(analyst_df, "analyst")
    with open(out_dir / f"macro_{asof_tag}.json", "w") as f:
        json.dump(macro_state, f, indent=2, default=str)

    try:
        if not top_opts.empty:
            backtest_track.log_signals(top_opts, run_asof)
            # Also log shares + futures so forward test covers ALL asset types
            backtest_track.log_signals_shares(top_sh, run_asof)
            backtest_track.log_signals_futures(top_fut, run_asof)
    except Exception as e:
        log.warning("signal log failed: %s", e)

    # v20.7 — position-level P&L tracking (distinguishes still-open from closed).
    # Adds new top-of-board signals as positions and marks open ones to market.
    try:
        from backtest import positions as _pos
        if not top_opts.empty:
            _pos.add_new_signals(top_opts, run_asof)
        _pos.mark_to_market(run_asof)
    except Exception as e:
        log.debug("position tracking skipped: %s", e)

    tv_text = fusion_rank.to_tv_watchlist(calls, puts, top_sh)
    tv_path = out_dir / f"tradingview_watchlist_{asof_tag}.txt"
    tv_path.write_text(tv_text)

    # Forward-test summary for the dashboard panel
    forward_summary = None
    calibration_summary = None
    if forward_signals_pre is not None:
        try:
            from backtest.forward import run_forward_test
            ft = run_forward_test()
            if not ft["signals"].empty:
                forward_summary = ft
                log.info("forward test: %d signals, %.1f%% win rate, %.2f%% avg P&L",
                         ft["overall"]["n_signals"],
                         ft["overall"]["win_rate"]*100,
                         ft["overall"]["avg_pnl_pct"]*100)
                # v19: also compute calibration (predicted vs realized accuracy)
                try:
                    from backtest.calibration import diagnostic_summary
                    calibration_summary = diagnostic_summary(ft["signals"])
                    overall_cal = calibration_summary.get("overall", {}).get("overall", {})
                    if overall_cal.get("rank_correlation") is not None:
                        log.info("calibration: rank_corr=%.2f bias=%+0.3f verdict=%s",
                                 overall_cal["rank_correlation"],
                                 overall_cal.get("avg_bias", 0),
                                 overall_cal.get("verdict", "")[:60])
                except Exception as e:
                    log.debug("calibration skipped: %s", e)
        except Exception as e:
            log.debug("forward test skipped: %s", e)

    # Refresh IC from a fresh backtest if possible (best-effort)
    try:
        from backtest.historical import run_historical_backtest
        factor_dfs = {
            "value_score": value_df, "fund_score": fund_df,
            "sentiment_delta": sent_df, "insider_score": ins_df,
        }
        bt = run_historical_backtest(universe_all, factor_dfs)
        if bt is not None and not bt.get("ic", pd.DataFrame()).empty:
            bt_predictor.cache_ic(bt["ic"])
            log.info("backtest IC refreshed and cached")
    except Exception as e:
        log.debug("post-run IC refresh skipped: %s", e)

    log.info("== Dashboard ==")
    elapsed = (datetime.now() - t_start).total_seconds()
    html_path = dash_build.render(
        calls=calls, puts=puts, shares=top_sh,
        value_plays=top_value, futures_plays=top_fut,
        ranked_options=ranked_opts, ranked_shares=ranked_shares,
        macro=macro_state, asof=run_asof, demo=use_demo,
        news=news_df, earnings=earn_df, insider=ins_df,
        congress=congress_df, sentiment=sent_df, social=social_df,
        analyst=analyst_df,
        trending=trending, trending_meta=trending_meta,
        elapsed=elapsed, universe_size=len(universe_all),
        forward_summary=forward_summary,
        calibration_summary=calibration_summary,
        bankroll=args.bankroll,
        aggressive=args.aggressive,
        # v20 new payloads (dash render must tolerate these as kwargs)
        portfolio_greeks=portfolio_greeks,
        hedge_suggestion=hedge_sugg,
        breaker_state=breaker_state,
        engine_timings=engine_timings if 'engine_timings' in dir() else {},
        v20_factors=v20_df_map,
        empty_engines=empty_engines,
    )
    log.info("dashboard: %s", html_path)
    log.info("tradingview watchlist: %s", tv_path)
    log.info("total elapsed: %.1f sec", elapsed)

    # Auto-open in default browser unless --no-open. In loop mode, only the
    # first iteration opens the browser (env var marks subsequent iterations).
    if not args.no_open and not os.environ.get("OPTEDGE_NO_OPEN"):
        try:
            import webbrowser
            file_url = "file://" + str(html_path.resolve())
            webbrowser.open(file_url)
            log.info("opened dashboard in browser")
            os.environ["OPTEDGE_NO_OPEN"] = "1"   # don't re-open in loop iterations
        except Exception as e:
            log.debug("browser open skipped: %s", e)

    print(f"\n┌──────────────────────────────────────────┐")
    print(f"│  Optedge run complete in {elapsed:5.1f}s")
    print(f"│  {len(calls)} calls / {len(puts)} puts / {len(top_sh)} shares / {len(top_value)} value / {len(top_fut)} futures")
    if trending:
        print(f"│  WSB trending added: {len(trending)}")
    if use_demo:
        print(f"│  ⚠  DEMO MODE (synthetic data)")
    print(f"└──────────────────────────────────────────┘")
    print("\n=== TOP LONG CALLS ===")
    for _, r in calls.head(10).iterrows():
        print(f"  {r['ticker']:<6} {r['contract']:<28} conf {int(r['confidence'])}")
    print("\n=== TOP LONG PUTS ===")
    for _, r in puts.head(10).iterrows():
        print(f"  {r['ticker']:<6} {r['contract']:<28} conf {int(r['confidence'])}")
    print("\n=== TOP SHARE BUYS ===")
    for _, r in top_sh.head(10).iterrows():
        print(f"  {r['ticker']:<6} score {r['share_score']:+.2f}  conf {int(r['confidence'])}")
    print("\n=== TOP VALUE PLAYS (cheap & quality) ===")
    for _, r in top_value.head(10).iterrows():
        bucket = r.get("value_bucket") or "—"
        print(f"  {r['ticker']:<6} score {r['value_score']:+.2f}  P/E {r.get('pe', '—'):>5}  bucket {bucket}")
    print("\n=== TOP FUTURES PLAYS ===")
    for _, r in top_fut.head(10).iterrows():
        side = "LONG" if r["futures_score"] > 0 else "SHORT"
        print(f"  {r['symbol']:<8} {r['name']:<22} {side}  score {r['futures_score']:+.2f}  ret20d {(r.get('ret_20d') or 0)*100:+.1f}%")
    print(f"\n→ Dashboard: file://{html_path}")
    print(f"→ TradingView watchlist: {tv_path}")
    return 0


def main_loop():
    """Continuous loop wrapper. Runs main() repeatedly, sleeping between.

    Memory hygiene: explicitly invokes Python's garbage collector after each
    iteration so DataFrames from previous runs don't accumulate.
    """
    import time
    import gc
    import argparse as _ap
    pre = _ap.ArgumentParser(add_help=False)
    pre.add_argument("--loop", type=int, default=None)
    known, _ = pre.parse_known_args()
    if known.loop is None:
        return main()
    interval_sec = max(60, known.loop * 60)
    iteration = 0
    print(f"\n=== LOOP MODE — running every {known.loop} min, Ctrl+C to stop ===\n", flush=True)
    # v20.4: import health harness for whole-day reliability tracking
    try:
        from telemetry import health as _health
        _have_health = True
    except Exception:
        _have_health = False

    while True:
        iteration += 1
        iter_t0 = time.time()
        if _have_health:
            try:
                _health.assert_can_continue()
            except Exception:
                pass
        print(f"\n┌─ Iteration {iteration} @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ─" + "─" * 30, flush=True)
        iter_error: str = ""
        rc = 0
        try:
            rc = main()
            if rc != 0:
                print(f"  iteration returned non-zero ({rc}), continuing")
        except KeyboardInterrupt:
            print("\n=== STOPPING (Ctrl+C) ===")
            return 130
        except Exception as e:
            iter_error = f"{type(e).__name__}: {e}"
            print(f"\n  ✗ iteration {iteration} crashed: {iter_error}")
            traceback.print_exc()
            print("  continuing in next interval…")
        # v20.4: record per-iter health row before we sleep
        if _have_health:
            try:
                _health.record({
                    "iteration": iteration,
                    "iter_seconds": time.time() - iter_t0,
                    "rc": rc,
                    "error": iter_error,
                })
            except Exception:
                pass
        # Memory hygiene
        gc.collect()
        next_run = datetime.now() + pd.Timedelta(seconds=interval_sec)
        print(f"\n└─ next run @ {next_run.strftime('%H:%M:%S')} (sleeping {known.loop} min)\n", flush=True)
        try:
            time.sleep(interval_sec)
        except KeyboardInterrupt:
            print("\n=== STOPPING (Ctrl+C during sleep) ===")
            return 130


if __name__ == "__main__":
    try:
        # Detect --loop and route accordingly
        if any(arg == "--loop" or arg.startswith("--loop=") for arg in sys.argv):
            sys.exit(main_loop())
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
    except Exception as e:
        print(f"\n\nFATAL: {type(e).__name__}: {e}\n", flush=True)
        traceback.print_exc()
        sys.exit(1)
