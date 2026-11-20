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

SECRET_RE = re.compile(
    r"""(?i)\b(password|passwd|secret|api[_-]?key|token|private[_-]?key)\b['"]?\s*[:=]\s*["'][^"']{6,}["']"""
)


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

    def __init__(self, path: Path) -> None:
        self.path = str(path)
        self.findings: list[Finding] = []
        self.is_test = "tests" in path.parts or path.name.startswith("test_")
        self.aliases: dict[str, str] = {}
        self.imported: dict[str, int] = {}
        self.used_names: set[str] = set()

    # -- вспомогательное ---------------------------------------------------
    def add(self, rule: str, node: ast.AST, message: str) -> None:
        self.findings.append(Finding(rule, self.path, getattr(node, "lineno", 0), message))

    # -- обход -------------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            self.aliases[name] = alias.name if alias.asname else name
            self.imported[name] = node.lineno

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "__future__":
            return
        for alias in node.names:
            if alias.name != "*":
                name = alias.asname or alias.name
                self.aliases[name] = f"{node.module}.{alias.name}"
                self.imported[name] = node.lineno

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.used_names.add(node.id)

    def visit_Assign(self, node: ast.Assign) -> None:
        if any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
            self.used_names.update(
                value.value for value in ast.walk(node.value)
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            )
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self.add("BARE-EXCEPT", node, "except без типа — глушит любые ошибки")
        elif isinstance(node.type, ast.Name) and node.type.id == "Exception":
            body = node.body
            silent = all(isinstance(stmt, ast.Pass) for stmt in body)
            if silent:
                self.add("SWALLOW-EXC", node, "except Exception: pass — ошибка теряется")
            elif len(body) == 1 and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
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
        func = node.func
        if isinstance(func, ast.Name):
            name = self.aliases.get(func.id, func.id)
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            module = self.aliases.get(func.value.id, func.value.id)
            name = f"{module}.{func.attr}"
        else:
            name = ""
        if name in ("eval", "exec", "compile", "builtins.eval", "builtins.exec", "builtins.compile"):
            self.add("DYNAMIC-CODE", node, f"{name}() — динамическая обработка кода")
        if name in ("pickle.load", "pickle.loads", "marshal.load", "marshal.loads", "shelve.open"):
            self.add("UNSAFE-PICKLE", node, f"{name}() — десериализация данных")
        shell_functions = ("subprocess.run", "subprocess.Popen", "subprocess.call",
                           "subprocess.check_call", "subprocess.check_output")
        if name in ("os.system", "os.popen") or (name in shell_functions and any(
            kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        )):
            self.add("SHELL", node, f"{name}() — вызов оболочки")
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

    checker = Checker(path)
    checker.visit(tree)
    findings = list(checker.findings)

    for line, content in enumerate(source.splitlines(), 1):
        if match := SECRET_RE.search(content):
            findings.append(Finding("SECRET", str(path), line, f"литерал в поле {match.group(1)}"))
    unused = [(name, line) for name, line in checker.imported.items() if name not in checker.used_names]
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
