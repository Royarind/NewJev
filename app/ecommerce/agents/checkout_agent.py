#!/usr/bin/env python3
"""
Checkout Agent
Handles cart navigation, form filling (shipping/contact), and final review.
Integrates Vision LLM (VLM) for robust element localization.
"""

import asyncio
import logging
import os
import json
import re
from typing import Dict, Any, Optional, List
from pathlib import Path
from .base_agent import BaseAgent, Task, Result
from ..utils.llm_client import SimpleLLMClient
from ..vision import EnhancedVisionSystem
from ..utils.distraction_manager import DistractionManager
from ..utils.smart_scroller import SmartScroller
from ..utils.status_display import StatusDisplay
from ..utils.text_llm_form_fixer import TextLLMFormFixer
from ..utils.form_state_extractor import FormStateExtractor
from ..utils.click_coordinator import ClickCoordinator
from ..address_autofill import AddressAutofill
from ..utils.dom_intelligence import DOMIntelligence
from ..utils.captcha_handler import CaptchaHandler
from ..utils.smart_waiter import SmartWaiter
from ..utils.address_validation_handler import AddressValidationHandler
from ..utils.payment_form_filler import PaymentFormFiller

class CheckoutAgent(BaseAgent):
    """
    Agent that manages the end-to-end checkout flow.
    Uses multi-tier detection (DOM -> VLM).
    """
    
    def __init__(self, page, llm_client: SimpleLLMClient, memory=None):
        super().__init__("CheckoutAgent", llm_client, memory)
        self.page = page
        self.vision = EnhancedVisionSystem(debug_mode=False)
        self.manual_review_final_step = True  # User requested manual review
        try:
            self.root_dir = Path(__file__).resolve().parents[4]
            self.js_assets_dir = self.Path(__file__).resolve().parents[1] / 'dom_assets'
        except Exception:
            self.js_assets_dir = Path(__file__).resolve().parents[1] / 'dom_assets'
        self.distraction_manager = DistractionManager(page)
        self.scroller = SmartScroller(page)
        self.status = StatusDisplay(page)
        self.click_coordinator = ClickCoordinator(page)  # For safe clicks
        self.address_autofill = AddressAutofill(page, self.llm, self.status)
        self.captcha_handler = CaptchaHandler(page, self.status)
        self.smart_waiter = SmartWaiter(page, self.logger)
        self.addr_validator = AddressValidationHandler(page, self.status, self.logger)
        self.payment_filler = PaymentFormFiller(page, self.status, self.logger)

    def _extract_json(self, text: str) -> Dict:
        """Extract JSON object from an LLM text response (markdown or raw)."""
        import re as _re, json as _json
        if isinstance(text, dict):
            return text
        text = text or ""
        # Try ```json ... ``` block first
        m = _re.search(r"```json\s*(\{.*?\})\s*```", text, _re.DOTALL)
        if m:
            try:
                return _json.loads(m.group(1))
            except Exception:
                pass
        # Fall back to first { ... last }
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            chunk = text[start:end + 1]
            try:
                return _json.loads(chunk)
            except Exception:
                chunk = _re.sub(r"//.*", "", chunk)
                try:
                    return _json.loads(chunk)
                except Exception:
                    pass
        return {}

    async def _visualize_click(self, x: float, y: float):
        """Show a blue circle at the click coordinates."""
        try:
            path = self.js_assets_dir / 'visualize_click.js'
            if not path.exists():
                self.logger.warning(f"JS Visualizer not found at {path}")
                return
                
            js_visualize = path.read_text(encoding='utf-8')
            self.logger.info(f"Visualizing click at ({x}, {y}) using {path}")
            await self.page.evaluate(js_visualize, {'x': x, 'y': y})
        except Exception as e:
            self.logger.warning(f"Click visualization failed: {e}")
        
    async def can_handle(self, task: Task) -> float:
        """Evaluate if we can handle the checkout task"""
        checkout_types = [
            'checkout',
            'navigate_to_cart',
            'login_or_guest',
            'fill_shipping',
            'fill_contact',
            'select_shipping_method',
            'place_order'
        ]
        if task.type in checkout_types:
            return 1.0
        return 0.0
        
    async def execute(self, task: Task) -> Result:
        """Route to specific checkout stage"""
        if task.type == 'navigate_to_cart':
            return await self._navigate_to_cart(task)
        elif task.type == 'checkout':
            return await self._run_full_checkout(task)
        elif task.type == 'login_or_guest':
            return await self._handle_login_or_guest(task)
        elif task.type == 'fill_shipping':
            return await self._fill_shipping(task)
        elif task.type == 'fill_contact':
            return await self._fill_contact(task)
        elif task.type == 'select_shipping_method':
            return await self._select_shipping_method(task)
        elif task.type == 'place_order':
            return await self._place_order(task)

        return Result(success=False, task=task, error=f"Unhandled task type: {task.type}")

    def _is_cart_or_checkout_url(self, url: str) -> bool:
        """
        Robustly verify if the URL corresponds to a cart or checkout page.
        Handles false positives like '/products/sleeping-bag' or '/cartridge-pen'.
        Also handles sites that embed keywords inside path segments, e.g.
        Flipkart's '/viewcart' or '/shoppingcart'.
        """
        url_lower = url.lower()
        # Remove query params/hash for precise path segment analysis
        clean_url = url_lower.split('?')[0].split('#')[0]
        
        # Check standard path segments
        segments = [seg for seg in clean_url.split('/') if seg]
        keywords = {'cart', 'checkout', 'bag', 'basket', 'order', 'buy', 'payment'}
        
        # False-positive substrings: segments that contain a keyword but are NOT cart pages
        # e.g. 'cartridge', 'discharge', 'buyout' (business page), 'payment-policy'
        keyword_fp_patterns = {
            'cart': {'cartridge', 'discard', 'carta', 'carte'},
            'buy': {'buyout', 'buyer-guide', 'buying-guide'},
            'order': {'disorder', 'preorder-info', 'order-history'},  # order-history is valid but handled below
        }

        for seg in segments:
            # --- Pass 1: exact word match after splitting on non-alphanumeric chars ---
            cleaned_words = re.sub(r'[^a-z0-9]', ' ', seg).split()
            for word in cleaned_words:
                if word in keywords:
                    # Guard against false positives like product names
                    if word == 'bag':
                        product_bag_words = {'sleeping', 'tote', 'hand', 'leather', 'backpack', 'shoulder', 'crossbody', 'travel', 'duffel', 'laptop'}
                        if any(w in cleaned_words for w in product_bag_words):
                            continue
                    return True

            # --- Pass 2: keyword substring match for compound segments like 'viewcart', 'mycart' ---
            for kw in ('cart', 'checkout', 'basket', 'payment'):
                if kw in seg:
                    # Skip known false-positive patterns
                    fp_set = keyword_fp_patterns.get(kw, set())
                    if any(fp in seg for fp in fp_set):
                        continue
                    self.logger.debug(f"_is_cart_or_checkout_url: segment '{seg}' contains keyword '{kw}' (substring match)")
                    return True
                    
        # Check for special URL substrings like gp/cart, gp/buy, or subdomains
        if '/gp/cart' in url_lower or '/gp/buy' in url_lower:
            return True
            
        return False

    def _is_checkout_url(self, url: str) -> bool:
        """
        Verify if the URL corresponds to an actual checkout page (past the cart page).
        Excludes cart/bag/basket/viewcart pages.
        """
        url_lower = url.lower()
        clean_url = url_lower.split('?')[0].split('#')[0]
        
        segments = [seg for seg in clean_url.split('/') if seg]
        checkout_keywords = {'checkout', 'address', 'shipping', 'delivery', 'payment', 'order', 'login', 'otp', 'signin'}
        cart_keywords = {'cart', 'bag', 'basket', 'viewcart'}
        
        for seg in segments:
            cleaned_words = re.sub(r'[^a-z0-9]', ' ', seg).split()
            for word in cleaned_words:
                if word in checkout_keywords:
                    if word not in cart_keywords:
                        return True
            for kw in ('checkout', 'payment', 'address', 'shipping', 'delivery', 'login', 'otp'):
                if kw in seg:
                    if not any(cw in seg for cw in cart_keywords):
                        return True
                        
        if '/checkout' in url_lower or '/shipping' in url_lower or '/payment' in url_lower:
            if not any(cw in url_lower for cw in ('/cart', '/viewcart', '/bag', '/basket')):
                return True
                
        return False

    async def _navigate_to_cart(self, task: Task) -> Result:
        """Find and click cart icon/button using See-Think-Act strategy.
        
        If the previous add_to_cart task detected a post-add cart state
        (e.g. Amazon side panel, Flipkart minicart drawer), we act on it
        immediately WITHOUT burning time on a generic DOM search.
        """
        self.logger.info("Navigating to cart...")
        
        # ── FAST PATH: Consume cart_state from add_to_cart result ──────────────
        cart_state = task.context.get('cart_state')
        if cart_state and cart_state.get('state') not in (None, 'unknown'):
            self.logger.info(f"🛒 Cart state from add_to_cart: '{cart_state['state']}' — acting immediately")
            fast_result = await self._act_on_cart_state(cart_state, task)
            if fast_result.success:
                return fast_result
            self.logger.warning("Fast-path cart state action failed, falling back to full search...")
 
        # FIRST: Check if already on checkout page
        current_url = self.page.url.lower()
        if self._is_checkout_url(current_url):
            self.logger.info(f"Already on checkout page: {current_url}")
            await self.status.show("✓ Already on checkout page", "success", 1500)
            await asyncio.sleep(0.5)
            return Result(success=True, task=task, data={'method': 'already_on_checkout_page'})

        # ── STEP 0: Direct URL navigation ─────────────────────────────────────
        # For Shopify-based and standard sites, navigating directly to /checkout
        # after a successful ATC is the most reliable path — no drawer fighting.
        # The cart lives server-side (session/cookie), so URL navigation preserves it.
        self.logger.info("Step 0: Trying direct checkout URL navigation...")
        _orig_url = self.page.url
        try:
            _base = '/'.join(_orig_url.split('/')[:3])
            for _direct_url in [f"{_base}/checkout", f"{_base}/cart"]:
                try:
                    self.logger.info(f"  → {_direct_url}")
                    await self.page.goto(_direct_url, wait_until='domcontentloaded', timeout=12000)
                    await asyncio.sleep(1.5)
                    _cur = self.page.url.lower()
                    if self._is_checkout_url(_cur):
                        self.logger.info(f"✅ Step 0 succeeded: {_cur}")
                        await self.status.show("✓ Navigated to checkout!", "success", 1500)
                        return Result(success=True, task=task, data={'method': 'step0_direct_checkout_url'})
                    elif self._is_cart_or_checkout_url(_cur):
                        self.logger.info(f"Step 0 landed on cart page: {_cur}")
                        _cr = await self._handle_cart_page_checkout(task)
                        if _cr and _cr.success:
                            return _cr
                        break  # on cart page but checkout failed — stop URL loop
                    else:
                        self.logger.debug(f"  Step 0 URL {_direct_url} → {_cur} (not cart/checkout)")
                        # Restore original page if we navigated away unexpectedly
                        if _cur != _orig_url.lower():
                            try:
                                await self.page.goto(_orig_url, wait_until='domcontentloaded', timeout=10000)
                                await asyncio.sleep(1)
                            except Exception:
                                pass
                except Exception as _ue:
                    self.logger.debug(f"  Step 0 {_direct_url} failed: {_ue}")
        except Exception as _e0:
            self.logger.warning(f"Step 0 direct URL attempt failed: {_e0}")

        await self.status.show("🔍 Analyzing page state...", "info", 2000)
        await asyncio.sleep(0.5)  # Let message render before action
        
        # 1. SEE: Capture state before action
        try:
            await self.status.show("📸 Taking screenshot for analysis", "info", 1500)
            await asyncio.sleep(0.5)  # Let message render before screenshot
            timestamp = int(asyncio.get_event_loop().time())
            await self.page.screenshot(path=f"debug_nav_cart_pre_{timestamp}.png")
        except: pass

        # 2. THINK: Is a "Checkout" button ALREADY visible? (e.g. Post-Add modal)
        # We check this BEFORE clearing distractions, as the modal might be considered a distraction
        await self.status.show("🔍 Checking for checkout buttons...", "info", 2000)
        await asyncio.sleep(0.5)  # Let message render before searching
        checkout_btn_selectors = [
            # ── Reliable href/attribute selectors (highest priority) ──────────
            'a[href*="/checkout"]',
            'a[href*="checkout.shopify"]',
            'a[href*="secure.checkout"]',
            'button[name="checkout"]',
            'input[name="checkout"]',
            'button[onclick*="checkout"]',
            '#atb_checkout_checkout_button',
            '[name="proceedToRetailCheckout"]',
            # ── Cart drawer specific ─────────────────────────────────────────
            '#v3CartDrawer a[href*="checkout"]',
            '#v3CartDrawer button[name="checkout"]',
            '[id*="cartDrawer"] a[href*="checkout"]',
            '[id*="cart-drawer"] a[href*="checkout"]',
            '[class*="cart-drawer"] a[href*="checkout"]',
            '[class*="CartDrawer"] a[href*="checkout"]',
            '[class*="minicart"] a[href*="checkout"]',
            '[class*="mini-cart"] a[href*="checkout"]',
            # ── button/a with exact checkout text (NOT div/span) ─────────────
            'button:has-text("Checkout")',
            'button:has-text("Check Out")',
            'button:has-text("Proceed to Checkout")',
            'button:has-text("Proceed to Pay")',
            'button:has-text("Continue to Payment")',
            'button:has-text("Secure Checkout")',
            'button:has-text("Place Order")',
            'button:has-text("Process Order")',
            'a:has-text("Checkout")',
            'a:has-text("Check Out")',
            'a:has-text("Proceed to Checkout")',
            'a:has-text("Proceed to Pay")',
            'a:has-text("Continue to Payment")',
            'a:has-text("Secure Checkout")',
            'a:has-text("Buy Now")',
        ]
        
        self.logger.info("Checking for immediate 'Checkout' buttons (modals/popups)...")
        for selector in checkout_btn_selectors:
            try:
                # We want visible buttons only
                elems = await self.page.query_selector_all(selector)
                for elem in elems:
                    if elem and await elem.is_visible():
                        self.logger.info(f"👉 Found immediate checkout button: {selector}")
                        await self.status.show("✓ Found checkout button - clicking!", "success", 2000)
                        await asyncio.sleep(0.5)  # Let message render before click
                        await self._visualize_click_element(elem)
                        # Bypass pointer-event interception (e.g. cart drawer progress bar overlay)
                        clicked_ok = False
                        try:
                            await elem.click(timeout=3000)
                            clicked_ok = True
                        except Exception:
                            pass
                        if not clicked_ok:
                            try:
                                # Force JS click — bypasses overlays that intercept pointer events
                                await self.page.evaluate(
                                    "el => { el.style.pointerEvents='auto'; el.click(); }",
                                    elem
                                )
                                clicked_ok = True
                            except Exception as je:
                                self.logger.warning(f"JS click fallback failed: {je}")
                        if not clicked_ok:
                            await self._safe_click_element(elem)

                        # Smart wait: URL change or DOM settle
                        await self.smart_waiter.wait_after_click(self.page.url, timeout=8.0)

                        # Persistent Wait for Navigation
                        for _ in range(3):
                            current_url = self.page.url.lower()
                            if self._is_checkout_url(current_url):
                                self.logger.info(f"Navigation confirmed directly to checkout: {current_url}")
                                await self.status.show("✓ Checkout page reached", "success", 2000)
                                return Result(success=True, task=task, data={'method': 'immediate_modal_click'})
                            elif self._is_cart_or_checkout_url(current_url):
                                self.logger.info("Landed on cart page, proceeding to click checkout button on cart page...")
                                cart_res = await self._handle_cart_page_checkout(task)
                                if cart_res and cart_res.success:
                                    return cart_res
                            # If not navigated, try clicking again if visible
                            if await elem.is_visible():
                                self.logger.info("Navigation stalled, retrying click...")
                                await self._safe_click_element(elem)
                            await self.smart_waiter.wait_after_click(self.page.url, timeout=5.0)

                        self.logger.warning("Clicked immediate button but URL did not change.")
            except Exception: continue

        # 3. CLEAN: If no immediate checkout, clear distractions (popups covering minicart)
        self.logger.info("No immediate checkout found. Clearing distractions...")
        await self.status.show("🧹 Clearing popups and overlays...", "info", 2000)
        await asyncio.sleep(0.5)  # Let message render before clearing
        await self.distraction_manager.clear_distractions()
        await asyncio.sleep(1)

        # 4. SEARCH: Look for Cart/Bag Icons (Minicart)
        await self.status.show("🛒 Searching for cart icon...", "info", 2000)
        await asyncio.sleep(0.5)  # Let message render before searching
        cart_selectors = [
            'a[href*="cart"]:visible', 'a[href*="bag"]:visible',
            'button[aria-label*="cart" i]', 'button[aria-label*="bag" i]',
            'div[class*="cart" i]', 'div[class*="bag" i]',
            'span[class*="cart" i]', 'span[class*="bag" i]',
            'div:has-text("Cart")', 'div:has-text("Bag")',
            'span:has-text("Cart")', 'span:has-text("Bag")',
            '.minicart-link', '.mini-cart', '.cart-icon', '.desktop-cart', '.bag-icon',
            '.show-cart', 'a[title*="Cart"]', 'a[title*="Bag" i]',
            'header .cart', 'header .bag', '.cart-link', '.bag-link'
        ]
        
        self.logger.info("Searching for Cart/Bag icons...")

        # Extended checkout selectors for drawer (non-Shopify sites use absolute URLs)
        drawer_checkout_selectors = list(checkout_btn_selectors) + [
            'a[href*="checkout"]',   # catches https://checkout.allbirds.com/... etc.
            'button[type="submit"]:has-text("Checkout")',
            'button[type="submit"]:has-text("Check Out")',
            '[data-testid*="checkout"]',
            '[data-action*="checkout"]',
        ]

        viewport_w = (self.page.viewport_size or {}).get('width', 1280)
        viewport_h_px = (self.page.viewport_size or {}).get('height', 800)

        for selector in cart_selectors:
            try:
                elems = await self.page.query_selector_all(selector)
                for elem in elems:
                    if not elem or not await elem.is_visible():
                        continue

                    # Skip elements whose CENTER is outside the visible viewport
                    # (Using center-point, not left-edge, prevents x=1280 width=500 → center=1530 off-screen)
                    try:
                        box = await elem.bounding_box()
                        if box:
                            cx = box['x'] + box['width'] / 2
                            cy = box['y'] + box['height'] / 2
                            if cx < 0 or cx > viewport_w or cy < 0 or cy > viewport_h_px:
                                self.logger.debug(f"Skipping off-screen cart element (center={cx:.0f},{cy:.0f}): {selector}")
                                continue
                    except Exception:
                        pass

                    self.logger.info(f"👉 Found visible cart element: {selector}")
                    await self.status.show("✓ Found cart - scrolling into view", "success", 1500)
                    await asyncio.sleep(0.5)
                    await self.scroller.scroll_to_element(elem)
                    await self.status.show("🖱️ Clicking cart icon...", "info", 1500)
                    await asyncio.sleep(0.5)
                    await self._visualize_click_element(elem)
                    await self._safe_click_element(elem)

                    # Wait for potential minicart drawer to open (animated drawers need 2s+)
                    await self.smart_waiter.wait_after_click(self.page.url, timeout=5.0)
                    await asyncio.sleep(2.0)  # extra settle for animated drawers

                    # ── JS-based drawer checkout finder ────────────────────────────────
                    # More reliable than CSS :has-text selectors — finds any visible
                    # link/button whose text or href contains "checkout"
                    _drawer_btn = None
                    try:
                        _drawer_btn = await self.page.evaluate("""
                        () => {
                            const els = document.querySelectorAll('a, button, [role="button"]');
                            const candidates = [];
                            for (const el of els) {
                                const rect = el.getBoundingClientRect();
                                if (rect.width < 1 || rect.height < 1) continue;
                                const style = window.getComputedStyle(el);
                                if (style.display === 'none' || style.visibility === 'hidden' ||
                                    parseFloat(style.opacity) < 0.1) continue;
                                const href = (el.getAttribute('href') || '').toLowerCase();
                                const text = (el.textContent || el.getAttribute('aria-label') || '').trim().toLowerCase();
                                const isCheckout = href.includes('checkout') ||
                                    text === 'checkout' || text === 'check out' ||
                                    /^(checkout|check out)[\s→►>]/.test(text) ||
                                    text.startsWith('proceed to checkout') ||
                                    text.startsWith('go to checkout');
                                if (!isCheckout) continue;
                                candidates.push({
                                    tag: el.tagName,
                                    text: el.textContent.trim().substring(0, 80),
                                    href: el.getAttribute('href') || '',
                                    x: Math.round(rect.left + rect.width / 2),
                                    y: Math.round(rect.top + rect.height / 2),
                                    area: rect.width * rect.height
                                });
                            }
                            candidates.sort((a, b) => a.area - b.area);
                            return candidates[0] || null;
                        }
                        """)
                    except Exception as _jse:
                        self.logger.debug(f"JS drawer scan failed: {_jse}")

                    if _drawer_btn:
                        self.logger.info(
                            f"JS found drawer checkout: {_drawer_btn['tag']} "
                            f"'{_drawer_btn['text']}' href='{_drawer_btn['href']}'"
                        )
                        await self.status.show("✓ Found drawer checkout — navigating", "success", 1500)
                        _dbase = '/'.join(self.page.url.split('/')[:3])
                        _dhref = _drawer_btn['href']
                        navigated = False
                        # Prefer href navigation for anchors — bypasses pointer-event overlays
                        if _dhref and 'checkout' in _dhref.lower():
                            if _dhref.startswith('/'):
                                _dhref = _dbase + _dhref
                            try:
                                await self.page.goto(_dhref, wait_until='domcontentloaded', timeout=15000)
                                navigated = True
                            except Exception:
                                pass
                        if not navigated:
                            # Mouse click at JS-detected coordinates
                            try:
                                await self.page.mouse.move(_drawer_btn['x'], _drawer_btn['y'])
                                await self.page.mouse.click(_drawer_btn['x'], _drawer_btn['y'])
                            except Exception:
                                pass
                        await self.smart_waiter.wait_after_click(self.page.url, timeout=8.0)
                        _dc_url = self.page.url.lower()
                        if self._is_checkout_url(_dc_url):
                            self.logger.info(f"✅ JS drawer checkout navigation succeeded: {_dc_url}")
                            await self.status.show("✓ Checkout page reached", "success", 2000)
                            return Result(success=True, task=task, data={'method': 'js_drawer_checkout_nav'})
                        elif self._is_cart_or_checkout_url(_dc_url):
                            cart_res = await self._handle_cart_page_checkout(task)
                            if cart_res and cart_res.success:
                                return cart_res
                    else:
                        # CSS selector fallback for drawer buttons
                        for btn_sel in drawer_checkout_selectors:
                            try:
                                btns = await self.page.query_selector_all(btn_sel)
                                for btn in btns:
                                    if not btn or not await btn.is_visible():
                                        continue
                                    self.logger.info(f"Found checkout button in drawer: {btn_sel}")
                                    await self.status.show("✓ Found drawer checkout button", "success", 1500)
                                    try:
                                        await self.page.evaluate(
                                            "el => { el.style.pointerEvents='auto'; el.click(); }",
                                            btn
                                        )
                                    except Exception:
                                        await self._safe_click_element(btn)
                                    for _ in range(4):
                                        await asyncio.sleep(2)
                                        current_url = self.page.url.lower()
                                        if self._is_checkout_url(current_url):
                                            self.logger.info(f"Navigation confirmed via drawer: {current_url}")
                                            await self.status.show("✓ Checkout page reached", "success", 2000)
                                            return Result(success=True, task=task, data={'method': 'minicart_drawer_click'})
                                        if self._is_cart_or_checkout_url(current_url):
                                            cart_res = await self._handle_cart_page_checkout(task)
                                            if cart_res and cart_res.success:
                                                return cart_res
                                        if await btn.is_visible():
                                            await self._safe_click_element(btn)
                            except Exception:
                                continue

                    # Navigated via cart icon click — check success
                    current_url_after = self.page.url.lower()
                    if self._is_checkout_url(current_url_after):
                        return Result(success=True, task=task, data={'method': 'dom_selector_click'})
                    elif self._is_cart_or_checkout_url(current_url_after):
                        cart_res = await self._handle_cart_page_checkout(task)
                        if cart_res and cart_res.success:
                            return cart_res

                    # Wait a bit more and check again
                    await asyncio.sleep(2)
                    current_url_after = self.page.url.lower()
                    if self._is_checkout_url(current_url_after):
                        return Result(success=True, task=task, data={'method': 'dom_selector_click'})
                    elif self._is_cart_or_checkout_url(current_url_after):
                        cart_res = await self._handle_cart_page_checkout(task)
                        if cart_res and cart_res.success:
                            return cart_res

                    self.logger.warning(f"Clicked cart icon but URL is still not cart/checkout: {current_url_after}")
            except Exception as e:
                self.logger.warning(f"Failed to click cart selector {selector}: {e}")
                continue

        # 4.5 DOM INTELLIGENCE: If DOM selectors failed, scan full page structure
        self.logger.info("4.5: DOM Intelligence scan for cart/checkout navigation...")
        try:
            dom_intel = DOMIntelligence(self.page, self.logger)
            await dom_intel.analyze()
            dom_intel.log_page_report()

            # Try checkout first (proceed to checkout / place order)
            for intent in ("proceed_to_checkout", "checkout", "go_to_cart"):
                match = dom_intel.find_best_match(intent)
                if match and not match.element.is_in_header:
                    self.logger.info(
                        f"DOM Intel recommends '{intent}': {match.element.tag} "
                        f"'{match.element.text[:50]}' @ ({match.element.x},{match.element.y}) "
                        f"score={match.score:.1f}"
                    )
                    await self._visualize_click(match.element.x, match.element.y)
                    # Try href navigation first (most reliable for cart links)
                    if match.element.href and any(
                        kw in match.element.href.lower()
                        for kw in ('/cart', '/checkout', '/bag', '/basket', '/order')
                    ):
                        await dom_intel.navigate_href(match)
                    else:
                        await dom_intel.click(match)
                    await self.smart_waiter.wait_after_click(self.page.url, timeout=8.0)
                    current_url_di = self.page.url.lower()
                    if self._is_checkout_url(current_url_di):
                        self.logger.info(f"✅ DOM Intelligence navigation succeeded: {current_url_di}")
                        return Result(success=True, task=task, data={'method': f'dom_intelligence_{intent}'})
                    elif self._is_cart_or_checkout_url(current_url_di):
                        self.logger.info("DOM Intelligence landed on cart page, proceeding to click checkout button on cart page...")
                        cart_res = await self._handle_cart_page_checkout(task)
                        if cart_res and cart_res.success:
                            return cart_res
                    self.logger.warning(f"DOM Intel click for '{intent}' did not navigate to cart")
        except Exception as dom_nav_err:
            self.logger.warning(f"DOM Intelligence nav scan failed: {dom_nav_err}")

        # 5. VLM FALLBACK: If DOM failed, use vision to "see" the checkout button
        self.logger.info("DOM search for Cart/Checkout failed. Invoking VLM Vision...")

        await self.status.show("👁️ Cart navigation failed - using vision...", "info", 3000)
        
        try:
            import tempfile as _tmpA
            _sA = await self.page.screenshot(type="png")
            with _tmpA.NamedTemporaryFile(suffix=".png", delete=False) as _fA:
                _fA.write(_sA); _fA_path = _fA.name
            _rA = self._extract_json(
                await self.llm.complete_with_vision(
                    "Find the Cart icon or Checkout button. Return JSON: {x: <int_pixels>, y: <int_pixels>}.",
                    _fA_path
                ) or ""
            )
        except Exception as _eA:
            self.logger.warning(f"VLM nav fallback error: {_eA}")
            _rA = {}
        if _rA.get('x') and _rA.get('y'):
            coords = _rA
            pixel_x = float(coords['x'])
            pixel_y = float(coords['y'])
            if pixel_x <= 1.0 and pixel_y <= 1.0:
                vp = self.page.viewport_size or {'width': 1280, 'height': 800}
                pixel_x = pixel_x * vp['width']
                pixel_y = pixel_y * vp['height']
        if _rA.get('x') and _rA.get('y'):
            
            self.logger.info(f"VLM found cart target at ({pixel_x}, {pixel_y})")
            await self._visualize_click(pixel_x, pixel_y)
            await self.page.mouse.move(pixel_x, pixel_y, steps=10)
            await self.page.mouse.click(pixel_x, pixel_y)

            # Smart wait for navigation
            await self.smart_waiter.wait_after_click(self.page.url, timeout=8.0)
            current_url = self.page.url.lower()
            if self._is_checkout_url(current_url):
                self.logger.info(f"VLM Navigation successful: {current_url}")
                return Result(success=True, task=task, data={'method': 'vlm_navigation'})
            elif self._is_cart_or_checkout_url(current_url):
                self.logger.info("VLM landed on cart page, proceeding to click checkout button on cart page...")
                cart_res = await self._handle_cart_page_checkout(task)
                if cart_res and cart_res.success:
                    return cart_res

        # 6. RECOVERY: Fallback to direct URL
        self.logger.info("⚠️ VLM/DOM Interaction failed. Executing URL Fallback Strategy.")
        await self.status.show("⚠️ Using URL navigation fallback...", "info", 3000)
        await asyncio.sleep(0.5)  # Let message render before attempting
        try:
            current_url = self.page.url
            clean_url = current_url.split('?')[0].split('#')[0]
            base_url = '/'.join(clean_url.split('/')[:3]) # https://site.com
            
            # Try /checkout first, then /cart
            fallback_urls = [f"{base_url}/checkout", f"{base_url}/cart", f"{base_url}/bag"]
            
            for url in fallback_urls:
                self.logger.info(f"Trying fallback URL: {url}")
                await self.status.show(f"🔗 Navigating to {url.split('/')[-1]}...", "info", 2000)
                await asyncio.sleep(0.5)  # Let message render before navigation
                response = await self.page.goto(url, wait_until='domcontentloaded', timeout=15000)
                if response and response.status < 400:
                    await asyncio.sleep(2)
                    current_url_after = self.page.url.lower()
                    if not self._is_cart_or_checkout_url(current_url_after):
                        self.logger.warning(f"URL fallback went to {url} but redirected to non-cart/checkout URL: {current_url_after}")
                        continue
                    
                    if self._is_checkout_url(current_url_after):
                        await self.status.show("✓ Navigation successful!", "success", 2000)
                        return Result(success=True, task=task, data={'method': 'url_fallback', 'url': url})
                    elif self._is_cart_or_checkout_url(current_url_after):
                        self.logger.info("URL fallback landed on cart page, proceeding to click checkout button on cart page...")
                        cart_res = await self._handle_cart_page_checkout(task)
                        if cart_res and cart_res.success:
                            return cart_res
        except Exception as e:
            self.logger.error(f"URL Fallback failed: {e}")

        # Final Failure
        await self.status.show("✗ Cart navigation failed", "error", 3000)
        timestamp = int(asyncio.get_event_loop().time())
        await self.page.screenshot(path=f"failure_nav_cart_{timestamp}.png")
        return Result(success=False, task=task, error="Could not navigate to cart/checkout via Modal, Icon, or URL")
    async def _is_checkout_or_login_wall(self) -> bool:
        """
        Check if we have reached either a checkout page or the login/guest wall.
        """
        current_url = self.page.url.lower()
        if self._is_checkout_url(current_url):
            return True
            
        # Check if we are on a login/guest wall (even if URL didn't change)
        login_indicators = [
            "input[type='password']",
            "input[name*='password' i]",
            "input[type='email']",
            "input[name*='email' i]",
            "button:has-text('Continue as Guest')",
            "button:has-text('Guest Checkout')",
            "button:has-text('Checkout as Guest')",
            "button:has-text('Continue without account')",
            "text=Sign In",
            "text=Log In"
        ]
        for indicator in login_indicators:
            try:
                if await self.page.is_visible(indicator, timeout=100):
                    return True
            except:
                pass
        return False

    async def _handle_cart_page_checkout(self, task: Task) -> Optional[Result]:
        """
        Handles clicking the checkout button when we are on a cart page.
        Returns a successful Result if we reach the checkout page or login wall, otherwise None.
        """
        checkout_btn_selectors = [
            'a[href*="/checkout"]', 'button[onclick*="checkout"]',
            'a:has-text("Checkout")', 'button:has-text("Checkout")',
            'div:has-text("Checkout")', 'span:has-text("Checkout")',
            'a:has-text("Check Out")', 'button:has-text("Check Out")', 
            'div:has-text("Check Out")', 'span:has-text("Check Out")', 
            'a:has-text("Proceed to Checkout")', 'button:has-text("Proceed to Checkout")',
            'div:has-text("Proceed to Checkout")', 'span:has-text("Proceed to Checkout")',
            'a:has-text("Place Order")', 'button:has-text("Place Order")',
            'div:has-text("Place Order")', 'span:has-text("Place Order")',
            'a:has-text("Proceed to Pay")', 'button:has-text("Proceed to Pay")',
            'div:has-text("Proceed to Pay")', 'span:has-text("Proceed to Pay")',
            'a:has-text("Continue to Payment")', 'button:has-text("Continue to Payment")',
            'a:has-text("Secure Checkout")', 'button:has-text("Secure Checkout")',
            'a:has-text("Buy Now")', 'button:has-text("Buy Now")',
            'div:has-text("Buy Now")', 'span:has-text("Buy Now")',
            'a:has-text("Process Order")', 'button:has-text("Process Order")',
            'div:has-text("Process Order")', 'span:has-text("Process Order")',
            'a:has-text("Continue")', 'button:has-text("Continue")',
            'div:has-text("Continue")', 'span:has-text("Continue")',
            '#atb_checkout_checkout_button', '[name="proceedToRetailCheckout"]'
        ]
        
        current_url = self.page.url.lower()
        if not self._is_checkout_url(current_url) and self._is_cart_or_checkout_url(current_url):
            self.logger.info("Landed on cart page, looking for checkout/place order button to proceed to checkout...")
            
            # Check if cart is blank first
            blank = await self._is_cart_blank()
            if blank:
                self.logger.warning("Arrived at an EMPTY cart page (blank_cart_detected).")
                return Result(success=False, task=task, error="blank_cart_detected")
                
            for btn_sel in checkout_btn_selectors:
                try:
                    btns = await self.page.query_selector_all(btn_sel)
                    for btn in btns:
                        if btn and await btn.is_visible():
                            self.logger.info(f"👉 Clicking checkout button on cart page: {btn_sel}")
                            await self._visualize_click_element(btn)
                            await btn.click()
                            
                            # Wait up to 5 seconds for page transition/navigation
                            for _ in range(10):
                                await asyncio.sleep(0.5)
                                if await self._is_checkout_or_login_wall():
                                    self.logger.info("Successfully reached checkout page or login wall after cart page click.")
                                    return Result(success=True, task=task, data={'method': 'cart_page_checkout_click', 'selector': btn_sel})
                except Exception as e:
                    self.logger.warning(f"Failed to click cart page checkout button {btn_sel}: {e}")
            
            # If we couldn't find/click any button, but we are on a cart page, wait 2 seconds and try one more time
            await asyncio.sleep(2)
            for btn_sel in checkout_btn_selectors:
                try:
                    btns = await self.page.query_selector_all(btn_sel)
                    for btn in btns:
                        if btn and await btn.is_visible():
                            self.logger.info(f"👉 Clicking checkout button on cart page (retry): {btn_sel}")
                            await self._visualize_click_element(btn)
                            await btn.click()
                            for _ in range(10):
                                await asyncio.sleep(0.5)
                                if await self._is_checkout_or_login_wall():
                                    self.logger.info("Successfully reached checkout page or login wall after cart page click (retry).")
                                    return Result(success=True, task=task, data={'method': 'cart_page_checkout_click_retry', 'selector': btn_sel})
                except Exception:
                    continue
                    
        return None

    async def _act_on_cart_state(self, cart_state: Dict[str, Any], task: Task) -> Result:
        """
        Intelligently act on the cart state detected after Add to Cart.
        
        cart_state comes from VariantSelectionAgent._detect_cart_state() and contains:
          - state: 'modal_checkout_visible' | 'modal_view_cart_visible' |
                   'minicart_updated' | 'navigated_to_cart' | 'toast_success'
          - checkout_button_selector: CSS selector for a direct checkout button (if known)
          - cart_button_selector: CSS selector for a 'View/Go to Cart' button (if known)
        """
        state = cart_state.get('state', 'unknown')
        self.logger.info(f"Acting on cart state: '{state}'")
        await self.status.show(f"🛒 Cart state: {state} — navigating...", "info", 2000)

        # ── Already on checkout page ──────────────────────────────────────────
        if self._is_checkout_url(self.page.url):
            return Result(success=True, task=task, data={'method': 'cart_state_already_on_checkout'})

        # ── Modal with direct Checkout CTA ────────────────────────────────────
        if state == 'modal_checkout_visible':
            selector = cart_state.get('checkout_button_selector')
            candidates = [
                selector,
                '#attach-sidesheet-checkout-button',  # Amazon drawer Proceed to checkout
                '#sw-ptc-buy-box',                    # Amazon post-add box Proceed to checkout
                '[name="proceedToRetailCheckout"]',
                '#attachSiNoCoverage-announce',
                'a[href*="signin/handlers/continue.html"]',
                '[name="checkout"]',
                '[name="goto_checkout"]',
                'form[action="/cart"] button[type="submit"]',
                'button:has-text("Proceed to Checkout")',
                'a:has-text("Proceed to Checkout")',
                'div:has-text("Proceed to Checkout")',
                'span:has-text("Proceed to Checkout")',
                'button:has-text("Checkout")',
                'a:has-text("Checkout")',
                'div:has-text("Checkout")',
                'span:has-text("Checkout")',
                'a[href*="/checkout"]'
            ]
            for sel in [s for s in candidates if s]:
                try:
                    elem = await self.page.query_selector(sel)
                    if elem and await elem.is_visible():
                        self.logger.info(f"Clicking checkout button via selector: {sel}")
                        await self.scroller.scroll_to_element(elem)
                        await self._visualize_click_element(elem)
                        await elem.click()
                        await self.smart_waiter.wait_after_click(self.page.url, timeout=6.0)
                        current_url = self.page.url.lower()
                        if self._is_checkout_url(current_url) or '/order' in current_url:
                            return Result(success=True, task=task, data={'method': 'cart_state_modal_checkout', 'selector': sel})
                        elif self._is_cart_or_checkout_url(current_url):
                            cart_res = await self._handle_cart_page_checkout(task)
                            if cart_res and cart_res.success:
                                return cart_res
                except Exception as e:
                    self.logger.warning(f"Checkout selector click failed for {sel}: {e}")

        # ── Modal with View/Go to Cart CTA ────────────────────────────────────
        if state == 'modal_view_cart_visible':
            # ── JS-based: find the SMALLEST element with exactly "Go to Cart" / "View Cart"
            #    text. This avoids Playwright's :has-text() matching huge container divs.
            try:
                js_gtc = await self.page.evaluate("""
                () => {
                    const terms = ['go to cart', 'view cart', 'go to bag', 'view bag'];
                    const allEls = document.querySelectorAll('div, span, button, a');
                    let best = null;
                    let bestArea = Infinity;
                    for (const el of allEls) {
                        const text = el.textContent.trim().toLowerCase();
                        if (!terms.some(t => text === t)) continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width < 1 || rect.height < 1) continue;
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') continue;
                        const area = rect.width * rect.height;
                        if (area < bestArea) {
                            bestArea = area;
                            best = {
                                tag: el.tagName,
                                x: Math.round(rect.left + rect.width / 2),
                                y: Math.round(rect.top + rect.height / 2),
                                w: Math.round(rect.width),
                                h: Math.round(rect.height),
                                href: el.tagName === 'A' ? el.href : null,
                                text: el.textContent.trim()
                            };
                        }
                    }
                    return best;
                }
                """)
                if js_gtc:
                    self.logger.info(f"JS leaf GTC found: {js_gtc['tag']} '{js_gtc['text']}' at ({js_gtc['x']}, {js_gtc['y']}) size=({js_gtc['w']}x{js_gtc['h']})")
                    # If it's an anchor with href, navigate directly
                    if js_gtc.get('href') and '/cart' in js_gtc['href'].lower():
                        self.logger.info(f"Navigating directly to cart URL: {js_gtc['href']}")
                        await self.page.goto(js_gtc['href'])
                        await self.smart_waiter.wait_after_click(self.page.url, timeout=6.0)
                        if self._is_checkout_url(self.page.url):
                            return Result(success=True, task=task, data={'method': 'js_gtc_href_nav'})
                        elif self._is_cart_or_checkout_url(self.page.url):
                            cart_res = await self._handle_cart_page_checkout(task)
                            if cart_res and cart_res.success:
                                return cart_res
                    # Dispatch a click event (bypasses iframe intercept)
                    dispatch_ok = await self.page.evaluate("""
                    () => {
                        const terms = ['go to cart', 'view cart', 'go to bag', 'view bag'];
                        const allEls = document.querySelectorAll('div, span, button, a');
                        let best = null;
                        let bestArea = Infinity;
                        for (const el of allEls) {
                            const text = el.textContent.trim().toLowerCase();
                            if (!terms.some(t => text === t)) continue;
                            const rect = el.getBoundingClientRect();
                            if (rect.width < 1 || rect.height < 1) continue;
                            const style = window.getComputedStyle(el);
                            if (style.display === 'none' || style.visibility === 'hidden') continue;
                            const area = rect.width * rect.height;
                            if (area < bestArea) {
                                bestArea = area;
                                best = el;
                            }
                        }
                        if (best) {
                            best.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                            return true;
                        }
                        return false;
                    }
                    """)
                    if dispatch_ok:
                        self.logger.info("JS dispatchEvent click on GTC button sent")
                        await self.smart_waiter.wait_after_click(self.page.url, timeout=6.0)
                        if self._is_checkout_url(self.page.url):
                            return Result(success=True, task=task, data={'method': 'js_gtc_dispatch'})
                        elif self._is_cart_or_checkout_url(self.page.url):
                            cart_res = await self._handle_cart_page_checkout(task)
                            if cart_res and cart_res.success:
                                return cart_res
                        # If dispatch click didn't navigate, try mouse click at coordinates
                        await self.page.mouse.click(js_gtc['x'], js_gtc['y'])
                        await self.smart_waiter.wait_after_click(self.page.url, timeout=6.0)
                        if self._is_checkout_url(self.page.url):
                            return Result(success=True, task=task, data={'method': 'js_gtc_mouse_click'})
                        elif self._is_cart_or_checkout_url(self.page.url):
                            cart_res = await self._handle_cart_page_checkout(task)
                            if cart_res and cart_res.success:
                                return cart_res
                        self.logger.warning(f"JS GTC click did not navigate to cart (URL: {self.page.url})")
            except Exception as js_err:
                self.logger.warning(f"JS leaf GTC approach failed: {js_err}")

            selector = cart_state.get('cart_button_selector')
            candidates = [
                selector,
                '#attach-sidesheet-view-cart-button', # Amazon drawer View Cart
                '#sw-gtc',                            # Amazon post-add View Cart
                'a[href*="/gp/cart/view.html"]',
                'a:has-text("View Cart")',
                'a:has-text("Go to Cart")',
                'button:has-text("View Cart")',
                'a:has-text("View Bag")',
                'a:has-text("Go to Bag")',
                'div:has-text("View Cart")',
                'div:has-text("Go to Cart")',
                'span:has-text("View Cart")',
                'span:has-text("Go to Cart")',
                'div:has-text("View Bag")',
                'div:has-text("Go to Bag")',
                'span:has-text("View Bag")',
                'span:has-text("Go to Bag")',
                '[data-action="view-cart"]'
            ]
            for sel in [s for s in candidates if s]:
                try:
                    elem = await self.page.query_selector(sel)
                    if elem and await elem.is_visible():
                        self.logger.info(f"Clicking 'View Cart' via: {sel}")
                        await self.scroller.scroll_to_element(elem)
                        await self._visualize_click_element(elem)
                        await elem.click()
                        
                        # Wait for transition
                        for _ in range(3):
                            await asyncio.sleep(1)
                            if self._is_checkout_url(self.page.url):
                                return Result(success=True, task=task, data={'method': 'cart_state_view_cart', 'selector': sel})
                            elif self._is_cart_or_checkout_url(self.page.url):
                                cart_res = await self._handle_cart_page_checkout(task)
                                if cart_res and cart_res.success:
                                    return cart_res
                except Exception as e:
                    self.logger.warning(f"View cart selector click failed for {sel}: {e}")

        # ── Toast/minicart: try clicking cart icon or badge ───────────────────
        if state in ('toast_success', 'minicart_updated'):
            self.logger.info("Toast/minicart success detected — searching for cart icon/badge to click...")
            await self.status.show("🛒 Item added! Clicking cart icon...", "info", 2000)
            
            cart_icon_selectors = [
                '#nav-cart',        # Amazon
                '#nav-cart-count',  # Amazon count badge
                'a[href*="/cart"]', 'a[href*="/bag"]',
                'button[aria-label*="cart" i]', 'button[aria-label*="bag" i]',
                '.minicart-link', '.cart-icon', '.header-cart'
            ]
            for sel in cart_icon_selectors:
                try:
                    elem = await self.page.query_selector(sel)
                    if elem and await elem.is_visible():
                        self.logger.info(f"Clicking cart icon: {sel}")
                        await self._visualize_click_element(elem)
                        await elem.click()
                        await self.smart_waiter.wait_after_click(self.page.url, timeout=6.0)
                        current_url = self.page.url.lower()
                        if self._is_checkout_url(current_url):
                            return Result(success=True, task=task, data={'method': 'cart_state_icon_click', 'selector': sel})
                        elif self._is_cart_or_checkout_url(current_url):
                            cart_res = await self._handle_cart_page_checkout(task)
                            if cart_res and cart_res.success:
                                return cart_res
                        # Might have opened a drawer — check for checkout buttons
                        for checkout_sel in ['button:has-text("Checkout")', 'a:has-text("Checkout")', 'a[href*="/checkout"]']:
                            btn = await self.page.query_selector(checkout_sel)
                            if btn and await btn.is_visible():
                                await btn.click()
                                await self.smart_waiter.wait_after_click(self.page.url, timeout=6.0)
                                if self._is_checkout_url(self.page.url):
                                    return Result(success=True, task=task, data={'method': 'cart_state_icon_then_checkout'})
                                elif self._is_cart_or_checkout_url(self.page.url):
                                    cart_res = await self._handle_cart_page_checkout(task)
                                    if cart_res and cart_res.success:
                                        return cart_res
                        break
                except Exception:
                    continue

        # Could not act — caller will fall back to full search
        return Result(success=False, task=task, error=f"Could not act on cart_state '{state}'")

    # ─────────────────────────────────────────────────────────────────────────
    # BLANK CART DETECTOR
    # ─────────────────────────────────────────────────────────────────────────
    async def _is_cart_blank(self) -> bool:
        """
        Returns True if the current cart/bag page appears to have zero items.
        Uses a broad set of selectors that cover Amazon, Shopify, and generic sites.
        """
        try:
            item_count = await self.page.evaluate("""
            () => {
                const selectors = [
                    '.sc-list-item',            // Amazon cart item
                    '[data-asin]',              // Amazon product reference
                    '.cart-item',               // Generic
                    '.basket-item',             // UK-style sites
                    '.line-item',               // Shopify
                    '[class*="CartItem"]',       // React-style components
                    '[class*="cart-item"]',
                    '[class*="bag-item"]',
                    '.order-product',           // Various checkout pages
                    '.product-item',
                    '[data-testid*="cart-item"]',
                    '[data-testid*="CartItem"]'
                ];
                for (const sel of selectors) {
                    try {
                        const els = document.querySelectorAll(sel);
                        if (els.length > 0) return els.length;
                    } catch(e) {}
                }
                return 0;
            }
            """)
            is_blank = (item_count == 0)
            self.logger.info(f"Cart blank check: {item_count} item(s) found → blank={is_blank}")
            return is_blank
        except Exception as e:
            self.logger.warning(f"Blank cart check failed: {e} — assuming not blank")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # VLM LOST-STATE DETECTOR & RECOVERY
    # ─────────────────────────────────────────────────────────────────────────
    async def _detect_current_state_and_recover(
        self,
        original_url: str,
        task: Task,
        attempt: int
    ) -> Result:
        """
        Uses VLM to assess what page the agent is currently on, then executes
        the appropriate recovery action.  Called by the orchestrator's recovery
        loop when add_to_cart or navigate_to_cart has failed.

        Possible VLM-classified states and recovery actions:
          PRODUCT_PAGE        → retry add_to_cart in-place (caller re-runs the flow)
          CART_PAGE           → already in cart (success, no action needed)
          EMPTY_CART_PAGE     → navigated to blank cart — go back to product URL
          SIGNIN_PAGE         → redirected to login — non-recoverable, report failure
          UNKNOWN_PAGE        → navigate directly to original_url
        """
        self.logger.info(f"🤔 Lost-State Recovery — attempt {attempt}/3 — assessing current page...")
        await self.status.show(f"🤔 Recovery attempt {attempt}/3 — assessing page...", "info", 3000)

        # 1. Screenshot
        screenshot_path = f"recovery_state_check_{attempt}.png"
        try:
            await self.page.screenshot(path=screenshot_path)
        except Exception as e:
            self.logger.error(f"Could not take recovery screenshot: {e}")
            # Blind recovery: just navigate to original URL
            await self._navigate_to_url(original_url)
            return Result(success=False, task=task, error=f"recovery_blind_navigate_attempt_{attempt}")

        # 2. VLM State Assessment
        current_url = self.page.url
        prompt = f"""
You are a web automation recovery assistant. The agent was trying to add a product to cart
on an e-commerce website but got confused and ended up somewhere unexpected.

Current URL: {current_url}
Original product URL: {original_url}

Look at the screenshot and classify the CURRENT PAGE into exactly ONE of these states:
- PRODUCT_PAGE   : A product detail page is visible (with title, price, color/size options, Add to Cart)
- CART_PAGE      : A shopping cart or bag page WITH items already in it
- EMPTY_CART_PAGE: A shopping cart or bag page that is EMPTY (no items)
- SIGNIN_PAGE    : A sign-in, login, or account creation page
- UNKNOWN_PAGE   : None of the above

Also answer: Is the Add to Cart button visible on screen right now? (yes/no)

Return ONLY valid JSON:
{{
    "page_state": "<one of: PRODUCT_PAGE, CART_PAGE, EMPTY_CART_PAGE, SIGNIN_PAGE, UNKNOWN_PAGE>",
    "atc_button_visible": <true/false>,
    "reasoning": "<1-2 sentence explanation of what you see>",
    "confidence": <float 0-1>
}}
"""
        page_state = 'UNKNOWN_PAGE'
        atc_visible = False
        try:
            if self.llm:
                if hasattr(self.llm, 'provider') and self.llm.provider == 'gemini':
                    vlm_model = os.getenv('GEMINI_MODEL', 'gemini-1.5-flash')
                else:
                    vlm_model = os.getenv('OPENROUTER_MODEL', 'qwen/qwen-2-vl-72b-instruct')
                response = await self.llm.complete_with_vision(prompt, screenshot_path, model=vlm_model)
                parsed = self._extract_json(response)
                page_state = parsed.get('page_state', 'UNKNOWN_PAGE')
                atc_visible = parsed.get('atc_button_visible', False)
                self.logger.info(
                    f"🤔 Recovery VLM says: page_state='{page_state}', "
                    f"atc_visible={atc_visible}, "
                    f"confidence={parsed.get('confidence')}, "
                    f"reasoning='{parsed.get('reasoning')}'"
                )
            else:
                self.logger.warning("No LLM client available for recovery assessment — defaulting to UNKNOWN_PAGE")
        except Exception as e:
            self.logger.warning(f"VLM recovery assessment failed: {e} — defaulting to UNKNOWN_PAGE")

        # 3. Act on classified state
        if page_state == 'CART_PAGE':
            # Item is already in cart — we're actually on the right track
            self.logger.info("✅ Recovery: VLM sees a CART_PAGE with items — proceeding as success")
            await self.status.show("✅ Recovery: Item already in cart!", "success", 2000)
            return Result(success=True, task=task, data={
                'method': 'recovery_already_in_cart',
                'page_state': page_state,
                'attempt': attempt
            })

        elif page_state == 'PRODUCT_PAGE':
            if atc_visible:
                self.logger.info("🔄 Recovery: On product page and ATC button visible — returning to retry ATC")
                await self.status.show("🔄 Recovery: Product page found, retrying Add to Cart...", "info", 2000)
            else:
                self.logger.info("🔄 Recovery: On product page but ATC not visible — returning to retry ATC with scroll")
                await self.status.show("🔄 Recovery: Scrolling to find Add to Cart...", "info", 2000)
            # Caller (orchestrator) will re-run add_to_cart
            return Result(success=False, task=task, error=f"recovery_retry_atc_attempt_{attempt}")

        elif page_state == 'EMPTY_CART_PAGE':
            self.logger.info("🔄 Recovery: EMPTY cart detected — navigating back to product URL")
            await self.status.show("🔄 Recovery: Empty cart — going back to product...", "info", 2000)
            nav_ok = await self._navigate_to_url(original_url)
            if nav_ok:
                return Result(success=False, task=task, error=f"recovery_returned_to_product_attempt_{attempt}")
            else:
                return Result(success=False, task=task, error=f"recovery_navigate_failed_attempt_{attempt}")

        elif page_state == 'SIGNIN_PAGE':
            self.logger.error("❌ Recovery: Agent landed on SIGN-IN page — cannot auto-recover without credentials")
            await self.status.show("❌ Sign-in required — cannot recover automatically", "error", 4000)
            return Result(success=False, task=task, error="recovery_signin_required")

        else:  # UNKNOWN_PAGE
            self.logger.info("🔄 Recovery: Unknown page — navigating directly to original product URL")
            await self.status.show("🔄 Recovery: Lost! Going back to product...", "info", 2000)
            nav_ok = await self._navigate_to_url(original_url)
            if nav_ok:
                return Result(success=False, task=task, error=f"recovery_returned_to_product_attempt_{attempt}")
            else:
                return Result(success=False, task=task, error=f"recovery_navigate_failed_attempt_{attempt}")

    async def _navigate_to_url(self, url: str) -> bool:
        """Navigate to a URL, first trying go_back(), then direct goto()."""
        # Try go_back() first (cheaper, preserves session cookies)
        try:
            current_url = self.page.url
            if current_url.lower() != url.lower():
                self.logger.info(f"Recovery navigate: trying page.go_back() first...")
                await self.page.go_back(wait_until='domcontentloaded', timeout=10000)
                await asyncio.sleep(1.5)
                # If back() landed on the product page, we're good
                if self.page.url.lower().rstrip('/') == url.lower().rstrip('/'):
                    self.logger.info("go_back() succeeded: back on product page")
                    # Try to wait for load and scroll trigger
                    try:
                        await self.page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    try:
                        await self.page.evaluate("window.scrollBy(0, 400)")
                        await asyncio.sleep(0.5)
                        await self.page.evaluate("window.scrollBy(0, -200)")
                        await asyncio.sleep(0.5)
                    except Exception:
                        pass
                    return True
        except Exception as e:
            self.logger.debug(f"go_back() failed or timed out: {e}")

        # Direct navigation
        try:
            self.logger.info(f"Recovery navigate: direct goto {url}")
            resp = await self.page.goto(url, wait_until='domcontentloaded', timeout=30000)
            if resp and resp.status < 400:
                self.logger.info(f"Direct navigate succeeded: {self.page.url}")
                # Wait for full page render
                try:
                    await self.page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                # Scroll down a bit to trigger lazy-loading of variant swatches
                try:
                    await self.page.evaluate("window.scrollBy(0, 400)")
                    await asyncio.sleep(0.5)
                    await self.page.evaluate("window.scrollBy(0, -200)")
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
                return True
            else:
                self.logger.warning(f"Direct navigate returned status {resp.status if resp else 'None'} for {url}")
                return False
        except Exception as e:
            self.logger.error(f"Recovery _navigate_to_url failed: {e}")
            return False

    async def _visualize_click_element(self, elem):
        """Safely visualize click coordinates"""
        try:
            coords = await self.click_coordinator.get_safe_click_coordinates(elem)
            if coords:
                await self._visualize_click(coords[0], coords[1])
        except: 
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # SITE-CONTEXT DETECTION
    # ─────────────────────────────────────────────────────────────────────────
    def _detect_site_context(self, url: str) -> Dict[str, str]:
        """
        Detect country / login-flow type from the product URL.

        Returns a dict with:
          country : 'IN'  (India)  |  'INTL' (default)
          flow    : 'phone_otp'    |  'email_guest'
        """
        u = url.lower()
        indian_domains = [
            'flipkart.com', 'myntra.com', 'meesho.com', 'ajio.com',
            'nykaa.com', 'snapdeal.com', 'tatacliq.com', 'amazon.in',
            'shopsy.in', 'firstcry.com', 'pepperfry.com'
        ]
        indian_tlds = ['.in/', '.in?', '.co.in']
        is_india = (
            any(d in u for d in indian_domains) or
            any(t in u for t in indian_tlds)
        )
        return {
            'country': 'IN'   if is_india else 'INTL',
            'flow':    'phone_otp' if is_india else 'email_guest',
        }

    # ─────────────────────────────────────────────────────────────────────────
    # CHECKOUT-FORM PAGE DETECTOR  (URL + DOM — broader than URL-only)
    # ─────────────────────────────────────────────────────────────────────────
    async def _is_checkout_form_page(self) -> bool:
        """
        Returns True if the current page looks like a checkout address/contact
        form — i.e. we are PAST the login wall.
        
        Key fix: Combined checkout forms (Shopify-style) show email + address
        on the SAME page. We must NOT reject a page just because it has an
        email field — only reject if it has email with NO address fields AND
        the URL doesn't indicate a checkout page.
        """
        url = self.page.url.lower()
        
        # Exclude cart pages
        cart_kws = ['/cart', '/bag', '/basket', '/viewcart']
        if any(kw in url for kw in cart_kws):
            return False
            
        # If URL looks like a checkout page, trust it (unless it's clearly a login URL)
        checkout_url_kws = [
            '/checkout', '/address', '/shipping', '/delivery', '/payment', '/order'
        ]
        is_checkout_url = any(kw in url for kw in checkout_url_kws)
        
        # Explicit login/OTP URL: only valid if address fields exist
        login_kws = ['/login', '/signin', '/otp', 'loginflow', 'login-flow']
        if any(kw in url for kw in login_kws) and not is_checkout_url:
            return await self._dom_has_address_fields()

        # Check for elements, and if none are found, wait up to 3 seconds for page loading/settling
        for attempt in range(6):
            dom_has_address_form = await self._dom_has_address_fields()
            if dom_has_address_form:
                return True
            
            # On a checkout URL, also accept email/phone fields as valid checkout form
            # (Combined Shopify-style: email + address on same page)
            if is_checkout_url:
                email_selectors = [
                    'input[type="email"]', 'input[type="tel"]',
                    'input[name*="email" i]', 'input[name*="phone" i]'
                ]
                for sel in email_selectors:
                    try:
                        elements = await self.page.query_selector_all(sel)
                        for el in elements:
                            if await el.is_visible():
                                self.logger.info(f"Found contact field '{sel}' on checkout URL — treating as checkout form page.")
                                return True
                    except:
                        pass
            else:
                # Not on a checkout URL: if only email/phone visible (no address), likely login wall
                email_selectors = [
                    'input[type="email"]', 'input[type="tel"]',
                    'input[name*="email" i]', 'input[name*="phone" i]'
                ]
                has_visible_email_or_phone = False
                for sel in email_selectors:
                    try:
                        elements = await self.page.query_selector_all(sel)
                        for el in elements:
                            if await el.is_visible():
                                has_visible_email_or_phone = True
                                break
                        if has_visible_email_or_phone:
                            break
                    except:
                        pass
                if has_visible_email_or_phone:
                    self.logger.info(f"Found email/phone but NOT on checkout URL ({url}) — treating as login wall.")
                    return False
                
            # Page might be loading. Wait 500ms and retry.
            if not is_checkout_url:
                return False
                
            await asyncio.sleep(0.5)
            
        # If we timed out and still on checkout URL, assume True (might be shipping method / review step)
        return is_checkout_url

    async def _dom_has_address_fields(self) -> bool:
        address_selectors = [
            'input[name*="address" i]',
            'input[name*="city" i]',
            'input[name*="zip" i]',
            'input[name*="postal" i]',
            'input[name*="firstName" i]',
            'input[name*="first_name" i]',
            'input[placeholder*="first name" i]',
            'input[placeholder*="address" i]',
            'select[name*="state" i]',
            'select[name*="country" i]',
            # Custom dropdown elements
            '[role="combobox"][name*="state" i]',
            '[role="combobox"][id*="state" i]',
            '[role="combobox"][name*="country" i]',
            '[role="combobox"][id*="country" i]',
        ]
        for sel in address_selectors:
            try:
                elements = await self.page.query_selector_all(sel)
                for el in elements:
                    if await el.is_visible():
                        return True
            except:
                pass
        return False

    async def _safe_fill_input(self, element, value: str) -> bool:
        """
        Safely fills an input element.
        Tries Playwright fill(), verifies, and falls back to JS value assignment if truncated.
        """
        try:
            # 1. Clear and fill using Playwright
            await element.fill("")
            await element.fill(value)
            await asyncio.sleep(0.2)
            
            # Verify value
            current_val = await element.evaluate("el => el.value")
            if current_val == value:
                return True
                
            self.logger.warning(f"Playwright fill resulted in '{current_val}' instead of '{value}'. Trying JS fallback...")
        except Exception as e:
            self.logger.warning(f"Playwright fill failed: {e}. Trying JS fallback...")
            
        # 2. JS Fallback
        try:
            await element.evaluate("""
                (el, val) => {
                    el.value = val;
                    el.dispatchEvent(new Event('input', { bubbles: true, cancelable: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true, cancelable: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true, cancelable: true }));
                }
            """, value)
            await asyncio.sleep(0.2)
            current_val = await element.evaluate("el => el.value")
            return current_val == value
        except Exception as e:
            self.logger.error(f"JS fallback fill failed: {e}")
            return False

    async def _safe_click_element(self, element, timeout_ms: int = 3000) -> bool:
        """
        Safely clicks an element using Playwright's click.
        Falls back to JS evaluation click if intercepted by overlays/recaptcha.
        """
        try:
            await element.click(timeout=timeout_ms)
            return True
        except Exception as click_err:
            self.logger.warning(
                f"Playwright click failed (interception/timeout): {click_err}. "
                "Trying JS click fallback..."
            )
            try:
                await element.evaluate("el => el.click()")
                return True
            except Exception as js_click_err:
                self.logger.error(f"JS click failed: {js_click_err}. Trying dispatchEvent fallback...")
                try:
                    await element.evaluate("el => el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}))")
                    return True
                except Exception as dispatch_err:
                    self.logger.error(f"dispatchEvent click failed: {dispatch_err}")
                    return False

    # ─────────────────────────────────────────────────────────────────────────
    # LOGIN / GUEST CHECKOUT  (Stage 3 of the pipeline)
    # ─────────────────────────────────────────────────────────────────────────
    async def _handle_login_or_guest(self, task: Task) -> Result:
        """
        Dynamically handle the login wall after cart navigation.

        Strategy (site-adaptive):
          1. Check if already past the wall (checkout form visible) → success.
          2. Try broad DOM guest-checkout button list → click → verify.
          3a. India  : enter phone from profile → Send OTP → pause for user OTP.
          3b. INTL   : enter email from profile → Continue as Guest.
          4. VLM fallback for step 3 if DOM approach fails.
          5. Abort with clear error if still stuck.
        """
        profile  = task.context.get('profile')
        orig_url = task.context.get('url', self.page.url)
        ctx      = self._detect_site_context(orig_url)

        self.logger.info(
            f"🔐 Login/Guest handler — site context: {ctx['country']} / {ctx['flow']}"
        )
        await self.status.show(
            f"🔐 Handling login wall ({ctx['country']} flow)...", "info", 2500
        )

        # ── 1. Already past login wall? ───────────────────────────────────────
        if await self._is_checkout_form_page():
            self.logger.info("✅ Already on checkout form page — no login wall detected.")
            await self.status.show("✅ No login wall — proceeding", "success", 1500)
            return Result(success=True, task=task, data={'method': 'no_login_wall'})

        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                self.logger.info(f"🔄 Retrying login/guest wall bypass (attempt {attempt}/{max_attempts})...")
                await self.status.show(f"🔄 Retrying login wall bypass ({attempt}/{max_attempts})...", "info", 2000)
                try:
                    await self.page.reload()
                    await self.page.wait_for_load_state("networkidle", timeout=10000)
                    await asyncio.sleep(2)
                except Exception as e:
                    self.logger.warning(f"Failed to reload page on retry: {e}")

            # ── 1.5 Check for CAPTCHA on login page ──────────────────────────────
            await self.captcha_handler.check_and_wait(timeout_seconds=120)

            # ── 2. DOM: broad guest-checkout button search ─────────────────────────
            guest_selectors = [
                # Generic text patterns
                'button:has-text("Continue as Guest")',
                'button:has-text("Guest Checkout")',
                'button:has-text("Checkout as Guest")',
                'button:has-text("Continue without account")',
                'button:has-text("Shop as Guest")',
                'a:has-text("Continue as Guest")',
                'a:has-text("Guest Checkout")',
                'a:has-text("Checkout as Guest")',
                # Data/ID attributes
                '[data-testid*="guest" i]',
                '[id*="guest" i]',
                '.guest-checkout',
                '#guest-checkout-button',
                # Amazon
                '#createAccountSubmit ~ a', '#ap_guest',
                # Shopify
                '[name="checkout[email]"] ~ button',
            ]
            for sel in guest_selectors:
                try:
                    elem = await self.page.query_selector(sel)
                    if elem and await elem.is_visible():
                        box = await elem.bounding_box()
                        if box and box['width'] > 0 and box['height'] > 0:
                            self.logger.info(f"Found visible guest button: {sel} size={box['width']}x{box['height']}")
                            await self.status.show("✅ Found guest checkout — clicking!", "success", 1500)
                            await self._visualize_click_element(elem)
                            await self._safe_click_element(elem, timeout_ms=3000)
                            
                            # Poll for transition up to 5 seconds
                            transitioned = False
                            for _ in range(10):
                                await asyncio.sleep(0.5)
                                if await self._is_checkout_form_page():
                                    transitioned = True
                                    break
                                    
                            if transitioned:
                                self.logger.info("✅ Past login wall via guest button.")
                                return Result(success=True, task=task,
                                              data={'method': 'guest_button', 'selector': sel})
                except Exception as e:
                    self.logger.warning(f"Error clicking guest button '{sel}': {e}")
                    continue

            # ── 3. Site-specific flow ─────────────────────────────────────────────
            if ctx['flow'] == 'phone_otp':
                result = await self._handle_phone_otp_login(task, profile, ctx)
            else:
                result = await self._handle_email_guest_login(task, profile, ctx)

            if result.success:
                return result

            # ── 4. VLM Fallback ───────────────────────────────────────────────────
            self.logger.info("Trying VLM fallback for login/guest wall...")
            await self.status.show("👁️ Using vision to find guest option...", "info", 2500)
            try:
                import tempfile as _tmpB
                _sB = await self.page.screenshot(type="png")
                with _tmpB.NamedTemporaryFile(suffix=".png", delete=False) as _fB:
                    _fB.write(_sB); _fB_path = _fB.name
                vlm = self._extract_json(
                    await self.llm.complete_with_vision(
                        "Find the Guest Checkout button, Continue as Guest link, or Skip login option. "
                        "Return JSON with keys x and y (pixel coords).",
                        _fB_path
                    ) or ""
                )
            except Exception as _eB:
                self.logger.warning(f"VLM guest fallback error: {_eB}")
                vlm = {}
            if vlm.get('x') and vlm.get('y'):
                vp       = self.page.viewport_size or {'width': 1280, 'height': 800}
                pixel_x  = float(vlm['x'])
                pixel_y  = float(vlm['y'])
                await self._visualize_click(pixel_x, pixel_y)
                await self.page.mouse.click(pixel_x, pixel_y)
                await self.smart_waiter.wait_after_click(self.page.url, timeout=6.0)
                if await self._is_checkout_form_page():
                    return Result(success=True, task=task, data={'method': 'vlm_guest_click'})

        # ── 5. Abort ──────────────────────────────────────────────────────────
        await self.status.show(
            "❌ Could not bypass login wall", "error", 4000
        )
        return Result(
            success=False, task=task,
            error="login_wall_not_bypassed — no guest option found and OTP/email flow failed"
        )

    async def _handle_phone_otp_login(self,
                                       task: Task,
                                       profile,
                                       ctx: Dict[str, str]) -> Result:
        """
        Indian site flow: enter mobile number → click Send OTP → pause for user.
        Supports auto-detect OTP when running on a phone (future: SMS reader).
        """
        phone = getattr(profile, 'phone', None) if profile else None
        if not phone:
            self.logger.warning("Phone number missing from profile — cannot do phone+OTP login.")
            await self.status.show(
                "⚠️ No phone number in profile — please log in manually", "warning", 5000
            )
            # Give user 60 s to manually log in
            logged_in = await self._wait_for_checkout_form(timeout_seconds=60)
            return Result(
                success=logged_in, task=task,
                data={'method': 'manual_phone_login'} if logged_in
                else None,
                error=None if logged_in else 'no_phone_in_profile_user_did_not_login'
            )

        self.logger.info(f"Attempting phone+OTP login with number: {phone[-4:].rjust(len(phone), '*')}")

        # ── Find phone input ──────────────────────────────────────────────────
        phone_selectors = [
            'input[type="tel"]',
            'input[name*="phone" i]',
            'input[name*="mobile" i]',
            'input[placeholder*="phone" i]',
            'input[placeholder*="mobile" i]',
            'input[placeholder*="number" i]',
        ]
        phone_field = None
        for sel in phone_selectors:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    phone_field = el
                    break
            except Exception:
                continue

        if not phone_field:
            self.logger.warning("Phone input field not found on page.")
            return Result(success=False, task=task, error='phone_input_not_found')

        # ── Fill phone number ─────────────────────────────────────────────────
        await self._safe_fill_input(phone_field, phone)

        # ── Click Send OTP / Request OTP button ───────────────────────────────
        otp_send_selectors = [
            'button:has-text("Send OTP")',
            'button:has-text("Request OTP")',
            'button:has-text("Get OTP")',
            'button:has-text("Continue")',
            'button[type="submit"]',
        ]
        sent = False
        for sel in otp_send_selectors:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    sent = True
                    self.logger.info(f"Clicked OTP send button: {sel}")
                    break
            except Exception:
                continue

        if not sent:
            self.logger.warning("Could not find Send OTP button — pressing Enter.")
            await self.page.keyboard.press('Enter')

        await asyncio.sleep(1.5)

        # ── Pause and wait for user to enter OTP ──────────────────────────────
        await self.status.show(
            f"⏳ OTP sent to {phone[-4:].rjust(len(phone), '*')}. "
            "Please enter the OTP in the browser...",
            "warning", 120_000
        )
        self.logger.info("Pausing — waiting for user to enter OTP (max 120 s)...")

        otp_entered = await self._wait_for_otp(timeout_seconds=120)
        if otp_entered:
            return Result(success=True, task=task, data={'method': 'phone_otp_login'})

        await self.status.show("⏱️ OTP timeout — please try logging in manually", "error", 5000)
        return Result(success=False, task=task, error='otp_timeout')

    async def _handle_email_guest_login(self,
                                         task: Task,
                                         profile,
                                         ctx: Dict[str, str]) -> Result:
        """
        INTL site flow: enter email in the login form → click Continue / Guest.
        Many sites accept just an email and then offer guest checkout.
        """
        email = getattr(profile, 'email', None) if profile else None
        if not email:
            self.logger.warning("Email missing from profile — cannot do email-guest login.")
            await self.status.show(
                "⚠️ No email in profile — please complete login manually", "warning", 5000
            )
            logged_in = await self._wait_for_checkout_form(timeout_seconds=60)
            return Result(
                success=logged_in, task=task,
                data={'method': 'manual_email_login'} if logged_in else None,
                error=None if logged_in else 'no_email_in_profile_user_did_not_login'
            )

        self.logger.info(f"Attempting email-guest login with: {email}")

        # ── Find email input ──────────────────────────────────────────────────
        email_selectors = [
            'input[type="email"]',
            'input[name*="email" i]',
            'input[placeholder*="email" i]',
            'input[id*="email" i]',
        ]
        email_field = None
        for sel in email_selectors:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    email_field = el
                    break
            except Exception:
                continue

        if not email_field:
            self.logger.warning("Email input field not found on page.")
            return Result(success=False, task=task, error='email_input_not_found')

        # ── Fill email address ────────────────────────────────────────────────
        await self._safe_fill_input(email_field, email)

        # ── Click Continue / Guest Checkout ───────────────────────────────────
        continue_selectors = [
            'button:has-text("Continue")',
            'button:has-text("Guest")',
            'button:has-text("Checkout")',
            'button:has-text("Next")',
            'button[type="submit"]',
            'input[type="submit"]',
            'a:has-text("Continue")',
            'a:has-text("Guest")',
            'a:has-text("Checkout")',
            '[role="button"]:has-text("Continue")',
            '[role="button"]:has-text("Guest")',
            '#continue-button',
            '.continue-button',
        ]
        for sel in continue_selectors:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    self.logger.info(f"Clicking continue button: '{sel}'")
                    await self._safe_click_element(btn, timeout_ms=3000)
                    
                    # Poll for up to 10 seconds for the checkout form to load
                    self.logger.info("Waiting for page transition to checkout form...")
                    transitioned = False
                    for _ in range(20):
                        await asyncio.sleep(0.5)
                        if await self._is_checkout_form_page():
                            transitioned = True
                            break
                            
                    if transitioned:
                        self.logger.info("✅ Email guest login succeeded.")
                        return Result(success=True, task=task,
                                      data={'method': 'email_guest_login'})
                                      
                    # Check if asked for password
                    pw_field = await self.page.query_selector('input[type="password"]')
                    if pw_field and await pw_field.is_visible():
                        password = getattr(profile, 'password', None) if profile else None
                        if password:
                            self.logger.info("Password field detected — filling from profile.")
                            await self._safe_fill_input(pw_field, password)
                            await self.page.keyboard.press('Enter')
                            
                            # Poll for up to 10 seconds after password entry
                            transitioned = False
                            for _ in range(20):
                                await asyncio.sleep(0.5)
                                if await self._is_checkout_form_page():
                                    transitioned = True
                                    break
                                    
                            if transitioned:
                                return Result(success=True, task=task,
                                              data={'method': 'email_password_login'})
                        else:
                            self.logger.warning("Password field appeared but no password in profile.")
                            await self.status.show(
                                "🔐 Password required — please log in manually", "warning", 5000
                            )
                            logged_in = await self._wait_for_checkout_form(timeout_seconds=60)
                            return Result(
                                success=logged_in, task=task,
                                data={'method': 'manual_password_login'} if logged_in else None,
                                error=None if logged_in else 'login_requires_password_not_configured'
                             )
                    break
            except Exception as e:
                self.logger.warning(f"Error during continue button checkout attempt: {e}")
                continue

        return Result(success=False, task=task, error='email_guest_flow_failed')

    async def _wait_for_otp(self, timeout_seconds: int = 120) -> bool:
        """
        Poll for the user to fill in an OTP field (4-8 digits).
        Also watches for automatic page transition past the OTP step.
        Returns True if OTP was entered and the page moved to checkout form.
        """
        otp_selectors = [
            'input[name*="otp" i]',
            'input[placeholder*="otp" i]',
            'input[placeholder*="enter otp" i]',
            'input[type="tel"][maxlength]',
            'input[type="number"][maxlength]',
        ]
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout_seconds:
            # Check if page already moved past OTP step
            if await self._is_checkout_form_page():
                self.logger.info("✅ OTP auto-accepted — checkout form now visible.")
                return True

            # Check if OTP field was filled by user
            for sel in otp_selectors:
                try:
                    val = await self.page.evaluate(
                        f"""() => {{
                            const el = document.querySelector('{sel}');
                            return el ? el.value.trim() : '';
                        }}"""
                    )
                    if val and len(val) >= 4:
                        self.logger.info(
                            f"OTP field filled with {len(val)}-digit code — submitting..."
                        )
                        await self.page.keyboard.press('Enter')
                        await asyncio.sleep(2.5)
                        if await self._is_checkout_form_page():
                            return True
                except Exception:
                    pass

            await asyncio.sleep(2)

        return False

    async def _wait_for_checkout_form(self, timeout_seconds: int = 60) -> bool:
        """
        Passively wait (no action) for the page to reach a checkout form.
        Used when prompting user to complete login manually.
        """
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout_seconds:
            if await self._is_checkout_form_page():
                return True
            await asyncio.sleep(2)
        return False

    # ─────────────────────────────────────────────────────────────────────────
    # MULTI-STEP CHECKOUT WIZARD
    # ─────────────────────────────────────────────────────────────────────────

    async def _detect_checkout_step(self) -> str:
        """
        Classify the current checkout page into one of these steps:
          'contact'         – email / phone entry (pre-address)
          'address'         – shipping address fields visible
          'shipping_method' – shipping radio buttons / delivery options
          'payment'         – payment card fields or payment processor iframe
          'review'          – order summary / place order button, no card fields
          'done'            – confirmation / thank-you page
          'unknown'         – none of the above matched

        Combines URL pattern analysis with lightweight DOM checks so it works
        on both full-page and SPA checkout flows.
        """
        url = self.page.url.lower()

        # ── Done / confirmation ───────────────────────────────────────────
        done_kws = ['/thank', '/confirmation', '/order-confirmed', '/order-complete',
                    '/success', '/thankyou', 'order_placed', 'order-placed']
        if any(k in url for k in done_kws):
            return 'done'

        # ── Try DOM signals first — more reliable on SPAs ─────────────────
        try:
            signals = await self.page.evaluate("""
            () => {
                const vis = sel => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                return {
                    hasCardNumber: vis('input[autocomplete="cc-number"]') ||
                                   vis('[data-elements-stable-field-name="cardNumber"]') ||
                                   vis('input[name*="card"][name*="number" i]') ||
                                   vis('input[id*="cardNumber" i]') ||
                                   vis('[name="cardnumber"]') ||
                                   document.querySelectorAll(
                                       'iframe[src*="stripe"], iframe[src*="braintree"], ' +
                                       'iframe[src*="adyen"], iframe[name*="__zoid__paypal_card"]'
                                   ).length > 0,
                    hasShippingRadio: vis('input[type="radio"][name*="ship" i]') ||
                                      vis('input[type="radio"][name*="deliver" i]') ||
                                      vis('[class*="shippingMethod"] input[type="radio"]'),
                    hasAddressLine1: vis('input[name*="address" i]') ||
                                     vis('input[autocomplete="address-line1"]') ||
                                     vis('input[placeholder*="address" i]'),
                    hasEmailOnly: vis('input[type="email"]') &&
                                  !vis('input[name*="address" i]') &&
                                  !vis('input[autocomplete="address-line1"]'),
                    hasPlaceOrderBtn: (()=>{
                        const texts = ['place order','pay now','complete order',
                                       'submit order','confirm order'];
                        for (const el of document.querySelectorAll('button,input[type=submit]')) {
                            const t = (el.textContent||el.value||'').toLowerCase().trim();
                            if (texts.some(kw => t.includes(kw))) return true;
                        }
                        return false;
                    })(),
                };
            }
            """)
        except Exception:
            signals = {}

        if signals.get('hasCardNumber'):
            return 'payment'
        if signals.get('hasShippingRadio'):
            return 'shipping_method'
        if signals.get('hasAddressLine1'):
            return 'address'
        if signals.get('hasPlaceOrderBtn') and not signals.get('hasAddressLine1'):
            return 'review'
        if signals.get('hasEmailOnly'):
            return 'contact'

        # ── URL pattern fallback ──────────────────────────────────────────
        if any(k in url for k in ['/payment', '/billing', 'step=payment']):
            return 'payment'
        if any(k in url for k in ['/shipping', '/delivery', 'step=shipping', 'step=delivery']):
            return 'shipping_method'
        if any(k in url for k in ['/address', 'step=address']):
            return 'address'
        if any(k in url for k in ['/contact', '/information', 'step=contact', 'step=information']):
            return 'contact'
        if any(k in url for k in ['/review', '/confirm', 'step=review']):
            return 'review'

        return 'unknown'

    async def _wizard_continue(self, current_step: str) -> bool:
        """
        Click the "Continue" / "Next" / "Proceed" button for the current step,
        then use SmartWaiter to wait for the page to transition.
        Returns True if a button was found and clicked.
        """
        old_url = self.page.url

        # ── Auto-check any required checkboxes first ──────────────────────
        await self._accept_required_checkboxes()

        # Step-specific preferred labels first, then generic fallbacks
        step_labels = {
            'contact':         ['Continue to shipping', 'Continue to address',
                                'Continue', 'Next', 'Proceed'],
            'address':         ['Continue to shipping', 'Continue to delivery',
                                'Continue', 'Next', 'Proceed'],
            'shipping_method': ['Continue to payment', 'Proceed to payment',
                                'Continue', 'Next'],
            'payment':         ['Pay now', 'Place order', 'Complete order',
                                'Submit order', 'Confirm order', 'Continue'],
            'review':          ['Place order', 'Confirm order', 'Submit order',
                                'Pay now', 'Complete order'],
        }
        labels = step_labels.get(current_step, ['Continue', 'Next', 'Proceed'])

        # Build selector list: text-match buttons + submit buttons
        selectors = []
        for lbl in labels:
            selectors.append(f'button:has-text("{lbl}")')
            selectors.append(f'input[type="submit"][value*="{lbl}" i]')
        selectors += [
            'button[type="submit"]',
            'input[type="submit"]',
            '[role="button"]:has-text("Continue")',
        ]

        for sel in selectors:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    box = await btn.bounding_box()
                    if not box or box['width'] < 1:
                        continue
                    self.logger.info(f"🔘 Wizard continue: clicking '{sel}'")
                    await self.status.show(f"➡️ Continuing ({current_step})...", "info", 2000)
                    await btn.scroll_into_view_if_needed()
                    await self._safe_click_element(btn)
                    # Smart wait: URL change or DOM settle
                    await self.smart_waiter.wait_after_click(old_url, timeout=10.0)
                    return True
            except Exception as e:
                self.logger.debug(f"_wizard_continue selector '{sel}' failed: {e}")
                continue

        self.logger.warning(f"_wizard_continue: no button found for step '{current_step}'")
        return False

    async def _run_checkout_wizard(self, task: Task) -> Result:
        """
        Multi-step checkout wizard loop.

        Replaces the fragile one-shot fill_contact / fill_shipping sequence.
        Loops detect → fill → continue until the payment page is reached or
        the wizard completes.

        Steps handled:
          contact  → fill email/phone (delegates to address_autofill)
          address  → fill shipping address + address validation dialog
          shipping_method → pick cheapest radio
          payment  → detect processor, fill card via PaymentFormFiller
          review   → stop (manual_review gate respected)
          done     → success
        """
        profile = task.context.get('profile')
        if not profile:
            return Result(success=False, task=task, error='no_profile_for_wizard')

        max_steps = 8   # Safety cap — real checkouts are 3-5 steps
        last_step = None
        stall_count = 0

        for iteration in range(max_steps):
            step = await self._detect_checkout_step()
            self.logger.info(f"🧭 Wizard iteration {iteration+1}: step='{step}' url={self.page.url}")
            await self.status.show(f"🧭 Checkout step: {step}", "info", 1500)

            # ── Check for CAPTCHA before doing anything ───────────────────
            await self.captcha_handler.check_and_wait(timeout_seconds=120)

            # ── Stall detection ───────────────────────────────────────────
            if step == last_step:
                stall_count += 1
                if stall_count >= 2:
                    self.logger.warning(
                        f"Wizard stalled on step '{step}' after {stall_count} iterations. "
                        "Attempting VLM recovery..."
                    )
                    # Try VLM fallback to find continue button
                    try:
                        import tempfile, base64 as _b64
                        _shot = await self.page.screenshot(type="png")
                        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as _tf:
                            _tf.write(_shot)
                            _tf_path = _tf.name
                        _vlm_prompt = (
                            "Look at this checkout page screenshot. "
                            "Find the primary Continue, Next, or Proceed button. "
                            "Return JSON: {{\"x\": <pixel x>, \"y\": <pixel y>}} "
                            "where x and y are the centre of that button in the image. "
                            "If no such button exists return {{}}."
                        )
                        _vlm_resp = await self.llm.complete_with_vision(_vlm_prompt, _tf_path)
                        _vlm_coords = self._extract_json(_vlm_resp or "")
                        if _vlm_coords.get("x") and _vlm_coords.get("y"):
                            await self.page.mouse.click(
                                float(_vlm_coords["x"]), float(_vlm_coords["y"])
                            )
                            await self.smart_waiter.wait_after_click(self.page.url, timeout=8.0)
                            stall_count = 0
                            continue
                    except Exception as _vlm_err:
                        self.logger.warning(f"VLM stall recovery failed: {_vlm_err}")
                    return Result(
                        success=False, task=task,
                        error=f"wizard_stalled_on_step_{step}"
                    )
            else:
                stall_count = 0
            last_step = step

            # ── Handle each step ─────────────────────────────────────────
            if step == 'done':
                self.logger.info("✅ Wizard: order confirmed!")
                await self.status.show("✅ Order confirmed!", "success", 3000)
                return Result(success=True, task=task, data={'method': 'wizard_done'})

            elif step == 'review':
                if self.manual_review_final_step:
                    self.logger.info("🛑 Wizard reached review step — pausing for manual review.")
                    await self.status.show(
                        "🛑 Review page — please inspect and place order manually.",
                        "warning", 600_000
                    )
                    await asyncio.sleep(600)
                    return Result(success=True, task=task,
                                  data={'method': 'wizard_manual_review'})
                else:
                    await self._wizard_continue('review')
                    continue

            elif step == 'payment':
                self.logger.info("💳 Wizard: filling payment form...")
                await self.status.show("💳 Filling payment details...", "info", 2000)
                pay_result = await self.payment_filler.fill_payment(profile)
                if pay_result['success']:
                    self.logger.info(
                        f"✅ Payment form filled via {pay_result['processor']} "
                        f"({pay_result['method']})"
                    )
                    # Respect manual review gate before clicking Pay
                    if self.manual_review_final_step:
                        await self.status.show(
                            "🛑 Payment filled — pausing for manual review before placing order.",
                            "warning", 600_000
                        )
                        await asyncio.sleep(600)
                        return Result(success=True, task=task,
                                      data={'method': 'wizard_payment_manual_review',
                                            'processor': pay_result['processor']})
                    await self._wizard_continue('payment')
                else:
                    self.logger.warning(
                        f"Payment fill failed: {pay_result['error']} — "
                        "pausing for manual payment entry."
                    )
                    await self.status.show(
                        "⚠️ Could not fill payment — please enter card details manually.",
                        "warning", 300_000
                    )
                    await asyncio.sleep(300)
                    return Result(success=False, task=task,
                                  error=f"payment_fill_failed: {pay_result['error']}")

            elif step == 'shipping_method':
                self.logger.info("🚚 Wizard: selecting shipping method...")
                # Reuse existing logic: pick cheapest radio
                ship_task = Task(
                    type='select_shipping_method',
                    description='Select shipping method (wizard)',
                    context={},
                    priority=2
                )
                ship_result = await self._select_shipping_method(ship_task)
                if not ship_result.success:
                    self.logger.warning("Shipping method selection failed in wizard — continuing anyway")

            elif step in ('address', 'contact', 'unknown'):
                # Fill whatever fields are visible
                self.logger.info(f"📝 Wizard: filling fields for step '{step}'...")
                fill_result = await self.address_autofill.fill_address(profile, max_retries=2)

                if fill_result.get('state') == 'address_filled':
                    # Run address validation dialog handler
                    await asyncio.sleep(1)
                    dismissed = await self.addr_validator.check_and_dismiss(wait_seconds=3.0)
                    if dismissed:
                        self.logger.info("📬 Address validation dialog dismissed.")

                    # LLM error recovery
                    if self.llm:
                        extractor = FormStateExtractor(self.page)
                        missing = await extractor.identify_missing_required_fields()
                        if missing:
                            fixer = TextLLMFormFixer(self.page, self.llm)
                            fix_res = await fixer.auto_fix_form_errors()
                            if not fix_res['success']:
                                self.logger.warning(
                                    f"Form errors remain after LLM fix: {fix_res['remaining_errors']}"
                                )

                    # Auto-check checkboxes
                    await self._accept_required_checkboxes()

                # Click continue regardless — the wizard will re-detect next iteration
                await self._wizard_continue(step)

        return Result(
            success=False, task=task,
            error=f"wizard_max_steps_exceeded_last_step_{last_step}"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # UNIFIED CHECKOUT FORM FILLER  (replaces separate _fill_contact /
    # _fill_shipping which both called fill_address() identically)
    # ─────────────────────────────────────────────────────────────────────────
    async def _fill_checkout_form(self, task: Task, label: str = "checkout form") -> Result:
        """
        Single-pass checkout form filler.

        Fills all visible contact + address fields in one call, then runs the
        Text-LLM error recovery pass.  Called by both _fill_contact and
        _fill_shipping so neither duplicates the work.

        The first caller (fill_contact) populates everything; the second caller
        (fill_shipping) detects that all required fields are already filled and
        returns success immediately.
        """
        self.logger.info(f"Filling {label}...")

        # ── Guard: must be on a checkout form page ────────────────────────
        if not await self._is_checkout_form_page():
            self.logger.error(
                f"Cannot fill {label}: not on a checkout form page (URL: {self.page.url})"
            )
            return Result(success=False, task=task, error="not_on_checkout_page")

        # ── Guard: profile required ───────────────────────────────────────
        profile = task.context.get('profile')
        if not profile:
            self.logger.error("No profile provided in task context")
            return Result(success=False, task=task, error="no_profile_provided")

        # ── Check for CAPTCHA before trying to fill ───────────────────────
        captcha_found = await self.captcha_handler.check_and_wait(timeout_seconds=120)
        if captcha_found:
            self.logger.info("CAPTCHA resolved — continuing with form fill.")

        # ── Fill address + contact in one pass ────────────────────────────
        await self.status.show(f"📝 Filling {label}...", "info", 2000)
        result = await self.address_autofill.fill_address(profile, max_retries=2)

        if result['state'] != 'address_filled':
            self.logger.error(f"{label} fill failed: {result.get('errors')}")
            return Result(
                success=False, task=task,
                error=f"autofill_failed: {result.get('errors')}"
            )

        self.logger.info(f"{label} filled: {result['filled_fields']}")

        # ── Auto-check any unchecked required checkboxes (terms / age) ───
        await self._accept_required_checkboxes()

        # ── Address validation dialog ("Did you mean?") ───────────────────
        await asyncio.sleep(1)  # brief settle for dialog to appear
        dismissed = await self.addr_validator.check_and_dismiss(wait_seconds=3.0)
        if dismissed:
            self.logger.info("📬 Address validation dialog dismissed in _fill_checkout_form.")

        # ── Text-LLM form error recovery ─────────────────────────────────
        if self.llm:
            await self.status.show("🧠 Validating form...", "info", 1500)
            extractor = FormStateExtractor(self.page)
            missing_fields = await extractor.identify_missing_required_fields()

            if missing_fields:
                self.logger.warning(
                    f"Found {len(missing_fields)} form error(s). Starting recovery..."
                )
                await self.status.show(
                    f"🔧 Fixing {len(missing_fields)} form error(s)...", "info", 2000
                )
                fixer = TextLLMFormFixer(self.page, self.llm)
                fix_result = await fixer.auto_fix_form_errors()

                if fix_result['success']:
                    self.logger.info(
                        f"✓ Form errors fixed: {len(fix_result['actions_taken'])} action(s)"
                    )
                    await self.status.show("✓ Form validated!", "success", 1500)
                    return Result(
                        success=True, task=task,
                        data={
                            'method': 'autofill_with_llm_recovery',
                            'initial_fields': result['filled_fields'],
                            'recovery_actions': fix_result['actions_taken'],
                        }
                    )
                else:
                    self.logger.error(
                        f"❌ Form errors remain: {fix_result['remaining_errors']}"
                    )
                    await self.status.show("⚠️ Form still has errors", "warning", 2000)
                    return Result(
                        success=False, task=task,
                        error=f"form_validation_failed: {fix_result['remaining_errors']}"
                    )
            else:
                self.logger.info("✓ All form fields valid")
                await self.status.show("✓ Form valid!", "success", 1000)

        return Result(
            success=True, task=task,
            data={'method': 'address_autofill', 'fields': result['filled_fields']}
        )

    async def _fill_contact(self, task: Task) -> Result:
        """
        Entry point for the checkout form pipeline.

        Runs the full multi-step wizard loop (contact → address → shipping
        method → payment) rather than a one-shot fill.  Subsequent tasks
        (_fill_shipping, _select_shipping_method) check if the wizard already
        advanced past their step and return success immediately if so.
        """
        step = await self._detect_checkout_step()

        # If we're already past the contact/address steps, the wizard already
        # ran — just return success so the orchestrator can continue.
        if step in ('shipping_method', 'payment', 'review', 'done'):
            self.logger.info(
                f"_fill_contact: already past contact step (step='{step}') — skipping wizard."
            )
            return Result(success=True, task=task,
                          data={'method': 'wizard_already_advanced', 'step': step})

        return await self._run_checkout_wizard(task)

    async def _fill_shipping(self, task: Task) -> Result:
        """
        If the wizard (_fill_contact) already advanced past the address step,
        return success.  Otherwise run a targeted single-pass address fill
        (for sites that separate contact and shipping into two pages and the
        orchestrator calls fill_shipping directly after fill_contact returns).
        """
        step = await self._detect_checkout_step()

        if step in ('shipping_method', 'payment', 'review', 'done'):
            self.logger.info(
                f"_fill_shipping: wizard already past address step (step='{step}') — skipping."
            )
            return Result(success=True, task=task,
                          data={'method': 'wizard_already_advanced', 'step': step})

        # Still on address step — do a targeted fill + continue
        return await self._fill_checkout_form(task, label="shipping address")

    # ─────────────────────────────────────────────────────────────────────────
    # TERMS / AGE VERIFICATION CHECKBOX HANDLER  (GAP 15 / Phase 1 - GAP 5)
    # ─────────────────────────────────────────────────────────────────────────

    # ─────────────────────────────────────────────────────────────────────────
    # TERMS / AGE VERIFICATION CHECKBOX HANDLER  (GAP 15 / Phase 1 - GAP 5)
    # ─────────────────────────────────────────────────────────────────────────
    async def _accept_required_checkboxes(self) -> int:
        """
        Scan the page for unchecked required checkboxes (terms, age, consent)
        and check them automatically.  Skips any checkbox whose label contains
        payment / charge / subscription keywords that the user must explicitly
        agree to.

        Returns the number of checkboxes that were checked.
        """
        _SKIP_KEYWORDS = [
            "payment", "charge", "billing", "subscribe", "subscription",
            "recurring", "auto-renew", "autorenovar",
        ]
        _ACCEPT_KEYWORDS = [
            "terms", "privacy", "policy", "age", "18", "21",
            "condition", "agree", "consent", "gdpr", "marketing",
            "newsletter", "accept", "confirm",
        ]

        checked_count: int = 0
        try:
            # Playwright evaluate() takes ONE arg — pack both keyword lists into a dict
            result = await self.page.evaluate(
                """({skipKw, acceptKw}) => {
                    const inputs = Array.from(
                        document.querySelectorAll('input[type="checkbox"]')
                    );
                    let checked = 0;
                    for (const cb of inputs) {
                        if (cb.checked) continue;
                        const labelEl = cb.labels && cb.labels[0]
                            ? cb.labels[0]
                            : document.querySelector(`label[for="${cb.id}"]`);
                        const ariaLabel = cb.getAttribute('aria-label') || '';
                        const parentEl = cb.closest('label,p,div,li');
                        const parentText = parentEl ? (parentEl.innerText || '') : '';
                        const text = (
                            (labelEl ? labelEl.innerText : '') + ' ' +
                            ariaLabel + ' ' + parentText
                        ).toLowerCase();
                        const hasSkip = skipKw.some(k => text.includes(k));
                        const hasAccept = acceptKw.some(k => text.includes(k));
                        if (hasSkip) continue;
                        const isRequired = cb.required ||
                            cb.getAttribute('aria-required') === 'true' ||
                            hasAccept;
                        if (isRequired) { cb.click(); checked++; }
                    }
                    return checked;
                }""",
                {"skipKw": _SKIP_KEYWORDS, "acceptKw": _ACCEPT_KEYWORDS}
            )
            checked_count = int(result or 0)
            if checked_count:
                self.logger.info(
                    f"Auto-checked {checked_count} required consent checkbox(es)."
                )
        except Exception as e:
            self.logger.warning(f"_accept_required_checkboxes error: {e}")

        return checked_count

    # PLACE ORDER (Phase 2 rewrite)
    async def _place_order(self, task: Task) -> Result:
        """Final checkout step: fill payment, click Place Order, detect outcome."""
        manual_review = (task.data or {}).get("manual_review_final_step", False)

        if manual_review:
            self.logger.info("manual_review_final_step=True — pausing 600 s for human review.")
            await self.status.show(
                "Manual review mode — please review and place the order manually.",
                "info", 600_000,
            )
            await asyncio.sleep(600)
            return Result(success=True, task=task,
                          data={"method": "manual_review", "note": "operator placed order"})

        # 1. Fill payment
        profile = task.data.get("profile") if task.data else None
        if profile is not None:
            await self.status.show("Filling payment details...", "info", 2000)
            pay_result = await self.payment_filler.fill_payment(profile)
            if pay_result["success"]:
                self.logger.info(
                    f"Payment filled via {pay_result['processor']} (method={pay_result['method']})"
                )
            else:
                self.logger.warning(
                    f"Payment fill failed: {pay_result.get('error')} — "
                    "continuing to attempt Place Order anyway."
                )

        # 2. Accept consent checkboxes
        await self._accept_required_checkboxes()

        # 3. Click Place Order button
        _PLACE_ORDER_LABELS = [
            "place order", "place my order", "complete purchase",
            "complete order", "submit order", "confirm order",
            "confirm purchase", "buy now", "pay now",
            "confirm & pay", "confirm and pay", "proceed to pay",
            "review & place order",
        ]
        await self.status.show("Clicking Place Order...", "info", 3000)
        old_url = self.page.url
        clicked = False

        for label in _PLACE_ORDER_LABELS:
            try:
                btn = self.page.get_by_role("button", name=label, exact=False)
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                    clicked = True
                    self.logger.info(f"Clicked Place Order via role/name: '{label}'")
                    break
            except Exception:
                pass

        if not clicked:
            self.logger.warning("Place Order button not found by text -- attempting VLM click.")
            await self.status.show("Using VLM to find Place Order button...", "info", 2000)
            try:
                import tempfile
                _shot2 = await self.page.screenshot(type="png")
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as _tf2:
                    _tf2.write(_shot2)
                    _tf2_path = _tf2.name
                _vlm_prompt2 = (
                    "Find the final Place Order, Complete Purchase, or Pay Now button "
                    'on this checkout page. Return JSON: {"x": <pixel x>, "y": <pixel y>}. '
                    "If not found return {}."
                )
                vlm_result = self._extract_json(
                    await self.llm.complete_with_vision(_vlm_prompt2, _tf2_path) or ""
                )
                if vlm_result.get("x"):
                    await self.page.mouse.click(
                        float(vlm_result["x"]), float(vlm_result["y"])
                    )
                    clicked = True
                    self.logger.info(
                        f"VLM clicked Place Order at ({vlm_result['x']}, {vlm_result['y']})"
                    )
            except Exception as vlm_err:
                self.logger.error(f"VLM Place Order fallback failed: {vlm_err}")

        if not clicked:
            return Result(success=False, task=task, error="place_order_button_not_found")

        # 4. Wait for confirmation / error
        _SUCCESS_PATTERNS = [
            "thank you", "order confirmed", "order placed", "order received",
            "your order", "confirmation", "payment successful", "order #",
            "order number", "purchase complete",
        ]
        _FAILURE_PATTERNS = [
            "declined", "payment failed", "card was declined",
            "transaction failed", "payment not accepted",
        ]
        new_url = await self.smart_waiter.wait_after_click(old_url, timeout=15.0)

        try:
            body_text = await self.page.evaluate("() => document.body.innerText.toLowerCase()")
            if any(p in body_text for p in _SUCCESS_PATTERNS):
                await self.status.show("Order confirmed!", "success", 5000)
                return Result(success=True, task=task,
                              data={"method": "place_order", "confirmation_url": new_url})
            if any(p in body_text for p in _FAILURE_PATTERNS):
                return Result(success=False, task=task, error="order_declined_dom_signal")
        except Exception:
            pass

        if new_url != old_url:
            self.logger.info(f"URL changed after Place Order ({old_url} -> {new_url}); assuming success.")
            return Result(success=True, task=task,
                          data={"method": "place_order", "confirmation_url": new_url})

        return Result(success=False, task=task, error="place_order_no_navigation_detected")
