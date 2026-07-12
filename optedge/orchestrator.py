# Purpose: Coordinate engines ranking risk logging and reports.
"""Optedge — orchestrator. Long-only options + small-cap shares + futures + value plays.

Engines run in parallel. WSB trending tickers added at runtime. Each engine
internally parallelizes per-ticker work.

First run? Run `python setup_check.py` first to verify data sources.
"""
from __future__ import annotations
import argparse
import importlib
import json
import logging
import os
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from config import (UNIVERSE, UNIVERSE_OPTIONS, UNIVERSE_SHARES, TOP_N_CALLS, TOP_N_PUTS, TOP_N_SHARES,
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
    from optedge.strategy_profile import is_known_index_option_symbol
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

except Exception as e:
    print(f"\nIMPORT ERROR - Optedge can't start.\n{e}\n", flush=True)
    traceback.print_exc()
    sys.exit(2)


_runtime_weights_applied = False


def _apply_runtime_weight_override() -> None:
    """Apply a trusted runtime model once, when a scan actually starts."""
    global _runtime_weights_applied
    if _runtime_weights_applied:
        return
    _runtime_weights_applied = True

    # Runtime overrides are optional and must prove freshness, coverage, and
    # independent walk-forward evidence before they can affect ranking.
    runtime_status = bt_predictor.runtime_weight_status()
    runtime_weights = runtime_status.get("weights") if runtime_status.get("usable") else None
    if runtime_weights:
        _config.SIGNAL_WEIGHTS = runtime_weights
        # Also patch the imported module so fusion sees the override.
        import fusion.rank as rank_module
        rank_module.SIGNAL_WEIGHTS = runtime_weights
        top_factor = max(runtime_weights, key=runtime_weights.get)
        print(
            "  [auto-retrain] using runtime SIGNAL_WEIGHTS: top factor "
            f"= {top_factor} ({runtime_weights[top_factor]:.2f})",
            flush=True,
        )
    elif runtime_status.get("exists"):
        reasons = list(runtime_status.get("reasons") or [])
        detail = "; ".join(reasons[:3])
        if len(reasons) > 3:
            detail += f"; plus {len(reasons) - 3} more guard(s)"
        print(
            f"  [auto-retrain] runtime weights ignored: {detail}; using configured priors",
            flush=True,
        )


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


def _load_optional_engine(module_name: str):
    """Load one optional engine, logging only genuine dependency absence.

    Unexpected import-time failures such as RuntimeError or SyntaxError are not
    swallowed; they surface immediately instead of silently removing a signal.
    """
    try:
        return importlib.import_module(f"engines.{module_name}")
    except ImportError as exc:
        logging.getLogger("optedge").warning(
            "[skip] optional engine %s is unavailable: %s", module_name, exc
        )
        return None


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
                           universe_heavy=None, skip_v20=None,
                           fast_insider: bool = False):
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
        tasks["insider"] = lambda: insider.run(universe_heavy, fast_mode=fast_insider)
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
    # Optional engines are registered from one auditable table. Missing
    # dependencies are logged; unexpected import failures remain fatal.
    optional_specs = [
        (True, "sector_rs", "sector_rs", universe_heavy),
        (True, "dark_pool", "dark_pool", universe_heavy),
        (True, "fda", "fda_calendar", universe_heavy),
        (True, "technicals", "technicals", universe_heavy),
        (True, "short_int", "short_interest", universe_heavy),
        (_v20_on("cot"), "cot", "cot", universe_all),
        (_v20_on("thirteen_f"), "thirteen_f", "thirteen_f", universe_heavy),
        (_v20_on("vix_term"), "vix_term", "vix_term", universe_all),
        (_v20_on("eia"), "eia", "eia", universe_all),
        (_v20_on("wasde"), "wasde", "wasde", universe_all),
        (_v20_on("buybacks"), "buybacks", "buybacks", universe_heavy),
        (_v20_on("gtrends"), "gtrends", "google_trends", universe_heavy),
        (_v20_on("form_144"), "form_144", "form_144", universe_heavy),
        (_v20_on("whisper"), "whisper", "whisper", universe_heavy),
        (_v20_on("hyperliquid"), "hyperliquid", "hyperliquid", universe_all),
        (_v20_on("twitter"), "twitter", "nitter", universe_heavy),
        (_v20_on("r_options"), "r_options", "r_options", universe_heavy),
        (_v20_on("yield_curve"), "yield_curve", "yield_curve_pca", universe_all),
        (_v20_on("credit_spread"), "credit_spread", "credit_spread", universe_all),
        (_v20_on("sec_ftd"), "sec_ftd", "sec_ftd", universe_heavy),
    ]
    for enabled, task_name, module_name, target_universe in optional_specs:
        if not enabled:
            continue
        module = _load_optional_engine(module_name)
        if module is not None:
            tasks[task_name] = lambda module=module, target=target_universe: module.run(target)

    flow_module = _load_optional_engine("sector_etf_flow")
    if flow_module is not None:
        def _sector_flow_task(module=flow_module):
            sector_flow = module.run()
            return module.per_ticker_score(universe_all, sector_flow)

        tasks["sector_flow"] = _sector_flow_task

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
                results[name] = fut.result()
                t = timings.get(name, {})
                elapsed = t.get("elapsed", 0)
                log.info(
                    "[ok] %s engine completed (%.1fs, %d rows)",
                    name,
                    elapsed,
                    t.get("rows", 0),
                )
                if elapsed > sla:
                    log.warning(
                        "[slow] %s engine exceeded its %.1fs SLA threshold (%.1fs)",
                        name,
                        sla,
                        elapsed,
                    )
            except Exception as e:
                log.error("[x] %s engine failed: %s", name, str(e)[:200])
                results[name] = None

    results["_timings"] = timings
    return results


