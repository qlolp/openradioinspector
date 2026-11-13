"""Статические проверки исходного кода без внешних зависимостей.

Инструмент применяет набор эвристик к модулям пакета и печатает находки с
указанием файла и строки. Каждое правило помечено идентификатором, чтобы
результат можно было отфильтровать и обсуждать по существу, а не по совпадению
строк.

Запуск:

    python3 tools/audit/code_checks.py radioinspector
    python3 tools/audit/code_checks.py radioinspector --rule BARE-EXCEPT
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: Каталоги, где находки ожидаемы и не считаются дефектами.
TEST_MARKERS = ("test_", "tests/")

SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|secret|api[_-]?key|token|private[_-]?key)\b\s*[:=]\s*[\"'][^\"']{6,}[\"']"
)
TODO_RE = re.compile(r"#\s*(TODO|FIXME|XXX|HACK)\b", re.I)


@dataclass
class Finding:
    """Находка статического анализа."""

    rule: str
    path: str
    line: int
    message: str

    def format(self) -> str:
        return f"{self.rule:<16} {self.path}:{self.line:<5} {self.message}"


class Checker(ast.NodeVisitor):
    """Сбор находок по одному модулю."""

    def __init__(self, path: str, source: str) -> None:
        self.path = path
        self.source = source
        self.findings: list[Finding] = []
        self.is_test = any(marker in path for marker in TEST_MARKERS)
        self.used_names: set[str] = set()

    # -- вспомогательное ---------------------------------------------------
    def add(self, rule: str, node: ast.AST, message: str) -> None:
        self.findings.append(Finding(rule, self.path, getattr(node, "lineno", 0), message))

    # -- обход -------------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            self.used_names.add(name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                continue
            self.used_names.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self.used_names.add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        base = node
        while isinstance(base, ast.Attribute):
            base = base.value
        if isinstance(base, ast.Name):
            self.used_names.add(base.id)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self.add("BARE-EXCEPT", node, "except без типа — глушит любые ошибки")
        elif isinstance(node.type, ast.Name) and node.type.id == "Exception":
            body = node.body
            silent = all(isinstance(stmt, ast.Pass) for stmt in body)
            if silent:
                self.add("SWALLOW-EXC", node, "except Exception: pass — ошибка теряется")
            elif len(body) == 1 and isinstance(body[0], ast.Expr):
                self.add("SWALLOW-EXC", node, "except Exception без логирования")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for default in list(node.args.defaults) + [d for d in node.args.kw_defaults if d]:
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                self.add(
                    "MUTABLE-DEFAULT",
                    node,
                    f"изменяемый аргумент по умолчанию в {node.name}()",
                )
        branches = sum(
            1
            for n in ast.walk(node)
            if isinstance(n, (ast.If, ast.For, ast.While, ast.Try, ast.With))
        )
        length = (node.end_lineno or node.lineno) - node.lineno
        if branches >= 12:
            self.add("COMPLEXITY", node, f"{node.name}(): ветвлений {branches}")
        if length >= 150 and not self.is_test:
            self.add("LONG-FUNC", node, f"{node.name}(): {length} строк")
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Assert(self, node: ast.Assert) -> None:
        if not self.is_test:
            self.add("ASSERT-IN-PROD", node, "assert в рабочем коде — снимается флагом -O")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = ""
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name in ("eval", "exec", "compile"):
            self.add("DYNAMIC-CODE", node, f"{name}() — исполнение кода из данных")
        elif name in ("pickle", "marshal", "shelve"):
            self.add("UNSAFE-PICKLE", node, f"{name}() — десериализация без проверки")
        elif name == "system" and isinstance(func, ast.Attribute) and func.attr == "popen":
            self.add("SHELL", node, "os.system()")
        if name == "run" and isinstance(func, ast.Attribute) and func.attr == "subprocess":
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value:
                    self.add("SHELL", node, "subprocess с shell=True")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            match = SECRET_RE.search(node.value)
            if match:
                self.add("SECRET", node, f"похоже на секрет: {match.group(0)[:40]}")
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.add("GLOBAL", node, f"global {', '.join(node.names)}")
        self.generic_visit(node)


def analyse(path: Path) -> tuple[list[Finding], list[str]]:
    """Находки и неиспользуемые импорты для одного файла."""
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [Finding("SYNTAX", str(path), exc.lineno or 0, f"синтаксическая ошибка: {exc.msg}")], []

    checker = Checker(str(path), source)
    checker.visit(tree)
    findings = list(checker.findings)

    imported: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported[alias.asname or alias.name.split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    imported[alias.asname or alias.name] = node.lineno
    unused = [
        (name, line) for name, line in imported.items() if name not in checker.used_names
    ]
    for name, line in sorted(unused, key=lambda item: item[1]):
        findings.append(Finding("UNUSED-IMPORT", str(path), line, f"импорт {name} не используется"))
    return findings, [name for name, _ in unused]


def main() -> None:
    parser = argparse.ArgumentParser(description="Статические проверки исходного кода")
    parser.add_argument("paths", nargs="+", help="каталоги или файлы для проверки")
    parser.add_argument("--rule", help="показывать только находки этого правила")
    args = parser.parse_args()

    targets: list[Path] = []
    for raw in args.paths:
        root = Path(raw)
        if root.is_dir():
            targets.extend(sorted(root.rglob("*.py")))
        else:
            targets.append(root)

    total = 0
    by_rule: dict[str, int] = {}
    for path in targets:
        findings, _ = analyse(path)
        for finding in findings:
            if args.rule and finding.rule != args.rule:
                continue
            total += 1
            by_rule[finding.rule] = by_rule.get(finding.rule, 0) + 1
            print(finding.format())
    print(f"\n# проверено файлов: {len(targets)}; находок: {total}")
    for rule, count in sorted(by_rule.items(), key=lambda item: -item[1]):
        print(f"#   {rule:<16} {count}")
    return None


if __name__ == "__main__":
    sys.exit(main())
