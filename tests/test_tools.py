"""Unit and integration tests for NewJev's Capability Registry, Tool Dispatcher, and Builtin Tools."""

import json
import tempfile
from pathlib import Path

import pytest

from app.tools.base import Tool, ToolResult, ToolRisk
from app.tools.builtin import is_safe_url, register_builtin_tools
from app.tools.dispatcher import ToolDispatcher
from app.tools.loop import ToolLoop
from app.tools.registry import CapabilityRegistry
from app.tools.router import ActionRouter
from tests.fakes import FakeLLM


def test_capability_registry():
    reg = CapabilityRegistry()
    assert len(reg.list_tools()) == 0

    tool = Tool(
        name="test_tool",
        description="A test tool",
        parameters={"type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"]},
        risk_level=ToolRisk.READ_ONLY,
        handler=lambda args, **k: f"Echo: {args['msg']}",
    )
    reg.register(tool)
    assert reg.get("test_tool") is tool
    assert len(reg.list_tools()) == 1

    manifest = reg.export_manifest()
    assert manifest["counts"]["total"] == 1
    assert manifest["counts"]["by_risk"]["read_only"] == 1

    schemas = reg.export_schemas()
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "test_tool"


def test_tool_dispatcher_validation_and_execution():
    reg = CapabilityRegistry()
    tool = Tool(
        name="add",
        description="Add two numbers",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
        risk_level=ToolRisk.READ_ONLY,
        handler=lambda args, **k: args["a"] + args["b"],
    )
    reg.register(tool)
    dispatcher = ToolDispatcher(registry=reg)

    # Valid execution
    res = dispatcher.dispatch("add", {"a": 10, "b": 25})
    assert res.success is True
    assert res.output == 35

    # Missing required argument
    res_err = dispatcher.dispatch("add", {"a": 10})
    assert res_err.success is False
    assert "Missing required parameter" in res_err.error

    # Wrong type
    res_type = dispatcher.dispatch("add", {"a": "ten", "b": 20})
    assert res_type.success is False
    assert "must be a number" in res_type.error

    # Unregistered tool
    res_unreg = dispatcher.dispatch("unknown", {})
    assert res_unreg.success is False
    assert "not registered" in res_unreg.error


def test_tool_dispatcher_risk_confirmation():
    reg = CapabilityRegistry()
    tool = Tool(
        name="send_alert",
        description="External alert",
        parameters={"type": "object", "properties": {"msg": {"type": "string"}}},
        risk_level=ToolRisk.EXTERNAL_COMMUNICATION,
        handler=lambda args, **k: "sent",
    )
    reg.register(tool)

    # Rejection by callback
    dispatcher_reject = ToolDispatcher(registry=reg, confirm_callback=lambda *a: False)
    res_rej = dispatcher_reject.dispatch("send_alert", {"msg": "hi"})
    assert res_rej.success is False
    assert "rejected execution" in res_rej.error

    # Approval by callback
    dispatcher_ok = ToolDispatcher(registry=reg, confirm_callback=lambda *a: True)
    res_ok = dispatcher_ok.dispatch("send_alert", {"msg": "hi"})
    assert res_ok.success is True
    assert res_ok.output == "sent"


def test_ssrf_guard():
    assert is_safe_url("https://example.com/api")[0] is True
    assert is_safe_url("http://127.0.0.1:8080")[0] is False
    assert is_safe_url("http://localhost:3000")[0] is False
    assert is_safe_url("http://10.0.0.1/secret")[0] is False
    assert is_safe_url("http://192.168.1.1/admin")[0] is False
    assert is_safe_url("file:///etc/passwd")[0] is False


