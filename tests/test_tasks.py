"""Saved tasks: templates, schedules, folder triggers, scheduler queueing."""

import datetime as dt
import threading

from app.tasks import TaskError, TaskStore, next_run, parse_schedule, placeholders, render
from app.tasks.scheduler import Scheduler

MON_0800 = dt.datetime(2026, 10, 5, 8, 0)   # a Monday


def test_templates():
    assert placeholders("Rename {file} to {new} ({file})") == ["file", "new"]
    assert render("Email {who}", {"who": "bob@x.com"}) == "Email bob@x.com"
    try:
        render("Email {who} about {topic}", {"who": "b"})
        raise AssertionError
    except TaskError as exc:
        assert "topic" in str(exc)
    assert render("JSON stays {\"a\": 1}", {}) == "JSON stays {\"a\": 1}"


def test_schedules():
    assert next_run("every 30m", MON_0800) == MON_0800 + dt.timedelta(minutes=30)
    assert next_run("daily 09:00", MON_0800) == MON_0800.replace(hour=9)
    assert next_run("daily 07:00", MON_0800) == dt.datetime(2026, 10, 6, 7, 0)
    assert next_run("weekdays 08:30", dt.datetime(2026, 10, 9, 9, 0)) == dt.datetime(2026, 10, 12, 8, 30)  # Fri→Mon
    assert next_run("wed,fri 10:00", MON_0800) == dt.datetime(2026, 10, 7, 10, 0)
    assert next_run("once 2026-10-05 09:00", MON_0800) == MON_0800.replace(hour=9)
    assert next_run("once 2026-10-05 09:00", MON_0800.replace(hour=10)) is None
    assert parse_schedule("") is None
    for bad in ("every 1m", "daily 25:00", "sometimes", "mon,xyz 10:00"):
        try:
            parse_schedule(bad)
            raise AssertionError(bad)
        except TaskError:
            pass


def test_store_crud_and_history(tmp_path):
    store = TaskStore(tmp_path / "t.json")
    t = store.upsert({"name": "Report", "template": "Open {file} and print it", "schedule": "daily 09:00"})
    assert store.get("report")["id"] == t["id"] and store.list()[0]["placeholders"] == ["file"]
    store.upsert({"id": t["id"], "name": "Report", "template": "Open {file} and print it twice"})
    assert store.get(t["id"])["template"].endswith("twice") and store.get(t["id"])["schedule"] == ""
    store.record_run(t["id"], {"status": "done", "steps": 4, "trace_path": "/x/trace_a_done.json"}, "manual")
    run = store.get(t["id"])["runs"][0]
    assert run["status"] == "done" and run["trace"] == "trace_a_done.json"
    assert store.delete(t["id"]) and not store.list()
    try:
        store.upsert({"name": "x", "template": "y", "schedule": "whenever"})
        raise AssertionError
    except TaskError:
        pass


def test_scheduler_queues_due_tasks_and_new_files(tmp_path):
    store = TaskStore(tmp_path / "t.json")
    due = store.upsert({"name": "Due", "template": "do it", "schedule": "every 30m"})
    store.upsert({"name": "Off", "template": "x", "schedule": "every 30m", "enabled": False})
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "old.csv").write_text("a")
    store.upsert({"name": "Import", "template": "Import {file} into Excel", "watch_folder": str(inbox),
                  "watch_pattern": "*.csv"})
    ran = []
    sch = Scheduler(store, lambda task, goal, trig: ran.append((goal, trig)) or {"status": "done"})
    later = dt.datetime.now() + dt.timedelta(minutes=31)
    assert sch.tick(later) == [("Due", "schedule")]
    assert sch.tick(later) == []                       # not queued twice while pending
    (inbox / "new.csv").write_text("1,2")
    (inbox / "skip.txt").write_text("x")
    assert sch.tick(dt.datetime.now()) == []          # first sighting: wait until the size is stable
    assert sch.tick(dt.datetime.now()) == [("Import", "file:new.csv")]
    while not sch._queue.empty():
        task, params, trig = sch._queue.get()
        sch.execute(task, params, trig)
    assert ("do it", "schedule") in ran
    assert any(g == f"Import {inbox / 'new.csv'} into Excel" for g, _ in ran)
    assert store.get(due["id"])["last_status"] == "done"
    assert sch.tick(dt.datetime.now()) == []          # just ran: next run is 30 minutes away


def test_scheduler_waits_while_the_ui_is_busy(tmp_path):
    store = TaskStore(tmp_path / "t.json")
    t = store.upsert({"name": "A", "template": "go"})
    busy = {"v": True}
    ran = []
    sch = Scheduler(store, lambda task, goal, trig: ran.append(goal) or {"status": "done"}, busy=lambda: busy["v"],
                    poll_s=2)
    sch.start()
    sch.run_now(store.get(t["id"]))
    threading.Event().wait(1.5)
    assert ran == []
    busy["v"] = False
    for _ in range(40):
        if ran:
            break
        threading.Event().wait(0.2)
    sch.shutdown()
    assert ran == ["go"]
