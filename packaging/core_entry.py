"""PyInstaller core entry, with explicit readiness and parent lifeline."""
from radioinspector.launcher import core_main

if __name__ == '__main__':
    raise SystemExit(core_main())
