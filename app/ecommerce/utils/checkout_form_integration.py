#!/usr/bin/env python3
"""
Integration Guide: Using Text-LLM Form Error Recovery

Usage in CheckoutAgent:

    # In the _fill_shipping() or _fill_contact() method, after initial fill:
    
    result = await self.address_autofill.fill_address(profile)
    
    # NEW: Check for remaining form errors using TEXT LLM
    if self.llm:
        fixer = TextLLMFormFixer(self.page, self.llm)
        fix_result = await fixer.auto_fix_form_errors()
        
        if fix_result['success']:
            self.logger.info(f"✓ Form errors fixed! Actions: {fix_result['actions_taken']}")
        else:
            self.logger.error(f"❌ Remaining errors: {fix_result['remaining_errors']}")
            # Optionally raise or return partial success
"""

import asyncio
import logging
from typing import Dict, Any
from .form_state_extractor import FormStateExtractor
from .text_llm_form_fixer import TextLLMFormFixer


async def checkout_fill_with_error_recovery(page, llm_client, profile, logger=None) -> Dict[str, Any]:
    """
    Complete checkout form filling with automatic text-LLM error recovery.
    
    Args:
        page: Playwright page object
        llm_client: Text-based LLM client (no vision needed)
        profile: CheckoutProfile with user data
        logger: Optional logger
    
    Returns:
        Dict with filled_fields, errors, and recovery_actions
    """
    if logger is None:
        logger = logging.getLogger(__name__)
    
    from ..address_autofill import AddressAutofill
    from ..utils.status_display import StatusDisplay
    
    status = StatusDisplay(page)
    autofill = AddressAutofill(page, llm_client, status)
    
    # Phase 1: Initial form filling
    logger.info("Phase 1: Initial form filling...")
    result = await autofill.fill_address(profile)
    
    # Phase 2: Text-LLM error recovery
    if llm_client:
        logger.info("Phase 2: Checking for form errors using text-LLM...")
        await status.show("🧠 Analyzing form for errors...", "info", 2000)
        
        # Extract current form state
        extractor = FormStateExtractor(page)
        missing_fields = await extractor.identify_missing_required_fields()
        
        if missing_fields:
            logger.warning(f"Found {len(missing_fields)} form errors. Using text-LLM for recovery...")
            
            fixer = TextLLMFormFixer(page, llm_client)
            fix_result = await fixer.auto_fix_form_errors()
            
            if fix_result['success']:
                logger.info("✓ All form errors fixed by text-LLM!")
                await status.show("✓ Form errors corrected!", "success", 2000)
                
                return {
                    'success': True,
                    'initial_fill': result,
                    'error_recovery': fix_result,
                    'total_actions': len(result.get('filled_fields', [])) + len(fix_result['actions_taken'])
                }
            else:
                logger.error(f"Form still has errors: {fix_result['remaining_errors']}")
                await status.show("⚠️ Some form errors remain", "warning", 2000)
                
                return {
                    'success': False,
                    'initial_fill': result,
                    'error_recovery': fix_result,
                    'remaining_errors': fix_result['remaining_errors']
                }
        else:
            logger.info("✓ No form errors detected")
            return {
                'success': True,
                'initial_fill': result,
                'error_recovery': None,
                'message': 'All fields valid on first try'
            }
    else:
        logger.warning("No LLM client available for error recovery")
        return {
            'success': result.get('errors', []) == [],
            'initial_fill': result,
            'error_recovery': None
        }


# Usage example in a test
async def example_test():
    from dotenv import load_dotenv
    from playwright.async_api import async_playwright
    from app.ecommerce.profiles import CheckoutProfile, Country
    from app.ecommerce.utils.llm_client import get_llm_client
    
    load_dotenv()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        
        # Navigate to a checkout page
        await page.goto("https://example-checkout.com/checkout")
        
        # Get text-based LLM (no vision needed!)
        llm = get_llm_client()  # Uses env vars for provider/key
        
        # Create profile
        profile = CheckoutProfile(
            first_name="John",
            last_name="Doe",
            email="john@example.com",
            phone="5551234567",
            address_line1="123 Main St",
            city="New York",
            state="New York",
            postal_code="10001",
            country=Country.USA
        )
        
        # Fill with automatic error recovery
        result = await checkout_fill_with_error_recovery(
            page, llm, profile
        )
        
        if result['success']:
            print("✓ Form filled and all errors fixed!")
        else:
            print(f"❌ Errors remain: {result.get('remaining_errors')}")
        
        await asyncio.sleep(5)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(example_test())
