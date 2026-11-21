"""NewJev — entry point. Run with: python run.py"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import webbrowser
from pathlib import Path


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def load_env() -> None:
    """Load .env file if it exists."""
    env_path = Path(".env")
    if env_path.exists():
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=env_path, override=True)
    else:
        print("[WARNING] .env not found. Copy .env.example to .env and fill in your keys.")


def run_server(host: str, port: int, auto_open: bool) -> None:
    """Launch the FastAPI web UI server."""
    import uvicorn
    from app.ui.server import create_app

    app = create_app()
    url = f"http://{host}:{port}"
    print(f"\n  \u26A1 NewJev Agent running at {url}")
    print(f"  Press Ctrl+C to stop.\n")

    if auto_open:
        import threading
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=host, port=port, log_level="warning")


def run_cli(goal: str, url: str | None, capture_mode: str | None, auto_approve: bool = False) -> dict:
    """Run the agent from the terminal (no web UI). Model time / cost include the planner call."""
    from app.llm.usage import UsageTracker
    with UsageTracker().activate():
        return _run_cli(goal, url, capture_mode, auto_approve)


def _run_cli(goal: str, url: str | None, capture_mode: str | None, auto_approve: bool = False) -> dict:
    from rich.console import Console
    from rich.panel import Panel
    from app.agent.loop import AgentLoop
    from app.agent.planner import Planner
    from app.agent.review import ConsoleReviewer
    from app.executor import CaptureManager

    console = Console()
    console.print(Panel(f"[bold cyan]Goal:[/] {goal}", title="NewJev Agent", border_style="blue"))

    from app.tasks.jobs import looks_like_job
    if looks_like_job(goal):
        from app.tasks.runner import run_request

        def show(event, data):
            if event == "job_progress" and data.get("item"):
                console.print(f"[cyan][{data.get('index')}/{data.get('total')}] {data.get('state')}:[/cyan] {data.get('item')}")
        result = run_request(goal, url, capture_mode, emit=show, reviewer=ConsoleReviewer(auto_approve=auto_approve))
        console.print(Panel(f"Status: [bold]{result['status']}[/bold]\nJob: {result.get('job_id', '-')} "
                            f"{result.get('job', {})}", title="Result"))
        return result

    console.print("[yellow]Planning...[/yellow]")
    try:
        plan = Planner().plan(goal)
    except Exception as exc:
        console.print(f"[yellow]Planner unavailable ({exc}); running the goal directly.[/yellow]")
        from app.agent.planner import fallback_plan
        plan = fallback_plan(goal, url, capture_mode)
    steps = plan["steps"]
    decision = plan.get("decision", "act")
    if decision == "ask" and plan.get("clarification_question"):
        console.print(f"[bold yellow]Clarification Recommended:[/] {plan['clarification_question']}")
    if plan.get("evidence_of_success"):
        console.print(f"[dim]Evidence of Success: {', '.join(plan['evidence_of_success'])}[/dim]")
    if capture_mode in ("browser", "desktop"):
        for st in steps:
            st["mode"] = capture_mode
    if url:
        steps[0]["url"] = url
    for i, st in enumerate(steps, 1):
        console.print(f"[green]{i}.[/green] [{st['mode']}] {st['task']}" + (f"  [dim]{st['url']}[/dim]" if st.get("url") else ""))

    def emit(event: str, data: dict) -> None:
        if event == "step":
            conf = data.get("effective_confidence", data.get("confidence", 0)) or 0
            flags = " [red]LOW[/red]" if "low_confidence" in data.get("flags", []) else ""
            console.print(
                f"  [dim]Step {data.get('step', '?')}[/dim] [bold]{data.get('operation', '')}[/bold] "
                f"[dim]{str(data.get('label', ''))[:60]}[/dim] [yellow]{conf * 100:.0f}%[/yellow]{flags} "
                f"[dim]{data.get('latency_ms', 0)}ms[/dim]"
            )
        elif event == "phase":
            if data.get("total"):
                console.print(f"[cyan]Phase {data['index']}/{data['total']} [{data['mode']}]:[/cyan] {data['task']}")
            else:
                console.print(f"[cyan]{data['task']}…[/cyan]")
        elif event == "status":
            status, msg = data.get("status", ""), data.get("message", "")
            colors = {"done": "green", "unverified": "yellow", "blocked": "red", "error": "red", "needs_user": "magenta"}
            console.print(f"[{colors.get(status, 'dim')}]{status.upper()}: {msg}[/]")
        elif event in ("warning", "error"):
            console.print(f"[yellow]{event.upper()}: {data.get('message', '')}[/yellow]")
        elif event == "info":
            console.print(f"[blue]{data.get('message', '')}[/blue]")
        elif event == "rescue" and data.get("by") == "llm":
            console.print(f"[magenta]Rescue model: {data.get('kind')} {data.get('why', '')}[/magenta]")
        elif event == "validation":
            console.print(f"  [dim]check: {data.get('note', '')}[/dim]")

    captures = CaptureManager()
    try:
        with AgentLoop(emit=emit, reviewer=ConsoleReviewer(auto_approve=auto_approve)) as agent:
            if len(steps) > 1:
                result = agent.run_plan(steps, plan["goal"], captures, None, user_request=goal)
            else:
                cap = captures.get(steps[0]["mode"], steps[0].get("url"))
                result = agent.run(steps[0]["task"] if steps[0].get("existing_file") else plan["goal"], cap, None,
                                   user_request=goal)
        from app.agent.planner import remember_plan
        remember_plan(goal, plan, result)
        u = result.get("usage") or {}
        models = "\n".join(f"  {m['role']:<8} {m['model']}: {m['calls']} calls, {m['time_ms'] / 1000:.1f}s"
                           + (f", {m['errors']} failed" if m['errors'] else "")
                           + (f", ${m['cost_usd']:.4f}" if m.get('cost_usd') else "") for m in u.get("models", []))
        console.print(Panel(
            f"Status: [bold]{result['status']}[/bold]\nSteps: {result['steps']}\n"
            f"Time: {result['elapsed_ms'] / 1000:.1f}s"
            + (f"\nCost: ${u['cost_usd']:.4f}{'' if u.get('cost_complete') else '+'}" if u.get("cost_usd") else "")
            + (f"\nModels:\n{models}" if models else ""),
            title="Result", border_style="green" if result["status"] == "done" else "red",
        ))
        return result
    finally:
        captures.close()


def run_interactive_cli() -> None:
    """Interactive terminal chat loop."""
    from rich.console import Console
    from rich.prompt import Prompt
    console = Console()
    console.print("[bold cyan]\u26A1 NewJev Interactive Agent[/bold cyan]")
    console.print("Type a goal and press Enter. Type 'quit' to exit.\n")
    while True:
        try:
            goal = Prompt.ask("[bold]Goal[/bold]")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Bye![/yellow]")
            break
        if goal.lower() in ("quit", "exit", "q"):
            break
        if not goal.strip():
            continue
        url = Prompt.ask("Start URL (optional)", default="")
        mode = Prompt.ask("Capture mode", choices=["browser", "desktop", "auto"], default="auto")
        run_cli(goal, url or None, mode)
        console.print()


def main() -> None:
    load_env()

    from app.config import get_settings
    s = get_settings()
    setup_logging(s.log_level)

    parser = argparse.ArgumentParser(
        description="NewJev — Computer Use Agent powered by Jev",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python run.py                          # Launch web UI (default)
  python run.py --cli                    # Interactive terminal chat
  python run.py --task "Open Google"     # Run single task headlessly
  python run.py --task "..." --url https://google.com --mode browser
  python run.py --list-tasks                                  # saved tasks (create them in the web UI)
  python run.py --saved "Import CSV" --param file=C:\\data\\a.csv
        """,
    )
    parser.add_argument("--cli", action="store_true", help="Interactive terminal mode (no web UI)")
    parser.add_argument("--task", type=str, default=None, help="Run a single task and exit")
    parser.add_argument("--url", type=str, default=None, help="Start URL for the task")
    parser.add_argument("--mode", type=str, default=None,
                        choices=["browser", "desktop", "auto"], help="Capture mode")
    parser.add_argument("--host", type=str, default=s.host, help="Web UI host")
    parser.add_argument("--port", type=int, default=s.port, help="Web UI port")
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open browser")
    parser.add_argument("--yes", action="store_true", help="Auto-approve risky / low-confidence confirmations")
    parser.add_argument("--saved", type=str, default=None, help="Run a saved task by name or id")
    parser.add_argument("--param", action="append", default=[], help="Value for a {placeholder}: name=value")
    parser.add_argument("--list-tasks", action="store_true", help="List saved tasks")
    parser.add_argument("--jobs", action="store_true", help="List long jobs and their progress")
    parser.add_argument("--resume-job", type=str, default=None, help="Resume a long job by id")

    args = parser.parse_args()

    if args.jobs:
        from app.tasks.jobs import JobStore
        for j in JobStore(s.jobs_path).list():
            print(f"{j['id']}  {j['status']:<18} {j['done']}/{j['total']} done, {j['failed']} failed   {j['request']}")
        sys.exit(0)
    if args.resume_job:
        from app.agent.review import ConsoleReviewer
        from app.tasks.runner import resume_job

        def show(event, data):
            if event == "job_progress" and data.get("item"):
                print(f"[{data.get('index')}/{data.get('total')}] {data.get('state')}: {data.get('item')}")
        job = resume_job(args.resume_job, emit=show, reviewer=ConsoleReviewer(auto_approve=args.yes))
        print(f"Job {job['id']}: {job['status']}")
        sys.exit(0 if job["status"] == "done" else 1)

    if args.list_tasks or args.saved:
        from app.tasks import TaskError, TaskStore, render
        store = TaskStore(s.tasks_path)
        if args.list_tasks:
            for t in store.list():
                print(f"{t['id']}  {t['name']:<30} {t['template']}"
                      + (f"   [{t['schedule']}]" if t.get("schedule") else "")
                      + (f"   [watch {t['watch_folder']}]" if t.get("watch_folder") else ""))
            sys.exit(0)
        task = store.get(args.saved)
        if task is None:
            print(f"No saved task named {args.saved!r}. Use --list-tasks.")
            sys.exit(2)
        params = dict(task.get("params", {}))
        for kv in args.param:
            k, _, v = kv.partition("=")
            params[k.strip()] = v
        try:
            goal = render(task["template"], params)
        except TaskError as exc:
            print(f"Task {task['name']!r}: {exc} (pass --param name=value)")
            sys.exit(2)
        result = run_cli(goal, task.get("url") or args.url, task.get("mode") or args.mode or s.capture_mode,
                         auto_approve=args.yes)
        store.record_run(task["id"], result, "cli")
        sys.exit(0 if result["status"] == "done" else 1)

    if args.task:
        result = run_cli(args.task, args.url, args.mode or s.capture_mode, auto_approve=args.yes)
        sys.exit(0 if result["status"] == "done" else 1)
    elif args.cli:
        run_interactive_cli()
    else:
        run_server(
            host=args.host,
            port=args.port,
            auto_open=s.auto_open_browser and not args.no_open,
        )


if __name__ == "__main__":
    main()
