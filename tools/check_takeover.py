"""
Check that NewJev notices when YOU use the mouse / keyboard (the take-over pause):
    .venv\\Scripts\\python tools\\check_takeover.py
Click and type anywhere for 15 seconds; every detected input is printed.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.takeover import InputWatcher  # noqa: E402

w = InputWatcher()
w.start()
print(f"Detection mode: {w.mode}  (hooks = best, polling = fallback, off = not Windows)")
print("Click / type anywhere outside this window for 15 seconds...")
seen, end = 0.0, time.time() + 15
while time.time() < end:
    if w.last_human > seen:
        seen = w.last_human
        print(f"  detected: {w.last_what}")
    time.sleep(0.1)
print(f"Done. Events seen by the hooks: {w.events_total}. "
      + ("Working." if w.last_human else "Nothing detected — please send me this output."))
