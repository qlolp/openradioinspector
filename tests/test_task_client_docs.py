"""Документация и подписи клиентов соответствуют create/cancel ControlTask."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TaskClientDocumentationTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_api_contract_describes_create_and_queued_cancel(self) -> None:
        document = self.read("docs/api-v1.md")
        self.assertIn("создают queued-задачи", document)
        self.assertIn("отменяют только задачи в статусе queued", document)
        self.assertNotIn("Клиенты не создают и не редактируют `ControlTask`", document)

    def test_scheduler_guide_covers_all_three_clients(self) -> None:
        document = self.read("docs/task-scheduler.md")
        self.assertIn("WebUI создаёт queued-задачи", document)
        self.assertIn("Форма создаёт queued-задачи", document)
        self.assertIn("Панель создаёт и отменяет queued-задачи через API", document)
        self.assertNotIn("Форма не создаёт и не редактирует задания", document)
        self.assertNotIn("Панель не создаёт задания", document)

    def test_webui_label_does_not_deny_task_creation(self) -> None:
        page = self.read("radioinspector/web/static/platform.html")
        self.assertIn("Браузер создаёт и отменяет queued-задачи только через API", page)
        self.assertNotIn("Браузер не создаёт задания", page)

    def test_macos_labels_match_public_taskbook_actions(self) -> None:
        pane = self.read("macos/DvoynoyObzor/PaneChrome.swift")
        self.assertIn("TaskBook · публичный снимок", pane)
        self.assertIn("Клиент создаёт и отменяет queued-задачи только через API", pane)
        self.assertNotIn("TaskBook · read-only", pane)
        self.assertNotIn("Клиент не создаёт задания", pane)


if __name__ == "__main__":
    unittest.main(verbosity=2)
