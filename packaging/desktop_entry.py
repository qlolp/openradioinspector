"""PyInstaller GUI entry; core and CLI remain separate installed commands."""
from radioinspector.launcher import main

if __name__ == '__main__':
    raise SystemExit(main())
