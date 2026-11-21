"""Real-browser checks for the Playwright executor (skipped when Playwright/Chromium is unavailable)."""

import json
import os
import tempfile
from pathlib import Path

import pytest

PAGE = """<!doctype html><html><head><title>Fixture</title></head><body>
<input id="q" aria-label="Search" />
<div id="ed" contenteditable="true" aria-label="Body" style="min-height:40px;border:1px solid"></div>
<div id="dd" role="button" aria-label="Open menu" tabindex="0">Menu</div>
<div id="menu" hidden><div role="option" aria-label="Pick me" onmousedown="document.title='picked'">Pick me</div></div>
<select aria-label="Class"><option value="f">First</option><option value="e">Economy</option></select>
<my-widget></my-widget>
<a href="tab2.html" target="_blank">Open second</a>
<iframe src="frame.html" style="width:400px;height:120px"></iframe>
<p id="out"></p>
<script>
  // only real pointer events open this menu (element.click() would not)
  document.getElementById('dd').addEventListener('mousedown', () => { document.getElementById('menu').hidden = false; });
  customElements.define('my-widget', class extends HTMLElement { connectedCallback() {
    const r = this.attachShadow({mode: 'open'});
    r.innerHTML = '<button aria-label="Shadow button">S</button>';
    r.querySelector('button').onclick = () => document.getElementById('out').textContent = 'shadow clicked';
  }});
  document.getElementById('q').addEventListener('keydown', e => { if (e.key === 'Enter') document.getElementById('out').textContent = 'searched ' + e.target.value; });
</script></body></html>"""
FRAME = """<!doctype html><html><body><input aria-label="Frame field" /><input type="password" aria-label="pw"/></body></html>"""
TAB2 = """<!doctype html><html><head><title>Second tab</title></head><body><p>second</p></body></html>"""


@pytest.fixture(scope="module")
def browser():
    pytest.importorskip("playwright")
    tmp = Path(tempfile.mkdtemp())
    (tmp / "index.html").write_text(PAGE)
    (tmp / "frame.html").write_text(FRAME)
    (tmp / "tab2.html").write_text(TAB2)
    os.environ.update(HEADLESS="true", BROWSER_CHANNEL="", BROWSER_USER_DATA_DIR=str(tmp / "profile"))
    from app.config.settings import get_settings
    get_settings.cache_clear()
    from app.capture.browser import BrowserCapture
    cap = BrowserCapture()
    try:
        cap.start((tmp / "index.html").as_uri())
        cap.fixture_url = (tmp / "index.html").as_uri()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"browser unavailable: {exc}")
    yield cap
    cap.close()
    get_settings.cache_clear()


def find(page, label, kind=None):
    for a in page["actions"]:
        if a.get("label") == label and (kind is None or a["kind"] == kind):
            return a
    raise AssertionError(f"{label} not found in {[x.get('label') for x in page['actions']]}")


def test_snapshot_sees_shadow_iframe_and_password(browser):
    page = browser.observe()
    find(page, "Shadow button")
    frame_field = find(page, "Frame field", "fill")
    assert frame_field["frame"] >= 1
    assert page["sensitive"].get("password") is True
    assert not any(a.get("label") == "pw" for a in page["actions"])  # password never offered


def test_fill_contenteditable_and_append(browser):
    page = browser.observe()
    browser.act(page, find(page, "Body", "fill")["id"], text="Hello", operation="TYPE_TEXT")
    page = browser.observe()
    assert find(page, "Body", "fill")["value"] == "Hello"
    browser.act(page, find(page, "Body", "fill")["id"], text=" world", operation="APPEND_TEXT")
    page = browser.observe()
    assert find(page, "Body", "fill")["value"] == "Hello world"


def test_type_then_press_enter(browser):
    page = browser.observe()
    browser.act(page, find(page, "Search", "fill")["id"], text="cats", operation="TYPE_TEXT")
    page = browser.observe()
    enter = next(a for a in page["actions"] if a["kind"] == "key" and a["key"] == "Enter")
    browser.act(page, enter["id"], operation="PRESS_KEY")
    assert "searched cats" in browser.observe()["text"]


