"""Model fallback + usage, rescue, recipes, steering, vision fallback, shortcuts."""

import json

import app.agent.loop as loop_mod
from app.agent.loop import AgentLoop
from app.agent.recipes import RecipeStore, app_key, similarity
from app.agent.rescue import RescuePlan, validate
from app.agent.review import Reviewer
from app.agent.vision import parse_point, parse_target
from app.llm import resilient, usage
from app.llm.base import LLMResponse
from tests.fakes import FakeCapture, FakeLLM, ScriptedJev

loop_mod.time.sleep = lambda *_: None


# ------------------------------------------------------------------ helpers
class Rec(Reviewer):
    interactive = True

    def __init__(self, confirm=True, hint=None):
        self.c, self.h, self.asked = confirm, hint, []

    def confirm(self, kind, message, details=None):
        self.asked.append((kind, message))
        return self.c

    def handoff(self, message, details=None):
        return True

    def hint(self, message, details=None):
        self.asked.append(("hint", message))
        return self.h


def agent_with(script, noul=None, reviewer=None, **kw):
    settings_update = kw.pop("settings", {})
    jev = ScriptedJev(script, noul)
    a = AgentLoop(jev=jev, text_llm=kw.pop("text_llm", FakeLLM("hello")), reviewer=reviewer, **kw)
    a._settings = a._settings.model_copy(update=settings_update)
    return a, jev


class HTTPErr(Exception):
    def __init__(self, code, headers=None):
        super().__init__(f"HTTP {code}")
        self.status_code = code
        self.response = type("R", (), {"status_code": code, "headers": headers or {}})()


class StubLLM:
    def __init__(self, outcomes, model="m"):
        self.outcomes, self.calls, self.model = list(outcomes), 0, model

    def complete(self, messages, **kw):
        self.calls += 1
        o = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(o, Exception):
            raise o
        return LLMResponse(content=o, model=self.model, prompt_tokens=1000, completion_tokens=100,
                           raw={"usage": {"cost": 0.002}} if self.model == "or" else {})


# ----------------------------------------------------- 4. fallback + usage
def test_fallback_switches_model_on_rate_limit_and_records_usage():
    resilient._cooldown.clear()
    slept = []
    a = StubLLM([HTTPErr(429, {"retry-after": "120"})], model="a")
    b = StubLLM(["fine"], model="or")
    llm = resilient.FallbackLLM("text", [("p:a", a), ("p:b", b)], sleep=slept.append)
    t = usage.UsageTracker()
    with t.activate():
        r = llm.complete([{"role": "user", "content": "x"}])
    assert r.content == "fine" and a.calls == 1 and b.calls == 1 and slept == []   # 120s wait is too long: switch
    summ = t.summary()
    assert summ["total_calls"] == 2 and summ["total_errors"] == 1 and summ["fallbacks"] == 1
    assert abs(summ["cost_usd"] - 0.002) < 1e-9
    # the rate-limited model is cooling down: next call goes straight to b
    b.outcomes = ["again"]
    assert llm.complete([]).content == "again" and a.calls == 1


def test_short_retry_after_is_honoured_and_bad_request_is_raised():
    resilient._cooldown.clear()
    slept = []
    a = StubLLM([HTTPErr(429, {"retry-after": "2"}), "ok2"])
    llm = resilient.FallbackLLM("text", [("p:a", a)], sleep=slept.append)
    assert llm.complete([]).content == "ok2" and slept == [2.0]
    bad = resilient.FallbackLLM("text", [("p:x", StubLLM([HTTPErr(400)])), ("p:y", StubLLM(["no"]))])
    try:
        bad.complete([])
        raise AssertionError("400 must not fall back")
    except HTTPErr:
        pass


def test_all_models_failing_raises_model_unavailable():
    resilient._cooldown.clear()
    llm = resilient.FallbackLLM("text", [("p:a", StubLLM([HTTPErr(503)] * 3)), ("p:b", StubLLM([TimeoutError()] * 3))],
                                sleep=lambda *_: None)
    try:
        llm.complete([])
        raise AssertionError
    except resilient.ModelUnavailable as exc:
        assert "p:a" in str(exc) and "p:b" in str(exc)


