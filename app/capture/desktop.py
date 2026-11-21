"""Desktop capture for Windows native apps — UI Automation tree → structured state, real mouse/keyboard input."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Any

from .clipboard import read_clipboard

logger = logging.getLogger(__name__)

# --- DPI awareness: without this, UIA coordinates and pyautogui clicks disagree at 125%/150% scaling.
try:  # pragma: no cover - Windows only
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

# canonical name -> (process exe names, window-title regex)
APP_ALIASES: dict[str, tuple[tuple[str, ...], str]] = {
    "notepad": (("notepad.exe",), r"notepad"),
    "calculator": (("calculatorapp.exe", "calc.exe"), r"^calculator"),
    "word": (("winword.exe",), r"\bword\b"),
    "excel": (("excel.exe",), r"\bexcel\b"),
    "powerpoint": (("powerpnt.exe",), r"powerpoint"),
    "outlook": (("outlook.exe", "olk.exe"), r"outlook"),
    "explorer": (("explorer.exe",), r"file explorer|this pc|downloads|documents|desktop|home|pictures"),
    "chrome": (("chrome.exe",), r"google chrome"),
    "edge": (("msedge.exe",), r"microsoft.*edge"),
    "paint": (("mspaint.exe",), r"\bpaint\b"),
    "terminal": (("windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe"), r"terminal|command prompt|powershell"),
    "vscode": (("code.exe",), r"visual studio code"),
    "settings": (("systemsettings.exe",), r"^settings$"),
    "teams": (("ms-teams.exe", "teams.exe"), r"microsoft teams"),
}
SYNONYMS = {
    "calc": "calculator", "file explorer": "explorer", "files": "explorer", "folder": "explorer",
    "downloads": "explorer", "documents": "explorer", "this pc": "explorer", "winword": "word",
    "microsoft word": "word", "word document": "word", "doc": "word", "docx": "word",
    "microsoft excel": "excel", "spreadsheet": "excel", "xlsx": "excel", "ppt": "powerpoint",
    "powerpnt": "powerpoint", "mspaint": "paint", "google chrome": "chrome", "browser": "chrome",
    "microsoft edge": "edge", "msedge": "edge", "cmd": "terminal", "command prompt": "terminal",
    "powershell": "terminal", "vs code": "vscode", "visual studio code": "vscode", "code": "vscode",
    "text editor": "notepad", "txt": "notepad",
}
EXCLUDED_WINDOW_NAMES = {"", "Taskbar", "Program Manager", "Start", "Search", "Desktop 1", "Task View",
                         "Notification Center", "Action center", "System tray overflow window."}
# The agent's own windows (web UI tab, quick box). Only these exact titles: files named "NewJev_..." are user files.
_OWN_UI = ("newjev agent", "newjev quick", "newjev connection")


def is_own_ui(title: str | None) -> bool:
    t = (title or "").lower()
    return any(o in t for o in _OWN_UI)


EXCLUDED_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW", "Windows.UI.Core.CoreWindow"}
# Context menus / flyouts are separate top-level windows, not children of the app window
POPUP_CLASSES = {"#32768", "Xaml_WindowedPopupClass", "Microsoft.UI.Content.PopupWindowSiteBridge", "PopupHost",
                 "DropDown", "Net UI Tool Window", "NUIDialog", "XamlExplorerHostIslandWindow"}
KNOWN_FOLDERS = ("downloads", "documents", "desktop", "pictures", "music", "videos")

# (label shown to Jev, keys for pyautogui.hotkey)
DESKTOP_KEYS: list[tuple[str, tuple[str, ...]]] = [
    ("Enter (confirm / submit / calculate)", ("enter",)),
    ("Escape (cancel / close menu)", ("esc",)),
    ("Tab (next control)", ("tab",)),
    ("Shift+Tab (previous control)", ("shift", "tab")),
    ("Arrow Up", ("up",)), ("Arrow Down", ("down",)), ("Arrow Left", ("left",)), ("Arrow Right", ("right",)),
    ("Backspace", ("backspace",)), ("Delete", ("delete",)),
    ("F2 (rename selected item)", ("f2",)), ("F5 (refresh)", ("f5",)),
    ("Shift+F10 (full classic context menu for the selected item)", ("shift", "f10")),
    ("Home", ("home",)), ("End", ("end",)), ("Ctrl+End (go to end of document)", ("ctrl", "end")),
    ("Ctrl+A (select all)", ("ctrl", "a")), ("Ctrl+C (copy)", ("ctrl", "c")),
    ("Ctrl+V (paste)", ("ctrl", "v")), ("Ctrl+X (cut)", ("ctrl", "x")), ("Ctrl+Z (undo)", ("ctrl", "z")),
    ("Ctrl+S (save)", ("ctrl", "s")), ("F12 (Save As dialog in Office apps)", ("f12",)),
    ("Ctrl+B (bold selection)", ("ctrl", "b")), ("Ctrl+I (italic selection)", ("ctrl", "i")),
    ("Ctrl+U (underline selection)", ("ctrl", "u")),
    ("Shift+Home (select to line start)", ("shift", "home")), ("Shift+End (select to line end)", ("shift", "end")),
    ("Ctrl+Home (go to start of document)", ("ctrl", "home")), ("Ctrl+N (new document / window)", ("ctrl", "n")),
    ("Ctrl+Shift+N (new folder in File Explorer)", ("ctrl", "shift", "n")),
    ("Ctrl+F (find)", ("ctrl", "f")), ("Alt (show menu bar / ribbon keys)", ("alt",)),
]

WALK_TIME_BUDGET_S = 3.0
MAX_ELEMENT_ACTIONS = 300


_KNOWN_FOLDER_IDS = {
    "Desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "Documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "Downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "Pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "Music": "4BD8D571-6D19-48D3-BE97-422220080E43",
    "Videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
}
_known_cache: dict[str, str] | None = None


def known_folders() -> dict[str, str]:
    """Real paths of Desktop/Documents/Downloads… (handles OneDrive-redirected folders on Windows)."""
    global _known_cache
    if _known_cache is not None:
        return _known_cache
    out: dict[str, str] = {}
    for name, guid in _KNOWN_FOLDER_IDS.items():
        path = None
        try:  # pragma: no cover - Windows only
            import ctypes
            import uuid
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            u = uuid.UUID(guid)
            g = GUID(u.time_low, u.time_mid, u.time_hi_version)
            for i, b in enumerate(u.bytes[8:]):
                g.Data4[i] = b
            p = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) == 0:
                path = p.value
                ctypes.windll.ole32.CoTaskMemFree(p)
        except Exception:
            path = None
        out[name] = path or str(Path.home() / name)
    _known_cache = out
    return out


def expand_known(path: str) -> str:
    """'~/Desktop/x.txt' -> real Desktop path (OneDrive-aware); otherwise expanduser."""
    p = path.replace("\\", "/")
    if p.startswith("~/"):
        head, _, rest = p[2:].partition("/")
        for name, real in known_folders().items():
            if head.lower() == name.lower():
                return str(Path(real) / rest) if rest else real
    return os.path.expanduser(path)


def canonical_app(name: str) -> str:
    n = (name or "").strip().lower().removesuffix(".exe")
    if n in APP_ALIASES:
        return n
    if n in SYNONYMS:
        return SYNONYMS[n]
    for k, v in SYNONYMS.items():
        if re.search(rf"\b{re.escape(k)}\b", n):
            return v
    for k in APP_ALIASES:
        if re.search(rf"\b{re.escape(k)}\b", n):
            return k
    return n


def _find_installed_app(app_name: str) -> str:
    """Resolve an executable path for common Windows applications."""
    name = canonical_app(app_name)
    office_dirs = [
        r"C:\Program Files\Microsoft Office\root\Office16",
        r"C:\Program Files (x86)\Microsoft Office\root\Office16",
        r"C:\Program Files\Microsoft Office\Office16",
        r"C:\Program Files (x86)\Microsoft Office\Office16",
        r"C:\Program Files\Microsoft Office\Office15",
    ]
    office_exe = {"word": "WINWORD.EXE", "excel": "EXCEL.EXE", "powerpoint": "POWERPNT.EXE", "outlook": "OUTLOOK.EXE"}
    if name in office_exe:
        for base in office_dirs:
            p = os.path.join(base, office_exe[name])
            if os.path.exists(p):
                return p
        return office_exe[name].lower()
    simple = {"notepad": "notepad.exe", "calculator": "calc.exe", "paint": "mspaint.exe", "explorer": "explorer.exe",
              "terminal": "wt.exe", "vscode": "code", "settings": "ms-settings:"}
    if name == "chrome":
        for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                  os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")):
            if os.path.exists(p):
                return p
    if name == "edge":
        return "msedge.exe"
    target = simple.get(name, app_name.strip())
    return shutil.which(target) or target


def _process_name(pid: int) -> str:
    """Executable name for a process id (Windows), lower-case; '' if unknown."""
    try:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(520)
            size = wintypes.DWORD(520)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
        finally:
            k32.CloseHandle(h)
    except Exception:
        pass
    return ""


def window_matches(app: str, name: str, exe: str, class_name: str = "") -> bool:
    """Does a top-level window belong to the canonical app?"""
    exes, title_re = APP_ALIASES.get(app, ((), re.escape(app) if app else r"$^"))
    if app == "explorer":
        return class_name == "CabinetWClass" or (exe == "explorer.exe" and bool(re.search(title_re, name, re.I)))
    if exe and exe in exes:
        return True
    return bool(name) and bool(re.search(title_re, name, re.I))


_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', " ": " "})


def _canon(text: str | None) -> str:
    """Field text for comparisons: one kind of line break, straight quotes (Word's smart quotes / autocorrect)."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").translate(_QUOTES)


def _chunks(content: str, size: int = 24) -> list[str]:
    """Split text into chunks of whole words (~size chars) so typing can be checked as it goes."""
    out, cur = [], ""
    for piece in re.findall(r"\S+\s*|\s+", content.replace("\r\n", "\n")):
        if cur and len(cur) + len(piece) > size:
            out.append(cur)
            cur = ""
        cur += piece
    if cur:
        out.append(cur)
    return out


def _chunk_arrived(before: str, after: str, chunk: str) -> bool:
    """Did exactly this chunk get inserted (allowing an editor's tiny autocorrections)?"""
    exp = len(_canon(chunk))
    delta = len(after) - len(before)
    if abs(delta - exp) > max(1, exp // 25):
        return False
    words = " ".join(_canon(chunk).split()).lower()
    return not words or words[:12] in " ".join(after.split()).lower()   # Word may capitalise the first letter


def fingerprint(state: dict) -> str:
    semantic = [(a.get("kind"), a.get("label"), a.get("value"), a.get("checked"), a.get("selected"), a.get("expanded"))
                for a in state.get("actions", [])]
    content = {"title": state.get("title"), "text": state.get("text"), "actions": semantic,
               "clipboard": state.get("clipboard")}
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()[:16]


class DesktopCapture:
    """Controls Windows desktop apps through UI Automation with real mouse and keyboard input."""

    mode = "desktop"

    CLICKABLE_TYPES = {
        "ButtonControl", "MenuItemControl", "ListItemControl", "TreeItemControl", "TabItemControl",
        "HyperlinkControl", "CheckBoxControl", "RadioButtonControl", "SplitButtonControl",
        "DataItemControl", "MenuBarControl",
    }
    FILLABLE_TYPES = {"EditControl", "DocumentControl"}
    SELECTABLE_TYPES = {"ComboBoxControl"}

    def __init__(self) -> None:
        self._target_window: Any = None
        self._controls_map: dict[str, Any] = {}
        self._last_window_title: str = "Desktop"
        self._target_pid: int | None = None   # process of the app we are driving (to find it again after dialogs)
        self._popups: list[Any] = []           # menus/flyouts opened by our own actions (class-agnostic)
        self._opened: dict[int, str] = {}      # hwnd -> title of windows the agent opened (closable at the end)
        self._run_opened: set[int] = set()     # ... during the CURRENT run (finishing-up only looks at these)
        self._process: subprocess.Popen | None = None
        try:
            import pyautogui
            pyautogui.FAILSAFE = True   # slam the mouse into a screen corner to abort
            pyautogui.PAUSE = 0.05
        except Exception:
            pass

    @property
    def is_alive(self) -> bool:
        return True

    # ------------------------------------------------------------ windows
    def _top_windows(self) -> list[Any]:
        import uiautomation as auto
        out = []
        try:
            for w in auto.GetRootControl().GetChildren():
                try:
                    if w.ControlTypeName not in ("WindowControl", "PaneControl"):
                        continue
                    name = (w.Name or "").strip()
                    if name in EXCLUDED_WINDOW_NAMES or is_own_ui(name) or w.ClassName in EXCLUDED_CLASSES:
                        continue
                    out.append(w)
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("Window enumeration failed: %s", exc)
        return out

    def _window_info(self, w: Any) -> tuple[str, str, str]:
        try:
            return (w.Name or "").strip(), _process_name(w.ProcessId), w.ClassName or ""
        except Exception:
            return "", "", ""

    def _activate(self, w: Any) -> None:
        import uiautomation as auto
        try:
            if w.IsTopLevel() and getattr(w, "GetWindowPattern", None):
                try:
                    if w.GetWindowPattern().WindowVisualState == 2:  # minimized
                        w.ShowWindow(auto.SW.Restore)
                except Exception:
                    pass
        except Exception:
            pass
        for attempt in range(2):
            for fn in ("SetActive", "SetFocus"):
                try:
                    getattr(w, fn)()
                except Exception:
                    pass
            time.sleep(0.25)
            if self._is_foreground(w):
                break
            # Windows blocks SetForegroundWindow from background processes; a key tap lifts the lock
            try:
                import pyautogui
                pyautogui.press("alt")
            except Exception:
                pass
        self._target_window = w
        self._last_window_title = (w.Name or "").strip() or self._last_window_title
        try:
            self._target_pid = w.ProcessId
        except Exception:
            pass

    @staticmethod
    def _is_foreground(w: Any) -> bool:
        """
        True if w (or a dialog it owns, or the window that owns it) is the foreground window.
        Compares real window handles: in the UIA tree an app's dialogs (Open / Save As) are nested
        inside the owner window, so UIA's 'top-level control' is not reliable for this.
        """
        try:  # pragma: no cover - Windows only
            import ctypes
            user32 = ctypes.windll.user32
            fg = user32.GetForegroundWindow()
            hwnd = w.NativeWindowHandle
            if not fg or not hwnd:
                return False
            if fg == hwnd:
                return True
            GA_ROOTOWNER = 3
            return user32.GetAncestor(fg, GA_ROOTOWNER) == user32.GetAncestor(hwnd, GA_ROOTOWNER)
        except Exception:
            return False

    @staticmethod
    def _foreground_window() -> Any:
        """UIA control for the real foreground window (a dialog stays a dialog, not its owner)."""
        import uiautomation as auto
        try:  # pragma: no cover - Windows only
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if hwnd:
                return auto.ControlFromHandle(hwnd)
        except Exception:
            pass
        try:
            fg = auto.GetForegroundControl()
            return fg.GetTopLevelControl() if fg else None
        except Exception:
            return None

    def focus_app(self, hint: str) -> bool:
        """Bring an already-open window of the app to the front."""
        app = canonical_app(hint)
        for w in self._top_windows():
            name, exe, cls = self._window_info(w)
            if window_matches(app, name, exe, cls):
                self._activate(w)
                return True
        return False

    def launch_app(self, name: str) -> bool:
        """Start an app (or open a folder / URL) and attach to the window that appears."""
        name = (name or "").strip()
        if not name:
            return False
        lower = name.lower()
        app = canonical_app(name)
        before = {w.NativeWindowHandle for w in self._top_windows()}

        try:
            if lower.startswith(("http://", "https://", "www.")):
                webbrowser.open(name if "://" in name else "https://" + name)
                app = "chrome"
            elif app == "explorer":
                folder = next((f for f in KNOWN_FOLDERS if f in lower), None)
                path = known_folders().get(folder.capitalize()) if folder else None
                rest = name.split(None, 1)[1].strip().strip('"') if len(name.split(None, 1)) == 2 else ""
                if rest and os.path.isdir(expand_known(rest)):
                    path = expand_known(rest)
                if path:   # reuse an Explorer window already showing that folder (no pile-up of windows)
                    base = os.path.basename(os.path.normpath(path)).lower()
                    for w in self._top_windows():
                        n, e, c = self._window_info(w)
                        if c == "CabinetWClass" and (n or "").lower().split(" - ")[0].strip() == base:
                            self._activate(w)
                            return True
                subprocess.Popen(["explorer.exe", path] if path and os.path.exists(path) else ["explorer.exe"])
            elif os.path.isdir(name):
                subprocess.Popen(["explorer.exe", name])
                app = "explorer"
            else:
                # "notepad C:\\path\\file.txt" -> open that file with the app
                parts = name.split(None, 1)
                arg = expand_known(parts[1].strip().strip('"')) if len(parts) == 2 else None
                if arg and not os.path.exists(arg):
                    arg = None
                exe = _find_installed_app(parts[0] if arg else name)
                try:
                    if exe.endswith(":"):
                        os.startfile(arg or exe)  # type: ignore[attr-defined]
                    else:
                        extra = []
                        if not arg and canonical_app(parts[0]) in ("word", "excel", "powerpoint"):
                            extra = {"word": ["/w"], "excel": ["/e"], "powerpoint": []}[canonical_app(parts[0])]
                        self._process = subprocess.Popen([exe] + ([arg] if arg else extra))
                except (FileNotFoundError, OSError):
                    # `start` resolves App Paths (msedge, winword, excel ...) that are not on PATH
                    subprocess.Popen(["cmd", "/c", "start", "", exe] + ([arg] if arg else []), shell=False)
        except Exception as exc:
            logger.warning("Could not launch '%s': %s", name, exc)

        return self._attach_new_window(before, app)

    def _attach_new_window(self, before: set, app: str, timeout: float = 10) -> bool:
        """Attach to the top-level window that appeared after `before` (UWP launchers exit immediately)."""
        deadline = time.time() + timeout
        fallback = None
        while time.time() < deadline:
            fresh = [w for w in self._top_windows() if w.NativeWindowHandle not in before]
            match = next((w for w in fresh if window_matches(app, *self._window_info(w))), None)
            if match is not None:
                self._activate(match)
                self._mark_opened(match)
                return True
            if fresh and fallback is None:
                fallback = fresh[0]
            time.sleep(0.3)
        if fallback is not None and not self.focus_app(app):
            self._activate(fallback)   # e.g. an app whose window title/process we don't know
            self._mark_opened(fallback)
            return True
        # Single-instance apps may just focus an existing window
        return self.focus_app(app)

    def open_file(self, spec: str) -> dict[str, Any]:
        """
        spec: "<name or path>" or "<name or path> | <app>". Finds the file (Desktop, Documents, Downloads …,
        OneDrive-aware) and opens it with its default app or the named one, then attaches to its window.
        """
        from .files import find_files, folder_hint, open_path

        name, _, app = (p.strip() for p in spec.partition("|"))
        hits = find_files(name, hint_dir=folder_hint(name))
        if not hits:
            from .files import recent_files
            recent = recent_files(name, folder_hint(name))
            hint = ""
            if recent:
                hint = (" Recently changed files there: " + "; ".join(recent) + ". If one of them is the file meant "
                        "(e.g. it was saved under another name), open that one or rename it (FILE_OP / F2) — do not "
                        "retry this name.")
            raise ValueError(f"No file named '{Path(name).name}' exists (searched Desktop, Documents, Downloads, "
                             f"Pictures, Music, Videos).{hint}")
        path = hits[0]
        note = f"Opened {path}"
        if len(hits) > 1:
            note += f" (newest of {len(hits)} matches: " + "; ".join(str(h) for h in hits[1:4]) + ")"
        before = {w.NativeWindowHandle for w in self._top_windows()}
        titles_before = self._window_titles()
        if app:
            self.launch_app(f"{app} {path}")
        else:
            open_path(path)
        title = self._attach_file_window(before, path, titles_before)
        if not title:
            # some apps show the file only after a moment, or in a window whose title we did not recognise:
            # attach to the app that handles this kind of file
            app_for = {".txt": "notepad", ".log": "notepad", ".md": "notepad", ".ini": "notepad",
                       ".docx": "word", ".doc": "word", ".xlsx": "excel", ".xls": "excel", ".csv": "excel",
                       ".pptx": "powerpoint"}.get(path.suffix.lower())
            if app_for and self.focus_app(app_for):
                title = (getattr(self._target_window, "Name", "") or "").strip()
                if not self._title_shows(title, path):
                    note += f" (attached to '{title}' — check that it shows {path.name}; if not, use its tab)"
            if not title:
                titles = [t for t in self._window_titles().values() if t][:10]
                note += f" Open windows now: {titles}."
        if title:
            note += (f". It is open in the window '{title}' (now the active window) — continue the task THERE; "
                     "do not switch back to File Explorer.")
        else:
            note += ". Its window could not be found yet — WAIT, then SWITCH_WINDOW to the window showing the file."
        return {"executed": "OPEN_FILE", "note": note}

    @staticmethod
    def _title_shows(title: str, path: Path) -> bool:
        t = (title or "").lower()
        return bool(t) and (path.name.lower() in t or path.stem.lower() in t)

    def _window_titles(self) -> dict[int, str]:
        out = {}
        for w in self._top_windows():
            try:
                out[w.NativeWindowHandle] = (w.Name or "").strip()
            except Exception:
                continue
        return out

    def _attach_file_window(self, before: set, path: Path, titles_before: dict[int, str] | None = None,
                            timeout: float = 8.0) -> str | None:
        """
        Attach to the window showing `path`. Apps such as the new Notepad open files as a TAB in an existing
        window (no new window appears) and hide the extension in the title ('notes - Notepad'), so match the
        window by the file's name, new windows first; failing that, take the window (not File Explorer) that
        appeared or changed its title since the file was opened.
        """
        titles_before = titles_before or {}
        deadline = time.time() + timeout
        while time.time() < deadline:
            wins = self._top_windows()
            hits = [w for w in wins if self._title_shows(w.Name or "", path)]
            hits.sort(key=lambda w: w.NativeWindowHandle in before)          # new windows first
            if not hits and time.time() > deadline - timeout / 2:
                for w in wins:
                    try:
                        n, exe, cls = self._window_info(w)
                    except Exception:
                        continue
                    changed = w.NativeWindowHandle not in titles_before or titles_before.get(w.NativeWindowHandle) != n
                    if changed and "explorer" not in exe.lower() and not is_own_ui(n):
                        hits = [w]
                        break
            if hits:
                w = hits[0]
                if w.NativeWindowHandle not in before:
                    self._mark_opened(w)
                self._activate(w)
                return (w.Name or "").strip()
            time.sleep(0.3)
        logger.info("OPEN_FILE: no window for %s; open windows: %s", path.name,
                    [(n, ) for n in self._window_titles().values()][:12])
        return None

    # ------------------------------------------------------- delete guard
    DELETE_DIALOG = re.compile(r"^(delete (file|folder|multiple items|shortcut)|confirm (file|folder|multiple file) "
                               r"delete|delete items?)$", re.I)

    @staticmethod
    def _destructive_keys(keys: list[str]) -> bool:
        ks = set(keys)
        return "delete" in ks or "del" in ks or ks == {"ctrl", "d"}

    def _refuse_unexpected_delete_dialog(self, op: str) -> None:
        """A Windows 'Delete …?' prompt the agent did not ask for (via DELETE_FILE) is always answered No."""
        if op == "DELETE_FILE":
            return
        try:
            import pyautogui
            import uiautomation as auto
            for w in auto.GetRootControl().GetChildren():
                name = (w.Name or "").strip()
                if not self.DELETE_DIALOG.match(name):
                    continue
                no = w.ButtonControl(Name="No", searchDepth=6)
                if no.Exists(0.3):
                    try:
                        no.GetInvokePattern().Invoke()
                    except Exception:
                        self._click(no)
                else:
                    w.SetFocus()
                    pyautogui.press("escape")
                logger.warning("Refused an unexpected delete prompt: %s", name)
                raise ValueError(f"Windows asked '{name}' after {op} — answered No, nothing was deleted. Something "
                                 "was selected that should not be; do not press Delete. Use DELETE_FILE to delete.")
        except ValueError:
            raise
        except Exception as exc:
            logger.debug("delete-dialog check failed: %s", exc)

    # ------------------------------------------------------------ save as
    SAVE_DIALOG_TITLES = ("save as", "save", "speichern", "enregistrer", "guardar", "salva")

    def _save_dialog(self) -> Any:
        w = self._foreground_window()
        if w is None:
            return None
        name, exe, cls = self._window_info(w)
        if cls == "#32770" and any(t == (name or "").strip().lower() or (name or "").lower().startswith(t)
                                   for t in self.SAVE_DIALOG_TITLES):
            return w
        return None

    @staticmethod
    def _file_name_box(dlg: Any) -> Any:
        import uiautomation as auto  # noqa: F401
        for finder in (lambda: dlg.EditControl(AutomationId="1001", searchDepth=8),
                       lambda: dlg.EditControl(Name="File name:", searchDepth=8),
                       lambda: dlg.ComboBoxControl(Name="File name:", searchDepth=8)):
            try:
                c = finder()
                if c.Exists(0.5):
                    return c
            except Exception:
                continue
        return None

    def save_as(self, target: str) -> str:
        """
        One step instead of four Jev decisions: open Save As, type the full path, Save, and CHECK the file exists.
        Refuses to overwrite: if Windows asks to replace an existing file, the answer is No.
        """
        import pyautogui
        path = Path(expand_known(target.strip().strip('"')))
        if not path.is_absolute():
            path = Path(known_folders().get("Documents", str(Path.home() / "Documents"))) / path
        if path.exists():
            raise ValueError(f"'{path}' already exists — nothing was overwritten. Pick another name or ask the user.")
        self._ensure_foreground()
        dlg = self._save_dialog()
        exe = (self._window_info(self._target_window)[1] if self._target_window is not None else "").lower()
        office = any(x in exe for x in ("winword", "excel", "powerpnt"))
        order = ((("f12",), ("ctrl", "s")) if office            # Ctrl+Shift+S means something else in Word
                 else (("ctrl", "shift", "s"), ("f12",), ("ctrl", "s")))
        for keys in order:
            if dlg is not None:
                break
            pyautogui.hotkey(*keys)
            for _ in range(10):
                time.sleep(0.25)
                dlg = self._save_dialog()
                if dlg is not None:
                    break
        if dlg is None:
            raise ValueError("No Save As dialog opened (tried Ctrl+Shift+S, F12, Ctrl+S) — use the File menu")
        box = self._file_name_box(dlg)
        if box is None:
            raise ValueError("The Save As dialog has no File name box I can find")
        self._target_window = dlg
        self._focus_field(box)
        self._clear_field(box)
        self._enter_text(box, str(path), replace=True)
        time.sleep(0.2)
        pyautogui.press("enter")
        deadline = time.time() + 8
        while time.time() < deadline:
            time.sleep(0.4)
            fg = self._foreground_window()
            fname = (getattr(fg, "Name", "") or "").lower()
            if fg is not None and ("confirm" in fname or "replace" in fname or "already exists" in fname):
                pyautogui.press("escape")             # never overwrite
                raise ValueError(f"'{path.name}' already exists there — nothing was overwritten")
            if path.exists():
                break
        if not path.exists():
            raise ValueError(f"Save As did not create '{path}' — check the dialog (folder / name / file type)")
        time.sleep(0.4)
        self._attach_foreground()
        return f"Saved as {path} (checked: the file exists on disk)."

    # ------------------------------------------- drag / ranges / office
    def _office_app(self) -> str | None:
        """'excel' / 'word' when that app is in front and Office automation is enabled."""
        try:
            from ..config import get_settings
            if not get_settings().office_automation:
                return None
            exe = _process_name(self._target_window.ProcessId) if self._target_window else ""
        except Exception:
            return None
        return {"excel.exe": "excel", "winword.exe": "word"}.get(exe)

    def _is_spreadsheet(self) -> bool:
        try:
            exe = _process_name(self._target_window.ProcessId) if self._target_window else ""
            return exe == "excel.exe" or "excel" in (self._target_window.Name or "").lower()
        except Exception:
            return False

    def office_context(self) -> dict[str, Any]:
        from .office import describe
        app = self._office_app()
        return describe(app) if app else {}

    @staticmethod
    def _excel_snapshot(actions: list[dict]) -> str:
        """
        What the sheet contains (read-only), so Jev and the verifier can see the table taking shape:
        active cell + up to 25×12 cells of the used range, read in ONE automation call.
        """
        name_box = next((a.get("value") for a in actions if a.get("label") == "Name Box"), "")
        lines = [f"[Excel] Active cell: {name_box or '?'} (cells are not listed one by one; use SELECT_RANGE / "
                 "TYPE_TABLE)"]
        try:  # pragma: no cover - Windows + Excel only
            from .office import _com_app
            xl = _com_app("excel")
            ws = xl.ActiveSheet
            used = ws.UsedRange
            rows, cols = min(used.Rows.Count, 25), min(used.Columns.Count, 12)
            block = ws.Range(used.Cells(1, 1), used.Cells(rows, cols)).Value
            if not isinstance(block, tuple):
                block = ((block,),)
            lines.append(f"[Sheet '{ws.Name}', used range {used.Address(False, False)}]")
            for r in block:
                vals = ["" if v is None else (str(int(v)) if isinstance(v, float) and v.is_integer() else str(v))
                        for v in r]
                if any(vals):
                    lines.append(" | ".join(vals))
        except Exception:
            pass
        return "\n".join(lines)

    def type_table(self, value: str) -> str:
        """
        Enter rows of values starting at an address. Office automation (if enabled) writes the block in one go;
        otherwise each row is typed with real Tab key presses after selecting its first cell by address.
        """
        import pyautogui
        from .sheet import as_number, block_range, parse_address, parse_table
        start, rows = parse_table(value)
        top_left, bottom_right = block_range(start, rows)
        sheet, col, row0 = parse_address(start)
        prefix = f"{sheet}!" if sheet else ""
        if self._office_app() == "excel":
            try:
                from .office import _com_app
                xl = _com_app("excel")
                ws = xl.ActiveWorkbook.Worksheets(sheet) if sheet else xl.ActiveSheet
                width = max(len(r) for r in rows)
                data = tuple(tuple(as_number(c) for c in r + [""] * (width - len(r))) for r in rows)
                ws.Range(f"{top_left}:{bottom_right}").Value = data
                return f"Entered {len(rows)} rows into {prefix}{top_left}:{bottom_right} (Office automation)."
            except Exception as exc:
                logger.info("Office fast path for TYPE_TABLE failed (%s); typing instead", exc)
        for i, cells in enumerate(rows):
            self.select_range(f"{prefix}{col}{row0 + i}")
            line = "\t".join(c.replace("\t", " ").replace("\n", " ") for c in cells)
            if not self._type_unicode(line + "\n"):
                pyautogui.write(line.replace("\t", ""), interval=0.01)   # last resort, no unicode
                pyautogui.press("enter")
            time.sleep(0.15)
        return f"Entered {len(rows)} rows into {prefix}{top_left}:{bottom_right}."

    def select_range(self, address: str) -> str:
        """Select a range by typing its address into Excel's Name Box (falls back to Ctrl+G Go To)."""
        import pyautogui
        import uiautomation as auto
        address = address.strip().strip("'\"")
        box = None
        try:
            box = self._target_window.EditControl(searchDepth=14, Name="Name Box")
            if not box.Exists(0.5):
                box = self._target_window.ComboBoxControl(searchDepth=14, Name="Name Box")
                box = box if box.Exists(0.3) else None
        except Exception:
            box = None
        if box is not None:
            try:
                self._focus_field(box)
                pyautogui.hotkey("ctrl", "a")      # inside the Name Box (checked): selects its text only
            except ValueError:
                box = None
        if box is None:
            pyautogui.hotkey("ctrl", "g")   # Go To dialog: the Reference box has focus
            time.sleep(0.5)
        self._type_unicode(address) or pyautogui.write(address, interval=0.02)
        pyautogui.press("enter")
        time.sleep(0.3)
        _ = auto  # keep import for type checkers
        return f"Selected {address}."

    def _drag(self, page: dict[str, Any], action: dict[str, Any], src: Any, dest_label: str | None) -> dict[str, Any]:
        """Drag the source element onto the element whose label the text LLM chose."""
        import pyautogui
        if not dest_label:
            raise ValueError("DRAG needs a destination")
        want = dest_label.strip().lower()
        cands = [a for a in page.get("actions", []) if a.get("id") != action.get("id")
                 and a.get("kind") in ("click", "fill", "select") and str(a.get("label", "")).strip().lower() == want]
        if not cands:
            cands = [a for a in page.get("actions", []) if a.get("id") != action.get("id")
                     and want in str(a.get("label", "")).lower()]
        dest = self._controls_map.get(cands[0]["id"]) if cands else None
        p1, p2 = self._center(src), self._center(dest) if dest is not None else None
        if not p1 or not p2:
            raise ValueError(f"Cannot drag: destination '{dest_label}' not found on screen")
        self._ensure_foreground()
        pyautogui.moveTo(*p1, duration=0.2)
        pyautogui.mouseDown()
        time.sleep(0.15)
        mid = ((p1[0] + p2[0]) // 2, (p1[1] + p2[1]) // 2)
        pyautogui.moveTo(*mid, duration=0.25)   # some apps need intermediate moves to start a drag
        pyautogui.moveTo(*p2, duration=0.35)
        time.sleep(0.15)
        pyautogui.mouseUp()
        time.sleep(0.4)
        return {"executed": action["id"], "note": f"Dragged '{action.get('label')}' onto '{cands[0].get('label')}'."}

    # --------------------------------------------------- finishing touches
    def _mark_opened(self, w: Any) -> None:
        try:
            self._opened[w.NativeWindowHandle] = (w.Name or "").strip()
            self._run_opened.add(w.NativeWindowHandle)
        except Exception:
            pass

    def begin_run(self) -> None:
        """A new task starts: windows from earlier tasks are no longer this task's business."""
        self._run_opened = set()

    def opened_windows(self, this_run: bool = True) -> list[str]:
        """Titles of windows the agent opened (this run by default) that are still open (refreshing titles)."""
        import uiautomation as auto
        out = []
        for hwnd in list(self._opened):
            if this_run and hwnd not in self._run_opened:
                continue
            try:
                w = auto.ControlFromHandle(hwnd)
                if w is None or not w.Exists(0.05):
                    self._opened.pop(hwnd, None)
                    continue
                self._opened[hwnd] = (w.Name or "").strip()
                out.append(self._opened[hwnd])
            except Exception:
                self._opened.pop(hwnd, None)
        return [t for t in out if t]

    def pending_issues(self, page: dict[str, Any] | None = None) -> list[str]:
        """Unfinished business that 'done' must not leave behind (unsaved edits, open rename box, open menu)."""
        import uiautomation as auto
        issues: list[str] = []
        titles = [(page or {}).get("title", "")] + self.opened_windows()
        for t in dict.fromkeys(titles):
            if t and (t.startswith("*") or t.startswith("● ") or "(Unsaved)" in t or " - Not saved" in t
                      or t.endswith(" •")):
                issues.append(f"'{t}' has unsaved changes — save them (PRESS_KEY Ctrl+S)")
        try:
            f = auto.GetFocusedControl()
            parent = f.GetParentControl() if f else None
            if (f is not None and f.ControlTypeName == "EditControl" and parent is not None
                    and parent.ControlTypeName in ("ListItemControl", "DataItemControl", "TreeItemControl")):
                issues.append("a rename/edit box is still open — confirm it (PRESS_KEY Enter)")
        except Exception:
            pass
        if any(p.Exists(0.05) for p in self._popups if p is not None):
            issues.append("a menu is still open — close it (PRESS_KEY Escape) or pick the item")
        return issues

    def close_window(self) -> str:
        """Close the current target window, but only if the agent opened it."""
        import pyautogui
        w = self._target_window
        hwnd = getattr(w, "NativeWindowHandle", None)
        if w is None or hwnd not in self._opened:
            raise ValueError("Only windows opened for this task may be closed — leave this one open")
        title = (w.Name or "").strip()
        try:
            w.GetWindowPattern().Close()
        except Exception:
            self._ensure_foreground()
            pyautogui.hotkey("alt", "f4")
        time.sleep(0.6)
        return title

    def windows_search(self, query: str) -> None:
        """Open taskbar search (Win+S), type the query and attach to the results flyout."""
        import pyautogui
        import uiautomation as auto
        pyautogui.hotkey("win", "s")
        time.sleep(0.9)
        self._paste_text(query)
        time.sleep(1.6)  # results stream in
        flyout = self._foreground_window()
        if flyout is None:
            raise ValueError("Windows Search did not open")
        self._target_window = flyout          # search UI is excluded from normal window lists, attach explicitly
        self._last_window_title = flyout.Name or "Search"
        try:
            self._target_pid = flyout.ProcessId
        except Exception:
            pass

    def start(self, url: str | None = None) -> None:
        if url:
            # Always start a fresh window/document so we never overwrite something the user has open.
            # Single-instance apps (e.g. Calculator) fall back to focusing the existing window.
            self.launch_app(url)
        else:
            self._attach_foreground()

    def navigate(self, url: str) -> None:
        self.start(url)

    def _attach_foreground(self) -> None:
        import uiautomation as auto
        top = self._foreground_window()
        if top and (top.Name or "") and not is_own_ui(top.Name) and top.Name not in EXCLUDED_WINDOW_NAMES:
            self._target_window = top
            self._last_window_title = top.Name
            try:
                self._target_pid = top.ProcessId
            except Exception:
                pass
            return
        wins = self._top_windows()
        if wins:
            self._activate(wins[0])
        else:
            self._target_window = auto.GetRootControl()

    # ------------------------------------------------------------ observe
    def observe(self, screenshot: bool = False) -> dict[str, Any]:
        import uiautomation as auto

        try:
            alive = self._target_window is not None and self._target_window.Exists(0.2)
        except Exception:
            alive = False
        if not alive:
            # e.g. a Save As dialog closed: go back to a window of the same app, not whatever is in front
            same_app = None
            if self._target_pid:
                same_app = next((w for w in self._top_windows() if w.ProcessId == self._target_pid), None)
            if same_app is not None:
                self._activate(same_app)
            else:
                self._attach_foreground()

        target = self._target_window or auto.GetRootControl()
        title = (getattr(target, "Name", "") or self._last_window_title or "Desktop").strip()
        self._last_window_title = title
        try:
            exe = _process_name(target.ProcessId)
        except Exception:
            exe = ""
        self._controls_map.clear()

        actions: list[dict] = []
        texts: list[str] = []
        sensitive: dict[str, bool] = {}
        node = 0
        started = time.perf_counter()

        self._menu_ids: set[str] = set()

        def add(action: dict, ctrl: Any) -> None:
            actions.append(action)
            self._controls_map[action["id"]] = ctrl
            if str(action.get("label", "")).startswith("[menu] "):
                self._menu_ids.add(action["id"])

        popups = self._open_popups(target)
        excel_grid = exe == "excel.exe"

        def pruned_walk(root, max_depth):
            """Depth-first walk that does not descend into Excel's cell grid (hundreds of cells = slow)."""
            stack = [(root, 0)]
            while stack:
                c, d = stack.pop()
                yield c, d
                if d >= max_depth:
                    continue
                if excel_grid and d > 0:
                    try:
                        if c.ClassName in ("EXCEL7", "EXCEL<", "EXCEL6") or c.ControlTypeName in ("DataGridControl",
                                                                                                 "TableControl"):
                            continue
                    except Exception:
                        pass
                try:
                    kids = c.GetChildren()
                except Exception:
                    kids = []
                for k in reversed(kids):
                    stack.append((k, d + 1))

        def walk_all():
            # open menus first, so their items are always within the time/size budget
            for p in popups:
                for c, d in pruned_walk(p, 8):
                    yield c, d + 1, True
            for c, d in pruned_walk(target, 10):
                yield c, d, False

        if popups:
            texts.append("[A context menu / popup is open — its items are marked [menu]]")
        try:
            for ctrl, depth, in_menu in walk_all():
                if depth == 0:
                    continue
                if time.perf_counter() - started > WALK_TIME_BUDGET_S or len(actions) >= MAX_ELEMENT_ACTIONS:
                    break
                try:
                    ctype = ctrl.ControlTypeName
                    if excel_grid and not in_menu and ctype in ("DataItemControl", "ListItemControl", "HeaderItemControl"):
                        continue   # spreadsheet cells: reached by address, contents come from the sheet snapshot
                    if not ctrl.IsEnabled or ctrl.IsOffscreen:
                        continue
                    name = (ctrl.Name or "").strip()
                    if name and len(texts) < 150 and name not in texts:
                        texts.append(name[:200])

                    if ctype in self.FILLABLE_TYPES:
                        try:
                            if ctrl.IsPassword:
                                sensitive["password"] = True
                                continue
                        except Exception:
                            pass
                        node += 1
                        val = ""
                        try:
                            val = ctrl.GetValuePattern().Value
                        except Exception:
                            try:
                                val = ctrl.GetTextPattern().DocumentRange.GetText(4000)
                            except Exception:
                                pass
                        label = name or ("Document editor" if ctype == "DocumentControl" else f"Text field {node}")
                        add({"id": f"fill_{node}", "kind": "fill", "node": node, "role": "textbox",
                             "label": label[:120], "value": (val or "")[:1500],
                             "multiline": ctype == "DocumentControl"}, ctrl)
                        if val and ctype == "DocumentControl":
                            texts.append(f"[{label[:40]} content] {val[:1500]}")

                    elif ctype in self.SELECTABLE_TYPES and name:
                        node += 1
                        current = ""
                        try:
                            current = ctrl.GetValuePattern().Value
                        except Exception:
                            pass
                        options: list[str] = []
                        try:
                            for child in ctrl.GetChildren():
                                kids = child.GetChildren() if child.ControlTypeName == "ListControl" else [child]
                                for item in kids:
                                    nm = (item.Name or "").strip()
                                    if nm and nm != current and nm not in options:
                                        options.append(nm)
                        except Exception:
                            pass
                        for i, opt in enumerate(options[:25]):
                            add({"id": f"select_{node}_{i}", "kind": "select", "node": node, "role": "combobox",
                                 "label": f"{name} → {opt}", "value": opt, "current_value": current}, ctrl)
                        add({"id": f"click_{node}", "kind": "click", "node": node, "role": "combobox",
                             "label": f"Open {name}", "value": current}, ctrl)

                    elif ctype in self.CLICKABLE_TYPES and name:
                        if self._center(ctrl) is None:
                            try:
                                ctrl.GetScrollItemPattern()   # off-screen but scrollable into view: keep it
                            except Exception:
                                continue                      # virtualized/hidden: not really clickable
                        node += 1
                        action = {"id": f"click_{node}", "kind": "click", "node": node,
                                  "role": ctype.replace("Control", "").lower(),
                                  "label": ("[menu] " if in_menu else "") + name[:120],
                                  "extra_ops": ["DOUBLE_CLICK", "RIGHT_CLICK", "DRAG"]}
                        if ctype in ("CheckBoxControl", "RadioButtonControl"):
                            try:
                                action["checked"] = str(ctrl.GetTogglePattern().ToggleState == 1).lower()
                            except Exception:
                                try:
                                    action["checked"] = str(ctrl.GetSelectionItemPattern().IsSelected).lower()
                                except Exception:
                                    pass
                        if ctype in ("ListItemControl", "TreeItemControl", "DataItemControl", "TabItemControl"):
                            try:
                                action["selected"] = str(ctrl.GetSelectionItemPattern().IsSelected).lower()
                            except Exception:
                                pass
                        add(action, ctrl)
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("Control walk error: %s", exc)

        if excel_grid:
            texts.insert(0, self._excel_snapshot(actions))
        actions.extend(self._control_actions())
        info: dict[str, Any] = {
            "url": f"desktop://{exe or 'app'}/{title}",
            "title": title,
            "text": "\n".join(texts)[:9000],
            "actions": actions,
            "scroll": {"y": 0, "height": 0},
            "sensitive": sensitive,
            "mode": "desktop",
            "clipboard": read_clipboard(),
        }
        info["fingerprint"] = fingerprint(info)
        if screenshot:
            info["screenshot"] = self._take_screenshot()
        return info

    @staticmethod
    def _raw_top_handles() -> set:
        import uiautomation as auto
        try:
            return {w.NativeWindowHandle for w in auto.GetRootControl().GetChildren()}
        except Exception:
            return set()

    def _detect_new_popups(self, raw_before: set, click_pt: tuple[int, int] | None) -> int:
        """
        Find the menu a right-click opened without relying on window class names:
          1. any new top-level window that appeared since the click, and
          2. whatever window sits just below-right of the click point (where context menus open).
        Returns how many menu items were found.
        """
        import uiautomation as auto
        cands: list[Any] = []
        try:
            for w in auto.GetRootControl().GetChildren():
                if w.NativeWindowHandle not in raw_before and not w.IsOffscreen:
                    cands.append(w)
        except Exception:
            pass
        if click_pt:
            for dx, dy in ((25, 25), (60, 40), (-60, 40), (25, -40)):
                try:
                    c = auto.ControlFromPoint(click_pt[0] + dx, click_pt[1] + dy)
                    top = c.GetTopLevelControl() if c else None
                    tgt = getattr(self._target_window, "NativeWindowHandle", None)
                    if top is not None and top.NativeWindowHandle != tgt:
                        cands.append(top)
                    elif c is not None and c.ControlTypeName in ("MenuItemControl", "MenuControl"):
                        menu = c if c.ControlTypeName == "MenuControl" else c.GetParentControl()
                        if menu is not None:
                            cands.append(menu)
                except Exception:
                    continue
        popups, items = [], 0
        for c in cands:
            try:
                if any(c.NativeWindowHandle and c.NativeWindowHandle == p.NativeWindowHandle for p in popups):
                    continue
                if c.ClassName in EXCLUDED_CLASSES or (c.Name or "") in ("Program Manager", "Taskbar"):
                    continue
                kinds = [x.ControlTypeName for x, _ in auto.WalkControl(c, maxDepth=8)]
                if "MenuItemControl" in kinds:          # a real menu, not some window behind the cursor
                    popups.append(c)
                    items += sum(k in ("MenuItemControl", "ButtonControl") for k in kinds)
            except Exception:
                continue
        self._popups = popups[:3]
        return items

    def _open_popups(self, target: Any) -> list[Any]:
        """Visible context menus / flyouts (Windows 11 right-click menu, classic menus, dropdowns)."""
        import uiautomation as auto
        out: list[Any] = []
        for p in self._popups:          # menus our last right-click opened
            try:
                if p.Exists(0.05) and not p.IsOffscreen:
                    out.append(p)
            except Exception:
                continue
        self._popups = list(out)
        target_hwnd = getattr(target, "NativeWindowHandle", None)
        try:
            for w in auto.GetRootControl().GetChildren():
                try:
                    if w.NativeWindowHandle == target_hwnd or w.IsOffscreen:
                        continue
                    if any(w.NativeWindowHandle == p.NativeWindowHandle for p in out):
                        continue
                    if w.ControlTypeName == "MenuControl" or w.ClassName in POPUP_CLASSES:
                        if self._center(w) is not None:
                            out.append(w)
                except Exception:
                    continue
        except Exception:
            pass
        fg = self._foreground_window()
        try:
            if (fg is not None and fg.NativeWindowHandle != target_hwnd
                    and all(fg.NativeWindowHandle != p.NativeWindowHandle for p in out)
                    and self._target_pid and fg.ProcessId == self._target_pid
                    and fg.ControlTypeName in ("MenuControl", "PaneControl", "WindowControl")
                    and not (fg.Name or "").strip()):
                out.append(fg)        # unnamed same-process foreground popup
        except Exception:
            pass
        return out[:3]

    def _control_actions(self) -> list[dict]:
        ctrls: list[dict] = [
            {"id": "SCROLL_UP", "kind": "scroll", "op": "SCROLL_UP", "delta": -400, "label": "Scroll up"},
            {"id": "SCROLL_DOWN", "kind": "scroll", "op": "SCROLL_DOWN", "delta": 400, "label": "Scroll down"},
            {"id": "WAIT", "kind": "wait", "op": "WAIT", "label": "Wait for the application"},
            {"id": "LAUNCH_APP", "kind": "launch", "op": "LAUNCH_APP", "label": "Launch an application"},
            {"id": "OPEN_FILE", "kind": "open_file", "op": "OPEN_FILE",
             "label": "Find an existing file on this PC by name and open it (optionally with a given app)"},
            {"id": "SEARCH_FILES", "kind": "search_files", "op": "SEARCH_FILES",
             "label": "Search files and apps with Windows Search (taskbar search)"},
            *([{"id": "SELECT_RANGE", "kind": "select_range", "op": "SELECT_RANGE",
                 "label": "Select cells / a range / a column by address (Name Box), e.g. A1:F200 or C:C"},
               {"id": "TYPE_TABLE", "kind": "type_table", "op": "TYPE_TABLE",
                 "label": "Enter a block of values (rows and columns) into cells starting at an address"}]
              if self._is_spreadsheet() else []),
            *([{"id": "OFFICE_ACTION", "kind": "office_action", "op": "OFFICE_ACTION",
                 "label": "Do this step directly with Office automation (sort, filter, pivot, formulas, styles)"}]
              if self._office_app() else []),
            {"id": "COPY_TEXT", "kind": "copy_text", "op": "COPY_TEXT", "label": "Copy text shown in the app to the clipboard"},
            {"id": "REPORT_ANSWER", "kind": "report", "op": "REPORT_ANSWER",
             "label": "Report the final extracted answer or data from the app to the user"},
            {"id": "SAVE_AS", "kind": "save_as", "op": "SAVE_AS",
             "label": "Save the open document as a named file in a folder (one step; never overwrites)"},
            {"id": "CLOSE_WINDOW", "kind": "close_window", "op": "CLOSE_WINDOW",
             "label": "Close the active window (only windows opened for this task)"},
            {"id": "DELETE_FILE", "kind": "delete_file", "op": "DELETE_FILE",
             "label": "Move a file or folder to the Recycle Bin (the user must confirm)"},
        ]
        for i, (label, keys) in enumerate(DESKTOP_KEYS, start=1):
            ctrls.append({"id": f"KEY_{i}", "kind": "key", "label": label, "keys": list(keys)})
        current = getattr(self._target_window, "NativeWindowHandle", None)
        for w in self._top_windows()[:15]:
            try:
                if w.NativeWindowHandle == current:
                    continue
                name, exe, _ = self._window_info(w)
                ctrls.append({"id": f"WIN_{w.NativeWindowHandle}", "kind": "window",
                              "label": f"Window: {name[:70]} ({exe or '?'})", "hwnd": w.NativeWindowHandle})
                self._controls_map[f"WIN_{w.NativeWindowHandle}"] = w
            except Exception:
                continue
        return ctrls

    # ---------------------------------------------------------------- act
    @staticmethod
    def _center(ctrl: Any) -> tuple[int, int] | None:
        """Fresh on-screen centre of a control (re-read at action time, so moved windows still work)."""
        try:
            r = ctrl.BoundingRectangle
            if r.width() > 0 and r.height() > 0:
                return (r.left + r.right) // 2, (r.top + r.bottom) // 2
        except Exception:
            pass
        return None

    def _ensure_foreground(self) -> None:
        """Make sure keystrokes go to the target app — never to another window (e.g. the NewJev UI)."""
        if not self._target_window:
            return
        if self._is_foreground(self._target_window):
            return
        self._activate(self._target_window)
        if not self._is_foreground(self._target_window):
            raise RuntimeError(f"Could not bring '{self._last_window_title}' to the front; not sending input")

    def _click(self, ctrl: Any, button: str = "left", double: bool = False) -> None:
        import pyautogui
        pt = self._center(ctrl)
        if pt:
            pyautogui.moveTo(*pt, duration=0.2, tween=pyautogui.easeInOutQuad)
            if double:
                pyautogui.doubleClick(*pt)
            else:
                pyautogui.click(*pt, button=button)
            return
        # No on-screen rectangle (virtualized / collapsed list or tree item): scroll it into view first
        try:
            ctrl.GetScrollItemPattern().ScrollIntoView()
            time.sleep(0.2)
            pt = self._center(ctrl)
            if pt:
                pyautogui.moveTo(*pt, duration=0.2)
                if double:
                    pyautogui.doubleClick(*pt)
                else:
                    pyautogui.click(*pt, button=button)
                return
        except Exception:
            pass
        if button == "left" and not double:
            for pattern, method in (("GetInvokePattern", "Invoke"), ("GetSelectionItemPattern", "Select"),
                                    ("GetExpandCollapsePattern", "Expand")):
                try:
                    getattr(getattr(ctrl, pattern)(), method)()
                    return
                except Exception:
                    continue
        # uiautomation's Click() only logs when it cannot move the cursor, so fail loudly here instead
        raise ValueError(f"'{(ctrl.Name or '')[:60]}' has no on-screen position and cannot be invoked")

    @staticmethod
    def _send_chars(content: str, delay: float = 0.015) -> bool:
        """
        Type like a real keyboard: each character becomes its virtual key (+Shift) on the active keyboard layout,
        so apps see normal key presses; characters the layout has no key for are sent as Unicode input.
        Enter / Tab are real key presses (new line, next cell).
        """
        try:  # pragma: no cover - Windows only
            import ctypes
            from ctypes import wintypes

            ULONG_PTR = ctypes.c_size_t

            class KEYBDINPUT(ctypes.Structure):
                _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                            ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

            class MOUSEINPUT(ctypes.Structure):
                _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                            ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

            class _U(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

            class INPUT(ctypes.Structure):
                _fields_ = [("type", wintypes.DWORD), ("u", _U)]

            user32 = ctypes.windll.user32
            KEYEVENTF_UNICODE, KEYEVENTF_KEYUP, INPUT_KEYBOARD = 0x0004, 0x0002, 1
            VK_SHIFT, VK_RETURN, VK_TAB, VK_CAPITAL = 0x10, 0x0D, 0x09, 0x14
            user32.VkKeyScanExW.restype = ctypes.c_short
            user32.GetKeyboardLayout.restype = ctypes.c_void_p
            user32.VkKeyScanExW.argtypes = [wintypes.WCHAR, ctypes.c_void_p]
            user32.MapVirtualKeyExW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
            fg = user32.GetForegroundWindow()
            layout = user32.GetKeyboardLayout(user32.GetWindowThreadProcessId(fg, None))
            caps = bool(user32.GetKeyState(VK_CAPITAL) & 1)

            def key(vk, up=False):
                scan = user32.MapVirtualKeyExW(vk, 0, layout) & 0xFFFF
                return INPUT(INPUT_KEYBOARD, _U(ki=KEYBDINPUT(vk, scan, KEYEVENTF_KEYUP if up else 0, 0, 0)))

            def uni(u, up=False):
                flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
                return INPUT(INPUT_KEYBOARD, _U(ki=KEYBDINPUT(0, u, flags, 0, 0)))

            for ch in content.replace("\r\n", "\n"):
                if ch in ("\n", "\t"):
                    vk = VK_RETURN if ch == "\n" else VK_TAB
                    events = [key(vk), key(vk, True)]
                else:
                    res = user32.VkKeyScanExW(ch, layout) if ord(ch) <= 0xFFFF else -1
                    vk, mods = res & 0xFF, (res >> 8) & 0xFF
                    if res != -1 and vk not in (0, 0xFF) and not (mods & 0x06):      # no Ctrl/Alt (AltGr) needed
                        shift = bool(mods & 0x01)
                        if caps and ch.isalpha():
                            shift = not shift                                      # Caps Lock inverts letters
                        events = ([key(VK_SHIFT)] if shift else []) + [key(vk), key(vk, True)] + \
                                 ([key(VK_SHIFT, True)] if shift else [])
                    else:
                        code = ord(ch)
                        units = [code] if code <= 0xFFFF else [0xD800 + ((code - 0x10000) >> 10),
                                                               0xDC00 + ((code - 0x10000) & 0x3FF)]
                        events = [e for u in units for e in (uni(u), uni(u, True))]
                arr = (INPUT * len(events))(*events)
                if user32.SendInput(len(events), arr, ctypes.sizeof(INPUT)) != len(events):
                    return False
                time.sleep(delay)
            return True
        except Exception:
            return False

    @classmethod
    def _type_unicode(cls, content: str) -> bool:
        """Name kept for the spreadsheet / address helpers."""
        return cls._send_chars(content, 0.012)

    @staticmethod
    def _control_text(ctrl: Any) -> str:
        for getter in (lambda c: c.GetValuePattern().Value,
                       lambda c: c.GetTextPattern().DocumentRange.GetText(20000)):
            try:
                v = getter(ctrl)
                if v:
                    return v
            except Exception:
                continue
        return ""

    # ------------------------------------------------ keyboard-focus safety
    EDITABLE_TYPES = ("EditControl", "DocumentControl")

    def _focused_editable(self, ctrl: Any | None = None) -> bool:
        """
        Is the keyboard focus in an editable text field (and, when given, inside `ctrl`)?
        Every select-all / delete / typing step checks this first: the same keys in a FILE LIST (File Explorer)
        would select and delete files.
        """
        import uiautomation as auto
        try:
            f = auto.GetFocusedControl()
        except Exception:
            return False
        if f is None or f.ControlTypeName not in self.EDITABLE_TYPES:
            return False
        try:
            if f.GetValuePattern().IsReadOnly:
                return False
        except Exception:
            pass
        if ctrl is None:
            return True
        try:
            a, b = f.BoundingRectangle, ctrl.BoundingRectangle
            cx, cy = (a.left + a.right) // 2, (a.top + a.bottom) // 2
            inside = b.left - 2 <= cx <= b.right + 2 and b.top - 2 <= cy <= b.bottom + 2
            cx2, cy2 = (b.left + b.right) // 2, (b.top + b.bottom) // 2
            contains = a.left - 2 <= cx2 <= a.right + 2 and a.top - 2 <= cy2 <= a.bottom + 2
            return inside or contains
        except Exception:
            return True

    def _focus_field(self, ctrl: Any, action: dict | None = None) -> None:
        """Put the caret into the field, or refuse (raise) without sending a single key."""
        import pyautogui
        self._click(ctrl)
        time.sleep(0.15)
        if self._focused_editable(ctrl):
            return
        try:
            ctrl.SetFocus()
            time.sleep(0.15)
        except Exception:
            pass
        if self._focused_editable(ctrl):
            return
        label = str((action or {}).get("label", "") or getattr(ctrl, "Name", "")).lower()
        exe = (self._window_info(self._target_window)[1] if self._target_window is not None else "").lower()
        if "explorer" in exe and ("address" in label or "search" in label):
            pyautogui.hotkey("ctrl", "l" if "address" in label else "e")   # File Explorer: edit the bar
            time.sleep(0.3)
            if self._focused_editable():
                return
        raise ValueError(f"Could not put the keyboard focus into '{label[:50]}' — nothing was typed (safety: keys "
                         "sent to the wrong place could select / delete files). Try another way, e.g. OPEN_FILE.")

    def _clear_field(self, ctrl: Any) -> None:
        """Empty the focused field. Ctrl+A / Delete only while the caret is verifiably inside a text field."""
        import pyautogui
        try:
            vp = ctrl.GetValuePattern()
            if not vp.IsReadOnly and ctrl.ControlTypeName == "EditControl":
                vp.SetValue("")
                if not vp.Value:
                    return
        except Exception:
            pass
        if not self._focused_editable(ctrl):
            raise ValueError("The text field lost the keyboard focus — not clearing it (safety)")
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.05)
        if not self._focused_editable(ctrl):
            raise ValueError("The text field lost the keyboard focus — not clearing it (safety)")
        pyautogui.press("delete")
        time.sleep(0.05)

    @staticmethod
    def _readable(ctrl: Any) -> bool:
        for getter in (lambda c: c.GetValuePattern().Value, lambda c: c.GetTextPattern().DocumentRange):
            try:
                getter(ctrl)
                return True
            except Exception:
                continue
        return False

    def _enter_text(self, ctrl: Any, content: str, replace: bool = False) -> None:
        """
        Put text into the focused control, exactly once.
        TYPING_MODE=type (default): typed visibly like a person, in short chunks of words. After each chunk the
        field is read back; a chunk the app garbled (dropped / repeated keys) is erased with Backspace and typed
        again more slowly, and only pasted as a last resort. paste: pasted in one go. auto: paste long text.
        """
        from ..config import get_settings
        s = get_settings()
        mode = s.typing_mode
        if mode == "paste" or (mode == "auto" and (len(content) > 60 or "\n" in content)):
            return self._paste_verified(ctrl, content, replace)
        delay = max(0.0, s.typing_delay_ms / 1000)
        before = _canon(self._control_text(ctrl))
        readable = self._readable(ctrl) and len(before) < 19000     # very big documents can't be read back fully
        good = before
        from ..agent.takeover import check_interrupt
        for chunk in _chunks(content):
            check_interrupt()          # the person grabbed the mouse / keyboard: stop typing at once
            if not self._focused_editable():
                raise ValueError("The keyboard focus left the text field — stopped typing (safety)")
            for attempt in range(3):
                if attempt < 2:
                    if not self._send_chars(chunk, delay * (1 + 2 * attempt)):
                        self._paste_text(chunk, wait=0.6)
                else:
                    logger.info("Chunk %r keeps arriving garbled; pasting it", chunk[:30])
                    self._paste_text(chunk, wait=0.6)
                if not readable:
                    break
                time.sleep(0.12 + 0.25 * attempt)   # let a busy editor catch up before checking
                got = _canon(self._control_text(ctrl))
                if _chunk_arrived(good, got, chunk) or (
                        attempt == 2 and abs(len(got) - len(good) - len(chunk)) <= max(2, len(chunk) // 5)):
                    good = got          # (a pasted chunk only has to be roughly right: editors autocorrect)
                    break
                logger.warning("Typed chunk did not arrive cleanly (attempt %d): %r", attempt + 1, chunk[:30])
                if not self._erase_to(ctrl, good):
                    raise ValueError("Typing went wrong and the extra characters could not be removed — "
                                     "press Ctrl+Z to undo the garbled text, then try again")
            else:
                raise ValueError(f"Typed text did not appear in '{(ctrl.Name or 'the field')[:50]}'")

    def _paste_verified(self, ctrl: Any, content: str, replace: bool) -> None:
        norm = lambda x: " ".join((x or "").split())  # noqa: E731
        if not self._focused_editable():
            raise ValueError("The keyboard focus is not in a text field — nothing was pasted (safety)")
        before = _canon(self._control_text(ctrl))
        self._paste_text(content, wait=1.0)
        time.sleep(0.4)
        got = self._control_text(ctrl)
        if not got or norm(content)[:200] in norm(got):
            return
        if not self._erase_to(ctrl, "" if replace else before):
            raise ValueError("Pasted text did not arrive and could not be cleaned up — check the field")
        self._paste_text(content, wait=1.0)
        time.sleep(0.4)
        got = self._control_text(ctrl)
        if got and norm(content)[:200] not in norm(got):
            raise ValueError(f"Text did not appear in '{(ctrl.Name or 'the field')[:50]}'")

    def _erase_to(self, ctrl: Any, target: str) -> bool:
        """Remove what was just typed at the cursor (Backspace) until the field holds `target` again."""
        import pyautogui
        if not self._focused_editable():
            return False                       # never send Ctrl+A / Delete / Backspace outside a text field
        if not target:
            pyautogui.hotkey("ctrl", "a")
            if not self._focused_editable():
                return False
            pyautogui.press("delete")
            time.sleep(0.2)
            return not _canon(self._control_text(ctrl)).strip()
        for _ in range(6):
            if not self._focused_editable():
                return False
            got = _canon(self._control_text(ctrl))
            if got == target:
                return True
            extra = len(got) - len(target)
            if extra <= 0 or extra > 3000 or not got.startswith(target[: max(0, len(target) - 2)]):
                return False   # not simply extra characters at the cursor: don't guess
            pyautogui.press("backspace", presses=extra, interval=0.01)
            time.sleep(0.25)
        return _canon(self._control_text(ctrl)) == target

    @staticmethod
    def _paste_text(content: str, wait: float = 0.35) -> None:
        """Paste via clipboard (fast, unicode-safe) and restore the user's clipboard afterwards."""
        import pyautogui
        try:
            import pyperclip
        except ImportError:
            pyautogui.write(content, interval=0.01)
            return
        try:
            previous = pyperclip.paste()
        except Exception:
            previous = None
        pyperclip.copy(content)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(wait)   # restoring the clipboard too early makes slow apps (Word) paste the old content
        if previous is not None:
            try:
                pyperclip.copy(previous)
            except Exception:
                pass

    def act(self, page: dict[str, Any], action_id: str, text: str | None = None,
            operation: str | None = None) -> dict[str, Any]:
        import pyautogui
        import uiautomation as auto

        action = next((a for a in page.get("actions", []) if a["id"] == action_id), None)
        if action is None:
            raise ValueError(f"Action '{action_id}' not found")
        kind = action["kind"]
        op = (operation or "").upper()
        ctrl = self._controls_map.get(action_id)
        windows_before = {w.NativeWindowHandle for w in self._top_windows()}
        raw_before = self._raw_top_handles()
        fg_before = getattr(self._foreground_window(), "NativeWindowHandle", None)

        if kind == "wait":
            time.sleep(0.8)

        elif kind == "scroll":
            self._ensure_foreground()
            pt = self._center(self._target_window) if self._target_window else None
            if pt:
                pyautogui.moveTo(*pt, duration=0.1)
            pyautogui.scroll(-int(action.get("delta", 400)))

        elif kind == "launch":
            if not text:
                raise ValueError("LAUNCH_APP requires an application name")
            if self._target_window is not None and len(text.split(None, 1)) == 1:  # "notepad <file>" is fine
                name, exe, cls = self._window_info(self._target_window)
                if window_matches(canonical_app(text), name, exe, cls):
                    raise ValueError(f"'{text}' is already open and is the active window — do not launch it again")
            if not self.launch_app(text):
                raise ValueError(f"Could not launch or find '{text}'")

        elif kind == "open_file":
            if not text:
                raise ValueError("OPEN_FILE needs a file name or path")
            return self.open_file(text)

        elif kind == "search_files":
            if not text:
                raise ValueError("SEARCH_FILES needs a search query")
            self.windows_search(text)
            return {"executed": action_id}

        elif kind == "type_table":
            if not text:
                raise ValueError("TYPE_TABLE needs the values to enter")
            self._ensure_foreground()
            return {"executed": action_id, "note": self.type_table(text)}

        elif kind == "select_range":
            if not text:
                raise ValueError("SELECT_RANGE needs an address like A1:F200")
            self._ensure_foreground()
            return {"executed": action_id, "note": self.select_range(text)}

        elif kind == "office_action":
            from .office import run_office_code
            app = self._office_app()
            if not app or not text:
                raise ValueError("OFFICE_ACTION needs an open Excel/Word window and code")
            return {"executed": action_id, "note": run_office_code(app, text)}

        elif kind == "copy_text":
            if not text:
                raise ValueError("COPY_TEXT needs the text to copy")
            shown = " ".join(str(page.get("text", "")).split()).lower()
            if " ".join(text.split()).lower() not in shown:
                raise ValueError(f"'{text[:80]}' is not shown in this window — copy text exactly as it appears")
            import pyperclip
            pyperclip.copy(text)
            return {"executed": action_id, "note": f"Copied '{text[:120]}' to the clipboard."}

        elif kind == "close_window":
            title = self.close_window()
            note = f"Closed '{title}'."
            # a 'Save changes?' prompt keeps the app open: follow it
            time.sleep(0.4)
            fg = self._foreground_window()
            if fg is not None and self._target_pid and getattr(fg, "ProcessId", None) == self._target_pid:
                self._target_window = fg
                self._last_window_title = fg.Name or ""
                note = f"Closing '{title}' asked a question: '{fg.Name}'. Answer it (usually Save)."
            return {"executed": action_id, "note": note}

        elif kind == "visual":
            # VISUAL_CLICK: the loop located the target on a screenshot; text = "x,y[,left|right|double]" (screen px)
            parts = [p.strip() for p in (text or "").split(",")]
            if len(parts) < 2:
                raise ValueError("VISUAL_CLICK needs screen coordinates")
            self.click_at(int(float(parts[0])), int(float(parts[1])), parts[2] if len(parts) > 2 else "left")
            time.sleep(0.4)
            return {"executed": action_id}

        elif kind == "type_focused":
            if not text:
                raise ValueError("TYPE_FOCUSED needs text")
            self._ensure_foreground()
            from ..config import get_settings
            if get_settings().typing_mode == "paste" or not self._send_chars(
                    text, max(0.0, get_settings().typing_delay_ms / 1000)):
                self._paste_text(text, wait=1.0)
            return {"executed": action_id}

        elif kind == "save_as":
            if not text:
                raise ValueError("SAVE_AS needs the full path to save to")
            return {"executed": action_id, "note": self.save_as(text)}

        elif kind == "delete_file":
            from .files import recycle
            target = Path(action.get("resolved_path") or "")
            if not action.get("resolved_path"):
                raise ValueError("DELETE_FILE needs a resolved path")
            recycle(target)
            return {"executed": action_id, "note": f"Moved '{target}' to the Recycle Bin"}

        elif kind == "window":
            if ctrl is None:
                raise ValueError("Window is gone")
            self._activate(ctrl)

        elif kind == "key":
            self._ensure_foreground()
            keys = [k.lower() for k in action["keys"]]
            if self._destructive_keys(keys) and not self._focused_editable():
                raise ValueError(f"Refused to press {'+'.join(keys)} outside a text field: in File Explorer and "
                                 "other lists it deletes the selected items. Use DELETE_FILE (the user confirms).")
            pyautogui.hotkey(*action["keys"])
            time.sleep(0.25)

        elif kind == "click":
            if ctrl is None:
                raise ValueError(f"Control for '{action_id}' is gone")
            if re.search(r"\b(delete|permanently delete)\b", str(action.get("label", "")), re.I) and \
                    "explorer" in (self._window_info(self._target_window)[1] if self._target_window else "").lower():
                raise ValueError("Deleting from File Explorer menus is disabled (the selection could hold many "
                                 "files). Use DELETE_FILE with the exact file — the user confirms it.")
            if action_id not in getattr(self, "_menu_ids", set()):   # re-focusing the app would close an open menu
                self._ensure_foreground()
            pt = self._center(ctrl)
            if op == "DRAG":
                return self._drag(page, action, ctrl, text)
            self._click(ctrl, button="right" if op == "RIGHT_CLICK" else "left", double=op == "DOUBLE_CLICK")
            if op == "RIGHT_CLICK":
                time.sleep(0.6)
                found = self._detect_new_popups(raw_before, pt)
                if not found:
                    return {"executed": action_id,
                            "note": "No context menu could be read after the right-click. Use another way: CLICK the "
                                    "item then PRESS_KEY F2 to rename, Delete to delete, or Shift+F10 for the menu."}
                return {"executed": action_id, "note": f"Context menu opened with {found} items (marked [menu])."}

        elif kind == "fill":
            if ctrl is None:
                raise ValueError(f"Field for '{action_id}' is gone")
            self._ensure_foreground()
            content = text or ""
            if op == "APPEND_TEXT":
                self._focus_field(ctrl, action)
                pyautogui.hotkey("ctrl", "end")
                self._enter_text(ctrl, content, replace=False)
            else:
                if op == "TYPE_SUBMIT" and action.get("multiline"):
                    raise ValueError("TYPE_SUBMIT is for single-line fields; use TYPE_TEXT in a text area")
                # Real typing first: setting the value directly (ValuePattern) changes what the box shows, but
                # many dialogs never notice — e.g. Save As then saves under its own default name.
                self._focus_field(ctrl, action)   # SAFETY: no key is sent unless the caret is in this field
                self._clear_field(ctrl)
                try:
                    self._enter_text(ctrl, content, replace=True)
                except ValueError:
                    if action.get("multiline"):
                        raise
                    vp = ctrl.GetValuePattern()      # last resort for fields that ignore typed keys
                    vp.SetValue(content)
                    if vp.Value != content:
                        raise
                if op == "TYPE_SUBMIT":
                    time.sleep(0.1)
                    pyautogui.press("enter")
                    time.sleep(0.4)

        elif kind == "select":
            if ctrl is None:
                raise ValueError("Combo box is gone")
            self._ensure_foreground()
            value = str(action.get("value", ""))
            selected = False
            try:
                selected = bool(ctrl.Select(value))
            except Exception:
                selected = False
            if not selected:
                self._click(ctrl)
                time.sleep(0.4)
                item = auto.ListItemControl(searchDepth=12, Name=value)
                if item.Exists(1.0):
                    self._click(item)
                else:
                    raise ValueError(f"Option '{value}' not found")

        else:
            raise ValueError(f"Unsupported action kind '{kind}'")

        time.sleep(0.35)
        self._refuse_unexpected_delete_dialog(op)
        # Follow dialogs / new windows the action opened (Save As, new document ...)
        try:
            top = self._foreground_window()
            if (top and not is_own_ui(top.Name) and (top.Name or "") not in EXCLUDED_WINDOW_NAMES
                    and (self._target_window is None or top.NativeWindowHandle != self._target_window.NativeWindowHandle)):
                same_app = self._target_pid is not None and top.ProcessId == self._target_pid
                opened_now = top.NativeWindowHandle not in windows_before
                # Opening an item (double-click / Enter) can bring an EXISTING window forward, e.g. the new
                # Notepad adds a tab to its open window: follow it when this action changed the foreground.
                opened_item = (op == "DOUBLE_CLICK" or (kind == "key" and "enter" in str(action.get("label", "")).lower())) \
                    and top.NativeWindowHandle != fg_before
                # Follow only the app's own dialogs/windows or a window this action opened —
                # never an unrelated app that just happened to get focus.
                if same_app or opened_now or opened_item or kind in ("window", "launch"):
                    if opened_now:
                        self._mark_opened(top)
                    self._target_window = top
                    self._last_window_title = top.Name
                    try:
                        self._target_pid = top.ProcessId
                    except Exception:
                        pass
        except Exception:
            pass
        return {"executed": action_id}

    # --------------------------------------------------------------- misc
    def _grab(self):
        from PIL import ImageGrab
        bbox = None
        try:
            r = self._target_window.BoundingRectangle
            if r.width() > 0 and r.height() > 0:
                bbox = (r.left, r.top, r.right, r.bottom)
        except Exception:
            pass
        return ImageGrab.grab(bbox=bbox, all_screens=True)

    def vision_shot(self, max_width: int = 1280) -> dict[str, Any]:
        """Screenshot of the target window for the vision fallback, with the mapping back to screen pixels."""
        import base64
        import io
        left = top = 0
        try:
            r = self._target_window.BoundingRectangle
            if r.width() > 0 and r.height() > 0:
                left, top = r.left, r.top
        except Exception:
            pass
        img = self._grab().convert("RGB")
        if left == 0 and top == 0:
            try:
                import ctypes
                left = ctypes.windll.user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN (all_screens grab origin)
                top = ctypes.windll.user32.GetSystemMetrics(77)
            except Exception:
                pass
        scale = min(1.0, max_width / max(1, img.width))
        if scale < 1.0:
            img = img.resize((int(img.width * scale), int(img.height * scale)))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return {"b64": base64.b64encode(buf.getvalue()).decode(), "width": img.width, "height": img.height,
                "left": left, "top": top, "scale": scale}

    def click_at(self, x: int, y: int, button: str = "left") -> None:
        import pyautogui
        self._ensure_foreground()
        pyautogui.moveTo(x, y, duration=0.2)
        if button == "double":
            pyautogui.doubleClick(x, y)
        else:
            pyautogui.click(x, y, button="right" if button == "right" else "left")

    def _take_screenshot(self) -> str:
        try:
            import base64
            import io
            buf = io.BytesIO()
            self._grab().convert("RGB").save(buf, format="JPEG", quality=72)
            return base64.b64encode(buf.getvalue()).decode()
        except Exception:
            return ""

    def screenshot(self, path: str) -> None:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._grab().save(path)
        except Exception as exc:
            logger.warning("Screenshot error: %s", exc)

    def close(self) -> None:
        self._controls_map.clear()

    def __enter__(self) -> "DesktopCapture":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
