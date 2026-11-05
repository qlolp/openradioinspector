"""Тонкий Windows GUI-клиент общего API OpenRadioInspector."""

from .client import PlatformClient, PlatformClientError, PlatformSnapshot

__all__ = ["PlatformClient", "PlatformClientError", "PlatformSnapshot"]
