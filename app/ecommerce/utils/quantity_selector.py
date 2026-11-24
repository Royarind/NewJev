#!/usr/bin/env python3
"""
Adaptive Quantity Selector Utility
Supports multiple e-commerce platforms with auto-detection
"""

import logging
from typing import Dict, Any, Optional
from playwright.async_api import Page
import asyncio

logger = logging.getLogger(__name__)

class QuantitySelector:
    """
    Adaptive quantity selector that works across different e-commerce platforms.
    
    Supported Patterns:
    - Strategy 1: Standard HTML <select> elements (Amazon, Walmart, etc.)
    - Strategy 2: Custom button-triggered dropdowns (Target, modern SPAs)
    - Strategy 3: Input fields with increment/decrement buttons
    - Strategy 4: Vision/LLM fallback for unknown patterns
    """
    
    def __init__(self, page: Page):
        self.page = page
        
    async def select_quantity(self, target_quantity: int) -> Dict[str, Any]:
        """
        Main entry point: Auto-detects pattern and selects quantity.
        
        Args:
            target_quantity: Desired quantity to select
            
        Returns:
            Dict with 'success', 'method', 'quantity', and optional 'error'
        """
        logger.info(f"🔢 SelectQuantity: Attempting to set quantity to {target_quantity}")
        
        # Detect which pattern the page uses
        pattern = await self._detect_pattern()
        logger.info(f"Detected quantity selector pattern: {pattern}")
        
        # Try the detected strategy
        if pattern == 'html_select':
            result = await self._select_via_html_select(target_quantity)
        elif pattern == 'custom_dropdown':
            result = await self._select_via_custom_dropdown(target_quantity)
        elif pattern == 'input_field':
            result = await self._select_via_input_field(target_quantity)
        else:
            # Unknown pattern - try all strategies in order
            logger.warning("Unknown pattern, trying all strategies...")
            for strategy_name, strategy_func in [
                ('html_select', self._select_via_html_select),
                ('custom_dropdown', self._select_via_custom_dropdown),
                ('input_field', self._select_via_input_field),
            ]:
                logger.info(f"Trying strategy: {strategy_name}")
                result = await strategy_func(target_quantity)
                if result.get('success'):
                    logger.info(f"✅ Success with strategy: {strategy_name}")
                    break
            else:
                # All strategies failed
                result = {'success': False, 'error': 'all_strategies_failed'}
        
        if result.get('success'):
            logger.info(f"✅ Successfully set quantity to {target_quantity} using {result.get('method')}")
        else:
            logger.error(f"❌ Failed to set quantity: {result.get('error')}")
            
        return result
    
    async def _detect_pattern(self) -> str:
        """
        Auto-detect which quantity selector pattern the page uses.
        
        Returns:
            'html_select' | 'custom_dropdown' | 'input_field' | 'unknown'
        """
        detection_js = """
        (() => {
            // Strategy 1: Check for standard HTML select
            const selectSelectors = [
                'select[name="quantity"]',
                'select#quantity',
                'select[id*="quantity" i]',
                'select[name*="qty" i]'
            ];
            
            for (const sel of selectSelectors) {
                const elem = document.querySelector(sel);
                if (elem && elem.offsetParent !== null) {
                    return 'html_select';
                }
            }
            
            // Strategy 2: Check for Target-style custom button dropdown
            const buttons = Array.from(document.querySelectorAll('button[id^="select-"]'));
            if (buttons.some(btn => btn.textContent.toLowerCase().includes('qty'))) {
                return 'custom_dropdown';
            }
            
            // Strategy 3: Check for input field
            const inputSelectors = [
                'input[name*="quantity" i]',
                'input[id*="quantity" i]',
                'input[type="number"]'
            ];
            
            for (const sel of inputSelectors) {
                const elem = document.querySelector(sel);
                if (elem && elem.offsetParent !== null) {
                    return 'input_field';
                }
            }
            
            // Costco/Generic fallback: check aria-label
            const ariaLabels = ['quantity', 'qty'];
            const genericInputs = document.querySelectorAll('input');
            for (const input of genericInputs) {
                const label = (input.getAttribute('aria-label') || '').toLowerCase();
                if (ariaLabels.some(kw => label.includes(kw)) && input.offsetParent !== null) {
                    return 'input_field';
                }
            }
            
            return 'unknown';
        })()
        """
        
        try:
            pattern = await self.page.evaluate(detection_js)
            return pattern
        except Exception as e:
            logger.error(f"Pattern detection failed: {e}")
            return 'unknown'
    
    async def _select_via_html_select(self, target_quantity: int) -> Dict[str, Any]:
        """
        Strategy 1: Handle standard HTML <select> elements.
        Works for: Amazon, Walmart, traditional e-commerce sites.
        """
        selectors = [
            'select[name="quantity"]',
            'select#quantity',
            'select[id*="quantity" i]',
            'select[name*="qty" i]'
        ]
        
        for selector in selectors:
            try:
                # Check if element exists and is visible
                element = await self.page.query_selector(selector)
                if not element:
                    continue
                
                is_visible = await element.is_visible()
                if not is_visible:
                    continue
                
                logger.info(f"Found HTML select using: {selector}")
                
                # Try to select the option
                await element.select_option(str(target_quantity))
                
                # Verify selection
                await asyncio.sleep(0.5)
                selected_value = await element.evaluate('el => el.value')
                
                if str(selected_value) == str(target_quantity):
                    return {
                        'success': True,
                        'method': 'html_select',
                        'quantity': target_quantity,
                        'selector': selector
                    }
                else:
                    logger.warning(f"Selection mismatch: expected {target_quantity}, got {selected_value}")
                    
            except Exception as e:
                logger.debug(f"Selector {selector} failed: {e}")
                continue
        
        return {'success': False, 'error': 'html_select_not_found'}
    
    async def _select_via_custom_dropdown(self, target_quantity: int) -> Dict[str, Any]:
        """
        Strategy 2: Handle custom button-triggered dropdowns.
        Works for: Target.com, modern SPA frameworks.
        
        This is the original Target.com logic with retry.
        """
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                logger.info(f"Custom dropdown attempt {attempt + 1}/{max_retries}")
                
                # Step 1: Find and click the trigger button
                open_menu_js = """
                ({targetQty}) => {
                    const buttons = Array.from(document.querySelectorAll('button[id^="select-"]'));
                    const qtyBtn = buttons.find(btn => btn.textContent.toLowerCase().includes('qty'));
                    
                    if (!qtyBtn) {
                        return { success: false, reason: 'qty_button_not_found' };
                    }
                    
                    const rect = qtyBtn.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) {
                        return { success: false, reason: 'qty_button_hidden' };
                    }

                    qtyBtn.click();
                    
                    return { 
                        success: true, 
                        action: 'opened_menu'
                    };
                }
                """
                
                result = await self.page.evaluate(open_menu_js, {'targetQty': target_quantity})
                if not result['success']:
                    logger.debug(f"Failed to open menu: {result.get('reason')}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)
                        continue
                    return {'success': False, 'error': result.get('reason')}
                
                # Wait for menu animation
                await asyncio.sleep(1.0)
                
                # Step 2: Click the option
                click_option_js = """
                ({targetQty}) => {
                    const target = targetQty.toString();
                    const options = Array.from(document.querySelectorAll('a, li, span, button')).filter(el => {
                        const text = el.textContent.trim();
                        const ariaLabel = el.getAttribute('aria-label') || '';
                        return (text === target || ariaLabel === target) && el.offsetParent !== null;
                    });
                    
                    if (options.length === 0) {
                        return { success: false, reason: 'option_not_found' };
                    }
                    
                    const bestOption = options.find(el => el.tagName === 'A' || el.hasAttribute('aria-label')) || options[0];
                    bestOption.click();
                    
                    return { success: true };
                }
                """
                
                select_result = await self.page.evaluate(click_option_js, {'targetQty': target_quantity})
                if not select_result['success']:
                    logger.debug(f"Failed to click option: {select_result.get('reason')}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)
                        continue
                
                # Step 3: Verify with polling
                verify_js = """
                ({targetQty}) => {
                    return new Promise((resolve) => {
                        const check = () => {
                            const buttons = Array.from(document.querySelectorAll('button[id^="select-"]'));
                            const qtyBtn = buttons.find(btn => btn.textContent.toLowerCase().includes('qty'));
                            if (!qtyBtn) return false;
                            
                            const text = qtyBtn.textContent.trim();
                            if (text.includes(targetQty.toString())) return true;
                            
                            const childDiv = qtyBtn.querySelector('div, span[class*="Text"]');
                            if (childDiv && childDiv.textContent.trim() === targetQty.toString()) return true;
                            
                            return false;
                        };
                        
                        let attempts = 0;
                        const interval = setInterval(() => {
                            attempts++;
                            if (check()) {
                                clearInterval(interval);
                                resolve(true);
                            } else if (attempts > 25) {
                                clearInterval(interval);
                                resolve(false);
                            }
                        }, 100);
                    });
                }
                """
                
                is_verified = await self.page.evaluate(verify_js, {'targetQty': target_quantity})
                
                if is_verified:
                    return {
                        'success': True,
                        'method': 'custom_dropdown_verified',
                        'quantity': target_quantity
                    }
                else:
                    logger.warning(f"Attempt {attempt+1} verification failed")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)
                        continue
            
            except Exception as e:
                logger.error(f"Custom dropdown error attempt {attempt+1}: {e}")
                await asyncio.sleep(1)
        
        return {'success': False, 'error': 'custom_dropdown_verification_failed'}
    
    async def _select_via_input_field(self, target_quantity: int) -> Dict[str, Any]:
        """
        Strategy 3: Handle numeric input fields with optional +/- buttons.
        Works for: Minimalist designs, mobile-first sites.
        """
        input_selectors = [
            'input[aria-label*="quantity" i]',
            'input[aria-label*="qty" i]',
            'input[name*="quantity" i]',
            'input[id*="quantity" i]',
            'input[type="number"]',
            'input[type="tel"]'
        ]
        
        for selector in input_selectors:
            try:
                element = await self.page.query_selector(selector)
                if not element:
                    continue
                
                is_visible = await element.is_visible()
                if not is_visible:
                    continue
                
                logger.info(f"Found input field using: {selector}")
                
                # Check for +/- buttons near this input field
                buttons_js = """
                (inputEl) => {
                    const getButtons = (startEl) => {
                        let parent = startEl.parentElement;
                        let depth = 0;
                        while (parent && depth < 3) {
                            const btns = Array.from(parent.querySelectorAll('button, a, div, span'))
                                .filter(el => {
                                    if (el === startEl) return false;
                                    const style = window.getComputedStyle(el);
                                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                                    const rect = el.getBoundingClientRect();
                                    if (rect.width < 5 || rect.height < 5) return false;
                                    return true;
                                });
                            
                            let decBtn = null;
                            let incBtn = null;
                            
                            for (const btn of btns) {
                                const text = (btn.textContent || btn.innerText || '').trim();
                                const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                                const cls = (btn.className || '').toLowerCase();
                                const id = (btn.id || '').toLowerCase();
                                
                                if (text === '-' || text === '−' || aria.includes('minus') || aria.includes('decrement') || aria.includes('sub') || cls.includes('minus') || cls.includes('dec') || id.includes('minus') || id.includes('dec')) {
                                    decBtn = btn;
                                }
                                if (text === '+' || aria.includes('plus') || aria.includes('increment') || aria.includes('add') || cls.includes('plus') || cls.includes('inc') || id.includes('plus') || id.includes('inc')) {
                                    incBtn = btn;
                                }
                            }
                            if (decBtn && incBtn) {
                                return { dec: decBtn, inc: incBtn };
                            }
                            parent = parent.parentElement;
                            depth++;
                        }
                        return null;
                    };
                    
                    const btns = getButtons(inputEl);
                    if (!btns) return null;
                    
                    const getUniqueSelector = (el) => {
                        if (el.id) return `#${el.id}`;
                        let path = [];
                        while (el && el.nodeType === Node.ELEMENT_NODE) {
                            let selector = el.nodeName.toLowerCase();
                            if (el.className) {
                                const classes = Array.from(el.classList).filter(c => typeof c === 'string' && !c.includes('active') && !c.includes('focus'));
                                if (classes.length > 0) selector += '.' + classes.join('.');
                            }
                            let sib = el, sibIndex = 1;
                            while (sib = sib.previousElementSibling) {
                                if (sib.nodeName.toLowerCase() === el.nodeName.toLowerCase()) sibIndex++;
                            }
                            selector += `:nth-of-type(${sibIndex})`;
                            path.unshift(selector);
                            el = el.parentNode;
                        }
                        return path.join(' > ');
                    };
                    
                    return {
                        decSelector: getUniqueSelector(btns.dec),
                        incSelector: getUniqueSelector(btns.inc)
                    };
                }
                """
                
                buttons_info = await self.page.evaluate(buttons_js, element)
                
                if buttons_info:
                    logger.info(f"Detected +/- buttons near quantity input: {buttons_info}")
                    
                    # Try setting quantity by clicking +/- buttons
                    current_val_str = await element.input_value()
                    try:
                        current_val = int(current_val_str.strip())
                    except:
                        current_val = 1
                    
                    max_adjust_attempts = 10
                    adjust_success = False
                    
                    for step in range(max_adjust_attempts):
                        if current_val == target_quantity:
                            adjust_success = True
                            break
                        
                        btn_selector = buttons_info['incSelector'] if current_val < target_quantity else buttons_info['decSelector']
                        btn = await self.page.query_selector(btn_selector)
                        if btn:
                            await btn.click()
                            await asyncio.sleep(0.3)
                            
                            new_val_str = await element.input_value()
                            try:
                                new_val = int(new_val_str.strip())
                            except:
                                new_val = current_val
                                
                            if new_val == current_val:
                                logger.warning(f"Quantity value did not change from {current_val} after click. Stopping button adjustment.")
                                break
                            
                            current_val = new_val
                        else:
                            logger.warning(f"Could not find button element for selector: {btn_selector}")
                            break
                    
                    if adjust_success:
                        return {
                            'success': True,
                            'method': 'buttons_adjustment',
                            'quantity': target_quantity,
                            'selector': selector
                        }
                
                # Fallback to direct typing if buttons don't exist or button adjustment failed
                logger.info("Proceeding with direct value typing fallback...")
                await element.click()
                await element.fill('')
                await element.type(str(target_quantity))
                
                # Trigger change event
                await element.evaluate('el => el.dispatchEvent(new Event("change", { bubbles: true }))')
                
                # Verify
                await asyncio.sleep(0.5)
                value = await element.input_value()
                
                if str(value) == str(target_quantity):
                    return {
                        'success': True,
                        'method': 'input_field',
                        'quantity': target_quantity,
                        'selector': selector
                    }
                else:
                    logger.warning(f"Typing mismatch: expected {target_quantity}, got {value}")
                    
            except Exception as e:
                logger.debug(f"Input field selector {selector} failed: {e}")
                continue
        
        return {'success': False, 'error': 'input_field_not_found'}
