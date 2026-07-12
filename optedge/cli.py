# Purpose: Route requests to scans loops lookups and the cockpit.
"""Command-line entry point for Optedge."""
from __future__ import annotations

import sys


def _arg_value(flag: str) -> str | None:
    for idx, arg in enumerate(sys.argv[1:], start=1):
        if arg == flag and idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
    return None


def _has_arg(flag: str) -> bool:
    return any(arg == flag or arg.startswith(flag + "=") for arg in sys.argv[1:])


def main() -> int:
    """Route CLI calls to the correct application mode."""
    if _has_arg("--cockpit"):
        from pathlib import Path

        from scripts.local_cockpit import main as cockpit_main

        args = []
        for flag in ("--host", "--port"):
            value = _arg_value(flag)
            if value:
                args.extend([flag, value])
        out_dir = _arg_value("--out-dir")
        if out_dir:
            args.extend(["--data-dir", out_dir])
        if _has_arg("--no-open"):
            args.append("--no-open")
        if _has_arg("--help") or _has_arg("-h"):
            args.append("--help")
        return cockpit_main(args)

    lookup = _arg_value("--lookup")
    if lookup:
        from pathlib import Path

        from scripts.lookup_symbol import lookup_symbol, save_lookup

        out_dir = _arg_value("--out-dir") or str(Path(__file__).resolve().parent.parent / "data")
        report = lookup_symbol(lookup, Path(out_dir))
        paths = save_lookup(report, Path(out_dir))
        print(f"\nLookup report: {paths['html']}")
        print(f"Lookup JSON: {paths['json']}")
        print(f"Hits: {report['total_hits']}")
        if report["total_hits"] == 0:
            print(f"Tip: run a focused scan with: python run.py --universe {lookup.upper()} --no-open")
        return 0

    from . import orchestrator

    if any(arg == "--loop" or arg.startswith("--loop=") for arg in sys.argv):
        return orchestrator.main_loop()
    return orchestrator.main()


if __name__ == "__main__":
    raise SystemExit(main())
