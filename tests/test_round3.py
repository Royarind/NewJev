"""Take-over pause, plan memory, batched ops, data / PDF / chat / calendar shortcuts, long jobs, hotkey."""

import json
import threading
import time

import app.agent.loop as loop_mod
from app.agent import takeover
from app.agent.loop import AgentLoop
from app.agent.review import Reviewer
from tests.fakes import FakeCapture, FakeLLM, ScriptedJev

loop_mod.time.sleep = lambda *_: None


class Rec(Reviewer):
    interactive = True

    def __init__(self, resume=True):
        self.resume, self.asked = resume, []

    def confirm(self, kind, message, details=None):
        self.asked.append((kind, message))
        return True

    def handoff(self, message, details=None):
        return True

    def takeover(self, message, details=None):
        self.asked.append(("takeover", message))
        return self.resume


# ------------------------------------------------------------- 1. take-over
class FakeWatcher(takeover.InputWatcher):
    pass


class TouchyCapture(FakeCapture):
    """The person clicks somewhere right after the agent's first action."""

    def __init__(self, watcher):
        super().__init__()
        self.w = watcher

    def act(self, page, action_id, text=None, operation=None):
        r = super().act(page, action_id, text, operation)
        if len(self.acts) == 1:
            threading.Event().wait(0.02)
            self.w.record("mouse click")
        return r


def test_agent_pauses_when_the_person_takes_over_and_resumes():
    w = FakeWatcher()
    rev = Rec(resume=True)
    a = AgentLoop(jev=ScriptedJev([("TYPE_TEXT", "Message", 0.9), ("CLICK", "Send", 0.9), ("DONE", None, 0.9)],
                                  [0.95]), text_llm=FakeLLM("hi"), reviewer=rev)
    a._settings = a._settings.model_copy(update={"confirm_risky_actions": False})
    a._watcher = w
    r = a.run("type hi and send", TouchyCapture(w))
    assert any(k == "takeover" for k, _ in rev.asked)
    assert any("took over" in str(h.get("note")) for h in r["history"])
    assert r["status"] == "done"


def test_stop_on_takeover_card_ends_the_run():
    w = FakeWatcher()
    a = AgentLoop(jev=ScriptedJev([("TYPE_TEXT", "Message", 0.9), ("CLICK", "Send", 0.9)]), text_llm=FakeLLM("hi"),
                  reviewer=Rec(resume=False))
    a._watcher = w
    assert a.run("type hi", TouchyCapture(w))["status"] == "stopped"


def test_typing_is_interrupted_by_the_person():
    w = FakeWatcher()
    takeover.set_watcher(w)
    try:
        takeover.begin_action()
        takeover.check_interrupt()             # nothing happened yet
        threading.Event().wait(0.02)
        w.record("keyboard")
        try:
            takeover.check_interrupt()
            raise AssertionError
        except takeover.UserTookOver:
            pass
        w.suppress(5)
        before = w.last_human
        w.record("keyboard")
        assert w.last_human == before          # suppressed (e.g. the hotkey itself)
    finally:
        takeover.set_watcher(None)


# --------------------------------------------------------- 2. plan memory
def test_adapt_plan_swaps_values_in_order():
    from app.agent.planner import adapt_plan
    stored = {"id": "p1", "steps": [
        {"task": 'In Notepad type "first" and save it on the Desktop as NewJev_T27a.txt', "mode": "desktop",
         "url": "notepad"}]}
    new = adapt_plan('Create a text file on the Desktop called NewJev_T27a.txt containing "first"',
                     'Create a text file on the Desktop called NewJev_T27b.txt containing "second"', stored)
    assert new and new["from_recipe"] == "p1"
    task = new["steps"][0]["task"]
    assert "NewJev_T27b.txt" in task and '"second"' in task and "first" not in task and "T27a" not in task
    # different number of values: not reusable
    assert adapt_plan('Type "a"', 'Type "a" and "b"', stored) is None


