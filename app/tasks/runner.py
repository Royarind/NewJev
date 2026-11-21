"""Plan + run one goal end to end (used by scheduled tasks, folder triggers and `run.py --task`)."""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def make_plan(goal: str, url: str | None, mode: str | None) -> tuple[dict, list[dict], str | None]:
    """Returns (plan, steps, warning). Never raises: falls back to running the goal directly."""
    from ..agent.planner import Planner, fallback_plan
    from ..config import get_settings
    forced = mode if mode in ("browser", "desktop") else None
    warning = None
    try:
        plan = Planner().plan(goal)
    except Exception as exc:
        s = get_settings()
        plan = fallback_plan(goal, url, forced or (s.capture_mode if s.capture_mode != "auto" else None))
        warning = f"Planner unavailable ({exc}); running the goal directly."
    steps = plan["steps"]
    if forced:
        for st in steps:
            st["mode"] = forced
    if url:
        steps[0]["url"] = url
    return plan, steps, warning


def run_goal(goal: str, url: str | None = None, mode: str | None = None,
             emit: Callable[[str, Any], None] | None = None, reviewer: Any = None,
             should_stop: Callable[[], bool] | None = None, steer: Callable[[], list[str]] | None = None,
             captures: Any = None) -> dict[str, Any]:
    from ..agent.loop import AgentLoop
    from ..executor import CaptureManager
    from ..llm import usage

    tracker = usage.UsageTracker()
    with tracker.activate():
        plan, steps, warning = make_plan(goal, url, mode)
        if warning and emit:
            emit("warning", {"message": warning})
        own = captures is None
        captures = captures or CaptureManager()
        try:
            with AgentLoop(emit=emit, reviewer=reviewer, should_stop=should_stop, steer=steer) as agent:
                if len(steps) == 1:
                    cap = captures.get(steps[0]["mode"], steps[0].get("url"))
                    result = agent.run(steps[0]["task"] if steps[0].get("existing_file") else plan["goal"], cap,
                                       None, user_request=goal)
                else:
                    result = agent.run_plan(steps, plan["goal"], captures, None, user_request=goal)
            from ..agent.planner import remember_plan
            remember_plan(goal, plan, result)
            return result
        finally:
            if own:
                try:
                    captures.close()
                except Exception:
                    pass


def run_request(goal: str, url: str | None = None, mode: str | None = None,
                emit: Callable[[str, Any], None] | None = None, reviewer: Any = None,
                should_stop: Callable[[], bool] | None = None, steer: Callable[[], list[str]] | None = None
                ) -> dict[str, Any]:
    """Like run_goal, but a repetitive request ("for every file …") runs as a resumable job."""
    from ..config import get_settings
    from .jobs import JobStore, looks_like_job, plan_job, progress, run_job
    job = plan_job(goal) if looks_like_job(goal) else None
    if job is None:
        return run_goal(goal, url, mode, emit, reviewer, should_stop, steer)
    store = JobStore(get_settings().jobs_path)
    store.save(job)
    from ..executor import CaptureManager
    captures = CaptureManager()
    try:
        done = run_job(job, store, lambda g, it: run_goal(g, url, mode, emit, reviewer, should_stop, steer, captures),
                       emit=emit, should_stop=should_stop)
    finally:
        try:
            captures.close()
        except Exception:
            pass
    pr = progress(done)
    return {"status": "done" if done["status"] == "done" else done["status"], "goal": goal, "job_id": done["id"],
            "steps": sum(int(i.get("steps") or 0) for i in done["items"]), "job": pr,
            "error": None if done["status"] == "done" else f"{pr['failed']} of {pr['total']} items failed"}


def resume_job(job_id: str, emit: Callable[[str, Any], None] | None = None, reviewer: Any = None,
               should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
    from ..config import get_settings
    from ..executor import CaptureManager
    from .jobs import JobStore, run_job
    store = JobStore(get_settings().jobs_path)
    job = store.load(job_id)
    captures = CaptureManager()
    try:
        return run_job(job, store, lambda g, it: run_goal(g, None, None, emit, reviewer, should_stop, None, captures),
                       emit=emit, should_stop=should_stop)
    finally:
        try:
            captures.close()
        except Exception:
            pass
