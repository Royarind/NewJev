"""
Per-run usage accounting: calls, tokens, time and cost for every model (LLMs and Jev).

A tracker is activated for the current thread / asyncio task with `tracker.activate()`; every LLM call
made through FallbackLLM (and every Jev request) is recorded into it. `summary()` is what ends up in the
run result, the trace file and the UI result card.
"""

from __future__ import annotations

import contextvars
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

# USD per 1M tokens (input, output). Used only when the provider does not report the cost itself
# (OpenRouter reports the real cost per call). Extend with LLM_PRICES in .env: "model=in:out,model2=in:out".
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-4o-mini": (0.15, 0.60),
    "gpt-4o-mini": (0.15, 0.60),
    "openai/gpt-4o": (2.50, 10.0),
    "gpt-4o": (2.50, 10.0),
}

_current: contextvars.ContextVar["UsageTracker | None"] = contextvars.ContextVar("newjev_usage", default=None)


def parse_prices(spec: str) -> dict[str, tuple[float, float]]:
    prices: dict[str, tuple[float, float]] = {}
    for part in (spec or "").split(","):
        if "=" not in part:
            continue
        model, _, cost = part.strip().rpartition("=")
        try:
            inp, _, out = cost.partition(":")
            prices[model.strip()] = (float(inp), float(out or inp))
        except ValueError:
            continue
    return prices


def price_for(model: str, prompt_tokens: int, completion_tokens: int, extra: dict | None = None) -> float | None:
    table = {**DEFAULT_PRICES, **(extra or {})}
    key = model if model in table else model.split("/", 1)[-1] if model.split("/", 1)[-1] in table else None
    if key is None:
        if model.endswith(":free"):
            return 0.0
        return None
    inp, out = table[key]
    return (prompt_tokens * inp + completion_tokens * out) / 1_000_000


@dataclass
class UsageTracker:
    records: list[dict[str, Any]] = field(default_factory=list)
    started: float = field(default_factory=time.perf_counter)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, role: str, model: str, *, prompt_tokens: int = 0, completion_tokens: int = 0,
            latency_ms: int = 0, cost: float | None = None, ok: bool = True, error: str = "",
            fallback_from: str = "") -> None:
        rec = {"role": role, "model": model, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
               "latency_ms": latency_ms, "cost": cost, "ok": ok, "error": error[:200], "ts": time.time()}
        if fallback_from:
            rec["fallback_from"] = fallback_from
        with self._lock:
            self.records.append(rec)

    def summary(self) -> dict[str, Any]:
        per: dict[str, dict[str, Any]] = {}
        with self._lock:
            recs = list(self.records)
        for r in recs:
            key = f"{r['role']}:{r['model']}"
            m = per.setdefault(key, {"role": r["role"], "model": r["model"], "calls": 0, "errors": 0,
                                     "prompt_tokens": 0, "completion_tokens": 0, "time_ms": 0,
                                     "cost_usd": 0.0, "cost_known": True})
            m["calls"] += 1
            m["errors"] += 0 if r["ok"] else 1
            m["prompt_tokens"] += r["prompt_tokens"]
            m["completion_tokens"] += r["completion_tokens"]
            m["time_ms"] += r["latency_ms"]
            if r["cost"] is None:
                if r["ok"]:
                    m["cost_known"] = False
            else:
                m["cost_usd"] += r["cost"]
        models = sorted(per.values(), key=lambda m: -m["time_ms"])
        for m in models:
            m["cost_usd"] = round(m["cost_usd"], 6)
        return {
            "models": models,
            "total_calls": sum(m["calls"] for m in models),
            "total_errors": sum(m["errors"] for m in models),
            "fallbacks": sum(1 for r in recs if r.get("fallback_from")),
            "model_time_ms": sum(m["time_ms"] for m in models),
            "cost_usd": round(sum(m["cost_usd"] for m in models), 6),
            "cost_complete": all(m["cost_known"] for m in models),
        }

    @contextmanager
    def activate(self) -> Iterator["UsageTracker"]:
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)


def current() -> UsageTracker | None:
    return _current.get()


def record(role: str, model: str, **kw: Any) -> None:
    t = _current.get()
    if t is not None:
        t.add(role, model, **kw)
