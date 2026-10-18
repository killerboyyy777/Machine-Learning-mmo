"""`python -m tui` entry point."""

import sys

try:
    from .app import main
except ImportError as exc:
    # rich is ops-only tooling, so a bare websockets install can import
    # tui but cannot render it; say so instead of a bare traceback.
    print(f"tui needs rich: pip install -r requirements.txt ({exc})", file=sys.stderr)
    sys.exit(2)

sys.exit(main())