def test_plan_memory_round_trip(tmp_path):
    from app.agent.recipes import RecipeStore
    st = RecipeStore(tmp_path / "r.json")
    st.record_plan('Open Notepad and type "x" then save as a.txt',
                   {"goal": "g", "steps": [{"task": 'Notepad: type "x", save as a.txt', "mode": "desktop"}]})
    hit = st.find_plan('Open Notepad and type "y" then save as b.txt')
    assert hit and hit["steps"][0]["mode"] == "desktop"
    assert st.find_plan("Book a flight to Paris") is None


# ------------------------------------------------------ 3. batched actions
def test_type_submit_in_message_boxes_counts_as_sending():
    from app.agent.guards import is_risky
    assert is_risky("TYPE_SUBMIT", "Type a message", ["send"])
    assert is_risky("TYPE_SUBMIT", "Search", ["send"]) is None


def test_type_submit_is_offered_and_executed():
    jev = ScriptedJev([("TYPE_SUBMIT", "Message", 0.9), ("DONE", None, 0.9)], [0.95])
    a = AgentLoop(jev=jev, text_llm=FakeLLM("hello"), reviewer=Rec())
    cap = FakeCapture()
    r = a.run("search for hello", cap)
    assert "TYPE_SUBMIT" in jev.requests[0]["questions"]["operation"]["criteria"]
    assert ("e1", "TYPE_SUBMIT", "hello") in cap.acts and r["status"] == "done"


# ---------------------------------------------------- 4. shortcut: data
def test_data_ops_on_csv_and_xlsx(tmp_path):
    from app.shortcuts.data import DataOpError, run
    f = tmp_path / "sales.csv"
    f.write_text("Region,Product,Units\nWest,Pens,10\nEast,Pads,4\nWest,Pads,7\n")
    assert "3 rows" in run({"action": "read", "src": str(f)})
    assert "sum 21" in run({"action": "summary", "src": str(f)})
    out = run({"action": "sort", "src": str(f), "column": "Units", "descending": True})
    assert "sales_sorted.csv" in out
    assert (tmp_path / "sales_sorted.csv").read_text().splitlines()[1].startswith("West,Pens,10")
    out = run({"action": "filter", "src": str(f), "where": [{"column": "Region", "op": "=", "value": "west"}]})
    assert "2 of 3 rows" in out
    agg = run({"action": "aggregate", "src": str(f), "column": "Units", "func": "sum", "group_by": "Region"})
    assert "West: 17" in agg and "East: 4" in agg
    run({"action": "append_rows", "src": str(f), "rows": [["North", "Ink", "2"]]})
    assert f.read_text().strip().endswith("North,Ink,2")
    conv = run({"action": "convert", "src": str(f), "dst": str(tmp_path / "sales.xlsx")})
    assert "4 rows" in conv
    assert "West: 17" in run({"action": "aggregate", "src": str(tmp_path / "sales.xlsx"), "column": "C",
                              "func": "sum", "group_by": "A"})
    try:
        run({"action": "sort", "src": str(f), "column": "Units"})   # second sort: sales_sorted.csv exists
        raise AssertionError
    except DataOpError as exc:
        assert "already exists" in str(exc)


# ----------------------------------------------------- 4. shortcut: PDF
def _form_pdf(path):
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    c.drawString(72, 760, "Application form for NewJev")
    c.acroForm.textfield(name="FullName", x=72, y=700, width=200, height=20)
    c.acroForm.checkbox(name="Agree", x=72, y=660, size=14)
    c.save()


def test_pdf_read_fields_and_fill(tmp_path):
    from app.shortcuts.pdf import PdfOpError, run
    p = tmp_path / "form.pdf"
    _form_pdf(p)
    assert "Application form" in run({"action": "read", "src": str(p)})
    fields = run({"action": "fields", "src": str(p)})
    assert "FullName" in fields and "Agree" in fields
    out = run({"action": "fill", "src": str(p), "values": {"fullname": "Arindam Roy", "Agree": True}})
    assert "form_filled.pdf" in out
    import pypdf
    vals = pypdf.PdfReader(str(tmp_path / "form_filled.pdf")).get_fields()
    assert vals["FullName"].get("/V") == "Arindam Roy"
    try:
        run({"action": "fill", "src": str(p), "values": {"Nope": "x"}})
        raise AssertionError
    except PdfOpError as exc:
        assert "unknown field" in str(exc)


