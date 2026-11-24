#!/usr/bin/env python3
"""
Post-Selection Validator
Validates that the correct variant was selected after clicking
"""

import logging
from typing import Dict, Any, Optional
from playwright.async_api import Page

logger = logging.getLogger(__name__)


class SelectionValidator:
    """
    Validates variant selections after clicking
    Checks DOM to confirm the right option was selected
    """
    
    def __init__(self, page: Page, llm_client=None):
        self.page = page
        self.llm = llm_client
        self.initial_cart_count = None

    def set_initial_cart_count(self, count: Optional[int]):
        """Set the initial cart count before a click action."""
        self.initial_cart_count = count
        logger.info(f"Validator: Initial cart count set to {count}")
    
    async def validate_color_selection(self, expected_color: str) -> Dict[str, Any]:
        """
        Validate that the correct color was selected
        
        STRICT validation - only passes if we find MATCHING selected element
        
        Checks:
        1. Selected attribute on buttons/swatches
        2. Active/checked classes
        3. aria-checked attributes
        4. Text content of selected elements MUST match expected
        
        Args:
            expected_color: The color we tried to select (e.g., "Olive Navy Glen Plaid")
        
        Returns:
            {
                'validated': bool,
                'selected_text': str,
                'confidence': float,
                'method': str
            }
        """
        expected_lower = expected_color.lower().strip()
        expected_normalized = expected_lower.replace(' ', '').replace('-', '')
        
        logger.info(f"VALIDATING: Looking for selected element matching '{expected_color}'")
        
        # Strategy 0: SIMPLE PAGE CHECK - Look for "Color: Value" text on page (NEW - MOST RELIABLE!)
        try:
            from ..utils.simple_page_validator import SimplePageValidator
            
            variant_type = getattr(self, '_current_variant_type', 'color')
            simple_validator = SimplePageValidator(self.page)
            result = await simple_validator.validate(variant_type, expected_color)
            
            if result['validated']:
                logger.info(f"Validated by simple page check")
                return result
        except Exception as e:
            logger.debug(f"Simple page validation failed: {e}")
        
        # Strategy 1: Check variant label text change (for some product pages)
        try:
            from ..utils.variant_label_validator import VariantLabelValidator
            
            # Use stored variant_type if available, otherwise default to 'color'
            variant_type = getattr(self, '_current_variant_type', 'color')
            
            label_validator = VariantLabelValidator(self.page)
            text_validation = await label_validator.validate_by_text_change(
                variant_type, 
                expected_color
            )
            
            if text_validation['validated']:
                logger.info(f"Validation success via text change: '{text_validation['selected_text']}'")
                return text_validation
        except Exception as e:
            logger.debug(f"Variant label text validation failed (trying other methods): {e}")
        
        # Strategy 1: Check dropdowns/select elements (for Amazon, etc.)
        try:
            js_check_selects = """
            (expectedText) => {
                const selects = document.querySelectorAll('select');
                const normalized = expectedText.toLowerCase().replace(/[\\s-]/g, '');
                
                for (const select of selects) {
                    const selectedOption = select.options[select.selectedIndex];
                    if (selectedOption) {
                        const optText = selectedOption.textContent.trim();
                        const optNormalized = optText.toLowerCase().replace(/[\\s-]/g, '');
                        
                        // Check if matches
                        if (optNormalized === normalized || 
                            optNormalized.includes(normalized) || 
                            normalized.includes(optNormalized)) {
                            return {
                                found: true,
                                text: optText,
                                value: selectedOption.value
                            };
                        }
                        
                        // Word overlap check
                        const expectedWords = expectedText.toLowerCase().split(/\\s+/).filter(w => w.length > 2);
                        const optWords = optText.toLowerCase().split(/\\s+/).filter(w => w.length > 2);
                        const overlap = expectedWords.filter(w => optWords.includes(w));
                        
                        if (overlap.length >= expectedWords.length * 0.6 && overlap.length >= 1) {
                            return {
                                found: true,
                                text: optText,
                                value: selectedOption.value
                            };
                        }
                    }
                }
                return {found: false};
            }
            """
            
            result = await self.page.evaluate(js_check_selects, expected_color)
            
            if result.get('found'):
                logger.info(f"Validation success: Found in dropdown - '{result['text']}'")
                return {
                    'validated': True,
                    'selected_text': result['text'],
                    'confidence': 0.95,
                    'method': 'dropdown selected option'
                }
        except Exception as e:
            logger.debug(f"Dropdown check failed (expected): {e}")
        
        # Strategy 1: Check for selected/active elements
        try:
            # Common selectors for selected color swatches
            selected_selectors = [
                '[aria-checked="true"]',
                '[aria-selected="true"]',
                '.selected',
                '.active',
                '[data-selected="true"]',
                'button[aria-pressed="true"]',
                'input[type="radio"]:checked + label',
                'input[type="radio"]:checked'
            ]
            
            found_any_selected = False
            
            for selector in selected_selectors:
                elements = await self.page.query_selector_all(selector)
                
                if elements:
                    logger.debug(f"Found {len(elements)} elements with selector: {selector}")
                
                for element in elements:
                    # Check if element is inside header, footer, nav, menu, review, or carousel
                    is_excluded = await element.evaluate("""
                        (el) => {
                            let parent = el;
                            while (parent && parent !== document.body) {
                                const tag = parent.tagName;
                                const className = parent.className || '';
                                if (tag === 'HEADER' || tag === 'FOOTER' || tag === 'NAV' ||
                                    /header|footer|nav|menu|review|recommend|carousel/i.test(className)) {
                                    return true;
                                }
                                parent = parent.parentElement;
                            }
                            return false;
                        }
                    """)
                    if is_excluded:
                        continue
                        
                    found_any_selected = True
                    
                    # Get text content
                    text = await element.text_content()
                    if not text:
                        # Try aria-label
                        text = await element.get_attribute('aria-label')
                    if not text:
                        # Try data-color or similar
                        text = await element.get_attribute('data-color')
                    if not text:
                        # Try value attribute
                        text = await element.get_attribute('value')
                    
                    if text:
                        text_stripped = text.strip()
                        text_lower = text_stripped.lower()
                        text_normalized = text_lower.replace(' ', '').replace('-', '')
                        
                        logger.debug(f"  Checking selected element: '{text_stripped}' (selector: {selector})")
                        
                        # STRICT: Require actual match
                        # Exact match
                        if expected_lower == text_lower or expected_normalized == text_normalized:
                            logger.info(f"Validation success: Exact match '{text_stripped}'")
                            return {
                                'validated': True,
                                'selected_text': text_stripped,
                                'confidence': 0.95,
                                'method': f'DOM exact: {selector}'
                            }
                        
                        # Contains match / word boundary match for short expected values
                        is_exact = (expected_normalized == text_normalized)
                        if is_exact:
                            has_match = True
                        elif len(expected_normalized) <= 3:
                            import re
                            escaped = re.escape(expected_lower)
                            has_match = bool(re.search(r"(?:^|[^a-z0-9'])" + escaped + r"(?:$|[^a-z0-9'])", text_lower))
                        else:
                            has_match = expected_normalized in text_normalized or text_normalized in expected_normalized
                        
                        if has_match:
                            logger.info(f"Validation success: Matches expected variant '{text_stripped}'")
                            return {
                                'validated': True,
                                'selected_text': text_stripped,
                                'confidence': 0.85,
                                'method': f'DOM matched: {selector}'
                            }
                        
                        # Keyword match (require 60% of expected words)
                        # FIXED: Allow numeric values and short tokens (1+ chars)
                        expected_words = set(w for w in expected_lower.split() if len(w) >= 1 or w.isdigit())
                        text_words = set(w for w in text_lower.split() if len(w) >= 1 or w.isdigit())
                        
                        if expected_words and text_words:
                            overlap = expected_words & text_words
                            overlap_ratio = len(overlap) / len(expected_words)
                            
                            if len(overlap) >= 1 and overlap_ratio >= 0.6:
                                logger.info(f"Validation success: Keyword match '{text_stripped}' (matched {len(overlap)}/{len(expected_words)} words)")
                                return {
                                    'validated': True,
                                    'selected_text': text_stripped,
                                    'confidence': 0.7,
                                    'method': f'DOM keywords: {selector}'
                                }
                        
                        # Semantic match fallback using LLM
                        if await self._semantic_match(expected_color, text_stripped):
                            logger.info(f"Validation success: Semantic LLM match '{text_stripped}'")
                            return {
                                'validated': True,
                                'selected_text': text_stripped,
                                'confidence': 0.85,
                                'method': f'DOM semantic LLM: {selector}'
                            }
            
            if found_any_selected:
                logger.warning(f"Found selected elements but NONE matched '{expected_color}'")
        
        except Exception as e:
            logger.warning(f"DOM validation failed: {e}")
        
        # Strategy 2: Check URL params (some sites update URL after variant selection)
        # IMPORTANT: Skip this for short values (≤ 3 chars) — single letters like 'L', 'M'
        # appear in virtually every URL string (in domain, path, query) causing false positives.
        try:
            if len(expected_normalized) > 3:
                url = self.page.url
                url_lower = url.lower()
                url_slug = url_lower.replace(' ', '').replace('-', '').replace('_', '').replace('+', '')
                if expected_normalized in url_slug:
                    logger.info(f"Validation success: Found in URL")
                    return {
                        'validated': True,
                        'selected_text': 'URL parameter',
                        'confidence': 0.8,
                        'method': 'URL'
                    }
            else:
                logger.debug(f"URL validation skipped for short value '{expected_color}' (≤ 3 chars)")
        except Exception as e:
            logger.warning(f"URL validation failed: {e}")
        
        # Validation failed
        logger.warning(f"❌ Validation FAILED: Could not confirm '{expected_color}' was selected")
        logger.warning(f"  No selected element text matched the expected value")
        return {
            'validated': False,
            'selected_text': None,
            'confidence': 0.0,
            'method': 'none'
        }
    
    async def validate_size_selection(self, expected_size: str) -> Dict[str, Any]:
        """Validate size selection (similar logic to color)"""
        return await self.validate_color_selection(expected_size)  # Same logic works
    
    async def _get_cart_count(self) -> Optional[int]:
        """Get the current cart item count from the page in a site-agnostic way."""
        js_code = """
        () => {
            const selectors = [
                '.nav-cart-count', // Amazon
                '._2Rz2C', // Flipkart
                '.cart-badge',
                '.cart-count',
                '.cart-qty',
                '.cart-quantity',
                '.bag-count',
                '.bag-qty',
                '.bag-quantity',
                '[class*="cart-count" i]',
                '[class*="cart-qty" i]',
                '[class*="cart-badge" i]',
                '[class*="bag-count" i]',
                '[class*="bag-qty" i]',
                '[class*="bag-badge" i]',
                '#cart-count',
                '#bag-count'
            ];
            
            for (const sel of selectors) {
                try {
                    const el = document.querySelector(sel);
                    if (el && el.offsetHeight > 0 && el.offsetWidth > 0) {
                        const txt = el.textContent.trim();
                        const num = parseInt(txt.replace(/[^0-9]/g, ''), 10);
                        if (!isNaN(num)) return num;
                    }
                } catch (e) {}
            }
            
            const allElements = document.querySelectorAll('span, a, div, button, p');
            for (const el of allElements) {
                try {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    
                    const txt = el.textContent.trim();
                    if (txt.length > 0 && txt.length < 50) {
                        const match = txt.match(/(?:cart|bag|basket|items?)\\s*(?:\\(|:)?\\s*(\\d+)\\s*\\)?/i);
                        if (match) {
                            return parseInt(match[1], 10);
                        }
                    }
                } catch (e) {}
            }
            return null;
        }
        """
        try:
            count = await self.page.evaluate(js_code)
            return count
        except Exception:
            return None

    async def _is_cart_drawer_visible(self) -> bool:
        """Check if a cart drawer or success modal is visible on the page."""
        js_code = """
        () => {
            const keywords = ['added to cart', 'added to bag', 'added to basket', 'item added', 'added!', 'view cart', 'checkout', 'go to cart', 'shopping bag'];
            
            const allElements = document.querySelectorAll('div, span, button, a, h1, h2, h3');
            for (const el of allElements) {
                try {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
                    
                    const text = (el.textContent || '').toLowerCase().trim();
                    if (text.length > 0 && text.length < 100) {
                        if (keywords.some(kw => text.includes(kw))) {
                            let parent = el;
                            while (parent && parent !== document.body) {
                                const className = (parent.className || '').toLowerCase();
                                const id = (parent.id || '').toLowerCase();
                                const combined = className + ' ' + id;
                                if (combined.includes('drawer') || combined.includes('modal') || combined.includes('popup') || combined.includes('flyout') || combined.includes('overlay') || combined.includes('minicart') || combined.includes('side-panel') || combined.includes('sidesheet')) {
                                    return true;
                                }
                                parent = parent.parentElement;
                            }
                        }
                    }
                } catch (e) {}
            }
            return false;
        }
        """
        try:
            return await self.page.evaluate(js_code)
        except Exception:
            return False

    async def validate_any_selection(self, variant_type: str, expected_value: str) -> Dict[str, Any]:
        """Generic validation for any variant type"""
        # Store variant_type for use in validation
        self._current_variant_type = variant_type
        
        # 1. First run DOM-based validation strategies (instant checks)
        if variant_type.lower() == 'color':
            res = await self.validate_color_selection(expected_value)
        elif variant_type.lower() == 'size':
            res = await self.validate_size_selection(expected_value)
        else:
            res = await self.validate_color_selection(expected_value)  # Generic
            
        if res.get('validated'):
            return res
            
        # 2. DOM validation failed - check if it was immediately added to cart (instant checks, no sleep)
        try:
            current_cart_count = await self._get_cart_count()
            if self.initial_cart_count is not None and current_cart_count is not None:
                if current_cart_count > self.initial_cart_count:
                    logger.info(f"✅ Validation success via Cart Count increase (instant): {self.initial_cart_count} -> {current_cart_count}")
                    return {
                        'validated': True,
                        'selected_text': f"Added to Cart (Count: {current_cart_count})",
                        'confidence': 0.95,
                        'method': 'cart_count_increase'
                    }
        except Exception as e:
            logger.warning(f"Error doing instant cart count validation check: {e}")
            
        try:
            if await self._is_cart_drawer_visible():
                logger.info("✅ Validation success via Cart Drawer/Modal visibility (instant)")
                return {
                    'validated': True,
                    'selected_text': "Cart Drawer/Modal Visible",
                    'confidence': 0.90,
                    'method': 'cart_drawer_visible'
                }
        except Exception as e:
            logger.warning(f"Error doing instant cart drawer validation check: {e}")
            
        # 3. Still not validated - poll briefly as a last resort before reporting failure
        # Poll up to 1.5 seconds (3 attempts, 500ms sleep) for cart count increase
        try:
            import asyncio
            for attempt in range(3):
                await asyncio.sleep(0.5)
                current_cart_count = await self._get_cart_count()
                logger.info(f"Validation Cart Poll Check (Attempt {attempt+1}): Initial count = {self.initial_cart_count}, Current count = {current_cart_count}")
                if self.initial_cart_count is not None and current_cart_count is not None:
                    if current_cart_count > self.initial_cart_count:
                        logger.info(f"✅ Validation success via Cart Count increase (poll): {self.initial_cart_count} -> {current_cart_count}")
                        return {
                            'validated': True,
                            'selected_text': f"Added to Cart (Count: {current_cart_count})",
                            'confidence': 0.95,
                            'method': 'cart_count_increase'
                        }
        except Exception as e:
            logger.warning(f"Error polling cart count validation fallback: {e}")
            
        # Poll up to 1.0 second (2 attempts, 500ms sleep) for drawer visibility
        try:
            for attempt in range(2):
                await asyncio.sleep(0.5)
                if await self._is_cart_drawer_visible():
                    logger.info("✅ Validation success via Cart Drawer/Modal visibility (poll)")
                    return {
                        'validated': True,
                        'selected_text': "Cart Drawer/Modal Visible",
                        'confidence': 0.90,
                        'method': 'cart_drawer_visible'
                    }
        except Exception as e:
            logger.warning(f"Error polling cart drawer validation fallback: {e}")
            
        return res

    def _is_local_synonym(self, expected: str, selected: str) -> bool:
        """
        Check if expected and selected strings are known synonyms (case-insensitive).
        Handles colors, sizes, and dimensions.
        """
        exp = expected.lower().strip()
        sel = selected.lower().strip()
        
        if exp == sel:
            return True
            
        # Common clothing size mappings
        size_synonyms = {
            'xs': {'extra small', 'x-small', 'x small', 'extra-small'},
            's': {'small'},
            'm': {'medium'},
            'l': {'large'},
            'xl': {'extra large', 'x-large', 'x large', 'extra-large'},
            'xxl': {'double extra large', 'xx-large', 'xx large', '2xl', '2x-large'},
            'xxxl': {'triple extra large', 'xxx-large', 'xxx large', '3xl', '3x-large'},
            'xxxxl': {'4xl', '4x-large', '4x large'},
            'os': {'one size', 'one-size', 'one size fits all'},
        }
        
        # Check standard mappings (bidirectional)
        for key, val in size_synonyms.items():
            if exp == key and sel in val:
                return True
            if sel == key and exp in val:
                return True
            if exp in val and sel in val:
                return True
                
        # Common color synonyms/variants
        color_synonyms = [
            {'black', ' Polo Black', 'polo black', 'active black', 'matte black', 'core black'},
            {'white', 'active white', 'core white', 'off white', 'off-white', 'eggshell', 'cream', 'ivory'},
            {'grey', 'gray', 'heather grey', 'heather gray', 'slate', 'slate grey', 'slate gray', 'charcoal', 'dark grey', 'light grey'},
            {'blue', 'navy', 'navy blue', 'active navy', 'indigo', 'royal blue', 'light blue', 'sky blue'},
            {'green', 'olive', 'olive green', 'forest green', 'sage', 'mint'},
            {'red', 'crimson', 'scarlet', 'burgundy', 'maroon'},
        ]
        
        for synonym_set in color_synonyms:
            if exp in synonym_set and sel in synonym_set:
                return True
                
        # Normalized dimensions (e.g. 32W x 30L, 32w 30l, 32x30)
        # Normalize by extracting digits
        import re
        exp_digits = re.findall(r'\d+', exp)
        sel_digits = re.findall(r'\d+', sel)
        if exp_digits and exp_digits == sel_digits:
            return True
            
        return False

    async def _semantic_match(self, expected: str, selected: str) -> bool:
        """
        Check if selected value is semantically equivalent to expected value using LLM.
        """
        if not expected or not selected:
            return False
            
        exp_normalized = expected.lower().strip().replace(' ', '').replace('-', '')
        sel_normalized = selected.lower().strip().replace(' ', '').replace('-', '')

        # Exact match (normalised) — safe
        if exp_normalized == sel_normalized:
            return True

        # Do NOT use substring containment here — "xl" in "2xl" is True in Python,
        # which would wrongly match XL with 2XL.  Let _is_local_synonym and the LLM handle it.
            
        # Local synonym check
        if self._is_local_synonym(expected, selected):
            logger.info(f"🎯 Local synonym match success: '{selected}' is equivalent to expected '{expected}'")
            return True
            
        # Get LLM client if not already loaded
        if not getattr(self, 'llm', None):
            try:
                from .llm_client import get_llm_client
                self.llm = get_llm_client()
            except Exception:
                pass
                
        if not self.llm:
            return False
            
        try:
            import json
            import re
            
            prompt = f"""
            Determine if the following two product variant option values are semantically equivalent (refer to the exact same option).

            Expected Option: "{expected}"
            Selected Option on Page: "{selected}"

            Rules:
            - Size abbreviations are STRICTLY distinct: "XL" and "2XL" are NOT equivalent. "L" and "XL" are NOT equivalent. "S" and "XS" are NOT equivalent.
            - Synonym pairs that ARE equivalent: "L"="Large", "M"="Medium", "S"="Small", "XL"="X-Large"="Extra Large", "XXL"="2XL"="Double Extra Large".
            - Color synonyms are acceptable if they refer to the same color family: "Slate Gray"="Slate", "Navy Blue"="Navy".
            - Different colors (even similar) are NOT equivalent: "Navy" and "Black" are NOT equivalent. "XL" and "2XL" are NOT equivalent.
            - If in doubt, return false.

            Return ONLY a valid JSON response:
            {{
                "equivalent": true/false,
                "reasoning": "one sentence"
            }}
            """
            
            response = await self.llm.complete(prompt, max_tokens=128)
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                is_equivalent = data.get("equivalent", False)
                if is_equivalent:
                    logger.info(f"🧠 LLM validation match success: '{selected}' is equivalent to expected '{expected}' (Reason: {data.get('reasoning')})")
                return is_equivalent
        except Exception as e:
            logger.warning(f"LLM semantic selection validation failed: {e}")
            
        return False

    async def validate_via_visual_diff(self, bbox: Dict[str, Any], before_img: str, after_img: str) -> Dict[str, Any]:
        """
        Validates selection by comparing element bounding box region before and after clicking.
        Uses local OpenCV pixel diffing. If a change is detected, uses VLM to confirm it represents selection.
        """
        import cv2
        import numpy as np
        import os
        import json
        import re
        
        logger.info(f"Visual Diff Validation: checking bbox {bbox}")
        
        try:
            # 1. Load screenshots
            img_before = cv2.imread(before_img)
            img_after = cv2.imread(after_img)
            
            if img_before is None or img_after is None:
                logger.warning("Could not load screenshots for visual diff validation")
                return {'validated': False, 'confidence': 0.0, 'method': 'visual_diff_error'}
                
            h_img, w_img, _ = img_before.shape
            
            width = bbox.get('width', 30)
            height = bbox.get('height', 30)
            
            if 'center_x' in bbox:
                x = bbox['center_x'] - width / 2
                y = bbox['center_y'] - height / 2
            else:
                x = bbox.get('x', 0)
                y = bbox.get('y', 0)
                
            # Add padding
            padding = 10
            x1 = max(0, int(x - padding))
            y1 = max(0, int(y - padding))
            x2 = min(w_img, int(x + width + padding))
            y2 = min(h_img, int(y + height + padding))
            
            # Crop regions
            crop_before = img_before[y1:y2, x1:x2]
            crop_after = img_after[y1:y2, x1:x2]
            
            if crop_before.size == 0 or crop_after.size == 0:
                logger.warning("Cropped region is empty")
                return {'validated': False, 'confidence': 0.0, 'method': 'visual_diff_empty'}
                
            # 2. Local OpenCV diff
            gray_before = cv2.cvtColor(crop_before, cv2.COLOR_BGR2GRAY)
            gray_after = cv2.cvtColor(crop_after, cv2.COLOR_BGR2GRAY)
            
            # Calculate absolute difference
            diff = cv2.absdiff(gray_before, gray_after)
            _, thresh = cv2.threshold(diff, 15, 255, cv2.THRESH_BINARY)
            
            changed_pixels = cv2.countNonZero(thresh)
            total_pixels = thresh.shape[0] * thresh.shape[1]
            change_ratio = changed_pixels / total_pixels
            
            logger.info(f"Visual Diff: {changed_pixels} pixels changed out of {total_pixels} ({change_ratio:.2%})")
            
            # Save cropped images for debugging/VLM
            crop_before_path = "temp_crop_before.png"
            crop_after_path = "temp_crop_after.png"
            cv2.imwrite(crop_before_path, crop_before)
            cv2.imwrite(crop_after_path, crop_after)
            
            # If change ratio is extremely low, there's no visual change (selection failed)
            if change_ratio < 0.005:
                logger.info("❌ Visual Diff: No significant visual change detected.")
                return {
                    'validated': False,
                    'selected_text': None,
                    'confidence': 0.0,
                    'method': 'visual_diff_no_change',
                    'change_ratio': change_ratio
                }
                
            # If change ratio is significant, let's confirm selection using the VLM
            if not getattr(self, 'llm', None):
                try:
                    from .llm_client import get_llm_client
                    self.llm = get_llm_client()
                except Exception:
                    pass
            
            vlm_success = False
            if self.llm:
                try:
                    # Merge images side-by-side for the VLM to compare
                    combined_height = max(crop_before.shape[0], crop_after.shape[0])
                    combined_width = crop_before.shape[1] + crop_after.shape[1] + 10 # 10px spacing
                    
                    combined = np.zeros((combined_height, combined_width, 3), dtype=np.uint8)
                    combined.fill(255) # White background
                    
                    # Copy crops
                    combined[0:crop_before.shape[0], 0:crop_before.shape[1]] = crop_before
                    combined[0:crop_after.shape[0], crop_before.shape[1]+10:] = crop_after
                    
                    combined_path = "temp_crop_combined.png"
                    cv2.imwrite(combined_path, combined)
                    
                    vlm_model = os.getenv('GEMINI_MODEL', "gemini-1.5-flash-latest")
                    if self.llm.provider != 'gemini':
                        vlm_model = os.getenv('VLM_FALLBACK_MODEL', "nvidia/nemotron-nano-12b-v2-vl:free")
                    
                    prompt = """
                    Compare the two images of an interactive swatch/button on a webpage.
                    The LEFT image is before clicking, and the RIGHT image is after clicking.
                    
                    Determine if the button/swatch transitioned to a SELECTED state in the RIGHT image.
                    Selected state characteristics include:
                    - A border appearing around the swatch/button (often black, blue, or thick gray).
                    - A checkmark appearing on or next to it.
                    - The background color or text highlight changing.
                    - An underline appearing.
                    
                    Note: A mouse cursor hovering or clicking (blue circle overlay) is NOT a selection state. Focus on the button's own styling.
                    
                    Return ONLY a valid JSON object in this format:
                    {
                        "selected": true/false,
                        "confidence": <float between 0 and 1>,
                        "reasoning": "short explanation"
                    }
                    """
                    
                    logger.info("Calling VLM to verify selection state change...")
                    vlm_response = await self.llm.complete_with_vision(prompt, combined_path, model=vlm_model)
                    
                    json_match = re.search(r'\{.*\}', vlm_response, re.DOTALL)
                    if json_match:
                        data = json.loads(json_match.group(0))
                        if "error" in data:
                            logger.warning(f"VLM vision call returned error: {data['error']}. Falling back to heuristic.")
                        else:
                            validated = data.get("selected", False)
                            confidence = data.get("confidence", 0.8)
                            reasoning = data.get("reasoning", "")
                            
                            if validated:
                                logger.info(f"✅ VLM confirmed selection: {reasoning}")
                                return {
                                    'validated': True,
                                    'selected_text': 'Visual state change (selected)',
                                    'confidence': confidence,
                                    'method': 'visual_diff_vlm_confirmed',
                                    'reasoning': reasoning
                                }
                            else:
                                logger.info(f"❌ VLM rejected selection: {reasoning}")
                                return {
                                    'validated': False,
                                    'confidence': confidence,
                                    'method': 'visual_diff_vlm_rejected',
                                    'reasoning': reasoning
                                }
                except Exception as e:
                    logger.warning(f"VLM visual validation failed/rate-limited: {e}. Falling back to heuristic.")
            
            # If no VLM is available or it failed, fallback to a simple change ratio heuristic
            if 0.01 <= change_ratio <= 0.60:
                logger.info("✅ Visual Diff: Heuristic match based on pixel change ratio.")
                return {
                    'validated': True,
                    'selected_text': 'Visual state change (heuristic)',
                    'confidence': 0.75,
                    'method': 'visual_diff_heuristic',
                    'change_ratio': change_ratio
                }
                
        except Exception as ve:
            logger.error(f"Error during visual diff validation: {ve}")
            
        return {'validated': False, 'confidence': 0.0, 'method': 'visual_diff_failed'}
