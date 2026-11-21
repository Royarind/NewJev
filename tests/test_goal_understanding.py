"""Unit tests for Goal Understanding and Targeted Verification."""

from app.agent.planner import (
    Planner,
    StructuredGoal,
    fallback_structured_goal,
)
from app.agent.verifier import Verifier
from tests.fakes import ScriptedJev


def test_fallback_structured_goal_ask_decision():
    # Email without recipient -> should decide to ask
    sg = fallback_structured_goal("Send an email with the quarterly summary")
    assert sg.decision == "ask"
    assert sg.clarification_question is not None
    assert "recipient" in sg.clarification_question.lower()


def test_fallback_structured_goal_inspect_decision():
    # "Get latest invoice and organize it" -> should decide to inspect first
    sg = fallback_structured_goal("Get the latest invoice and organize it")
    assert sg.decision == "inspect"
    assert any("invoice" in str(a).lower() for a in sg.assumptions)


def test_fallback_structured_goal_act_with_evidence():
    # Specific file saving goal
    sg = fallback_structured_goal('Calculate sum and save to "report.csv"')
    assert sg.decision == "act"
    assert any("file_exists: report.csv" in e for e in sg.evidence_of_success)
    assert sg.inputs.get("file") == "report.csv"


def test_planner_structured_goal_parsing():
    raw_response = '''```json
{
  "desired_outcome": "Find cheapest flight from JFK to LHR on Nov 10 and report details",
  "constraints": ["Extract airline, duration, stops, and price"],
  "inputs": {"origin": "JFK", "destination": "LHR", "date": "Nov 10"},
  "assumptions": ["Currency in USD", "Direct flights preferred"],
  "evidence_of_success": ["answer_reported"],
  "decision": "act",
  "recommended_mode": "browser",
  "steps": [
    {"task": "Navigate to Google Flights and search JFK to LHR", "mode": "browser", "url": "https://www.google.com/travel/flights"}
  ]
}
```'''
    sg = Planner.parse_structured(raw_response, "find flight")
    assert isinstance(sg, StructuredGoal)
    assert sg.decision == "act"
    assert sg.recommended_mode == "browser"
    assert sg.evidence_of_success == ["answer_reported"]
    assert len(sg.steps) == 1

    d = sg.to_dict()
    assert d["capture_mode"] == "browser"
    assert d["goal"] == "Find cheapest flight from JFK to LHR on Nov 10 and report details"
    assert "inputs" in d


def test_verifier_targeted_criteria(tmp_path):
    jev = ScriptedJev([("DONE", None, 0.95)], noul=[0.95])
    verifier = Verifier(jev)

    sample_file = tmp_path / "result.txt"
    sample_file.write_text("Revenue: $120,000", encoding="utf-8")

    # 1. file_exists check
    ok, score, details = verifier.verify_criteria(
        [f"file_exists: {sample_file}"],
        page={},
        history=[],
    )
    assert ok is True
    assert score == 1.0

    # 2. file_contains check
    ok_c, score_c, _ = verifier.verify_criteria(
        [f"file_contains: {sample_file} | Revenue: $120,000"],
        page={},
        history=[],
    )
    assert ok_c is True

    # 3. file_contains check failure
    fail_c, _, det = verifier.verify_criteria(
        [f"file_contains: {sample_file} | Deficit"],
        page={},
        history=[],
    )
    assert fail_c is False
    assert "Failed criteria" in det["explanation"]

    # 4. answer_reported check
    ok_rep, _, _ = verifier.verify_criteria(
        ["answer_reported"],
        page={},
        history=[],
        final_report="Found Revenue: $120,000",
    )
    assert ok_rep is True
