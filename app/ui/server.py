"""FastAPI server with WebSocket — serves the chat UI, streams agent events, relays user confirmations."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..agent.loop import AgentLoop
from ..agent.planner import Planner
from ..agent.review import AutoReviewer, EventReviewer
from ..config import get_settings
from ..executor import CaptureManager
from ..llm import usage as usage_mod
from ..tasks import TaskError, TaskStore, render

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
CLOSE_WORDS = {"close", "close browser", "close window", "exit", "quit", "close app", "close notepad", "close word"}


class ConnectionManager:
    """Manages active WebSocket connections."""

    def __init__(self) -> None:
        self.active: dict[str, WebSocket] = {}

    async def connect(self, session_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self.active.pop(session_id, None)
        self.active[session_id] = ws          # last = most recently connected (gets quick-box goals)

    def disconnect(self, session_id: str) -> None:
        self.active.pop(session_id, None)

    async def broadcast(self, event: str, data: Any) -> None:
        for sid in list(self.active):
            await self.send(sid, event, data)

    async def send(self, session_id: str, event: str, data: Any) -> None:
        if event in POPUP_EVENTS:
            try:
                desktop_popup(event, data)
            except Exception as exc:
                logger.debug("desktop pop-up failed: %s", exc)
        ws = self.active.get(session_id)
        if ws:
            try:
                await ws.send_json({"event": event, "data": data, "ts": time.time()})
            except Exception:
                self.disconnect(session_id)


manager = ConnectionManager()
_planner: Planner | None = None

# session_id -> {"captures": CaptureManager, "status", "stop_requested", "reviewer", "goal", "steer"}
sessions: dict[str, dict] = {}
_scheduler = None
_main_loop: asyncio.AbstractEventLoop | None = None
TRACE_NAME = re.compile(r"^trace_[\w-]+\.json$")
SHOT_NAME = re.compile(r"^[\w-]+\.png$")
RUN_ID = re.compile(r"^[\w-]+$")


def _oauth_page(ok: bool, message: str) -> str:
    import html
    color = "#3fb950" if ok else "#f85149"
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>NewJev connection</title></head>"
            f"<body style='background:#0d1117;color:#e6edf3;font:15px system-ui;display:flex;align-items:center;"
            f"justify-content:center;height:90vh'><div style='max-width:520px;text-align:center'>"
            f"<div style='font-size:42px;color:{color}'>{'&#10004;' if ok else '&#10008;'}</div>"
            f"<p>{html.escape(message)}</p>{'<script>setTimeout(()=>window.close(),2500)</script>' if ok else ''}"
            f"</div></body></html>")


POPUP_EVENTS = {"confirm_request", "handoff", "takeover_request", "rescue_request"}


def popup_payload(event: str, data: dict) -> dict:
    """What the desktop pop-up shows and which answers it offers (same choices as the cards in the web UI)."""
    kind = data.get("kind")
    titles = {"delete": "\U0001f5d1 Confirm deletion", "send_email": "\u2709 Confirm email",
              "send_message": "\U0001f4ac Confirm message", "send_invite": "\U0001f4c5 Confirm meeting invite",
              "upload": "\u2601 Confirm upload", "risky_action": "\u26a0 Confirm action",
              "low_confidence": "\u2753 Agent is unsure"}
    yes = {"delete": "Delete", "send_email": "Send", "send_message": "Send", "send_invite": "Send",
           "upload": "Upload"}.get(kind, "Allow")
    if event == "handoff":
        title, buttons = "\U0001f64b Your turn", [{"label": "I've done it \u2014 continue", "approved": True, "style": "primary"},
                                              {"label": "Abort run", "approved": False}]
    elif event == "takeover_request":
        title, buttons = "\u23f8 Paused \u2014 you took over", [{"label": "Resume", "approved": True, "style": "primary"},
                                                         {"label": "Stop run", "approved": False}]
    elif event == "rescue_request":
        title, buttons = "\U0001f9ed I'm stuck \u2014 what should I do?", [
            {"label": "Send hint", "approved": True, "use_entry": True, "style": "primary"},
            {"label": "I did it myself", "approved": True, "text": "done", "style": "blue"},
            {"label": "Give up", "approved": False}]
    else:
        title = titles.get(kind, "\u2753 Confirm")
        buttons = [{"label": yes, "approved": True, "style": "danger" if kind == "delete" else "primary"}]
        if kind == "low_confidence":
            buttons.append({"label": "Allow all unsure steps", "approved": True, "remember": True, "style": "blue"})
        buttons.append({"label": "Reject", "approved": False})
    s = get_settings()
    return {"base": f"http://127.0.0.1:{s.port}", "request_id": data.get("request_id"), "title": title,
            "message": str(data.get("message", "")), "buttons": buttons, "hint": event == "rescue_request",
            "timeout_s": data.get("timeout_s", 600)}


def _ui_in_front() -> bool:
    try:
        import ctypes
        user32 = ctypes.windll.user32
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(user32.GetForegroundWindow(), buf, 256)
        return "newjev agent" in buf.value.lower()
    except Exception:
        return False


def desktop_popup(event: str, data: dict) -> None:
    """Pop the request up above the taskbar unless the NewJev window is already in front."""
    import base64
    import os
    import subprocess
    import sys
    s = get_settings()
    if not s.desktop_popups or sys.platform != "win32" or not data.get("request_id") or _ui_in_front():
        return
    payload = base64.b64encode(json.dumps(popup_payload(event, data)).encode("utf-8")).decode()
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    subprocess.Popen([pyw if os.path.exists(pyw) else exe, "-m", "app.ui.notifier", payload],
                     cwd=str(Path(__file__).resolve().parents[2]), creationflags=0x08000000)   # no console window


def _find_reviewer(request_id: str):
    for info in list(sessions.values()):
        r = info.get("reviewer")
        if r is not None and hasattr(r, "is_pending") and r.is_pending(request_id):
            return r
    return None


def ui_busy() -> bool:
    return any(info.get("status") == "running" for info in sessions.values())


def _logs() -> Path:
    return Path(get_settings().log_dir)


_trace_cache: dict[str, tuple[float, dict]] = {}


def trace_summary(path: Path) -> dict | None:
    try:
        mtime = path.stat().st_mtime
        hit = _trace_cache.get(path.name)
        if hit and hit[0] == mtime:
            return hit[1]
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    summ = {
        "name": path.name, "run_id": data.get("run_id"), "started": data.get("started"),
        "status": data.get("status"), "goal": data.get("goal"), "user_request": data.get("user_request") or data.get("goal"),
        "steps": data.get("steps"), "elapsed_ms": data.get("elapsed_ms"),
        "cost_usd": (data.get("usage") or {}).get("cost_usd"), "phases": len(data.get("phases") or []),
        "rescues": len(data.get("rescues") or []), "shots": len(data.get("shots") or []),
        "mtime": mtime,
    }
    _trace_cache[path.name] = (mtime, summ)
    return summ


def _executor(session_id: str) -> ThreadPoolExecutor:
    """One worker thread per session: Playwright's sync API must always be used from the thread that created it."""
    session = sessions.get(session_id)
    if session is None:
        raise RuntimeError("Session is closed.")
    if session.get("executor") is None:
        session["executor"] = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"agent-{session_id}")
    return session["executor"]


