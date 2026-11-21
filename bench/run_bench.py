"""
NewJev benchmark runner.

    python -m bench.run_bench                      # all tasks that can run on this machine
    python -m bench.run_bench --tags web            # only web tasks
    python -m bench.run_bench --only fixture_shop,notepad_save --repeat 3
    python -m bench.run_bench --interactive         # you handle logins / confirmations
    python -m bench.run_bench --tags excel,word     # Office tasks (needs Office installed)
    python -m bench.run_bench --tags office --office-automation   # same, with the Office shortcut on
    python -m bench.run_bench --list

Results go to bench/results/<timestamp>.json and .md. The key numbers:
  success rate  — run finished AND every post-condition check passed
  false DONE    — agent claimed DONE but the checks failed (should be 0)
"""

from __future__ import annotations

import argparse
import datetime
import functools
import http.server
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BENCH_DIR = Path(__file__).resolve().parent
SAFE_PREFIX = "NewJev_Bench_"


def _path(p: str) -> Path:
    from app.capture.desktop import expand_known
    path = Path(expand_known(p))
    if not path.name.startswith(SAFE_PREFIX):
        raise ValueError(f"Benchmarks may only touch files named {SAFE_PREFIX}*: {p}")
    return path


def _serve_fixtures() -> tuple[str, http.server.ThreadingHTTPServer]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(BENCH_DIR / "fixtures"))
    handler.log_message = lambda *a, **k: None  # type: ignore[assignment]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


def _fill(text: str | None, values: dict[str, str]) -> str | None:
    if text is None:
        return None
    for k, v in values.items():
        text = text.replace("{" + k + "}", v)
    return text


def _cleanup(task: dict) -> None:
    for p in task.get("cleanup", []):
        path = _path(p)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()


def _setup(task: dict) -> None:
    for step in task.get("setup", []):
        path = _path(step["path"])
        if step["type"] == "write_file":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(step.get("text", ""), encoding="utf-8")
        elif step["type"] == "make_dir":
            path.mkdir(parents=True, exist_ok=True)
        elif step["type"] == "excel_data":
            from bench.office_checks import make_workbook
            path.parent.mkdir(parents=True, exist_ok=True)
            make_workbook(path, step["rows"])
        elif step["type"] == "word_doc":
            from bench.office_checks import make_document
            path.parent.mkdir(parents=True, exist_ok=True)
            make_document(path, step["paragraphs"])


def _evaluate(task: dict, captures) -> list[dict[str, Any]]:
    out = []
    pages: dict[str, dict] = {}
    for check in task.get("checks", []):
        ctype = check["type"]
        ok = False
        detail = ""
        try:
            if ctype.startswith("excel_") or ctype.startswith("word_"):
                from bench.office_checks import check_excel, check_word
                path = _path(check["path"])
                ok, detail = (check_excel if ctype.startswith("excel_") else check_word)(path, check)
            elif ctype in ("file_contains", "dir_exists", "not_exists"):
                path = _path(check["path"])
                if ctype == "file_contains":
                    ok = path.exists() and check["text"] in path.read_text(encoding="utf-8", errors="ignore")
                elif ctype == "dir_exists":
                    ok = path.is_dir()
                else:
                    ok = not path.exists()
                detail = str(path)
            else:
                mode = check.get("mode", "browser")
                if mode not in pages:
                    cap = captures.peek(mode)
                    pages[mode] = cap.observe() if cap else {}
                page = pages[mode]
                haystack = page.get("url", "") if ctype.startswith("url") else (
                    page.get("text", "") + "\n" + "\n".join(str(a.get("value", "")) for a in page.get("actions", [])))
                if ctype in ("text_contains", "url_contains"):
                    ok = check["text"].lower() in haystack.lower()
                elif ctype == "url_not_contains":
                    ok = bool(haystack) and check["text"].lower() not in haystack.lower()
                elif ctype == "text_contains_any":
                    ok = any(t.lower() in haystack.lower() for t in check["texts"])
                detail = page.get("url", "")
        except Exception as exc:
            detail = f"check error: {exc}"
        out.append({"check": check, "passed": ok, "detail": detail})
    return out


