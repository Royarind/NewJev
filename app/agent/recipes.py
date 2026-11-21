"""
Learned recipes: a small memory of what worked (verified runs) and what did not (failures), per app/site.

- After a phase is VERIFIED done, its procedure (or the actions that made progress) is saved as a recipe.
- Before a phase starts, the best matching recipe for the same app/site becomes the first procedure,
  so a repeat task does not need the procedure LLM and avoids the detours of the first run.
- Failures are stored as short lessons and shown to Jev / the procedure planner / the rescue LLM next time.
Values (names, numbers) from old runs are never reused: the checklist says to take values from the current request.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_STOP = {"the", "a", "an", "to", "and", "or", "of", "in", "on", "for", "with", "then", "it", "its", "this", "that",
         "is", "be", "from", "into", "as", "at", "by", "my", "me", "please", "called", "named", "file", "new"}
MAX_RECIPES = 300
MAX_FAILURES = 300
MAX_PLANS = 200
_lock = threading.Lock()


def generic_tokens(task: str) -> set[str]:
    """Words describing WHAT is done, without the specific values (quoted text, numbers, paths, file names)."""
    t = re.sub(r"[\"“'‘][^\"”'’]{1,200}[\"”'’]", " ", task or "")
    t = re.sub(r"\b[\w.-]+\.[a-z0-9]{2,5}\b", " ", t, flags=re.I)        # file names
    t = re.sub(r"[A-Za-z]:\\[^\s]+|https?://\S+", " ", t)                    # paths / urls
    return {w for w in re.findall(r"[a-z]+", t.lower()) if len(w) > 2 and w not in _STOP}


def similarity(a: str, b: str) -> float:
    ta, tb = generic_tokens(a), generic_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def app_key(page: dict[str, Any] | None) -> str:
    url = str((page or {}).get("url", ""))
    if url.startswith("desktop://"):
        return "desktop:" + url[len("desktop://"):].split("/", 1)[0].lower()
    host = urlparse(url).netloc.lower()
    return "web:" + (host[4:] if host.startswith("www.") else host) if host else ""


def steps_from_history(history: list[dict[str, Any]], limit: int = 20) -> list[str]:
    """Readable steps from the actions that changed something (drops no-ops and repeated attempts)."""
    out: list[str] = []
    for h in history:
        op = h.get("operation")
        if op in (None, "NOTE", "WAIT") or not h.get("page_changed", True):
            continue
        step = f"{op} '{h.get('label', '')}'"
        if h.get("text") and op not in ("OFFICE_ACTION",):
            step += f" (value used last time: {str(h['text'])[:60]!r})"
        if out and out[-1] == step:
            continue
        out.append(step)
    return out[-limit:]


class RecipeStore:
    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)

    # ---------------------------------------------------------------- io
    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("recipes", [])
                data.setdefault("failures", [])
                data.setdefault("plans", [])
                return data
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("Recipe store unreadable (%s); starting empty", exc)
        return {"recipes": [], "failures": [], "plans": []}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ lookup
    def find(self, task: str, app: str, threshold: float = 0.55) -> dict[str, Any] | None:
        with _lock:
            data = self._load()
        best, best_s = None, 0.0
        for r in data["recipes"]:
            if app and r.get("app") and r["app"] != app:
                continue
            s = similarity(task, r.get("task", ""))
            s += 0.05 * min(3, r.get("successes", 1) - 1)        # proven recipes win ties
            if s > best_s:
                best, best_s = r, s
        if best is None or best_s < threshold:
            return None
        return {**best, "score": round(min(best_s, 1.0), 3)}

    def lessons(self, task: str, app: str, limit: int = 4) -> list[str]:
        with _lock:
            data = self._load()
        scored = []
        for f in data["failures"]:
            if app and f.get("app") and f["app"] != app:
                continue
            s = similarity(task, f.get("task", ""))
            if s >= 0.4:
                scored.append((s, f.get("ts", ""), f["lesson"]))
        seen, out = set(), []
        for _, _, lesson in sorted(scored, key=lambda t: (-t[0], t[1]), reverse=False):
            if lesson not in seen:
                seen.add(lesson)
                out.append(lesson)
        return out[:limit]

    def all(self) -> dict[str, Any]:
        with _lock:
            return self._load()

    # ------------------------------------------------------------ record
    def record_success(self, task: str, app: str, steps: list[str], user_request: str | None = None,
                       source: str = "actions") -> dict[str, Any] | None:
        steps = [s for s in steps if s][:20]
        if len(steps) < 2 or not app:
            return None
        now = datetime.datetime.now().isoformat(timespec="seconds")
        with _lock:
            data = self._load()
            for r in data["recipes"]:
                if r.get("app") == app and similarity(task, r.get("task", "")) >= 0.9:
                    # same kind of task again: keep the shorter (more direct) path
                    if len(steps) <= len(r.get("steps", [])) or r.get("source") != "procedure":
                        r["steps"], r["source"] = steps, source
                    r["successes"] = r.get("successes", 1) + 1
                    r["updated"] = now
                    self._save(data)
                    return r
            r = {"id": uuid.uuid4().hex[:10], "app": app, "task": task, "steps": steps, "source": source,
                 "successes": 1, "created": now, "updated": now}
            if user_request and user_request != task:
                r["example_request"] = user_request[:300]
            data["recipes"].append(r)
            data["recipes"] = data["recipes"][-MAX_RECIPES:]
            self._save(data)
            return r

    def record_failure(self, task: str, app: str, status: str, reason: str, tried: list[str]) -> None:
        if not app:
            return
        lesson = f"Previously ended '{status}' ({reason})" + (f"; tried: {', '.join(tried[-4:])}" if tried else "") \
                 + ". Try a different approach early."
        with _lock:
            data = self._load()
            data["failures"].append({"app": app, "task": task, "status": status, "lesson": lesson[:400],
                                     "ts": datetime.datetime.now().isoformat(timespec="seconds")})
            data["failures"] = data["failures"][-MAX_FAILURES:]
            self._save(data)

    def mark_used(self, recipe_id: str, worked: bool) -> None:
        with _lock:
            data = self._load()
            for r in data["recipes"]:
                if r.get("id") == recipe_id:
                    r["uses"] = r.get("uses", 0) + 1
                    if not worked:
                        r["misses"] = r.get("misses", 0) + 1
                    if r.get("misses", 0) >= 3 and r.get("misses", 0) > r.get("successes", 1):
                        data["recipes"].remove(r)   # a recipe that keeps failing is forgotten
                    break
            self._save(data)

    # ------------------------------------------------------------ plans
    def find_plan(self, request: str, threshold: float = 0.8) -> dict[str, Any] | None:
        """A verified plan for the same kind of request (values may differ: the caller adapts them)."""
        with _lock:
            data = self._load()
        best, best_s = None, 0.0
        for p in data["plans"]:
            sc = similarity(request, p.get("request", ""))
            if sc > best_s:
                best, best_s = p, sc
        return {**best, "score": round(best_s, 3)} if best is not None and best_s >= threshold else None

    def record_plan(self, request: str, plan: dict[str, Any]) -> None:
        steps = [{k: st.get(k) for k in ("task", "mode", "url", "existing_file") if st.get(k) is not None}
                 for st in plan.get("steps", [])]
        if not steps:
            return
        now = datetime.datetime.now().isoformat(timespec="seconds")
        with _lock:
            data = self._load()
            for p in data["plans"]:
                if similarity(request, p.get("request", "")) >= 0.95 and len(p.get("steps", [])) == len(steps):
                    p["successes"] = p.get("successes", 1) + 1
                    p["updated"] = now
                    self._save(data)
                    return
            data["plans"].append({"id": uuid.uuid4().hex[:10], "request": request, "goal": plan.get("goal", request),
                                  "steps": steps, "successes": 1, "created": now, "updated": now})
            data["plans"] = data["plans"][-MAX_PLANS:]
            self._save(data)

    def delete(self, recipe_id: str) -> bool:
        with _lock:
            data = self._load()
            n = len(data["recipes"]) + len(data["plans"])
            data["recipes"] = [r for r in data["recipes"] if r.get("id") != recipe_id]
            data["plans"] = [p for p in data["plans"] if p.get("id") != recipe_id]
            if len(data["recipes"]) + len(data["plans"]) != n:
                self._save(data)
                return True
        return False