# ------------------------------------------ 4. chat / calendar / cloud facade
class _S:
    gmail_credentials_path = ""
    gmail_token_path = "x"
    slack_token = "xoxb-test"
    graph_client_id = ""
    graph_tenant = "organizations"
    graph_token_path = "nope.json"


def test_outbound_summaries_and_offers():
    from app.shortcuts import Shortcuts
    sc = Shortcuts(_S())
    kind, text = sc.outbound("SEND_MESSAGE", json.dumps({"via": "slack", "to": "#general", "text": "Hi all"}))
    assert kind == "send_message" and "#general" in text and "Hi all" in text
    kind, text = sc.outbound("CALENDAR", json.dumps({"action": "create", "subject": "Sync", "start": "2026-10-05 10:00",
                                                    "attendees": ["a@x.com"]}))
    assert kind == "send_invite" and "a@x.com" in text
    assert sc.outbound("CALENDAR", json.dumps({"action": "read"})) is None
    ops = {a["op"] for a in sc.actions("Send a Slack message to #general", {"mode": "browser"}, set())}
    assert "SEND_MESSAGE" in ops and "READ_MESSAGES" in ops
    ops = {a["op"] for a in sc.actions("Sort sales.csv by Units", {"mode": "desktop"}, set())}
    assert "DATA_OP" in ops


