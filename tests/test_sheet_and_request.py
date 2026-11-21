import json

import pytest

from app.agent.loop import AgentLoop
from app.agent.planner import Planner, keeps_literals
from app.capture.sheet import as_number, block_range, index_to_col, parse_table
from tests.fakes import FakeCapture, ScriptedJev

USER = ("Open Excel, create a blank workbook and enter this table starting at A1 — headers: Region, Product, Units, "
        "Sales. Rows: West, Pens, 10, 120 / East, Pads, 4, 300. Save it in Documents as NewJev_sales.xlsx.")


def test_planner_summary_that_drops_values_is_replaced_by_user_words():
    kimi = ('{"goal": "Open Microsoft Excel, create a blank workbook, enter a sales table starting at cell A1 with '
            'headers Region, Product, Units, Sales and six data rows, then save it as NewJev_sales.xlsx", '
            '"steps": [{"task": "enter a sales table with six data rows and save", "mode": "desktop", "url": "excel"}]}')
    p = Planner.parse(kimi, USER)
    assert p["goal"] == USER and p["steps"][0]["task"] == USER
    assert keeps_literals("type '10' and 'Pens'", "Type '10' and 'Pens' now")
    assert not keeps_literals(USER, "enter six rows")


def test_multi_phase_tasks_get_the_exact_request_when_values_were_lost():
    raw = ('{"goal": "%s", "steps": [{"task": "make the table", "mode": "desktop", "url": "excel"},'
           '{"task": "write a summary", "mode": "desktop", "url": "word"}]}') % USER.replace('"', "'")
    p = Planner.parse(raw, USER)
    assert all("User's exact request" in s["task"] for s in p["steps"])


def test_parse_table_formats():
    start, rows = parse_table("A1\nRegion\tProduct\tUnits\nWest\tPens\t10")
    assert start == "A1" and rows == [["Region", "Product", "Units"], ["West", "Pens", "10"]]
    assert parse_table('{"start": "B3", "rows": [["a", "b"], ["c", "d"]]}') == ("B3", [["a", "b"], ["c", "d"]])
    assert parse_table("West | Pens | 10")[1] == [["West", "Pens", "10"]]
    assert block_range("A1", [["1", "2", "3", "4"]] * 7) == ("A1", "D7")
    assert block_range("Z5", [["1", "2"]]) == ("Z5", "AA5") and index_to_col(28) == "AB"
    assert as_number("120") == 120 and as_number("1,234.5") == 1234.5 and as_number("Pens") == "Pens"
    with pytest.raises(ValueError):
        parse_table("A1\n")


class RecordingLLM:
    def __init__(self):
        self.contexts = []

    def complete(self, messages, **kw):
        self.contexts.append(json.loads(messages[1]["content"]))
        return type("R", (), {"content": '{"text": "hello"}'})()


class CountingCapture(FakeCapture):
    observes = 0

    def observe(self, screenshot=False):
        CountingCapture.observes += 1
        return super().observe(screenshot)


def test_user_request_reaches_jev_and_text_llm_and_snapshots_are_reused():
    jev = ScriptedJev([("TYPE_TEXT", "Message", 0.9), ("CLICK", "Send", 0.9), ("DONE", None, 0.9)], noul=[0.95])
    llm = RecordingLLM()
    agent = AgentLoop(jev=jev, text_llm=llm)
    CountingCapture.observes = 0
    agent.run("send the message", CountingCapture(), user_request=USER)
    ins = jev.requests[0]["questions"]["operation"]["instructions"]
    assert ins["user_request_verbatim"] == USER
    assert llm.contexts[0]["user_request_verbatim"] == USER
    assert "never invent" in llm.contexts[0]["data_rule"].lower()
    noul = [r for r in jev.requests if "check" in r["questions"]][0]
    assert "West, Pens, 10, 120" in noul["state"]["context"]       # the DONE check sees the exact request too
    assert CountingCapture.observes <= 4    # 3 decisions: 1 initial + 2 after-action snapshots reused (+1 slack)
