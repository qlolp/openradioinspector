"""PyInstaller GUI entry; core and CLI remain separate installed commands."""
from radioinspector.launcher import installed_main

if __name__ == '__main__':
    raise SystemExit(installed_main())
