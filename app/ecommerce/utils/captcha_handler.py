#!/usr/bin/env python3
"""
CAPTCHA Handler
Detects reCAPTCHA v2/v3, hCaptcha, and Cloudflare Turnstile.
Pauses automation and waits for the human operator to solve the challenge,
then resumes automatically once the CAPTCHA is dismissed.

This mirrors the existing OTP-pause pattern: the system shows a status
message, polls for the CAPTCHA to disappear, and returns True when clear.
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Detection selectors ───────────────────────────────────────────────────────
# Each tuple is (provider_name, iframe_url_fragment, fallback_dom_selector)
_CAPTCHA_SIGNATURES = [
    (
        "reCAPTCHA v2 checkbox",
        "google.com/recaptcha",
        'iframe[src*="google.com/recaptcha"]',
    ),
    (
        "reCAPTCHA enterprise",
        "recaptcha.net",
        'iframe[src*="recaptcha.net"]',
    ),
    (
        "hCaptcha",
        "hcaptcha.com",
        'iframe[src*="hcaptcha.com"]',
    ),
    (
        "Cloudflare Turnstile",
        "challenges.cloudflare.com",
        'iframe[src*="challenges.cloudflare.com"]',
    ),
    (
        "Cloudflare challenge",
        "cf-challenge",
        '[class*="cf-challenge"]',
    ),
]

# ── Solved / dismissed indicators ─────────────────────────────────────────────
# After the user solves the CAPTCHA these elements appear (or the iframe hides).
_SOLVED_SELECTORS = [
    '.recaptcha-checkbox-checked',          # reCAPTCHA v2 checked state
    '.recaptcha-checkbox[aria-checked="true"]',
    '[data-hcaptcha-response]:not([data-hcaptcha-response=""])',  # hCaptcha token filled
    'textarea#g-recaptcha-response:not(:empty)',                   # reCAPTCHA hidden textarea
    'input[name="h-captcha-response"]:not([value=""])',
    '[name="cf-turnstile-response"]:not([value=""])',              # Turnstile token
]


class CaptchaHandler:
    """
    Detects CAPTCHA challenges and pauses automation until the operator solves them.

    Usage:
        handler = CaptchaHandler(page, status_display)
        detected = await handler.check_and_wait(timeout_seconds=120)
        # detected is True if a CAPTCHA was found AND solved; False if none found or timed out.
    """

    def __init__(self, page, status_display=None):
        self.page = page
        self.status = status_display
        self.logger = logging.getLogger(__name__)

    async def is_captcha_present(self) -> Optional[str]:
        """
        Returns the name of the detected CAPTCHA provider, or None if no CAPTCHA found.
        Checks both iframe[src] and DOM class/attribute signatures.
        """
        for provider, _url_frag, selector in _CAPTCHA_SIGNATURES:
            try:
                el = await self.page.query_selector(selector)
                if el and await el.is_visible():
                    self.logger.info(f"CAPTCHA detected: {provider} via '{selector}'")
                    return provider
            except Exception:
                pass

        # Secondary: scan all frame URLs
        for frame in self.page.frames:
            url = frame.url or ""
            for provider, url_frag, _selector in _CAPTCHA_SIGNATURES:
                if url_frag and url_frag in url:
                    self.logger.info(f"CAPTCHA frame detected: {provider} (frame URL: {url[:80]})")
                    return provider

        return None

    async def is_captcha_solved(self) -> bool:
        """
        Returns True if the CAPTCHA appears to have been solved (token filled,
        checkbox checked, or CAPTCHA iframe no longer visible).
        """
        # Check solved-state indicators
        for sel in _SOLVED_SELECTORS:
            try:
                el = await self.page.query_selector(sel)
                if el:
                    self.logger.debug(f"CAPTCHA solved indicator found: {sel}")
                    return True
            except Exception:
                pass

        # Check if all known CAPTCHA iframes are now hidden / gone
        any_visible = False
        for _provider, _url_frag, selector in _CAPTCHA_SIGNATURES:
            try:
                el = await self.page.query_selector(selector)
                if el and await el.is_visible():
                    any_visible = True
                    break
            except Exception:
                pass

        return not any_visible

    async def check_and_wait(self, timeout_seconds: int = 120) -> bool:
        """
        Main entry point.

        1. Detect whether a CAPTCHA is present.
        2. If none detected, return False immediately (no intervention needed).
        3. If detected, show a status message and poll every 2 s for up to
           `timeout_seconds` seconds waiting for the operator to solve it.
        4. Return True if solved in time, False if timed out.

        The caller should treat a False return from a previous CAPTCHA detection
        as a soft failure (log a warning and try to proceed anyway, since some
        invisible v3 CAPTCHAs auto-resolve).
        """
        provider = await self.is_captcha_present()
        if not provider:
            return False  # No CAPTCHA — nothing to do

        self.logger.warning(
            f"🔒 CAPTCHA detected ({provider}). "
            f"Pausing automation for up to {timeout_seconds}s for manual solve..."
        )

        if self.status:
            try:
                await self.status.show(
                    f"🔒 CAPTCHA detected ({provider}) — please solve it in the browser...",
                    "warning",
                    timeout_seconds * 1000,
                )
            except Exception:
                pass

        start = asyncio.get_event_loop().time()
        poll_interval = 2.0

        while asyncio.get_event_loop().time() - start < timeout_seconds:
            await asyncio.sleep(poll_interval)

            if await self.is_captcha_solved():
                elapsed = asyncio.get_event_loop().time() - start
                self.logger.info(
                    f"✅ CAPTCHA solved by operator after {elapsed:.1f}s. Resuming automation."
                )
                if self.status:
                    try:
                        await self.status.show(
                            "✅ CAPTCHA solved — resuming...", "success", 2000
                        )
                    except Exception:
                        pass
                # Brief settle time for the page to process the CAPTCHA token
                await asyncio.sleep(1.5)
                return True

        self.logger.error(
            f"⏱️ CAPTCHA solve timeout after {timeout_seconds}s. "
            "Attempting to proceed anyway (may fail)."
        )
        if self.status:
            try:
                await self.status.show(
                    "⏱️ CAPTCHA timeout — proceeding (may fail)...", "error", 3000
                )
            except Exception:
                pass

        return False
