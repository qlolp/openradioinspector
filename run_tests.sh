#!/usr/bin/env bash
# Запуск тестов Python с авто-обнаружением подходящего окружения
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON=""
if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    PYTHON="$ROOT_DIR/.venv/bin/python"
elif [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then
    PYTHON="$VIRTUAL_ENV/bin/python"
elif command -v uv >/dev/null 2>&1; then
    exec uv run --no-project python -m unittest discover -s tests -t . "$@"
else
    for candidate in python3.12 python3.11 python3.10 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c "import sys, numpy; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >/dev/null 2>&1; then
                PYTHON="$(command -v "$candidate")"
                break
            fi
        fi
    done
fi

if [[ -z "$PYTHON" ]]; then
    echo "Ошибка: Не найден Python >= 3.10 с установленным numpy." >&2
    echo "На macOS системный /usr/bin/python3 (версия 3.9) не содержит numpy и tomllib." >&2
    echo "Способы запуска:" >&2
    echo "  1) Активировать существующее виртуальное окружение: source .venv/bin/activate" >&2
    echo "  2) Или через uv: uv run --no-project python -m unittest discover -s tests -t ." >&2
    echo "  3) Или напрямую: ./.venv/bin/python -m unittest discover -s tests -t ." >&2
    exit 1
fi

exec "$PYTHON" -m unittest discover -s tests -t . "$@"
