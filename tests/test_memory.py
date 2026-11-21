"""Unit tests for NewJev's Context-Aware Memory (Preferences, Knowledge Index, History, and Task Memory)."""

import pytest
from pathlib import Path
from app.agent.memory import MemoryStore, sanitize_sensitive


def test_sanitize_sensitive():
    text = "User login with password='SuperSecret123' and token='bearer 1234567890abcdef'."
    cleaned = sanitize_sensitive(text)
    assert "SuperSecret123" not in cleaned
    assert "[REDACTED]" in cleaned


def test_preference_memory(tmp_path):
    mem = MemoryStore(base_dir=tmp_path)

    # Empty initially
    assert mem.get_preferences() == {}

    # Set and get preferences
    mem.set_preference("preferred_save_folder", "Downloads")
    mem.set_preference("default_format", "csv")
    assert mem.get_preference("preferred_save_folder") == "Downloads"
    assert mem.get_preference("default_format") == "csv"

    # Reject sensitive keys
    with pytest.raises(ValueError, match="Sensitive key"):
        mem.set_preference("email_password", "123456")

    # Forget preference
    assert mem.forget_preference("default_format") is True
    assert mem.get_preference("default_format") is None
    assert mem.forget_preference("nonexistent") is False

    # Clear all preferences
    mem.clear_preferences()
    assert mem.get_preferences() == {}


def test_knowledge_index(tmp_path):
    mem = MemoryStore(base_dir=tmp_path)

    doc = tmp_path / "company_policy.txt"
    doc.write_text("Remote work expenses up to $500 per year are reimbursable through payroll.", encoding="utf-8")

    # Add to index
    ok = mem.add_to_knowledge_index(doc, tags="hr,policy")
    assert ok is True

    # List sources
    sources = mem.list_knowledge_sources()
    assert len(sources) == 1
    assert "company_policy.txt" in sources[0]["path"]

    # Search knowledge
    results = mem.search_knowledge("reimbursable payroll")
    assert len(results) > 0
    assert "company_policy.txt" in results[0]["filename"]
    assert "payroll" in results[0]["snippet"].lower()

    # Remove source
    mem.remove_knowledge_source(str(doc))
    assert len(mem.list_knowledge_sources()) == 0
    assert len(mem.search_knowledge("reimbursable")) == 0


def test_run_history_audit(tmp_path):
    mem = MemoryStore(base_dir=tmp_path)

    mem.record_run(
        run_id="run_101",
        goal="Download statement with secret token: a1b2c3d4e5f6g7h8",
        status="done",
        summary="Retrieved statement with token: 9876543210abcdef",
        metadata={"auth_header": "bearer 1234567890abcdef", "safe_field": "statement.pdf"},
    )

    history = mem.get_history()
    assert len(history) == 1
    assert history[0]["run_id"] == "run_101"
    assert history[0]["status"] == "done"
    # Ensure sensitive token patterns were sanitized in goal, summary, and metadata
    assert "9876543210abcdef" not in history[0]["summary"]
    assert "[REDACTED]" in history[0]["summary"]
    assert "a1b2c3d4e5f6g7h8" not in history[0]["goal"]
    assert "[REDACTED]" in history[0]["goal"]
    assert "1234567890abcdef" not in history[0]["metadata"]["auth_header"]
    assert "[REDACTED]" in history[0]["metadata"]["auth_header"]
    assert history[0]["metadata"]["safe_field"] == "statement.pdf"


def test_preference_logging_sanitized(tmp_path, caplog):
    import logging
    mem = MemoryStore(base_dir=tmp_path)
    with caplog.at_level(logging.INFO):
        mem.set_preference("notes", "Use token: 9876543210abcdef")
    assert "9876543210abcdef" not in caplog.text
    assert "[REDACTED]" in caplog.text

