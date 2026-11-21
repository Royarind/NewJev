"""
Notice when the PERSON uses the mouse or keyboard during a run, so the agent never types into the wrong window.

Windows low-level hooks see every input event and flag the ones that were injected by software (the agent's own
clicks and key presses), so only real human input counts. Input while the NewJev window itself is in front
(steering, Stop, answering a card) does not count. Other platforms: a no-op watcher.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# the agent's own UI / quick box — NOT user files that happen to be called "NewJev_..."
EXEMPT_TITLES = ("newjev agent", "newjev quick", "newjev connection")


class UserTookOver(Exception):
    """Raised inside long actions (typing) when the person starts using the mouse/keyboard."""


class InputWatcher:
    def __init__(self, include_moves: bool = False, move_px: int = 120) -> None:
        self.include_moves = include_moves
        self.move_px = move_px
        self.last_human = 0.0
        self.last_what = ""
        self._suppress_until = 0.0
        self._thread: threading.Thread | None = None
        self._ok = False
        self._move_origin: tuple[int, int] | None = None
        self.events_total = 0          # every event the hooks saw (also the agent's own): proves the hooks work
        self.mode = "off"              # hooks | polling | off
        self._poller: threading.Thread | None = None

    # ---------------------------------------------------------------- api
    @property
    def available(self) -> bool:
        return self._ok

    def suppress(self, seconds: float) -> None:
        """Ignore input for a moment (e.g. the hotkey that opened the quick box)."""
        self._suppress_until = time.time() + seconds

    def mark(self) -> float:
        """A reference time; `since(mark)` tells whether the person did something after it."""
        return time.time()

    def since(self, t: float) -> bool:
        return self.last_human > t

    def idle_for(self) -> float:
        return time.time() - self.last_human if self.last_human else 1e9

    def record(self, what: str) -> None:   # also used by tests
        if time.time() < self._suppress_until:
            return
        self.last_human, self.last_what = time.time(), what
        logger.info("You took over: %s", what)

    def status(self) -> dict[str, Any]:
        return {"mode": self.mode, "events_seen": self.events_total,
                "last_human_s_ago": round(self.idle_for(), 1) if self.last_human else None}

    # --------------------------------------------- polling fallback (no hooks)
    def start_polling(self) -> None:
        """GetLastInputInfo changes on ANY input; the agent's own input is excluded by its busy windows."""
        if sys.platform != "win32" or self._poller is not None:
            return
        self.mode = "polling"
        self._poller = threading.Thread(target=self._poll, name="input-poller", daemon=True)
        self._poller.start()
        logger.warning("Take-over detection uses polling (hooks unavailable)")

    def _poll(self) -> None:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        last = None
        while True:
            time.sleep(0.15)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                continue
            if last is None:
                last = info.dwTime
                continue
            if info.dwTime != last:
                last = info.dwTime
                if agent_is_busy() or self._foreground_is_ours():
                    continue
                self.record("mouse/keyboard")

    # ------------------------------------------------------------ windows
    def start(self) -> None:
        if sys.platform != "win32" or self._thread is not None:
            return
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), name="input-watcher", daemon=True)
        self._thread.start()
        ready.wait(2.0)
        if self._ok:
            self.mode = "hooks"
            logger.info("Take-over detection active (input hooks installed)")
        else:
            self.start_polling()

    def hooks_look_dead(self) -> None:
        """Called after the agent injected input: if the hooks never saw a single event, fall back to polling."""
        if self.mode == "hooks" and self.events_total == 0:
            logger.warning("Input hooks saw no events — switching take-over detection to polling")
            self.start_polling()

    def _foreground_is_ours(self) -> bool:  # pragma: no cover - Windows only
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        title = buf.value.lower()
        return any(t in title for t in EXEMPT_TITLES)

    def _run(self, ready: threading.Event) -> None:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
        LLKHF_INJECTED, LLMHF_INJECTED = 0x10, 0x01
        WM_MOUSEMOVE = 0x0200
        KEYDOWNS = {0x0100, 0x0104}                     # WM_KEYDOWN, WM_SYSKEYDOWN
        CLICKS = {0x0201, 0x0204, 0x0207, 0x020A, 0x020E, 0x020B}   # buttons down + wheels

        class KBD(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class MSL(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = LRESULT
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p     # 64-bit handles must not be truncated
        kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p

        def on_key(code, wparam, lparam):
            try:
                self.events_total += 1
                if code >= 0 and wparam in KEYDOWNS:
                    k = ctypes.cast(lparam, ctypes.POINTER(KBD)).contents
                    if not (k.flags & LLKHF_INJECTED) and not self._foreground_is_ours():
                        self.record("keyboard")
            except Exception:
                pass
            return user32.CallNextHookEx(None, code, wparam, lparam)

        def on_mouse(code, wparam, lparam):
            try:
                if wparam != WM_MOUSEMOVE:
                    self.events_total += 1
                if code >= 0:
                    m = ctypes.cast(lparam, ctypes.POINTER(MSL)).contents
                    if not (m.flags & LLMHF_INJECTED):
                        if wparam in CLICKS and not self._foreground_is_ours():
                            self.record("mouse click")
                        elif wparam == WM_MOUSEMOVE and self.include_moves:
                            o = self._move_origin
                            if o is None or time.time() - self.last_human > 2:
                                self._move_origin = (m.pt.x, m.pt.y)
                            elif abs(m.pt.x - o[0]) + abs(m.pt.y - o[1]) > self.move_px:
                                self._move_origin = None
                                if not self._foreground_is_ours():
                                    self.record("mouse movement")
            except Exception:
                pass
            return user32.CallNextHookEx(None, code, wparam, lparam)

        self._kb_proc, self._ms_proc = HOOKPROC(on_key), HOOKPROC(on_mouse)   # keep references alive
        hmod = kernel32.GetModuleHandleW(None)
        kb = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, hmod, 0) or \
            user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, None, 0)
        ms = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_proc, hmod, 0) or \
            user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_proc, None, 0)
        self._ok = bool(kb and ms)
        if not self._ok:
            logger.warning("Input hooks could not be installed (error %s)", ctypes.GetLastError())
        ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


