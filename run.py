"""Start Optedge from a source checkout by delegating to its CLI router."""

from optedge.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
