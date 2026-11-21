import json

import app.agent.loop as loop_mod
from app.agent.loop import AgentLoop
from app.agent.review import AutoReviewer, Reviewer
from tests.fakes import FakeCapture, FakeLLM, ScriptedJev

loop_mod.time.sleep = lambda *_: None  # keep tests fast


class Recorder(Reviewer):
    interactive = True

    def __init__(self, confirm=True, handoff=True):
        self.c, self.h, self.asked = confirm, handoff, []

    def confirm(self, kind, message, details=None):
        self.asked.append(kind)
        return self.c

    def handoff(self, message, details=None):
        self.asked.append("handoff")
        return self.h


def make(script, noul=None, reviewer=None, llm="hello"):
    jev = ScriptedJev(script, noul)
    return AgentLoop(jev=jev, text_llm=FakeLLM(llm), reviewer=reviewer), jev


def test_type_send_done_verified():
    agent, jev = make([("TYPE_TEXT", "Message", 0.9), ("CLICK", "Send", 0.9), ("DONE", None, 0.9)], noul=[0.9],
                      reviewer=Recorder())
    cap = FakeCapture()
    r = agent.run("send hello", cap)
    assert r["status"] == "done"
    assert cap.value == "hello" and cap.sent
    assert agent._reviewer.asked == ["risky_action"]  # Send needed confirmation


def test_rejected_risky_action_is_not_executed():
    agent, _ = make([("CLICK", "Send", 0.9), ("BLOCKED", None, 0.9)], reviewer=Recorder(confirm=False))
    cap = FakeCapture()
    r = agent.run("send", cap)
    assert not cap.sent and r["status"] == "blocked"


def test_done_rejected_then_unverified():
    agent, _ = make([("DONE", None, 0.9)] * 4, noul=[0.1, 0.1, 0.1, 0.1])
    r = agent.run("g", FakeCapture())
    assert r["status"] == "unverified"
    assert sum(1 for h in r["history"] if "DONE rejected" in str(h.get("note"))) == 2


def test_done_rejected_then_work_then_done():
    agent, _ = make([("DONE", None, 0.9), ("TYPE_TEXT", "Message", 0.9), ("DONE", None, 0.9)], noul=[0.2, 0.95])
    cap = FakeCapture()
    assert agent.run("g", cap)["status"] == "done" and cap.value == "hello"


def test_low_confidence_retry_then_confirm():
    rev = Recorder(confirm=True)
    agent, jev = make([("TYPE_TEXT", "Message", 0.3), ("TYPE_TEXT", "Message", 0.3), ("DONE", None, 0.9)],
                      noul=[0.9], reviewer=rev)
    r = agent.run("g", FakeCapture())
    assert r["status"] == "done"
    assert rev.asked[0] == "low_confidence"
    assert r["decisions"][0]["retries"] == 1 and "low_confidence" in r["decisions"][0]["flags"]


def test_stuck_loop_warns_then_blocks():
    agent, _ = make([("PRESS_KEY", "Enter", 0.9)] * 10)
    r = agent.run("g", FakeCapture())
    assert r["status"] == "blocked"
    assert any("Loop detected" in str(h.get("note")) for h in r["history"])
    assert r["steps"] <= 7


def test_handoff_auto_detected_and_unattended_stops():
    agent, _ = make([("DONE", None, 0.9)], reviewer=AutoReviewer())
    r = agent.run("log in", FakeCapture(sensitive={"password": True}))
    assert r["status"] == "needs_user" and r["steps"] == 0


def test_handoff_resume_continues():
    rev = Recorder(handoff=True)
    agent, _ = make([("NEED_USER", None, 0.9), ("DONE", None, 0.9)], noul=[0.9], reviewer=rev)
    r = agent.run("g", FakeCapture(sensitive={"captcha": True}))
    assert r["status"] == "done" and rev.asked.count("handoff") == 2


def test_append_text_keeps_existing():
    agent, _ = make([("APPEND_TEXT", "Message", 0.9), ("DONE", None, 0.9)], noul=[0.9], llm=" world")
    cap = FakeCapture()
    cap.value = "hello"
    agent.run("g", cap)
    assert cap.value == "hello world"