def test_real_mouse_opens_mousedown_menu(browser):
    page = browser.observe()
    browser.act(page, find(page, "Open menu")["id"], operation="CLICK")
    page = browser.observe()
    browser.act(page, find(page, "Pick me")["id"], operation="CLICK")
    assert browser.observe()["title"] == "picked"


def test_shadow_click_select_and_iframe_fill(browser):
    page = browser.observe()
    browser.act(page, find(page, "Shadow button")["id"], operation="CLICK")
    page = browser.observe()
    assert "shadow clicked" in page["text"]
    browser.act(page, find(page, "Class → Economy", "select")["id"], operation="SELECT")
    page = browser.observe()
    browser.act(page, find(page, "Frame field", "fill")["id"], text="in frame", operation="TYPE_TEXT")
    page = browser.observe()
    assert find(page, "Frame field", "fill")["value"] == "in frame"
    assert any(a.get("label", "").startswith("Class → First") for a in page["actions"])  # Economy now selected


def test_new_tab_followed_and_switch_back(browser):
    for _ in range(2):  # headless Chromium sometimes swallows the very first popup of a context
        page = browser.observe()
        if page["title"] == "Second tab":
            break
        browser.act(page, find(page, "Open second")["id"], operation="CLICK")
    page = browser.observe()
    assert page["title"] == "Second tab"
    tab = next(a for a in page["actions"] if a["kind"] == "tab")
    browser.act(page, tab["id"], operation="SWITCH_TAB")
    assert browser.observe()["title"] in ("Fixture", "picked")


def test_agent_loop_end_to_end_on_real_page(browser):
    from app.agent.loop import AgentLoop
    from tests.fakes import FakeLLM, ScriptedJev

    page = browser.observe()
    if page["title"] != "Fixture" and page["title"] != "picked":
        browser.act(page, next(a for a in page["actions"] if a["kind"] == "tab")["id"], operation="SWITCH_TAB")
    jev = ScriptedJev([("TYPE_TEXT", "Search", 0.95), ("PRESS_KEY", "Enter", 0.95), ("DONE", None, 0.95)], noul=[0.9])
    from app.agent.review import Reviewer

    class Resume(Reviewer):
        interactive = True
        handoffs = 0

        def confirm(self, *a, **k):
            return True

        def handoff(self, *a, **k):  # the fixture iframe has a password field -> agent must hand off first
            Resume.handoffs += 1
            return True

    agent = AgentLoop(jev=jev, text_llm=FakeLLM("dogs"), reviewer=Resume())
    result = agent.run("search for dogs", browser)
    assert Resume.handoffs == 1
    assert result["status"] == "done"
    assert "searched dogs" in browser.observe()["text"]
    assert all(h.get("page_changed") for h in result["history"] if h["operation"] != "NOTE")


def test_bench_shop_fixture_with_scripted_decisions(browser):
    """The offline benchmark page: pointerdown dropdown, React-style number input, srcdoc iframe."""
    from app.agent.loop import AgentLoop
    from tests.fakes import ScriptedJev

    shop = Path(__file__).resolve().parents[1] / "bench" / "fixtures" / "shop.html"
    browser.navigate(shop.as_uri())

    class Values:
        def __init__(self):
            self.queue = ["2", "Happy birthday"]

        def complete(self, messages, **kw):
            return type("R", (), {"content": json.dumps({"text": self.queue.pop(0)})})()

    jev = ScriptedJev([("CLICK", "Size", 0.9), ("CLICK", "Large", 0.9), ("TYPE_TEXT", "Quantity", 0.9),
                       ("TYPE_TEXT", "Gift message", 0.9), ("CLICK", "Add to cart", 0.9), ("DONE", None, 0.9)],
                      noul=[0.9])
    try:
        result = AgentLoop(jev=jev, text_llm=Values()).run("buy", browser)
        assert result["status"] == "done", result["history"]
        assert "Cart: 2 × Large — gift message: Happy birthday" in browser.observe()["text"]
    finally:
        browser.navigate(browser.fixture_url)


