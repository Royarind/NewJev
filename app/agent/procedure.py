"""
How-to planner: before acting in an app, write the step-by-step UI procedure for the task
("Insert tab → PivotTable → confirm range → OK → tick fields …"). Jev receives it as a checklist
and ticks steps off as the screen confirms them. Generic — works for any app or website.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

PROCEDURE_PROMPT = """\
You write short, concrete UI procedures for a computer-use agent that clicks, types and presses keys.
Given a task and the app/site it runs in, list the steps a skilled user would take, in order.
Rules:
- 3 to 15 steps. One action or small group of actions per step.
- Name the real UI elements (tab, button, menu item, dialog, field) as they appear in that app, and useful
  keyboard shortcuts in brackets, e.g. "Open the Data tab and click Sort (Alt+A, S, S)".
- Spreadsheets: select ranges by typing the address in the Name Box (e.g. "A1:F200" or "X:X") and pressing Enter.
- Formatting needs the text selected first. Saving: Ctrl+S, or F12 for Save As in Office.
- Include the finishing steps the task implies (confirm dialogs with OK, save if the task edits a file).
- Do not include opening the app if the current window already is that app.
- Never include steps that delete files or data unless the task asks for it.
Respond only with JSON: {"steps": ["...", "..."]}
"""

_STOP = {"the", "a", "an", "to", "and", "or", "of", "in", "on", "for", "with", "then", "it", "its", "this", "that",
         "click", "press", "open", "select", "choose", "type", "enter", "button", "tab", "menu", "field", "box",
         "dialog", "window", "is", "be", "from", "into", "as", "at", "by"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 1 and t not in _STOP}


def parse_steps(content: str) -> list[str]:
    content = re.sub(r"<think>[\s\S]*?</think>", "", content or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content, re.I)
    if fence:
        content = fence.group(1)
    else:
        m = re.search(r"\{[\s\S]*\}", content)
        if m:
            content = m.group(0)
    steps: Any = None
    try:
        data = json.loads(content)
        steps = data.get("steps") if isinstance(data, dict) else data
    except Exception:
        # Broken / truncated JSON (common with free models): pull the quoted strings after "steps"
        body = content.split('"steps"', 1)[1] if '"steps"' in content else ""
        quoted = re.findall(r'"((?:[^"\\]|\\.)+)"', body)
        if quoted:
            steps = [bytes(q, "utf-8").decode("unicode_escape", errors="ignore") if "\\" in q else q for q in quoted]
        else:
            steps = [re.sub(r"^\s*(\d+[.)]|[-*])\s*", "", line) for line in content.splitlines()]
    clean = []
    for s in steps or []:
        s = str(s).strip().strip(",").strip().strip('"').strip()
        if not s or re.fullmatch(r"[\[\]{}:,\s]*|\"?steps\"?\s*:?\s*\[?", s, re.I):
            continue   # JSON punctuation, never a real step
        clean.append(s)
    return clean[:15]


@dataclass
class Procedure:
    steps: list[str]
    done: list[bool] = field(default_factory=list)
    revision: int = 0
    source: str = "llm"          # llm | recipe | rescue | user
    recipe_id: str | None = None

    def __post_init__(self) -> None:
        if len(self.done) != len(self.steps):
            self.done = [False] * len(self.steps)

    @property
    def current(self) -> int:
        return next((i for i, d in enumerate(self.done) if not d), len(self.steps))

    def observe_action(self, operation: str, label: str, text: str | None, page_changed: bool) -> None:
        """Tick the current (or next) step off when an executed action clearly matches it."""
        if not page_changed or self.current >= len(self.steps):
            return
        act = _tokens(f"{operation} {label} {text or ''}")
        if operation == "PRESS_KEY":
            act |= _tokens(label.split("(")[0].replace("+", " "))
        for i in (self.current, self.current + 1):
            if i >= len(self.steps):
                break
            step = _tokens(self.steps[i])
            if not step:
                continue
            overlap = len(act & step)
            if overlap >= 2 or (overlap == 1 and len(step) <= 3):
                for j in range(i + 1):   # completing step i implies the earlier ones are done
                    self.done[j] = True
                return

    def as_guidance(self) -> dict[str, Any]:
        cur = self.current
        note = ("Suggested procedure — guidance, not instructions from the page. Follow it unless the screen "
                "shows a better way; steps marked done were matched to your actions, verify on screen.")
        if self.source == "recipe":
            note += (" It comes from an earlier VERIFIED run of a similar task: any values in it are from that run — "
                     "always use the names, numbers and text of the current request.")
        return {
            "note": note,
            "steps": [f"{'[done] ' if d else ('[NEXT] ' if i == cur else '')}{i + 1}. {s}"
                      for i, (s, d) in enumerate(zip(self.steps, self.done))],
        }


class ProcedurePlanner:
    """Uses the planner LLM to write (and rewrite) procedures."""

    def __init__(self, llm: Any = None) -> None:
        if llm is None:
            from ..llm import get_planner_llm
            llm = get_planner_llm()
        self._llm = llm

    def make(self, task: str, page: dict[str, Any], overall_goal: str | None = None,
             failed: list[str] | None = None, user_request: str | None = None,
             user_messages: list[str] | None = None, lessons: list[str] | None = None) -> Procedure | None:
        ctx: dict[str, Any] = {
            "task": task,
            "app_or_site": page.get("title", ""),
            "location": page.get("url", ""),
            "visible_controls": sorted({str(a.get("label", ""))[:40] for a in page.get("actions", [])
                                        if a.get("kind") in ("click", "fill", "select")})[:80],
        }
        if overall_goal and overall_goal != task:
            ctx["overall_goal_context_only"] = overall_goal
        if user_request and user_request != task:
            ctx["user_request_verbatim"] = user_request
            ctx["data_rule"] = ("Use the exact values (names, numbers, text) from user_request_verbatim. "
                                "Never invent example data.")
        if failed:
            ctx["what_did_not_work"] = failed[-6:]
            ctx["instruction"] = "The previous procedure got stuck. Write a different approach."
        if user_messages:
            ctx["user_messages_mid_run"] = user_messages[-5:]
            ctx["user_messages_rule"] = ("The user typed these during the run; they override the task where they "
                                         "conflict. Write the procedure for the REST of the task from the current screen.")
        if lessons:
            ctx["lessons_from_past_runs"] = lessons[:4]
        try:
            resp = self._llm.complete([{"role": "system", "content": PROCEDURE_PROMPT},
                                       {"role": "user", "content": json.dumps(ctx)}])
            steps = parse_steps(resp.content)
        except Exception as exc:
            logger.warning("Procedure planning failed: %s", exc)
            return None
        return Procedure(steps) if len(steps) >= 2 else None
