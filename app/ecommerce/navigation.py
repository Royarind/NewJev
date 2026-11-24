import logging
import asyncio
from typing import Optional

class CheckoutNavigator:
    """
    Handles navigation actions for the checkout process.
    """
    def __init__(self, page):
        self.page = page
        self.logger = logging.getLogger(__name__)

    async def proceed_to_checkout(self) -> bool:
        """
        Finds and clicks the 'Proceed to Checkout' button.
        Returns True if successful, False otherwise.
        """
        self.logger.info("Attempting to proceed to checkout...")
        
        # Common selectors for checkout buttons
        checkout_selectors = [
            "text=Proceed to Checkout",
            "text=Checkout",
            "text=Secure Checkout",
            "button[name='proceedToRetailCheckout']", # Amazon specific
            "input[name='proceedToRetailCheckout']", # Amazon specific
            "#sc-buy-box-ptc-button", # Amazon specific
            "button.checkout-button",
            "a.checkout-button"
        ]
        
        for selector in checkout_selectors:
            try:
                element = await self.page.query_selector(selector)
                if element and await element.is_visible():
                    self.logger.info(f"Found checkout button: {selector}")
                    await element.click()
                    await self.page.wait_for_load_state('networkidle', timeout=10000)
                    return True
            except Exception as e:
                self.logger.warning(f"Failed to click selector {selector}: {e}")
                continue
                
        self.logger.error("Could not find 'Proceed to Checkout' button.")
        return False