def test_prices_and_chain_parsing():
    assert usage.price_for("openai/gpt-4o-mini", 1_000_000, 1_000_000) == 0.75
    assert usage.price_for("qwen/qwen3:free", 10, 10) == 0.0
    assert usage.price_for("unknown/model", 10, 10) is None
    assert usage.parse_prices("kimi-k3=0.6:2.5") == {"kimi-k3": (0.6, 2.5)}
    chain = resilient.parse_chain("openrouter:qwen/qwen3.8-27b:free, moonshot:kimi-k3, junk",
                                  ("openrouter", "moonshot"))
    assert chain == [("openrouter", "qwen/qwen3.8-27b:free"), ("moonshot", "kimi-k3")]


def test_run_result_contains_usage_summary():
    a, _ = agent_with([("TYPE_TEXT", "Message", 0.9), ("DONE", None, 0.9)], noul=[0.9])
    r = a.run("type hello", FakeCapture())
    assert "usage" in r and "rescue_stats" in r and r["run_id"]


# ------------------------------------------------------------- 1. rescue
def test_rescue_validation_only_accepts_offered_actions():
    page = FakeCapture().observe()
    plan = validate({"kind": "actions", "actions": [{"id": "e1", "operation": "TYPE_TEXT", "text": "x"},
                                                    {"id": "zzz", "operation": "CLICK"},
                                                    {"id": "e2", "operation": "DELETE_FILE"}]}, page)
    assert plan.kind == "actions" and [x["id"] for x in plan.actions] == ["e1"]
    assert validate({"kind": "actions", "actions": [{"id": "nope"}]}, page).kind == "none"
    assert validate({"kind": "replan", "steps": ["a", "b"]}, page).steps == ["a", "b"]
    assert validate(None, page).kind == "none"


class FakeRescuer:
    def __init__(self, plan):
        self.plan, self.calls = plan, 0

    def suggest(self, *a, **k):
        self.calls += 1
        return self.plan


STUCK = [("CLICK", "Send", 0.9)] * 12   # Send once, then no visible change → loop


def test_llm_rescue_runs_suggested_action_then_continues():
    rescuer = FakeRescuer(RescuePlan("actions", why="type first",
                                     actions=[{"id": "e1", "operation": "TYPE_TEXT", "label": "Message", "text": "hi"}]))
    a, jev = agent_with(STUCK[:10] + [("DONE", None, 0.9)], noul=[0.95], reviewer=Rec(), rescuer=rescuer,
                        settings={"rescue_enabled": True, "confirm_risky_actions": False})
    cap = FakeCapture()
    r = a.run("send hi", cap)
    assert rescuer.calls == 1
    assert ("e1", "TYPE_TEXT", "hi") in cap.acts           # the rescue action ran with its own value
    assert any(h.get("rescue") for h in r["history"])
    assert r["rescues"][0]["by"] == "llm" and r["rescue_stats"]["llm_helpful"] == 1
    assert r["status"] == "done"


def test_rescue_escalates_to_user_hint_then_blocked():
    rescuer = FakeRescuer(RescuePlan("none", why="no idea"))
    rev = Rec(hint="press Enter in the message box")
    a, _ = agent_with(STUCK * 3, reviewer=rev, rescuer=rescuer,
                      settings={"rescue_enabled": True, "confirm_risky_actions": False})
    r = a.run("send it", FakeCapture())
    hints = [m for k, m in rev.asked if k == "hint"]
    assert rescuer.calls >= 1 and len(hints) >= 1
    assert any("press Enter" in str(h.get("note")) for h in r["history"])
    assert r["status"] == "blocked"     # the scripted Jev never follows the hint
    assert r["rescue_stats"]["user_hints"] >= 1


def test_unattended_rescue_never_waits_for_a_user():
    rescuer = FakeRescuer(RescuePlan("none"))
    a, _ = agent_with(STUCK * 2, reviewer=None, rescuer=rescuer, settings={"rescue_enabled": True})
    r = a.run("send it", FakeCapture())
    assert r["status"] == "blocked" and all(x["by"] == "llm" for x in r["rescues"])