def test_file_tools_in_registry(tmp_path):
    reg = CapabilityRegistry()
    register_builtin_tools(reg)
    dispatcher = ToolDispatcher(registry=reg)

    test_file = tmp_path / "notes.txt"

    # Write file
    w_res = dispatcher.dispatch("write_file", {"path": str(test_file), "content": "hello world"})
    assert w_res.success is True
    assert test_file.exists()

    # Refuse overwrite without flag
    w_dup = dispatcher.dispatch("write_file", {"path": str(test_file), "content": "new"})
    assert w_dup.success is False
    assert "already exists" in w_dup.error

    # Allow overwrite with flag
    w_over = dispatcher.dispatch("write_file", {"path": str(test_file), "content": "updated", "overwrite": True})
    assert w_over.success is True
    assert test_file.read_text(encoding="utf-8") == "updated"

    # Read file
    r_res = dispatcher.dispatch("read_file", {"path": str(test_file)})
    assert r_res.success is True
    assert "updated" in str(r_res.output)

    # Search files
    s_res = dispatcher.dispatch("search_files", {"query": "notes.txt", "locations": [str(tmp_path)]})
    assert s_res.success is True
    assert any("notes.txt" in item["name"] for item in s_res.output)


def test_semantic_search_tool(tmp_path):
    reg = CapabilityRegistry()
    register_builtin_tools(reg)
    dispatcher = ToolDispatcher(registry=reg)

    doc1 = tmp_path / "q3_budget.txt"
    doc1.write_text("The Q3 marketing budget allocation is $50,000 for digital advertising campaigns.", encoding="utf-8")

    doc2 = tmp_path / "hiring.txt"
    doc2.write_text("Engineering headcount roadmap for frontend and backend roles.", encoding="utf-8")

    res = dispatcher.dispatch(
        "semantic_search",
        {"query": "marketing budget", "locations": [str(tmp_path)], "file_types": ["txt"]},
    )
    assert res.success is True
    matches = res.output.get("matches", [])
    assert len(matches) > 0
    assert "q3_budget.txt" in matches[0]["filename"]


def test_action_router():
    router = ActionRouter()

    # Ambiguous email -> ask user
    d1 = router.route("Send an email with the report")
    assert d1.route_type == "ask_user"
    assert "recipient" in d1.clarification_question.lower()

    # Inspect first for latest invoice
    d2 = router.route("Get the latest invoice and organize it")
    assert d2.route_type == "inspect_first"
    assert d2.recommended_tool == "search_files"

    # Direct tool for reading table
    d3 = router.route("Read the summary of data.csv table")
    assert d3.route_type == "direct_tool"
    assert d3.recommended_tool == "read_table"

    # Browser automation for travel
    d4 = router.route("Search Google Flights for tickets to Paris")
    assert d4.route_type == "browser"


