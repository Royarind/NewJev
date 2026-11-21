"""Abstract base class for all LLM providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMResponse:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class BaseLLM(ABC):
    """Minimal interface every LLM provider must implement."""

    @abstractmethod
    def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMResponse:
        """Synchronous completion. Returns an LLMResponse."""
        ...

    @abstractmethod
    async def acomplete(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMResponse:
        """Async completion. Returns an LLMResponse."""
        ...