# ------------------------------------------------------------ 2. recipes
def test_recipe_similarity_ignores_specific_values(tmp_path):
    assert similarity('Rename "a.txt" on the Desktop to "b.txt"', 'Rename "x.txt" on the Desktop to "y.txt"') == 1.0
    assert similarity("sort column C descending", "create a pivot table") < 0.3
    assert app_key({"url": "desktop://excel.exe/Book1"}) == "desktop:excel.exe"
    assert app_key({"url": "https://www.google.com/flights"}) == "web:google.com"
    store = RecipeStore(tmp_path / "r.json")
    store.record_success("Sort the data by Units descending in Excel", "desktop:excel.exe",
                         ["Select the table", "Data > Sort", "Pick Units, Largest to Smallest", "OK"])
    hit = store.find("Sort the data by Region descending in Excel", "desktop:excel.exe", 0.5)
    assert hit and hit["steps"][1] == "Data > Sort"
    assert store.find("Sort the data by Region descending in Excel", "desktop:winword.exe", 0.5) is None
    store.record_failure("Sort the data by Units", "desktop:excel.exe", "blocked", "ribbon not found", ["CLICK 'Data'"])
    assert store.lessons("Sort the data by Units", "desktop:excel.exe")


def test_verified_run_is_learned_and_reused(tmp_path):
    store = RecipeStore(tmp_path / "r.json")
    a, _ = agent_with([("TYPE_TEXT", "Message", 0.9), ("CLICK", "Send", 0.9), ("DONE", None, 0.9)], noul=[0.95],
                      reviewer=Rec(), recipes=store)
    assert a.run("write the greeting message and send it", FakeCapture())["status"] == "done"
    saved = store.all()["recipes"]
    assert len(saved) == 1 and saved[0]["app"] == "web:x.test" and len(saved[0]["steps"]) == 2

    b, jev = agent_with([("TYPE_TEXT", "Message", 0.9), ("DONE", None, 0.9)], noul=[0.95], reviewer=Rec(),
                        recipes=store)
    r = b.run("write the greeting message and send it", FakeCapture())
    proc = jev.requests[0]["questions"]["operation"]["instructions"]["procedure"]
    assert "VERIFIED run" in proc["note"] and r["recipes_used"]


# ----------------------------------------------------------- 8. steering
def test_steering_message_reaches_jev_and_history():
    msgs = [["use the word hola instead"]]
    a, jev = agent_with([("TYPE_TEXT", "Message", 0.9), ("DONE", None, 0.9)], noul=[0.95],
                        steer=lambda: msgs.pop(0) if msgs else [])
    r = a.run("type hello", FakeCapture())
    instr = jev.requests[0]["questions"]["operation"]["instructions"]
    assert instr["procedure"]["user_messages_mid_run"] == ["use the word hola instead"]
    assert r["steering"] == ["use the word hola instead"]
    assert any("hola" in str(h.get("note")) for h in r["history"])


# -------------------------------------------------------------- 3. vision
def test_parse_point_and_target():
    assert parse_point('{"found": true, "x": 0.5, "y": 0.25}', 200, 100)["x"] == 100
    assert parse_point('{"found": true, "x": 500, "y": 5}', 200, 100) is None
    assert parse_point('{"found": false, "what": "a login page"}', 10, 10)["found"] is False
    assert parse_target("the Play icon (double-click)") == ("the Play icon", "double")


class SparseCapture(FakeCapture):
    """A canvas-like app: hardly any accessible elements."""

    mode = "desktop"

    def observe(self, screenshot=False):
        page = super().observe(screenshot)
        page["actions"] = [a for a in page["actions"] if a["kind"] != "click"]
        page["url"] = "desktop://game.exe/Game"
        return page

    def vision_shot(self):
        return {"b64": "AAAA", "width": 400, "height": 300, "left": 1000, "top": 50, "scale": 0.5}

    def act(self, page, action_id, text=None, operation=None):
        if action_id == "VISUAL_CLICK":
            self.acts.append(("VISUAL_CLICK", text))
            self.sent = True
            return {"executed": action_id}
        return super().act(page, action_id, text, operation)


def test_vision_click_maps_screenshot_to_screen_coordinates():
    vision = StubLLM(['{"found": true, "x": 100, "y": 40, "what": "Play"}'])
    a, jev = agent_with([("VISUAL_CLICK", None, 0.9), ("DONE", None, 0.9)], noul=[0.95],
                        text_llm=FakeLLM("the Play button"), vision_llm=vision)
    a._settings = a._settings.model_copy(update={"vision_fallback": True})
    cap = SparseCapture()
    r = a.run("press play", cap)
    assert "VISUAL_CLICK" in jev.requests[0]["questions"]["operation"]["criteria"]
    assert ("VISUAL_CLICK", "1200,130,left") in cap.acts    # 1000 + 100/0.5, 50 + 40/0.5
    assert r["decisions"][0]["vision"]["found"] is True


