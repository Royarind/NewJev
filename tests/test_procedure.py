import json

from app.agent.loop import AgentLoop
from app.agent.procedure import Procedure, ProcedurePlanner, parse_steps
from tests.fakes import FakeCapture, FakeLLM, ScriptedJev


def test_parse_steps_json_and_lists():
    assert parse_steps('```json\n{"steps": ["Open Data tab", "Click Sort"]}\n```') == ["Open Data tab", "Click Sort"]
    assert parse_steps("1. Open Insert tab\n2) Click PivotTable\n- Click OK") == ["Open Insert tab", "Click PivotTable",
                                                                                   "Click OK"]


def test_progress_ticks_on_matching_actions():
    p = Procedure(["Select the data range in the Name Box (A1:F200)", "Open the Insert tab",
                   "Click PivotTable", "Click OK in the Create PivotTable dialog", "Tick the Region field"])
    p.observe_action("SELECT_RANGE", "Name Box", "A1:F200", True)
    assert p.current == 1
    p.observe_action("CLICK", "Insert", None, True)
    assert p.current == 2
    p.observe_action("CLICK", "PivotTable", None, False)          # no visible change -> not ticked
    assert p.current == 2
    p.observe_action("CLICK", "PivotTable", None, True)
    p.observe_action("CLICK", "OK", None, True)
    assert p.current == 4
    g = p.as_guidance()
    assert g["steps"][4].startswith("[NEXT] 5.") and g["steps"][0].startswith("[done]")


class StubPlanner:
    def __init__(self):
        self.calls = []

    def make(self, task, page, overall_goal=None, failed=None, **kw):
        self.calls.append(failed)
        return Procedure(["Click into Message", "Type the text", "Click Send"])


def test_loop_sends_procedure_to_jev_and_replans_when_stuck():
    jev = ScriptedJev([("PRESS_KEY", "Enter", 0.9)] * 5 + [("DONE", None, 0.9)], [0.9])
    planner = StubPlanner()
    agent = AgentLoop(jev=jev, text_llm=FakeLLM(), procedure_planner=planner)
    agent._settings = agent._settings.model_copy(update={"procedure_mode": "always"})
    agent.run("send a message saying hello to the team", FakeCapture())
    first = jev.requests[0]["questions"]["operation"]["instructions"]["procedure"]
    assert first["steps"][0].startswith("[NEXT] 1.")
    assert len(planner.calls) >= 2 and planner.calls[0] is None and planner.calls[1]   # re-planned with failures


def test_planner_uses_llm_output():
    class LLM:
        def complete(self, messages):
            ctx = json.loads(messages[1]["content"])
            assert ctx["task"] == "sort column C"
            return type("R", (), {"content": '{"steps": ["Type C:C in the Name Box", "Data tab → Sort A to Z"]}'})()
    p = ProcedurePlanner(LLM()).make("sort column C", {"title": "Book1 - Excel", "actions": []})
    assert p.steps[1].startswith("Data tab")


def test_parse_steps_broken_json_from_free_models():
    raw = ('{\n"steps": [\n"Launch Excel (double-click the Excel shortcut).",\n'
           '"If a workbook does not appear, press Ctrl+N to create a new blank workbook.",\n'
           '"After the table is entered, press F12 to open the Save As dialog.",\n'
           '"In the Save As pane, click Documents on the')   # truncated mid-string
    steps = parse_steps(raw)
    assert steps[0].startswith("Launch Excel") and steps[2].startswith("After the table")
    assert not any(s in ("{", '"steps": [') for s in steps)