async def _in_session_thread(session_id: str, fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_executor(session_id), fn, *args)


def get_planner() -> Planner:
    global _planner
    if _planner is None:
        _planner = Planner()
    return _planner


def create_app() -> FastAPI:
    app = FastAPI(title="NewJev Agent", version="0.2.0")

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        html_path = STATIC_DIR / "index.html"
        if html_path.exists():
            return HTMLResponse(html_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>NewJev Agent</h1><p>Static files not found.</p>")

    @app.get("/popup", response_class=HTMLResponse)
    async def popup_page():
        return HTMLResponse((STATIC_DIR / "popup.html").read_text(encoding="utf-8"))

    @app.get("/quick", response_class=HTMLResponse)
    async def quick_box():
        return HTMLResponse((STATIC_DIR / "quick.html").read_text(encoding="utf-8"))

    @app.post("/api/quick_run")
    async def quick_run(data: dict = Body(...)):
        """From the Ctrl+Alt+J box: start the goal in the open NewJev window (or steer the run in progress)."""
        goal = str(data.get("goal") or "").strip()[:2000]
        if not goal:
            raise HTTPException(400, "no goal")
        running = next((sid for sid, info in sessions.items() if info.get("status") == "running"), None)
        if running and running in manager.active:
            info = sessions[running]
            with info.setdefault("steer_lock", threading.Lock()):
                info.setdefault("steer", []).append(goal)
            await manager.send(running, "info", {"message": f"From the quick box: {goal[:200]}"})
            return JSONResponse({"delivered": "steer", "message": "Sent to the running task as an instruction."})
        if manager.active:
            sid = list(manager.active)[-1]
            await manager.send(sid, "quick_goal", {"goal": goal})
            return JSONResponse({"delivered": "ui", "message": "Started in the NewJev window."})
        s = get_settings()
        import urllib.parse
        import webbrowser
        webbrowser.open(f"http://{s.host}:{s.port}/?goal={urllib.parse.quote(goal)}")
        return JSONResponse({"delivered": "opened", "message": "Opening NewJev to run it…"})

    # ------------------------------------------------------------ answers from the desktop pop-up
    @app.get("/api/requests/{request_id}")
    async def request_state(request_id: str):
        return JSONResponse({"pending": _find_reviewer(request_id) is not None})

    @app.post("/api/respond")
    async def respond_api(data: dict = Body(...)):
        rid = str(data.get("request_id") or "")
        r = _find_reviewer(rid)
        if r is None:
            raise HTTPException(404, "request already answered or expired")
        r.respond(rid, bool(data.get("approved")), bool(data.get("remember")), data.get("text"))
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------ connections (one-click sign-in)
    @app.get("/api/connections")
    async def connections_status():
        from .. import connections
        return JSONResponse({"connections": connections.status()})

    @app.get("/oauth/{provider}/start")
    async def oauth_start(provider: str):
        from fastapi.responses import RedirectResponse
        from .. import connections
        try:
            return RedirectResponse(connections.start(provider, get_settings().port))
        except connections.ConnectError as exc:
            return HTMLResponse(_oauth_page(False, str(exc)), status_code=400)

    @app.get("/oauth/{provider}/callback")
    async def oauth_callback(provider: str, code: str = "", state: str = "", error: str = "",
                             error_description: str = ""):
        from .. import connections
        if error or not code:
            return HTMLResponse(_oauth_page(False, error_description or error or "sign-in was cancelled"))
        try:
            what = await asyncio.to_thread(connections.finish, provider, code, state)
        except connections.ConnectError as exc:
            return HTMLResponse(_oauth_page(False, str(exc)))
        await manager.broadcast("connections_changed", {"provider": provider, "connected": True, "what": what})
        return HTMLResponse(_oauth_page(True, f"Connected: {what}. You can close this tab and go back to NewJev."))

    @app.post("/api/connections/{provider}/disconnect")
    async def oauth_disconnect(provider: str):
        from .. import connections
        if provider not in ("slack", "microsoft", "google"):
            raise HTTPException(404, "unknown connection")
        done = connections.disconnect(provider)
        await manager.broadcast("connections_changed", {"provider": provider, "connected": False})
        return JSONResponse({"disconnected": done})

    # ------------------------------------------------------------ tools & capability registry
    @app.get("/api/tools")
    async def list_tools():
        from ..tools import get_registry
        return JSONResponse(get_registry().export_manifest())

    @app.post("/api/tools/execute")
    async def execute_tool(data: dict = Body(...)):
        from ..tools import ToolDispatcher
        name = str(data.get("name") or "")
        arguments = data.get("arguments") or {}
        confirmed = bool(data.get("confirmed", False))
        confirm_fn = (lambda *a: True) if confirmed else None
        dispatcher = ToolDispatcher(confirm_callback=confirm_fn)
        session_id = data.get("session_id")
        context = {}
        if session_id and session_id in sessions:
            caps = sessions[session_id].get("captures")
            if caps:
                context["browser_capture"] = caps.get("browser")
        res = dispatcher.dispatch(name, arguments, context=context)
        return JSONResponse(res.to_dict())

    # ------------------------------------------------------------ goal understanding
    @app.post("/api/goals/understand")
    async def understand_goal_api(data: dict = Body(...)):
        goal = str(data.get("goal") or "").strip()
        if not goal:
            raise HTTPException(400, "Goal is required")
        planner = get_planner()
        structured = await planner.aunderstand(goal)
        return JSONResponse(structured.to_dict())

    # ------------------------------------------------------------ memory & preferences
    @app.get("/api/preferences")
    async def get_preferences_api():
        from ..agent.memory import get_memory
        return JSONResponse({"preferences": get_memory().get_preferences()})

    @app.post("/api/preferences")
    async def set_preference_api(data: dict = Body(...)):
        from ..agent.memory import get_memory
        key = str(data.get("key") or "")
        value = data.get("value")
        if not key:
            raise HTTPException(400, "Key is required")
        try:
            get_memory().set_preference(key, value)
            return JSONResponse({"ok": True, "key": key, "value": value})
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/api/preferences/{key}")
    async def delete_preference_api(key: str):
        from ..agent.memory import get_memory
        forgotten = get_memory().forget_preference(key)
        return JSONResponse({"ok": True, "forgotten": forgotten})

    @app.get("/api/knowledge")
    async def list_knowledge_api():
        from ..agent.memory import get_memory
        return JSONResponse({"sources": get_memory().list_knowledge_sources()})

    @app.post("/api/knowledge/index")
    async def index_knowledge_api(data: dict = Body(...)):
        from ..agent.memory import get_memory
        path = str(data.get("path") or "")
        tags = str(data.get("tags") or "")
        ok = get_memory().add_to_knowledge_index(path, tags)
        if not ok:
            raise HTTPException(400, f"Could not index file '{path}'")
        return JSONResponse({"ok": True, "path": path})

    @app.delete("/api/knowledge")
    async def remove_knowledge_api(data: dict = Body(...)):
        from ..agent.memory import get_memory
        path = str(data.get("path") or "")
        ok = get_memory().remove_knowledge_source(path)
        return JSONResponse({"ok": True, "removed": ok})

    @app.get("/health")
    async def health():
        s = get_settings()
        try:
            from ..agent.takeover import get_watcher
            w = get_watcher(start=False)
            takeover_status = w.status() if w else {"mode": "off"}
        except Exception:
            takeover_status = {"mode": "error"}
        return JSONResponse({
            "status": "ok",
            "takeover": takeover_status,
            "jev_model": s.jev_model,
            "text_llm": f"{s.text_llm_provider}/{s.text_llm_model}",
            "planner_llm": f"{s.planner_llm_provider}/{s.planner_llm_model}",
            "headless": s.headless,
            "capture_mode": s.capture_mode,
        })

    @app.get("/config")
    async def config():
        s = get_settings()
        return JSONResponse({
            "jev_model": s.jev_model,
            "jev_confidence_threshold": s.jev_confidence_threshold,
            "confirm_low_confidence": s.confirm_low_confidence,
            "verify_done": s.verify_done,
            "confirm_risky_actions": s.confirm_risky_actions,
            "text_llm_provider": s.text_llm_provider,
            "text_llm_model": s.text_llm_model,
            "planner_llm_provider": s.planner_llm_provider,
            "planner_llm_model": s.planner_llm_model,
            "headless": s.headless,
            "capture_mode": s.capture_mode,
            "max_steps": s.max_steps,
            "phase_max_steps": s.phase_max_steps,
            "browser_type": s.browser_type,
            "text_llm_fallbacks": s.text_llm_fallbacks,
            "planner_llm_fallbacks": s.planner_llm_fallbacks,
            "rescue_enabled": s.rescue_enabled,
            "rescue_llm": s.rescue_llm or "(planner)",
            "recipes_enabled": s.recipes_enabled,
            "vision_fallback": s.vision_fallback,
            "vision_llm": s.vision_llm,
            "shortcuts_enabled": s.shortcuts_enabled,
            "scheduler_enabled": s.scheduler_enabled,
        })

    # ------------------------------------------------------------ run history
    @app.get("/api/runs")
    async def list_runs(limit: int = 60):
        files = sorted(_logs().glob("trace_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:max(1, min(limit, 500))]
        return JSONResponse({"runs": [r for r in (trace_summary(p) for p in files) if r]})

    @app.get("/api/runs/{name}")
    async def get_run(name: str):
        if not TRACE_NAME.match(name) or not (_logs() / name).is_file():
            raise HTTPException(404, "run not found")
        return JSONResponse(json.loads((_logs() / name).read_text(encoding="utf-8")))

    @app.get("/api/shots/{run_id}/{file}")
    async def get_shot(run_id: str, file: str):
        path = _logs() / "shots" / run_id / file
        if not RUN_ID.match(run_id) or not SHOT_NAME.match(file) or not path.is_file():
            raise HTTPException(404, "screenshot not found")
        return FileResponse(path, media_type="image/png")

    # ------------------------------------------------------------ long jobs
    @app.get("/api/jobs")
    async def list_jobs():
        from ..tasks.jobs import JobStore
        return JSONResponse({"jobs": JobStore(get_settings().jobs_path).list()})

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str):
        from ..tasks.jobs import JobError, JobStore
        try:
            return JSONResponse(JobStore(get_settings().jobs_path).load(job_id))
        except JobError:
            raise HTTPException(404, "job not found")

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        from ..tasks.jobs import JobError, JobStore
        st = JobStore(get_settings().jobs_path)
        try:
            job = st.load(job_id)
        except JobError:
            raise HTTPException(404, "job not found")
        job["status"] = "cancelled"
        st.save(job)
        return JSONResponse({"cancelled": True})

    # ------------------------------------------------------------ learned recipes
    @app.get("/api/recipes")
    async def list_recipes():
        from ..agent.recipes import RecipeStore
        return JSONResponse(RecipeStore(get_settings().recipes_path).all())

    @app.delete("/api/recipes/{recipe_id}")
    async def delete_recipe(recipe_id: str):
        from ..agent.recipes import RecipeStore
        return JSONResponse({"deleted": RecipeStore(get_settings().recipes_path).delete(recipe_id)})

    # ------------------------------------------------------------ saved tasks
    def _store() -> TaskStore:
        return TaskStore(get_settings().tasks_path)

    @app.get("/api/tasks")
    async def list_tasks():
        cur = _scheduler.current if _scheduler else None
        return JSONResponse({"tasks": _store().list(), "running": cur,
                             "scheduler": bool(_scheduler)})

    @app.post("/api/tasks")
    async def save_task(data: dict = Body(...)):
        try:
            return JSONResponse(_store().upsert(data))
        except TaskError as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: str):
        return JSONResponse({"deleted": _store().delete(task_id)})

    @app.post("/api/tasks/{task_id}/render")
    async def render_task(task_id: str, data: dict = Body(default={})):
        task = _store().get(task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        try:
            goal = render(task["template"], {**task.get("params", {}), **(data.get("params") or {})})
        except TaskError as exc:
            raise HTTPException(400, str(exc))
        return JSONResponse({"goal": goal, "url": task.get("url") or None, "mode": task.get("mode", "auto")})

    @app.post("/api/tasks/{task_id}/run_background")
    async def run_task_background(task_id: str, data: dict = Body(default={})):
        task = _store().get(task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        if _scheduler is None:
            raise HTTPException(400, "the scheduler is off (SCHEDULER_ENABLED=false)")
        try:
            goal = _scheduler.run_now(task, data.get("params") or {})
        except TaskError as exc:
            raise HTTPException(400, str(exc))
        return JSONResponse({"queued": True, "goal": goal})

    @app.post("/api/tasks/stop")
    async def stop_background():
        return JSONResponse({"stopping": bool(_scheduler and _scheduler.stop_current())})

    @app.on_event("startup")
    async def _start_scheduler():
        global _scheduler, _main_loop
        _main_loop = asyncio.get_running_loop()
        s = get_settings()
        if s.hotkey_enabled:
            try:
                from .hotkey import start_hotkey
                start_hotkey(f"http://{s.host}:{s.port}", s.hotkey)
            except Exception as exc:
                logger.warning("hotkey unavailable: %s", exc)
        if s.takeover_pause:
            try:
                from ..agent.takeover import get_watcher
                get_watcher()          # start watching early so the first run can be paused too
            except Exception:
                pass
        if not s.scheduler_enabled or _scheduler is not None:
            return
        from ..tasks.scheduler import Scheduler

        def notify(event: str, data: Any) -> None:
            if _main_loop is not None:
                asyncio.run_coroutine_threadsafe(manager.broadcast(event, data), _main_loop)

        def runner(task: dict, goal: str, trigger: str) -> dict:
            from ..tasks.runner import run_request

            def emit(event: str, data: Any) -> None:
                if event in ("status", "warning", "rescue", "job_progress"):
                    notify("task_event", {"task": task["name"], "event": event, "data": data})

            return run_request(goal, task.get("url") or None, task.get("mode"), emit=emit,
                            reviewer=AutoReviewer(approve=bool(task.get("allow_risky"))),
                            should_stop=lambda: bool(_scheduler and _scheduler.stop_requested))

        _scheduler = Scheduler(TaskStore(s.tasks_path), runner, busy=ui_busy, notify=notify,
                               poll_s=s.scheduler_poll_s)
        _scheduler.start()
        logger.info("Task scheduler started (%s)", s.tasks_path)

    @app.on_event("shutdown")
    async def _stop_scheduler():
        if _scheduler is not None:
            _scheduler.shutdown()

    @app.get("/sessions")
    async def list_sessions():
        return JSONResponse({"sessions": [
            {"id": sid, "goal": info.get("goal"), "status": info.get("status")} for sid, info in sessions.items()
        ]})

    def _close_session_captures(session_id: str) -> None:
        session = sessions.get(session_id)
        if session and session.get("captures"):
            try:
                session["captures"].close()
            except Exception:
                pass
            session["captures"] = None

    @app.websocket("/ws/{session_id}")
    async def websocket_endpoint(websocket: WebSocket, session_id: str):
        await manager.connect(session_id, websocket)
        logger.info("WebSocket connected: %s", session_id)
        try:
            s = get_settings()
            await manager.send(session_id, "config", {
                "jev_model": s.jev_model,
                "text_llm": f"{s.text_llm_provider}/{s.text_llm_model}",
                "planner_llm": f"{s.planner_llm_provider}/{s.planner_llm_model}",
                "headless": s.headless,
                "capture_mode": s.capture_mode,
            })
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await manager.send(session_id, "error", {"message": "Invalid JSON"})
                    continue
                cmd = msg.get("command")

                if cmd == "run":
                    goal = (msg.get("goal") or "").strip()
                    if not goal:
                        await manager.send(session_id, "error", {"message": "No goal provided"})
                        continue
                    if sessions.get(session_id, {}).get("status") == "running":
                        await manager.send(session_id, "error", {"message": "A task is already running. Stop it first."})
                        continue
                    if _scheduler is not None and _scheduler.current:
                        await manager.send(session_id, "error", {"message":
                            f"A scheduled task is running ({_scheduler.current['task']}). Wait or stop it in Tasks."})
                        continue
                    if goal.lower() in CLOSE_WORDS:
                        sessions.setdefault(session_id, {})
                        await _in_session_thread(session_id, _close_session_captures, session_id)
                        await manager.send(session_id, "status", {"status": "closed", "message": "Browser / application session closed."})
                        continue
                    asyncio.create_task(_run_agent(session_id, goal, msg.get("url") or None, msg.get("capture_mode") or None))

                elif cmd == "respond":
                    reviewer = sessions.get(session_id, {}).get("reviewer")
                    ok = bool(reviewer and reviewer.respond(str(msg.get("request_id")), bool(msg.get("approved")),
                                                            bool(msg.get("remember")), msg.get("text")))
                    if not ok:
                        await manager.send(session_id, "warning", {"message": "That request has already expired."})

                elif cmd == "close":
                    if sessions.get(session_id, {}).get("status") == "running":
                        await manager.send(session_id, "warning", {"message": "Stop the running task before closing the window."})
                        continue
                    sessions.setdefault(session_id, {})
                    await _in_session_thread(session_id, _close_session_captures, session_id)
                    await manager.send(session_id, "status", {"status": "closed", "message": "Browser / application session closed."})

                elif cmd == "stop":
                    session = sessions.get(session_id)
                    if session:
                        session["stop_requested"] = True
                        if session.get("reviewer"):
                            session["reviewer"].cancel_all()
                    await manager.send(session_id, "status", {"status": "stopping", "message": "Stop requested."})

                elif cmd == "resume_job":
                    if sessions.get(session_id, {}).get("status") == "running":
                        await manager.send(session_id, "error", {"message": "A task is already running. Stop it first."})
                        continue
                    from ..tasks.jobs import JobError, JobStore
                    try:
                        job = JobStore(get_settings().jobs_path).load(str(msg.get("job_id")))
                    except JobError as exc:
                        await manager.send(session_id, "error", {"message": str(exc)})
                        continue
                    sessions.setdefault(session_id, {})
                    asyncio.create_task(_run_job(session_id, job))

                elif cmd == "steer":
                    text = str(msg.get("text") or "").strip()
                    session = sessions.get(session_id)
                    if not text:
                        continue
                    if not session or session.get("status") != "running":
                        await manager.send(session_id, "warning", {"message": "No run in progress to steer."})
                        continue
                    with session.setdefault("steer_lock", threading.Lock()):
                        session.setdefault("steer", []).append(text[:1000])
                    await manager.send(session_id, "info", {"message": f"Got it — will apply: {text[:200]}"})

                elif cmd == "ping":
                    await manager.send(session_id, "pong", {})

                else:
                    await manager.send(session_id, "error", {"message": f"Unknown command: {cmd}"})

        except WebSocketDisconnect:
            logger.info("WebSocket disconnected: %s", session_id)
        finally:
            manager.disconnect(session_id)
            session = sessions.pop(session_id, None)
            if session:
                session["stop_requested"] = True
                if session.get("reviewer"):
                    session["reviewer"].cancel_all()
                ex = session.get("executor")
                if ex is not None:
                    caps = session.get("captures")
                    if caps:
                        ex.submit(caps.close)
                    ex.shutdown(wait=False)

    async def _run_agent(session_id: str, goal: str, url: str | None, capture_mode: str | None) -> None:
        """Plan, then run the agent in a worker thread, streaming events over the WebSocket."""
        s = get_settings()
        session = sessions.setdefault(session_id, {})
        session.update(goal=goal, status="running", stop_requested=False, steer=[], steer_lock=threading.Lock())
        tracker = usage_mod.UsageTracker()
        usage_mod._current.set(tracker)     # this asyncio task's context: the planner call is counted too

        await manager.send(session_id, "status", {"status": "planning", "message": "Analysing goal..."})
        forced_mode = capture_mode if capture_mode in ("browser", "desktop") else None

        from ..tasks.jobs import JobError, looks_like_job, plan_job
        if looks_like_job(goal):
            try:
                job = await asyncio.to_thread(plan_job, goal)
            except JobError as exc:
                await manager.send(session_id, "error", {"message": f"Could not set up the job: {exc}"})
                session["status"] = "idle"
                return
            if job is not None:
                await _run_job(session_id, job, url, forced_mode)
                return

        context = {
            "active_url": session.get("last_url"),
            "last_goal": session.get("last_goal"),
            "last_report": session.get("last_report"),
        }
        # If browser capture is already active in session, grab its current URL
        if not context["active_url"] and "captures" in session:
            try:
                b_cap = session["captures"]._captures.get("browser")
                if b_cap and hasattr(b_cap, "_page") and b_cap._page and not b_cap._page.is_closed():
                    context["active_url"] = b_cap._page.url
            except Exception:
                pass

        try:
            plan = await get_planner().aplan(goal, context=context)
        except Exception as exc:
            logger.warning("Planner failed (%s); continuing with raw goal.", exc)
            from ..agent.planner import fallback_plan
            plan = fallback_plan(goal, url, forced_mode or (s.capture_mode if s.capture_mode != "auto" else None))
            await manager.send(session_id, "warning", {"message": f"Planner unavailable ({exc}); running the goal directly."})

        if session_id not in sessions or session.get("stop_requested"):
            logger.info("Session %s closed or stopped during planning; not starting the run.", session_id)
            session["status"] = "idle"
            return

        steps = plan["steps"]
        if forced_mode:  # user picked a mode explicitly in the UI
            for st in steps:
                st["mode"] = forced_mode
        if url:
            steps[0]["url"] = url
        elif not steps[0].get("url") and (session.get("last_url") or context.get("active_url")):
            # Auto-inherit the active page URL if referring to the page
            active_page = session.get("last_url") or context.get("active_url")
            if re.search(r"\b(in the page|on the page|this page|that page|from the page|the page|webpage|page)\b", goal, re.I):
                steps[0]["url"] = active_page
                logger.info("Inherited active page URL '%s' for goal '%s'", active_page, goal)
        effective_goal = plan["goal"]
        session["goal"] = effective_goal

        await manager.send(session_id, "plan", {
            "goal": effective_goal,
            "steps": [f"[{st['mode']}] {st['task']}" for st in steps],
            "url": steps[0].get("url"),
            "capture_mode": steps[0]["mode"] if len({st["mode"] for st in steps}) == 1 else "hybrid",
            "from_recipe": bool(plan.get("from_recipe")),
            "decision": plan.get("decision", "act"),
            "clarification_question": plan.get("clarification_question"),
            "evidence_of_success": plan.get("evidence_of_success", []),
            "constraints": plan.get("constraints", []),
        })
        loop = asyncio.get_running_loop()

        def emit(event: str, data: Any) -> None:
            fut = asyncio.run_coroutine_threadsafe(manager.send(session_id, event, data), loop)
            try:
                fut.result(timeout=5)
            except Exception:
                pass

        def should_stop() -> bool:
            return bool(sessions.get(session_id, {}).get("stop_requested")) or session_id not in sessions

        reviewer = EventReviewer(emit, timeout_s=s.handoff_timeout_s, should_stop=should_stop)
        session["reviewer"] = reviewer

        # 1. Ask-decision flow: if planner returned ask, request user clarification
        if plan.get("decision") == "ask" and plan.get("clarification_question"):
            # Check if clarification can be resolved from active session context before pausing
            q_lower = str(plan["clarification_question"]).lower()
            last_url = session.get("last_url") or context.get("active_url")
            last_goal = session.get("last_goal")
            if (last_url or last_goal) and any(w in q_lower for w in ("which page", "what page", "url", "who is", "who", "page")):
                logger.info("Planner asked '%s' but session has active context (url=%s, last_goal=%s); auto-resolving.",
                            plan["clarification_question"], last_url, last_goal)
                plan["decision"] = "act"
                if last_url and not steps[0].get("url"):
                    steps[0]["url"] = last_url
                effective_goal = f"{effective_goal} (Context: page={last_url or last_goal})"
                session["goal"] = effective_goal
            else:
                question = plan["clarification_question"]
                await manager.send(session_id, "status", {
                    "status": "needs_user",
                    "message": question,
                })
                clarification = await _in_session_thread(session_id, reviewer.hint, question)
                if not clarification:
                    await manager.send(session_id, "result", {
                        "status": "needs_user",
                        "steps": 0,
                        "elapsed_ms": 0,
                        "goal": effective_goal,
                        "final_answer": f"Task paused: {question}",
                        "user_request": goal,
                    })
                    session["status"] = "idle"
                    session["reviewer"] = None
                    return
                effective_goal = f"{effective_goal} (Clarification: {clarification})"
                session["goal"] = effective_goal

        # 2. Router & ToolLoop: if direct mode, direct tool, or inspection recommended
        from ..tools.router import ActionRouter
        router = ActionRouter()
        route = router.route(effective_goal)
        use_tool_loop = (not forced_mode) and (
            plan.get("recommended_mode") == "direct"
            or plan.get("decision") == "inspect"
            or route.route_type in ("direct_tool", "inspect_first")
        )

        if use_tool_loop:
            def tool_worker():
                from ..tools.loop import ToolLoop
                from ..tools.dispatcher import ToolDispatcher
                from ..tools.registry import get_registry

                dispatcher = ToolDispatcher(
                    registry=get_registry(),
                    confirm_callback=lambda kind, summary, args: reviewer.confirm(kind, summary, args),
                )
                tool_loop = ToolLoop(dispatcher=dispatcher, router=router)
                return tool_loop.run(
                    goal=effective_goal,
                    evidence_required=plan.get("evidence_of_success"),
                    context={"session_id": session_id, "url": url},
                    emit_callback=emit,
                )

            tool_res = await _in_session_thread(session_id, tool_worker)
            if tool_res.status in ("done", "needs_user", "unverified"):
                await manager.send(session_id, "result", {
                    "status": tool_res.status,
                    "steps": len(tool_res.steps),
                    "elapsed_ms": 0,
                    "goal": effective_goal,
                    "final_answer": tool_res.final_answer or tool_res.clarification_question,
                    "evidence": tool_res.evidence,
                    "user_request": goal,
                })
                session["status"] = "idle"
                session["reviewer"] = None
                return
            logger.info("ToolLoop finished with status '%s'; continuing with AgentLoop.", tool_res.status)

        def steer() -> list[str]:
            with session.setdefault("steer_lock", threading.Lock()):
                msgs, session["steer"] = list(session.get("steer") or []), []
            return msgs

        def worker() -> dict:
            captures = session.get("captures") or CaptureManager()
            session["captures"] = captures
            with tracker.activate(), AgentLoop(emit=emit, reviewer=reviewer, should_stop=should_stop,
                                               steer=steer) as agent:
                if len(steps) == 1:
                    cap = captures.get(steps[0]["mode"], steps[0].get("url"))
                    return agent.run(steps[0]["task"] if steps[0].get("existing_file") else effective_goal, cap, None,
                                     user_request=goal)
                return agent.run_plan(steps, effective_goal, captures, None, user_request=goal)

        try:
            result = await _in_session_thread(session_id, worker)
            from ..agent.planner import remember_plan
            remember_plan(goal, plan, result)

            # Persist run to MemoryStore & session context for cross-run memory
            try:
                from ..agent.memory import get_memory
                mem = get_memory()
                last_url = next((h.get("url") for h in reversed(result.get("history", [])) if h.get("url")), None)
                if not last_url and steps and steps[0].get("url"):
                    last_url = steps[0].get("url")
                mem.record_run(
                    run_id=result.get("run_id", ""),
                    goal=goal,
                    status=result.get("status", ""),
                    summary=result.get("final_report") or "",
                    metadata={"url": last_url or "", "steps": result.get("steps", 0)},
                )
                session["last_url"] = last_url
                session["last_goal"] = goal
                session["last_report"] = result.get("final_report")
            except Exception as _mem_err:
                logger.debug("Memory record_run failed: %s", _mem_err)

            await manager.send(session_id, "result", {
                "status": result["status"], "steps": result["steps"], "elapsed_ms": result["elapsed_ms"],
                "goal": result["goal"], "phases": result.get("phases", []), "usage": result.get("usage", {}),
                "rescue_stats": result.get("rescue_stats", {}), "run_id": result.get("run_id"),
                "trace": Path(result.get("trace_path", "")).name or None, "user_request": goal,
                "recipes_used": result.get("recipes_used", []),
                "final_report": result.get("final_report"),
            })
        except Exception as exc:
            logger.exception("Agent run failed for session %s", session_id)
            await manager.send(session_id, "error", {"message": str(exc)})
        finally:
            if session_id in sessions:
                sessions[session_id]["status"] = "idle"
                sessions[session_id]["reviewer"] = None

    def _session_tools(session_id: str, session: dict, loop: asyncio.AbstractEventLoop):
        s = get_settings()

        def emit(event: str, data: Any) -> None:
            fut = asyncio.run_coroutine_threadsafe(manager.send(session_id, event, data), loop)
            try:
                fut.result(timeout=5)
            except Exception:
                pass

        def should_stop() -> bool:
            return bool(sessions.get(session_id, {}).get("stop_requested")) or session_id not in sessions

        reviewer = EventReviewer(emit, timeout_s=s.handoff_timeout_s, should_stop=should_stop)
        session["reviewer"] = reviewer

        def steer() -> list[str]:
            with session.setdefault("steer_lock", threading.Lock()):
                msgs, session["steer"] = list(session.get("steer") or []), []
            return msgs

        return emit, should_stop, reviewer, steer

    async def _run_job(session_id: str, job: dict, url: str | None = None, mode: str | None = None) -> None:
        """Run / resume a long job item by item in the session's worker thread."""
        from ..tasks.jobs import JobStore, progress, run_job
        from ..tasks.runner import run_goal
        s = get_settings()
        session = sessions.setdefault(session_id, {})
        session.update(goal=job["request"], status="running", stop_requested=False, job_id=job["id"])
        store = JobStore(s.jobs_path)
        store.save(job)
        pr = progress(job)
        await manager.send(session_id, "job", {"job_id": job["id"], "request": job["request"],
                                               "per_item": job["per_item"], "after": job.get("after"),
                                               "items": [i["label"] for i in job["items"]][:200], **pr})
        emit, should_stop, reviewer, steer = _session_tools(session_id, session, asyncio.get_running_loop())
        started = time.perf_counter()

        def worker() -> dict:
            captures = session.get("captures") or CaptureManager()
            session["captures"] = captures

            def run_item(goal: str, item: dict) -> dict:
                emit("status", {"status": "running", "message": f"Job item: {item.get('label')}"})
                return run_goal(goal, url, mode, emit=emit, reviewer=reviewer, should_stop=should_stop,
                                steer=steer, captures=captures)

            return run_job(job, store, run_item, emit=emit, should_stop=should_stop)

        try:
            done = await _in_session_thread(session_id, worker)
            pr = progress(done)
            await manager.send(session_id, "result", {
                "status": "done" if done["status"] == "done" else done["status"],
                "steps": sum(int(i.get("steps") or 0) for i in done["items"]),
                "elapsed_ms": round((time.perf_counter() - started) * 1000), "goal": job["request"],
                "phases": [{"task": i["label"], "mode": "job", "status": i["status"]} for i in done["items"]][:100],
                "user_request": job["request"], "job_id": job["id"], "job": pr,
            })
        except Exception as exc:
            logger.exception("Job failed for session %s", session_id)
            await manager.send(session_id, "error", {"message": str(exc)})
        finally:
            if session_id in sessions:
                sessions[session_id]["status"] = "idle"
                sessions[session_id]["reviewer"] = None

    return app
