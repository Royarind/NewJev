from enum import Enum
import logging
from typing import Dict, Any, Optional

class CheckoutState(Enum):
    CART = "cart"
    LOGIN_REQUIRED = "login_required"
    GUEST_CHECKOUT = "guest_available"
    ADDRESS_PAGE = "address_page"
    SHIPPING_METHOD = "shipping_method"
    PAYMENT_PAGE = "payment_page"
    REVIEW_PAGE = "review_page"
    CONFIRMATION = "confirmation"
    FAILED = "failed"
    UNKNOWN = "unknown"

class CheckoutStateDetector:
    """
    Detects the current state of the checkout process.
    """
    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def detect_state(self, page) -> Dict[str, Any]:
        """
        Detects the current state based on DOM elements and URL.
        Returns a dictionary with state and site info.
        """
        try:
            url = page.url
            domain = self._get_domain(url)
            
            # Simple heuristic detection (can be expanded with VLM later)
            # Prioritize Login/Address/Guest detection over Cart to avoid false positives 
            # (e.g. login page with 'cart' in return URL)
            
            if await self._is_login_page(page):
                return {"state": CheckoutState.LOGIN_REQUIRED.value, "site": domain}
            
            if await self._is_guest_checkout_option(page):
                return {"state": CheckoutState.GUEST_CHECKOUT.value, "site": domain}

            if await self._is_address_page(page):
                return {"state": CheckoutState.ADDRESS_PAGE.value, "site": domain}

            if await self._is_cart_page(page):
                return {"state": CheckoutState.CART.value, "site": domain}
                
            # Fallback/Default
            return {"state": CheckoutState.UNKNOWN.value, "site": domain}

        except Exception as e:
            self.logger.error(f"State detection failed: {e}")
            return {"state": CheckoutState.FAILED.value, "site": "unknown", "error": str(e)}

    def _get_domain(self, url: str) -> str:
        from urllib.parse import urlparse
        try:
            return urlparse(url).netloc
        except:
            return "unknown"

    async def _is_cart_page(self, page) -> bool:
        # Check for cart indicators
        if "cart" in page.url.lower() or "bag" in page.url.lower() or "basket" in page.url.lower():
             # Basic URL check often sufficient, but can check for "Proceed to Checkout" button presence
             # to confirm it's a cart view.
             # However, for this phase, we want to detect if we ARE in cart to click checkout.
             return True
        return False

    async def _is_login_page(self, page) -> bool:
        # Check for login indicators in URL
        if "login" in page.url.lower() or "signin" in page.url.lower() or "account" in page.url.lower():
             # Verify it's not just a product page with 'login' in the name (unlikely but possible)
             # But usually URL check is strong.
             return True
        
        # Check for explicit login form elements (Password field is key)
        if await page.is_visible("input[type='password']") or await page.is_visible("input[name*='password']"):
             return True
             
        # Check for specific login form containers
        if await page.is_visible("#login-form") or await page.is_visible("form[action*='login']"):
             return True

        return False

    async def _is_guest_checkout_option(self, page) -> bool:
        # Often on the login page or an interstitial
        guest_selectors = [
            "text=Guest Checkout", "text=Continue as Guest", 
            "input[value*='guest']", "#guest-checkout-button",
            "button:has-text('Guest')"
        ]
        for sel in guest_selectors:
             if await page.is_visible(sel):
                 return True
        return False

    async def _is_address_page(self, page) -> bool:
        # Detects address form fields
        address_selectors = [
            "input[name*='address']", "input[name*='shipping']", 
            "input[autocomplete='address-line1']",
            "text=Shipping Address", "text=Enter your delivery address"
        ]
        for sel in address_selectors:
             if await page.is_visible(sel):
                 return True
        return False
