"""LLM provider factory — returns the correct provider based on .env settings."""

from __future__ import annotations

from functools import lru_cache

from ..config import get_settings
from .base import BaseLLM
from .openai_compat import OpenAICompatLLM
from .anthropic_provider import AnthropicLLM
from .resilient import FallbackLLM, parse_chain
from .usage import parse_prices

PROVIDERS = ("openrouter", "openai", "anthropic", "groq", "huggingface", "llama", "moonshot")


def _build_provider(provider: str, model: str, temperature: float, max_tokens: int,
                    top_p: float, timeout: float, reasoning: str = "none",
                    frequency_penalty: float = 0.0, presence_penalty: float = 0.0,
                    api_key_override: str = "", base_url_override: str = "") -> BaseLLM:
    """Instantiate the correct LLM provider class."""
    s = get_settings()

    api_key = api_key_override or s.api_key_for(provider)  # type: ignore[arg-type]
    base_url = base_url_override or s.base_url_for(provider)  # type: ignore[arg-type]

    if provider == "anthropic":
        return AnthropicLLM(
            api_key=api_key or s.anthropic_api_key,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            timeout=timeout,
        )

    # OpenRouter benefits from identifying the app
    extra_headers: dict[str, str] = {}
    extra_body: dict = {}
    if provider == "openrouter":
        extra_headers = {
            "HTTP-Referer": "https://github.com/newjev",
            "X-Title": "NewJev Agent",
        }
        extra_body = {"usage": {"include": True}}   # OpenRouter then reports the real cost of each call

    return OpenAICompatLLM(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        top_p=top_p,
        frequency_penalty=frequency_penalty,
        presence_penalty=presence_penalty,
        timeout=timeout,
        reasoning=reasoning,
        extra_headers=extra_headers,
        extra_body=extra_body,
    )


def _wrap(role: str, primary: tuple[str, BaseLLM], fallbacks: str, build) -> FallbackLLM:
    s = get_settings()
    chain = [primary]
    for prov, model in parse_chain(fallbacks, PROVIDERS):
        label = f"{prov}:{model}"
        if label != primary[0]:
            chain.append((label, build(prov, model)))
    return FallbackLLM(role, chain, retry_after_max_s=s.llm_retry_after_max_s, cooldown_s=s.llm_cooldown_s,
                       prices=parse_prices(s.llm_prices))


def _text_model(provider: str, model: str, overrides: bool = False) -> BaseLLM:
    s = get_settings()
    return _build_provider(
        provider=provider, model=model, temperature=s.text_llm_temperature, max_tokens=s.text_llm_max_tokens,
        top_p=s.text_llm_top_p, timeout=s.text_llm_timeout, reasoning=s.text_llm_reasoning,
        api_key_override=s.text_llm_api_key if overrides else "",
        base_url_override=s.text_llm_base_url if overrides else "",
    )


def _planner_model(provider: str, model: str, overrides: bool = False) -> BaseLLM:
    s = get_settings()
    return _build_provider(
        provider=provider, model=model, temperature=s.planner_llm_temperature, max_tokens=s.planner_llm_max_tokens,
        top_p=s.planner_llm_top_p, timeout=s.planner_llm_timeout,
        api_key_override=s.planner_llm_api_key if overrides else "",
        base_url_override=s.planner_llm_base_url if overrides else "",
    )


@lru_cache(maxsize=1)
def get_text_llm() -> BaseLLM:
    """Text LLM (field values) with automatic fallback to TEXT_LLM_FALLBACKS."""
    s = get_settings()
    primary = (f"{s.text_llm_provider}:{s.text_llm_model}", get_raw_text_llm())
    return _wrap("text", primary, s.text_llm_fallbacks, _text_model)


@lru_cache(maxsize=1)
def get_planner_llm() -> BaseLLM:
    """Planner LLM (plans, procedures) with automatic fallback to PLANNER_LLM_FALLBACKS."""
    s = get_settings()
    primary = (f"{s.planner_llm_provider}:{s.planner_llm_model}", get_raw_planner_llm())
    return _wrap("planner", primary, s.planner_llm_fallbacks, _planner_model)


@lru_cache(maxsize=1)
def get_rescue_llm() -> BaseLLM:
    """LLM used to rescue a stuck run: RESCUE_LLM, else the planner chain."""
    s = get_settings()
    chain = parse_chain(s.rescue_llm, PROVIDERS)
    if not chain:
        return get_planner_llm()
    prov, model = chain[0]
    primary = (f"{prov}:{model}", _planner_model(prov, model))
    return _wrap("rescue", primary, s.planner_llm_fallbacks, _planner_model)


@lru_cache(maxsize=1)
def get_vision_llm() -> BaseLLM | None:
    """Image-capable model for the screenshot fallback (OpenAI-compatible providers only)."""
    s = get_settings()
    chain = parse_chain(s.vision_llm, PROVIDERS)
    if not chain or chain[0][0] == "anthropic":
        return None
    prov, model = chain[0]
    llm = _build_provider(provider=prov, model=model, temperature=0.0, max_tokens=300, top_p=1.0,
                          timeout=max(s.text_llm_timeout, 45.0))
    return FallbackLLM("vision", [(f"{prov}:{model}", llm)], retry_after_max_s=s.llm_retry_after_max_s,
                       cooldown_s=s.llm_cooldown_s, prices=parse_prices(s.llm_prices))


@lru_cache(maxsize=1)
def get_raw_text_llm() -> BaseLLM:
    """The configured text model alone (no fallback)."""
    s = get_settings()
    return _build_provider(
        provider=s.text_llm_provider,
        model=s.text_llm_model,
        temperature=s.text_llm_temperature,
        max_tokens=s.text_llm_max_tokens,
        top_p=s.text_llm_top_p,
        timeout=s.text_llm_timeout,
        reasoning=s.text_llm_reasoning,
        frequency_penalty=s.text_llm_frequency_penalty,
        presence_penalty=s.text_llm_presence_penalty,
        api_key_override=s.text_llm_api_key,
        base_url_override=s.text_llm_base_url,
    )


@lru_cache(maxsize=1)
def get_raw_planner_llm() -> BaseLLM:
    """The configured planner model alone (no fallback)."""
    s = get_settings()
    return _build_provider(
        provider=s.planner_llm_provider,
        model=s.planner_llm_model,
        temperature=s.planner_llm_temperature,
        max_tokens=s.planner_llm_max_tokens,
        top_p=s.planner_llm_top_p,
        timeout=s.planner_llm_timeout,
        api_key_override=s.planner_llm_api_key,
        base_url_override=s.planner_llm_base_url,
    )
