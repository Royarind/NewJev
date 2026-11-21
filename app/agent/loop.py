# -*- coding: utf-8 -*-
"""Main agent loop: observe -> Jev decides -> (LLM fills text) -> execute -> check progress, until verified DONE."""

from __future__ import annotations

import datetime
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import get_settings
from ..jev import JevClient
from ..jev.client import TERMINAL_OPS, JevDecision
from ..llm import get_text_llm
from ..llm import usage as usage_mod
from ..shortcuts import SHORTCUT_OPS
from .guards import SAFE_OPS, detect_stuck, is_delete, is_risky, needs_handoff, parse_keywords, signatures
from .recipes import app_key as recipe_app_key, steps_from_history
from .rescue import ops_for
from . import takeover
from .review import Reviewer
from .verifier import Verifier, summarize_state
from .vision import element_count, locate, parse_target

logger = logging.getLogger(__name__)

VALUE_PROMPTS = {
    "field": """\
Return a JSON object with exactly one key, text: the exact string to enter in the selected field.
Infer the value from the user's goal and the meaning of the field.
- If the goal specifies the text to write/type, return exactly that text.
- For search inputs, return the search query from the goal.
- For document editors / text areas, return the body text the goal asks for.
- If mode is "append", return only the text to ADD (the field already contains current_value).
- For the file-name box of a Save / Open dialog, when the goal names a folder (Desktop, Documents, Downloads ...),
  return the FULL path using known_folders, e.g. "C:\\Users\\me\\Desktop\\report.txt" -- the dialog accepts it.
- Never invent passwords, codes or personal data that are not in the goal.
Respond only with JSON: {"text": "..."}""",
    "url": """\
Return a JSON object with exactly one key, text: the full URL (https://...) the browser should open next
to make progress on the user's goal. Prefer the official site. Respond only with JSON: {"text": "https://..."}""",
    "app": """\
Return a JSON object with exactly one key, text: the Windows application that must be launched next for the
user's goal, as a short common name (e.g. "notepad", "calculator", "word", "excel", "explorer downloads", "paint").
Never return the application that is already the active_window -- it is open already.
To open an EXISTING file, return "<app> <full path>" using known_folders, e.g. "notepad C:\\Users\\me\\Desktop\\notes.txt".
Respond only with JSON: {"text": "..."}""",
    "file": """\
Return a JSON object with exactly one key, text: the file (or folder) the goal refers to, as its name or full path
(e.g. "NewJev_test1.txt", "C:\\Users\\me\\Desktop\\report.docx", "*.csv"). If the goal says which app must open
it, append " | <app>" (e.g. "notes.txt | notepad"). Use known_folders for folder names. Respond only with JSON.""",
    "copy": """\
Return a JSON object with exactly one key, text: the exact piece of text the goal wants copied, taken VERBATIM
from page.full_text (same characters, numbers and units as shown there -- do not round, reformat or explain).
Copy only the value asked for (e.g. "8,848.86 m" for a height, or one sentence), not the whole line.
Respond only with JSON: {"text": "..."}""",
    "drop": """\
Return a JSON object with exactly one key, text: the label of the element where the dragged item must be dropped,
copied exactly from drop_targets. Respond only with JSON: {"text": "..."}""",
    "table": """\
Return a JSON object with exactly one key, text: the block of values to enter into the spreadsheet.
Format: first line = start cell address (e.g. A1), then one line per row, cells separated by a TAB character.
Use \\n for new lines and \\t for tabs in the JSON string. Copy every value EXACTLY from user_request_verbatim
(headers and all rows, in order). Never invent or summarise data.
Example: {"text": "A1\\nRegion\\tProduct\\tUnits\\nWest\\tPens\\t10\\nEast\\tPads\\t4"}""",
    "range": """\
Return a JSON object with exactly one key, text: the spreadsheet address to select for the current step, e.g.
"A1:F200", "C:C" (whole column), "3:3" (row), "Sheet2!B2:B50". Use the column headers / data visible on screen.
Respond only with JSON: {"text": "..."}""",
    "office": """\
Write Python for Office COM automation that performs ONLY the current step of the goal on the document that is open.
Available objects -- Excel: app, wb (active workbook), ws (active sheet), sel (selection), xl (constants such as
xl.xlAscending, xl.xlDescending, xl.xlYes, xl.xlDatabase, xl.xlRowField, xl.xlColumnField, xl.xlDataField, xl.xlSum,
xl.xlCount, xl.xlAverage, xl.xlUp). Word: app, doc (active document), sel (selection), wd (constants such as
wd.wdStyleHeading1, wd.wdReplaceAll, wd.wdFindContinue, wd.wdStory, wd.wdHeaderFooterPrimary).
Use office_context (sheet names, used range, header row) for real addresses and names.
Rules: no imports, no while loops, no def/class/try/with, no files, no saving/closing/opening, no macros.
Set result = "<one-line summary of what changed>".
Examples:
- Excel sort by column C descending with headers: ws.UsedRange.Sort(Key1=ws.Range("C1"), Order1=xl.xlDescending, Header=xl.xlYes); result = "sorted by C desc"
- Excel pivot: src = ws.UsedRange
  pc = wb.PivotCaches().Create(SourceType=xl.xlDatabase, SourceData=src)
  sh = wb.Sheets.Add()
  pt = pc.CreatePivotTable(TableDestination=sh.Range("A3"), TableName="Pivot1")
  pt.PivotFields("Region").Orientation = xl.xlRowField
  pt.AddDataField(pt.PivotFields("Sales"), "Sum of Sales", xl.xlSum); result = "pivot on new sheet"
- Word replace all: f = doc.Content.Find; f.Execute(FindText="draft", ReplaceWith="final", Replace=wd.wdReplaceAll); result = "replaced"
Respond only with JSON: {"text": "<python code, use \\n for new lines>"}""",
    "search": """\
Return a JSON object with exactly one key, text: what to type into Windows Search to find the file or app the goal
needs -- usually just the file name (e.g. "NewJev_test1.txt") or app name. Respond only with JSON.""",
    "saveas": """\
Return a JSON object with exactly one key, text: the FULL path where the open document must be saved, built from the
folder and file name in the goal and known_folders (e.g. "C:\\Users\\me\\Documents\\report.docx"). If the goal gives
no extension, add the one the active app uses (.txt Notepad, .docx Word, .xlsx Excel, .pptx PowerPoint, .png Paint).
If the goal names no folder, use Documents. Respond only with JSON: {"text": "..."}""",
    "visual": """\
Return a JSON object with exactly one key, text: a short visual description of the ONE thing to click next on the
screen for the goal, so it can be found on a screenshot (e.g. "the green Play button at the bottom", "the text box
labelled Search"). Add " (double-click)" or " (right-click)" if needed. Respond only with JSON: {"text": "..."}""",
    "email": """\
Return ONLY a JSON object describing the email to send for the goal:
{"to": ["a@b.com"], "cc": [], "subject": "...", "body": "...", "attachments": ["full path, only if the goal asks"]}
Take recipients, subject and text exactly from the goal / user_request_verbatim; never invent addresses.
If a recipient address is not given, return {"error": "missing recipient"}.""",
    "mail_query": """\
Return ONLY a JSON object describing which emails to look for:
{"folder": "inbox", "query": "words that must appear (sender, subject words)", "unread_only": false, "limit": 10}""",
    "message": """\
Return ONLY a JSON object for the chat message to send:
{"via": "slack|teams", "to": "#channel, person name or email (DM / chat)", "team": "Teams team (channel posts only)",
 "channel": "Teams channel (channel posts only)", "text": "the message"}
Take the recipient and the text exactly from the goal / user_request_verbatim; never invent recipients.""",
    "message_query": """\
Return ONLY a JSON object: {"via": "slack|teams", "to": "#channel or person", "team": "", "channel": "", "limit": 10}""",
    "calendar": """\
Return ONLY a JSON object for the calendar step. Use now (given) to turn words like "tomorrow 3pm" into dates.
Read: {"action": "read", "start": "YYYY-MM-DD HH:MM", "end": "YYYY-MM-DD HH:MM"}
Create: {"action": "create", "subject": "...", "start": "YYYY-MM-DD HH:MM", "duration_min": 30,
         "attendees": ["email", ...], "location": "", "body": "", "online": false}
attendees only if the goal names people; take every value from the goal.""",
    "cloud": """\
Return ONLY a JSON object: {"action": "search|download|upload", "query": "words or file name",
 "name": "exact file name to download", "src": "local file to upload", "folder": "OneDrive folder or local folder"}""",
    "pdf": """\
Return ONLY a JSON object: {"action": "read|info|fields|fill", "src": "PDF path or name", "pages": "1-3",
 "values": {"field name": "value"}, "dst": "optional new file"}
For fill, use the exact field names (run fields first if they are not known) and values from the goal.""",
    "data": """\
Return ONLY a JSON object for the CSV / Excel file step:
{"action": "read|summary|sort|filter|aggregate|append_rows|write|convert|list_sheets", "src": "file path or name",
 "dst": "new file (optional)", "sheet": "optional", "column": "column name or letter", "descending": false,
 "where": [{"column": "...", "op": "=|!=|>|<|>=|<=|contains", "value": "..."}],
 "func": "sum|avg|min|max|count", "group_by": "column", "rows": [["header", "..."], ["value", "..."]]}
Use the column names / values exactly as in the goal.""",
    "fileop": """\
Return ONLY a JSON object with the file operation for the current step:
{"action": "copy|move|rename|mkdir|write|append|replace|read|list|exists", "src": "path or name",
 "dst": "path or new name", "content": "text for write/append", "find": "text to find (replace)",
 "replace_with": "new text (replace)"}
Use full paths built from known_folders (e.g. "C:\\Users\\me\\Desktop\\report.txt"). rename: dst is the new name.
Take names and text exactly from the goal / user_request_verbatim.
Choose the action that makes PROGRESS: look at recent_actions results first -- never read a file again whose content
is already there. To change text inside a file use replace (it saves the file at once). replace / write / append
save immediately: there is no separate save step, and the file does not need to be open in an app.""",
    "report": (
        "Return a JSON object with exactly one key, text: a clear, concise markdown report answering the user's goal "
        "based on the information visible on the page (e.g. flight details, product prices, search results, summaries). "
        "Extract the exact details requested (airlines, departure/arrival times, durations, stops, prices, figures, names). "
        "Never invent data. Format with bullet points or a markdown table when listing items. "
        'Respond only with JSON: {"text": "..."}'
    ),
}
JSON_KINDS = {"email", "mail_query", "fileop", "message", "message_query", "calendar", "cloud", "pdf", "data"}
VALUE_OPS = {"TYPE_TEXT": "field", "APPEND_TEXT": "field", "TYPE_FOCUSED": "field", "TYPE_SUBMIT": "field",
             "SAVE_AS": "saveas", "OPEN_URL": "url",
             "LAUNCH_APP": "app", "OPEN_FILE": "file", "UPLOAD_FILE": "file", "DELETE_FILE": "file", "SEARCH_FILES": "search",
             "COPY_TEXT": "copy", "REPORT_ANSWER": "report", "DRAG": "drop", "SELECT_RANGE": "range", "OFFICE_ACTION": "office",
             "TYPE_TABLE": "table", "VISUAL_CLICK": "visual", "SEND_EMAIL": "email", "READ_EMAIL": "mail_query",
             "FILE_OP": "fileop", "SEND_MESSAGE": "message", "READ_MESSAGES": "message_query",
             "CALENDAR": "calendar", "CLOUD_FILES": "cloud", "PDF_OP": "pdf", "DATA_OP": "data"}

