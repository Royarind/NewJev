"""Test setup: no network, no traces, and stub SDKs so tests run without API keys."""

import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SAVE_TRACES", "false")
os.environ.setdefault("SCREENSHOT_ON_ERROR", "false")
os.environ.setdefault("STEP_DELAY_MS", "0")
os.environ.setdefault("PROCEDURE_MODE", "off")
os.environ.setdefault("OFFICE_AUTOMATION", "false")
os.environ.setdefault("RECIPES_ENABLED", "false")
os.environ.setdefault("RESCUE_ENABLED", "false")
os.environ.setdefault("VISION_FALLBACK", "false")
os.environ.setdefault("SHORTCUTS_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("TAKEOVER_PAUSE", "false")
os.environ.setdefault("PLAN_RECIPES", "false")
os.environ.setdefault("HOTKEY_ENABLED", "false")
import tempfile  # noqa: E402
_TMP = tempfile.mkdtemp(prefix="newjev_test_")
os.environ.setdefault("CONNECTIONS_PATH", os.path.join(_TMP, "connections"))
os.environ.setdefault("GRAPH_TOKEN_PATH", os.path.join(_TMP, "graph_token.json"))

for name, classes in {"openai": ("OpenAI", "AsyncOpenAI"), "anthropic": ("Anthropic", "AsyncAnthropic")}.items():
    try:
        __import__(name)
    except ImportError:  # pragma: no cover
        mod = types.ModuleType(name)
        for cls in classes:
            setattr(mod, cls, type(cls, (), {"__init__": lambda self, *a, **k: None}))
        sys.modules[name] = mod
