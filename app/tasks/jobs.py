"""
Long jobs: "for every file in this folder …", "for each row in this sheet …".

The request is turned into a job: an item source (files in a folder, rows of a CSV / Excel file, or a list) and a
per-item task. Each item runs as its own agent run; progress is saved to data/jobs/<id>.json after every item, so
a job survives a crash, a restart or a Stop and can be resumed where it left off (an item that was running is run
again). Three failures in a row pause the job so it doesn't burn through every item the same wrong way.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import json
import logging
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

LOOP_HINT = re.compile(
    r"\b(for (each|every)|each of (the|my)|every (single )?(file|row|line|item|email|pdf|csv|document|image|photo|"
    r"invoice|record|entry)|all (the |my )?(files|rows|items|pdfs|documents|images|photos|invoices|records|entries)"
    r" (in|from|of)|one by one)\b", re.I)

JOB_PROMPT = """\
You turn a request that repeats the same work for many items into a job for a computer-use agent.
Return ONLY JSON:
{"is_job": true,
 "source": {"type": "files", "folder": "folder path or Desktop / Documents / Downloads ...", "pattern": "*.pdf",
            "recursive": false}
         | {"type": "rows", "file": "CSV / Excel file", "sheet": ""}
         | {"type": "list", "items": ["...", "..."]},
 "per_item": "the task for ONE item, self-contained, using placeholders: {item} (file: full path; row: the whole row;
              list: the value), {name} (file name), {stem} (file name without extension), {folder}, {n} (item number),
              {col:Column Name} (a value of the current row)",
 "after": "a final task once every item is done, or \\"\\"",
 "max_items": 200}
