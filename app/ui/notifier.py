"""
Desktop pop-up for requests that need the user (confirmations, hand-offs, take-over, hints).

It slides up from the bottom-right corner above the taskbar, stays on top, and answers the request directly
(POST /api/respond), so the user never has to switch back to the NewJev browser tab. It closes itself as soon as
the request is answered anywhere (here or in the web UI) or expires.

Run by the server as a separate process:  python -m app.ui.notifier <base64 json payload>
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import time
import urllib.request

BG, FG, MUTED, BORDER = "#161b22", "#e6edf3", "#8b949e", "#d29922"
BTN = {"primary": ("#238636", "#ffffff"), "blue": ("#1f6feb", "#ffffff"), "plain": ("#30363d", "#e6edf3"),
       "danger": ("#da3633", "#ffffff")}
WIDTH = 400


def _post(base: str, path: str, data: dict) -> bool:
    try:
        req = urllib.request.Request(base + path, data=json.dumps(data).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def _pending(base: str, rid: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/api/requests/{rid}", timeout=3) as r:
            return bool(json.loads(r.read()).get("pending"))
    except Exception:
        return False


def _work_area() -> tuple[int, int, int, int]:
    """Screen area without the taskbar (left, top, right, bottom)."""
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)   # SPI_GETWORKAREA
        return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        return 0, 0, 1366, 728


def show(p: dict) -> None:
    import tkinter as tk

    base, rid = p["base"], p["request_id"]
    root = tk.Tk()
    root.title("NewJev Agent - needs you")          # "NewJev Agent" = the agent's own window (no take-over)
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=BORDER)
    try:
        root.attributes("-alpha", 0.98)
    except Exception:
        pass

    frame = tk.Frame(root, bg=BG, padx=14, pady=12)
    frame.pack(fill="both", expand=True, padx=1, pady=1)
    head = tk.Frame(frame, bg=BG)
    head.pack(fill="x")
    tk.Label(head, text="⚡ NewJev", fg="#58a6ff", bg=BG, font=("Segoe UI", 9, "bold")).pack(side="left")
    closer = tk.Label(head, text="✕", fg=MUTED, bg=BG, font=("Segoe UI", 10), cursor="hand2")
    closer.pack(side="right")
    tk.Label(frame, text=p.get("title", "NewJev needs you"), fg=FG, bg=BG, font=("Segoe UI", 11, "bold"),
             anchor="w", justify="left", wraplength=WIDTH - 30).pack(fill="x", pady=(6, 2))
    tk.Label(frame, text=p.get("message", "")[:700], fg=FG, bg=BG, font=("Segoe UI", 9), anchor="w",
             justify="left", wraplength=WIDTH - 30).pack(fill="x")

    entry = None
    if p.get("hint"):
        entry = tk.Text(frame, height=2, bg="#0d1117", fg=FG, insertbackground=FG, relief="flat",
                        font=("Segoe UI", 9), wrap="word", highlightthickness=1, highlightbackground="#30363d")
        entry.pack(fill="x", pady=(8, 0))
        entry.focus_set()
    status = tk.Label(frame, text="", fg=MUTED, bg=BG, font=("Segoe UI", 8), anchor="w")
    bar = tk.Frame(frame, bg=BG)
    bar.pack(fill="x", pady=(10, 0))
    done = {"v": False}

    def close() -> None:
        if not done["v"]:
            done["v"] = True
            slide(out=True)

    def answer(btn: dict) -> None:
        text = btn.get("text")
        if btn.get("use_entry") and entry is not None:
            text = entry.get("1.0", "end").strip()
            if not text:
                status.configure(text="Type a hint first, or choose another option.")
                status.pack(fill="x", pady=(6, 0))
                return
        ok = _post(base, "/api/respond", {"request_id": rid, "approved": bool(btn.get("approved")),
                                          "remember": bool(btn.get("remember")), "text": text})
        if not ok:
            status.configure(text="NewJev did not accept the answer (already answered or closed).")
            status.pack(fill="x", pady=(6, 0))
            root.after(1500, close)
            return
        close()

    for b in p.get("buttons", []):
        bg, fg = BTN.get(b.get("style", "plain"), BTN["plain"])
        w = tk.Button(bar, text=b["label"], bg=bg, fg=fg, activebackground=bg, activeforeground=fg, relief="flat",
                      font=("Segoe UI", 9, "bold"), padx=10, pady=4, cursor="hand2",
                      command=lambda b=b: answer(b))
        w.pack(side="left", padx=(0, 6))
    tk.Label(frame, text="You can also answer in the NewJev window.", fg=MUTED, bg=BG,
             font=("Segoe UI", 8), anchor="w").pack(fill="x", pady=(8, 0))
    closer.bind("<Button-1>", lambda _e: close())

    root.update_idletasks()
    h = root.winfo_reqheight()
    left, top, right, bottom = _work_area()
    x, y_end = right - WIDTH - 16, bottom - h - 16
    pos = {"y": bottom}

    def slide(out: bool = False) -> None:
        target = bottom + 10 if out else y_end
        step = 28 if out else -28
        pos["y"] = pos["y"] + step
        if (not out and pos["y"] <= target) or (out and pos["y"] >= target):
            pos["y"] = target
        root.geometry(f"{WIDTH}x{h}+{x}+{pos['y']}")
        if pos["y"] != target:
            root.after(12, lambda: slide(out))
        elif out:
            root.destroy()

    root.geometry(f"{WIDTH}x{h}+{x}+{bottom}")
    slide()
    try:
        import winsound
        winsound.MessageBeep(0x40)       # MB_ICONASTERISK: the normal Windows notification sound
    except Exception:
        pass

    deadline = time.time() + float(p.get("timeout_s", 600))

    def watch() -> None:            # close when answered elsewhere / expired
        while not done["v"]:
            time.sleep(1.5)
            if time.time() > deadline or not _pending(base, rid):
                try:
                    root.after(0, close)
                except Exception:
                    pass
                return

    threading.Thread(target=watch, daemon=True).start()
    root.mainloop()


def show_in_browser(p: dict, raw: str) -> None:
    """Fallback when tkinter is missing: the same pop-up as a small Edge / Chrome app window, bottom-right."""
    import subprocess
    import urllib.parse
    from .hotkey import _browser
    left, top, right, bottom = _work_area()
    w, h = WIDTH + 20, 250 + (60 if p.get("hint") else 0)
    url = f"{p['base']}/popup?p={urllib.parse.quote(raw)}"
    exe = _browser()
    if exe:
        subprocess.Popen([exe, f"--app={url}", f"--window-size={w},{h}",
                          f"--window-position={right - w - 12},{bottom - h - 12}"])
    else:
        import webbrowser
        webbrowser.open(url)


def main() -> None:
    raw = sys.argv[1]
    payload = json.loads(base64.b64decode(raw).decode("utf-8"))
    try:
        import tkinter  # noqa: F401
    except ImportError:
        show_in_browser(payload, raw)
        return
    show(payload)


if __name__ == "__main__":
    main()
