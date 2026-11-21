"""Human-in-the-loop hooks: confirmations (risky / low-confidence steps) and hand-offs (login, 2FA, CAPTCHA)."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Callable

logger = logging.getLogger(__name__)


class Reviewer:
    """Base reviewer. confirm() -> approve?  handoff() -> user finished the manual step?"""

    interactive = False

    def confirm(self, kind: str, message: str, details: dict[str, Any] | None = None) -> bool:
        raise NotImplementedError

    def handoff(self, message: str, details: dict[str, Any] | None = None) -> bool:
        raise NotImplementedError

    def hint(self, message: str, details: dict[str, Any] | None = None) -> str | None:
        """The agent is stuck: ask the user what to do. Returns the hint text, or None."""
        return None

    def takeover(self, message: str, details: dict[str, Any] | None = None) -> bool:
        """The person took over the mouse/keyboard: wait until they say Resume (True) or Stop (False)."""
        return self.handoff(message, details)


# Confirmations that "allow all" / --yes never cover
ALWAYS_ASK = {"delete", "send_email", "send_message", "send_invite", "upload"}


class AutoReviewer(Reviewer):
    """Unattended runs (benchmarks): approve confirmations, never wait for a human."""

    def __init__(self, approve: bool = True, allow_delete_prefix: str | None = None) -> None:
        self._approve = approve
        self._delete_prefix = allow_delete_prefix

    def confirm(self, kind: str, message: str, details: dict[str, Any] | None = None) -> bool:
        if kind == "delete":  # unattended runs never delete, except benchmark files with a known prefix
            from pathlib import Path
            path = (details or {}).get("path")
            ok = bool(self._delete_prefix and path and Path(path).name.startswith(self._delete_prefix))
            logger.info("[auto-%s] %s", "approve" if ok else "reject", message)
            return ok
        if kind in ALWAYS_ASK and not self._approve:
            logger.info("[auto-reject] %s", message)
            return False
        logger.info("[auto-%s] %s", "approve" if self._approve else "reject", message)
        return self._approve

    def handoff(self, message: str, details: dict[str, Any] | None = None) -> bool:
        logger.info("[auto] hand-off needed but no human available: %s", message)
        return False


class ConsoleReviewer(Reviewer):
    """Terminal prompts for `run.py --cli` / `--task`."""

    interactive = True

    def __init__(self, auto_approve: bool = False) -> None:
        self._auto = auto_approve

    def confirm(self, kind: str, message: str, details: dict[str, Any] | None = None) -> bool:
        if self._auto and kind not in ALWAYS_ASK:  # --yes never covers deletions or sending mail
            return True
        try:
            return input(f"\n[{kind}] {message}\nProceed? [y/N] ").strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def handoff(self, message: str, details: dict[str, Any] | None = None) -> bool:
        try:
            ans = input(f"\n[YOUR TURN] {message}\nComplete it in the window, then press Enter (or type 'abort'): ")
            return ans.strip().lower() != "abort"
        except (EOFError, KeyboardInterrupt):
            return False

    def takeover(self, message: str, details: dict[str, Any] | None = None) -> bool:
        try:
            return input(f"\n[PAUSED] {message}\nPress Enter to resume (or type 'stop'): ").strip().lower() != "stop"
        except (EOFError, KeyboardInterrupt):
            return False

    def hint(self, message: str, details: dict[str, Any] | None = None) -> str | None:
        try:
            ans = input(f"\n[STUCK] {message}\nHint (empty = give up): ").strip()
            return ans or None
        except (EOFError, KeyboardInterrupt):
            return None


class EventReviewer(Reviewer):
    """
    Reviewer for the web UI: emits a request event and blocks the worker thread until
    respond() is called from the WebSocket handler (or timeout / stop).
    """

    interactive = True

    def __init__(self, emit: Callable[[str, Any], None], timeout_s: float = 600,
                 should_stop: Callable[[], bool] | None = None) -> None:
        self._emit = emit
        self._timeout = timeout_s
        self._should_stop = should_stop or (lambda: False)
        self._pending: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._always_allow: set[str] = set()   # kinds the user said "allow all" for, this run

    def _ask(self, event: str, payload: dict[str, Any], slot_out: dict | None = None) -> bool:
        rid = uuid.uuid4().hex[:10]
        slot = slot_out if slot_out is not None else {}
        slot.update({"event": threading.Event(), "answer": False, "kind": payload.get("kind"), "text": None})
        with self._lock:
            self._pending[rid] = slot
        self._emit(event, {**payload, "request_id": rid, "timeout_s": self._timeout})
        deadline = time.time() + self._timeout
        try:
            while time.time() < deadline:
                if slot["event"].wait(0.5):
                    return bool(slot["answer"])
                if self._should_stop():
                    return False
            return False
        finally:
            with self._lock:
                self._pending.pop(rid, None)
            self._emit("request_closed", {"request_id": rid})

    def respond(self, request_id: str, approved: bool, remember: bool = False, text: str | None = None) -> bool:
        with self._lock:
            slot = self._pending.get(request_id)
        if not slot:
            return False
        if approved and remember and slot.get("kind") and slot["kind"] not in ALWAYS_ASK:
            self._always_allow.add(slot["kind"])
        slot["text"] = (text or "").strip() or None
        slot["answer"] = approved
        slot["event"].set()
        return True

    def is_pending(self, request_id: str) -> bool:
        with self._lock:
            return request_id in self._pending

    def cancel_all(self) -> None:
        with self._lock:
            for slot in self._pending.values():
                slot["answer"] = False
                slot["event"].set()

    def confirm(self, kind: str, message: str, details: dict[str, Any] | None = None) -> bool:
        if kind in self._always_allow:
            return True
        return self._ask("confirm_request", {"kind": kind, "message": message, "details": details or {}})

    def handoff(self, message: str, details: dict[str, Any] | None = None) -> bool:
        return self._ask("handoff", {"message": message, "details": details or {}})

    def takeover(self, message: str, details: dict[str, Any] | None = None) -> bool:
        return self._ask("takeover_request", {"kind": "takeover", "message": message, "details": details or {}})

    def hint(self, message: str, details: dict[str, Any] | None = None) -> str | None:
        slot: dict[str, Any] = {}
        ok = self._ask("rescue_request", {"kind": "hint", "message": message, "details": details or {}}, slot)
        return slot.get("text") if ok else None