def test_run_plan_stops_at_failed_phase_and_reports_it():
    agent, _ = make([("TYPE_TEXT", "Message", 0.9), ("DONE", None, 0.9), ("BLOCKED", None, 0.9)], noul=[0.9])
    cap = FakeCapture()
    r = agent.run_plan([{"task": "a", "mode": "browser", "url": "https://a"}, {"task": "b", "mode": "browser", "url": "https://b"},
                        {"task": "c", "mode": "browser", "url": "https://c"}], "abc", cap)
    assert r["status"] == "blocked"
    assert [p["status"] for p in r["phases"]] == ["done", "blocked"]


def test_run_plan_switches_capture_per_mode():
    class Mgr:
        def __init__(self):
            self.caps = {"browser": FakeCapture(), "desktop": FakeCapture()}
            self.caps["desktop"].mode = "desktop"
            self.calls = []

        def get(self, mode, url=None):
            self.calls.append((mode, url))
            return self.caps[mode]

    agent, _ = make([("DONE", None, 0.9), ("DONE", None, 0.9)], noul=[0.9, 0.9])
    m = Mgr()
    r = agent.run_plan([{"task": "web", "mode": "browser", "url": "https://a"},
                        {"task": "app", "mode": "desktop", "url": "notepad"}], "both", m)
    assert r["status"] == "done" and m.calls == [("browser", "https://a"), ("desktop", "notepad")]


def test_low_confidence_above_ask_threshold_runs_without_asking():
    rev = Recorder(confirm=False)
    agent, _ = make([("TYPE_TEXT", "Message", 0.5), ("TYPE_TEXT", "Message", 0.5), ("DONE", None, 0.9)],
                    noul=[0.9], reviewer=rev)
    r = agent.run("g", FakeCapture())
    assert r["status"] == "done" and "low_confidence" not in rev.asked
    assert "low_confidence" in r["decisions"][0]["flags"]


def test_relapse_into_warned_loop_blocks_quickly():
    agent, _ = make([("PRESS_KEY", "Enter", 0.9), ("CLICK", "Send", 0.9)] * 10)
    r = agent.run("g", FakeCapture())
    assert r["status"] == "blocked"
    assert r["steps"] <= 14


def test_long_task_with_low_confidence_but_progress_is_not_blocked():
    class Counter(FakeCapture):
        def act(self, page, action_id, text=None, operation=None):
            self.value = (self.value or "") + "x"     # every step visibly changes something
            return super().act(page, action_id, text, operation)

    script = []
    for i in range(60):
        script.append(("TYPE_TEXT" if i % 3 == 0 else "CLICK", "Message" if i % 3 == 0 else "Send", 0.4))
    script.append(("DONE", None, 0.9))
    agent, _ = make(script, noul=[0.9], llm="step")
    r = agent.run("a long multi-step task", Counter())
    assert r["status"] == "done", [h.get("note") for h in r["history"] if h.get("note")]
    assert r["steps"] >= 30   # each low-confidence step is re-asked once (retry), so ~30 decisions


def test_clipboard_baseline_hidden_from_jev():
    class ClipCap(FakeCapture):
        clip = "my secret"

        def observe(self, screenshot=False):
            p = super().observe()
            p["clipboard"] = self.clip
            return p

        def act(self, page, action_id, text=None, operation=None):
            if action_id == "key_1":
                self.clip = "1728"
            return super().act(page, action_id, text, operation)

    agent, jev = make([("PRESS_KEY", "Enter", 0.9), ("DONE", None, 0.9)], noul=[0.9])
    agent.run("g", ClipCap())
    sent = [r["state"]["page"].get("clipboard") for r in jev.requests if "operation" in r["questions"]]
    assert sent[0] == "(nothing copied during this task)" and sent[1] == "1728"
    assert all("secret" not in json.dumps(r) for r in jev.requests)


