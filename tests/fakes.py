"""Fake Jev / LLM / capture used by loop tests."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.jev.client import JevClient


def answer(choice: str, keys: list[str], conf: float = 0.9) -> dict:
    rest = (1 - conf) / max(1, len(keys) - 1) if len(keys) > 1 else 0
    probs = {k: (conf if k == choice else rest) for k in keys}
    if len(keys) == 1:
        probs = {choice: 1.0}
    return {"choice": choice, "confidence": conf, "probabilities": probs}


class ScriptedJev(JevClient):
    """Real request building / parsing; responses come from a script of (operation, target_label, conf)."""

    def __init__(self, script: list[tuple[str, str | None, float]], noul: list[float] | None = None) -> None:
        super().__init__()
        self.script = list(script)
        self.noul_values = list(noul or [])
        self.requests: list[dict] = []

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(body)
        if "check" in body["questions"]:
            return {"answers": {"check": {"noul": self.noul_values.pop(0) if self.noul_values else 0.95}}}
        op, label, conf = self.script.pop(0) if self.script else ("BLOCKED", None, 0.9)
        qs = body["questions"]
        answers = {"operation": answer(op, list(qs["operation"]["criteria"]), conf)}
        for qname, q in qs.items():
            if qname == "operation":
                continue
            keys = list(q["criteria"])
            pick = keys[0]
            if label is not None:
                for k, c in q["criteria"].items():
                    if label in c["element"]:
                        pick = k
                        break
            answers[qname] = answer(pick, keys, conf)
        return {"answers": answers, "model": "fake"}


class FakeLLM:
    def __init__(self, value: str = "hello") -> None:
        self.value = value
        self.calls = 0

    def complete(self, messages, **kw):
        self.calls += 1
        return type("R", (), {"content": json.dumps({"text": self.value})})()


class FakeCapture:
    """A tiny 'app': a text field and a Send button. Typing changes state; Send marks it sent."""

    mode = "browser"
    is_alive = True

    def __init__(self, sensitive: dict | None = None) -> None:
        self.value = ""
        self.sent = False
        self.acts: list[tuple] = []
        self.sensitive = sensitive or {}

    def start(self, url=None):
        pass

    def navigate(self, url):
        self.acts.append(("navigate", url))

    def observe(self, screenshot=False):
        actions = [
            {"id": "e1", "kind": "fill", "node": 1, "role": "textbox", "label": "Message", "value": self.value},
            {"id": "e2", "kind": "click", "node": 2, "role": "button", "label": "Send"},
            {"id": "scroll_down", "kind": "scroll", "op": "SCROLL_DOWN", "label": "Scroll down"},
            {"id": "key_1", "kind": "key", "label": "Enter", "key": "Enter"},
        ]
        page = {"url": "https://x.test/", "title": "X", "text": f"value={self.value} sent={self.sent}",
                "actions": actions, "sensitive": dict(self.sensitive), "mode": self.mode}
        page["fingerprint"] = hashlib.md5(json.dumps(page["text"]).encode()).hexdigest()
        return page

    def act(self, page, action_id, text=None, operation=None):
        self.acts.append((action_id, operation, text))
        if action_id == "e1":
            self.value = text if operation == "TYPE_TEXT" else self.value + (text or "")
        elif action_id == "e2":
            self.sent = True
        return {"executed": action_id}

    def screenshot(self, path):
        pass

    def close(self):
        pass
