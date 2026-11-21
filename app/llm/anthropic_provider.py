"""Anthropic Claude provider."""

from __future__ import annotations

import time
from typing import Any

import anthropic

from .base import BaseLLM, LLMResponse


class AnthropicLLM(BaseLLM):
    """Wraps the official Anthropic Python SDK."""

    def __init__(
        self,
        api_key: str,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 512,
        top_p: float = 0.95,
        timeout: float = 30.0,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.top_p = top_p
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=0)
        self._async_client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=0)

    def _split_messages(self, messages: list[dict[str, str]]) -> tuple[str | None, list[dict]]:
        """Separate the system prompt from the conversation messages."""
        system = None
        human_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                human_messages.append({"role": msg["role"], "content": msg["content"]})
        return system, human_messages

    def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMResponse:
        system, human_messages = self._split_messages(messages)
        started = time.perf_counter()
        extra: dict[str, Any] = {}
        if system:
            extra["system"] = system
        response = self._client.messages.create(
            model=self.model,
            max_tokens=kwargs.get("max_tokens", self.max_tokens),
            temperature=kwargs.get("temperature", self.temperature),
            top_p=kwargs.get("top_p", self.top_p),
            messages=human_messages,
            **extra,
        )
        latency_ms = round((time.perf_counter() - started) * 1000)
        content = response.content[0].text if response.content else ""
        usage = response.usage
        return LLMResponse(
            content=content,
            model=response.model,
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
            total_tokens=usage.input_tokens + usage.output_tokens,
            latency_ms=latency_ms,
            raw=response.model_dump(),
        )

    async def acomplete(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMResponse:
        system, human_messages = self._split_messages(messages)
        started = time.perf_counter()
        extra: dict[str, Any] = {}
        if system:
            extra["system"] = system
        response = await self._async_client.messages.create(
            model=self.model,
            max_tokens=kwargs.get("max_tokens", self.max_tokens),
            temperature=kwargs.get("temperature", self.temperature),
            top_p=kwargs.get("top_p", self.top_p),
            messages=human_messages,
            **extra,
        )
        latency_ms = round((time.perf_counter() - started) * 1000)
        content = response.content[0].text if response.content else ""
        usage = response.usage
        return LLMResponse(
            content=content,
            model=response.model,
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
            total_tokens=usage.input_tokens + usage.output_tokens,
            latency_ms=latency_ms,
            raw=response.model_dump(),
        )
