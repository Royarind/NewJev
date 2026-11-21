import pytest

from app.jev.client import JevClient
from tests.fakes import answer

ACTIONS = [
    {"id": "e1", "kind": "fill", "node": 1, "role": "textbox", "label": "Search", "value": ""},
    {"id": "e2", "kind": "click", "node": 1, "role": "textbox", "label": "Open Search", "value": ""},
    {"id": "e3", "kind": "click", "node": 2, "role": "button", "label": "Go", "extra_ops": ["DOUBLE_CLICK"]},
    {"id": "e4", "kind": "select", "node": 3, "role": "combobox", "label": "Class → Economy", "value": "eco",
     "current_value": "First"},
    {"id": "key_1", "kind": "key", "label": "Enter", "key": "Enter"},
    {"id": "tab_1", "kind": "tab", "label": "Tab: Other", "tab_index": 1},
    {"id": "scroll_down", "kind": "scroll", "op": "SCROLL_DOWN", "label": "Scroll down"},
    {"id": "go_back", "kind": "nav", "op": "GO_BACK", "label": "Back"},
]


def test_action_space_groups_and_ops():
    space = JevClient.build_action_space(ACTIONS)
    assert [e["index"] for e in space["elements"]] == ["1", "2", "3"]
    assert set(space["elements"][0]["operations"]) == {"TYPE_TEXT", "APPEND_TEXT", "TYPE_SUBMIT", "CLICK"}
    assert space["op_group"]["DOUBLE_CLICK"] == "click"
    assert space["op_group"]["PRESS_KEY"] == "key"
    assert space["op_group"]["SWITCH_TAB"] == "tab"
    assert set(space["groups"]["key"]) == {"K1"}
    assert set(space["groups"]["select"]) == {"3:1"}
    assert set(space["controls"]) == {"SCROLL_DOWN", "GO_BACK"}


def test_request_and_parse_key_target():
    jev = JevClient()
    page = {"url": "u", "title": "t", "text": "x", "actions": ACTIONS, "sensitive": {"password": True}}
    history = [{"operation": "NOTE", "note": "loop detected"}]
    body, space, ops = jev.build_request(page, "search cats", history, overall_goal="big goal")
    assert {"NEED_USER", "DONE", "BLOCKED", "WAIT", "PRESS_KEY", "GO_BACK"} <= set(ops)
    assert "RIGHT_CLICK" not in ops and "LAUNCH_APP" not in ops
    assert body["state"]["page"]["sensitive"] == {"password": True}
    assert body["state"]["recent_actions"] == [{"operation": "NOTE", "note": "loop detected"}]
    assert body["questions"]["operation"]["instructions"]["overall_goal_context_only"] == "big goal"
    result = {"answers": {
        "operation": answer("PRESS_KEY", list(ops), 0.8),
        "key_target": answer("K1", ["K1"], 1.0),
    }}
    d = jev.parse_decision(result, body, space, ops)
    assert d.choice == "key_1" and d.action["key"] == "Enter"
    assert d.effective_confidence == pytest.approx(0.8)


def test_parse_append_uses_fill_target():
    jev = JevClient()
    page = {"url": "u", "title": "t", "text": "x", "actions": ACTIONS}
    body, space, ops = jev.build_request(page, "g", [])
    fill_keys = list(body["questions"]["fill_target"]["criteria"])
    result = {"answers": {"operation": answer("APPEND_TEXT", list(ops), 0.9),
                          "fill_target": answer("1", fill_keys, 0.5)}}
    d = jev.parse_decision(result, body, space, ops)
    assert d.choice == "e1" and d.operation == "APPEND_TEXT"
    assert d.effective_confidence == pytest.approx(0.5)


def test_invalid_answer_rejected():
    jev = JevClient()
    page = {"url": "u", "title": "t", "text": "x", "actions": ACTIONS}
    body, space, ops = jev.build_request(page, "g", [])
    bad = {"answers": {"operation": {"choice": "FLY", "confidence": 1, "probabilities": {"FLY": 1}}}}
    with pytest.raises(ValueError):
        jev.parse_decision(bad, body, space, ops)


def test_phase_rule_added_for_multi_phase():
    jev = JevClient()
    page = {"url": "u", "title": "t", "text": "x", "actions": ACTIONS}
    body, _, _ = jev.build_request(page, "calculate 1250/25", [], overall_goal="calc then paste in notepad")
    ins = body["questions"]["operation"]["instructions"]
    assert ins["goal"] == "calculate 1250/25" and "phase_rule" in ins
    assert "overall_goal" not in ins


def test_large_uis_are_trimmed_to_jev_limit():
    from app.jev.client import MAX_CHOICES
    acts = [{"id": f"c{i}", "kind": "click", "node": i, "role": "dataitem", "label": f"Cell R{i}"} for i in range(400)]
    acts.append({"id": "sort", "kind": "click", "node": 999, "role": "button", "label": "Sort A to Z"})
    acts.append({"id": "m", "kind": "click", "node": 1000, "role": "menuitem", "label": "[menu] Rename"})
    body, space, _ = JevClient().build_request({"url": "u", "title": "t", "text": "", "actions": acts},
                                              "sort the table", [])
    crit = body["questions"]["click_target"]["criteria"]
    total = sum(len(q["criteria"]) for q in body["questions"].values())
    assert total <= MAX_CHOICES <= 255          # budget is for the whole request, not per question
    assert len(body["state"]["elements"]) <= MAX_CHOICES
    labels = " ".join(c["element"] for c in crit.values())
    assert "Sort A to Z" in labels and "[menu] Rename" in labels


def test_budget_shared_across_groups_with_keys_and_fields():
    from app.jev.client import MAX_CHOICES
    acts = [{"id": f"c{i}", "kind": "click", "node": i, "role": "button", "label": f"Ribbon {i}"} for i in range(300)]
    acts += [{"id": f"f{i}", "kind": "fill", "node": 1000 + i, "role": "textbox", "label": f"Field {i}"} for i in range(60)]
    acts += [{"id": f"k{i}", "kind": "key", "label": f"Key {i}", "keys": ["a"]} for i in range(45)]
    acts += [{"id": f"w{i}", "kind": "window", "label": f"Window {i}"} for i in range(20)]
    body, space, ops = JevClient().build_request({"url": "u", "title": "t", "text": "", "actions": acts}, "g", [])
    total = sum(len(q["criteria"]) for q in body["questions"].values())
    assert total <= MAX_CHOICES
    assert len(body["questions"]["fill_target"]["criteria"]) >= 10 and len(body["questions"]["key_target"]["criteria"]) == 40
