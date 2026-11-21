"""Verifier — uses Jev Noul to independently check step and goal completion."""

from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings
from ..jev import JevClient

logger = logging.getLogger(__name__)


def summarize_state(page: dict[str, Any], limit: int = 3500) -> str:
    """Compact, verifier-friendly view of a page: title, URL, field values, visible text."""
    lines = [f"Title: {page.get('title', '')}", f"Location: {page.get('url', '')}"]
    fields = []
    for a in page.get("actions", []):
        if a.get("kind") == "fill" and a.get("value"):
            fields.append(f"- {a.get('label', '')}: {str(a['value'])[:300]}")
        elif a.get("kind") == "click" and a.get("checked") in ("true", True):
            fields.append(f"- [checked] {a.get('label', '')}")
        elif a.get("kind") == "select" and a.get("current_value"):
            fields.append(f"- {str(a.get('label', '')).split(' → ')[0]}: {a['current_value']}")
    seen: set[str] = set()
    fields = [f for f in fields if not (f in seen or seen.add(f))]
    if fields:
        lines.append("Field values:\n" + "\n".join(fields[:40]))
    if page.get("clipboard"):
        lines.append(f"Clipboard: {str(page['clipboard'])[:200]}")
    lines.append("Visible text:\n" + str(page.get("text", ""))[:limit])
    return "\n".join(lines)


def format_actions(history: list[dict[str, Any]], limit: int = 25) -> str:
    rows = []
    for h in history:
        op = h.get("operation")
        if op in (None, "NOTE"):
            continue
        row = f"{len(rows) + 1}. {op} '{str(h.get('label', ''))[:90]}'"
        if h.get("text"):
            row += f" with '{str(h['text'])[:80]}'"
        if h.get("failed"):
            row += f" FAILED: {str(h['failed'])[:120]}"
        if h.get("result"):
            row += f" -> {str(h['result'])[:160]}"
        if h.get("page_changed") is False:
            row += " (no visible change)"
        rows.append(row)
    return "\n".join(rows[-limit:]) or "(none)"


class Verifier:
    """Uses Jev's Noul primitive to verify whether a goal has been achieved."""

    def __init__(self, jev: JevClient) -> None:
        self._jev = jev
        self._threshold = get_settings().done_verify_threshold

    def verify(self, goal: str, page: dict[str, Any] | str,
               history: list[dict[str, Any]] | None = None) -> tuple[bool | None, float]:
        """
        Is the goal achieved? Judged from the actions taken (evidence for earlier requirements, e.g. a
        filter or sort that is no longer visible) plus the current state (evidence for the end result).
        Returns (achieved, probability). achieved is None when verification itself failed.
        """
        summary = page if isinstance(page, str) else summarize_state(page, 5000)
        steps = format_actions(history or [])
        state_text = f"Goal: {goal}\n\nActions taken so far:\n{steps}\n\nCurrent state:\n{summary}"
        question = ("Considering the actions taken and the current state together, has the goal been accomplished? "
                    "Requirements satisfied by earlier actions (e.g. a chosen date, filter or sort) count even if they "
                    "are no longer visible, as long as the current state is consistent with them. "
                    "Answer no if a required step was never done, failed, or the end result is not reached "
                    "(e.g. not sent, not saved, not selected). "
                    "Results after '->' come from direct operations the system executed and checked (file changes "
                    "saved on disk, emails sent, data written): they are proof even if the screen does not show "
                    "them. Opening a file in an app is only a means: if the requested change was made and saved "
                    "directly, the goal is accomplished.")
        try:
            prob, latency = self._jev.noul(state_text, question)
            logger.debug("Verifier: prob=%.3f latency=%dms", prob, latency)
            return prob >= self._threshold, prob
        except Exception as exc:
            logger.warning("Verifier failed: %s", exc)
            return None, 0.0

    def verify_criteria(
        self,
        criteria: list[str],
        page: dict[str, Any] | str,
        history: list[dict[str, Any]] | None = None,
        final_report: str | None = None,
    ) -> tuple[bool, float, dict[str, Any]]:
        """
        Evaluate concrete success criteria directly against system state, file system, and history.
        Returns (all_passed, confidence, details).
        """
        if not criteria:
            return True, 1.0, {"criteria": {}, "explanation": "No specific criteria defined."}

        results: dict[str, bool] = {}
        hist = history or []
        page_dict = page if isinstance(page, dict) else {}
        page_text = str(page_dict.get("text", "")) if isinstance(page, dict) else str(page)

        for c in criteria:
            c_str = c.strip()
            if not c_str:
                continue

            # 1. file_exists: <path>
            if c_str.startswith("file_exists:"):
                raw_path = c_str[len("file_exists:"):].strip().strip("\"'")
                try:
                    from ..shortcuts.fileops import _expand
                    p = _expand(raw_path)
                    passed = p.exists()
                except Exception:
                    passed = False
                results[c_str] = passed

            # 2. file_contains: <path> | <text>
            elif c_str.startswith("file_contains:"):
                part = c_str[len("file_contains:"):].strip()
                p_str, _, exp_text = part.partition("|")
                try:
                    from ..shortcuts.fileops import _expand
                    p = _expand(p_str.strip().strip("\"'"))
                    passed = p.exists() and exp_text.strip().lower() in p.read_text(encoding="utf-8", errors="replace").lower()
                except Exception:
                    passed = False
                results[c_str] = passed

            # 3. answer_reported
            elif c_str in ("answer_reported", "reported_answer"):
                passed = bool(final_report and str(final_report).strip()) or any(
                    h.get("operation") == "REPORT_ANSWER" for h in hist
                )
                results[c_str] = passed

            # 4. email_sent: <recipient>
            elif c_str.startswith("email_sent:"):
                rcpt = c_str[len("email_sent:"):].strip().lower()
                passed = any(
                    h.get("operation") == "SEND_EMAIL" and (not rcpt or rcpt in str(h.get("text", "")).lower())
                    for h in hist
                )
                results[c_str] = passed

            # 5. message_sent: <channel_or_user>
            elif c_str.startswith("message_sent:"):
                dest = c_str[len("message_sent:"):].strip().lower()
                passed = any(
                    h.get("operation") == "SEND_MESSAGE" and (not dest or dest in str(h.get("text", "")).lower())
                    for h in hist
                )
                results[c_str] = passed

            # 6. text_contains: <string>
            elif c_str.startswith("text_contains:"):
                exp = c_str[len("text_contains:"):].strip().lower()
                passed = exp in page_text.lower()
                results[c_str] = passed

            # 7. Generic check
            else:
                results[c_str] = True

        all_passed = all(results.values()) if results else True
        score = sum(1.0 for v in results.values() if v) / max(1, len(results))
        failed_items = [k for k, v in results.items() if not v]
        explanation = (
            "All explicit success criteria verified."
            if all_passed
            else f"Failed criteria: {', '.join(failed_items)}"
        )
        return all_passed, score, {"criteria": results, "explanation": explanation}

    def verify_step(self, action_desc: str, expected: str, after_state: str) -> tuple[bool | None, float, str]:
        """Did a specific step visibly succeed? (informational only)"""
        state_text = f"Action: {action_desc}\nIntended to advance: {expected}\nObserved state:\n{after_state[:2000]}"
        question = "Did the executed action succeed and visibly change the state as intended?"
        try:
            prob, latency = self._jev.noul(state_text, question)
            return prob >= 0.6, prob, f"Step check {round(prob * 100)}% ({latency}ms)"
        except Exception as exc:
            return None, 0.0, f"Step check unavailable: {exc}"
