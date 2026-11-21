"""
Global hotkey (default Ctrl+Alt+J): opens a small NewJev command box from anywhere in Windows.
The box is the /quick page in a compact Edge / Chrome app window (type or speak a goal; Enter runs it).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import webbrowser

logger = logging.getLogger(__name__)

MODS = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312


def parse_combo(combo: str) -> tuple[int, int]:
    parts = [p.strip().lower() for p in (combo or "").split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    mods, key = 0, parts[-1]
    for p in parts[:-1]:
        if p not in MODS:
            raise ValueError(f"unknown modifier {p!r}")
        mods |= MODS[p]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    elif key == "space":
        vk = 0x20
    else:
        raise ValueError(f"unsupported key {key!r}")
    if not mods:
        raise ValueError("a global hotkey needs Ctrl / Alt / Shift / Win")
    return mods, vk


def _browser() -> str | None:
    for exe in ("msedge", "chrome"):
        p = shutil.which(exe)
        if p:
            return p
    for p in (r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
              r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
              r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
              r"%LocalAppData%\Google\Chrome\Application\chrome.exe"):
        p = os.path.expandvars(p)
        if os.path.exists(p):
            return p
    return None


def open_quick_box(base_url: str) -> None:
    try:
        from ..agent.takeover import get_watcher
        w = get_watcher(start=False)
        if w is not None:
            w.suppress(3.0)        # the hotkey itself is not "taking over" a run
    except Exception:
        pass
    url = base_url.rstrip("/") + "/quick"
    exe = _browser()
    if exe:
        subprocess.Popen([exe, f"--app={url}", "--window-size=600,190", "--window-position=520,260"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(url)


def start_hotkey(base_url: str, combo: str = "ctrl+alt+j") -> bool:
    """Register the hotkey in a background thread (Windows only). Returns False if it can't be registered."""
    if sys.platform != "win32":
        return False
    try:
        mods, vk = parse_combo(combo)
    except ValueError as exc:
        logger.warning("Hotkey %r not usable: %s", combo, exc)
        return False
    ok = threading.Event()
    failed = threading.Event()

    def loop() -> None:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk):
            logger.warning("Could not register the hotkey %s (another app uses it?)", combo)
            failed.set()
            return
        ok.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                try:
                    open_quick_box(base_url)
                except Exception as exc:
                    logger.warning("quick box failed: %s", exc)

    threading.Thread(target=loop, name="hotkey", daemon=True).start()
    ok.wait(1.5)
    if ok.is_set():
        logger.info("Global hotkey %s opens the NewJev command box", combo)
    return ok.is_set()
