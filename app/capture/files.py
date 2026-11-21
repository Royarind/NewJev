"""File finding / opening / recycling helpers for the desktop agent (the 'R', 'U' and 'D' of file CRUD)."""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

FILE_EXTS = ("txt|md|csv|tsv|json|log|xml|ini|cfg|yaml|yml|rtf|docx?|xlsx?|xlsm|pptx?|pdf|odt|ods|odp|"
             "png|jpe?g|gif|bmp|svg|webp|mp3|wav|mp4|mov|avi|zip|7z|rar|py|js|ts|html?|css|bat|ps1|exe|msi")
_QUOTED = re.compile(rf"[\"'“‘]([^\"'”’]+?\.(?:{FILE_EXTS}))[\"'”’]", re.I)
_BARE = re.compile(rf"(?<![\w.\\/])([\w\-.()]+\.(?:{FILE_EXTS}))\b", re.I)
_PATH = re.compile(rf"((?:[A-Za-z]:\\|~[\\/])[^\"'<>|?*\n]+?\.(?:{FILE_EXTS}))\b", re.I)

FOLDER_WORDS = {"desktop": "Desktop", "documents": "Documents", "my documents": "Documents", "downloads": "Downloads",
                "pictures": "Pictures", "photos": "Pictures", "music": "Music", "videos": "Videos"}
SKIP_DIRS = {"appdata", "node_modules", ".git", "__pycache__", ".venv", "venv", "$recycle.bin", "windows",
             "program files", "program files (x86)", "programdata"}
CREATE_HINT = re.compile(r"\b(save (it |this |the file |the document )?as|name it|call it|create (a |the )?(new )?file|"
                         r"new file|save (it )?on|save (it )?to)\b", re.I)


def _known() -> dict[str, str]:
    from .desktop import known_folders
    return known_folders()


def referenced_files(text: str) -> list[str]:
    """File names / paths mentioned in a goal ("open 'Q3 report.docx'", "NewJev_test1.txt", "C:\\x\\a.csv")."""
    found: list[str] = []
    for rx in (_PATH, _QUOTED, _BARE):
        for m in rx.finditer(text or ""):
            v = m.group(1).strip()
            if v and not any(v in f or f.endswith(v) for f in found):
                found.append(v)
    return found


def folder_hint(text: str) -> str | None:
    """Known folder named in the text ('on the Desktop', 'in Downloads') → its real path."""
    low = (text or "").lower()
    for word, name in FOLDER_WORDS.items():
        if re.search(rf"\b{word}\b", low):
            return _known().get(name)
    return None


def _walk(root: Path, max_depth: int):
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            entries = list(os.scandir(d))
        except (PermissionError, FileNotFoundError, NotADirectoryError, OSError):
            continue
        for e in entries:
            yield e
            try:
                if e.is_dir(follow_symlinks=False) and depth < max_depth and e.name.lower() not in SKIP_DIRS \
                        and not e.name.startswith("."):
                    stack.append((Path(e.path), depth + 1))
            except OSError:
                continue


def find_files(name: str, hint_dir: str | None = None, roots: list[str] | None = None,
               max_depth: int = 4, limit: int = 10, include_dirs: bool = False) -> list[Path]:
    """
    Find files (optionally folders) by name, path, or wildcard. Searches the hinted folder first, then
    Desktop / Documents / Downloads / Pictures … (OneDrive-aware). Exact name matches beat partial ones;
    newer files first.
    """
    from .desktop import expand_known

    name = (name or "").strip().strip("\"'")
    if not name:
        return []
    direct = Path(expand_known(name))
    if direct.is_absolute() and direct.exists():
        return [direct]

    base = Path(name).name
    pattern = base.lower() if any(c in base for c in "*?[") else None
    stem = Path(base).stem.lower()
    search_roots: list[str] = []
    for r in ([hint_dir] if hint_dir else []) + (roots if roots is not None else list(_known().values())):
        if r and r not in search_roots and os.path.isdir(r):
            search_roots.append(r)

    exact: list[Path] = []
    partial: list[Path] = []
    seen: set[str] = set()
    for root in search_roots:
        for e in _walk(Path(root), max_depth):
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir and not include_dirs:
                continue
            low = e.name.lower()
            key = os.path.normcase(e.path)
            if key in seen:
                continue
            if (pattern and fnmatch.fnmatch(low, pattern)) or low == base.lower():
                exact.append(Path(e.path))
                seen.add(key)
            elif not pattern and len(stem) >= 3 and stem in low:
                partial.append(Path(e.path))
                seen.add(key)
        if len(exact) >= limit:
            break

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    exact.sort(key=mtime, reverse=True)
    partial.sort(key=mtime, reverse=True)
    return (exact or partial)[:limit]


