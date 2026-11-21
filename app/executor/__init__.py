"""Capture/executor factory and a manager that keeps one browser and one desktop session alive."""

from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings

logger = logging.getLogger(__name__)


def _new_capture(mode: str):
    if mode == "desktop":
        from ..capture.desktop import DesktopCapture
        return DesktopCapture()
    from ..capture.browser import BrowserCapture
    return BrowserCapture()


def get_capture(mode: str | None = None):
    """Return a fresh capture/executor instance ('auto' resolves to browser)."""
    m = mode or get_settings().capture_mode
    return _new_capture("desktop" if m == "desktop" else "browser")


class CaptureManager:
    """
    Lazily creates and reuses captures per mode so a single plan can move between the
    automated browser and desktop apps (e.g. copy from a web page, paste into Notepad).
    """

    def __init__(self) -> None:
        self._captures: dict[str, Any] = {}

    def get(self, mode: str | None, url: str | None = None):
        m = "desktop" if mode == "desktop" else "browser"
        cap = self._captures.get(m)
        if cap is not None and not getattr(cap, "is_alive", True):
            try:
                cap.close()
            except Exception:
                pass
            cap = None
        if cap is None:
            cap = _new_capture(m)
            cap.start(url=url)
            self._captures[m] = cap
        elif url:
            try:
                cap.navigate(url)
            except Exception as exc:
                logger.warning("Navigation to '%s' failed: %s", url, exc)
        return cap

    def peek(self, mode: str):
        return self._captures.get(mode)

    @property
    def is_alive(self) -> bool:
        return any(getattr(c, "is_alive", False) for c in self._captures.values())

    def close(self) -> None:
        for cap in self._captures.values():
            try:
                cap.close()
            except Exception as exc:
                logger.warning("Error closing capture: %s", exc)
        self._captures.clear()


__all__ = ["get_capture", "CaptureManager"]
