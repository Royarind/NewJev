"""Centralised settings loaded from .env — governs all LLM and Jev configuration."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderType = Literal["openrouter", "openai", "anthropic", "groq", "huggingface", "llama", "moonshot"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -------------------------------------------------------------------------
    # Jev / TypeSafe
    # -------------------------------------------------------------------------
    typesafe_api_key: str = Field(default="", description="Native TypeSafe API key")
    typesafe_model: str = Field(default="jev-latest", description="TypeSafe model identifier")
    typesafe_api_url: str = Field(default="https://api.typesafe.ai/v1", description="TypeSafe base URL")
    openrouter_api_key: str = Field(default="", description="OpenRouter API key")
    jev_model: str = Field(default="jev-latest")
    jev_base_url: str = Field(default="")
    jev_timeout: float = Field(default=25.0)
    jev_max_retries: int = Field(default=3)
    jev_confidence_threshold: float = Field(default=0.6)

    # -------------------------------------------------------------------------
    # Text LLM (TYPE_TEXT — field value generation)
    # -------------------------------------------------------------------------
    text_llm_provider: ProviderType = Field(default="openrouter")
    text_llm_model: str = Field(default="nvidia/nemotron-3-ultra-550b-a55b:free")
    text_llm_api_key: str = Field(default="", description="Optional direct override for Text LLM API key")
    text_llm_base_url: str = Field(default="", description="Optional direct override for Text LLM base URL")
    text_llm_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    text_llm_max_tokens: int = Field(default=2048, ge=1)
    text_llm_top_p: float = Field(default=0.95, ge=0.0, le=1.0)
    text_llm_frequency_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    text_llm_presence_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    text_llm_timeout: float = Field(default=30.0)
    text_llm_reasoning: Literal["none", "low", "high"] = Field(default="none")

    # -------------------------------------------------------------------------
    # Planner LLM (goal understanding and step planning)
    # -------------------------------------------------------------------------
    planner_llm_provider: ProviderType = Field(default="openrouter")
    planner_llm_model: str = Field(default="nvidia/nemotron-3-ultra-550b-a55b:free")
    planner_llm_api_key: str = Field(default="", description="Optional direct override for Planner LLM API key")
    planner_llm_base_url: str = Field(default="", description="Optional direct override for Planner LLM base URL")
    planner_llm_temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    planner_llm_max_tokens: int = Field(default=2048, ge=1)
    planner_llm_top_p: float = Field(default=0.95, ge=0.0, le=1.0)
    planner_llm_timeout: float = Field(default=30.0)

    # -------------------------------------------------------------------------
    # Model fallback, rate limits, cost
    # -------------------------------------------------------------------------
    text_llm_fallbacks: str = Field(default="", description="Comma list provider:model tried when the text LLM fails, e.g. openrouter:qwen/qwen3.8-27b:free")
    planner_llm_fallbacks: str = Field(default="", description="Comma list provider:model tried when the planner LLM fails")
    llm_retry_after_max_s: float = Field(default=8.0, description="Wait for Retry-After up to this long; longer waits switch model")
    llm_cooldown_s: float = Field(default=60.0, description="Skip a rate-limited / failing model for this long")
    llm_prices: str = Field(default="", description="USD per 1M tokens for cost estimates: model=in:out,model2=in:out")

    # -------------------------------------------------------------------------
    # Rescue when stuck (LLM first, then the user, then blocked)
    # -------------------------------------------------------------------------
    rescue_enabled: bool = Field(default=True)
    rescue_llm: str = Field(default="", description="provider:model for rescues (empty: the planner LLM chain)")
    max_rescues: int = Field(default=3, description="LLM rescues per run")
    rescue_ask_user: bool = Field(default=True, description="After the LLM rescue fails, ask the user for a hint")

    # -------------------------------------------------------------------------
    # You take over: pause when the person uses the mouse / keyboard during a run
    # -------------------------------------------------------------------------
    takeover_pause: bool = Field(default=True)
    takeover_include_moves: bool = Field(default=False, description="Also pause on large mouse movements (not only clicks / keys)")
    takeover_idle_resume_s: int = Field(default=30, description="Unattended runs resume after this many idle seconds")

    # -------------------------------------------------------------------------
    # Learned recipes (memory of verified runs and failures)
    # -------------------------------------------------------------------------
    recipes_enabled: bool = Field(default=True)
    recipes_path: str = Field(default="data/recipes.json")
    recipe_match_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    plan_recipes: bool = Field(default=True, description="Reuse the plan of a verified run for the same kind of request (skips the planner LLM)")

    # -------------------------------------------------------------------------
    # Vision fallback (screenshots for apps with poor accessibility info)
    # -------------------------------------------------------------------------
    vision_fallback: bool = Field(default=True)
    vision_llm: str = Field(default="openrouter:openai/gpt-4o-mini", description="provider:model of an image-capable model (OpenAI-compatible)")
    vision_min_elements: int = Field(default=6, description="Offer vision when a desktop window exposes fewer controls than this")

    # -------------------------------------------------------------------------
    # Shortcuts beyond the UI (mail, files) — UI is always the fallback
    # -------------------------------------------------------------------------
    shortcuts_enabled: bool = Field(default=True)
    gmail_credentials_path: str = Field(default="", description="Google OAuth client JSON; empty disables the Gmail API shortcut")
    gmail_token_path: str = Field(default="data/gmail_token.json")
    slack_token: str = Field(default="", description="Optional manual Slack token; normally use Connections -> Connect Slack")
    slack_client_id: str = Field(default="", description="Publisher's Slack app client id (overrides app/config/connectors.json)")
    google_client_id: str = Field(default="", description="Publisher's Google desktop OAuth client id")
    google_client_secret: str = Field(default="", description="Google desktop client 'secret' (not confidential for desktop apps)")
    connections_path: str = Field(default="data/connections", description="Where sign-in tokens of connected accounts are kept")
    graph_client_id: str = Field(default="", description="Entra ID app (public client) for Teams / calendar / OneDrive / SharePoint")
    graph_tenant: str = Field(default="organizations", description="Tenant id or domain; 'organizations' for work accounts")
    graph_token_path: str = Field(default="data/graph_token.json")

    # -------------------------------------------------------------------------
    # Saved tasks, schedules, folder triggers
    # -------------------------------------------------------------------------
    tasks_path: str = Field(default="data/tasks.json")
    jobs_path: str = Field(default="data/jobs", description="Folder with the progress of long jobs (resume after a crash)")
    scheduler_enabled: bool = Field(default=True, description="Run scheduled tasks / folder triggers while the UI server runs")
    scheduler_poll_s: int = Field(default=20)

    # -------------------------------------------------------------------------
    # Provider credentials
    # -------------------------------------------------------------------------
    openrouter_base_url: str = Field(default="https://openrouter.ai/api/v1")
    openai_api_key: str = Field(default="")
    openai_base_url: str = Field(default="https://api.openai.com/v1")
    anthropic_api_key: str = Field(default="")
    groq_api_key: str = Field(default="")
    groq_base_url: str = Field(default="https://api.groq.com/openai/v1")
    huggingface_api_key: str = Field(default="")
    huggingface_base_url: str = Field(default="https://api-inference.huggingface.co/v1")
    llama_base_url: str = Field(default="http://localhost:11434/v1")
    llama_api_key: str = Field(default="ollama")
    llama_model: str = Field(default="llama3.1:8b")
    moonshot_api_key: str = Field(default="")
    moonshot_base_url: str = Field(default="https://api.moonshot.cn/v1")

    # -------------------------------------------------------------------------
    # Browser
    # -------------------------------------------------------------------------
    headless: bool = Field(default=False)
    browser_type: Literal["chromium", "firefox", "webkit"] = Field(default="chromium")
    browser_channel: str = Field(default="chrome", description="Set to 'chrome' to use installed Google Chrome with real user profiles, or 'msedge', or empty for bundled chromium")
    browser_user_data_dir: str = Field(default="data/chrome_profile", description="Persistent profile directory to maintain signed-in logins, cookies, and Google accounts across sessions")
    browser_cdp_url: str = Field(default="", description="Optional CDP URL to attach to an already running Chrome (e.g. http://127.0.0.1:9222)")
    browser_width: int = Field(default=1280)
    browser_height: int = Field(default=800)
    browser_slow_mo: int = Field(default=0)
    dom_settle_ms: int = Field(default=200)
    combobox_wait_ms: int = Field(default=200)

    # -------------------------------------------------------------------------
    # Agent
    # -------------------------------------------------------------------------
    capture_mode: Literal["browser", "desktop", "auto"] = Field(default="browser")
    max_steps: int = Field(default=150, description="Safety cap per run; loops are stopped by progress checks, not this")
    step_delay_ms: int = Field(default=0)
    screenshot_on_error: bool = Field(default=True)
    screenshot_interval: int = Field(default=0)
    history_window: int = Field(default=8)
    phase_max_steps: int = Field(default=80, description="Safety cap per phase of a multi-step plan")

    # -------------------------------------------------------------------------
    # Reliability & safety
    # -------------------------------------------------------------------------
    low_confidence_retries: int = Field(default=1, description="Re-observe and re-ask Jev this many times when confidence is below threshold")
    confirm_low_confidence: bool = Field(default=True, description="Ask the user before executing a very unsure step")
    low_confidence_ask_threshold: float = Field(default=0.35, ge=0.0, le=1.0,
        description="Only steps below this (after retries) are confirmed with the user; others run and are flagged")
    verify_done: bool = Field(default=True, description="Independently verify DONE with Jev Noul before accepting it")
    done_verify_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    max_done_rejections: int = Field(default=2, description="After this many rejected DONEs the run ends as 'unverified'")
    verify_each_step: bool = Field(default=False, description="Extra Jev Noul call after every step (slower, informational only)")
    stuck_repeat_limit: int = Field(default=3, description="Same action this many times in a row counts as a loop")
    confirm_risky_actions: bool = Field(default=True, description="Ask before clicking Send/Delete/Pay/etc.")
    risky_keywords: str = Field(
        default="send,delete,remove,pay,purchase,buy now,place order,checkout,transfer,confirm payment,"
                "unsubscribe,empty trash,empty recycle bin,permanently,format,uninstall,submit order,book now",
    )
    handoff_timeout_s: int = Field(default=600, description="How long to wait for the user during a hand-off")

    # -------------------------------------------------------------------------
    # Finishing touches (implicit steps)
    # -------------------------------------------------------------------------
    wrap_up: bool = Field(default=True, description="After the task: commit pending edits, save, close what the agent opened")
    close_opened_windows: bool = Field(default=True, description="Close desktop windows the agent opened, unless the goal is to show/open something")
    wrap_up_max_steps: int = Field(default=10)

    # -------------------------------------------------------------------------
    # How-to procedures, extra actions, Office automation
    # -------------------------------------------------------------------------
    procedure_mode: Literal["auto", "always", "off"] = Field(
        default="auto", description="Write a step-by-step UI procedure before acting (auto: desktop apps + longer web tasks)")
    typing_mode: Literal["type", "paste", "auto"] = Field(
        default="type", description="type: visible, checked typing like a person; paste: clipboard; auto: paste long text")
    typing_delay_ms: int = Field(default=15, ge=0, le=500, description="Pause between typed characters")
    office_automation: bool = Field(
        default=False, description="Allow the OFFICE_ACTION shortcut (Excel/Word automation, allow-listed)")

    # -------------------------------------------------------------------------
    # UI Server
    # -------------------------------------------------------------------------
    host: str = Field(default="127.0.0.1")
    desktop_popups: bool = Field(default=True, description="Show requests that need you as a pop-up above the taskbar (bottom-right)")
    hotkey_enabled: bool = Field(default=True, description="Global hotkey opens a small command box from anywhere")
    hotkey: str = Field(default="ctrl+alt+j")
    port: int = Field(default=8766)
    auto_open_browser: bool = Field(default=True)

    # -------------------------------------------------------------------------
    # Logging
    # -------------------------------------------------------------------------
    log_level: str = Field(default="INFO")
    log_dir: str = Field(default="logs")
    save_traces: bool = Field(default=True)
    save_screenshots: bool = Field(default=True, description="Screenshots at key moments (phase end, stuck) for run history")

    @field_validator("jev_confidence_threshold")
    @classmethod
    def _validate_confidence(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("jev_confidence_threshold must be between 0.0 and 1.0")
        return v

    def api_key_for(self, provider: ProviderType) -> str:
        """Return the API key for the given provider."""
        mapping = {
            "openrouter": self.openrouter_api_key,
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "groq": self.groq_api_key,
            "huggingface": self.huggingface_api_key,
            "llama": self.llama_api_key,
            "moonshot": self.moonshot_api_key,
        }
        return mapping.get(provider, "")

    def base_url_for(self, provider: ProviderType) -> str:
        """Return the base URL for the given provider."""
        mapping = {
            "openrouter": self.openrouter_base_url,
            "openai": self.openai_base_url,
            "anthropic": "",  # Anthropic SDK handles its own URL
            "groq": self.groq_base_url,
            "huggingface": self.huggingface_base_url,
            "llama": self.llama_base_url,
            "moonshot": self.moonshot_base_url,
        }
        return mapping.get(provider, "")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings singleton."""
    return Settings()