def recent_files(name: str, hint_dir: str | None = None, limit: int = 5) -> list[str]:
    """The most recently changed files near where `name` was expected (helps when a file was saved
    under another name, e.g. an editor's default 'line one.txt')."""
    import time as _time
    from .desktop import expand_known
    p = Path(expand_known((name or "").strip().strip("\"'")))
    folders = [str(p.parent)] if p.is_absolute() and p.parent.is_dir() else []
    folders += [d for d in [hint_dir, _known().get("Desktop"), _known().get("Documents")] if d]
    ext = p.suffix.lower()
    found: list[tuple[float, Path]] = []
    seen: set[str] = set()
    for d in folders:
        if not os.path.isdir(d) or os.path.normcase(d) in seen:
            continue
        seen.add(os.path.normcase(d))
        try:
            for e in os.scandir(d):
                if e.is_file() and (not ext or e.name.lower().endswith(ext)):
                    found.append((e.stat().st_mtime, Path(e.path)))
        except OSError:
            continue
    found.sort(reverse=True)
    now = _time.time()
    out = []
    for mt, fp in found[:limit]:
        mins = max(0, int((now - mt) // 60))
        out.append(f"{fp} ({'just now' if mins < 1 else f'{mins} min ago' if mins < 120 else 'earlier'})")
    return out


def existing_file_for_task(task: str) -> Path | None:
    """If the task refers to a file that already exists (and is not asking to create it), return it."""
    names = referenced_files(task)
    if not names:
        return None
    hint = folder_hint(task)
    for n in names:
        hits = find_files(n, hint_dir=hint, max_depth=3, limit=2)
        exact = [h for h in hits if h.name.lower() == Path(n).name.lower() or h.is_absolute() and str(h) == n]
        if exact:
            # "type X and save it as NewJev.txt" mentions a file it is about to (re)create
            m = CREATE_HINT.search(task)
            if m and task.lower().find(Path(n).name.lower()) > m.start():
                continue
            return exact[0]
    return None


def is_protected(path: Path) -> bool:
    """Never recycle drive roots, the home folder or the known folders themselves."""
    try:
        p = path.resolve()
    except OSError:
        return True
    protected = {Path.home().resolve(), *[Path(v).resolve() for v in _known().values()]}
    return p in protected or p.parent == p or len(p.parts) <= 2


def recycle(path: Path) -> None:
    """Move a file/folder to the Recycle Bin (undoable). Never hard-deletes."""
    if is_protected(path):
        raise ValueError(f"Refusing to delete protected location: {path}")
    if not path.exists():
        raise FileNotFoundError(str(path))
    if sys.platform != "win32":
        raise RuntimeError("Recycle Bin deletion is only supported on Windows")
    import ctypes  # pragma: no cover - Windows only
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(None, FO_DELETE, str(path) + "\0", None,
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or op.fAnyOperationsAborted or path.exists():
        raise RuntimeError(f"Could not move '{path}' to the Recycle Bin (code {rc})")


def open_path(path: Path, app: str | None = None) -> None:
    """Open with its default app (or a named app via the desktop launcher)."""
    if sys.platform == "win32":  # pragma: no cover
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])
