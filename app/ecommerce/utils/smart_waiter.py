"""
smart_waiter.py
---------------
SPA-aware navigation wait utilities for Playwright async automation.

Replaces fragile asyncio.sleep() calls with proper wait strategies that
respect single-page app navigation, network activity, and DOM stability.

No playwright-specific imports at module level — `page` is accepted as a
constructor argument and used via duck typing, so this module imports cleanly
even without playwright installed.
"""

import asyncio
import logging
import re


class SmartWaiter:
    """
    Drop-in replacement for asyncio.sleep()-based waits in Playwright automation.

    All public methods are safe wrappers: they catch all exceptions, log
    warnings on timeout or error, and never raise to the caller.

    Usage:
        waiter = SmartWaiter(page, logger=logging.getLogger(__name__))
        new_url = await waiter.wait_after_click(old_url)
    """

    def __init__(self, page, logger=None):
        """
        Parameters
        ----------
        page : playwright.async_api.Page
            An active Playwright async Page object (duck-typed; no import needed).
        logger : logging.Logger, optional
            Logger instance. If None, a module-level logger is used.
        """
        self._page = page
        self._log = logger or logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def wait_after_click(self, old_url: str, timeout: float = 8.0) -> str:
        """
        Wait for a meaningful page change after a click action.

        Strategy (tried in order; stops at first success):
          1. wait_for_url() with a lambda checking url != old_url  (timeout/2)
          2. wait_for_load_state('networkidle', timeout=timeout*1000)
          3. _wait_dom_stable() polling

        Falls back to asyncio.sleep(1.5) on any exception.

        Parameters
        ----------
        old_url : str
            The URL that was active before the click.
        timeout : float
            Total seconds to allow for the transition (default 8.0).

        Returns
        -------
        str
            The new page URL after navigation (may equal old_url if no change occurred).
        """
        half_ms = (timeout / 2) * 1000  # milliseconds for playwright calls
        full_ms = timeout * 1000

        # --- Strategy 1: URL change ---
        try:
            await self._page.wait_for_url(
                lambda url: url != old_url,
                timeout=half_ms,
            )
            new_url = self._page.url
            self._log.debug("wait_after_click: URL changed to %s", new_url)
            return new_url
        except Exception as exc:
            self._log.debug(
                "wait_after_click: URL-change wait did not fire (%s); trying networkidle",
                type(exc).__name__,
            )

        # --- Strategy 2: network idle ---
        try:
            await self._page.wait_for_load_state("networkidle", timeout=full_ms)
            new_url = self._page.url
            self._log.debug("wait_after_click: networkidle reached; url=%s", new_url)
            return new_url
        except Exception as exc:
            self._log.debug(
                "wait_after_click: networkidle wait failed (%s); trying DOM stability",
                type(exc).__name__,
            )

        # --- Strategy 3: DOM stability polling ---
        try:
            await self._wait_dom_stable(timeout=timeout)
            new_url = self._page.url
            self._log.debug("wait_after_click: DOM stable; url=%s", new_url)
            return new_url
        except Exception as exc:
            self._log.debug(
                "wait_after_click: DOM-stable wait failed (%s); falling back to sleep",
                type(exc).__name__,
            )

        # --- Fallback: fixed sleep ---
        self._log.warning(
            "wait_after_click: all strategies exhausted; sleeping 1.5 s (old_url=%s)",
            old_url,
        )
        await asyncio.sleep(1.5)
        try:
            return self._page.url
        except Exception:
            return old_url

    async def wait_for_url_pattern(self, pattern: str, timeout: float = 10.0) -> bool:
        """
        Wait until page.url contains `pattern`.

        Tries page.wait_for_url() with a compiled regex first; falls back to
        manual polling if that raises.

        Parameters
        ----------
        pattern : str
            Substring or regex pattern to match against the current URL.
        timeout : float
            Seconds to wait (default 10.0).

        Returns
        -------
        bool
            True if the pattern was matched within the timeout, False otherwise.
        """
        compiled = re.compile(pattern)

        # --- Primary: playwright's built-in wait_for_url ---
        try:
            await self._page.wait_for_url(
                compiled,
                timeout=timeout * 1000,
            )
            self._log.debug(
                "wait_for_url_pattern: pattern '%s' matched; url=%s",
                pattern,
                self._page.url,
            )
            return True
        except Exception as exc:
            self._log.debug(
                "wait_for_url_pattern: wait_for_url failed (%s); falling back to polling",
                type(exc).__name__,
            )

        # --- Fallback: manual polling ---
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                current_url = self._page.url
                if compiled.search(current_url):
                    self._log.debug(
                        "wait_for_url_pattern: polling matched pattern '%s'; url=%s",
                        pattern,
                        current_url,
                    )
                    return True
            except Exception as exc:
                self._log.debug(
                    "wait_for_url_pattern: error reading page.url during polling (%s)",
                    type(exc).__name__,
                )
            await asyncio.sleep(0.25)

        self._log.warning(
            "wait_for_url_pattern: pattern '%s' not matched within %.1f s",
            pattern,
            timeout,
        )
        return False

    async def wait_dom_stable(
        self, timeout: float = 5.0, stability_ms: int = 500
    ) -> bool:
        """
        Poll document.body.innerHTML.length every 200 ms until it stops
        changing for `stability_ms` milliseconds.

        Parameters
        ----------
        timeout : float
            Maximum seconds to wait (default 5.0).
        stability_ms : int
            Milliseconds of unchanging DOM length required to declare stability
            (default 500).

        Returns
        -------
        bool
            True if the DOM became stable, False if the timeout was reached first.
        """
        try:
            return await self._wait_dom_stable(timeout=timeout, stability_ms=stability_ms)
        except Exception as exc:
            self._log.warning(
                "wait_dom_stable: unexpected error (%s: %s)",
                type(exc).__name__,
                exc,
            )
            return False

    async def wait_for_element(self, selector: str, timeout: float = 8.0) -> bool:
        """
        Wait for a CSS selector to appear and be visible.

        Parameters
        ----------
        selector : str
            CSS selector string.
        timeout : float
            Seconds to wait (default 8.0).

        Returns
        -------
        bool
            True if the element became visible, False on timeout or error.
        """
        try:
            await self._page.wait_for_selector(
                selector,
                state="visible",
                timeout=timeout * 1000,
            )
            self._log.debug("wait_for_element: selector '%s' is visible", selector)
            return True
        except Exception as exc:
            self._log.warning(
                "wait_for_element: selector '%s' not visible within %.1f s (%s)",
                selector,
                timeout,
                type(exc).__name__,
            )
            return False

    async def wait_for_network_idle(self, timeout: float = 8.0) -> bool:
        """
        Wait for the page to reach networkidle load state.

        Safe wrapper — never raises.

        Parameters
        ----------
        timeout : float
            Seconds to wait (default 8.0).

        Returns
        -------
        bool
            True if networkidle was reached, False on timeout or error.
        """
        try:
            await self._page.wait_for_load_state(
                "networkidle",
                timeout=timeout * 1000,
            )
            self._log.debug("wait_for_network_idle: networkidle reached")
            return True
        except Exception as exc:
            self._log.warning(
                "wait_for_network_idle: networkidle not reached within %.1f s (%s)",
                timeout,
                type(exc).__name__,
            )
            return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _wait_dom_stable(
        self, timeout: float = 5.0, stability_ms: int = 500
    ) -> bool:
        """
        Internal DOM-stability poller used by wait_after_click and wait_dom_stable.

        Polls document.body.innerHTML.length every 200 ms. Declares stability
        when the value has not changed for `stability_ms` consecutive milliseconds.

        Raises underlying exceptions (caller is responsible for catching).
        """
        poll_interval = 0.2  # seconds
        required_stable_polls = max(1, int(stability_ms / (poll_interval * 1000)))

        deadline = asyncio.get_event_loop().time() + timeout
        last_length = None
        stable_count = 0

        while asyncio.get_event_loop().time() < deadline:
            try:
                current_length = await self._page.evaluate(
                    "() => document.body ? document.body.innerHTML.length : 0"
                )
            except Exception as exc:
                self._log.debug(
                    "_wait_dom_stable: evaluate error (%s); resetting stable count",
                    type(exc).__name__,
                )
                stable_count = 0
                await asyncio.sleep(poll_interval)
                continue

            if current_length == last_length:
                stable_count += 1
                if stable_count >= required_stable_polls:
                    self._log.debug(
                        "_wait_dom_stable: DOM stable (length=%d) after %d polls",
                        current_length,
                        stable_count,
                    )
                    return True
            else:
                stable_count = 0
                last_length = current_length

            await asyncio.sleep(poll_interval)

        self._log.warning(
            "_wait_dom_stable: DOM did not stabilise within %.1f s", timeout
        )
        return False
