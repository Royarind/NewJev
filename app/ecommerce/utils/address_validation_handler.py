import asyncio
import logging
import re


logger = logging.getLogger(__name__)


class AddressValidationHandler:
    """
    Detects and dismisses address validation dialogs that appear after filling
    a shipping address on e-commerce sites (Shopify, Walmart, Target, etc.).

    Handles dialogs like:
    - "Did you mean ...?"
    - "We couldn't verify your address"
    - "Confirm your address"
    - "Suggested address" modals
    """

    DETECTION_PATTERNS = [
        "did you mean",
        "couldn't verify",
        "address not found",
        "address verification",
        "confirm your address",
        "verify address",
        "suggested address",
        "we found a match",
        "address entered",
        "use entered",
        "use original",
        "keep as entered",
    ]

    DIALOG_SELECTORS = [
        '[role="dialog"]',
        '[role="alertdialog"]',
        '.address-verification',
        '[class*="address-confirm"]',
        '[class*="address-verify"]',
        '[class*="AddressModal"]',
        '[data-testid*="address-verification"]',
    ]

    # Priority-ordered button selectors: "keep entered" options first, then fallbacks
    KEEP_ENTERED_BUTTON_SELECTORS = [
        'button:has-text("Use entered address")',
        'button:has-text("Use entered")',
        'button:has-text("Keep as entered")',
        'button:has-text("Use my address")',
        'button:has-text("Use original address")',
        'button:has-text("Use original")',
        '[data-testid*="use-entered"]',
        '[data-testid*="confirm-address"]',
    ]

    FALLBACK_BUTTON_SELECTORS = [
        'button:has-text("Use suggested")',
        'button:has-text("Confirm")',
        'button:has-text("OK")',
    ]

    def __init__(self, page, status_display=None, logger=None):
        """
        Args:
            page: Playwright async Page object.
            status_display: Optional status display object with an `update(msg)` method.
            logger: Optional logger instance. Falls back to module-level logger.
        """
        self.page = page
        self.status_display = status_display
        self.log = logger if logger is not None else globals()["logger"]

    def _update_status(self, message: str) -> None:
        """Push a status message to the display if available."""
        if self.status_display is not None:
            try:
                self.status_display.update(message)
            except Exception:
                pass

    async def _dialog_visible(self) -> bool:
        """
        Return True if a known address-validation dialog is currently visible.
        Checks both element selectors and raw page text.
        """
        try:
            # 1. Check structured dialog selectors
            for selector in self.DIALOG_SELECTORS:
                try:
                    elements = await self.page.query_selector_all(selector)
                    for el in elements:
                        try:
                            visible = await el.is_visible()
                            if not visible:
                                continue
                            text = (await el.inner_text() or "").lower()
                            for pattern in self.DETECTION_PATTERNS:
                                if pattern in text:
                                    self.log.debug(
                                        "Address validation dialog detected via selector '%s' (pattern: '%s')",
                                        selector,
                                        pattern,
                                    )
                                    return True
                        except Exception:
                            continue
                except Exception:
                    continue

            # 2. Fall back to scanning full page text
            try:
                body_text = await self.page.evaluate("document.body.innerText")
                body_lower = (body_text or "").lower()
                for pattern in self.DETECTION_PATTERNS:
                    if pattern in body_lower:
                        self.log.debug(
                            "Address validation dialog detected via page text (pattern: '%s')",
                            pattern,
                        )
                        return True
            except Exception:
                pass

        except Exception as exc:
            self.log.warning("Error during dialog detection: %s", exc)

        return False

    async def _try_click_selector(self, selector: str) -> bool:
        """
        Attempt to find and click a visible element matching `selector`.
        Returns True on success, False otherwise. Never raises.
        """
        try:
            elements = await self.page.query_selector_all(selector)
            for el in elements:
                try:
                    if await el.is_visible():
                        await el.click()
                        self.log.debug("Clicked address dialog button via selector: %s", selector)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    async def _dismiss_dialog(self) -> bool:
        """
        Attempt to dismiss the detected dialog by clicking the best available button.

        Priority:
        1. "Keep entered" variants (user's original address).
        2. "Use suggested" / generic confirm buttons as a last resort.

        Returns True if a button was successfully clicked.
        """
        # Priority 1: keep-as-entered buttons
        for selector in self.KEEP_ENTERED_BUTTON_SELECTORS:
            if await self._try_click_selector(selector):
                return True

        # Priority 2: fallback buttons (use suggested / confirm / ok)
        for selector in self.FALLBACK_BUTTON_SELECTORS:
            if await self._try_click_selector(selector):
                return True

        self.log.warning("Address validation dialog detected but no dismissal button found.")
        return False

    async def check_and_dismiss(self, wait_seconds: float = 3.0) -> bool:
        """
        Main entry point. Wait up to `wait_seconds` for an address validation
        dialog to appear, then dismiss it.

        Args:
            wait_seconds: How long to poll for the dialog before giving up.

        Returns:
            True  — a dialog was found and dismissed (or a click was attempted).
            False — no dialog appeared within the wait window.
        """
        poll_interval = 0.25  # seconds between checks
        elapsed = 0.0

        try:
            while elapsed < wait_seconds:
                if await self._dialog_visible():
                    self._update_status("Address validation dialog detected — dismissing…")
                    dismissed = await self._dismiss_dialog()
                    if dismissed:
                        self._update_status("Address validation dialog dismissed.")
                        # Give the page a moment to process the click
                        await asyncio.sleep(2.0)
                    return True

                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

        except Exception as exc:
            self.log.warning("Unexpected error in check_and_dismiss: %s", exc)

        return False