_watcher: InputWatcher | None = None
_lock = threading.Lock()


def get_watcher(start: bool = True) -> InputWatcher | None:
    """Process-wide watcher (created on first use when TAKEOVER_PAUSE is on)."""
    global _watcher
    with _lock:
        if _watcher is None:
            try:
                from ..config import get_settings
                s = get_settings()
                if not s.takeover_pause:
                    return None
                _watcher = InputWatcher(include_moves=s.takeover_include_moves)
            except Exception:
                _watcher = InputWatcher()
            if start:
                _watcher.start()
        return _watcher


def set_watcher(w: Any) -> None:   # tests
    global _watcher
    _watcher = w


# typing checks this between chunks: the mark of the current action
_action_mark = threading.local()
_busy = {"depth": 0, "until": 0.0}
_busy_lock = threading.Lock()


def agent_is_busy() -> bool:
    """True while (and shortly after) the agent itself sends input — used by the polling fallback."""
    with _busy_lock:
        return _busy["depth"] > 0 or time.time() < _busy["until"]


class agent_input:
    """with agent_input(): ... — marks a stretch in which the agent clicks / types itself."""

    def __enter__(self):
        with _busy_lock:
            _busy["depth"] += 1
        return self

    def __exit__(self, *_):
        with _busy_lock:
            _busy["depth"] = max(0, _busy["depth"] - 1)
            _busy["until"] = time.time() + 0.8      # input events arrive a little after they are sent
        return False


def begin_action() -> None:
    _action_mark.t = time.time()


def check_interrupt() -> None:
    """Raise UserTookOver if the person used the mouse/keyboard since the current action began."""
    w = _watcher
    t = getattr(_action_mark, "t", None)
    if w is not None and t is not None and w.since(t):
        raise UserTookOver(w.last_what or "input")
