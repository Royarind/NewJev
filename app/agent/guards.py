"""Pure safety / progress heuristics used by the agent loop (kept free of I/O so they are unit-testable)."""

from __future__ import annotations

import re
from typing import Any

NON_PROGRESS_OPS = {"SCROLL_UP", "SCROLL_DOWN", "WAIT"}
RISKY_OPS = {"CLICK", "DOUBLE_CLICK", "PRESS_KEY", "SELECT"}


def parse_keywords(raw: str) -> list[str]:
    return [k.strip().lower() for k in (raw or "").split(",") if k.strip()]


_MESSAGE_FIELD = re.compile(r"\b(message|chat|reply|comment|post|tweet|compose|write to)\b", re.I)


def is_risky(operation: str, label: str, keywords: list[str]) -> str | None:
    """Return the matched keyword if executing this would be hard to undo (send, delete, pay ...)."""
    if operation == "TYPE_SUBMIT":   # Enter in a chat / message box sends it
        return "send (Enter in a message box)" if _MESSAGE_FIELD.search(label or "") else None
    if operation not in RISKY_OPS or not label:
        return None
    text = label.lower()
    if operation == "PRESS_KEY" and "send" not in text and "submit" not in text:
        return None
    for kw in keywords:
        if re.search(rf"(?<![a-z]){re.escape(kw)}(?![a-z])", text):
            return kw
    return None


_DELETE_LABEL = re.compile(r"\b(delete|move to recycle bin|send to recycle bin|permanently delete|remove file)\b", re.I)


def is_delete(operation: str, label: str, page: dict[str, Any] | None = None) -> bool:
    """Would this action delete files/items? (context-menu 'Delete', Delete key in File Explorer, Shift+Delete …)"""
    label = label or ""
    url = str((page or {}).get("url", "")).lower()
    in_explorer = url.startswith("desktop://explorer.exe")
    if operation == "PRESS_KEY":
        lab = label.lower()
        return lab.startswith("shift+delete") or (lab.startswith("delete") and in_explorer)
    if operation in ("CLICK", "DOUBLE_CLICK"):
        return bool(_DELETE_LABEL.search(label))
    return False


def _sig(h: dict[str, Any]) -> tuple:
    return (h.get("operation"), h.get("label"), h.get("text"))


SAFE_OPS = {"SWITCH_WINDOW", "SWITCH_TAB", "SCROLL_UP", "SCROLL_DOWN", "WAIT", "GO_BACK"}


def signatures(history: list[dict[str, Any]], n: int = 4) -> set[tuple]:
    """Signatures of the last n real actions (used to spot the agent falling back into a loop it was warned about)."""
    return {_sig(h) for h in [h for h in history if h.get("operation") not in (None, "NOTE")][-n:]}


def detect_stuck(history: list[dict[str, Any]], repeat_limit: int = 3, threshold: float = 0.6) -> str | None:
    """
    Return a reason string when recent history shows the agent is not making progress:
      - the same (non-scroll) action `repeat_limit` times in a row, even if the page "changed"
      - A-B-A-B oscillation between two actions
      - 4 consecutive steps that did not change the observable state
      - 8 consecutive scroll/wait steps
      - very low average confidence over the last 5 steps
    Notes (history entries with operation 'NOTE') are ignored.
    """
    steps = [h for h in history if h.get("operation") not in (None, "NOTE")]
    if not steps:
        return None

    tail = steps[-repeat_limit:]
    if (len(tail) == repeat_limit and len({_sig(h) for h in tail}) == 1
            and tail[0].get("operation") not in NON_PROGRESS_OPS):
        return f"repeated '{tail[0].get('operation')} {tail[0].get('label', '')}' {repeat_limit} times"

    last4 = steps[-4:]
    if len(last4) == 4:
        a, b, c, d = (_sig(h) for h in last4)
        if a == c and b == d and a != b:
            return f"oscillating between '{last4[0].get('label', '')}' and '{last4[1].get('label', '')}'"
        if all(h.get("page_changed") is False for h in last4):
            return "4 steps in a row without any visible change"

    last8 = steps[-8:]
    if len(last8) == 8 and all(h.get("operation") in NON_PROGRESS_OPS for h in last8):
        return "8 scroll/wait steps in a row"

    last5 = [h.get("confidence") for h in steps[-5:] if isinstance(h.get("confidence"), (int, float))]
    no_progress = sum(1 for h in steps[-5:] if h.get("page_changed") is False)
    # low confidence alone is normal in busy UIs (Office ribbons); only count it when progress also stalls
    if len(last5) == 5 and sum(last5) / 5 < threshold * 0.8 and no_progress >= 3:
        return f"average confidence {sum(last5) / 5:.0%} over the last 5 steps"
    return None


def needs_handoff(page: dict[str, Any]) -> str | None:
    """Explain why a human must take over (login / 2FA / CAPTCHA), or None."""
    s = page.get("sensitive") or {}
    if s.get("captcha"):
        return "A CAPTCHA / human verification is shown."
    if s.get("otp"):
        return "A verification (2FA) code is requested."
    if s.get("password"):
        return "A password is required to sign in."
    return None
