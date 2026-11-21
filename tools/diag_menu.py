"""
Diagnostic: shows how Windows exposes a right-click menu on this PC.
Run:  .venv\\Scripts\\python tools\\diag_menu.py
Then, within 5 seconds, right-click a file/folder in File Explorer and leave the menu open.
Paste the output back to the developer.
"""
import ctypes
import time

import uiautomation as auto

print("Right-click something now and keep the menu open...")
before = {w.NativeWindowHandle for w in auto.GetRootControl().GetChildren()}
time.sleep(5)
fg = ctypes.windll.user32.GetForegroundWindow()
print("\nForeground window:", fg)
print("\nNew top-level windows since start:")
for w in auto.GetRootControl().GetChildren():
    tag = "NEW " if w.NativeWindowHandle not in before else "    "
    print(f"{tag}{w.ControlTypeName:<16} class={w.ClassName!r:<45} name={w.Name!r:<40} hwnd={w.NativeWindowHandle}")
x, y = auto.GetCursorPos()
print(f"\nUnder/near the cursor ({x},{y}):")
for dx, dy in ((0, 0), (25, 25), (60, 40)):
    c = auto.ControlFromPoint(x + dx, y + dy)
    if c:
        top = c.GetTopLevelControl()
        print(f"  +{dx},{dy}: {c.ControlTypeName} {c.Name!r} | top: {top.ControlTypeName if top else None} "
              f"class={top.ClassName if top else None!r} name={top.Name if top else None!r}")
print("\nMenu items found anywhere (depth 6 under root):")
for c, d in auto.WalkControl(auto.GetRootControl(), maxDepth=6):
    if c.ControlTypeName == "MenuItemControl" and not c.IsOffscreen:
        print("  ", c.Name, "| parent:", c.GetParentControl().ControlTypeName, "| top class:",
              c.GetTopLevelControl().ClassName if c.GetTopLevelControl() else None)
