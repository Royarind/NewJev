#!/usr/bin/env python3
"""
Dropdown Selector Utility
Handles HTML <select> dropdowns for sites like Amazon
"""

import logging
from typing import Dict, Any, Optional, List
from playwright.async_api import Page
import asyncio

logger = logging.getLogger(__name__)


class DropdownSelector:
    """
    Handles selection from HTML dropdown menus
    """
    
    def __init__(self, page: Page):
        self.page = page
    
    async def find_dropdown_by_label(self, label_text: str) -> Optional[str]:
        """
        Find a dropdown by its label text (e.g., "Size", "Color")
        
        Returns:
            Selector for the dropdown, or None if not found
        """
        try:
            # Strategy 1: Find by label element
            label_selector = f'//label[contains(translate(text(), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "{label_text.lower()}")]'
            label = await self.page.query_selector(label_selector)
            
            if label:
                # Get the 'for' attribute
                for_attr = await label.get_attribute('for')
                if for_attr:
                    return f'#{for_attr}'
            
            # Strategy 2: Find select with matching name/id
            select = await self.page.query_selector(f'select[name*="{label_text}" i], select[id*="{label_text}" i]')
            if select:
                select_id = await select.get_attribute('id')
                if select_id:
                    return f'#{select_id}'
                return 'select'
            
            # Strategy 3: Find by nearby text (select elements)
            js_code = f"""
            () => {{
                const selects = document.querySelectorAll('select');
                for (const select of selects) {{
                    const parent = select.parentElement;
                    if (parent && parent.textContent.toLowerCase().includes('{label_text.lower()}')) {{
                        return select.id ? '#' + select.id : 'select';
                    }}
                }}
                return null;
            }}
            """
            result = await self.page.evaluate(js_code)
            if result:
                return result

            # Strategy 4: Find custom elements by text/labels/roles
            custom_js = f"""
            () => {{
                const label = "{label_text.lower()}";
                const candidates = Array.from(document.querySelectorAll('button, div, [role="combobox"], [aria-haspopup="listbox"]'))
                    .filter(el => {{
                        const text = (el.textContent || el.placeholder || el.id || el.name || '').toLowerCase();
                        return text.includes(label) && el.offsetParent !== null;
                    }});
                
                if (candidates.length > 0) {{
                    // Prefer buttons or comboboxes
                    const preferred = candidates.find(el => el.tagName === 'BUTTON' || el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox');
                    const target = preferred || candidates[0];
                    
                    // Helper to get unique selector
                    const getUniqueSelector = (el) => {{
                        if (el.id) return `#${{el.id}}`;
                        let path = [];
                        while (el.nodeType === Node.ELEMENT_NODE) {{
                            let selector = el.nodeName.toLowerCase();
                            if (el.className) {{
                                const classes = Array.from(el.classList).filter(c => !c.includes('active') && !c.includes('focus'));
                                if (classes.length > 0) {{
                                    selector += '.' + classes.join('.');
                                }}
                            }}
                            let sib = el, sibIndex = 1;
                            while (sib = sib.previousElementSibling) {{
                                if (sib.nodeName.toLowerCase() === el.nodeName.toLowerCase()) {{
                                    sibIndex++;
                                }}
                            }}
                            selector += `:nth-of-type(${{sibIndex}})`;
                            path.unshift(selector);
                            el = el.parentNode;
                        }}
                        return path.join(' > ');
                    }};
                    return getUniqueSelector(target);
                }}
                return null;
            }}
            """
            custom_result = await self.page.evaluate(custom_js)
            if custom_result:
                logger.info(f"Found custom dropdown selector for '{label_text}': {custom_result}")
                return custom_result
            
        except Exception as e:
            logger.warning(f"Error finding dropdown for '{label_text}': {e}")
        
        return None
    
    async def select_option_by_text(self, dropdown_selector: str, option_text: str) -> Dict[str, Any]:
        """
        Select an option from dropdown by matching text (handles both native and custom dropdowns)
        
        Args:
            dropdown_selector: CSS selector for the dropdown
            option_text: Text to match (e.g., "8 Short", "Jaded")
        
        Returns:
            {
                'success': bool,
                'selected_text': str,
                'method': str
            }
        """
        try:
            element = await self.page.query_selector(dropdown_selector)
            if not element:
                return {'success': False, 'error': f'Dropdown element not found: {dropdown_selector}'}
            
            tag_name = await element.evaluate('el => el.tagName.toLowerCase()')
            
            if tag_name == 'select':
                # Original native select logic
                # Get all options
                options_js = f"""
                (selector) => {{
                    const select = document.querySelector(selector);
                    if (!select) return null;
                    
                    const options = Array.from(select.options).map(opt => ({{
                        value: opt.value,
                        text: opt.textContent.trim(),
                        index: opt.index
                    }}));
                    
                    return options;
                }}
                """
                
                options = await self.page.evaluate(options_js, dropdown_selector)
                
                if not options:
                    return {'success': False, 'error': 'Dropdown not found'}
                
                logger.info(f"Found {len(options)} options in dropdown")
                
                # Try to match option text
                option_text_lower = option_text.lower().strip()
                option_text_normalized = option_text_lower.replace(' ', '').replace('-', '')
                
                best_match = None
                best_score = 0
                
                for option in options:
                    opt_text_lower = option['text'].lower().strip()
                    opt_text_normalized = opt_text_lower.replace(' ', '').replace('-', '')
                    
                    logger.debug(f"Checking option: '{option['text']}'")
                    
                    # Exact match
                    if opt_text_lower == option_text_lower or opt_text_normalized == option_text_normalized:
                        best_match = option
                        best_score = 1.0
                        break
                    
                    # Contains match
                    if option_text_normalized in opt_text_normalized or opt_text_normalized in option_text_normalized:
                        is_exact = (option_text_normalized == opt_text_normalized)
                        if is_exact or len(opt_text_normalized) > 1:
                            score = 0.8
                            if score > best_score:
                                best_match = option
                                best_score = score
                    
                    # Word overlap
                    option_words = set(w for w in option_text_lower.split() if len(w) >= 1)
                    opt_words = set(w for w in opt_text_lower.split() if len(w) >= 1)
                    
                    if option_words and opt_words:
                        overlap = option_words & opt_words
                        if overlap:
                            score = len(overlap) / len(option_words)
                            if score > best_score and score >= 0.5:
                                best_match = option
                                best_score = score
                
                if best_match:
                    logger.info(f"Found matching option: '{best_match['text']}' (score: {best_score:.2f})")
                    
                    # Select the option
                    await self.page.select_option(dropdown_selector, value=best_match['value'])
                    await asyncio.sleep(0.5)  # Wait for page update
                    
                    return {
                        'success': True,
                        'selected_text': best_match['text'],
                        'method': 'dropdown_select',
                        'confidence': best_score
                    }
                else:
                    logger.warning(f"No matching option found for '{option_text}'")
                    logger.warning(f"Available options: {[opt['text'] for opt in options[:5]]}")
                    return {'success': False, 'error': 'No matching option'}
            
            else:
                # Custom dropdown menu logic
                logger.info(f"Selecting option in custom dropdown: {dropdown_selector}")
                # Click custom dropdown to open it
                await element.scroll_into_view_if_needed()
                await element.click()
                await asyncio.sleep(0.5)
                
                # Use robust JS to scan the page for options and click
                js_select = """
                (optionText) => {
                    const norm = (s) => s.toLowerCase().trim().replace(/\\s+/g, ' ');
                    const targetNorm = norm(optionText);
                    
                    const containers = Array.from(document.querySelectorAll('[role="listbox"], [role="menu"], [class*="dropdown" i], [class*="menu" i], [class*="select" i]'))
                        .filter(el => {
                            const style = window.getComputedStyle(el);
                            return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null;
                        });
                        
                    let candidates = [];
                    for (const container of containers) {
                        const items = Array.from(container.querySelectorAll('[role="option"], [role="menuitem"], li, div, a, button'))
                            .filter(el => {
                                const style = window.getComputedStyle(el);
                                return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null;
                            });
                        candidates.push(...items);
                    }
                    
                    if (candidates.length === 0) {
                        candidates = Array.from(document.querySelectorAll('[role="option"], [role="menuitem"], li, div[class*="option" i], button[class*="option" i]'))
                            .filter(el => {
                                const style = window.getComputedStyle(el);
                                return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null;
                            });
                    }
                    
                    let best = null;
                    let bestScore = 0;
                    for (const cand of candidates) {
                        const text = cand.textContent || cand.innerText || '';
                        if (!text.trim()) continue;
                        
                        const candNorm = norm(text);
                        if (candNorm === targetNorm) {
                            best = cand;
                            bestScore = 1.0;
                            break;
                        }
                        if (candNorm.includes(targetNorm) || targetNorm.includes(candNorm)) {
                            const score = Math.min(candNorm.length, targetNorm.length) / Math.max(candNorm.length, targetNorm.length);
                            if (score > bestScore) {
                                best = cand;
                                bestScore = score;
                            }
                        }
                    }
                    
                    if (best) {
                        best.scrollIntoView({ block: 'center' });
                        best.click();
                        best.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                        return { success: true, text: best.textContent.trim(), score: bestScore };
                    }
                    return { success: false };
                }
                """
                
                res = await self.page.evaluate(js_select, option_text)
                if res and res.get('success'):
                    logger.info(f"Custom dropdown selection succeeded for '{option_text}': selected '{res.get('text')}' (score: {res.get('score'):.2f})")
                    return {
                        'success': True,
                        'selected_text': res.get('text'),
                        'method': 'custom_dropdown_select',
                        'confidence': res.get('score')
                    }
                
                # If custom select failed, try keyboard input as fallback
                logger.warning(f"Custom option click failed for '{option_text}', trying keyboard input fallback...")
                await element.focus()
                await self.page.keyboard.type(option_text, delay=30)
                await asyncio.sleep(0.3)
                await self.page.keyboard.press("Enter")
                await self.page.keyboard.press("Tab")
                
                new_text = await element.evaluate('el => el.textContent || el.value || ""')
                logger.info(f"Trigger text after keyboard fallback: '{new_text.strip()}'")
                return {
                    'success': True,
                    'selected_text': new_text.strip(),
                    'method': 'custom_dropdown_keyboard_fallback'
                }
        except Exception as e:
            logger.error(f"Error selecting from dropdown: {e}")
            return {'success': False, 'error': str(e)}
    
    async def select_variant(self, variant_type: str, variant_value: str) -> Dict[str, Any]:
        """
        High-level method to select a variant from a dropdown
        
        Args:
            variant_type: Type of variant (e.g., "color", "size")
            variant_value: Value to select (e.g., "Jaded", "8 Short")
        
        Returns:
            Result dictionary with success status
        """
        logger.info(f"Looking for {variant_type} dropdown...")
        
        # Find the dropdown
        dropdown_selector = await self.find_dropdown_by_label(variant_type)
        
        if not dropdown_selector:
            logger.warning(f"Could not find dropdown for '{variant_type}'")
            return {'success': False, 'error': f'No {variant_type} dropdown found'}
        
        logger.info(f"Found {variant_type} dropdown: {dropdown_selector}")
        
        # Select the option
        result = await self.select_option_by_text(dropdown_selector, variant_value)
        
        return result
