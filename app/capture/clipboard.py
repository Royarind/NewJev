"""Read the OS clipboard so Jev can see whether a copy worked (text only, truncated)."""

from __future__ import annotations


def read_clipboard(limit: int = 300) -> str | None:
    try:
        import pyperclip
        value = pyperclip.paste()
    except Exception:
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else ""