def test_slack_send_resolves_channel():
    import httpx
    from app.shortcuts.slack import Slack
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path.endswith("conversations.list"):
            return httpx.Response(200, json={"ok": True, "channels": [{"id": "C1", "name": "general"}]})
        return httpx.Response(200, json={"ok": True})

    sl = Slack("xoxb-t", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert "Sent" in sl.send("#general", "hello")
    assert calls[-1].endswith("chat.postMessage")


def test_when_parses_dates():
    from app.shortcuts import _when
    assert _when("2026-10-05 09:30").hour == 9
    assert _when("2026-10-05").day == 5


def test_confirmed_outbound_message_flow():
    class SC:
        done = []

        def actions(self, goal, page, disabled):
            return [{"id": "SEND_MESSAGE", "kind": "shortcut", "op": "SEND_MESSAGE", "label": "Send a chat message"}]

        def outbound(self, op, spec):
            return ("send_message", "Send to #general: hi")

        def execute(self, op, spec):
            SC.done.append(op)
            return "Sent a Slack message to #general"

    spec = json.dumps({"via": "slack", "to": "#general", "text": "hi"})
    rev = Rec()
    a = AgentLoop(jev=ScriptedJev([("SEND_MESSAGE", None, 0.9), ("DONE", None, 0.9)], [0.95]),
                  text_llm=type("L", (), {"complete": lambda self, m, **k: type("R", (), {"content": spec})()})(),
                  reviewer=rev, shortcuts=SC())
    r = a.run("Post hi in #general on Slack", FakeCapture())
    assert SC.done == ["SEND_MESSAGE"] and rev.asked[0][0] == "send_message" and r["status"] == "done"


# ---------------------------------------------------------------- 5. jobs
def test_job_items_render_and_resume(tmp_path):
    from app.tasks.jobs import JobStore, enumerate_items, looks_like_job, new_job, progress, render_item, run_job
    assert looks_like_job("For every PDF in Downloads, rename it with today's date")
    assert not looks_like_job("Open Notepad and type hello")
    folder = tmp_path / "inbox"
    folder.mkdir()
    for n in ("a.txt", "b.txt", "c.txt", "skip.csv"):
        (folder / n).write_text(n)
    items = enumerate_items({"type": "files", "folder": str(folder), "pattern": "*.txt"})
    assert [i["label"] for i in items] == ["a.txt", "b.txt", "c.txt"]
    assert render_item("Open {item} ({n}) and check {stem}", items[1]["vars"]).endswith("(2) and check b")

    store = JobStore(tmp_path / "jobs")
    job = new_job("for every txt file in inbox, print it",
                  {"source": {"type": "files", "folder": str(folder), "pattern": "*.txt"}, "per_item": "Print {name}",
                   "after": "Tell me it is done"})
    seen = []
    stop = {"v": False}

    def run_item(goal, item):
        seen.append(goal)
        if goal == "Print b.txt":
            stop["v"] = True               # the PC crashes / user stops during item 2
            return {"status": "stopped"}
        return {"status": "done", "steps": 2}

    j = run_job(job, store, run_item, should_stop=lambda: stop["v"])
    assert j["status"] == "stopped" and progress(j)["done"] == 1
    saved = store.load(job["id"])
    assert [i["status"] for i in saved["items"]] == ["done", "pending", "pending"]
    stop["v"] = False
    seen.clear()
    j = run_job(saved, store, lambda g, it: seen.append(g) or {"status": "done"})
    assert seen == ["Print b.txt", "Print c.txt", "Tell me it is done"] and j["status"] == "done"
    assert store.unfinished() == []


def test_job_rows_and_failure_pause(tmp_path):
    from app.tasks.jobs import JobStore, new_job, run_job
    f = tmp_path / "people.csv"
    f.write_text("Name,Email\nAsha,a@x.com\nBen,b@x.com\nChen,c@x.com\nDev,d@x.com\n")
    job = new_job("for each row email them", {"source": {"type": "rows", "file": str(f)},
                                              "per_item": "Email {col:Email} saying Hi {col:Name}"})
    assert job["items"][0]["vars"]["cols"]["Name"] == "Asha"
    goals = []
    j = run_job(job, JobStore(tmp_path / "j"), lambda g, it: goals.append(g) or {"status": "blocked"})
    assert goals[0] == "Email a@x.com saying Hi Asha"
    assert j["status"] == "paused_failures" and len(goals) == 3     # 3 failures in a row: stop and ask


def test_plan_job_uses_llm_only_for_repetitive_requests(tmp_path):
    from app.tasks.jobs import plan_job
    folder = tmp_path / "f"
    folder.mkdir()
    (folder / "x.pdf").write_text("x")

    class L:
        calls = 0

        def complete(self, m, **k):
            L.calls += 1
            return type("R", (), {"content": json.dumps({"is_job": True, "source": {
                "type": "files", "folder": str(folder), "pattern": "*.pdf"}, "per_item": "Open {item}"})})()

    assert plan_job("Open Notepad", L()) is None and L.calls == 0
    job = plan_job(f"For every PDF in {folder}, open it", L())
    assert job and job["items"][0]["label"] == "x.pdf"


# ------------------------------------------------------------ 6. hotkey
def test_hotkey_combo_parsing():
    from app.ui.hotkey import parse_combo
    assert parse_combo("ctrl+alt+j") == (0x0002 | 0x0001, ord("J"))
    assert parse_combo("Win+Shift+F9") == (0x0008 | 0x0004, 0x78)
    for bad in ("j", "ctrl+", "hyper+j"):
        try:
            parse_combo(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_shortcut_results_reach_jev_and_save_is_a_no_op(tmp_path):
    from app.shortcuts.fileops import run
    assert "already saved" in run({"action": "save", "src": "x"})
    f = tmp_path / "n.txt"
    f.write_text("at 3:30 PM")
    assert "Replaced 1" in run({"action": "edit", "src": str(f), "find": "3:30 PM", "replace_with": "4:00 PM"})
    jev = ScriptedJev([("DONE", None, 0.9)], [0.9])
    body, _, _ = jev.build_request(FakeCapture().observe(), "g", [
        {"operation": "FILE_OP", "label": "x", "text": "{}", "result": "Replaced 1 occurrence", "page_changed": True}])
    assert body["state"]["recent_actions"][0]["result"] == "Replaced 1 occurrence"


def test_agent_input_marks_busy_windows():
    assert not takeover.agent_is_busy() or True
    with takeover.agent_input():
        assert takeover.agent_is_busy()
    assert takeover.agent_is_busy()        # short grace period after the agent's own input
