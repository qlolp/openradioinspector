"""On-demand сводка всего архива для Windows GUI."""

from __future__ import annotations

from typing import Any, Optional

from ..archive_ingest import ArchiveIngestError
from .client import PlatformClientError
from .completion_app import CompletionWindowsApp


def analytics_text(summary: Optional[dict[str, Any]], error: Optional[str] = None) -> str:
    """Форматирует только фактический ответ analytics API с исходными единицами."""
    if error:
        return f"Сводка архива недоступна: {error}. Live-измерения продолжаются"
    if not summary:
        return "Сводка архива не запрашивается при обычном обновлении"
    sweeps = summary.get("sweeps") or {}
    observations = summary.get("observations") or {}
    ratio = observations.get("squelch_open_ratio")
    squelch = "нет данных" if ratio is None else f"{ratio * 100:.1f}%"
    text = (
        f"Архив: {sweeps.get('count', 0)} sweeps "
        f"{','.join(sweeps.get('level_units') or []) or 'нет данных'} · "
        f"{observations.get('count', 0)} observations "
        f"{','.join(observations.get('level_units') or []) or 'нет данных'} · "
        f"шумодав {squelch}"
    )
    peak = sweeps.get("latest_peak")
    if peak:
        text += (
            f" · пик {peak.get('frequency_hz')} Гц "
            f"({peak.get('level')} {peak.get('level_unit')})"
        )
    return text


class AnalyticsWindowsApp(CompletionWindowsApp):
    """Не включает archive-wide запрос в периодический snapshot."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._analytics_session_id: Optional[str] = None
        self.analytics_var = self.tk.StringVar(value=analytics_text(None))
        self.analytics_label = self.tk.Label(
            self.root,
            textvariable=self.analytics_var,
            wraplength=1100,
            justify="left",
        )
        self.analytics_label.pack(side="top", fill="x", padx=10, pady=(0, 8))
        self.analytics_button = self.tk.Button(
            self.root,
            text="Запросить сводку всего архива",
            command=self.refresh_analytics,
        )
        self.analytics_button.pack(side="top", fill="x", padx=10, pady=(0, 8))
        self.archive_export_var = self.tk.StringVar(
            value="NDJSON snapshot ещё не выгружен"
        )
        self.archive_export_label = self.tk.Label(
            self.root,
            textvariable=self.archive_export_var,
            wraplength=1100,
            justify="left",
        )
        self.archive_export_label.pack(side="top", fill="x", padx=10, pady=(0, 8))
        self.archive_export_button = self.tk.Button(
            self.root,
            text="Скачать NDJSON snapshot…",
            command=self.export_archive,
        )
        self.archive_export_button.pack(side="top", fill="x", padx=10, pady=(0, 8))

    def _apply_snapshot(self, snapshot) -> None:
        super()._apply_snapshot(snapshot)
        if self._analytics_session_id != self.session_id:
            self._analytics_session_id = self.session_id
            self.analytics_var.set(analytics_text(None))

    def _render_controls(self) -> None:
        super()._render_controls()
        if hasattr(self, "analytics_button"):
            self.analytics_button.configure(
                state="normal"
                if self.client is not None
                and self.session_id is not None
                and not self._action_running
                else "disabled"
            )
        if hasattr(self, "archive_export_button"):
            self.archive_export_button.configure(
                state="normal"
                if self.client is not None
                and self.session_id is not None
                and not self._action_running
                else "disabled"
            )

    def refresh_analytics(self) -> None:
        """Выполняет один archive-wide запрос для захваченного сеанса."""
        if (
            self.client is None
            or self.session_id is None
            or self._action_running
            or self._closed
        ):
            return
        client = self.client
        session_id = self.session_id
        self._analytics_session_id = session_id
        self.analytics_var.set("Сводка всего архива запрашивается у ядра…")

        def load() -> tuple[Optional[dict[str, Any]], Optional[str]]:
            try:
                return client.analytics_summary(session_id), None
            except PlatformClientError as exc:
                return None, str(exc)

        self._submit_action(
            "Запрос сводки всего архива",
            load,
            self._apply_analytics_result,
        )

    def export_archive(self) -> None:
        """Сохраняет серверный NDJSON snapshot в выбранный оператором файл."""
        if (
            self.client is None
            or self.session_id is None
            or self._action_running
            or self._closed
        ):
            return
        safe_name = self.session_id.replace("/", "_").replace("\\", "_")
        destination = self.tk.filedialog.asksaveasfilename(
            title="Сохранить NDJSON snapshot",
            initialfile=f"archive-{safe_name}.ndjson",
            defaultextension=".ndjson",
            filetypes=[("NDJSON", "*.ndjson"), ("Все файлы", "*")],
        )
        if not destination:
            return
        client = self.client
        session_id = self.session_id
        self.archive_export_var.set("Сервер формирует NDJSON snapshot…")

        def export() -> dict[str, Any]:
            try:
                return client.export_archive(session_id, destination)
            except (ArchiveIngestError, OSError, ValueError) as exc:
                return {"error": str(exc)}

        self._submit_action(
            "Экспорт NDJSON snapshot",
            export,
            self._apply_archive_export_result,
        )

    def _apply_archive_export_result(self, result: dict[str, Any]) -> None:
        if result.get("error"):
            self.archive_export_var.set(
                f"NDJSON не сохранён: {result['error']}. Live-измерения продолжаются"
            )
            return
        self.archive_export_var.set(
            f"NDJSON сохранён: {result.get('sweeps', 0)} sweeps, "
            f"{result.get('observations', 0)} observations · "
            f"SHA-256 {result.get('sha256')}"
        )

    def _apply_analytics_result(
        self, result: tuple[Optional[dict[str, Any]], Optional[str]]
    ) -> None:
        summary, error = result
        self.analytics_var.set(analytics_text(summary, error))