def run_task(task: dict, values: dict[str, str], interactive: bool, use_planner: bool) -> dict[str, Any]:
    from app.agent.loop import AgentLoop
    from app.agent.planner import Planner
    from app.agent.review import AutoReviewer, ConsoleReviewer
    from app.executor import CaptureManager

    goal = _fill(task["goal"], values)
    url = _fill(task.get("url"), values)
    _cleanup(task)
    _setup(task)

    if task.get("mode") and not use_planner:
        from app.agent.planner import start_existing_files_in_explorer
        steps = start_existing_files_in_explorer([{"task": goal, "mode": task["mode"], "url": url}])
    else:
        plan = Planner().plan(goal)
        steps = plan["steps"]
        if url and task.get("mode"):
            steps[0].update(url=url, mode=task["mode"])

    reviewer = ConsoleReviewer() if interactive else AutoReviewer(approve=True, allow_delete_prefix=SAFE_PREFIX)
    captures = CaptureManager()
    started = time.perf_counter()
    result: dict[str, Any] = {}
    try:
        with AgentLoop(reviewer=reviewer) as agent:
            if len(steps) == 1:
                result = agent.run(steps[0]["task"], captures.get(steps[0]["mode"], steps[0].get("url")), None,
                                   user_request=goal)
            else:
                result = agent.run_plan(steps, goal, captures, None, user_request=goal)
        time.sleep(1.0)
        checks = _evaluate(task, captures)
    except Exception as exc:
        result = {"status": "error", "steps": 0, "error": str(exc)}
        checks = [{"check": c, "passed": False, "detail": "not evaluated"} for c in task.get("checks", [])]
    finally:
        captures.close()
        _cleanup(task)

    passed = bool(checks) and all(c["passed"] for c in checks)
    status = result.get("status", "error")
    return {
        "id": task["id"], "tags": task.get("tags", []), "goal": goal, "status": status,
        "checks_passed": passed, "success": passed and status in ("done", "unverified"),
        "false_done": status == "done" and not passed,
        "steps": result.get("steps", 0), "seconds": round(time.perf_counter() - started, 1),
        "phases": result.get("phases", []), "checks": checks, "trace": result.get("trace_path"),
        "error": result.get("error"),
    }


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    ap = argparse.ArgumentParser(description="Run the NewJev benchmark suite")
    ap.add_argument("--only", help="comma-separated task ids")
    ap.add_argument("--tags", help="comma-separated tags (task must have at least one)")
    ap.add_argument("--skip-tags", default="office", help="comma-separated tags to skip (default: office)")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--interactive", action="store_true", help="ask you for logins / confirmations")
    ap.add_argument("--planner", action="store_true", help="always use the planner, even for single-mode tasks")
    ap.add_argument("--office-automation", action="store_true",
                    help="enable the OFFICE_ACTION shortcut (compare with a normal run to measure the gain)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--learn", action="store_true",
                    help="let runs use and update learned recipes (off by default so scores stay comparable)")
    args = ap.parse_args()
    if args.office_automation:
        os.environ["OFFICE_AUTOMATION"] = "true"
    if not args.learn:
        os.environ["RECIPES_ENABLED"] = "false"

    os.chdir(ROOT)
    tasks = json.loads((BENCH_DIR / "tasks.json").read_text(encoding="utf-8"))["tasks"]
    only = set(filter(None, (args.only or "").split(",")))
    tags = set(filter(None, (args.tags or "").split(",")))
    skip = set(filter(None, (args.skip_tags or "").split(",")))
    is_windows = sys.platform == "win32"

    url, srv = _serve_fixtures()
    values = {
        "fixtures": url,
        "date_plus_30": (datetime.date.today() + datetime.timedelta(days=30)).strftime("%B %d, %Y"),
        "email": os.environ.get("NEWJEV_BENCH_EMAIL", ""),
        "stamp": datetime.datetime.now().strftime("%Y%m%d-%H%M"),
    }

    selected, skipped = [], []
    for t in tasks:
        reason = None
        if only and t["id"] not in only:
            continue
        if tags and not tags & set(t.get("tags", [])):
            continue
        if not only and (skip - tags) & set(t.get("tags", [])):
            reason = "skipped tag"
        elif "desktop" in t.get("tags", []) and not is_windows:
            reason = "desktop tasks need Windows"
        elif "email" in t.get("requires", []) and not values["email"]:
            reason = "set NEWJEV_BENCH_EMAIL"
        elif "login" in t.get("tags", []) and not args.interactive and not only:
            reason = "needs --interactive (login)"
        (skipped if reason else selected).append((t, reason))

    if args.list:
        for t, r in selected + skipped:
            print(f"{t['id']:<26} {','.join(t.get('tags', [])):<22} {'SKIP: ' + r if r else ''}")
        return

    results = []
    for t, _ in selected:
        for i in range(args.repeat):
            print(f"\n=== {t['id']} (run {i + 1}/{args.repeat}) ===")
            r = run_task(t, values, args.interactive, args.planner)
            print(f"--> {r['status']}, checks {'PASS' if r['checks_passed'] else 'FAIL'}, {r['steps']} steps, {r['seconds']}s")
            results.append(r)
    srv.shutdown()

    n = len(results)
    ok = sum(r["success"] for r in results)
    false_done = sum(r["false_done"] for r in results)
    summary = {
        "when": datetime.datetime.now().isoformat(timespec="seconds"),
        "runs": n, "success": ok, "success_rate": round(ok / n, 3) if n else None,
        "false_done": false_done,
        "by_tag": {},
        "skipped": [{"id": t["id"], "reason": r} for t, r in skipped],
        "results": results,
    }
    for tag in sorted({tg for r in results for tg in r["tags"]}):
        rs = [r for r in results if tag in r["tags"]]
        summary["by_tag"][tag] = f"{sum(r['success'] for r in rs)}/{len(rs)}"

    out_dir = BENCH_DIR / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    (out_dir / f"{stamp}.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    lines = [f"# NewJev benchmark — {summary['when']}", "",
             f"**Success: {ok}/{n}** ({(ok / n * 100 if n else 0):.0f}%) · false DONE: {false_done}", "",
             "| Task | Status | Checks | Steps | Time |", "|---|---|---|---|---|"]
    lines += [f"| {r['id']} | {r['status']} | {'pass' if r['checks_passed'] else 'FAIL'} | {r['steps']} | {r['seconds']}s |"
              for r in results]
    if summary["by_tag"]:
        lines += ["", "By tag: " + ", ".join(f"{k} {v}" for k, v in summary["by_tag"].items())]
    if skipped:
        lines += ["", "Skipped: " + ", ".join(f"{t['id']} ({r})" for t, r in skipped)]
    (out_dir / f"{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    print(f"\nSaved bench/results/{stamp}.json")


if __name__ == "__main__":
    main()
