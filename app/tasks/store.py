"""
Saved, reusable tasks with {placeholders}, optional schedules and folder triggers.

A task:
  {"id", "name", "template": "Rename {file} to {new_name}", "params": {"new_name": "Report.docx"},
   "mode": "auto|browser|desktop", "url": "", "schedule": "daily 09:00", "watch_folder": "~/Downloads",
   "watch_pattern": "*.csv", "allow_risky": false, "enabled": true, "last_run": ..., "next_run": ...,
   "runs": [{"ts", "status", "steps", "elapsed_ms", "goal", "trace"}]}

Schedules: "every 30m" | "every 2h" | "every 1d" | "daily 09:00" | "weekdays 08:30" | "mon,wed,fri 10:00" |
           "once 2026-10-05 09:00". Times are local.
Folder triggers: a new file matching watch_pattern in watch_folder runs the task with {file} (full path),
{filename} and {folder} filled in.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MAX_RUNS_KEPT = 30
_lock = threading.RLock()


class TaskError(ValueError):
    pass


def placeholders(template: str) -> list[str]:
    seen: list[str] = []
    for m in _PLACEHOLDER.finditer(template or ""):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def render(template: str, params: dict[str, Any]) -> str:
    missing = [p for p in placeholders(template) if str(params.get(p, "")).strip() == ""]
    if missing:
        raise TaskError("missing value for: " + ", ".join(missing))
    return _PLACEHOLDER.sub(lambda m: str(params[m.group(1)]), template)


# ------------------------------------------------------------------ schedules
def _hhmm(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise TaskError(f"bad time {text!r} (use HH:MM)")
    return int(m.group(1)), int(m.group(2))


def parse_schedule(spec: str) -> dict[str, Any] | None:
    spec = (spec or "").strip().lower()
    if not spec:
        return None
    m = re.fullmatch(r"every\s+(\d+)\s*(m|min|mins|minutes?|h|hours?|d|days?)", spec)
    if m:
        n = int(m.group(1))
        unit = m.group(2)[0]
        minutes = n * {"m": 1, "h": 60, "d": 1440}[unit]
        if minutes < 5:
            raise TaskError("the shortest interval is 5 minutes")
        return {"kind": "interval", "minutes": minutes}
    m = re.fullmatch(r"once\s+(\d{4}-\d{2}-\d{2})[ t](\d{1,2}:\d{2})", spec)
    if m:
        h, mi = _hhmm(m.group(2))
        return {"kind": "once", "at": dt.datetime.fromisoformat(m.group(1)).replace(hour=h, minute=mi)}
    m = re.fullmatch(r"(daily|weekdays|weekends|[a-z]{3}(?:\s*,\s*[a-z]{3})*)\s+(\d{1,2}:\d{2})", spec)
    if m:
        days_s, (h, mi) = m.group(1), _hhmm(m.group(2))
        if days_s == "daily":
            days = list(range(7))
        elif days_s == "weekdays":
            days = list(range(5))
        elif days_s == "weekends":
            days = [5, 6]
        else:
            names = [d.strip() for d in days_s.split(",")]
            if any(d not in DAYS for d in names):
                raise TaskError(f"unknown day in {days_s!r} (use mon,tue,...)")
            days = sorted({DAYS.index(d) for d in names})
        return {"kind": "weekly", "days": days, "hour": h, "minute": mi}
    raise TaskError(f"unknown schedule {spec!r}; e.g. 'every 30m', 'daily 09:00', 'weekdays 08:30', "
                    "'mon,fri 10:00', 'once 2026-10-05 09:00'")


def next_run(spec: str, since: dt.datetime) -> dt.datetime | None:
    """The first scheduled time strictly after `since` (the last run, or when the task was created).
    A time in the past means the task is due now (e.g. the PC was off at the scheduled time)."""
    sch = parse_schedule(spec)
    if sch is None:
        return None
    if sch["kind"] == "interval":
        return since + dt.timedelta(minutes=sch["minutes"])
    if sch["kind"] == "once":
        return sch["at"] if sch["at"] > since else None
    for add in range(0, 8):
        day = (since + dt.timedelta(days=add)).replace(hour=sch["hour"], minute=sch["minute"], second=0,
                                                       microsecond=0)
        if day.weekday() in sch["days"] and day > since:
            return day
    return None


def task_next_run(task: dict[str, Any]) -> dt.datetime | None:
    if not task.get("enabled", True) or not task.get("schedule"):
        return None
    since = _ts(task.get("last_run")) or _ts(task.get("schedule_set")) or _ts(task.get("created")) or dt.datetime.now()
    try:
        return next_run(task["schedule"], since)
    except TaskError:
        return None


# ---------------------------------------------------------------------- store
class TaskStore:
    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except Exception:
            return []

    def _save(self, tasks: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(tasks, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, self.path)

    def list(self) -> list[dict[str, Any]]:
        with _lock:
            tasks = self._load()
        for t in tasks:
            t["placeholders"] = placeholders(t.get("template", ""))
            nr = task_next_run(t)
            t["next_run"] = nr.isoformat(timespec="minutes") if nr else None
        return tasks

    def get(self, task_id: str) -> dict[str, Any] | None:
        return next((t for t in self.list() if t["id"] == task_id or t["name"].lower() == str(task_id).lower()), None)

    def upsert(self, data: dict[str, Any]) -> dict[str, Any]:
        name = str(data.get("name", "")).strip()
        template = str(data.get("template", "")).strip()
        if not name or not template:
            raise TaskError("a task needs a name and a template (the goal text)")
        if data.get("schedule"):
            parse_schedule(data["schedule"])           # validate
        params = data.get("params") or {}
        if not isinstance(params, dict):
            raise TaskError("params must be an object")
        clean = {
            "name": name[:80], "template": template[:4000],
            "params": {str(k): str(v) for k, v in params.items()},
            "mode": data.get("mode") if data.get("mode") in ("auto", "browser", "desktop") else "auto",
            "url": str(data.get("url") or "")[:500],
            "schedule": str(data.get("schedule") or "").strip(),
            "watch_folder": str(data.get("watch_folder") or "").strip(),
            "watch_pattern": str(data.get("watch_pattern") or "*").strip() or "*",
            "allow_risky": bool(data.get("allow_risky", False)),
            "enabled": bool(data.get("enabled", True)),
        }
        with _lock:
            tasks = self._load()
            existing = next((t for t in tasks if t["id"] == data.get("id")), None)
            if existing is None and any(t["name"].lower() == name.lower() for t in tasks):
                raise TaskError(f"a task named {name!r} already exists")
            if existing is None:
                existing = {"id": uuid.uuid4().hex[:8], "created": _now(), "runs": []}
                tasks.append(existing)
            if clean["schedule"] != existing.get("schedule"):
                existing["schedule_set"] = _now()      # a new schedule counts from now, not from creation
            existing.update(clean)
            self._save(tasks)
            return dict(existing)

    def delete(self, task_id: str) -> bool:
        with _lock:
            tasks = self._load()
            keep = [t for t in tasks if t["id"] != task_id]
            if len(keep) == len(tasks):
                return False
            self._save(keep)
            return True

    def record_run(self, task_id: str, result: dict[str, Any], trigger: str) -> None:
        with _lock:
            tasks = self._load()
            for t in tasks:
                if t["id"] == task_id:
                    t["last_run"] = _now()
                    t["last_status"] = result.get("status")
                    t.setdefault("runs", []).append({
                        "ts": t["last_run"], "trigger": trigger, "status": result.get("status"),
                        "steps": result.get("steps"), "elapsed_ms": result.get("elapsed_ms"),
                        "goal": result.get("goal"), "trace": Path(result.get("trace_path", "")).name or None,
                        "cost_usd": (result.get("usage") or {}).get("cost_usd"), "error": result.get("error"),
                    })
                    t["runs"] = t["runs"][-MAX_RUNS_KEPT:]
                    break
            self._save(tasks)


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _ts(value: Any) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
