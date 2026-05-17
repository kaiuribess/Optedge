# Architecture

Optedge is a local research cockpit built around a scan, fuse, size, log, and validate loop.

## Flow

1. Universe construction combines configured tickers with live WSB discovery.
2. Engines collect independent factors: options mispricing, sentiment, news, fundamentals, earnings, insider activity, Congress, futures, macro, flows, and technicals.
3. Fusion combines factor rows into ranked options, shares, value plays, and futures.
4. Sizing applies EV, fractional Kelly, slippage, sector caps, and setup-quality multipliers.
5. Tracking writes signal logs and position state.
6. Validation reads logged signals and closed/open positions to produce a formal research report.

## Main Modules

- `run.py`: compatibility entry point for the current live scanner.
- `engines/`: individual data/factor collectors.
- `fusion/`: cross-factor ranking and watchlist generation.
- `backtest/`: sizing, forward tests, position tracking, calibration, drawdown controls.
- `dashboard/`: local HTML cockpit rendering.
- `reports/`: formal validation reports and research artifacts.
- `risk/`: research safety guardrails.

## Refactor Direction

The long-term app shape is:

```text
optedge/
  cli.py
  orchestrator.py
  engine_registry.py
  modes/
    scan.py
    forward.py
    backtest.py
    loop.py
```

The current release adds the research/reporting layer first because it improves trust without destabilizing the live scanner.