def test_copy_text_selects_value_and_fills_clipboard(browser):
    """Ctrl+C alone copies nothing; COPY_TEXT must find the text on the page, select it and copy it."""
    import sys
    import types

    store = {"v": ""}
    fake = types.ModuleType("pyperclip")
    fake.copy = lambda v: store.__setitem__("v", v)
    fake.paste = lambda: store["v"]
    real = sys.modules.get("pyperclip")
    sys.modules["pyperclip"] = fake
    page_html = ("<html><head><title>Mount Everest</title></head><body><table class='infobox'>"
                 "<tr><th>Elevation</th><td>8,848.86&nbsp;m<br>(29,031.7 ft)</td></tr></table>"
                 + "<p>filler</p>" * 300 + "<p id='far'>Height   of the summit</p></body></html>")
    tmp = Path(tempfile.mkdtemp()) / "everest.html"
    tmp.write_text(page_html)
    try:
        browser.navigate(tmp.as_uri())
        assert "8,848.86" in browser.full_text()
        note = browser.copy_text("8,848.86 m")
        assert store["v"] == "8,848.86\xa0m" or store["v"].replace("\xa0", " ") == "8,848.86 m", store["v"]
        assert "Copied" in note
        browser.copy_text("height of the summit")          # whitespace/case-insensitive, off-screen text
        assert store["v"].split() == ["Height", "of", "the", "summit"]
        with pytest.raises(ValueError):
            browser.copy_text("8,849 m")                    # not on the page -> refuse instead of inventing
        page = browser.observe()
        assert any(a["id"] == "copy_text" for a in page["actions"])
    finally:
        if real is not None:
            sys.modules["pyperclip"] = real
        else:
            sys.modules.pop("pyperclip", None)
        browser.navigate(browser.fixture_url)


def test_drag_and_drop_mouse_and_html5(browser):
    from app.agent.loop import AgentLoop
    from tests.fakes import FakeLLM, ScriptedJev

    html = """<html><head><title>Board</title></head><body>
    <div id="card" role="button" aria-label="Card A" draggable="true"
         style="width:120px;height:40px;background:#ddd">Card A</div>
    <div id="done" role="button" aria-label="Done column" style="margin-top:80px;width:200px;height:120px;border:1px solid">Done</div>
    <p id="out"></p>
    <script>
      const card = document.getElementById('card'), done = document.getElementById('done');
      card.addEventListener('dragstart', e => e.dataTransfer.setData('text/plain', 'Card A'));
      done.addEventListener('dragover', e => e.preventDefault());
      done.addEventListener('drop', e => { e.preventDefault(); document.getElementById('out').textContent = 'dropped ' + e.dataTransfer.getData('text/plain'); });
    </script></body></html>"""
    tmp = Path(tempfile.mkdtemp()) / "board.html"
    tmp.write_text(html)
    try:
        browser.navigate(tmp.as_uri())
        page = browser.observe()
        card = find(page, "Card A")
        assert "DRAG" in card.get("extra_ops", [])
        jev = ScriptedJev([("DRAG", "Card A", 0.95), ("DONE", None, 0.95)], noul=[0.9])
        r = AgentLoop(jev=jev, text_llm=FakeLLM("Done column")).run("move Card A to Done", browser)
        assert "dropped Card A" in browser.observe()["text"], r["history"]
        assert r["status"] == "done"
    finally:
        browser.navigate(browser.fixture_url)


def test_vision_shot_and_coordinate_click(browser):
    browser.navigate(browser.fixture_url)
    shot = browser.vision_shot()
    assert shot["width"] > 100 and shot["height"] > 100 and len(shot["b64"]) > 100
    box = browser._page.locator("#q").bounding_box()
    page = browser.observe()
    page["actions"].append({"id": "VISUAL_CLICK", "kind": "visual", "op": "VISUAL_CLICK", "label": "v"})
    page["actions"].append({"id": "TYPE_FOCUSED", "kind": "type_focused", "op": "TYPE_FOCUSED", "label": "t"})
    browser.act(page, "VISUAL_CLICK", text=f"{box['x'] + 5},{box['y'] + 5},left", operation="VISUAL_CLICK")
    browser.act(page, "TYPE_FOCUSED", text="via vision", operation="TYPE_FOCUSED")
    assert browser._page.locator("#q").input_value() == "via vision"