def test_tool_loop_execution():
    reg = CapabilityRegistry()
    reg.register(Tool(
        name="get_weather",
        description="Get weather for a city",
        parameters={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        risk_level=ToolRisk.READ_ONLY,
        handler=lambda args, **k: f"Weather in {args['city']}: Sunny, 24C",
    ))

    dispatcher = ToolDispatcher(registry=reg)

    class ScriptedLLM:
        def __init__(self):
            self.turns = [
                '{"action": "call", "tool": "get_weather", "arguments": {"city": "Tokyo"}, "rationale": "need weather"}',
                '{"action": "finish", "final_answer": "Tokyo is sunny at 24C", "rationale": "done"}',
            ]

        def complete(self, msgs):
            text = self.turns.pop(0)
            return type("Resp", (), {"content": text})()

    loop = ToolLoop(registry=reg, dispatcher=dispatcher, llm=ScriptedLLM(), max_steps=5)
    res = loop.run("What is the weather in Tokyo?")
    assert res.status == "done"
    assert "Tokyo is sunny" in res.final_answer
    assert len(res.steps) == 1
    assert res.steps[0].tool == "get_weather"


def test_tool_dispatcher_fail_closed_without_callback():
    reg = CapabilityRegistry()
    reg.register(Tool(
        name="delete_account",
        description="Destructive action",
        parameters={"type": "object", "properties": {"user": {"type": "string"}}, "required": ["user"]},
        risk_level=ToolRisk.DESTRUCTIVE,
        handler=lambda args, **k: "deleted",
    ))
    dispatcher = ToolDispatcher(registry=reg, confirm_callback=None)
    res = dispatcher.dispatch("delete_account", {"user": "alice"})
    assert res.success is False
    assert "fail-closed" in res.error.lower()


def test_tool_dispatcher_timeout_does_not_block():
    import threading
    import time
    reg = CapabilityRegistry()
    reg.register(Tool(
        name="slow_task",
        description="Slow task",
        parameters={"type": "object"},
        risk_level=ToolRisk.READ_ONLY,
        handler=lambda args, **k: threading.Event().wait(0.3) or "done",
    ))
    dispatcher = ToolDispatcher(registry=reg, timeout=0.05)
    t0 = time.perf_counter()
    res = dispatcher.dispatch("slow_task", {})
    elapsed = time.perf_counter() - t0
    assert res.success is False
    assert "timed out" in res.error.lower()
    assert elapsed < 0.2  # Must not block on thread shutdown


def test_tool_loop_unverified_when_criteria_fail():
    reg = CapabilityRegistry()
    reg.register(Tool(
        name="noop",
        description="No-op",
        parameters={"type": "object"},
        risk_level=ToolRisk.READ_ONLY,
        handler=lambda args, **k: "ok",
    ))
    dispatcher = ToolDispatcher(registry=reg)

    class PrematureFinishLLM:
        def complete(self, msgs):
            return type("Resp", (), {"content": '{"action": "finish", "final_answer": "Done!", "rationale": "finished"}'})()

    loop = ToolLoop(registry=reg, dispatcher=dispatcher, llm=PrematureFinishLLM(), max_steps=2)
    res = loop.run("Save report", evidence_required=["file_exists: non_existent_file_xyz_123.txt"])
    assert res.status == "unverified"
    assert res.evidence.get("verified") is False


def test_browser_open_requires_active_context():
    reg = CapabilityRegistry()
    register_builtin_tools(reg)
    dispatcher = ToolDispatcher(registry=reg)
    res = dispatcher.dispatch("browser_open", {"url": "https://example.com"})
    assert res.success is False
    assert "browser session" in res.error.lower()


def test_extract_web_content_blocks_private_redirect(monkeypatch):
    import httpx
    from app.tools.builtin import _tool_extract_web_content

    class FakeRedirectResp:
        is_redirect = True
        status_code = 302
        headers = {"location": "http://127.0.0.1:8080/internal"}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url, **kw):
            return FakeRedirectResp()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    res = _tool_extract_web_content({"url": "https://example.com/redirect"})
    assert res.success is False
    assert "validation failed" in res.error.lower()
    assert "127.0.0.1" in res.error or "restricted" in res.error.lower() or "localhost" in res.error.lower()


def test_read_table_cell_range(tmp_path):
    csv_file = tmp_path / "sheet.csv"
    csv_file.write_text("Name,Age,Role\nAlice,30,Engineer\nBob,25,Designer\nCharlie,35,Manager\n", encoding="utf-8")

    reg = CapabilityRegistry()
    register_builtin_tools(reg)
    dispatcher = ToolDispatcher(registry=reg)

    res = dispatcher.dispatch("read_table", {"path": str(csv_file), "range": "A1:B3"})
    assert res.success is True
    out_str = str(res.output)
    assert "Alice" in out_str
    assert "30" in out_str
    assert "Bob" in out_str
    # Manager is on row 4, outside A1:B3
    assert "Charlie" not in out_str
    # Role is col C, outside A1:B3
    assert "Engineer" not in out_str


def test_semantic_search_unifies_with_knowledge_store(tmp_path):
    from app.agent.memory import MemoryStore, set_memory
    mem = MemoryStore(base_dir=tmp_path)

    doc = tmp_path / "secret_specs.txt"
    doc.write_text("Quantum processor specifications and cryogenic cooling details.", encoding="utf-8")
    assert mem.add_to_knowledge_index(doc, tags="hardware") is True

    set_memory(mem)
    try:
        reg = CapabilityRegistry()
        register_builtin_tools(reg)
        dispatcher = ToolDispatcher(registry=reg)
        res = dispatcher.dispatch("semantic_search", {"query": "cryogenic cooling"})
        assert res.success is True
        matches = res.output.get("matches", [])
        assert len(matches) > 0
        assert any(m.get("source") == "knowledge_index" and "secret_specs.txt" in m["filename"] for m in matches)
    finally:
        set_memory(None)