def test_delete_file_needs_confirmation_and_is_not_executed_when_rejected():
    class DelCap(FakeCapture):
        def observe(self, screenshot=False):
            p = super().observe()
            p["actions"].append({"id": "DELETE_FILE", "kind": "delete_file", "op": "DELETE_FILE", "label": "Delete file"})
            return p

    import app.agent.loop as lm
    orig = lm.AgentLoop._resolve_delete_target
    lm.AgentLoop._resolve_delete_target = staticmethod(lambda spec: __import__("pathlib").Path("/x/report.txt"))
    try:
        rev = Recorder(confirm=False)
        agent, _ = make([("DELETE_FILE", None, 0.95), ("BLOCKED", None, 0.9)], reviewer=rev, llm="report.txt")
        cap = DelCap()
        r = agent.run("delete report.txt", cap)
        assert rev.asked == ["delete"]
        assert not any(a[0] == "DELETE_FILE" for a in cap.acts) and r["status"] == "blocked"

        agent, _ = make([("DELETE_FILE", None, 0.95), ("DONE", None, 0.9)], noul=[0.9], reviewer=Recorder(), llm="report.txt")
        cap = DelCap()
        agent.run("delete report.txt", cap)
        assert any(a[0] == "DELETE_FILE" for a in cap.acts)
    finally:
        lm.AgentLoop._resolve_delete_target = orig


class TidyCap(FakeCapture):
    """Desktop-like capture with unsaved-changes tracking and windows the agent opened."""
    mode = "desktop"

    def __init__(self):
        super().__init__()
        self.unsaved = True
        self.open_windows = ["*notes.txt - Notepad"]

    def pending_issues(self, page=None):
        return ["'*notes.txt - Notepad' has unsaved changes — save them (PRESS_KEY Ctrl+S)"] if self.unsaved else []

    def opened_windows(self):
        return list(self.open_windows)

    def act(self, page, action_id, text=None, operation=None):
        if action_id == "key_1":      # stands in for Ctrl+S / closing in this fake
            if self.unsaved:
                self.unsaved = False
            else:
                self.open_windows = []
        return super().act(page, action_id, text, operation)


def test_done_blocked_until_changes_saved():
    agent, _ = make([("DONE", None, 0.9), ("PRESS_KEY", "Enter", 0.9), ("DONE", None, 0.9)], noul=[0.9])
    cap = TidyCap()
    cap.open_windows = []
    r = agent.run("add a line to notes.txt and save", cap)
    assert r["status"] == "done" and not cap.unsaved
    assert any("Not finished yet" in str(h.get("note")) for h in r["history"])


def test_wrap_up_closes_opened_windows_after_edit_task():
    cap = TidyCap()
    cap.unsaved = False
    agent, jev = make([("DONE", None, 0.9), ("PRESS_KEY", "Enter", 0.9), ("DONE", None, 0.9)], noul=[0.9])
    r = agent.run("add a line to notes.txt and save", cap)
    assert r["status"] == "done"
    assert r["phases"][-1]["task"].startswith("Finishing up") and r["phases"][-1]["status"] == "done"
    assert cap.open_windows == []
    wrap_goal = jev.requests[-1]["questions"]["operation"]["instructions"]["goal"]
    assert "ALREADY COMPLETE" in wrap_goal and "*notes.txt - Notepad" in wrap_goal


def test_no_wrap_up_when_goal_is_to_show_something():
    cap = TidyCap()
    cap.unsaved = False
    agent, _ = make([("DONE", None, 0.9)], noul=[0.9])
    r = agent.run("open notes.txt", cap)
    assert r["status"] == "done" and len(r["phases"]) == 1 and cap.open_windows


class UnsavedCapture(FakeCapture):
    def __init__(self):
        super().__init__()
        self.closed_checks = 0

    def pending_issues(self, page=None):
        return ["'*Untitled - Notepad' has unsaved changes — save them (PRESS_KEY Ctrl+S)"]

    def opened_windows(self, this_run=True):
        return ["*Untitled - Notepad"]


def test_new_unnamed_document_is_not_forced_to_be_saved_or_closed():
    agent, jev = make([("TYPE_TEXT", "Message", 0.9), ("DONE", None, 0.9)], noul=[0.95])
    r = agent.run("Open Notepad and type a short paragraph about the ocean", UnsavedCapture())
    assert r["status"] == "done"
    assert not any("unsaved" in str(h.get("note")) for h in r["history"])
    assert [p["task"] for p in r["phases"]] == ["Open Notepad and type a short paragraph about the ocean"]


