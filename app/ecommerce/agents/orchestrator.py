#!/usr/bin/env python3
"""
Agent Orchestrator
Manages multiple agents and delegates tasks
"""

from typing import List, Dict, Any, Optional
from .base_agent import BaseAgent, Task, Result
import logging

logger = logging.getLogger(__name__)


class AgentOrchestrator:
    """
    Orchestrates multiple specialized agents
    Routes tasks to the most capable agent
    """
    
    def __init__(self):
        self.agents: List[BaseAgent] = []
        self.logger = logging.getLogger("orchestrator")
    
    def register_agent(self, agent: BaseAgent):
        """Register a new agent"""
        self.agents.append(agent)
        self.logger.info(f"Registered agent: {agent.name}")
    
    async def execute_task(self, task: Task) -> Result:
        """
        Execute a task by finding the best agent
        
        Process:
        1. Ask all agents if they can handle the task
        2. Select agent with highest confidence
        3. Delegate task to that agent
        4. Return result
        """
        if not self.agents:
            return Result(
                success=False,
                task=task,
                error="No agents registered"
            )
        
        # Find best agent for this task
        best_agent = None
        best_confidence = 0.0
        
        self.logger.info(f"Finding agent for task: {task}")
        
        for agent in self.agents:
            confidence = await agent.can_handle(task)
            self.logger.debug(f"  {agent.name}: confidence={confidence:.2f}")
            
            if confidence > best_confidence:
                best_confidence = confidence
                best_agent = agent
        
        if not best_agent or best_confidence < 0.3:
            return Result(
                success=False,
                task=task,
                error=f"No capable agent found (best confidence: {best_confidence:.2f})"
            )
        
        # Execute task with best agent
        self.logger.info(f"Delegating to {best_agent.name} (confidence={best_confidence:.2f})")
        result = await best_agent.run(task)
        
        return result
    
    async def _is_single_page_checkout(self, page) -> bool:
        """
        Detects if the checkout is a single-page layout where Contact,
        Delivery, Shipping Method, and Payment sections all appear together
        on one page (common in Shopify, WooCommerce, etc).
        Returns True if this is a single-page checkout.
        """
        try:
            # A single-page checkout has both an address/contact form AND
            # a payment section visible at the same time
            has_contact_or_address = False
            early_selectors = [
                'input[type="email"]',
                'input[name*="email" i]',
                'input[name*="address" i]',
                'input[name*="firstName" i]',
                'input[name*="first_name" i]',
                'input[placeholder*="first name" i]',
                'input[placeholder*="address" i]',
            ]
            for sel in early_selectors:
                try:
                    els = await page.query_selector_all(sel)
                    for el in els:
                        if await el.is_visible():
                            has_contact_or_address = True
                            break
                    if has_contact_or_address:
                        break
                except:
                    pass

            has_payment_section = False
            payment_section_selectors = [
                "iframe[title*='payment' i]",
                "input[name*='card' i]",
                "input[id*='card' i]",
                "input[placeholder*='Card number' i]",
                "[data-testid*='payment' i]",
                "#payment",
                ".payment",
            ]
            for sel in payment_section_selectors:
                try:
                    els = await page.query_selector_all(sel)
                    for el in els:
                        if await el.is_visible():
                            has_payment_section = True
                            break
                    if has_payment_section:
                        break
                except:
                    pass

            return has_contact_or_address and has_payment_section
        except Exception as e:
            self.logger.warning(f"Error in _is_single_page_checkout: {e}")
            return False

    async def _get_field_value(self, page, selector: str) -> str:
        """Returns the current value of a visible input field, or empty string."""
        try:
            el = await page.query_selector(selector)
            if el and await el.is_visible():
                val = await el.get_attribute('value') or ''
                return val.strip()
        except:
            pass
        return ''

    async def _detect_current_checkout_stage(self, page) -> int:
        """
        Detects the current checkout stage from browser state.
        Returns a stage index from 1 to 7.

        KEY FIX: On single-page checkouts (Shopify, etc.), all sections
        (Contact, Delivery, Shipping, Payment) are visible simultaneously.
        In this case, we must NOT jump to Stage 7 just because payment UI exists.
        Instead, we find the LOWEST stage that still has unfilled required fields
        and return that, so the pipeline fills them in order.
        """
        if not page:
            return 1
        try:
            url = page.url.lower()

            # ── Detect single-page checkout FIRST ─────────────────────────────
            # On a single-page checkout, all sections coexist on one page.
            # We determine stage by the FIRST unfilled required section.
            if await self._is_single_page_checkout(page):
                self.logger.info(
                    "Single-page checkout detected — determining stage by first unfilled section."
                )

                # Stage 3 check: is there a login/guest wall ONLY (no address fields)?
                # On a true single-page checkout this is usually already past.

                # Stage 4 check: is the email/contact field empty?
                contact_field_selectors = [
                    'input[type="email"]',
                    'input[name*="email" i]',
                    'input[placeholder*="email" i]',
                ]
                contact_empty = False
                for sel in contact_field_selectors:
                    val = await self._get_field_value(page, sel)
                    if val == '':
                        # Field exists and is empty → contact not yet filled
                        try:
                            el = await page.query_selector(sel)
                            if el and await el.is_visible():
                                contact_empty = True
                                break
                        except:
                            pass
                if contact_empty:
                    self.logger.info(
                        "Single-page checkout: contact email field is empty → Stage 4"
                    )
                    return 4

                # Stage 5 check: is the address/name field empty?
                address_field_selectors = [
                    'input[name*="firstName" i]',
                    'input[name*="first_name" i]',
                    'input[placeholder*="first name" i]',
                    'input[name*="address" i]',
                    'input[placeholder*="address" i]',
                ]
                address_empty = False
                for sel in address_field_selectors:
                    val = await self._get_field_value(page, sel)
                    if val == '':
                        try:
                            el = await page.query_selector(sel)
                            if el and await el.is_visible():
                                address_empty = True
                                break
                        except:
                            pass
                if address_empty:
                    self.logger.info(
                        "Single-page checkout: address field is empty → Stage 5"
                    )
                    return 5

                # Stage 6 check: shipping method radios present and none selected?
                shipping_radio_selectors = [
                    'input[type="radio"][name*="shipping" i]',
                    'input[type="radio"][id*="shipping" i]',
                    'input[type="radio"][name*="delivery" i]',
                ]
                any_shipping_radio = False
                shipping_selected = False
                for sel in shipping_radio_selectors:
                    try:
                        radios = await page.query_selector_all(sel)
                        for radio in radios:
                            if await radio.is_visible():
                                any_shipping_radio = True
                                checked = await radio.get_attribute('checked')
                                is_checked = await page.evaluate(
                                    'el => el.checked', radio
                                )
                                if is_checked:
                                    shipping_selected = True
                                    break
                        if any_shipping_radio:
                            break
                    except:
                        pass
                if any_shipping_radio and not shipping_selected:
                    self.logger.info(
                        "Single-page checkout: shipping radios present but none selected → Stage 6"
                    )
                    return 6

                # All earlier fields appear filled → page is at payment stage
                self.logger.info(
                    "Single-page checkout: contact + address appear filled → Stage 7 (payment)"
                )
                return 7

            # ── Standard multi-step checkout detection (original logic) ────────

            # 1. Stage 7: Place Order / Review / Payment
            payment_selectors = [
                "iframe[title*='payment' i]",
                "input[name*='card' i]",
                "input[name*='cc-' i]",
                "input[id*='card' i]",
                "input[placeholder*='Card number' i]",
                "input[placeholder*='MM / YY' i]",
                "input[placeholder*='CVV' i]",
                "button:has-text('Place Order')",
                "button:has-text('Complete order')",
                "button:has-text('Submit Order')",
            ]
            has_visible_payment_input = False
            for sel in payment_selectors:
                try:
                    elements = await page.query_selector_all(sel)
                    for el in elements:
                        if await el.is_visible():
                            has_visible_payment_input = True
                            break
                    if has_visible_payment_input:
                        break
                except:
                    pass
            has_payment = has_visible_payment_input or ("payment" in url and not "shipping" in url)
            if has_payment:
                return 7

            # 2. Stage 6: Shipping Method
            has_shipping_method = (
                await page.is_visible("input[type='radio'][name*='shipping' i]") or
                await page.is_visible("input[type='radio'][id*='shipping' i]") or
                await page.is_visible("input[type='radio'][name*='delivery' i]") or
                "shipping_method" in url
            )
            if has_shipping_method:
                return 6

            # 3. Stage 5: Shipping Address
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
            ]
            has_visible_address_input = False
            for sel in address_selectors:
                try:
                    elements = await page.query_selector_all(sel)
                    for el in elements:
                        if await el.is_visible():
                            has_visible_address_input = True
                            break
                    if has_visible_address_input:
                        break
                except:
                    pass
            if has_visible_address_input:
                return 5

            # 4. Stage 4: Contact Form
            contact_selectors = [
                "input[type='email']",
                "input[name*='email' i]",
                "input[name*='phone' i]"
            ]
            has_contact = False
            for sel in contact_selectors:
                try:
                    elements = await page.query_selector_all(sel)
                    for el in elements:
                        if await el.is_visible():
                            has_contact = True
                            break
                    if has_contact:
                        break
                except:
                    pass
            if has_contact:
                return 4

            # 5. Stage 3: Login / Guest Wall
            login_selectors = [
                "input[type='password']",
                "input[name*='password' i]",
                "button:has-text('Guest Checkout')",
                "button:has-text('Continue as Guest')",
                "a:has-text('Continue as Guest')",
            ]
            has_login = False
            for sel in login_selectors:
                try:
                    elements = await page.query_selector_all(sel)
                    for el in elements:
                        if await el.is_visible():
                            has_login = True
                            break
                    if has_login:
                        break
                except:
                    pass
            if not has_login and any(k in url for k in ('login', 'signin')):
                has_login = True
            if has_login:
                return 3

            is_cart = "cart" in url or "bag" in url or "basket" in url
            if is_cart:
                return 2

            return 1
        except Exception as e:
            self.logger.warning(f"Error in _detect_current_checkout_stage: {e}")
            return 1

    async def execute_workflow(self, tasks: List[Task]) -> List[Result]:
        """
        Hybrid sequential/state-driven checkout pipeline.
        Runs state detection at each step to auto-fast-forward/skip stages already bypassed.

        PIPELINE STAGES (in order):
          Stage 1 — Variant Selection   : each variant retried up to 3×; any failure → abort
          Stage 2 — Add to Cart + Nav   : 3-attempt recovery loop (VLM-assisted)
          Stage 3 — Login / Guest       : phone+OTP (India) or email+guest (INTL)
          Stage 4 — Fill Contact        : email / phone fields
          Stage 5 — Fill Shipping       : address form
          Stage 6 — Shipping Method     : cheapest / free option
          Stage 7 — Place Order         : manual review gate if configured

        Every active stage is a HARD GATE — failure stops the pipeline immediately.
        Bypassed stages are skipped with a fast_forward_skipped result.
        """
        MAX_VARIANT_RETRIES  = 3
        MAX_CART_ATTEMPTS    = 3
        results: List[Result] = []

        # ── Locate specialised agents ─────────────────────────────────────────
        checkout_agent = next(
            (a for a in self.agents if a.name == 'CheckoutAgent'), None
        )
        
        # Get Playwright page reference
        page = None
        for agent in self.agents:
            if hasattr(agent, 'page') and agent.page:
                page = agent.page
                break

        # ── Bucket tasks into named stages ────────────────────────────────────
        variant_tasks:        List[Task] = []
        atc_task:             Optional[Task] = None
        nav_task:             Optional[Task] = None
        login_task:           Optional[Task] = None
        contact_task:         Optional[Task] = None
        shipping_addr_task:   Optional[Task] = None
        shipping_method_task: Optional[Task] = None
        place_order_task:     Optional[Task] = None

        for t in tasks:
            if   t.type == 'select_variant':        variant_tasks.append(t)
            elif t.type == 'add_to_cart'    and atc_task             is None: atc_task             = t
            elif t.type == 'navigate_to_cart' and nav_task           is None: nav_task             = t
            elif t.type == 'login_or_guest'  and login_task          is None: login_task           = t
            elif t.type == 'fill_contact'    and contact_task        is None: contact_task         = t
            elif t.type == 'fill_shipping'   and shipping_addr_task  is None: shipping_addr_task   = t
            elif t.type == 'select_shipping_method' and shipping_method_task is None: shipping_method_task = t
            elif t.type == 'place_order'     and place_order_task    is None: place_order_task     = t

        def _gate_fail(stage: str, reason: str) -> List[Result]:
            self.logger.error(
                f"🚫 GATE BLOCKED [{stage}]: {reason}  "
                f"— pipeline aborted, {len(results)} results collected so far."
            )
            return results

        # ══════════════════════════════════════════════════════════════════════
        # STAGE 1 — Variant Selection
        # ══════════════════════════════════════════════════════════════════════
        if variant_tasks:
            self.logger.info(
                f"━━ STAGE 1: Variant Selection ({len(variant_tasks)} variant(s)) ━━"
            )
            all_variants_ok = True

            for vtask in variant_tasks:
                vtype = vtask.context.get('variant_type', '?')
                vval  = vtask.context.get('variant_value', '?')
                variant_ok = False

                for attempt in range(1, MAX_VARIANT_RETRIES + 1):
                    self.logger.info(
                        f"  Selecting '{vtype}={vval}' — attempt {attempt}/{MAX_VARIANT_RETRIES}"
                    )
                    vresult = await self.execute_task(vtask)
                    results.append(vresult)

                    if vresult.success:
                        self.logger.info(f"  ✅ '{vtype}={vval}' selected.")
                        variant_ok = True
                        break
                    else:
                        self.logger.warning(
                            f"  ⚠️  '{vtype}={vval}' attempt {attempt} failed: {vresult.error}"
                        )

                if not variant_ok:
                    all_variants_ok = False
                    self.logger.error(
                        f"  ❌ '{vtype}={vval}' could not be selected after "
                        f"{MAX_VARIANT_RETRIES} attempts."
                    )

            if not all_variants_ok:
                return _gate_fail(
                    "STAGE 1 — Variant Selection",
                    "One or more required variants could not be selected. "
                    "No point adding an unselected product to cart."
                )
            self.logger.info("✅ STAGE 1 complete — all variants selected.")

        # ══════════════════════════════════════════════════════════════════════
        # STAGE 2 — Add to Cart  +  Navigate to Cart  (3-attempt recovery loop)
        # ══════════════════════════════════════════════════════════════════════
        if atc_task or nav_task:
            self.logger.info("━━ STAGE 2: Add to Cart → Navigate to Cart ━━")

        cart_phase_success = False
        original_url = (atc_task or nav_task).context.get('url', '') if (atc_task or nav_task) else ''

        if atc_task and nav_task:
            for attempt in range(1, MAX_CART_ATTEMPTS + 1):
                self.logger.info(
                    f"  🔄 Cart phase attempt {attempt}/{MAX_CART_ATTEMPTS}"
                )

                # ── Add to Cart ───────────────────────────────────────────────
                atc_result = await self.execute_task(atc_task)
                last_cart_state = atc_result.data.get('cart_state') if atc_result.data else None

                if last_cart_state:
                    self.logger.info(
                        f"  Cart state: '{last_cart_state.get('state')}' "
                        f"(signals: {last_cart_state.get('signals', [])})"
                    )

                if not atc_result.success:
                    self.logger.error(f"  add_to_cart FAILED (attempt {attempt}): {atc_result.error}")
                    if attempt < MAX_CART_ATTEMPTS and checkout_agent:
                        fake = Task(
                            type='navigate_to_cart',
                            description='recovery assessment',
                            context={'url': original_url},
                            priority=5
                        )
                        rec = await checkout_agent._detect_current_state_and_recover(
                            original_url=original_url, task=fake, attempt=attempt
                        )
                        if rec.error == 'recovery_signin_required':
                            results.extend([atc_result, rec])
                            return _gate_fail("STAGE 2 — ATC", "Sign-in required, no credentials configured.")
                        continue  # retry
                    else:
                        results.append(atc_result)
                        break

                # ── Navigate to Cart ──────────────────────────────────────────
                if last_cart_state:
                    nav_task.context['cart_state'] = last_cart_state

                nav_result = await self.execute_task(nav_task)

                if nav_result.success:
                    self.logger.info(f"  ✅ navigate_to_cart succeeded (attempt {attempt}).")
                    results.extend([atc_result, nav_result])
                    cart_phase_success = True
                    break

                recoverable = (
                    not nav_result.success
                    and nav_result.error
                    and 'recovery_signin_required' not in (nav_result.error or '')
                )
                if recoverable and attempt < MAX_CART_ATTEMPTS and checkout_agent:
                    self.logger.warning(
                        f"  navigate_to_cart failed (attempt {attempt}): {nav_result.error} — recovering..."
                    )
                    rec = await checkout_agent._detect_current_state_and_recover(
                        original_url=original_url, task=nav_task, attempt=attempt
                    )
                    if rec.error == 'recovery_signin_required':
                        results.extend([atc_result, nav_result, rec])
                        return _gate_fail("STAGE 2 — Nav", "Sign-in required during recovery.")
                    if rec.success:
                        results.extend([atc_result, rec])
                        cart_phase_success = True
                        break
                    continue  # retry
                else:
                    results.extend([atc_result, nav_result])
                    break

            if not cart_phase_success:
                return _gate_fail(
                    "STAGE 2 — Add to Cart / Navigate",
                    f"Cart phase exhausted all {MAX_CART_ATTEMPTS} recovery attempts. "
                    "No point proceeding to checkout."
                )
            self.logger.info("✅ STAGE 2 complete — item in cart, on cart page.")

        elif atc_task:
            atc_result = await self.execute_task(atc_task)
            results.append(atc_result)
            if not atc_result.success:
                return _gate_fail("STAGE 2 — ATC only", atc_result.error or "add_to_cart failed")
            cart_phase_success = True

        elif nav_task:
            nav_result = await self.execute_task(nav_task)
            results.append(nav_result)
            if not nav_result.success:
                return _gate_fail("STAGE 2 — Nav only", nav_result.error or "navigate_to_cart failed")
            cart_phase_success = True

        # ══════════════════════════════════════════════════════════════════════
        # STAGE 3 — Login / Guest Checkout
        # ══════════════════════════════════════════════════════════════════════
        if login_task:
            current_stage = await self._detect_current_checkout_stage(page) if page else 3
            if current_stage > 3:
                self.logger.info(f"⏭️ Skipping Stage 3 (Login/Guest) because page is already at Stage {current_stage}")
                results.append(Result(success=True, task=login_task, data={'method': 'fast_forward_skipped', 'skipped_by_state_detection': True}))
            else:
                self.logger.info("━━ STAGE 3: Login / Guest Checkout ━━")
                login_result = await self.execute_task(login_task)
                results.append(login_result)
                if not login_result.success:
                    return _gate_fail(
                        "STAGE 3 — Login/Guest",
                        f"{login_result.error}. "
                        "Cannot fill contact/address on a login page."
                    )
                self.logger.info("✅ STAGE 3 complete — past login/guest wall.")

        # ══════════════════════════════════════════════════════════════════════
        # STAGE 4 — Fill Contact (email / phone)
        # ══════════════════════════════════════════════════════════════════════
        if contact_task:
            current_stage = await self._detect_current_checkout_stage(page) if page else 4
            if current_stage > 4:
                self.logger.info(f"⏭️ Skipping Stage 4 (Fill Contact) because page is already at Stage {current_stage}")
                results.append(Result(success=True, task=contact_task, data={'method': 'fast_forward_skipped', 'skipped_by_state_detection': True}))
            else:
                self.logger.info("━━ STAGE 4: Fill Contact Info ━━")
                contact_result = await self.execute_task(contact_task)
                results.append(contact_result)
                if not contact_result.success:
                    return _gate_fail(
                        "STAGE 4 — Fill Contact",
                        f"{contact_result.error}. "
                        "Cannot fill shipping address without contact info."
                    )
                self.logger.info("✅ STAGE 4 complete — contact info filled.")

        # ══════════════════════════════════════════════════════════════════════
        # STAGE 5 — Fill Shipping Address
        # ══════════════════════════════════════════════════════════════════════
        if shipping_addr_task:
            current_stage = await self._detect_current_checkout_stage(page) if page else 5
            if current_stage > 5:
                self.logger.info(f"⏭️ Skipping Stage 5 (Fill Shipping Address) because page is already at Stage {current_stage}")
                results.append(Result(success=True, task=shipping_addr_task, data={'method': 'fast_forward_skipped', 'skipped_by_state_detection': True}))
            else:
                self.logger.info("━━ STAGE 5: Fill Shipping Address ━━")
                addr_result = await self.execute_task(shipping_addr_task)
                results.append(addr_result)
                if not addr_result.success:
                    return _gate_fail(
                        "STAGE 5 — Fill Shipping Address",
                        f"{addr_result.error}. "
                        "Cannot select shipping method without a valid address."
                    )
                self.logger.info("✅ STAGE 5 complete — shipping address filled.")

        # ══════════════════════════════════════════════════════════════════════
        # STAGE 6 — Select Shipping Method (cheapest / free)
        # ══════════════════════════════════════════════════════════════════════
        if shipping_method_task:
            current_stage = await self._detect_current_checkout_stage(page) if page else 6
            if current_stage > 6:
                self.logger.info(f"⏭️ Skipping Stage 6 (Select Shipping Method) because page is already at Stage {current_stage}")
                results.append(Result(success=True, task=shipping_method_task, data={'method': 'fast_forward_skipped', 'skipped_by_state_detection': True}))
            else:
                self.logger.info("━━ STAGE 6: Select Shipping Method ━━")
                ship_result = await self.execute_task(shipping_method_task)
                results.append(ship_result)
                if not ship_result.success:
                    return _gate_fail(
                        "STAGE 6 — Shipping Method",
                        f"{ship_result.error}. "
                        "Cannot proceed to payment without a shipping method."
                    )
                self.logger.info("✅ STAGE 6 complete — shipping method selected.")

        # ══════════════════════════════════════════════════════════════════════
        # STAGE 7 — Place Order (manual review gate if configured)
        # ══════════════════════════════════════════════════════════════════════
        if place_order_task:
            self.logger.info("━━ STAGE 7: Place Order / Review ━━")
            order_result = await self.execute_task(place_order_task)
            results.append(order_result)
            # No downstream gate — this is the final step
            if order_result.success:
                self.logger.info("✅ STAGE 7 complete — order placed / paused for manual review.")
            else:
                self.logger.warning(f"⚠️  Place Order step reported: {order_result.error}")

        return results

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics for all agents"""
        return {
            'total_agents': len(self.agents),
            'agents': [agent.get_stats() for agent in self.agents]
        }
