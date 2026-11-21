"""
FallbackLLM: one logical LLM backed by an ordered list of models.

- Rate limits (429): honour Retry-After when it is short, otherwise put the model on cool-down and use the next.
- Timeouts, connection errors, 5xx, 401/403/404 (bad key / unknown model): switch to the next model.
- 400 (bad request) is the prompt's fault, not the model's: raised immediately.
- The SDKs' own hidden retries are disabled (max_retries=0) so a slow provider can't stall a run silently.
Every attempt is recorded in the active UsageTracker (time, tokens, cost, errors, which fallback was used).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from .base import BaseLLM, LLMResponse
from . import usage

logger = logging.getLogger(__name__)

# model label -> monotonic time until which it is skipped (shared by every FallbackLLM in the process)
_cooldown: dict[str, float] = {}
_cool_lock = threading.Lock()


class ModelUnavailable(Exception):
    """Every model of a role failed."""


def status_of(exc: Exception) -> int | None:
    for attr in ("status_code", "status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    v = getattr(resp, "status_code", None)
    return v if isinstance(v, int) else None


def retry_after_s(exc: Exception) -> float | None:
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) or {}
    try:
        if headers.get("retry-after-ms"):
            return float(headers["retry-after-ms"]) / 1000
        if headers.get("retry-after"):
            return float(headers["retry-after"])
        if headers.get("x-ratelimit-reset-requests"):
            v = str(headers["x-ratelimit-reset-requests"]).rstrip("s")
            return float(v)
    except (TypeError, ValueError):
        return None
    return None


def classify(exc: Exception) -> str:
    """'rate' | 'transient' | 'config' | 'fatal'."""
    name = type(exc).__name__.lower()
    code = status_of(exc)
    if code == 429 or "ratelimit" in name:
        return "rate"
    if code in (401, 403, 404) or "authentication" in name or "permissiondenied" in name or "notfound" in name:
        return "config"
    if code == 400 or "badrequest" in name:
        return "fatal"
    if code is not None and code >= 500:
        return "transient"
    if any(k in name for k in ("timeout", "connection", "internalserver", "apierror", "serviceunavailable")):
        return "transient"
    return "transient"


def _cost_from(resp: LLMResponse, prices: dict) -> float | None:
    raw_usage = (resp.raw or {}).get("usage") or {}
    if isinstance(raw_usage, dict) and raw_usage.get("cost") is not None:
        try:
            return float(raw_usage["cost"])
        except (TypeError, ValueError):
            pass
    return usage.price_for(resp.model or "", resp.prompt_tokens, resp.completion_tokens, prices)


class FallbackLLM(BaseLLM):
    def __init__(self, role: str, chain: list[tuple[str, BaseLLM]], retry_after_max_s: float = 8.0,
                 cooldown_s: float = 60.0, prices: dict | None = None, sleep=time.sleep) -> None:
        if not chain:
            raise ValueError("FallbackLLM needs at least one model")
        self.role = role
        self.chain = chain
        self.retry_after_max_s = retry_after_max_s
        self.cooldown_s = cooldown_s
        self.prices = prices or {}
        self._sleep = sleep
        self.last_model: str = chain[0][0]

    @property
    def model(self) -> str:
        return self.chain[0][0]

    def _order(self) -> list[tuple[str, BaseLLM]]:
        now = time.monotonic()
        with _cool_lock:
            ready = [c for c in self.chain if _cooldown.get(c[0], 0) <= now]
            cooling = sorted((c for c in self.chain if _cooldown.get(c[0], 0) > now),
                             key=lambda c: _cooldown.get(c[0], 0))
        return ready + cooling   # cooled-down models are tried last rather than never

    @staticmethod
    def _cool(label: str, seconds: float) -> None:
        with _cool_lock:
            _cooldown[label] = time.monotonic() + seconds

    def _record_ok(self, label: str, resp: LLMResponse, ms: int, primary: str) -> None:
        usage.record(self.role, label, prompt_tokens=resp.prompt_tokens, completion_tokens=resp.completion_tokens,
                     latency_ms=ms, cost=_cost_from(resp, self.prices),
                     fallback_from=primary if label != primary else "")

    def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMResponse:
        primary = self.chain[0][0]
        errors: list[str] = []
        for rnd in range(2):
            for label, llm in self._order():
                for attempt in range(2):
                    t0 = time.perf_counter()
                    try:
                        resp = llm.complete(messages, **kwargs)
                    except Exception as exc:
                        ms = round((time.perf_counter() - t0) * 1000)
                        kind = classify(exc)
                        usage.record(self.role, label, latency_ms=ms, ok=False, error=f"{type(exc).__name__}: {exc}")
                        errors.append(f"{label}: {type(exc).__name__} {status_of(exc) or ''}".strip())
                        if kind == "fatal":
                            raise
                        wait = retry_after_s(exc)
                        if kind == "rate" and attempt == 0 and wait is not None and wait <= self.retry_after_max_s:
                            logger.info("%s rate-limited; waiting %.1fs (Retry-After)", label, wait)
                            self._sleep(wait)
                            continue
                        self._cool(label, max(wait or 0, self.cooldown_s) if kind in ("rate", "config")
                                   else min(self.cooldown_s, 20))
                        logger.warning("%s LLM %s failed (%s) — trying the next model", self.role, label, kind)
                        break
                    ms = round((time.perf_counter() - t0) * 1000)
                    self._record_ok(label, resp, ms, primary)
                    self.last_model = label
                    return resp
            if rnd == 0 and errors and all("RateLimit" in e or " 429" in e for e in errors[-len(self.chain):]):
                self._sleep(min(self.retry_after_max_s, 5.0))   # everything is rate-limited: one short pause
                continue
            break
        raise ModelUnavailable(f"All {self.role} models failed: " + "; ".join(errors[-6:]))

    async def acomplete(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMResponse:
        primary = self.chain[0][0]
        errors: list[str] = []
        for label, llm in self._order():
            for attempt in range(2):
                t0 = time.perf_counter()
                try:
                    resp = await llm.acomplete(messages, **kwargs)
                except Exception as exc:
                    ms = round((time.perf_counter() - t0) * 1000)
                    kind = classify(exc)
                    usage.record(self.role, label, latency_ms=ms, ok=False, error=f"{type(exc).__name__}: {exc}")
                    errors.append(f"{label}: {type(exc).__name__} {status_of(exc) or ''}".strip())
                    if kind == "fatal":
                        raise
                    wait = retry_after_s(exc)
                    if kind == "rate" and attempt == 0 and wait is not None and wait <= self.retry_after_max_s:
                        await asyncio.sleep(wait)
                        continue
                    self._cool(label, max(wait or 0, self.cooldown_s) if kind in ("rate", "config")
                               else min(self.cooldown_s, 20))
                    break
                ms = round((time.perf_counter() - t0) * 1000)
                self._record_ok(label, resp, ms, primary)
                self.last_model = label
                return resp
        raise ModelUnavailable(f"All {self.role} models failed: " + "; ".join(errors[-6:]))


def parse_chain(spec: str, known_providers: tuple[str, ...]) -> list[tuple[str, str]]:
    """'openrouter:qwen/qwen3.8-27b:free, moonshot:kimi-k3' -> [(provider, model), ...]."""
    out: list[tuple[str, str]] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        prov, sep, model = part.partition(":")
        if sep and prov.strip().lower() in known_providers and model.strip():
            out.append((prov.strip().lower(), model.strip()))
        else:
            logger.warning("Ignoring fallback model %r (use provider:model)", part)
    return out
