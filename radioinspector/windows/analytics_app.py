"""On-demand сводка всего архива для Windows GUI."""

from __future__ import annotations

from typing import Any, Optional

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
        self.analytics_button = self.tk.Button(
            self.root,
            text="Запросить сводку всего архива",
            command=self.refresh_analytics,
        )
        self.analytics_button.pack(side="top", fill="x", padx=10, pady=(0, 8))

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

    def _apply_analytics_result(
        self, result: tuple[Optional[dict[str, Any]], Optional[str]]
    ) -> None:
        summary, error = result
        self.analytics_var.set(analytics_text(summary, error))