Keep every concrete value from the request (texts, recipients, names). Never invent items.
If the request does not repeat work over items, return {"is_job": false}.
"""
_lock = threading.Lock()


class JobError(Exception):
    pass


def looks_like_job(request: str) -> bool:
    return bool(LOOP_HINT.search(request or ""))


def _parse(content: str) -> dict[str, Any] | None:
    content = re.sub(r"<think>[\s\S]*?</think>", "", content or "")
    m = re.search(r"\{[\s\S]*\}", content)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    return d if isinstance(d, dict) else None


# --------------------------------------------------------------------- items
def _folder(path: str) -> Path:
    from ..shortcuts.fileops import _expand
    return _expand(path)


def enumerate_items(source: dict[str, Any], limit: int = 200) -> list[dict[str, Any]]:
    kind = str(source.get("type", "")).lower()
    items: list[dict[str, Any]] = []
    if kind == "files":
        folder = _folder(str(source.get("folder") or "Documents"))
        if not folder.is_dir():
            raise JobError(f"folder not found: {folder}")
        pattern = str(source.get("pattern") or "*")
        walker = folder.rglob("*") if source.get("recursive") else folder.iterdir()
        files = sorted((p for p in walker if p.is_file() and fnmatch.fnmatch(p.name.lower(), pattern.lower())
                        and not p.name.startswith(("~$", "."))), key=lambda p: str(p).lower())
        for p in files[:limit]:
            items.append({"label": p.name, "vars": {"item": str(p), "name": p.name, "stem": p.stem,
                                                    "folder": str(p.parent)}})
    elif kind == "rows":
        from ..shortcuts.data import load
        from ..shortcuts.fileops import _resolve_existing
        src = _resolve_existing(str(source.get("file")), want_dir=False)
        header, rows = load(src, source.get("sheet") or None)
        for i, r in enumerate(rows[:limit]):
            cols = {h: ("" if v is None else str(v)) for h, v in zip(header, r)}
            text = "; ".join(f"{h}: {v}" for h, v in cols.items())
            items.append({"label": f"row {i + 2}: " + ", ".join(list(cols.values())[:3]),
                          "vars": {"item": text, "row": str(i + 2), "file": str(src), "cols": cols}})
    elif kind == "list":
        for v in (source.get("items") or [])[:limit]:
            items.append({"label": str(v)[:80], "vars": {"item": str(v)}})
    else:
        raise JobError(f"unknown item source {kind!r}")
    for n, it in enumerate(items, 1):
        it["vars"]["n"] = str(n)
    return items


def render_item(template: str, vars: dict[str, Any]) -> str:
    cols = vars.get("cols") or {}

    def col(m: re.Match) -> str:
        name = m.group(1).strip()
        for k, v in cols.items():
            if k.strip().lower() == name.lower():
                return v
        return m.group(0)

    out = re.sub(r"\{col:([^}]+)\}", col, template)
    for k, v in vars.items():
        if k != "cols":
            out = out.replace("{" + k + "}", str(v))
    return out


# ---------------------------------------------------------------------- store
class JobStore:
    def __init__(self, folder: str | os.PathLike) -> None:
        self.folder = Path(folder)

    def path(self, job_id: str) -> Path:
        if not re.fullmatch(r"[\w-]+", job_id or ""):
            raise JobError("bad job id")
        return self.folder / f"{job_id}.json"

    def save(self, job: dict[str, Any]) -> None:
        job["updated"] = dt.datetime.now().isoformat(timespec="seconds")
        self.folder.mkdir(parents=True, exist_ok=True)
        p = self.path(job["id"])
        tmp = p.with_suffix(".tmp")
        with _lock:
            tmp.write_text(json.dumps(job, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
            os.replace(tmp, p)

    def load(self, job_id: str) -> dict[str, Any]:
        try:
            return json.loads(self.path(job_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise JobError(f"no job {job_id}") from None

    def list(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.folder.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:100]:
            try:
                j = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append({k: j.get(k) for k in ("id", "request", "status", "created", "updated")} | progress(j))
        return out

    def unfinished(self) -> list[dict[str, Any]]:
        return [j for j in self.list() if j["status"] not in ("done", "cancelled")]


def progress(job: dict[str, Any]) -> dict[str, int]:
    items = job.get("items", [])
    return {"total": len(items), "done": sum(1 for i in items if i["status"] == "done"),
            "failed": sum(1 for i in items if i["status"] == "failed"),
            "pending": sum(1 for i in items if i["status"] in ("pending", "running"))}


def new_job(request: str, spec: dict[str, Any]) -> dict[str, Any]:
    items = enumerate_items(spec.get("source") or {}, int(spec.get("max_items") or 200))
    if not items:
        raise JobError("the job has no items (empty folder / sheet, or nothing matched the pattern)")
    per_item = str(spec.get("per_item") or "").strip()
    if not per_item:
        raise JobError("the job has no per-item task")
    return {"id": dt.datetime.now().strftime("job_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4], "request": request,
            "source": spec.get("source"), "per_item": per_item, "after": str(spec.get("after") or "").strip(),
            "status": "pending", "created": dt.datetime.now().isoformat(timespec="seconds"),
            "items": [{**it, "status": "pending", "attempts": 0} for it in items], "after_status": None}


def plan_job(request: str, llm: Any = None) -> dict[str, Any] | None:
    """Ask the planner LLM whether this is a job and how to enumerate it; None when it is a normal task."""
    if not looks_like_job(request):
        return None
    if llm is None:
        from ..llm import get_planner_llm
        llm = get_planner_llm()
    try:
        resp = llm.complete([{"role": "system", "content": JOB_PROMPT}, {"role": "user", "content": request}])
    except Exception as exc:
        logger.warning("job planning failed: %s", exc)
        return None
    spec = _parse(resp.content)
    if not spec or not spec.get("is_job"):
        return None
    return new_job(request, spec)


# --------------------------------------------------------------------- runner
def run_job(job: dict[str, Any], store: JobStore, run_item: Callable[[str, dict[str, Any]], dict[str, Any]],
            emit: Callable[[str, Any], None] | None = None, should_stop: Callable[[], bool] | None = None,
            max_attempts: int = 2, max_consecutive_failures: int = 3) -> dict[str, Any]:
    """Run (or resume) a job. run_item(goal, item) runs one agent run and returns its result dict."""
    emit = emit or (lambda *_: None)
    should_stop = should_stop or (lambda: False)
    for it in job["items"]:
        if it["status"] == "running":            # crashed / stopped mid-item: do it again
            it["status"] = "pending"
    job["status"] = "running"
    store.save(job)
    fails = 0
    total = len(job["items"])
    for idx, it in enumerate(job["items"]):
        if it["status"] == "done" or (it["status"] == "failed" and it.get("attempts", 0) >= max_attempts):
            continue
        if should_stop():
            job["status"] = "stopped"
            break
        goal = render_item(job["per_item"], it["vars"])
        it.update(status="running", goal=goal, started=dt.datetime.now().isoformat(timespec="seconds"))
        it["attempts"] = it.get("attempts", 0) + 1
        job["current"] = idx
        store.save(job)
        emit("job_progress", {"job_id": job["id"], "index": idx + 1, "total": total, "item": it["label"],
                              "state": "running", **progress(job)})
        try:
            res = run_item(goal, it)
        except Exception as exc:
            logger.exception("job item failed")
            res = {"status": "error", "error": str(exc)}
        ok = res.get("status") == "done"
        it.update(status="done" if ok else ("pending" if res.get("status") == "stopped" else "failed"),
                  result=res.get("status"), trace=Path(res.get("trace_path", "")).name or None,
                  error=res.get("error"), steps=res.get("steps"), elapsed_ms=res.get("elapsed_ms"))
        store.save(job)
        emit("job_progress", {"job_id": job["id"], "index": idx + 1, "total": total, "item": it["label"],
                              "state": it["status"], **progress(job)})
        if res.get("status") == "stopped" or should_stop():
            job["status"] = "stopped"
            break
        fails = 0 if ok else fails + 1
        if fails >= max_consecutive_failures:
            job["status"] = "paused_failures"
            break
    else:
        pr = progress(job)
        if job.get("after") and job.get("after_status") != "done" and not should_stop():
            emit("job_progress", {"job_id": job["id"], "index": total, "total": total, "item": "final step",
                                  "state": "running", **pr})
            res = run_item(job["after"], {"label": "final step", "vars": {}})
            job["after_status"] = res.get("status")
        job["status"] = "done" if pr["failed"] == 0 and job.get("after_status") in (None, "done") else "done_with_failures"
    job["current"] = None
    store.save(job)
    emit("job_progress", {"job_id": job["id"], "state": job["status"], **progress(job)})
    return job