# ----------------------------------------------------------- 5. shortcuts
def test_file_ops_never_overwrite(tmp_path):
    from app.shortcuts.fileops import FileOpError, run
    f = tmp_path / "a.txt"
    assert "Wrote" in run({"action": "write", "dst": str(f), "content": "one"})
    assert "Appended" in run({"action": "append", "src": str(f), "content": "two"})
    assert f.read_text() == "one\ntwo"
    try:
        run({"action": "write", "dst": str(f), "content": "x"})
        raise AssertionError
    except FileOpError as exc:
        assert "already exists" in str(exc)
    assert "Copied" in run({"action": "copy", "src": str(f), "dst": str(tmp_path / "b.txt")})
    run({"action": "mkdir", "dst": str(tmp_path / "sub")})
    assert "Moved" in run({"action": "move", "src": str(tmp_path / "b.txt"), "dst": str(tmp_path / "sub")})
    assert (tmp_path / "sub" / "b.txt").exists()
    assert "Renamed" in run({"action": "rename", "src": str(f), "dst": "c.txt"})
    assert "one" in run({"action": "read", "src": str(tmp_path / "c.txt")})
    assert "sub/" in run({"action": "list", "src": str(tmp_path)})
    try:
        run({"action": "delete", "src": str(tmp_path / "c.txt")})
        raise AssertionError
    except FileOpError:
        pass


class FakeShortcuts:
    def __init__(self):
        self.done = []

    def actions(self, goal, page, disabled):
        return [] if "SEND_EMAIL" in disabled else [
            {"id": "SEND_EMAIL", "kind": "shortcut", "op": "SEND_EMAIL", "label": "Send an email directly"}]

    def execute(self, op, spec):
        self.done.append((op, json.loads(spec)))
        return "Sent via Outlook to bob@x.com: 'Hi'"


MAIL = json.dumps({"to": ["bob@x.com"], "subject": "Hi", "body": "Hello Bob"})


def test_send_email_needs_confirmation_and_records_result():
    sc = FakeShortcuts()
    rev = Rec(confirm=True)
    a, _ = agent_with([("SEND_EMAIL", None, 0.9), ("DONE", None, 0.9)], noul=[0.95], reviewer=rev,
                      text_llm=FakeLLM(MAIL), shortcuts=sc)
    a._text_llm = type("L", (), {"complete": lambda self, m, **k: type("R", (), {"content": MAIL})()})()
    r = a.run("email bob@x.com saying Hello Bob", FakeCapture())
    assert sc.done and sc.done[0][1]["to"] == ["bob@x.com"]
    assert rev.asked[0][0] == "send_email" and "Hello Bob" in rev.asked[0][1]
    entry = next(h for h in r["history"] if h.get("operation") == "SEND_EMAIL")
    assert "Sent via Outlook" in entry["result"] and entry["page_changed"] is True


def test_send_email_refused_without_a_person():
    sc = FakeShortcuts()
    a, _ = agent_with([("SEND_EMAIL", None, 0.9), ("BLOCKED", None, 0.9)], reviewer=None, shortcuts=sc)
    a._text_llm = type("L", (), {"complete": lambda self, m, **k: type("R", (), {"content": MAIL})()})()
    a.run("email bob@x.com", FakeCapture())
    assert sc.done == []


def test_file_op_replace_keeps_line_endings(tmp_path):
    from app.shortcuts.fileops import FileOpError, run
    f = tmp_path / "notice.txt"
    f.write_bytes(b"Meeting at 3:30 PM\r\nRoom 4B 3:30 PM\r\n")
    out = run({"action": "replace", "src": str(f), "find": "3:30 PM", "replace_with": "4:00 PM"})
    assert "2 occurrence" in out
    assert f.read_bytes() == b"Meeting at 4:00 PM\r\nRoom 4B 4:00 PM\r\n"
    try:
        run({"action": "replace", "src": str(f), "find": "9:99", "replace_with": "x"})
        raise AssertionError
    except FileOpError as exc:
        assert "does not occur" in str(exc)


def test_file_window_title_matching():
    from pathlib import PureWindowsPath
    from app.capture.desktop import DesktopCapture
    p = PureWindowsPath(r"C:\Users\me\Documents\NewJev_T01_notice.txt")
    assert DesktopCapture._title_shows("NewJev_T01_notice - Notepad", p)          # new Notepad hides .txt
    assert DesktopCapture._title_shows("*NewJev_T01_notice.txt - Notepad", p)
    assert not DesktopCapture._title_shows("Documents - File Explorer", p)
