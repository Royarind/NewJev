"""OpenAI-compatible provider: OpenRouter, OpenAI, Groq, HuggingFace, Llama/Ollama, Moonshot."""

from __future__ import annotations

import time
from typing import Any

import openai

from .base import BaseLLM, LLMResponse


class OpenAICompatLLM(BaseLLM):
    """
    Wraps any OpenAI-compatible API using the official openai SDK.
    Supports: openrouter, openai, groq, huggingface, llama/ollama, moonshot.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 512,
        top_p: float = 0.95,
        frequency_penalty: float = 0.0,
        presence_penalty: float = 0.0,
        timeout: float = 30.0,
        reasoning: str = "none",
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.extra_body = extra_body or {}
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.top_p = top_p
        self.frequency_penalty = frequency_penalty
        self.presence_penalty = presence_penalty
        self.timeout = timeout
        self.reasoning = reasoning

        self._client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            default_headers=extra_headers or {},
            max_retries=0,   # retries / fallbacks are handled (visibly) by FallbackLLM
        )
        self._async_client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            default_headers=extra_headers or {},
            max_retries=0,   # retries / fallbacks are handled (visibly) by FallbackLLM
        )

    def _build_params(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "top_p": kwargs.get("top_p", self.top_p),
            "frequency_penalty": kwargs.get("frequency_penalty", self.frequency_penalty),
            "presence_penalty": kwargs.get("presence_penalty", self.presence_penalty),
        }
        # Add reasoning effort if not none
        if self.reasoning == "low":
            params["reasoning_effort"] = "low"
        elif self.reasoning == "high":
            params["reasoning_effort"] = "high"
        if self.extra_body:
            params["extra_body"] = dict(self.extra_body)
        return params

    def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMResponse:
        params = self._build_params(kwargs)
        started = time.perf_counter()
        response = self._client.chat.completions.create(messages=messages, **params)
        latency_ms = round((time.perf_counter() - started) * 1000)
        choices = getattr(response, "choices", None) or []
        content = choices[0].message.content or "" if choices and getattr(choices[0], "message", None) else ""
        usage = getattr(response, "usage", None)
        return LLMResponse(
            content=content,
            model=getattr(response, "model", self.model),
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
            latency_ms=latency_ms,
            raw=response.model_dump() if hasattr(response, "model_dump") else {},
        )

    async def acomplete(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMResponse:
        params = self._build_params(kwargs)
        started = time.perf_counter()
        response = await self._async_client.chat.completions.create(messages=messages, **params)
        latency_ms = round((time.perf_counter() - started) * 1000)
        choices = getattr(response, "choices", None) or []
        content = choices[0].message.content or "" if choices and getattr(choices[0], "message", None) else ""
        usage = getattr(response, "usage", None)
        return LLMResponse(
            content=content,
            model=getattr(response, "model", self.model),
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
            latency_ms=latency_ms,
            raw=response.model_dump() if hasattr(response, "model_dump") else {},
        )