def main():
    ap = argparse.ArgumentParser(description="Optedge — long-only options/shares/futures/value ranker")
    ap.add_argument(
        "--cockpit",
        action="store_true",
        help="Open the local risk-first Trade Desk (handled by the Optedge CLI router)",
    )
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
    ap.add_argument("--skip-sec-ftd", action="store_true",
                    help="Skip SEC fails-to-deliver context engine (free/no-key)")
    ap.add_argument("--skip-finbert", action="store_true",
                    help="Skip FinBERT sentiment engine (v20.3, GPU-aware, optional)")
    ap.add_argument("--fast-insider", action="store_true",
                    help="Insider engine: count-only mode (skip XML parsing, ~5x faster)")
    ap.add_argument("--turbo", action="store_true",
                    help="Performance preset: RAM cache + batched GPU FinBERT + fast insider parsing")
    ap.add_argument("--demo", action="store_true",
                    help="Force synthetic data (sandbox / first-look mode)")
    ap.add_argument("--no-auto-detect", action="store_true")
    ap.add_argument("--sequential", action="store_true",
                    help="Run engines sequentially (debug mode)")
    ap.add_argument("--forward", action="store_true",
                    help="Forward test only — replay logged signals with current prices")
    ap.add_argument("--backtest", action="store_true",
                    help="Historical backtest — IC analysis on 7/30/60/90d forward returns")
    ap.add_argument("--validation-report", action="store_true",
                    help="Build the formal validation report from local logs/positions")
    ap.add_argument("--validation-all-time", action="store_true",
                    help="Validation report: include stale historical data instead of current model era only")
    ap.add_argument("--heston-stability", action="store_true",
                    help="Run a Heston numerical stability report without enabling Heston")
    ap.add_argument("--lookup", metavar="SYMBOL",
                    help="Look up one ticker/symbol in latest local Optedge outputs without rerunning engines")
    ap.add_argument("--bankroll", type=float, default=10000,
                    help="Account size used for Kelly position sizing (default $10K)")
    ap.add_argument("--aggressive", action="store_true",
                    help="½ Kelly + 10%% per option / 15%% per share caps (vs default ¼ Kelly + 5/8%%)")
    ap.add_argument("--loop", type=int, metavar="MINUTES",
                    help="Run continuously, sleeping N minutes between iterations")
    ap.add_argument("--no-open", action="store_true",
                    help="Don't auto-open the dashboard in browser when done")
    ap.add_argument("--robinhood-agentic-queue", action="store_true",
                    help="Write an options-only Robinhood research/paper shortlist after each scan")
    ap.add_argument("--robinhood-budget", type=float, default=None,
                    help="Budget for Robinhood Agentic queue caps (defaults to --bankroll)")
    ap.add_argument("--robinhood-max-candidates", type=int, default=5,
                    help="Max option candidates to keep in the manual-review shortlist")
    ap.add_argument("--robinhood-max-orders", type=int, default=2,
                    help="Compatibility name for the manual-review comparison cap; authorizes zero submissions")
    ap.add_argument("--robinhood-min-dte", type=int, default=90,
                    help="Minimum DTE for Robinhood agentic option candidates (default 90d+ swing)")
    ap.add_argument("--robinhood-min-confidence", type=float, default=55.0,
                    help="Minimum confidence for Robinhood agentic option candidates")
    ap.add_argument("--robinhood-max-premium-per-order", type=float, default=None,
                    help="Max debit per option candidate in the Robinhood manual-review shortlist")
    ap.add_argument("--robinhood-refresh-chain", action="store_true",
                    help="Refresh the free/provider option-chain shortlist before building the Robinhood queue")
    ap.add_argument("--robinhood-chain-preset", default="auto",
                    choices=["auto", "swing", "leaps", "liquid", "custom"],
                    help="Chain refresh preset for Robinhood queue (auto uses DTE to choose swing/leaps)")
    ap.add_argument("--robinhood-chain-symbols-limit", type=int, default=6,
                    help="Max symbols to scan during Robinhood chain refresh")
    ap.add_argument("--robinhood-chain-contracts-per-symbol", type=int, default=4,
                    help="Max contracts per symbol to keep during Robinhood chain refresh")
    ap.add_argument("--quiet", action="store_true",
                    help="Reduce log verbosity (only WARNING and above)")
    ap.add_argument("--minimal", action="store_true",
                    help="Lightweight preset: skip slow engines (wsb, news, congress, social, analyst). ~30s runs.")
    ap.add_argument("--out-dir", default=str(ROOT / "data"))
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    print("Optedge starting...", flush=True)
    _apply_runtime_weight_override()

    # --quiet overrides log_level
    setup_logging("WARNING" if args.quiet else args.log_level)
    log = logging.getLogger("optedge")
    t_start = datetime.now()
    run_asof = datetime.now(timezone.utc)

    # --minimal preset: stack of skip flags for fast iteration
    if args.turbo:
        args.fast_insider = True
        os.environ.setdefault("OPTEDGE_RAM_CACHE", "1")
        os.environ.setdefault("OPTEDGE_FINBERT_BATCH_SIZE", "96")
        try:
            data_provider.configure_ram_cache(enabled=True)
        except Exception:
            pass

    if args.minimal:
        args.skip_wsb = True
        args.skip_news = True
        args.skip_congress = True
        args.skip_social = True
        args.skip_analyst = True
        args.fast_insider = True

    # Standalone modes — validation / forward / backtest only
    if args.lookup:
        from scripts.lookup_symbol import lookup_symbol, save_lookup
        log.info("== LOCAL LOOKUP: %s ==", args.lookup.upper())
        report = lookup_symbol(args.lookup, Path(args.out_dir))
        paths = save_lookup(report, Path(args.out_dir))
        print(f"\nLookup report: {paths['html']}")
        print(f"Lookup JSON: {paths['json']}")
        print(f"Hits: {report['total_hits']}")
        if report["total_hits"] == 0:
            print(f"Tip: run a focused scan with: python run.py --universe {args.lookup.upper()} --no-open")
        return 0

    if args.validation_report:
        try:
            from backtest.fixed_horizon import run_fixed_horizon_test

            run_fixed_horizon_test()
        except Exception as e:
            log.warning("fixed-horizon refresh skipped: %s", e)
        from reports.validation_report import (
            EQUITY_PNG, FACTOR_IC_JSON, POSITION_AGING_JSON, REPORT_HTML,
            SUMMARY_JSON, write_report,
        )
        log.info("== VALIDATION REPORT ==")
        summary = write_report(scope="all_time" if args.validation_all_time else "current_model")
        print(f"\nValidation report: {REPORT_HTML}")
        print(f"Validation summary: {SUMMARY_JSON}")
        print(f"Equity curve: {EQUITY_PNG}")
        print(f"Factor IC summary: {FACTOR_IC_JSON}")
        print(f"Position aging summary: {POSITION_AGING_JSON}")
        if summary.get("warnings"):
            print("\nWarnings:")
            for warning in summary["warnings"]:
                print(f"  - {warning}")
        return 0

    if args.heston_stability:
        from reports.heston_stability import OUT_JSON, write_report
        log.info("== HESTON STABILITY ==")
        report = write_report()
        print(f"\nHeston stability: {OUT_JSON}")
        print(f"Contracts checked: {report.get('contracts_checked', 0)}")
        print(f"Stable enough to enable: {report.get('ok')}")
        if report.get("reason"):
            print(f"Reason: {report['reason']}")
        return 0

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
        print("\n=== FORWARD TEST RESULTS ===")
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
        fixed = result.get("fixed_horizon", {}) or {}
        headline = fixed.get("headline_shadow", {}) or {}
        if fixed:
            print(f"\n  Fixed {fixed.get('headline_horizon_sessions', 10)}-session current-method shadow sample:")
            print(f"    Outcomes:          {int(headline.get('n') or 0)}")
            print(f"    Entry days:        {int(headline.get('unique_entry_days') or 0)}")
            if headline.get("win_rate") is not None:
                print(f"    Win rate:          {float(headline['win_rate'])*100:.1f}%")
                print(f"    Avg after costs:   {float(headline['avg_return'])*100:+.2f}%")
            executed = int((fixed.get("headline") or {}).get("n") or 0)
            print(f"    Executed outcomes: {executed}")
            option_data = fixed.get("option_market_data", {}) or {}
            observed = int(option_data.get("broker_observed_outcomes") or 0)
            modeled = int(option_data.get("modeled_proxy_outcomes") or 0)
            print("    Shadow rows passed strategy rules before guardrails. Shares/futures use observed closes.")
            print(f"    Option evidence:   {observed} broker-observed / {modeled} modeled fallback outcome(s)")
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
            ins_df = insider.run(UNIVERSE, fast_mode=args.fast_insider)
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
        log.info("performance: fast insider mode enabled")
    if args.turbo:
        try:
            log.info("performance: turbo mode enabled; cache=%s finbert_batch=%s",
                     data_provider.cache_stats(),
                     os.environ.get("OPTEDGE_FINBERT_BATCH_SIZE", "auto"))
        except Exception:
            log.info("performance: turbo mode enabled")

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
                ins_df = insider.run(universe_all, fast_mode=args.fast_insider)
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
                "sec_ftd": args.skip_sec_ftd,
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
                fast_insider=args.fast_insider,
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
            ins_df = pd.DataFrame() if skip_insider else insider.run(
                universe_all, fast_mode=args.fast_insider
            )
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

    # Auto-retrain BEFORE fusion from independent lifecycle outcomes. The full
    # forward-test stream is intentionally not repriced here: it contains
    # repeated scan snapshots and is run once later for monitoring.
    try:
        adaptive_outcomes_pre = bt_predictor.load_adaptive_outcomes()
        if adaptive_outcomes_pre.empty:
            adaptive_outcomes_pre = None
    except Exception:
        adaptive_outcomes_pre = None
    try:
        ic_df_pre = bt_predictor.load_cached_ic()
        if ic_df_pre is not None or adaptive_outcomes_pre is not None:
            coef_payload = bt_predictor.fit_return_predictor(adaptive_outcomes_pre, ic_df_pre)
            new_w = bt_predictor.update_runtime_weights(adaptive_outcomes_pre, ic_df_pre)
            if new_w:
                _config.SIGNAL_WEIGHTS = new_w
                fusion_rank.SIGNAL_WEIGHTS = new_w
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
        sec_ftd_df = _to_df(results.get("sec_ftd"))
        engine_timings = results.get("_timings", {}) if isinstance(results, dict) else {}
    except NameError:
        cot_df = thirteen_f_df = vix_term_df = eia_df = wasde_df = pd.DataFrame()
        buybacks_df = gtrends_df = form_144_df = whisper_df = pd.DataFrame()
        hyperliquid_df = twitter_df = r_options_df = pd.DataFrame()
        yield_curve_df = credit_spread_df = sec_ftd_df = pd.DataFrame()
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
        "yield_curve":  "public/keyed curve series unavailable",
        "credit_spread":"public/keyed HY/IG series unavailable",
        "sec_ftd":      "no SEC fails-to-deliver rows overlapped the universe, or SEC fetch failed",
        "cluster_buys": "no insider triple-buys in last 90 days",
    }
    v20_df_map = {
        "cot": cot_df, "thirteen_f": thirteen_f_df, "vix_term": vix_term_df,
        "eia": eia_df, "wasde": wasde_df, "buybacks": buybacks_df,
        "gtrends": gtrends_df, "form_144": form_144_df, "whisper": whisper_df,
        "hyperliquid": hyperliquid_df, "twitter": twitter_df,
        "r_options": r_options_df, "yield_curve": yield_curve_df,
        "credit_spread": credit_spread_df, "sec_ftd": sec_ftd_df,
        "cluster_buys": cluster_buys_df,
    }
    empty_engines = []
    for name, df in v20_df_map.items():
        if df is None or (hasattr(df, "empty") and df.empty):
            empty_engines.append({"name": name, "reason": EMPTY_REASONS.get(name, "")})
    if empty_engines:
        log.info("v20 empty engines (%d): %s",
                 len(empty_engines),
                 ", ".join(e["name"] for e in empty_engines))
    engine_health_summary = {}
    try:
        from telemetry.engine_health import record as _record_engine_health
        engine_health_summary = _record_engine_health(
            engine_timings if "engine_timings" in dir() else {},
            empty_engines=empty_engines,
        )
    except Exception as e:
        log.debug("engine health record skipped: %s", e)

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
    ranked_opts = bt_sizing.add_pre_guard_qualification(ranked_opts, asset="option")
    research_guard_report = None
    try:
        from risk.research_guard import (
            apply_to_asset as _apply_research_guard_asset,
            apply_to_recommendations as _apply_research_guard,
            build_guard_report as _build_guard_report,
            save_guard_report as _save_guard_report,
        )
        research_guard_report = _build_guard_report(
            empty_engines=empty_engines if "empty_engines" in dir() else None,
            engine_health=engine_health_summary if "engine_health_summary" in dir() else None,
        )
        _save_guard_report(research_guard_report)
        ranked_opts, _ = _apply_research_guard(ranked_opts, guard_report=research_guard_report)
        for warning in research_guard_report.get("warnings", [])[:4]:
            log.warning("research guard: %s", warning.get("message"))
    except Exception as e:
        log.debug("research guard skipped for options: %s", e)
    ranked_opts = bt_sizing.sort_for_trade_selection(ranked_opts, asset="option")
    option_candidates = ranked_opts
    if "trade_status" in ranked_opts.columns:
        option_candidates = ranked_opts[ranked_opts["trade_status"] != "Skip"]
    top_opts = fusion_rank.top_options(option_candidates, max_calls=args.max_calls,
                                       max_puts=args.max_puts)
    if not top_opts.empty:
        if "underlying_type" not in top_opts.columns:
            top_opts["underlying_type"] = "equity"
        else:
            top_opts["underlying_type"] = (
                top_opts["underlying_type"].fillna("").astype(str).str.strip().str.lower()
            )
            top_opts.loc[top_opts["underlying_type"].eq(""), "underlying_type"] = "equity"
        option_symbols = top_opts.get("ticker", pd.Series("", index=top_opts.index)).astype(str)
        top_opts.loc[option_symbols.map(is_known_index_option_symbol), "underlying_type"] = "index"

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
                                             congress=congress_df, social=social_df,
                                             analyst=analyst_df, sector_rs=sector_rs_df,
                                             dark_pool=dark_pool_df, fda=fda_df,
                                             sector_flow=sector_flow_df,
                                             technicals=technicals_df,
                                             short_int=short_int_df, cot=cot_df,
                                             thirteen_f=thirteen_f_df,
                                             vix_term=vix_term_df, eia=eia_df,
                                             wasde=wasde_df, buybacks=buybacks_df,
                                             gtrends=gtrends_df, form_144=form_144_df,
                                             whisper=whisper_df,
                                             hyperliquid=hyperliquid_df,
                                             twitter=twitter_df,
                                             r_options=r_options_df,
                                             yield_curve=yield_curve_df,
                                             credit_spread=credit_spread_df,
                                             cluster_buys=cluster_buys_df,
                                             sec_ftd=sec_ftd_df)
    if has_predictor:
        ranked_shares = bt_predictor.add_predictions_to_shares(ranked_shares, coefs)
    ranked_shares = bt_sizing.add_sizing_to_shares(ranked_shares, bankroll=args.bankroll,
                                                    aggressive=args.aggressive,
                                                    drawdown_mult=drawdown_mult)
    ranked_shares = bt_sizing.add_pre_guard_qualification(ranked_shares, asset="share")
    if research_guard_report is not None:
        try:
            _, ranked_shares = _apply_research_guard(
                None, ranked_shares, guard_report=research_guard_report
            )
        except Exception as e:
            log.debug("research guard skipped for shares: %s", e)
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
        try:
            futures_df = fusion_rank.enrich_futures_context(
                futures_df, macro_state, sentiment=sent_df,
                fundamentals=fund_df, insider=ins_df, news=news_df,
                earnings=earn_df, value=value_df, congress=congress_df,
                social=social_df, analyst=analyst_df, sector_rs=sector_rs_df,
                dark_pool=dark_pool_df, fda=fda_df,
                sector_flow=sector_flow_df, technicals=technicals_df,
                short_int=short_int_df, cot=cot_df,
                thirteen_f=thirteen_f_df, vix_term=vix_term_df,
                eia=eia_df, wasde=wasde_df, buybacks=buybacks_df,
                gtrends=gtrends_df, form_144=form_144_df,
                whisper=whisper_df, hyperliquid=hyperliquid_df,
                twitter=twitter_df, r_options=r_options_df,
                yield_curve=yield_curve_df,
                credit_spread=credit_spread_df,
                cluster_buys=cluster_buys_df,
                sec_ftd=sec_ftd_df,
            )
        except Exception as e:
            log.debug("futures context enrichment skipped: %s", e)
        fut_rank_col = "rank_score" if "rank_score" in futures_df.columns else "futures_score"
        # Bullish + bearish split
        bullish = futures_df[futures_df["futures_score"] > 0.3].sort_values(fut_rank_col, ascending=False)
        bearish = futures_df[futures_df["futures_score"] < -0.3].sort_values(fut_rank_col)
        top_fut = pd.concat([bullish.head(args.max_futures // 2),
                             bearish.head(args.max_futures // 2)], ignore_index=True)
        try:
            from backtest.futures_sizing import add_sizing_to_futures
            top_fut = add_sizing_to_futures(top_fut, bankroll=args.bankroll,
                                            aggressive=args.aggressive)
            top_fut = bt_sizing.add_pre_guard_qualification(top_fut, asset="futures")
            if not futures_df.empty:
                futures_df = add_sizing_to_futures(futures_df, bankroll=args.bankroll,
                                                   aggressive=args.aggressive)
                futures_df = bt_sizing.add_pre_guard_qualification(
                    futures_df, asset="futures",
                )
            if research_guard_report is not None:
                top_fut = _apply_research_guard_asset(
                    top_fut, guard_report=research_guard_report, asset="futures",
                )
                futures_df = _apply_research_guard_asset(
                    futures_df, guard_report=research_guard_report, asset="futures",
                )
        except Exception as e:
            log.debug("futures sizing skipped: %s", e)

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
    _save(sec_ftd_df, "sec_ftd")
    with open(out_dir / f"macro_{asof_tag}.json", "w") as f:
        json.dump(macro_state, f, indent=2, default=str)

    try:
        if not top_opts.empty:
            backtest_track.log_signals(top_opts, run_asof)
        if not top_sh.empty:
            backtest_track.log_signals_shares(top_sh, run_asof)
        if not top_fut.empty:
            backtest_track.log_signals_futures(top_fut, run_asof)
    except Exception as e:
        log.warning("signal log failed: %s", e)

    # v20.7 — position-level P&L tracking (distinguishes still-open from closed).
    # Review positions present at scan start before adding new recommendations;
    # this prevents a fresh entry from becoming a synthetic same-scan exit.
    try:
        from backtest import positions as _pos
        position_summary = _pos.mark_to_market(run_asof, current_signals=ranked_opts)
        if position_summary.get("expired_removed_from_open", 0) > 0:
            log.info(
                "positions: expiry cleanup removed %d stale open record(s), added %d closed",
                position_summary.get("expired_removed_from_open", 0),
                position_summary.get("expired_closed", 0),
            )
        if not top_opts.empty:
            _pos.add_new_signals(top_opts, run_asof)
    except Exception as e:
        log.debug("position tracking skipped: %s", e)

    try:
        from backtest import share_positions as _share_pos
        _share_pos.mark_to_market_shares(run_asof, current_signals=ranked_shares)
        if not top_sh.empty:
            _share_pos.add_new_share_signals(top_sh, run_asof)
    except Exception as e:
        log.warning("share lifecycle skipped: %s", e)

    try:
        from backtest import futures_positions as _fut_pos
        _fut_pos.mark_to_market_futures(run_asof, current_signals=futures_df)
        if not top_fut.empty:
            _fut_pos.add_new_futures_signals(top_fut, run_asof)
    except Exception as e:
        log.warning("futures lifecycle skipped: %s", e)

    try:
        from backtest.exit_learning import refit_exit_policy
        refit_exit_policy()
    except Exception as e:
        log.debug("exit policy refit skipped: %s", e)

    fixed_horizon_result = None
    try:
        from backtest.fixed_horizon import run_fixed_horizon_test

        fixed_horizon_result = run_fixed_horizon_test()
        fixed_summary = fixed_horizon_result.get("summary", {})
        fixed_headline = fixed_summary.get("headline", {})
        fixed_shadow = fixed_summary.get("headline_shadow", {})
        log.info(
            "fixed horizon: %d total pairs; %d executed / %d shadow at %d sessions",
            fixed_summary.get("matured_outcome_pairs", 0),
            fixed_headline.get("n", 0),
            fixed_shadow.get("n", 0),
            fixed_summary.get("headline_horizon_sessions", 10),
        )
    except Exception as e:
        log.warning("fixed-horizon validation skipped: %s", e)

    validation_summary = None
    try:
        from reports.validation_report import write_report as _write_validation_report
        validation_summary = _write_validation_report(scope="current_model")
        log.info("validation refreshed: %d closed / %d open",
                 validation_summary.get("closed_positions", 0),
                 validation_summary.get("open_positions", 0))
    except Exception as e:
        log.debug("validation refresh skipped: %s", e)

    tv_text = fusion_rank.to_tv_watchlist(calls, puts, top_sh)
    tv_path = out_dir / f"tradingview_watchlist_{asof_tag}.txt"
    tv_path.write_text(tv_text)

    # Forward-test summary for the dashboard panel
    forward_summary = None
    calibration_summary = None
    try:
        from backtest.forward import run_forward_test
        ft = run_forward_test(include_fixed_horizon=False)
        if not ft["signals"].empty:
            forward_summary = ft
            log.info("forward test: %d signals, %.1f%% win rate, %.2f%% avg P&L",
                     ft["overall"]["n_signals"],
                     ft["overall"]["win_rate"]*100,
                     ft["overall"]["avg_pnl_pct"]*100)
            # Calibration requires one fixed holding period. Mixed-age current
            # marks remain telemetry and cannot prove prediction calibration.
            try:
                from backtest.calibration import diagnostic_summary
                fixed_outcomes = (
                    fixed_horizon_result.get("outcomes", pd.DataFrame())
                    if fixed_horizon_result else pd.DataFrame()
                )
                if fixed_outcomes.empty:
                    raise ValueError("no fixed-horizon outcomes")
                headline_horizon = int(
                    fixed_horizon_result.get("summary", {}).get(
                        "headline_horizon_sessions", 10,
                    )
                )
                eligible = fixed_outcomes[
                    fixed_outcomes.get(
                        "is_independent", pd.Series(False, index=fixed_outcomes.index),
                    ).fillna(False).astype(bool)
                    & fixed_outcomes.get(
                        "eligible_for_shadow_metrics",
                        pd.Series(False, index=fixed_outcomes.index),
                    ).fillna(False).astype(bool)
                    & (pd.to_numeric(
                        fixed_outcomes.get("horizon_sessions"), errors="coerce",
                    ) == headline_horizon)
                ].copy()
                if eligible.empty:
                    raise ValueError("no matured fixed-horizon current-method shadow outcomes")
                calibration_summary = diagnostic_summary(eligible)
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
        research_guard_report=research_guard_report,
        engine_timings=engine_timings if 'engine_timings' in dir() else {},
        engine_health=engine_health_summary,
        validation_summary=validation_summary,
        v20_factors=v20_df_map,
        empty_engines=empty_engines,
    )
    log.info("dashboard: %s", html_path)
    log.info("tradingview watchlist: %s", tv_path)
    log.info("total elapsed: %.1f sec", elapsed)

    robinhood_queue_paths = None
    robinhood_queue_summary = None
    if args.robinhood_agentic_queue:
        try:
            from scripts.export_robinhood_agentic_queue import (
                build_robinhood_queue as _build_robinhood_queue,
                write_outputs as _write_robinhood_queue,
            )
            rh_budget = args.robinhood_budget if args.robinhood_budget is not None else args.bankroll
            queue = _build_robinhood_queue(
                data_dir=out_dir,
                account_budget=rh_budget,
                max_orders=args.robinhood_max_orders,
                max_candidates=args.robinhood_max_candidates,
                min_dte=args.robinhood_min_dte,
                min_confidence=args.robinhood_min_confidence,
                max_premium_per_order=args.robinhood_max_premium_per_order,
                refresh_chain=args.robinhood_refresh_chain,
                chain_preset=args.robinhood_chain_preset,
                chain_symbols_limit=args.robinhood_chain_symbols_limit,
                chain_contracts_per_symbol=args.robinhood_chain_contracts_per_symbol,
            )
            robinhood_queue_paths = _write_robinhood_queue(queue, out_dir)
            robinhood_queue_summary = queue
            rh_diag = queue.get("diagnostics") if isinstance(queue.get("diagnostics"), dict) else {}
            rh_refresh = queue.get("chain_refresh") if isinstance(queue.get("chain_refresh"), dict) else {}
            log.info(
                "robinhood agentic queue: %s (%d candidates, max %d orders, min_dte %d, diagnosis=%s, chain_refresh=%s)",
                robinhood_queue_paths[0],
                len(queue.get("orders") or []),
                int(queue.get("max_orders_to_submit") or 0),
                int(queue.get("min_dte") or 0),
                rh_diag.get("label") or "-",
                "ok" if rh_refresh.get("ok") else "off" if not rh_refresh.get("attempted") else "failed",
            )
        except Exception as e:
            log.warning("robinhood agentic queue export failed: %s", e)

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

    print("\n+------------------------------------------+")
    print(f"|  Optedge run complete in {elapsed:5.1f}s")
    print(f"|  {len(calls)} calls / {len(puts)} puts / {len(top_sh)} shares / {len(top_value)} value / {len(top_fut)} futures")
    if trending:
        print(f"|  WSB trending added: {len(trending)}")
    if use_demo:
        print("|  [!] DEMO MODE (synthetic data)")
    print("+------------------------------------------+")
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
    def _fmt_cli_num(value, digits=2):
        try:
            if value is None or pd.isna(value):
                return "-"
            return f"{float(value):.{digits}f}"
        except Exception:
            return "-"

    for _, r in top_fut.head(10).iterrows():
        side = "LONG" if r["futures_score"] > 0 else "SHORT"
        contract = r.get("contract") or "-"
        micro = "micro" if r.get("using_micro") else "full"
        status = r.get("trade_status") or "Watch"
        ctx = r.get("futures_context_score")
        atr = r.get("atr20") if r.get("atr20") is not None else r.get("atr_estimate")
        print(
            f"  {r['symbol']:<8} {r['name']:<22} {side:<5} "
            f"score {r['futures_score']:+.2f}  ctx {(ctx if ctx is not None else 0):+.2f}  "
            f"20d {(r.get('ret_20d') or 0)*100:+.1f}%  "
            f"ATR {_fmt_cli_num(atr, 2)}  "
            f"{contract} {micro} x{int(r.get('suggested_contracts') or 0)}  "
            f"stop {_fmt_cli_num(r.get('stop_price'), 2)}  "
            f"target {_fmt_cli_num(r.get('target_price'), 2)}  "
            f"{status}"
        )
    print(f"\nDashboard: file://{html_path}")
    print(f"TradingView watchlist: {tv_path}")
    if robinhood_queue_paths:
        count = len((robinhood_queue_summary or {}).get("orders") or [])
        max_submit = int((robinhood_queue_summary or {}).get("max_orders_to_submit") or 0)
        rh_diag = (
            robinhood_queue_summary.get("diagnostics")
            if isinstance(robinhood_queue_summary, dict)
            and isinstance(robinhood_queue_summary.get("diagnostics"), dict)
            else {}
        )
        rh_refresh = (
            robinhood_queue_summary.get("chain_refresh")
            if isinstance(robinhood_queue_summary, dict)
            and isinstance(robinhood_queue_summary.get("chain_refresh"), dict)
            else {}
        )
        refresh_label = (
            "ok" if rh_refresh.get("ok")
            else "off" if not rh_refresh.get("attempted")
            else "failed"
        )
        print(
            f"Robinhood agentic queue: {robinhood_queue_paths[0]} "
            f"({count} candidates, max {max_submit} orders, "
            f"diagnosis {rh_diag.get('label') or '-'}, chain refresh {refresh_label})"
        )
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
    print(f"\n=== LOOP MODE - running every {known.loop} min, Ctrl+C to stop ===\n", flush=True)
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
        print(f"\n-- Iteration {iteration} @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} " + "-" * 30, flush=True)
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
            print(f"\n  [x] iteration {iteration} crashed: {iter_error}")
            traceback.print_exc()
            print("  continuing in next interval...")
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
        print(f"\n-- next run @ {next_run.strftime('%H:%M:%S')} (sleeping {known.loop} min)\n", flush=True)
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
