"""Platform-independent parts of the desktop capture (the UIA parts need Windows)."""

from app.capture.desktop import DESKTOP_KEYS, canonical_app, fingerprint, window_matches


def test_canonical_app():
    assert canonical_app("calc") == "calculator"
    assert canonical_app("Microsoft Word") == "word"
    assert canonical_app("explorer downloads") == "explorer"
    assert canonical_app("notepad.exe") == "notepad"
    assert canonical_app("SomeTool") == "sometool"


def test_window_matching_prefers_process():
    assert window_matches("word", "Document1 - Word", "winword.exe")
    assert window_matches("calculator", "Calculator", "applicationframehost.exe")  # UWP host, matched by title
    assert not window_matches("calculator", "Notes about calculators", "notepad.exe")
    assert window_matches("explorer", "Downloads", "explorer.exe", "CabinetWClass")
    assert not window_matches("explorer", "Taskbar", "explorer.exe", "Shell_TrayWnd")


def test_keys_and_fingerprint():
    labels = [k[0] for k in DESKTOP_KEYS]
    assert any(l.startswith("Ctrl+Shift+N") for l in labels)
    a = {"title": "t", "text": "x", "actions": [{"kind": "fill", "label": "Doc", "value": "a"}]}
    b = {"title": "t", "text": "x", "actions": [{"kind": "fill", "label": "Doc", "value": "ab"}]}
    assert fingerprint(a) != fingerprint(b)


def test_expand_known_paths():
    from pathlib import Path

    from app.capture.desktop import expand_known, known_folders

    kf = known_folders()
    assert set(kf) >= {"Desktop", "Documents", "Downloads"}
    assert expand_known("~/Desktop/NewJev_x.txt") == str(Path(kf["Desktop"]) / "NewJev_x.txt")
    assert expand_known("~/desktop") == kf["Desktop"]


# ------------------------------------------------ typing must never leave garbage behind
import sys
import types


class _Editor:
    """A text control with an undo stack, like Notepad."""

    def __init__(self, text=""):
        self.text, self.undo, self.Name = text, [], "Text editor"

    def GetValuePattern(self):
        return types.SimpleNamespace(Value=self.text)

    def insert(self, s):
        self.undo.append(self.text)
        self.text += s


def _fake_desktop(editor, garble):
    import app.capture.desktop as d
    keys = types.ModuleType("pyautogui")

    def hotkey(*k):
        if k == ("ctrl", "z") and editor.undo:
            editor.text = editor.undo.pop()
        elif k == ("ctrl", "a"):
            editor.selected = True

    def press(k, presses=1, interval=0):
        if k == "delete" and getattr(editor, "selected", False):
            editor.insert("")
            editor.text, editor.selected = "", False
        if k == "backspace":
            editor.text = editor.text[:-presses]

    keys.hotkey, keys.press = hotkey, press
    sys.modules["pyautogui"] = keys
    cap = d.DesktopCapture.__new__(d.DesktopCapture)
    state = {"n": 0}

    def send(s, delay=0.015):
        state["n"] += 1
        if garble and state["n"] % 2 == 1:      # every first attempt of a chunk is garbled
            for c in s[::2]:
                editor.insert(c * 3)
        else:
            editor.insert(s)
        return True

    cap._send_chars = send
    cap.typed_attempts = state
    cap._focused_editable = lambda ctrl=None: True
    cap._paste_text = lambda s, wait=0.35: editor.insert(s)
    d.time.sleep = lambda *_: None
    return cap


def test_long_text_is_typed_and_garbled_chunks_are_retyped():
    ed = _Editor("first line\n")
    cap = _fake_desktop(ed, garble=True)
    pasted = []
    cap._paste_text = lambda s, wait=0.35: pasted.append(s)
    text = "The ocean is a vast and mysterious body of water that covers most of the Earth."
    cap._enter_text(ed, text)
    assert ed.text == "first line\n" + text      # typed, not pasted, and exactly once
    assert pasted == [] and cap.typed_attempts["n"] > 4


def test_chunks_and_arrival_check():
    from app.capture.desktop import _canon, _chunk_arrived, _chunks
    parts = _chunks("The ocean is a vast and mysterious body of water.")
    assert "".join(parts) == "The ocean is a vast and mysterious body of water."
    assert all(len(p) <= 30 for p in parts)
    assert _chunk_arrived("Hi ", "Hi there ", "there ")
    assert not _chunk_arrived("Hi ", "Hi thhheeerrre ", "there ")
    assert _chunk_arrived("", _canon("\u201cquoted\u201d"), '"quoted"')  # Word smart quotes


def test_garbled_short_text_is_undone_before_retry():
    ed = _Editor("Dear Bob,")
    cap = _fake_desktop(ed, garble=True)
    cap._enter_text(ed, " see you")
    assert ed.text == "Dear Bob, see you"          # garbage undone, text inserted exactly once


def test_garbled_replace_clears_field_first():
    ed = _Editor("")
    cap = _fake_desktop(ed, garble=True)
    cap._enter_text(ed, "hello", replace=True)
    assert ed.text == "hello"


def test_user_files_named_newjev_are_not_the_agents_own_window():
    from app.agent.takeover import EXEMPT_TITLES
    from app.capture.desktop import is_own_ui
    assert is_own_ui("NewJev Agent - Google Chrome") and is_own_ui("NewJev Quick")
    assert not is_own_ui("NewJev_T01_notice.txt - Notepad")
    assert not any(t in "newjev_t01_notice.txt - notepad" for t in EXEMPT_TITLES)



def test_no_keys_are_sent_when_the_focus_is_not_in_a_text_field():
    """Safety: Ctrl+A / Delete / typing in a FILE LIST would select and delete files."""
    ed = _Editor("keep me")
    cap = _fake_desktop(ed, garble=False)
    sent = []
    cap._send_chars = lambda s, delay=0.0: sent.append(s) or True
    cap._paste_text = lambda s, wait=0.35: sent.append(("paste", s))
    cap._focused_editable = lambda ctrl=None: False
    try:
        cap._enter_text(ed, "notepad C:\\x.txt")
        raise AssertionError("typing must be refused")
    except ValueError as exc:
        assert "safety" in str(exc)
    assert sent == [] and ed.text == "keep me"
    assert cap._erase_to(ed, "") is False and ed.text == "keep me"     # no Ctrl+A + Delete
    try:
        cap._paste_verified(ed, "x", True)
        raise AssertionError
    except ValueError:
        pass
    from app.capture.desktop import DesktopCapture
    assert DesktopCapture._destructive_keys(["delete"]) and DesktopCapture._destructive_keys(["shift", "delete"])
    assert DesktopCapture._destructive_keys(["ctrl", "d"]) and not DesktopCapture._destructive_keys(["ctrl", "s"])
    assert DesktopCapture.DELETE_DIALOG.match("Delete Multiple Items")
    assert DesktopCapture.DELETE_DIALOG.match("Delete File") and not DesktopCapture.DELETE_DIALOG.match("Save As")
