"""Browser capture via Playwright — DOM snapshot (all frames, open shadow roots) + real input events."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from ..config import get_settings
from .clipboard import read_clipboard

logger = logging.getLogger(__name__)

STEALTH_INIT_JS = """
try {
  // 1. Mask navigator.webdriver
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

  // 2. Mock chrome runtime object
  window.chrome = {
    app: { isInstalled: false },
    webstore: { onInstallStageChanged: {}, onDownloadProgress: {} },
    runtime: { PlatformOs: { MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' } },
    loadTimes: function() {},
    csi: function() {}
  };

  // 3. Mock plugins and mimeTypes
  const makePlugin = (name, filename, desc) => ({
    name, filename, description: desc, length: 1,
    0: { type: 'application/pdf', suffixes: 'pdf', description: desc, enabledPlugin: null }
  });
  const plugins = [
    makePlugin('Chrome PDF Plugin', 'internal-pdf-viewer', 'Portable Document Format'),
    makePlugin('Chrome PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', ''),
    makePlugin('Native Client', 'internal-nacl-plugin', '')
  ];
  Object.defineProperty(navigator, 'plugins', { get: () => plugins });
  Object.defineProperty(navigator, 'mimeTypes', { get: () => [
    { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: plugins[0] }
  ]});

  // 4. Mock notification permissions
  const origQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (origQuery) {
    window.navigator.permissions.query = (params) => (
      params && params.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission }) :
        origQuery(params)
    );
  }

  // 5. Mock language
  Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
} catch (e) {}
"""

SNAPSHOT_JS_PATH = Path(__file__).parent / "snapshot.js"
SNAPSHOT_JS = SNAPSHOT_JS_PATH.read_text(encoding="utf-8")

GET_NODE_JS = "id => (window.__jevFast && window.__jevFast.nodes.get(id)) || null"

# Keys Jev may press in the browser (label shown to Jev, Playwright key name)
BROWSER_KEYS: list[tuple[str, str]] = [
    ("Enter (submit / confirm / pick highlighted suggestion)", "Enter"),
    ("Escape (close popup / menu / dialog)", "Escape"),
    ("Tab (next field)", "Tab"),
    ("Shift+Tab (previous field)", "Shift+Tab"),
    ("Arrow Down (next suggestion / option)", "ArrowDown"),
    ("Arrow Up (previous suggestion / option)", "ArrowUp"),
    ("Backspace", "Backspace"),
    ("Page Down", "PageDown"),
    ("Ctrl+A (select all)", "Control+A"),
    ("Ctrl+C (copy)", "Control+C"),
    ("Ctrl+V (paste)", "Control+V"),
    ("Ctrl+Enter (send / submit in editors)", "Control+Enter"),
]

MAX_ELEMENT_ACTIONS = 250
MAX_FRAMES = 6


class StalePage(ValueError):
    """Raised when an observed element is gone before the action was executed."""


def fingerprint(state: dict) -> str:
    """Stable hash of what the agent can perceive (used for page_changed + loop detection)."""
    semantic = [
        (a.get("kind"), a.get("label"), a.get("value"), a.get("checked"), a.get("expanded"), a.get("selected"))
        for a in state.get("actions", [])
    ]
    content = {"url": state.get("url"), "title": state.get("title"), "text": state.get("text"),
               "actions": semantic, "scroll": state.get("scroll", {}).get("y"), "clipboard": state.get("clipboard")}
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _clean_stale_profile_lock(user_data_dir: Path) -> None:
    """If a previous browser run crashed or was killed, clean up stale lockfiles and orphaned processes."""
    try:
        lock_files = [user_data_dir / f for f in ("lockfile", "SingletonLock", "SingletonSocket", "SingletonCookie")]
        if any(f.exists() for f in lock_files):
            import subprocess
            dir_filter = str(user_data_dir).replace("'", "''")
            cmd = (
                f'Get-CimInstance Win32_Process -Filter "Name = \'chrome.exe\'" | '
                f'Where-Object {{ $_.CommandLine -like "*{dir_filter}*" }} | '
                f'ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}'
            )
            try:
                subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, timeout=5)
            except Exception:
                pass
            time.sleep(0.3)
            for f in lock_files:
                try:
                    f.unlink(missing_ok=True)
                except Exception:
                    pass
    except Exception as exc:
        logger.debug("Failed cleaning stale profile lock: %s", exc)


class BrowserCapture:
    """Manages a Playwright browser session: observe() → structured state, act() → real input."""

    mode = "browser"

    def __init__(self) -> None:
        self._settings = get_settings()
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._frames: dict[int, Any] = {}   # frame index (from last observe) -> Frame
        self._new_pages: list[Any] = []     # tabs/popups opened since the last observe
        self._opened_pages: list[Any] = []  # tabs opened during this task (closable at the end)
        self._last_download: str | None = None

    # ------------------------------------------------------------ lifecycle
    @property
    def is_alive(self) -> bool:
        try:
            if self._context is None:
                return False
            if self._page is not None and not self._page.is_closed():
                return True
            pages = [p for p in self._context.pages if not p.is_closed()]
            if pages:
                self._page = pages[-1]
                return True
            return False
        except Exception:
            return False

    def _apply_stealth(self, context: Any) -> None:
        if context is None:
            return
        try:
            context.add_init_script(STEALTH_INIT_JS)
        except Exception as exc:
            logger.debug("Failed to inject stealth script: %s", exc)

    def _attach_page_listeners(self, page: Any) -> None:
        if page is None:
            return
        try:
            page.on("dialog", lambda dialog: dialog.accept())
        except Exception:
            pass
        try:
            page.on("download", self._on_download)
        except Exception:
            pass

    def _on_download(self, download: Any) -> None:
        try:
            filename = download.suggested_filename
            from .files import _known
            d_dir = Path(_known().get("Downloads", Path.home() / "Downloads"))
            d_dir.mkdir(parents=True, exist_ok=True)
            target = d_dir / filename
            if target.exists():
                stem, ext = target.stem, target.suffix
                target = d_dir / f"{stem}_{int(time.time())}{ext}"
            download.save_as(str(target))
            self._last_download = str(target)
            logger.info("Saved downloaded file to %s", target)
        except Exception as exc:
            logger.warning("Could not save download: %s", exc)

    def dismiss_overlays_and_cookies(self) -> bool:
        """Dismiss common cookie banners, GDPR consent popups, and modal backdrops."""
        if self._page is None or self._page.is_closed():
            return False
        try:
            return bool(self._page.evaluate("""() => {
              const selectors = [
                '#onetrust-accept-btn-handler',
                '#accept-cookie-notification',
                '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
                '#didomi-notice-agree-button',
                '[data-testid="cookie-policy-manage-dialog-accept-button"]',
                'button[id*="cookie" i][id*="accept" i]',
                'button[class*="cookie" i][class*="accept" i]',
                'button[aria-label*="accept all" i]',
                'button[aria-label*="agree" i]',
                'button[aria-label*="allow all" i]'
              ];
              for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el && el.offsetParent !== null) {
                  try { el.click(); return true; } catch (e) {}
                }
              }
              const buttons = Array.from(document.querySelectorAll('button, [role="button"], a.btn'));
              for (const b of buttons) {
                if (b.offsetParent === null) continue;
                const txt = (b.innerText || b.textContent || '').trim().toLowerCase();
                if (/^(accept all( cookies)?|i accept|accept|i agree|agree|allow all( cookies)?|got it|consent|accept & continue)$/i.test(txt)) {
                  try { b.click(); return true; } catch (e) {}
                }
              }
              return false;
            }"""))
        except Exception:
            return False

    def auto_solve_turnstile(self) -> bool:
        """Attempt to autonomously click Cloudflare Turnstile verification checkboxes."""
        if self._page is None or self._page.is_closed():
            return False
        try:
            for frame in self._page.frames:
                url = frame.url or ""
                if "challenges.cloudflare.com" in url or "turnstile" in url:
                    box = frame.locator('input[type="checkbox"], .ctp-checkbox-label, #challenge-stage')
                    if box.count() > 0:
                        box.first.click(timeout=2000)
                        time.sleep(1.5)
                        return True
        except Exception as exc:
            logger.debug("auto_solve_turnstile attempt failed: %s", exc)
        return False

    def start(self, url: str | None = None) -> None:
        """Launch Playwright with a persistent profile (keeps logins), or attach over CDP."""
        if self.is_alive:
            if url:
                self.navigate(url)
            return

        from playwright.sync_api import sync_playwright
        s = self._settings
        self._pw = sync_playwright().start()

        if s.browser_cdp_url:
            try:
                self._browser = self._pw.chromium.connect_over_cdp(s.browser_cdp_url)
                self._context = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
                self._apply_stealth(self._context)
                self._page = self._context.pages[-1] if self._context.pages else self._context.new_page()
                self._watch_new_pages()
                if url:
                    self.navigate(url)
                return
            except Exception as exc:
                logger.warning("Failed to connect via CDP to '%s': %s", s.browser_cdp_url, exc)

        user_data_dir = Path(s.browser_user_data_dir).resolve()
        user_data_dir.mkdir(parents=True, exist_ok=True)
        _clean_stale_profile_lock(user_data_dir)
        args = ["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"]
        viewport = {"width": s.browser_width, "height": s.browser_height}
        browser_type = getattr(self._pw, s.browser_type, self._pw.chromium)
        channel = s.browser_channel or None

        try:
            self._context = browser_type.launch_persistent_context(
                user_data_dir=str(user_data_dir), channel=channel, headless=s.headless,
                slow_mo=s.browser_slow_mo, args=args, viewport=viewport,
            )
        except Exception as exc:
            logger.warning("Persistent launch with channel='%s' failed: %s. Falling back to bundled chromium.", channel, exc)
            try:
                self._context = self._pw.chromium.launch_persistent_context(
                    user_data_dir=str(user_data_dir), headless=s.headless,
                    slow_mo=s.browser_slow_mo, args=args, viewport=viewport,
                )
            except Exception as exc2:
                logger.warning("Persistent launch failed: %s. Falling back to a temporary profile.", exc2)
                self._browser = browser_type.launch(headless=s.headless, slow_mo=s.browser_slow_mo)
                self._context = self._browser.new_context(viewport=viewport)

        self._apply_stealth(self._context)
        self._page = self._context.pages[-1] if self._context.pages else self._context.new_page()
        self._watch_new_pages()
        if url:
            self.navigate(url)

    def _watch_new_pages(self) -> None:
        try:
            self._context.on("page", self._on_new_page)
        except Exception:
            pass
        if self._page:
            self._attach_page_listeners(self._page)

    def _on_new_page(self, p: Any) -> None:
        self._new_pages.append(p)
        self._attach_page_listeners(p)

    def _adopt_new_page(self) -> bool:
        """Switch to the most recent tab opened by the site (target=_blank, window.open ...)."""
        fresh = [p for p in self._new_pages if not p.is_closed()]
        self._new_pages.clear()
        if not fresh:
            return False
        self._page = fresh[-1]
        self._opened_pages.extend(fresh)
        try:
            self._page.bring_to_front()
            self._page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        return True

    def open_new_tab(self, url: str | None = None) -> None:
        if not self.is_alive or self._context is None:
            self.start(url)
            return
        self._page = self._context.new_page()
        self._new_pages.clear()
        if url:
            self.navigate(url)

    def navigate(self, url: str) -> None:
        if self._page is None or not self.is_alive:
            self.start(url)
            return
        url = url.strip()
        if url and "://" not in url and not url.startswith("about:"):
            url = "https://" + url
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            logger.warning("Goto '%s' failed: %s", url, exc)
        self.dismiss_overlays_and_cookies()
        self._wait_settle()

    # -------------------------------------------------------------- observe
    def _visible_frames(self) -> list[Any]:
        """Child frames that are attached, non-blank and visible in the viewport."""
        out = []
        if self._page is None:
            return out
        for frame in self._page.frames:
            if frame == self._page.main_frame or frame.is_detached():
                continue
            if frame.url.startswith("javascript:"):
                continue
            try:
                el = frame.frame_element()
                box = el.bounding_box()
                if not box or box["width"] < 40 or box["height"] < 20:
                    continue
                if box["y"] > self._settings.browser_height or box["y"] + box["height"] < 0:
                    continue
            except Exception:
                continue
            out.append(frame)
            if len(out) >= MAX_FRAMES:
                break
        return out

    def observe(self, screenshot: bool = False) -> dict[str, Any]:
        """Snapshot main frame + visible iframes into one structured state."""
        if self._page is None:
            raise RuntimeError("Browser not started.")
        self._adopt_new_page()
        if self._page.is_closed() and not self.is_alive:
            raise RuntimeError("Browser was closed.")
        self.auto_solve_turnstile()
        self.dismiss_overlays_and_cookies()
        main = None
        for attempt in range(6):
            try:
                main = self._page.evaluate(SNAPSHOT_JS)
                if main:
                    break
            except Exception as exc:
                if attempt == 5:
                    raise StalePage(f"Page did not settle after 6 attempts: {exc}") from exc
            time.sleep(0.15)
        if not main:
            raise StalePage("Empty page snapshot")

        self._frames = {0: self._page.main_frame}
        actions: list[dict] = [dict(a, frame=0) for a in main["actions"]]
        texts = [main["text"]]
        sensitive = dict(main.get("sensitive") or {})
        if sensitive.get("captcha") and self.auto_solve_turnstile():
            sensitive["captcha"] = False

        for fi, frame in enumerate(self._visible_frames(), start=1):
            try:
                snap = frame.evaluate(SNAPSHOT_JS)
            except Exception:
                continue
            if not snap:
                continue
            self._frames[fi] = frame
            for a in snap["actions"]:
                actions.append(dict(a, frame=fi, node=f"f{fi}:{a['node']}", raw_node=a["node"]))
            if snap.get("text"):
                texts.append(f"[frame {fi}: {snap.get('title') or frame.url[:60]}]\n{snap['text'][:2000]}")
            for k, v in (snap.get("sensitive") or {}).items():
                sensitive[k] = sensitive.get(k) or v

        omitted = max(0, len(actions) - MAX_ELEMENT_ACTIONS) + main.get("omitted_actions", 0)
        actions = actions[:MAX_ELEMENT_ACTIONS]
        for i, a in enumerate(actions):
            a["id"] = f"e{i + 1}"
            a.setdefault("raw_node", a["node"])
            if a.get("kind") == "click":
                ops = ["DOUBLE_CLICK", "RIGHT_CLICK", "HOVER", "DRAG"]
                if a.get("is_file") or re.search(r"\b(upload|choose file|browse)\b", str(a.get("label", "")), re.I):
                    ops.append("UPLOAD_FILE")
                a["extra_ops"] = ops

        actions.extend(self._control_actions())

        info: dict[str, Any] = {
            "url": main["url"], "title": main["title"], "w": main["w"], "h": main["h"],
            "text": "\n".join(texts)[:9000], "scroll": main["scroll"], "actions": actions,
            "omitted_actions": omitted, "sensitive": {k: v for k, v in sensitive.items() if v},
            "mode": "browser",
            "clipboard": read_clipboard(),
        }
        info["fingerprint"] = fingerprint(info)

        if screenshot:
            try:
                info["screenshot"] = base64.b64encode(self._page.screenshot(type="jpeg", quality=72)).decode()
            except Exception:
                pass
        return info

    def _control_actions(self) -> list[dict]:
        ctrls: list[dict] = [
            {"id": "scroll_down", "kind": "scroll", "op": "SCROLL_DOWN", "label": "Scroll down", "delta": 560},
            {"id": "scroll_up", "kind": "scroll", "op": "SCROLL_UP", "label": "Scroll up", "delta": -560},
            {"id": "wait", "kind": "wait", "op": "WAIT", "label": "Wait for the page to update"},
            {"id": "go_back", "kind": "nav", "op": "GO_BACK", "label": "Browser back"},
            {"id": "open_url", "kind": "nav", "op": "OPEN_URL", "label": "Open a URL in this tab"},
            {"id": "copy_text", "kind": "copy_text", "op": "COPY_TEXT", "label": "Select and copy text from the page"},
            {"id": "report_answer", "kind": "report", "op": "REPORT_ANSWER",
             "label": "Report the final extracted answer or data from the page to the user"},
        ]
        if self._page is not None and self._page in self._opened_pages:
            ctrls.append({"id": "close_tab", "kind": "close_tab", "op": "CLOSE_TAB",
                          "label": "Close this tab (it was opened during this task)"})
        for i, (label, key) in enumerate(BROWSER_KEYS, start=1):
            ctrls.append({"id": f"key_{i}", "kind": "key", "label": label, "key": key})
        try:
            pages = [p for p in self._context.pages if not p.is_closed()] if self._context else []
        except Exception:
            pages = []
        if len(pages) > 1:
            for i, p in enumerate(pages):
                if p == self._page:
                    continue
                try:
                    title = p.title()
                except Exception:
                    title = ""
                ctrls.append({"id": f"tab_{i}", "kind": "tab", "label": f"Tab: {title[:60]} ({p.url[:80]})", "tab_index": i})
        return ctrls

    # ------------------------------------------------------------------ act
    def _handle(self, action: dict) -> Any:
        frame = self._frames.get(action.get("frame", 0)) or (self._page.main_frame if self._page else None)
        if frame is None:
            return None
        try:
            h = frame.evaluate_handle(GET_NODE_JS, action.get("raw_node", action.get("node")))
            el = h.as_element()
            if el is None:
                return None
            if not el.evaluate("e => e.isConnected"):
                return None
            return el
        except Exception:
            return None

    def _locator_fallback(self, action: dict) -> Any:
        """Find the element again by role + accessible name when the cached node is gone."""
        frame = self._frames.get(action.get("frame", 0)) or self._page.main_frame
        label = str(action.get("label", "")).removeprefix("Open ").split(" → ")[0]
        role = action.get("role")
        try:
            loc = frame.get_by_role(role, name=label) if role and label else frame.get_by_text(label)
            if loc.count() > 0:
                return loc.first
        except Exception:
            pass
        return None

    def _real_click(self, target: Any, button: str = "left", double: bool = False) -> None:
        try:
            target.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        try:
            if double:
                target.dblclick(timeout=4000)
            else:
                target.click(timeout=4000, button=button)
        except Exception as exc:
            logger.debug("Actionable click failed (%s); retrying with force", exc)
            try:
                if double:
                    target.dblclick(timeout=2000, force=True)
                else:
                    target.click(timeout=2000, button=button, force=True)
            except Exception:
                if button == "left" and not double:
                    target.evaluate("e => e.click()")  # last resort
                else:
                    raise

    def act(self, page: dict[str, Any], action_id: str, text: str | None = None,
            operation: str | None = None) -> dict[str, Any]:
        """Execute action_id from the observed page with real Playwright input."""
        if self._page is None:
            raise RuntimeError("Browser not started.")
        action = next((a for a in page.get("actions", []) if a["id"] == action_id), None)
        if action is None:
            raise ValueError(f"Action '{action_id}' not found in page actions")

        kind = action["kind"]
        op = (operation or "").upper()

        if kind == "wait":
            try:
                self._page.wait_for_load_state("networkidle", timeout=2500)
            except Exception:
                time.sleep(0.8)

        elif kind == "scroll":
            delta = action.get("delta", 560)
            try:
                self._page.mouse.move(self._settings.browser_width / 2, self._settings.browser_height / 2)
                self._page.mouse.wheel(0, delta)   # scrolls inner containers too (e.g. Gmail)
            except Exception:
                self._page.evaluate(f"window.scrollBy(0, {int(delta)})")
            time.sleep(0.3)

        elif kind == "key":
            self._page.keyboard.press(action["key"])
            time.sleep(0.25)

        elif kind == "tab":
            pages = [p for p in self._context.pages if not p.is_closed()]
            idx = action.get("tab_index", -1)
            if 0 <= idx < len(pages):
                self._page = pages[idx]
                self._page.bring_to_front()

        elif kind == "copy_text":
            if not text:
                raise ValueError("COPY_TEXT needs the text to copy")
            return {"executed": action_id, "note": self.copy_text(text)}

        elif kind == "close_tab":
            if self._page not in self._opened_pages:
                raise ValueError("Only tabs opened during this task may be closed")
            closing = self._page
            self._opened_pages.remove(closing)
            closing.close()
            pages = [p for p in self._context.pages if not p.is_closed()]
            if pages:
                self._page = pages[-1]
                self._page.bring_to_front()
            return {"executed": action_id, "note": "Closed the tab."}

        elif kind == "nav":
            if action["op"] == "GO_BACK":
                try:
                    self._page.go_back(wait_until="domcontentloaded", timeout=15000)
                except Exception as exc:
                    logger.warning("Back navigation failed: %s", exc)
            elif action["op"] == "OPEN_URL":
                if not text:
                    raise ValueError("OPEN_URL requires a URL")
                self.navigate(text)

        elif kind == "select":
            target = self._handle(action)
            if target is None:
                raise StalePage(f"Select for '{action_id}' is gone")
            val = str(action.get("value", ""))
            try:
                target.select_option(value=val, timeout=4000)
            except Exception:
                target.select_option(label=action.get("label", "").split(" → ")[-1], timeout=2000)

        elif kind == "fill":
            target = self._handle(action) or self._locator_fallback(action)
            if target is None:
                raise StalePage(f"Field for '{action_id}' is gone")
            content = text or ""
            if op == "APPEND_TEXT":
                self._real_click(target)
                self._page.keyboard.press("Control+End" if action.get("multiline") else "End")
                self._page.keyboard.type(content, delay=8)
            else:
                try:
                    target.fill(content, timeout=4000)   # real input events; works for React + contenteditable
                except Exception as exc:
                    logger.debug("fill() failed (%s); typing instead", exc)
                    self._real_click(target)
                    self._page.keyboard.press("Control+A")
                    self._page.keyboard.press("Backspace")
                    self._page.keyboard.type(content, delay=8)
            if action.get("role") in ("combobox", "searchbox") and op != "TYPE_SUBMIT":
                time.sleep(max(self._settings.combobox_wait_ms, 300) / 1000)
            if op == "TYPE_SUBMIT":
                self._page.keyboard.press("Enter")

        elif kind == "click":
            target = self._handle(action) or self._locator_fallback(action)
            if target is None:
                raise StalePage(f"Element for '{action_id}' is no longer present")
            if op == "DRAG":
                return self._drag(page, action, target, text)
            if op == "HOVER":
                target.scroll_into_view_if_needed(timeout=2000)
                target.hover(timeout=3000)
                time.sleep(0.3)
                return {"executed": action_id, "note": f"Hovered over '{action.get('label')}'."}
            if op == "UPLOAD_FILE":
                if not text:
                    raise ValueError("UPLOAD_FILE needs a file path or file name")
                from .files import find_files, folder_hint
                spec = text.partition("|")[0].strip()
                hits = find_files(spec, hint_dir=folder_hint(spec), limit=1)
                chosen_path = str(hits[0]) if hits else spec
                if not Path(chosen_path).exists():
                    raise ValueError(f"File not found: '{chosen_path}'")
                try:
                    target.set_input_files(chosen_path, timeout=3000)
                except Exception as exc:
                    logger.debug("set_input_files failed (%s); trying expect_file_chooser", exc)
                    try:
                        with self._page.expect_file_chooser(timeout=3000) as fc_info:
                            self._real_click(target)
                        fc_info.value.set_files(chosen_path)
                    except Exception as exc2:
                        raise RuntimeError(f"Could not upload file: {exc2}") from exc2
                self._wait_settle()
                return {"executed": action_id, "note": f"Uploaded file '{Path(chosen_path).name}'."}
            if op == "DOUBLE_CLICK":
                self._real_click(target, double=True)
            elif op == "RIGHT_CLICK":
                self._real_click(target, button="right")
            else:
                self._real_click(target)

        elif kind == "visual":
            parts = [p.strip() for p in (text or "").split(",")]
            if len(parts) < 2:
                raise ValueError("VISUAL_CLICK needs page coordinates")
            self.click_at(int(float(parts[0])), int(float(parts[1])), parts[2] if len(parts) > 2 else "left")

        elif kind == "type_focused":
            if not text:
                raise ValueError("TYPE_FOCUSED needs text")
            self._page.keyboard.type(text, delay=8)

        else:
            raise ValueError(f"Unsupported action kind '{kind}'")

        self._wait_settle()

        # Follow a tab the action opened (target=_blank, window.open, ...); popups open asynchronously
        if kind in ("click", "key") and not self._new_pages:
            deadline = time.time() + 0.6
            try:
                while time.time() < deadline and not self._new_pages:
                    self._page.wait_for_timeout(100)   # lets Playwright dispatch the 'page' event
            except Exception:
                pass
        new_tab = self._adopt_new_page()
        res: dict[str, Any] = {"executed": action_id, "new_tab": new_tab}
        if getattr(self, "_last_download", None):
            res["note"] = f"Downloaded file saved to: {self._last_download}"
            self._last_download = None
        return res

    def _wait_settle(self, ms: int | None = None) -> None:
        if self._page is None:
            return
        try:
            self._page.wait_for_load_state("domcontentloaded", timeout=4000)
        except Exception:
            pass
        time.sleep((ms or self._settings.dom_settle_ms) / 1000)

    def _drag(self, page: dict[str, Any], action: dict[str, Any], src: Any, dest_label: str | None) -> dict[str, Any]:
        """Real mouse drag from the source element to the element whose label the text LLM chose."""
        if not dest_label:
            raise ValueError("DRAG needs a destination")
        want = dest_label.strip().lower()
        pool = [a for a in page.get("actions", []) if a.get("id") != action.get("id") and a.get("node") is not None]
        cands = [a for a in pool if str(a.get("label", "")).strip().lower() == want] or \
                [a for a in pool if want in str(a.get("label", "")).lower()]
        dest = (self._handle(cands[0]) or self._locator_fallback(cands[0])) if cands else None
        if dest is None:
            raise ValueError(f"Cannot drag: destination '{dest_label}' not found")
        try:
            src.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        b1, b2 = src.bounding_box(), dest.bounding_box()
        if not b1 or not b2:
            raise ValueError("Cannot drag: element not visible")
        x1, y1 = b1["x"] + b1["width"] / 2, b1["y"] + b1["height"] / 2
        x2, y2 = b2["x"] + b2["width"] / 2, b2["y"] + b2["height"] / 2
        m = self._page.mouse
        m.move(x1, y1)
        m.down()
        m.move(x1 + 5, y1 + 5, steps=3)          # cross the drag threshold
        m.move(x2, y2, steps=15)
        m.up()
        self._wait_settle()
        return {"executed": action["id"], "note": f"Dragged '{action.get('label')}' onto '{cands[0].get('label')}'."}

    # ------------------------------------------------------------- reading
    def full_text(self, limit: int = 20000) -> str:
        """Whole page text (not just the viewport) — for picking values to copy."""
        try:
            return self._page.evaluate("() => document.body ? document.body.innerText : ''")[:limit]
        except Exception:
            return ""

    def copy_text(self, wanted: str) -> str:
        """Select the text on the page like a user (scrolls to it, highlights it) and copy it to the OS clipboard."""
        found = self._page.evaluate("""(wanted) => {
          const norm = s => s.replace(/\\s+/g, ' ').trim().toLowerCase();
          const target = norm(wanted);
          const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
          const nodes = []; let n, flat = '';
          while ((n = walker.nextNode())) {
            if (!n.parentElement || n.parentElement.closest('script,style,noscript')) continue;
            nodes.push([n, flat.length]); flat += n.textContent + ' ';
          }
          // map positions in a whitespace-normalised copy back to text nodes
          const map = []; let normFlat = '', prevSpace = false;
          for (let i = 0; i < flat.length; i++) {
            const c = flat[i], sp = /\\s/.test(c);
            if (sp && (prevSpace || normFlat.length === 0)) continue;
            normFlat += sp ? ' ' : c.toLowerCase(); map.push(i); prevSpace = sp;
          }
          const at = normFlat.indexOf(target);
          if (at < 0) return null;
          const startRaw = map[at], endRaw = map[at + target.length - 1] + 1;
          const locate = pos => { for (let i = nodes.length - 1; i >= 0; i--) if (nodes[i][1] <= pos)
            return [nodes[i][0], Math.min(pos - nodes[i][1], nodes[i][0].textContent.length)]; };
          const [sn, so] = locate(startRaw), [en, eo] = locate(endRaw);
          const r = document.createRange(); r.setStart(sn, so); r.setEnd(en, eo);
          (sn.parentElement || document.body).scrollIntoView({block: 'center'});
          const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
          return sel.toString();
        }""", wanted)
        if not found:
            raise ValueError(f"'{wanted[:80]}' is not on this page — copy text exactly as it appears")
        self._page.keyboard.press("Control+C")
        time.sleep(0.3)
        copied = read_clipboard(limit=5000)
        if not copied or copied.split() != found.split():
            try:  # some pages block copy events; put the selected text on the clipboard directly
                import pyperclip
                pyperclip.copy(found.replace("\xa0", " ").strip())
            except Exception as exc:
                raise ValueError(f"Selected the text but could not copy it: {exc}") from exc
        return f"Copied '{found.strip()[:120]}' to the clipboard."

    # ---------------------------------------------------------- finishing
    def begin_run(self) -> None:
        """A new task starts: tabs opened by earlier tasks are not closed by this task's finishing-up."""
        self._earlier_pages = {id(p) for p in self._opened_pages}

    def opened_windows(self, this_run: bool = True) -> list[str]:
        out = []
        earlier = getattr(self, "_earlier_pages", set()) if this_run else set()
        for p in list(self._opened_pages):
            if p.is_closed():
                self._opened_pages.remove(p)
                continue
            if id(p) in earlier:
                continue
            try:
                out.append(p.title() or p.url)
            except Exception:
                pass
        return out

    def pending_issues(self, page: dict[str, Any] | None = None) -> list[str]:
        """Unfinished business in the page: a field still focused with an open suggestion list, an open dialog."""
        if self._page is None:
            return []
        try:
            return self._page.evaluate("""() => {
              const out = [];
              const d = [...document.querySelectorAll('dialog[open],[role=dialog],[role=alertdialog]')]
                .find(e => e.offsetParent !== null && /unsaved|discard|leave (this )?page|save changes/i.test(e.innerText || ''));
              if (d) out.push('a dialog about unsaved changes is open — answer it');
              return out;
            }""")
        except Exception:
            return []

    # ---------------------------------------------------------------- misc
    def vision_shot(self, max_width: int = 1280) -> dict[str, Any]:
        """Viewport screenshot in CSS pixels (what page.mouse uses) for the vision fallback."""
        if self._page is None:
            raise RuntimeError("Browser not started.")
        data = self._page.screenshot(type="jpeg", quality=80, scale="css")
        vp = self._page.viewport_size or {"width": self._settings.browser_width,
                                          "height": self._settings.browser_height}
        return {"b64": base64.b64encode(data).decode(), "width": vp["width"], "height": vp["height"],
                "left": 0, "top": 0, "scale": 1.0}

    def click_at(self, x: int, y: int, button: str = "left") -> None:
        m = self._page.mouse
        m.move(x, y, steps=4)
        if button == "double":
            m.dblclick(x, y)
        else:
            m.click(x, y, button="right" if button == "right" else "left")

    def screenshot(self, path: str) -> None:
        if self._page:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._page.screenshot(path=path)

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception as exc:
            logger.warning("Error closing browser: %s", exc)
        finally:
            self._browser = None
            self._page = None
            self._context = None
            self._pw = None

    def __enter__(self) -> "BrowserCapture":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