WRAP_UP_GOAL = (
    "Finish cleanly after this task, which is ALREADY COMPLETE -- do not redo, undo or change its result: \"{goal}\". "
    "Remaining housekeeping: {todo} "
    "Do not close or change anything else. Choose DONE as soon as nothing is left."
)
# Saving is part of the job only when the task asks for it or edits an existing file. A new, unnamed document the
# user only asked to write is left open and unsaved (saving would need a name and a place they never gave).
SAVE_INTENT = re.compile(r"\b(save|saved|saving|edit|update|modify|overwrite|rename|append|keep (it|the file))\b", re.I)
UNSAVED_ISSUE = "has unsaved changes"
KEEP_OPEN = re.compile(r"^\s*(open|show|display|find|search|look up|navigate|go to|launch|start|play|watch)\b", re.I)
EDITS = re.compile(r"\b(save|edit|add|append|change|update|write|type|rename|delete|remove|create|paste|copy|move|fill)\b", re.I)

FINAL_STATUSES = ("done", "unverified", "blocked", "needs_user", "max_steps", "stopped", "error")


def _extract_field_value(raw: str | dict | None) -> str | None:
    """Extract and validate a value from LLM output."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        v = raw.get("text")
        return str(v).strip() if v is not None and str(v).strip() and len(str(v)) <= 4000 else None
    if not isinstance(raw, str):
        return None
    content = re.sub(r"<think>[\s\S]*?</think>", "", raw.strip()).strip()
    if not content:
        return None
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content, re.IGNORECASE)
    if fence:
        content = fence.group(1).strip()
    else:
        obj = re.search(r"\{[\s\S]*\}", content)
        if obj:
            content = obj.group(0)
    try:
        data = json.loads(content)
        if isinstance(data, dict) and data.get("text") is not None:
            v = str(data["text"]).strip()
            return v if v and len(v) <= 4000 else None
    except Exception:
        pass
    if len(content) <= 100 and not content.startswith("{"):
        return content.strip("\"'")
    return None


def threading_wait(seconds: float) -> None:
    """Sleep that tests' patched time.sleep can't turn into a busy loop."""
    import threading
    threading.Event().wait(seconds)


def _extract_json_object(raw: str | None) -> dict | None:
    """A whole JSON object from LLM output (also unwraps {"text": {...}} / {"text": "<json>"})."""
    content = re.sub(r"<think>[\s\S]*?</think>", "", (raw or "").strip())
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content, re.IGNORECASE)
    if fence:
        content = fence.group(1)
    m = re.search(r"\{[\s\S]*\}", content)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    if isinstance(data, dict) and set(data) == {"text"}:
        inner = data["text"]
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except Exception:
                return None
        data = inner
    return data if isinstance(data, dict) and data else None


@dataclass
class RunContext:
    history: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    text_calls: list[dict[str, Any]] = field(default_factory=list)
    phases: list[dict[str, Any]] = field(default_factory=list)
    handed_off: set = field(default_factory=set)
    clipboard_baseline: str | None = None
    clipboard_seen: bool = False
    user_request: str | None = None   # the user's own words, verbatim (planner restatements may lose details)
    started_at: float = field(default_factory=time.perf_counter)
    run_id: str = field(default_factory=lambda: datetime.datetime.now().strftime("%Y%m%d_%H%M%S_")
                        + uuid.uuid4().hex[:4])
    started_wall: str = field(default_factory=lambda: datetime.datetime.now().isoformat(timespec="seconds"))
    steering: list[str] = field(default_factory=list)     # messages the user typed during the run
    rescues: list[dict[str, Any]] = field(default_factory=list)
    llm_rescues: int = 0
    user_hints: int = 0
    shots: list[dict[str, Any]] = field(default_factory=list)
    recipes_used: list[dict[str, Any]] = field(default_factory=list)
    final_report: str | None = None
    tracker: Any = None

    def elapsed_ms(self) -> int:
        return round((time.perf_counter() - self.started_at) * 1000)


class AgentLoop:
    """observe -> ask Jev -> execute -> repeat until verified DONE, BLOCKED, hand-off, or step limit."""

    def __init__(
        self,
        emit: Callable[[str, Any], None] | None = None,
        reviewer: Reviewer | None = None,
        should_stop: Callable[[], bool] | None = None,
        jev: JevClient | None = None,
        text_llm: Any = None,
        procedure_planner: Any = None,
        steer: Callable[[], list[str]] | None = None,
        rescuer: Any = None,
        recipes: Any = None,
        vision_llm: Any = None,
        shortcuts: Any = None,
    ) -> None:
        self._settings = get_settings()
        self._jev = jev or JevClient()
        self._verifier = Verifier(self._jev)
        self._text_llm = text_llm if text_llm is not None else get_text_llm()
        self._emit = emit or (lambda *_: None)
        self._reviewer = reviewer
        self._should_stop = should_stop or (lambda: False)
        self._risky_keywords = parse_keywords(self._settings.risky_keywords)
        self._procedures = procedure_planner   # created lazily (needs the planner LLM)
        self._user_request: str | None = None
        self._steering: list[str] = []
        self._steer = steer or (lambda: [])
        self._rescuer = rescuer                 # None: created lazily; False: disabled
        if recipes is None and self._settings.recipes_enabled:
            from .recipes import RecipeStore
            recipes = RecipeStore(self._settings.recipes_path)
        self._recipes = recipes or None
        self._vision_llm = vision_llm           # None: lazily from settings; False: disabled
        if shortcuts is None and self._settings.shortcuts_enabled:
            try:
                from ..shortcuts import Shortcuts
                shortcuts = Shortcuts(self._settings)
            except Exception as exc:
                logger.debug("Shortcuts unavailable: %s", exc)
                shortcuts = None
        self._shortcuts = shortcuts or None
        self._watcher = takeover.get_watcher() if self._settings.takeover_pause else None
        self._input_mark = time.time()
        self._acts_done = 0

    # ----------------------------------------------------------- helpers
    def _safe_emit(self, event: str, data: Any) -> None:
        try:
            self._emit(event, data)
        except Exception as exc:
            logger.debug("emit(%s) failed: %s", event, exc)

    def _status(self, status: str, message: str = "") -> None:
        self._safe_emit("status", {"status": status, "message": message})

    def _note(self, ctx: RunContext, note: str) -> None:
        ctx.history.append({"operation": "NOTE", "note": note, "step": len(ctx.history) + 1})
        self._safe_emit("warning", {"message": note})

    def _generate_value(self, kind: str, goal: str, overall_goal: str, action: dict | None,
                        page: dict, history: list, mode: str = "replace", full_text: str | None = None,
                        guidance_step: str | None = None) -> str | None:
        context: dict[str, Any] = {
            "goal": goal,
            "page": {"title": page.get("title", ""), "url": page.get("url", ""), "text": page.get("text", "")[:5000]},
            "recent_actions": [{**{k: h.get(k) for k in ("operation", "label", "text")},
                                **({"result": str(h["result"])[:600]} if h.get("result") else {}),
                                **({"failed": str(h["failed"])[:200]} if h.get("failed") else {})}
                               for h in history[-6:] if h.get("operation") != "NOTE"],
        }
        if kind in ("copy", "report"):
            context["page"]["full_text"] = (full_text or page.get("text", ""))[:20000]
        if kind == "office":
            context["office_context"] = full_text or "{}"
        if kind == "drop":
            context["dragging"] = (action or {}).get("label")
            context["drop_targets"] = sorted({str(a.get("label", ""))[:80] for a in page.get("actions", [])
                                              if a.get("node") is not None and a.get("id") != (action or {}).get("id")})[:150]
        if guidance_step:
            context["current_procedure_step"] = guidance_step
        if overall_goal and overall_goal != goal:
            context["overall_goal_context_only"] = overall_goal
            context["note"] = "goal is the current phase; return the value needed for THIS phase."
        if self._user_request and self._user_request != goal:
            context["user_request_verbatim"] = self._user_request
            context["data_rule"] = "Take exact values (names, numbers, text) from user_request_verbatim; never invent data."
        if page.get("mode") == "desktop":
            context["active_window"] = page.get("title", "")
            try:
                from ..capture.desktop import known_folders
                context["known_folders"] = known_folders()
            except Exception:
                pass
        if kind == "field" and action:
            context["field"] = {k: action.get(k) for k in ("label", "role", "value")}
            context["field"]["current_value"] = context["field"].pop("value", "")
            context["mode"] = mode
        if self._steering:
            context["user_messages_mid_run"] = self._steering[-5:]
        if kind in JSON_KINDS or kind == "saveas":
            context["now"] = datetime.datetime.now().strftime("%A %Y-%m-%d %H:%M")
        if kind in ("fileop",) and "known_folders" not in context:
            try:
                from ..capture.desktop import known_folders
                context["known_folders"] = known_folders()
            except Exception:
                pass
        messages = [{"role": "system", "content": VALUE_PROMPTS[kind]}, {"role": "user", "content": json.dumps(context)}]
        for attempt in range(2):
            try:
                content = self._text_llm.complete(messages).content
                if kind in JSON_KINDS:
                    obj = _extract_json_object(content)
                    if obj is not None:
                        return json.dumps(obj)
                    continue
                value = _extract_field_value(content)
                if value:
                    if kind == "url" and "://" not in value:
                        value = "https://" + value.lstrip("/")
                    return value
            except Exception as exc:
                logger.warning("Text LLM error (attempt %d): %s", attempt + 1, exc)
                time.sleep(0.5)
        if kind == "field":  # last resort: quoted text in the goal
            quoted = re.search(r"['\"“]([^'\"”]+)['\"”]", goal)
            if quoted:
                return quoted.group(1).strip()
        return None

    def _observe(self, capture, ctx: RunContext, step: int) -> dict | None:
        s = self._settings
        shot = s.screenshot_interval > 0 and step % s.screenshot_interval == 0
        for attempt in range(3):
            try:
                with takeover.agent_input():
                    page = capture.observe(screenshot=shot)
                # Privacy: only show Jev clipboard text that was copied during this task
                if "clipboard" in page:
                    if not ctx.clipboard_seen:
                        ctx.clipboard_seen, ctx.clipboard_baseline = True, page["clipboard"]
                    if page["clipboard"] == ctx.clipboard_baseline:
                        page["clipboard"] = "(nothing copied during this task)" if page["clipboard"] is not None else None
                return page
            except Exception as exc:
                logger.warning("Observation failed (attempt %d): %s", attempt + 1, exc)
                time.sleep(0.6)
        return None

    def _step_record(self, decision, page: dict, ctx: RunContext, **extra: Any) -> dict:
        rec = {
            "step": len(ctx.decisions) + 1,
            "operation": decision.operation,
            "choice": decision.choice,
            "label": decision.label,
            "target": decision.target,
            "confidence": decision.confidence,
            "target_confidence": decision.target_confidence,
            "effective_confidence": decision.effective_confidence,
            "operation_probabilities": decision.operation_probabilities,
            "target_probabilities": decision.target_probabilities,
            "latency_ms": decision.latency_ms,
            "elapsed_ms": ctx.elapsed_ms(),
            "usage": decision.usage,
            "url": page.get("url", ""),
            "title": page.get("title", ""),
        }
        rec.update(extra)
        return rec

    # ------------------------------------------------------------- phase
    def _run_phase(self, goal: str, capture, ctx: RunContext, max_steps: int, overall_goal: str,
                   verify: bool = True) -> str:
        """Run Jev on one (sub-)goal with one capture. Returns a status from FINAL_STATUSES."""
        s = self._settings
        thr = s.jev_confidence_threshold
        phase_start = len(ctx.history)
        done_rejections = 0
        pending_rejections = 0
        user_rejections = 0
        exec_errors = 0
        stuck_warnings = 0
        last_note_idx = phase_start
        loop_sigs: set = set()      # actions involved in the loop we warned about
        relapse = 0                 # consecutive post-warning actions that repeat that loop
        progress_streak = 0         # consecutive steps with real, new progress
        procedure = None            # how-to checklist for this phase
        lessons: list[str] = []     # what failed in earlier runs of similar tasks
        app = ""
        rescue_level = 0            # 0: next rescue asks the LLM; 1: next asks the user; 2: give up
        forced: list[dict] = []     # rescue actions to run before asking Jev again
        disabled_shortcuts: set[str] = set()
        replan_for_steer = False
        last_reason = ""

        fresh_page = None           # the after-action snapshot, reused as the next step's observation
        for step_num in range(max_steps):
            if self._should_stop():
                return "stopped"

            # --- Steering: messages the user typed while the run was going ---
            for msg in self._poll_steer():
                ctx.steering.append(msg)
                ctx.history.append({"operation": "NOTE", "step": len(ctx.history) + 1,
                                    "note": f"The user said during the run: \"{msg}\" -- follow it; it overrides "
                                            "earlier instructions where they conflict."})
                self._safe_emit("steer_ack", {"message": msg})
                stuck_warnings, loop_sigs, relapse, rescue_level = 0, set(), 0, 0
                last_note_idx = len(ctx.history)
                forced.clear()
                replan_for_steer = True

            if self._watcher is not None and self._watcher.since(self._input_mark):
                status = self._paused_by_user(ctx, self._watcher.last_what)
                if status:
                    return status
                fresh_page = None          # the screen may have changed: look again

            page, fresh_page = (fresh_page, None) if fresh_page is not None else (self._observe(capture, ctx, step_num), None)
            if page is None:
                self._status("error", "Could not observe the application state.")
                return "error"

            if replan_for_steer and step_num == 0:
                replan_for_steer = False   # the first procedure below already sees the message
            if step_num == 0 and verify:
                app = recipe_app_key(page)
                procedure, lessons = self._initial_procedure(goal, page, overall_goal, ctx, app)
            if replan_for_steer:
                replan_for_steer = False
                new = (self._make_procedure(goal, page, overall_goal, lessons=lessons)
                       if verify and (procedure is not None or self._wants_procedure(goal, page)) else None)
                if new is not None:
                    new.revision = (procedure.revision + 1) if procedure else 1
                    new.source = "user"
                    procedure = new
            guidance = self._guidance(procedure, ctx, lessons)

            # --- Hand-off for things only a human may do (login, 2FA, CAPTCHA) ---
            reason = needs_handoff(page)
            key = (page.get("url", "").split("?")[0], reason)
            if reason and key not in ctx.handed_off:
                ctx.handed_off.add(key)
                status = self._handoff(ctx, reason, page)
                if status:
                    return status
                continue

            page = self._augment(page, goal, capture, disabled_shortcuts, stuck=bool(stuck_warnings or loop_sigs))

            # --- Decide (a pending rescue action first, otherwise Jev) ---
            decision = None
            preset_text = None
            is_rescue = False
            if forced:
                item = forced.pop(0)
                decision = self._forced_decision(item, page)
                if decision is None:
                    forced.clear()
                    self._note(ctx, f"Rescue action {item['operation']} '{item['label']}' is no longer available.")
                else:
                    preset_text, is_rescue = item.get("text"), True
            if decision is None:
                try:
                    decision = self._jev.choose(page, goal, ctx.history, overall_goal, guidance,
                                                user_request=ctx.user_request)
                except Exception as exc:
                    logger.error("Jev decision failed: %s", exc)
                    self._status("error", f"Jev error: {exc}")
                    return "error"

            # --- Low confidence: re-observe and ask again before acting ---
            retries = 0
            while (not is_rescue and decision.effective_confidence < thr and retries < s.low_confidence_retries
                   and decision.operation not in TERMINAL_OPS):
                retries += 1
                time.sleep(0.8)
                page = self._augment(self._observe(capture, ctx, step_num) or page, goal, capture,
                                     disabled_shortcuts, stuck=bool(stuck_warnings or loop_sigs))
                try:
                    decision = self._jev.choose(page, goal, ctx.history, overall_goal, guidance,
                                                user_request=ctx.user_request)
                except Exception as exc:
                    self._status("error", f"Jev error: {exc}")
                    return "error"

            flags: list[str] = ["rescue"] if is_rescue else []
            if decision.effective_confidence < thr and decision.operation not in TERMINAL_OPS:
                flags.append("low_confidence")
                if (s.confirm_low_confidence and self._reviewer and self._reviewer.interactive
                        and decision.effective_confidence < s.low_confidence_ask_threshold
                        and decision.operation not in SAFE_OPS):
                    ok = self._reviewer.confirm(
                        "low_confidence",
                        f"Jev is only {decision.effective_confidence:.0%} confident about: "
                        f"{decision.operation} -> '{decision.label}'. Run this step?",
                        {"operation": decision.operation, "label": decision.label},
                    )
                    if not ok:
                        user_rejections += 1
                        self._note(ctx, f"User rejected {decision.operation} '{decision.label}'. Choose a different action.")
                        if user_rejections >= 3:
                            return "blocked"
                        continue

            rec = self._step_record(decision, page, ctx, flags=flags, retries=retries)
            self._safe_emit("step", rec)
            ctx.decisions.append(rec)
            op = decision.operation

            # --- Terminal operations ---
            if op == "BLOCKED":
                rescued, rescue_level, forced, procedure = self._try_rescue(
                    ctx, goal, page, overall_goal, "Jev reports no operation can make progress", procedure,
                    lessons, rescue_level, capture)
                if rescued:
                    stuck_warnings, loop_sigs, relapse = 0, set(), 0
                    last_note_idx = len(ctx.history)
                    continue
                self._status("blocked", "Jev reports no operation can make progress.")
                return self._phase_end("blocked", goal, app, ctx, phase_start, capture, verify,
                                       "no operation could make progress", procedure)
            if op == "NEED_USER":
                status = self._handoff(ctx, "Jev needs you to complete a step (sign-in, code, or a decision).", page)
                if status:
                    return status
                continue
            if op == "DONE":
                # Implicit finishing steps (unsaved changes, open rename box, open menu) come before DONE
                issues = []
                if hasattr(capture, "pending_issues") and pending_rejections < 3:
                    try:
                        issues = self._relevant_issues(capture.pending_issues(page), overall_goal or goal)
                    except Exception:
                        issues = []
                if issues:
                    pending_rejections += 1
                    self._note(ctx, "Not finished yet: " + "; ".join(issues) + ". Do this before choosing DONE.")
                    continue
                if not s.verify_done or not verify:
                    return self._phase_end("done", goal, app, ctx, phase_start, capture, verify, "", procedure)
                check_goal = goal if not ctx.user_request or ctx.user_request in goal else \
                    f"{goal}\n(The user's exact request: {ctx.user_request})"
                if ctx.final_report:
                    check_goal += f"\n(Reported answer to user: {ctx.final_report})"
                if ctx.steering:
                    check_goal += "\n(The user added during the run: " + " | ".join(ctx.steering[-5:]) + ")"
                ok, prob = self._verifier.verify(check_goal, page, ctx.history[phase_start:])
                rec["validation"] = {"verified": ok, "score": prob,
                                     "note": f"Goal check {prob:.0%}" if ok is not None else "Goal check unavailable"}
                self._safe_emit("validation", {"step": rec["step"], **rec["validation"]})
                if ok:
                    return self._phase_end("done", goal, app, ctx, phase_start, capture, verify, "", procedure)
                if ok is None:
                    return self._phase_end("unverified", goal, app, ctx, phase_start, capture, verify,
                                           "goal check unavailable", procedure)
                done_rejections += 1
                self._note(ctx, f"DONE rejected by verifier ({prob:.0%}): the goal is not visibly complete yet. "
                                "Check what is missing and continue.")
                if done_rejections >= s.max_done_rejections:
                    self._status("unverified", "Jev keeps reporting DONE but the goal could not be verified.")
                    return self._phase_end("unverified", goal, app, ctx, phase_start, capture, verify,
                                           "DONE could not be verified", procedure)
                continue

            # --- Deleting anything ALWAYS needs the user's OK (independent of other settings) ---
            if op != "DELETE_FILE" and is_delete(op, decision.label, page):
                if not self._confirm_delete(ctx, f"{op} '{decision.label}' in {page.get('title', '')}", {}):
                    user_rejections += 1
                    if user_rejections >= 3:
                        return "blocked"
                    continue

            # --- Risky actions need a human OK ---
            elif s.confirm_risky_actions and self._reviewer is not None:
                kw = is_risky(op, decision.label, self._risky_keywords)
                if kw:
                    ok = self._reviewer.confirm(
                        "risky_action", f"About to {op} '{decision.label}' (matches '{kw}'). Allow?",
                        {"operation": op, "label": decision.label, "keyword": kw},
                    )
                    if not ok:
                        user_rejections += 1
                        self._note(ctx, f"User did not allow {op} '{decision.label}'.")
                        if user_rejections >= 3:
                            return "blocked"
                        continue

            # --- Values from the text LLM (or from the rescue plan) ---
            text: str | None = None
            if op in VALUE_OPS:
                kind = VALUE_OPS[op]
                if preset_text and kind not in JSON_KINDS and op not in ("OFFICE_ACTION", "DELETE_FILE"):
                    text = preset_text
                else:
                    full_text = None
                    if op == "OFFICE_ACTION" and hasattr(capture, "office_context"):
                        try:
                            full_text = json.dumps(capture.office_context(), default=str)
                        except Exception:
                            full_text = None
                    if op in ("COPY_TEXT", "REPORT_ANSWER") and hasattr(capture, "full_text"):
                        try:
                            full_text = capture.full_text()
                        except Exception:
                            full_text = None
                    t0 = time.perf_counter()
                    text = self._generate_value(kind, goal, overall_goal, decision.action, page, ctx.history,
                                                mode="append" if op == "APPEND_TEXT" else "replace",
                                                full_text=full_text,
                                                guidance_step=(procedure.steps[procedure.current]
                                                               if procedure and procedure.current < len(procedure.steps)
                                                               else None))
                    ctx.text_calls.append({"operation": op, "field": decision.label, "value": text,
                                           "latency_ms": round((time.perf_counter() - t0) * 1000)})
                if text is None:
                    self._note(ctx, f"Could not generate a value for {op} '{decision.label}'.")
                    continue
                if op == "APPEND_TEXT" and decision.action:
                    existing = str(decision.action.get("value") or "")
                    if existing and not existing[-1].isspace() and not text[:1].isspace() and text[:1] not in ".,;:!?)":
                        text = ("\n" if decision.action.get("multiline") else " ") + text
                rec["text"] = text

            if op == "OFFICE_ACTION":
                from ..capture.office import UnsafeCode, check_code
                app_name = "excel" if "excel" in str(page.get("url", "")).lower() else "word"
                try:
                    if check_code(text, app_name):
                        if not self._confirm_delete(ctx, f"run an Office action that deletes/clears content:\n{text}",
                                                    {"code": text}):
                            user_rejections += 1
                            continue
                except UnsafeCode as exc:
                    self._note(ctx, f"OFFICE_ACTION code rejected ({exc}). Write simpler code or use the normal UI.")
                    continue

            if op == "DELETE_FILE":
                target = self._resolve_delete_target(text)
                if target is None:
                    self._note(ctx, f"DELETE_FILE: nothing named '{text}' was found -- check the name or location.")
                    continue
                if not self._confirm_delete(ctx, f"Move '{target}' to the Recycle Bin", {"path": str(target)}):
                    user_rejections += 1
                    if user_rejections >= 3:
                        return "blocked"
                    continue
                page = {**page, "actions": [dict(a, resolved_path=str(target)) if a["id"] == decision.choice else a
                                            for a in page["actions"]]}

            if op == "SEND_EMAIL":
                if not self._confirm_send(ctx, text):
                    user_rejections += 1
                    disabled_shortcuts.add("SEND_EMAIL")
                    if user_rejections >= 3:
                        return "blocked"
                    continue
            elif op in SHORTCUT_OPS and self._shortcuts is not None and hasattr(self._shortcuts, "outbound"):
                out = self._shortcuts.outbound(op, text or "")
                if out is not None and not self._confirm_outbound(ctx, out[0], out[1], text):
                    user_rejections += 1
                    if user_rejections >= 3:
                        return "blocked"
                    continue

            # --- Execute ---
            act_text = text
            result_note = None
            if op == "VISUAL_CLICK":
                act_text = self._locate_visual(ctx, capture, text or "", goal, rec)
                if act_text is None:
                    continue
            takeover.begin_action()
            if self._watcher is not None and self._watcher.since(self._input_mark):
                status = self._paused_by_user(ctx, self._watcher.last_what)
                if status:
                    return status
                fresh_page = None
                continue                   # decide again on the current screen
            try:
                if op == "REPORT_ANSWER":
                    ctx.final_report = text
                    self._safe_emit("report", {"text": text, "step": len(ctx.history) + 1})
                    result_note = f"Reported answer:\n{text}"
                    exec_result = {"executed": "report_answer", "note": result_note}
                elif op in SHORTCUT_OPS:
                    try:
                        result_note = self._shortcuts.execute(op, text)
                    except Exception as exc:
                        disabled_shortcuts.add(op)
                        self._note(ctx, f"{op} failed ({exc}). Do this step in the normal app UI instead.")
                        continue
                    exec_result = {"executed": op}
                    self._safe_emit("info", {"message": result_note})
                else:
                    with takeover.agent_input():
                        exec_result = capture.act(page, decision.choice, text=act_text, operation=op) or {}
                    self._acts_done += 1
                    if self._watcher is not None and self._acts_done == 5 and getattr(capture, "mode", "") == "desktop":
                        self._watcher.hooks_look_dead()
                exec_errors = 0
                if exec_result.get("note"):
                    self._note(ctx, exec_result["note"])
            except takeover.UserTookOver as exc:
                status = self._paused_by_user(ctx, str(exc))
                if status:
                    return status
                self._note(ctx, f"{op} '{decision.label}' was interrupted when you took over -- it may be incomplete. "
                                "Check the screen and finish or redo it.")
                fresh_page = None
                continue
            except Exception as exc:
                exec_errors += 1
                logger.warning("Execution failed: %s", exc)
                if s.screenshot_on_error:
                    try:
                        capture.screenshot(str(Path(s.log_dir) / f"error_step_{rec['step']}.png"))
                    except Exception:
                        pass
                self._note(ctx, f"Executing {op} '{decision.label}' failed: {exc}"
                                + (" Do not repeat this exact action -- choose another way." if exec_errors >= 2 else ""))
                ctx.history.append({"step": len(ctx.history) + 1, "operation": op, "action": decision.choice,
                                    "label": decision.label, "text": text, "page_changed": False,
                                    "failed": str(exc)[:300], "url": page.get("url", ""),
                                    "confidence": decision.effective_confidence, "elapsed_ms": ctx.elapsed_ms()})
                if exec_errors >= 3:
                    # don't end the run on the first dead end: let the rescue model / the user find another way
                    rescued, rescue_level, forced, procedure = self._try_rescue(
                        ctx, goal, page, overall_goal, f"'{op} {decision.label}' failed 3 times: {exc}", procedure,
                        lessons, rescue_level, capture)
                    if rescued:
                        exec_errors = 0
                        stuck_warnings, loop_sigs, relapse = 0, set(), 0
                        last_note_idx = len(ctx.history)
                        continue
                    self._status("error", f"3 execution errors in a row: {exc}")
                    return self._phase_end("error", goal, app, ctx, phase_start, capture, verify, str(exc)[:200],
                                           procedure)
                continue

            # --- Progress check ---
            new_page = self._observe(capture, ctx, step_num) or page
            fresh_page = new_page   # nothing happens between this snapshot and the next decision: reuse it
            page_changed = new_page.get("fingerprint") != page.get("fingerprint") or result_note is not None
            rec["page_changed"] = page_changed
            if s.verify_each_step:
                ok, score, note = self._verifier.verify_step(f"{op} on '{decision.label}'", goal,
                                                             summarize_state(new_page, 2000))
                rec["validation"] = {"verified": ok, "score": score, "note": note}
                self._safe_emit("validation", {"step": rec["step"], **rec["validation"]})

            entry = {
                "step": len(ctx.history) + 1,
                "operation": op,
                "action": decision.choice,
                "label": decision.label,
                "text": text,
                "page_changed": page_changed,
                "url": page.get("url", ""),
                "confidence": decision.effective_confidence,
                "elapsed_ms": ctx.elapsed_ms(),
            }
            if result_note:
                entry["result"] = result_note[:1500]
            if is_rescue:
                entry["rescue"] = True
            ctx.history.append(entry)

            if procedure is not None:
                before = procedure.current
                procedure.observe_action(op, decision.label, text, page_changed)
                if procedure.current != before:
                    self._safe_emit("procedure", {"steps": procedure.steps, "done": procedure.done,
                                                  "revision": procedure.revision, "source": procedure.source})

            last = ctx.history[-1]
            last_sig = (last.get("operation"), last.get("label"), last.get("text"))
            recent_sigs = {(h.get("operation"), h.get("label"), h.get("text")) for h in ctx.history[-4:-1]}
            progress_streak = progress_streak + 1 if (page_changed and last_sig not in recent_sigs) else 0
            if progress_streak >= 3 and (stuck_warnings or loop_sigs or rescue_level):
                # the agent recovered: long tasks must not be killed by an old, already-resolved loop
                stuck_warnings = max(0, stuck_warnings - 1)
                loop_sigs, relapse = set(), 0
                rescue_level = 0

            give_up = None
            if loop_sigs:
                relapse = relapse + 1 if (last_sig in loop_sigs and not page_changed) else 0
                if relapse >= 3:
                    give_up = "fell back into the same loop after being warned"

            reason = detect_stuck(ctx.history[last_note_idx:], s.stuck_repeat_limit, thr)
            if reason and not give_up:
                stuck_warnings += 1
                last_reason = reason
                if stuck_warnings >= 3:
                    give_up = reason
                else:
                    self._note(ctx, f"Loop detected ({reason}). That approach is not working -- try a different "
                                    "element or operation, or choose NEED_USER / BLOCKED.")
                    loop_sigs = signatures(ctx.history)
                    relapse = 0
                    last_note_idx = len(ctx.history)
                    if procedure is not None or self._wants_procedure(goal, page):
                        failed = [f"{h.get('operation')} '{h.get('label')}'" for h in ctx.history[phase_start:][-8:]
                                  if h.get("operation") != "NOTE"] + [reason]
                        new = self._make_procedure(goal, new_page, overall_goal, failed=failed, lessons=lessons)
                        if new is not None:
                            new.revision = (procedure.revision + 1) if procedure else 1
                            procedure = new

            if give_up:
                rescued, rescue_level, forced, procedure = self._try_rescue(
                    ctx, goal, new_page, overall_goal, give_up, procedure, lessons, rescue_level, capture)
                if rescued:
                    stuck_warnings, loop_sigs, relapse = 0, set(), 0
                    last_note_idx = len(ctx.history)
                    fresh_page = None if forced else fresh_page
                else:
                    self._status("blocked", f"Stuck: {give_up}.")
                    return self._phase_end("blocked", goal, app, ctx, phase_start, capture, verify, give_up,
                                           procedure)

            if s.step_delay_ms > 0:
                time.sleep(s.step_delay_ms / 1000)

        self._status("max_steps", f"Reached the step limit ({max_steps}).")
        return self._phase_end("max_steps", goal, app, ctx, phase_start, capture, verify,
                               last_reason or "step limit", procedure)

    # -------------------------------------------------------- take-over
    def _paused_by_user(self, ctx: RunContext, what: str) -> str | None:
        """The person used the mouse / keyboard: stop acting until they hand back control."""
        s = self._settings
        msg = (f"Paused -- you took over ({what or 'mouse/keyboard'}). Finish what you are doing, then press Resume. "
               "The agent will look at the screen again before continuing.")
        self._status("paused", msg)
        self._safe_emit("paused", {"what": what})
        ctx.history.append({"operation": "NOTE", "step": len(ctx.history) + 1,
                            "note": "The user took over the mouse/keyboard and may have changed the screen. "
                                    "Re-check the current window and state before continuing."})
        if self._reviewer is not None and self._reviewer.interactive:
            ok = self._reviewer.takeover(msg, {"what": what})
            if not ok:
                return "stopped"
        else:   # unattended: wait until the person has stopped using the PC for a while
            deadline = time.time() + s.handoff_timeout_s
            while self._watcher is not None and self._watcher.idle_for() < s.takeover_idle_resume_s:
                if self._should_stop():
                    return "stopped"
                if time.time() > deadline:
                    return "needs_user"
                threading_wait(1.0)
        if self._should_stop():
            return "stopped"
        self._status("running", "Resuming -- checking the screen again.")
        self._input_mark = time.time() + 0.3
        return None

    # ----------------------------------------------------- phase helpers
    def _poll_steer(self) -> list[str]:
        try:
            return [str(m).strip() for m in (self._steer() or []) if str(m).strip()]
        except Exception:
            return []

    def _guidance(self, procedure, ctx: RunContext, lessons: list[str]) -> dict | None:
        g: dict[str, Any] = procedure.as_guidance() if procedure else {}
        if lessons:
            g["lessons_from_past_runs"] = lessons
        if ctx.steering:
            g["user_messages_mid_run"] = ctx.steering[-5:]
        return g or None

    def _initial_procedure(self, goal: str, page: dict, overall_goal: str, ctx: RunContext, app: str):
        """A learned recipe for this app + kind of task if one exists, else (maybe) a fresh LLM procedure."""
        lessons: list[str] = []
        if self._recipes is not None and app:
            try:
                lessons = self._recipes.lessons(goal, app)
                recipe = self._recipes.find(goal, app, self._settings.recipe_match_threshold)
            except Exception as exc:
                logger.debug("recipe lookup failed: %s", exc)
                recipe = None
            if recipe:
                from .procedure import Procedure
                proc = Procedure(list(recipe["steps"]), source="recipe", recipe_id=recipe.get("id"))
                ctx.recipes_used.append({"id": recipe.get("id"), "task": recipe.get("task"),
                                         "score": recipe.get("score")})
                self._status("running", f"Using a learned recipe from an earlier verified run "
                                        f"({recipe.get('successes', 1)}× successful).")
                self._safe_emit("procedure", {"steps": proc.steps, "done": proc.done, "revision": 0,
                                              "source": "recipe"})
                return proc, lessons
        if self._wants_procedure(goal, page):
            return self._make_procedure(goal, page, overall_goal, lessons=lessons), lessons
        return None, lessons

    def _phase_end(self, status: str, goal: str, app: str, ctx: RunContext, phase_start: int, capture,
                   verify: bool, reason: str, procedure) -> str:
        """Learn from the phase (recipes / lessons) and keep a screenshot of how it ended."""
        if verify:
            self._shot(ctx, capture, f"phase_{status}")
        if self._recipes is None or not verify or not app:
            return status
        hist = ctx.history[phase_start:]
        try:
            if procedure is not None and procedure.source == "recipe" and procedure.recipe_id:
                self._recipes.mark_used(procedure.recipe_id, status == "done")
            if status == "done":
                if procedure is not None and procedure.source != "recipe":
                    steps, source = list(procedure.steps), "procedure"
                else:
                    steps, source = steps_from_history(hist), "actions"
                self._recipes.record_success(goal, app, steps, ctx.user_request, source=source)
            elif status in ("blocked", "max_steps", "unverified", "error"):
                tried = [f"{h.get('operation')} '{str(h.get('label', ''))[:40]}'" for h in hist
                         if h.get("operation") not in (None, "NOTE")][-6:]
                self._recipes.record_failure(goal, app, status, reason, tried)
        except Exception as exc:
            logger.debug("recipe update failed: %s", exc)
        return status

    def _shot(self, ctx: RunContext, capture, tag: str) -> None:
        s = self._settings
        if not (s.save_traces and s.save_screenshots) or capture is None or not hasattr(capture, "screenshot"):
            return
        name = f"{len(ctx.shots) + 1:02d}_{re.sub(r'[^a-z0-9_]+', '_', tag.lower())[:40]}.png"
        path = Path(s.log_dir) / "shots" / ctx.run_id / name
        try:
            capture.screenshot(str(path))
            if path.exists():
                ctx.shots.append({"file": name, "tag": tag, "step": len(ctx.decisions), "elapsed_ms": ctx.elapsed_ms()})
        except Exception as exc:
            logger.debug("screenshot failed: %s", exc)

    # ------------------------------------------- extra actions (vision, shortcuts)
    def _vision(self):
        if self._vision_llm is None:
            if not self._settings.vision_fallback:
                self._vision_llm = False
            else:
                try:
                    from ..llm import get_vision_llm
                    self._vision_llm = get_vision_llm() or False
                except Exception as exc:
                    logger.debug("vision LLM unavailable: %s", exc)
                    self._vision_llm = False
        return self._vision_llm or None

    def _augment(self, page: dict, goal: str, capture, disabled_shortcuts: set, stuck: bool) -> dict:
        extra: list[dict] = []
        if self._shortcuts is not None:
            try:
                extra += self._shortcuts.actions(" ".join(filter(None, [goal, self._user_request])), page,
                                                 disabled_shortcuts)
            except Exception as exc:
                logger.debug("shortcut offer failed: %s", exc)
        if (hasattr(capture, "vision_shot") and (stuck or element_count(page) < self._settings.vision_min_elements)
                and self._vision() is not None):
            extra += [
                {"id": "VISUAL_CLICK", "kind": "visual", "op": "VISUAL_CLICK",
                 "label": "Click something visible on screen that is not in the element list (uses a screenshot)"},
                {"id": "TYPE_FOCUSED", "kind": "type_focused", "op": "TYPE_FOCUSED",
                 "label": "Type text where the keyboard focus is"},
            ]
        if not extra:
            return page
        have = {a["id"] for a in page.get("actions", [])}
        return {**page, "actions": list(page.get("actions", [])) + [a for a in extra if a["id"] not in have]}

    def _locate_visual(self, ctx: RunContext, capture, description: str, goal: str, rec: dict) -> str | None:
        llm = self._vision()
        if llm is None:
            self._note(ctx, "VISUAL_CLICK is not available (no vision model configured).")
            return None
        target, button = parse_target(description)
        try:
            shot = capture.vision_shot()
        except Exception as exc:
            self._note(ctx, f"VISUAL_CLICK: could not take a screenshot ({exc}).")
            return None
        pt = locate(llm, shot, target, self._user_request or goal)
        rec["vision"] = {"target": target, "button": button, **(pt or {"found": False})}
        if not pt or not pt.get("found"):
            seen = (pt or {}).get("what", "")
            self._note(ctx, f"VISUAL_CLICK: '{target}' was not found on the screenshot"
                            + (f" (the screen shows: {seen})" if seen else "") + ". Try something else.")
            return None
        return f"{pt['screen_x']},{pt['screen_y']},{button}"

    def _confirm_send(self, ctx: RunContext, text: str | None) -> bool:
        """Sending mail always needs the user's OK, with what will be sent shown in full."""
        try:
            msg = json.loads(text or "{}")
        except Exception:
            msg = {}
        if msg.get("error") or not msg.get("to"):
            self._note(ctx, f"SEND_EMAIL: {msg.get('error') or 'no recipient'} -- the request must name the recipient.")
            return False
        if self._reviewer is None:
            self._note(ctx, "Not sending: email needs the user's confirmation and nobody can confirm.")
            return False
        to = msg.get("to") if isinstance(msg.get("to"), str) else ", ".join(msg.get("to") or [])
        summary = (f"Send email to {to}" + (f" (cc {msg.get('cc')})" if msg.get("cc") else "")
                   + f"\nSubject: {msg.get('subject', '')}\n\n{str(msg.get('body', ''))[:1500]}"
                   + (f"\nAttachments: {msg.get('attachments')}" if msg.get("attachments") else ""))
        ok = self._reviewer.confirm("send_email", summary, {"email": msg})
        if not ok:
            self._note(ctx, "User did not allow sending this email. Ask (NEED_USER) or change it as the user wants.")
        return ok

    def _confirm_outbound(self, ctx: RunContext, kind: str, summary: str, spec: str | None) -> bool:
        """Messages, invites and uploads reach other people / the cloud: always the user's OK, shown in full."""
        if self._reviewer is None:
            self._note(ctx, f"Not done ({kind}): it needs the user's confirmation and nobody can confirm.")
            return False
        ok = self._reviewer.confirm(kind, summary, {"spec": spec})
        if not ok:
            self._note(ctx, f"User did not allow this ({kind}). Ask (NEED_USER) or change it as the user wants.")
        return ok

    # ----------------------------------------------------------- rescue
    def _forced_decision(self, item: dict, page: dict) -> JevDecision | None:
        acts = page.get("actions", [])
        action = next((a for a in acts if a["id"] == item["id"] and str(a.get("label", a["id"])) == item["label"]),
                      None) or next((a for a in acts if str(a.get("label", "")) == item["label"]
                                     and item["operation"] in ops_for(a)), None)
        if action is None or item["operation"] not in ops_for(action):
            return None
        return JevDecision(operation=item["operation"], choice=action["id"], target=None, confidence=1.0,
                           target_confidence=None, probabilities={action["id"]: 1.0},
                           operation_probabilities={item["operation"]: 1.0}, target_probabilities={},
                           raw_answers={}, model="rescue", latency_ms=0, action=action)

    def _try_rescue(self, ctx: RunContext, goal: str, page: dict, overall_goal: str, reason: str,
                    procedure, lessons: list[str], level: int, capture):
        """
        Escalate instead of giving up: LLM rescue (offered actions or a new procedure) -> the user's hint -> blocked.
        Returns (rescued, new_level, forced_actions, procedure).
        """
        s = self._settings
        if not s.rescue_enabled:
            return False, level, [], procedure
        self._shot(ctx, capture, "stuck")
        if level == 0 and ctx.llm_rescues < s.max_rescues:
            if self._rescuer is None:
                try:
                    from .rescue import Rescuer
                    self._rescuer = Rescuer()
                except Exception as exc:
                    logger.warning("Rescue LLM unavailable: %s", exc)
                    self._rescuer = False
            if self._rescuer:
                ctx.llm_rescues += 1
                self._status("running", f"Stuck ({reason}) -- asking the rescue model for another way.")
                plan = self._rescuer.suggest(goal, page, ctx.history, reason, ctx.user_request, lessons, ctx.steering)
                entry = {"step": len(ctx.decisions), "by": "llm", "reason": reason, "kind": plan.kind,
                         "why": plan.why, "actions": plan.actions, "steps": plan.steps, "question": plan.question}
                ctx.rescues.append(entry)
                self._safe_emit("rescue", entry)
                if plan.kind == "actions":
                    self._note(ctx, f"Rescue: {plan.why or 'trying a different way'} -- "
                                    + "; ".join(f"{a['operation']} '{a['label']}'" for a in plan.actions))
                    return True, 1, list(plan.actions), procedure
                if plan.kind == "replan":
                    from .procedure import Procedure
                    proc = Procedure(plan.steps, source="rescue",
                                     revision=(procedure.revision + 1) if procedure else 1)
                    self._safe_emit("procedure", {"steps": proc.steps, "done": proc.done, "revision": proc.revision,
                                                  "source": "rescue"})
                    self._note(ctx, f"Rescue: new approach -- {plan.why or 'follow the revised procedure'}.")
                    return True, 1, [], proc
                if plan.kind == "needs_person" and plan.question:
                    reason = f"{reason}. The rescue model needs: {plan.question}"
            level = 1
        if level <= 1 and s.rescue_ask_user and ctx.user_hints < 3 and self._reviewer is not None \
                and self._reviewer.interactive:
            ctx.user_hints += 1
            self._status("needs_user", "Stuck -- waiting for a hint from you.")
            hint = self._reviewer.hint(
                f"I'm stuck: {reason}. Tell me what to do next (e.g. 'the button is under the File menu', "
                "'use Ctrl+Shift+S'), or do the step yourself in the window and send 'done'.",
                {"goal": goal, "title": page.get("title"), "url": page.get("url")})
            self._input_mark = time.time() + 0.3     # the person may have worked in the window: not a take-over
            entry = {"step": len(ctx.decisions), "by": "user", "reason": reason, "hint": hint}
            ctx.rescues.append(entry)
            self._safe_emit("rescue", {**entry, "kind": "hint" if hint else "none"})
            if hint:
                done_by_user = hint.strip().lower() in ("done", "i did it", "did it", "finished", "ok done")
                note = ("The user did the stuck step manually. Continue from the current screen."
                        if done_by_user else f"Hint from the user: \"{hint}\" -- follow it.")
                ctx.history.append({"operation": "NOTE", "note": note, "step": len(ctx.history) + 1})
                if not done_by_user:
                    ctx.steering.append(f"(hint while stuck) {hint}")
                self._status("running", "Continuing with your hint.")
                new = None
                if procedure is not None or self._wants_procedure(goal, page):
                    failed = [f"{h.get('operation')} '{h.get('label')}'" for h in ctx.history[-10:]
                              if h.get("operation") != "NOTE"] + [reason]
                    new = self._make_procedure(goal, page, overall_goal, failed=failed, lessons=lessons)
                    if new is not None:
                        new.revision, new.source = (procedure.revision + 1) if procedure else 1, "user"
                return True, 1, [], new or procedure
        return False, 2, [], procedure


    # ------------------------------------------------------- procedures
    def _wants_procedure(self, goal: str, page: dict) -> bool:
        mode = self._settings.procedure_mode
        if mode == "off":
            return False
        if mode == "always":
            return True
        return page.get("mode") == "desktop" or len(goal.split()) >= 10

    def _make_procedure(self, goal: str, page: dict, overall_goal: str, failed: list[str] | None = None,
                        lessons: list[str] | None = None):
        if self._procedures is None:
            from .procedure import ProcedurePlanner
            try:
                self._procedures = ProcedurePlanner()
            except Exception as exc:
                logger.warning("Procedure planner unavailable: %s", exc)
                self._procedures = False
        if not self._procedures:
            return None
        try:
            proc = self._procedures.make(goal, page, overall_goal, failed=failed, user_request=self._user_request,
                                         user_messages=list(self._steering) or None, lessons=lessons or None)
        except TypeError:   # planners written for the older signature
            proc = self._procedures.make(goal, page, overall_goal, failed=failed, user_request=self._user_request)
        if proc is not None:
            self._safe_emit("procedure", {"steps": proc.steps, "done": proc.done, "revision": proc.revision,
                                          "source": proc.source})
        return proc

    def _confirm_delete(self, ctx: RunContext, what: str, details: dict) -> bool:
        """Mandatory confirmation for any deletion. No reviewer -> not allowed."""
        if self._reviewer is None:
            self._note(ctx, f"Not deleting ({what}): deletion needs the user's confirmation and nobody can confirm.")
            return False
        ok = self._reviewer.confirm("delete", f"Delete: {what}. Allow?", details)
        if not ok:
            self._note(ctx, f"User did not allow the deletion ({what}). Do not try to delete it another way.")
        return ok

    @staticmethod
    def _resolve_delete_target(spec: str | None):
        from ..capture.files import find_files, folder_hint
        name = (spec or "").partition("|")[0].strip()
        hits = find_files(name, hint_dir=folder_hint(name), include_dirs=True, limit=2)
        return hits[0] if hits else None

    def _handoff(self, ctx: RunContext, reason: str, page: dict) -> str | None:
        """Ask the user to take over. Returns a final status if the run must end, else None."""
        msg = f"{reason} Please complete it in the {'browser' if page.get('mode') == 'browser' else 'app'} window, then continue."
        self._status("needs_user", msg)
        if self._reviewer is None or not self._reviewer.handoff(msg, {"url": page.get("url"), "title": page.get("title")}):
            return "stopped" if self._should_stop() else "needs_user"
        ctx.history.append({"operation": "NOTE", "note": f"User completed a manual step: {reason}",
                            "step": len(ctx.history) + 1})
        self._input_mark = time.time() + 0.3     # the manual step itself is not a take-over
        self._status("running", "Resuming after your manual step.")
        time.sleep(0.8)
        return None

    # -------------------------------------------------------------- runs
    def run(self, goal: str, capture, start_url: str | None = None,
            user_request: str | None = None) -> dict[str, Any]:
        """Single-phase run on one capture. user_request: the user's exact words (kept alongside the goal)."""
        ctx = self._new_context(user_request)
        with ctx.tracker.activate():
            self._status("running", f"Starting: {goal}")
            self._begin_run(capture)
            if start_url:
                capture.navigate(start_url)
            status = self._run_phase(goal, capture, ctx, self._settings.max_steps, goal)
            ctx.phases.append({"task": goal, "mode": getattr(capture, "mode", "?"), "status": status})
            if status in ("done", "unverified"):
                self._wrap_up(ctx, [capture], goal)
            return self._finish(goal, status, ctx)

    def _new_context(self, user_request: str | None) -> RunContext:
        ctx = RunContext(user_request=user_request)
        self._input_mark = time.time()
        ctx.tracker = usage_mod.current() or usage_mod.UsageTracker()   # the server may have started it (planner)
        self._user_request = user_request
        self._steering = ctx.steering
        self._safe_emit("run_started", {"run_id": ctx.run_id})
        return ctx

    def _needs_saving(self, goal: str) -> bool:
        text = " ".join(filter(None, [goal, self._user_request]))
        if SAVE_INTENT.search(text):
            return True
        try:
            from ..capture.files import referenced_files
            return bool(referenced_files(text))     # an existing (named) file is being worked on
        except Exception:
            return False

    def _relevant_issues(self, issues: list[str], goal: str) -> list[str]:
        if self._needs_saving(goal):
            return issues
        return [i for i in issues if UNSAVED_ISSUE not in i]

    @staticmethod
    def _begin_run(capture) -> None:
        if capture is not None and hasattr(capture, "begin_run"):
            try:
                capture.begin_run()
            except Exception:
                pass

    def _wrap_up(self, ctx: RunContext, captures: list, goal: str) -> None:
        """
        The 'unsaid' steps a person does after finishing: commit pending edits, save, close what they opened.
        Driven by Jev (situation-dependent), bounded, and never allowed to change the task's result.
        """
        s = self._settings
        if not s.wrap_up or self._should_stop():
            return
        keep_open = bool(KEEP_OPEN.search(goal)) and not EDITS.search(goal)
        for cap in captures:
            if cap is None or not hasattr(cap, "pending_issues"):
                continue
            try:
                issues = self._relevant_issues(cap.pending_issues(None), goal)
                opened = cap.opened_windows() if (s.close_opened_windows and not keep_open
                                                  and getattr(cap, "mode", "") == "desktop") else []
                if not self._needs_saving(goal):
                    # closing an unsaved new document would force a Save / Don't save choice: leave it open
                    opened = [t for t in opened if not (t.startswith("*") or t.startswith("● ") or t.endswith(" *")
                                                        or "(Unsaved)" in t or " - Not saved" in t)]
            except Exception as exc:
                logger.debug("wrap-up inspection failed: %s", exc)
                continue
            if not issues and not opened:
                continue
            todo = " ".join(f"- {i}." for i in issues)
            if opened:
                todo += (" - Save any unsaved changes, then close these windows that were opened for the task: "
                         + "; ".join(f"'{t}'" for t in opened)
                         + ". If closing asks whether to save, choose Save (same name and location).")
            self._status("running", "Finishing up: " + (", ".join(issues) or "closing windows opened for the task"))
            self._safe_emit("phase", {"index": 0, "total": 0, "task": "Finishing up", "mode": cap.mode, "url": None})
            status = self._run_phase(WRAP_UP_GOAL.format(goal=goal, todo=todo), cap, ctx,
                                     s.wrap_up_max_steps, goal, verify=False)
            ctx.phases.append({"task": "Finishing up (save / close)", "mode": cap.mode, "status": status})
            if status != "done":
                self._safe_emit("warning", {"message": f"Finishing-up steps did not complete ({status}); "
                                                       "please check for unsaved work or open windows."})

    def run_plan(self, plan_steps: list, overall_goal: str, capture, start_url: str | None = None,
                 user_request: str | None = None) -> dict[str, Any]:
        """
        Multi-phase run. `capture` is a CaptureManager (switches browser/desktop per phase) or a single capture.
        Every phase is driven by Jev; the run stops at the first phase that does not finish.
        """
        ctx = self._new_context(user_request)
        with ctx.tracker.activate():
            return self._run_plan(plan_steps, overall_goal, capture, start_url, ctx)

    def _run_plan(self, plan_steps: list, overall_goal: str, capture, start_url: str | None,
                  ctx: RunContext) -> dict[str, Any]:
        from .planner import normalize_steps

        s = self._settings
        default_mode = getattr(capture, "mode", "browser")
        steps = normalize_steps(plan_steps, overall_goal, default_mode, start_url)
        self._status("running", f"Starting {len(steps)}-phase plan: {overall_goal}")
        overall = "done"
        used: list = []

        for idx, step in enumerate(steps, 1):
            if self._should_stop():
                overall = "stopped"
                break
            task, mode, url = step["task"], step["mode"], step.get("url")
            self._safe_emit("phase", {"index": idx, "total": len(steps), "task": task, "mode": mode, "url": url})
            self._status("running", f"Phase {idx}/{len(steps)} [{mode}]: {task}")
            try:
                if hasattr(capture, "get"):
                    cap = capture.get(mode, url)
                else:
                    cap = capture
                    if getattr(cap, "mode", mode) != mode:
                        logger.warning("Phase wants %s but only a %s capture is available", mode, cap.mode)
                    if url:
                        cap.navigate(url)
            except Exception as exc:
                self._status("error", f"Could not open {url or mode}: {exc}")
                overall = "error"
                ctx.phases.append({"task": task, "mode": mode, "status": "error"})
                break

            if cap not in used:
                used.append(cap)
                self._begin_run(cap)
            status = self._run_phase(task, cap, ctx, min(s.phase_max_steps, s.max_steps), overall_goal)
            ctx.phases.append({"task": task, "mode": mode, "url": url, "status": status})
            if status == "unverified":
                overall = "unverified"
                continue
            if status != "done":
                overall = status
                break

        if overall in ("done", "unverified"):
            self._wrap_up(ctx, used, overall_goal)
        return self._finish(overall_goal, overall, ctx)

    def _finish(self, goal: str, status: str, ctx: RunContext) -> dict[str, Any]:
        messages = {
            "done": "Goal achieved and verified.",
            "unverified": "Finished, but the result could not be verified -- please check it.",
            "blocked": "Agent is blocked.",
            "needs_user": "Waiting on you -- the run stopped at a step only you can do.",
            "max_steps": "Step limit reached.",
            "stopped": "Stopped by user.",
            "error": "Run ended with an error.",
        }
        self._status(status, messages.get(status, status))
        usage = ctx.tracker.summary() if ctx.tracker is not None else {}
        llm_rescues = [r for r in ctx.rescues if r.get("by") == "llm"]
        result = {
            "run_id": ctx.run_id,
            "started": ctx.started_wall,
            "status": status,
            "goal": goal,
            "user_request": ctx.user_request,
            "final_report": ctx.final_report,
            "steps": len(ctx.decisions),
            "elapsed_ms": ctx.elapsed_ms(),
            "phases": ctx.phases,
            "usage": usage,
            "rescues": ctx.rescues,
            "rescue_stats": {"llm": len(llm_rescues),
                             "llm_helpful": sum(1 for r in llm_rescues if r.get("kind") in ("actions", "replan")),
                             "user_hints": sum(1 for r in ctx.rescues if r.get("by") == "user" and r.get("hint"))},
            "steering": ctx.steering,
            "recipes_used": ctx.recipes_used,
            "shots": ctx.shots,
            "history": ctx.history,
            "decisions": ctx.decisions,
            "text_calls": ctx.text_calls,
        }
        if self._settings.save_traces:
            self._save_trace(result)
        return result

    def _save_trace(self, result: dict) -> None:
        log_dir = Path(self._settings.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"trace_{result.get('run_id') or datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{result['status']}.json"
        try:
            path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
            result["trace_path"] = str(path)
        except Exception as exc:
            logger.warning("Could not save trace: %s", exc)

    def close(self) -> None:
        self._jev.close()

    def __enter__(self) -> "AgentLoop":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
