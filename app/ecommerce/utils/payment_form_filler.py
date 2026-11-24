import asyncio
import logging


class PaymentFormFiller:
    def __init__(self, page, status_display=None, logger=None):
        self.page = page
        self.status_display = status_display
        self.logger = logger or logging.getLogger(__name__)

    def _log(self, msg, level="info"):
        if self.status_display:
            try:
                self.status_display(msg)
            except Exception:
                pass
        getattr(self.logger, level, self.logger.info)(msg)

    async def detect_processor(self) -> str:
        """
        Scan page frames and DOM for payment processor signatures.
        Returns: 'stripe' | 'braintree' | 'adyen' | 'paypal' | 'generic' | 'none'
        """
        try:
            # Check all frames for URL-based signatures
            for frame in self.page.frames:
                url = frame.url or ""
                if "js.stripe.com" in url or "stripe.com/v3" in url:
                    self._log("Detected Stripe via frame URL")
                    return "stripe"
                if "assets.braintreegateway.com" in url:
                    self._log("Detected Braintree via frame URL")
                    return "braintree"
                if "adyen.com" in url:
                    self._log("Detected Adyen via frame URL")
                    return "adyen"
                if "paypal.com" in url:
                    self._log("Detected PayPal via frame URL")
                    return "paypal"

            # Check DOM for processor-specific signatures
            dom_checks = [
                ("stripe", [
                    "[data-stripe]",
                    ".__PrivateStripeElement",
                    "iframe[name*='__privateStripeFrame']",
                    "iframe[src*='js.stripe.com']",
                    "iframe[title*='Secure card number']",
                ]),
                ("braintree", [
                    "[data-braintree-name]",
                    "#braintree-hosted-field",
                    "iframe[id*='braintree']",
                ]),
                ("adyen", [
                    "[class*='adyen-checkout']",
                    "[data-fieldtype]",
                    "iframe[class*='adyen-checkout__iframe']",
                    "iframe[name*='adyen-encrypted']",
                ]),
                ("paypal", [
                    "[class*='paypal-button']",
                    "iframe[name*='__zoid__paypal_card']",
                ]),
            ]

            for processor, selectors in dom_checks:
                for sel in selectors:
                    try:
                        el = self.page.locator(sel).first
                        if await el.count() > 0:
                            self._log(f"Detected {processor} via DOM selector: {sel}")
                            return processor
                    except Exception:
                        continue

            # Check for generic card fields
            generic_selectors = [
                "input[type='text']",
                "input[type='tel']",
            ]
            card_keywords = ["card number", "credit card", "card no", "card num"]
            for sel in generic_selectors:
                try:
                    elements = self.page.locator(sel)
                    count = await elements.count()
                    for i in range(min(count, 20)):
                        el = elements.nth(i)
                        try:
                            if not await el.is_visible():
                                continue
                            placeholder = (await el.get_attribute("placeholder") or "").lower()
                            aria_label = (await el.get_attribute("aria-label") or "").lower()
                            name_attr = (await el.get_attribute("name") or "").lower()
                            label_text = placeholder + " " + aria_label + " " + name_attr
                            for kw in card_keywords:
                                if kw in label_text:
                                    self._log("Detected generic payment form")
                                    return "generic"
                        except Exception:
                            continue
                except Exception:
                    continue

            self._log("No payment processor detected")
            return "none"

        except Exception as e:
            self._log(f"Error during processor detection: {e}", "warning")
            return "none"

    async def fill_payment(self, profile) -> dict:
        """
        Main entry point. Detect processor, fill card fields.
        Returns: {'success': bool, 'processor': str, 'method': str, 'error': str|None}
        """
        result = {"success": False, "processor": "none", "method": "", "error": None}
        try:
            processor = await self.detect_processor()
            result["processor"] = processor

            if processor == "stripe":
                result["method"] = "stripe_iframe"
                return await self._fill_stripe(profile, result)
            elif processor == "braintree":
                result["method"] = "braintree_iframe"
                return await self._fill_braintree(profile, result)
            elif processor == "adyen":
                result["method"] = "adyen_iframe"
                return await self._fill_adyen(profile, result)
            elif processor == "paypal":
                result["method"] = "paypal_iframe"
                return await self._fill_paypal(profile, result)
            elif processor == "generic":
                result["method"] = "generic_dom"
                return await self._fill_generic(profile, result)
            else:
                result["error"] = "No supported payment processor detected"
                return result

        except Exception as e:
            self._log(f"Unexpected error in fill_payment: {e}", "warning")
            result["error"] = str(e)
            return result

    async def _try_fill_field(self, frame_loc, selectors, value, use_type=False, type_delay=30):
        """
        Try a list of selectors within a frame locator (or page).
        Returns True if fill succeeded on any selector.
        """
        for sel in selectors:
            try:
                loc = frame_loc.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.scroll_into_view_if_needed()
                    if use_type:
                        await loc.click()
                        await loc.type(value, delay=type_delay)
                    else:
                        await loc.fill(value)
                    return True
            except Exception:
                continue
        return False

    async def _fill_stripe(self, profile, result) -> dict:
        """Fill Stripe hosted fields via frame_locator."""
        try:
            frame_selectors = [
                "iframe[name*='__privateStripeFrame']",
                "iframe[src*='js.stripe.com']",
                "iframe[title*='Secure card number input frame']",
                "iframe[title*='card']",
            ]

            card_number = getattr(profile, "card_number", None)
            card_expiry = getattr(profile, "card_expiry", None)
            card_cvv = getattr(profile, "card_cvv", None)
            card_name = getattr(profile, "card_name_resolved", "")

            filled_number = False

            # Stripe may use separate iframes per field or a single combined iframe.
            # Strategy: try each frame selector, attempt to fill all fields within it.
            for frame_sel in frame_selectors:
                try:
                    fl = self.page.frame_locator(frame_sel)
                    if fl is None:
                        continue

                    if card_number and not filled_number:
                        number_selectors = [
                            "[name='cardnumber']",
                            "[placeholder*='card number' i]",
                            "[data-elements-stable-field-name='cardNumber']",
                        ]
                        filled_number = await self._try_fill_field(
                            fl, number_selectors, card_number, use_type=True, type_delay=30
                        )
                        if filled_number:
                            self._log(f"Stripe: filled card number via frame {frame_sel}")
                except Exception as e:
                    self._log(f"Stripe: error with frame {frame_sel}: {e}", "warning")
                    continue

            # Expiry — may be in a different iframe
            filled_expiry = False
            if card_expiry:
                for frame_sel in frame_selectors:
                    try:
                        fl = self.page.frame_locator(frame_sel)
                        expiry_selectors = [
                            "[name='exp-date']",
                            "[placeholder*='MM' i]",
                            "[data-elements-stable-field-name='cardExpiry']",
                        ]
                        filled_expiry = await self._try_fill_field(fl, expiry_selectors, card_expiry)
                        if filled_expiry:
                            self._log(f"Stripe: filled expiry via frame {frame_sel}")
                            break
                    except Exception as e:
                        self._log(f"Stripe: error filling expiry via {frame_sel}: {e}", "warning")

            # CVV
            filled_cvv = False
            if card_cvv:
                for frame_sel in frame_selectors:
                    try:
                        fl = self.page.frame_locator(frame_sel)
                        cvv_selectors = [
                            "[name='cvc']",
                            "[placeholder*='CVC' i]",
                            "[data-elements-stable-field-name='cardCvc']",
                        ]
                        filled_cvv = await self._try_fill_field(fl, cvv_selectors, card_cvv)
                        if filled_cvv:
                            self._log(f"Stripe: filled CVV via frame {frame_sel}")
                            break
                    except Exception as e:
                        self._log(f"Stripe: error filling CVV via {frame_sel}: {e}", "warning")

            # Name on card
            if card_name:
                for frame_sel in frame_selectors:
                    try:
                        fl = self.page.frame_locator(frame_sel)
                        name_selectors = [
                            "[name='name']",
                            "[placeholder*='name on card' i]",
                        ]
                        filled_name = await self._try_fill_field(fl, name_selectors, card_name)
                        if filled_name:
                            self._log(f"Stripe: filled name via frame {frame_sel}")
                            break
                    except Exception as e:
                        self._log(f"Stripe: error filling name via {frame_sel}: {e}", "warning")

            result["success"] = filled_number
            if not filled_number:
                result["error"] = "Stripe: could not fill card number in any iframe"
            return result

        except Exception as e:
            self._log(f"Stripe fill error: {e}", "warning")
            result["error"] = str(e)
            return result

    async def _fill_braintree(self, profile, result) -> dict:
        """Fill Braintree hosted fields — each field in its own iframe."""
        try:
            card_number = getattr(profile, "card_number", None)
            card_expiry = getattr(profile, "card_expiry", None)
            card_cvv = getattr(profile, "card_cvv", None)

            field_map = [
                ("card_number", card_number, "iframe[id*='braintree'][id*='number']",
                 ["input[type='tel']", "input[type='text']"]),
                ("expiry", card_expiry, "iframe[id*='braintree'][id*='expiration']",
                 ["input[type='tel']", "input[type='text']"]),
                ("cvv", card_cvv, "iframe[id*='braintree'][id*='cvv']",
                 ["input[type='tel']", "input[type='text']"]),
            ]

            filled_number = False
            for field_name, value, frame_sel, input_selectors in field_map:
                if not value:
                    continue
                try:
                    fl = self.page.frame_locator(frame_sel)
                    filled = await self._try_fill_field(fl, input_selectors, value)
                    if filled:
                        self._log(f"Braintree: filled {field_name}")
                        if field_name == "card_number":
                            filled_number = True
                    else:
                        self._log(f"Braintree: could not fill {field_name}", "warning")
                except Exception as e:
                    self._log(f"Braintree: error filling {field_name}: {e}", "warning")

            result["success"] = filled_number
            if not filled_number:
                result["error"] = "Braintree: could not fill card number"
            return result

        except Exception as e:
            self._log(f"Braintree fill error: {e}", "warning")
            result["error"] = str(e)
            return result

    async def _fill_adyen(self, profile, result) -> dict:
        """Fill Adyen hosted fields via iframe."""
        try:
            card_number = getattr(profile, "card_number", None)
            card_expiry = getattr(profile, "card_expiry", None)
            card_cvv = getattr(profile, "card_cvv", None)

            frame_selectors = [
                "iframe[class*='adyen-checkout__iframe']",
                "iframe[name*='adyen-encrypted']",
            ]

            field_map = [
                ("card_number", card_number, ["[data-fieldtype='encryptedCardNumber']"]),
                ("expiry", card_expiry, ["[data-fieldtype='encryptedExpiryDate']"]),
                ("cvv", card_cvv, ["[data-fieldtype='encryptedSecurityCode']"]),
            ]

            filled_number = False
            for field_name, value, input_selectors in field_map:
                if not value:
                    continue
                for frame_sel in frame_selectors:
                    try:
                        fl = self.page.frame_locator(frame_sel)
                        filled = await self._try_fill_field(fl, input_selectors, value)
                        if filled:
                            self._log(f"Adyen: filled {field_name}")
                            if field_name == "card_number":
                                filled_number = True
                            break
                    except Exception as e:
                        self._log(f"Adyen: error filling {field_name} via {frame_sel}: {e}", "warning")

            result["success"] = filled_number
            if not filled_number:
                result["error"] = "Adyen: could not fill card number"
            return result

        except Exception as e:
            self._log(f"Adyen fill error: {e}", "warning")
            result["error"] = str(e)
            return result

    async def _fill_paypal(self, profile, result) -> dict:
        """Fill PayPal hosted card fields — separate iframes per field."""
        try:
            card_number = getattr(profile, "card_number", None)
            card_expiry = getattr(profile, "card_expiry", None)
            card_cvv = getattr(profile, "card_cvv", None)

            field_map = [
                ("card_number", card_number,
                 "iframe[name='__zoid__paypal_card_number']",
                 ["input[name='card-number']", "input[type='tel']", "input[type='text']"]),
                ("expiry", card_expiry,
                 "iframe[name='__zoid__paypal_card_expiry']",
                 ["input[name='expiry-date']", "input[type='tel']", "input[type='text']"]),
                ("cvv", card_cvv,
                 "iframe[name='__zoid__paypal_card_cvv']",
                 ["input[name='cvv']", "input[type='tel']", "input[type='text']"]),
            ]

            filled_number = False
            for field_name, value, frame_sel, input_selectors in field_map:
                if not value:
                    continue
                try:
                    fl = self.page.frame_locator(frame_sel)
                    filled = await self._try_fill_field(fl, input_selectors, value)
                    if filled:
                        self._log(f"PayPal: filled {field_name}")
                        if field_name == "card_number":
                            filled_number = True
                    else:
                        self._log(f"PayPal: could not fill {field_name}", "warning")
                except Exception as e:
                    self._log(f"PayPal: error filling {field_name}: {e}", "warning")

            result["success"] = filled_number
            if not filled_number:
                result["error"] = "PayPal: could not fill card number"
            return result

        except Exception as e:
            self._log(f"PayPal fill error: {e}", "warning")
            result["error"] = str(e)
            return result

    async def _fill_generic(self, profile, result) -> dict:
        """Fill generic card fields in the main page context."""
        try:
            card_number = getattr(profile, "card_number", None)
            card_expiry = getattr(profile, "card_expiry", None)
            card_cvv = getattr(profile, "card_cvv", None)
            card_name = getattr(profile, "card_name_resolved", "")

            field_map = [
                ("card_number", card_number, [
                    "input[name*='card' i][name*='number' i]",
                    "input[id*='cardNumber' i]",
                    "input[autocomplete='cc-number']",
                    "input[placeholder*='card number' i]",
                ]),
                ("expiry", card_expiry, [
                    "input[name*='expir' i]",
                    "input[autocomplete='cc-exp']",
                    "input[placeholder*='MM/YY' i]",
                    "input[placeholder*='expiry' i]",
                ]),
                ("cvv", card_cvv, [
                    "input[name*='cvv' i]",
                    "input[name*='cvc' i]",
                    "input[autocomplete='cc-csc']",
                    "input[placeholder*='CVV' i]",
                ]),
                ("name", card_name, [
                    "input[name*='card' i][name*='name' i]",
                    "input[autocomplete='cc-name']",
                ]),
            ]

            filled_number = False
            for field_name, value, selectors in field_map:
                if not value:
                    continue
                for sel in selectors:
                    try:
                        loc = self.page.locator(sel).first
                        if await loc.count() > 0 and await loc.is_visible():
                            await loc.scroll_into_view_if_needed()
                            await loc.fill(value)
                            # Dispatch events to ensure JS validation picks up the value
                            try:
                                await self.page.evaluate(
                                    """(selector) => {
                                        const el = document.querySelector(selector);
                                        if (el) {
                                            el.dispatchEvent(new Event('input', { bubbles: true }));
                                            el.dispatchEvent(new Event('change', { bubbles: true }));
                                        }
                                    }""",
                                    sel,
                                )
                            except Exception:
                                pass
                            self._log(f"Generic: filled {field_name} via {sel}")
                            if field_name == "card_number":
                                filled_number = True
                            break
                    except Exception as e:
                        self._log(f"Generic: error filling {field_name} via {sel}: {e}", "warning")
                        continue

            result["success"] = filled_number
            if not filled_number:
                result["error"] = "Generic: could not fill card number"
            return result

        except Exception as e:
            self._log(f"Generic fill error: {e}", "warning")
            result["error"] = str(e)
            return result