def test_hover_operation(browser):
    html = """<html><head><title>HoverTest</title></head><body>
    <div id="trigger" role="button" aria-label="Hover menu" style="width:100px;height:30px">Hover menu</div>
    <div id="secret" style="display:none">Hidden revealed!</div>
    <script>
      const tr = document.getElementById('trigger');
      tr.addEventListener('mouseenter', () => { document.getElementById('secret').style.display = 'block'; });
    </script></body></html>"""
    tmp = Path(tempfile.mkdtemp()) / "hover.html"
    tmp.write_text(html)
    try:
        browser.navigate(tmp.as_uri())
        page = browser.observe()
        btn = find(page, "Hover menu")
        assert "HOVER" in btn.get("extra_ops", [])
        browser.act(page, btn["id"], operation="HOVER")
        page2 = browser.observe()
        assert "Hidden revealed!" in page2["text"]
    finally:
        browser.navigate(browser.fixture_url)


def test_file_upload_input(browser):
    html = """<html><head><title>UploadTest</title></head><body>
    <input type="file" id="fupload" aria-label="Upload document" />
    <p id="out"></p>
    <script>
      document.getElementById('fupload').addEventListener('change', e => {
        document.getElementById('out').textContent = 'file:' + (e.target.files[0] ? e.target.files[0].name : '');
      });
    </script></body></html>"""
    tmp_dir = Path(tempfile.mkdtemp())
    (tmp_dir / "upload.html").write_text(html)
    sample_file = tmp_dir / "test_doc.txt"
    sample_file.write_text("hello upload")
    try:
        browser.navigate((tmp_dir / "upload.html").as_uri())
        page = browser.observe()
        upl = find(page, "Upload document")
        assert "UPLOAD_FILE" in upl.get("extra_ops", [])
        browser.act(page, upl["id"], text=str(sample_file), operation="UPLOAD_FILE")
        page2 = browser.observe()
        assert "file:test_doc.txt" in page2["text"]
    finally:
        browser.navigate(browser.fixture_url)


def test_dismiss_cookie_overlay(browser):
    html = """<html><head><title>CookieTest</title></head><body>
    <div id="banner" style="position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5)">
      <button id="onetrust-accept-btn-handler">Accept all cookies</button>
    </div>
    <script>
      document.getElementById('onetrust-accept-btn-handler').onclick = () => {
        document.getElementById('banner').remove();
      };
    </script></body></html>"""
    tmp = Path(tempfile.mkdtemp()) / "cookie.html"
    tmp.write_text(html)
    try:
        browser.navigate(tmp.as_uri())
        assert browser._page.locator("#banner").count() == 0
    finally:
        browser.navigate(browser.fixture_url)


def test_report_answer_operation(browser):
    from app.agent.loop import AgentLoop
    from tests.fakes import FakeLLM, ScriptedJev

    tmp = Path(tempfile.mkdtemp()) / "flight.html"
    tmp.write_text("<html><head><title>Flights</title></head><body><h1>Flights</h1><p>IndiGo 6E-101: $45</p></body></html>")
    try:
        browser.navigate(tmp.as_uri())
        page = browser.observe()
        assert any(a["id"] == "report_answer" for a in page["actions"])

        jev = ScriptedJev([("REPORT_ANSWER", "report_answer", 0.95), ("DONE", None, 0.95)], noul=[0.95])
        llm = FakeLLM('Found flight: IndiGo 6E-101 for $45')
        r = AgentLoop(jev=jev, text_llm=llm).run("find flight and report it", browser)
        assert r["status"] == "done"
        assert "IndiGo 6E-101" in (r.get("final_report") or "")
    finally:
        browser.navigate(browser.fixture_url)

