"""Точка входа для запуска пакета как модуля: ``python -m radioinspector``."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
