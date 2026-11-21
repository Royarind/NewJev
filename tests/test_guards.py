from app.agent.guards import detect_stuck, is_risky, needs_handoff, parse_keywords
from app.agent.loop import _extract_field_value

KW = parse_keywords("send,delete,pay,place order")


def h(op, label, changed=True, conf=0.9, text=None):
    return {"operation": op, "label": label, "page_changed": changed, "confidence": conf, "text": text}


def test_repeat_detected_even_when_page_changes():
    assert detect_stuck([h("CLICK", "Next")] * 3, 3) is not None


def test_scrolling_is_not_a_repeat():
    assert detect_stuck([h("SCROLL_DOWN", "Scroll down")] * 5, 3) is None
    assert detect_stuck([h("SCROLL_DOWN", "Scroll down")] * 8, 3) is not None


def test_oscillation_and_no_change():
    assert "oscillat" in detect_stuck([h("CLICK", "A"), h("CLICK", "B")] * 2, 3)
    seq = [h("CLICK", str(i), changed=False) for i in range(4)]
    assert "without any visible change" in detect_stuck(seq, 3)


def test_calculator_digits_are_fine():
    seq = [h("CLICK", d) for d in ["One", "Two", "Five", "Zero", "Divide by", "Two", "Five", "Equals"]]
    assert detect_stuck(seq, 3) is None


def test_low_confidence_streak_only_counts_without_progress():
    progressing = [h("CLICK", str(i), conf=0.3) for i in range(5)]
    assert detect_stuck(progressing, 3, threshold=0.6) is None      # busy UI, but things are happening
    stalled = [h("CLICK", str(i), conf=0.3, changed=(i == 0)) for i in range(5)]
    assert detect_stuck(stalled, 3, threshold=0.6) is not None


def test_notes_ignored():
    seq = [h("CLICK", "A"), {"operation": "NOTE", "note": "x"}, h("CLICK", "B")]
    assert detect_stuck(seq, 3) is None


def test_risky():
    assert is_risky("CLICK", "Send", KW) == "send"
    assert is_risky("CLICK", "Delete forever", KW) == "delete"
    assert is_risky("CLICK", "Sender details", KW) is None
    assert is_risky("TYPE_TEXT", "Send", KW) is None
    assert is_risky("PRESS_KEY", "Enter (submit / confirm)", KW) is None
    assert is_risky("CLICK", "Place order", KW) == "place order"


def test_handoff():
    assert needs_handoff({"sensitive": {"password": True}})
    assert needs_handoff({"sensitive": {"captcha": True}}).startswith("A CAPTCHA")
    assert needs_handoff({"sensitive": {}}) is None


def test_extract_field_value():
    assert _extract_field_value('```json\n{"text": "hi"}\n```') == "hi"
    assert _extract_field_value('<think>x</think>{"text": "yo"}') == "yo"
    assert _extract_field_value({"text": "  a "}) == "a"
    assert _extract_field_value("") is None


def test_verifier_gets_action_history():
    from app.agent.verifier import Verifier

    class J:
        def noul(self, state, q):
            J.state = state
            return 0.9, 1

    v = Verifier(J())
    ok, _ = v.verify("cheapest flight", {"title": "Booking", "url": "u", "text": "IX 1056", "actions": []},
                     [{"operation": "CLICK", "label": "Cheapest from 6610"}, {"operation": "NOTE", "note": "x"},
                      {"operation": "TYPE_TEXT", "label": "Where from?", "text": "Delhi"}])
    assert ok
    assert "1. CLICK 'Cheapest from 6610'" in J.state and "2. TYPE_TEXT 'Where from?' with 'Delhi'" in J.state
