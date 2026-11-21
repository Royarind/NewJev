import os
import tempfile
from pathlib import Path

import app.capture.desktop as desktop
from app.agent import planner
from app.agent.guards import is_delete
from app.agent.review import AutoReviewer, ConsoleReviewer, EventReviewer
from app.capture import files


def _fake_home():
    root = Path(tempfile.mkdtemp())
    kf = {n: str(root / n) for n in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")}
    for v in kf.values():
        os.makedirs(v)
    desktop._known_cache = kf
    return root, kf


def test_referenced_files_and_hints():
    assert files.referenced_files("Open NewJev_test1.txt from the Desktop") == ["NewJev_test1.txt"]
    assert files.referenced_files("open 'Q3 report.docx' please") == ["Q3 report.docx"]
    assert files.referenced_files(r"edit C:\Users\me\Desktop\a b.csv now")[0] == r"C:\Users\me\Desktop\a b.csv"
    assert files.referenced_files("calculate 12.5 divided by 2") == []


def test_find_files_prefers_hint_exact_and_newest():
    _, kf = _fake_home()
    (Path(kf["Documents"]) / "sub").mkdir()
    (Path(kf["Documents"]) / "sub" / "notes.txt").write_text("a")
    (Path(kf["Desktop"]) / "notes.txt").write_text("b")
    (Path(kf["Desktop"]) / "notes_old.txt").write_text("c")
    hits = files.find_files("notes.txt", hint_dir=kf["Desktop"])
    assert hits[0] == Path(kf["Desktop"]) / "notes.txt" and len(hits) == 2
    assert files.find_files("*.txt", roots=[kf["Desktop"]])[0].suffix == ".txt"
    assert files.find_files("~/Desktop/notes.txt") == [Path(kf["Desktop"]) / "notes.txt"]
    assert files.find_files("missing.txt") == []


def test_existing_file_for_task_vs_create():
    _, kf = _fake_home()
    (Path(kf["Desktop"]) / "NewJev_test1.txt").write_text("x")
    assert files.existing_file_for_task("Open NewJev_test1.txt from the Desktop and add a line") is not None
    assert files.existing_file_for_task("Type hi and save it on the Desktop as NewJev_test1.txt") is None
    assert files.existing_file_for_task("Open nothere.txt") is None


def test_planner_starts_existing_file_in_explorer():
    _, kf = _fake_home()
    (Path(kf["Desktop"]) / "NewJev_test1.txt").write_text("x")
    p = planner.Planner.parse('{"goal":"g","steps":[{"task":"Open NewJev_test1.txt from the Desktop in Notepad, '
                              'add Line two and save","mode":"desktop","url":"notepad"}]}', "g")
    st = p["steps"][0]
    assert st["url"] == f"explorer {kf['Desktop']}"
    assert st["existing_file"].endswith("NewJev_test1.txt") and "do not create a new one" in st["task"]


def test_protected_paths():
    _, kf = _fake_home()
    assert files.is_protected(Path(kf["Desktop"]))
    assert files.is_protected(Path("/"))
    assert not files.is_protected(Path(kf["Desktop"]) / "x.txt")


def test_delete_detection():
    explorer = {"url": "desktop://explorer.exe/Desktop", "mode": "desktop"}
    notepad = {"url": "desktop://notepad.exe/x", "mode": "desktop"}
    assert is_delete("PRESS_KEY", "Delete", explorer)
    assert not is_delete("PRESS_KEY", "Delete", notepad)          # deleting a character
    assert is_delete("PRESS_KEY", "Shift+Delete", notepad)
    assert is_delete("CLICK", "Delete", {"url": "https://mail.google.com", "mode": "browser"})
    assert not is_delete("CLICK", "Deleted items folder", explorer)


def test_reviewers_never_blanket_approve_deletes():
    assert ConsoleReviewer(auto_approve=True).confirm("risky_action", "x")
    assert not AutoReviewer().confirm("delete", "x", {"path": "/tmp/report.txt"})
    assert AutoReviewer(allow_delete_prefix="NewJev_Bench_").confirm("delete", "x", {"path": "/d/NewJev_Bench_a.txt"})
    r = EventReviewer(lambda *a: None, timeout_s=0.2)
    r._always_allow.add("low_confidence")
    assert r.confirm("low_confidence", "x")
    assert not r.confirm("delete", "x")   # times out -> not approved, never auto-allowed


def test_context_menu_delete_needs_confirmation():
    explorer = {"url": "desktop://explorer.exe/Downloads", "mode": "desktop"}
    assert is_delete("CLICK", "[menu] Delete", explorer)
    assert not is_delete("CLICK", "[menu] Rename", explorer)
    assert not is_delete("CLICK", "[menu] Show more options", explorer)


def test_jev_rules_mention_show_more_options():
    from app.jev.client import JevClient
    assert "Show more options" in JevClient.NEXT_ACTION and "Shift+F10" in JevClient.NEXT_ACTION
    from app.capture.desktop import DESKTOP_KEYS
    assert any(k[1] == ("shift", "f10") for k in DESKTOP_KEYS)


def test_recent_files_suggests_what_was_saved_instead(tmp_path):
    from app.capture.files import recent_files
    (tmp_path / "line one.txt").write_text("line one")
    (tmp_path / "other.csv").write_text("x")
    out = recent_files(str(tmp_path / "NewJev_ops.txt"))
    assert out and "line one.txt" in out[0] and not any("other.csv" in o for o in out)
