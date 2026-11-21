"""
Background scheduler for saved tasks: runs due schedules and folder triggers one at a time, unattended.

Unattended runs use AutoReviewer: deletions are never approved, mail is only sent / risky clicks only allowed
when the task has allow_risky, and hand-offs (login, CAPTCHA) end the run as needs_user. A run never starts
while a run from the UI is in progress (`busy()`), so the agent never fights the user for the mouse.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .store import TaskError, TaskStore, render, task_next_run

logger = logging.getLogger(__name__)


def _expand_folder(folder: str) -> Path:
    try:
        from ..capture.desktop import expand_known, known_folders
        low = folder.strip().strip("\"'")
        for name, real in known_folders().items():
            if low.lower() == name.lower():
                return Path(real)
        return Path(expand_known(low))
    except Exception:
        return Path(os.path.expanduser(folder))


class Scheduler:
    def __init__(self, store: TaskStore, runner: Callable[[dict, str, str], dict],
                 busy: Callable[[], bool] = lambda: False, notify: Callable[[str, Any], None] | None = None,
                 poll_s: float = 20.0) -> None:
        self.store = store
        self._runner = runner            # (task, goal, trigger) -> result
        self._busy = busy
        self._notify = notify or (lambda *_: None)
        self._poll = max(2.0, poll_s)
        self._queue: "queue.Queue[tuple[dict, dict, str]]" = queue.Queue()
        self._pending: set[str] = set()  # task ids queued or running (schedule triggers)
        self._seen: dict[str, dict[str, Any]] = {}   # folder snapshots per task
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.current: dict[str, Any] | None = None
        self.stop_requested = False

    # ---------------------------------------------------------- control
    def start(self) -> None:
        if self._threads:
            return
        for target, name in ((self._ticker, "scheduler-tick"), (self._worker, "scheduler-run")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def shutdown(self) -> None:
        self._stop.set()
        self.stop_requested = True

    def run_now(self, task: dict, params: dict | None = None) -> str:
        goal = render(task["template"], {**task.get("params", {}), **(params or {})})
        self._queue.put((task, {**task.get("params", {}), **(params or {})}, "manual"))
        self._notify("task_queued", {"task": task["name"], "goal": goal})
        return goal

    def stop_current(self) -> bool:
        if self.current is None:
            return False
        self.stop_requested = True
        return True

    # ------------------------------------------------------------ ticks
    def _ticker(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick(dt.datetime.now())
            except Exception as exc:
                logger.warning("scheduler tick failed: %s", exc)
            self._stop.wait(self._poll)

    def tick(self, now: dt.datetime) -> list[tuple[str, str]]:
        """Queue due work. Returns [(task name, trigger)] queued in this tick (for tests / logs)."""
        queued: list[tuple[str, str]] = []
        for task in self.store.list():
            if not task.get("enabled", True):
                continue
            if task.get("schedule") and task["id"] not in self._pending:
                nr = task_next_run(task)
                if nr is not None and nr <= now:
                    self._pending.add(task["id"])
                    self._queue.put((task, dict(task.get("params", {})), "schedule"))
                    queued.append((task["name"], "schedule"))
            if task.get("watch_folder"):
                for path in self._new_files(task):
                    params = {**task.get("params", {}), "file": str(path), "filename": path.name,
                              "folder": str(path.parent)}
                    self._queue.put((task, params, f"file:{path.name}"))
                    queued.append((task["name"], f"file:{path.name}"))
        return queued

    def _new_files(self, task: dict) -> list[Path]:
        folder = _expand_folder(task["watch_folder"])
        if not folder.is_dir():
            return []
        pattern = task.get("watch_pattern") or "*"
        try:
            current = {p.name: p.stat().st_size for p in folder.iterdir()
                       if p.is_file() and fnmatch.fnmatch(p.name.lower(), pattern.lower())
                       and not p.name.startswith(("~$", ".")) and not p.name.endswith((".tmp", ".crdownload", ".part"))}
        except OSError:
            return []
        state = self._seen.get(task["id"])
        if state is None:   # first look: existing files never trigger
            self._seen[task["id"]] = {"known": set(current), "sizes": {}}
            return []
        ready = []
        for name, size in current.items():
            if name in state["known"]:
                continue
            if state["sizes"].get(name) == size:    # unchanged since the last tick: download/copy finished
                state["known"].add(name)
                state["sizes"].pop(name, None)
                ready.append(folder / name)
            else:
                state["sizes"][name] = size
        state["known"] &= set(current)          # a file removed and added again triggers again
        return ready

    # ----------------------------------------------------------- worker
    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                task, params, trigger = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            while self._busy() and not self._stop.is_set():   # never run alongside a UI run
                self._stop.wait(2.0)
            self.execute(task, params, trigger)

    def execute(self, task: dict, params: dict, trigger: str) -> dict:
        try:
            goal = render(task["template"], params)
        except TaskError as exc:
            result = {"status": "error", "error": str(exc), "goal": task["template"]}
            self.store.record_run(task["id"], result, trigger)
            self._pending.discard(task["id"])
            self._notify("task_result", {"task": task["name"], "trigger": trigger, **result})
            return result
        self.current = {"task": task["name"], "goal": goal, "trigger": trigger, "started": time.time()}
        self.stop_requested = False
        self._notify("task_started", {"task": task["name"], "goal": goal, "trigger": trigger})
        try:
            result = self._runner(task, goal, trigger)
        except Exception as exc:
            logger.exception("scheduled task %s failed", task.get("name"))
            result = {"status": "error", "error": str(exc), "goal": goal}
        finally:
            self.current = None
            self._pending.discard(task["id"])
        self.store.record_run(task["id"], result, trigger)
        self._notify("task_result", {
            "task": task["name"], "trigger": trigger, "status": result.get("status"), "goal": goal,
            "steps": result.get("steps"), "elapsed_ms": result.get("elapsed_ms"),
            "trace": Path(result.get("trace_path", "")).name or None, "error": result.get("error"),
            "cost_usd": (result.get("usage") or {}).get("cost_usd")})
        return result