def test_editing_an_existing_file_still_requires_saving():
    agent, _ = make([("TYPE_TEXT", "Message", 0.9), ("DONE", None, 0.9), ("PRESS_KEY", "Enter", 0.9),
                     ("DONE", None, 0.9)], noul=[0.95, 0.95])
    r = agent.run("Add a line to notes.txt on the Desktop", UnsavedCapture())
    assert any("unsaved" in str(h.get("note")) for h in r["history"])


class FailingCapture(FakeCapture):
    """Typing into the field keeps failing (e.g. a file that does not exist)."""

    def act(self, page, action_id, text=None, operation=None):
        if action_id == "e1":
            raise ValueError("No file named 'x.txt' exists. Recently changed files there: C:\\\\D\\\\line one.txt")
        return super().act(page, action_id, text, operation)


def test_repeated_execution_errors_go_to_rescue_not_straight_to_error():
    class R:
        calls = 0

        def suggest(self, *a, **k):
            R.calls += 1
            from app.agent.rescue import RescuePlan
            return RescuePlan("actions", actions=[{"id": "e2", "operation": "CLICK", "label": "Send", "text": None}])

    jev = ScriptedJev([("TYPE_TEXT", "Message", 0.9)] * 3 + [("DONE", None, 0.9)], [0.95])
    agent = AgentLoop(jev=jev, text_llm=FakeLLM("x"), reviewer=Recorder(), rescuer=R())
    agent._settings = agent._settings.model_copy(update={"rescue_enabled": True, "confirm_risky_actions": False})
    cap = FailingCapture()
    r = agent.run("open x.txt", cap)
    assert R.calls == 1 and cap.sent and r["status"] == "done"
    assert sum(1 for h in r["history"] if h.get("failed")) == 3
    assert any("Do not repeat this exact action" in str(h.get("note")) for h in r["history"])


def test_report_answer_low_confidence_accepted_without_asking_and_hints_done():
    class ReportCapture(FakeCapture):
        def observe(self, screenshot=False):
            page = super().observe(screenshot=screenshot)
            page["actions"].append({
                "id": "report_answer", "kind": "report", "op": "REPORT_ANSWER",
                "label": "Report the final extracted answer",
            })
            return page

    rev = Recorder(confirm=False)
    # REPORT_ANSWER has low confidence 0.45 (< 0.60 threshold), followed by DONE
    agent, _ = make([("REPORT_ANSWER", "report", 0.45), ("DONE", None, 0.9)],
                    noul=[0.95], reviewer=rev, llm="Here is the summary")
    r = agent.run("Give me latest news", ReportCapture())
    assert r["status"] == "done"
    assert "low_confidence" not in rev.asked
    assert r["final_report"] == "Here is the summary"
    # Ensure the post-report DONE note was injected into history
    assert any("successfully reported" in str(h.get("note", "")) for h in r["history"])


def test_visual_click_offered_when_few_elements_even_if_not_stuck():
    from app.agent.loop import AgentLoop
    from tests.fakes import FakeCapture

    class FewElementsCapture(FakeCapture):
        def observe(self, screenshot=False):
            page = super().observe(screenshot=screenshot)
            # Only 2 elements (fewer than vision_min_elements=6)
            page["actions"] = page["actions"][:2]
            return page

        def vision_shot(self):
            return b"fake-png-bytes"

    agent = AgentLoop(text_llm=FakeLLM("x"), vision_llm=FakeLLM("x"))
    page = {"actions": [{"id": "a1", "kind": "click", "label": "btn"}], "mode": "desktop"}
    # Call _augment with stuck=False; VISUAL_CLICK should still be offered
    augmented = agent._augment(page, "click speaker", FewElementsCapture(), disabled_shortcuts=set(), stuck=False)
    ids = [a["id"] for a in augmented["actions"]]
    assert "VISUAL_CLICK" in ids

