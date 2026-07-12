"""Archive generated Optedge run artifacts without deleting anything.

Moves generated data and logs into uniquely timestamped directories, with a
dry-run preview and explicit control over whether learned state is preserved.
"""
from __future__ import annotations

import argparse
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Tuple

ROOT = Path(__file__).resolve().parent

ARCHIVE_PATTERNS = [
    "data/*.parquet",
    "data/signals_*.json",
    "data/backtest_summary.json",
    "data/engine_health.json",
    "data/engine_health_history.jsonl",
    "data/heston_stability.json",
    "data/model_weights_history.jsonl",
    "data/predictor_coefs.json",
    "data/research_guard.json",
    "data/open_positions.json",
    "data/closed_positions.json",
    "data/open_share_positions.json",
    "data/closed_share_positions.json",
    "data/open_futures_positions.json",
    "data/closed_futures_positions.json",
    "data/tracked_*.json",
    "data/validation_report.html",
    "data/validation_summary.json",
    "data/fixed_horizon_summary.json",
    "data/robinhood_option_history_requests.json",
    "data/robinhood_option_history_prompt.md",
    "data/robinhood_option_history_snapshot.json",
    "data/robinhood_option_history_coverage.json",
    "data/robinhood_research_requests.json",
    "data/robinhood_research_prompt.md",
    "data/robinhood_research_snapshot.json",
    "data/robinhood_research_coverage.json",
    "data/equity_curve.png",
    "data/factor_ic_summary.json",
    "data/position_aging_summary.json",
    "data/model_weights.json",
    "data/exit_policy.json",
    "data/exit_policy_history.jsonl",
    "data/exit_reviews.jsonl",
    "data/robinhood_agentic_queue.json",
    "data/robinhood_agentic_prompt.md",
    "data/robinhood_agentic_cycle.json",
    "data/robinhood_agentic_cycle_prompt.md",
    "data/robinhood_agentic_decisions.jsonl",
    "data/agentic_paper_positions.json",
    "data/agentic_paper_orders.jsonl",
    "data/robinhood_live_order_tickets.json",
    "data/robinhood_broker_snapshot.json",
    "data/robinhood_mcp_snapshot_raw.json",
    "data/lookup_*.json",
    "data/lookup_*.html",
    "data/lookup_history.jsonl",
    "data/lookup_reports/**/*",
    "data/dashboard_*.html",
    "data/tradingview_watchlist_*.txt",
    "data/macro_*.json",
    "logs/**/*",
]

LEARNED_FILES = {
    Path("data/model_weights.json"),
    Path("data/exit_policy.json"),
    Path("data/exit_policy_history.jsonl"),
    Path("data/exit_reviews.jsonl"),
}


def _unique_archive_root(root: Path) -> Path:
    base = root / "archive" / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if not base.exists():
        return base
    for i in range(1, 100):
        candidate = Path(f"{base}_{i:02d}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("Could not create a unique archive folder")


def _iter_matches(root: Path, keep_learned: bool = False) -> List[Path]:
    matches = []
    seen = set()
    for pattern in ARCHIVE_PATTERNS:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if path.name == ".keep":
                continue
            if keep_learned and rel in LEARNED_FILES:
                continue
            if rel in seen:
                continue
            seen.add(rel)
            matches.append(path)
    return sorted(matches, key=lambda p: str(p.relative_to(root)))


def run_archive(root: Path = ROOT, dry_run: bool = False,
                keep_learned: bool = False) -> Tuple[Path, List[Path]]:
    root = Path(root).resolve()
    archive_root = _unique_archive_root(root)
    matches = _iter_matches(root, keep_learned=keep_learned)
    if dry_run:
        return archive_root, matches
    for src in matches:
        rel = src.relative_to(root)
        dst = archive_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    return archive_root, matches


def _print_summary(archive_root: Path, moved: Iterable[Path], root: Path,
                   dry_run: bool, keep_learned: bool) -> None:
    moved = list(moved)
    counts = Counter(str(p.relative_to(root).parts[0]) for p in moved)
    verb = "Would archive" if dry_run else "Archived"
    learned_mode = "preserved" if keep_learned else "archived"
    print(f"{verb} {len(moved)} files")
    print(f"Archive folder: {archive_root}")
    for folder, count in sorted(counts.items()):
        print(f"  {folder}: {count}")
    print(f"Learned/adaptive files: {learned_mode}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive generated Optedge artifacts safely")
    parser.add_argument("--dry-run", action="store_true", help="Show what would move without moving files")
    parser.add_argument("--keep-learned", action="store_true",
                        help="Preserve learned/adaptive files while archiving run history")
    args = parser.parse_args()
    archive_root, moved = run_archive(ROOT, dry_run=args.dry_run, keep_learned=args.keep_learned)
    _print_summary(archive_root, moved, ROOT, args.dry_run, args.keep_learned)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
