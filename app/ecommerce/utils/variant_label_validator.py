#!/usr/bin/env python3
"""
Variant Label Text Validator
Tracks text changes near variant labels (Color:, Size:, etc.)
Most reliable validation method for product variants
"""

import logging
from typing import Dict, Any, Optional, Tuple
from playwright.async_api import Page
from pathlib import Path
import asyncio

logger = logging.getLogger(__name__)


class VariantLabelValidator:
    """
    Validates variant selections by tracking text changes near labels
    
    Example:
        Before: "Color: Select a color"
        After:  "Color: Jaded"
        
        Validates by checking if "Jaded" appears after "Color:"
    """
    
    def __init__(self, page: Page):
        self.page = page
    
    async def capture_variant_section_text(self, variant_type: str) -> Dict[str, Any]:
        """
        Capture text from the product variant section using OCR
        Focuses on the area near variant labels
        
        Args:
            variant_type: Type of variant ("color", "size", etc.)
        
        Returns:
            Dictionary with extracted text and analysis
        """
        try:
            # Take screenshot
            screenshot_path = f'temp_variant_validation_{variant_type}.png'
            await self.page.screenshot(path=screenshot_path)
            
            # Use vision system for OCR
            from ..vision import EnhancedVisionSystem
            vision = EnhancedVisionSystem(debug_mode=False)
            
            analysis = await vision.analyze_page(screenshot_path)
            
            # Extract all text elements
            text_elements = analysis.get('text_elements', [])
            
            # Find text near variant labels
            variant_keywords = {
                'color': ['color', 'colour', 'shade'],
                'size': ['size', 'fit', 'dimension'],
                'style': ['style', 'design'],
                'material': ['material', 'fabric']
            }
            
            keywords = variant_keywords.get(variant_type.lower(), [variant_type.lower()])
            
            # Find elements containing variant label
            relevant_texts = []
            for elem in text_elements:
                text = elem.get('text', '').lower()
                for keyword in keywords:
                    if keyword in text:
                        relevant_texts.append(elem.get('text', '').strip())
                        break
            
            # Also capture text in likely variant section (upper portion of page)
            upper_texts = [
                elem.get('text', '').strip() 
                for elem in text_elements 
                if elem.get('box', {}).get('y', 999) < 600  # Upper half
            ]
            
            # Clean up
            try:
                Path(screenshot_path).unlink()
            except:
                pass
            
            return {
                'all_text': ' '.join([e.get('text', '') for e in text_elements]),
                'relevant_texts': relevant_texts,
                'upper_section_texts': upper_texts,
                'text_elements': text_elements
            }
            
        except Exception as e:
            logger.error(f"Error capturing variant section text: {e}")
            return {
                'all_text': '',
                'relevant_texts': [],
                'upper_section_texts': [],
                'text_elements': []
            }
    
    async def validate_by_text_change(
        self, 
        variant_type: str, 
        expected_value: str,
        before_state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Validate selection by checking text change near variant label
        
        Args:
            variant_type: Type of variant ("color", "size", etc.)
            expected_value: Expected value (e.g., "Jaded", "8 Short")
            before_state: Optional previous state to compare against
        
        Returns:
            Validation result
        """
        logger.info(f"Validating {variant_type} by text change (looking for '{expected_value}')")
        
        # FAST PATH: Try DOM-based validation first (much faster than OCR)
        try:
            dom_result = await self._validate_by_dom(variant_type, expected_value)
            if dom_result['validated']:
                logger.info(f"✅ Fast DOM validation succeeded: {dom_result['selected_text']}")
                return dom_result
        except Exception as e:
            logger.debug(f"DOM validation failed, falling back to OCR: {e}")
        
        # SLOW PATH: OCR-based validation (fallback)
        # Capture current state
        current_state = await self.capture_variant_section_text(variant_type)
        
        expected_lower = expected_value.lower().strip()
        expected_normalized = expected_lower.replace(' ', '').replace('-', '')
        # FIXED: Allow numeric values and 2+ character values (including XXL, XL, etc.)
        expected_words = set(w for w in expected_lower.split() if len(w) >= 2 or w.isdigit())
        
        # Strategy 1: Check relevant texts (near variant labels)
        for relevant_text in current_state['relevant_texts']:
            text_lower = relevant_text.lower()
            text_normalized = text_lower.replace(' ', '').replace('-', '')
            
            # Exact match
            if expected_normalized in text_normalized:
                logger.info(f"Validated by text change: Found '{expected_value}' in '{relevant_text}'")
                return {
                    'validated': True,
                    'selected_text': relevant_text,
                    'confidence': 0.95,
                    'method': 'variant_label_text_change'
                }
            
            # Word overlap (for multi-word values)
            if expected_words:
                # FIXED: Allow numeric values and 2+ character values
                text_words = set(w for w in text_lower.split() if len(w) >= 2 or w.isdigit())
                overlap = expected_words & text_words
                
                if len(overlap) >= len(expected_words) * 0.7:  # 70% word match
                    logger.info(f"Validated by text change: Keyword match in '{relevant_text}'")
                    return {
                        'validated': True,
                        'selected_text': relevant_text,
                        'confidence': 0.85,
                        'method': 'variant_label_keyword_match'
                    }
        
        # Strategy 2: Check upper section texts (broader search)
        for upper_text in current_state['upper_section_texts']:
            text_lower = upper_text.lower()
            text_normalized = text_lower.replace(' ', '').replace('-', '')
            
            # Must be substantial match to avoid false positives
            if len(expected_normalized) > 3 and expected_normalized in text_normalized:
                # Additional check: must be near a colon (likely "Color: Jaded" format)
                if ':' in upper_text:
                    logger.info(f"Validated by text change: Found '{expected_value}' in upper section: '{upper_text}'")
                    return {
                        'validated': True,
                        'selected_text': upper_text,
                        'confidence': 0.80,
                        'method': 'upper_section_text_change'
                    }
        
        # Strategy 3: If we have before state, check for additions
        if before_state:
            before_texts = set(before_state.get('relevant_texts', []))
            current_texts = set(current_state['relevant_texts'])
            
            # New text appeared
            new_texts = current_texts - before_texts
            
            for new_text in new_texts:
                text_normalized = new_text.lower().replace(' ', '').replace('-', '')
                
                if expected_normalized in text_normalized:
                    logger.info(f"Validated by new text: '{new_text}' appeared after selection")
                    return {
                        'validated': True,
                        'selected_text': new_text,
                        'confidence': 0.90,
                        'method': 'new_text_after_selection'
                    }
        
        # Validation failed
        logger.warning(f"✗ Text change validation failed for '{expected_value}'")
        logger.debug(f"Relevant texts found: {current_state['relevant_texts'][:3]}")
        
        return {
            'validated': False,
            'selected_text': None,
            'confidence': 0.0,
            'method': 'text_change_validation_failed'
        }
    
    async def _validate_by_dom(self, variant_type: str, expected_value: str) -> Dict[str, Any]:
        """
        Fast DOM-based validation without OCR
        Checks for selected/active states and aria-labels
        """
        expected_lower = expected_value.lower().strip()
        expected_normalized = expected_lower.replace(' ', '').replace('-', '')
        
        js_code = """
        ({variantType, expectedValue}) => {
            const normalize = (text) => text ? text.toLowerCase().trim().replace(/[^a-z0-9]/g, '') : '';
            const expectedNorm = normalize(expectedValue);
            const expectedLower = expectedValue.toLowerCase().trim();
            
            const isExcludedElement = (elem) => {
                let parent = elem;
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
            };

            const valueMatches = (text) => {
                const textLower = text.toLowerCase().trim();
                if (!textLower) return false;
                
                // Exclude common unrelated texts
                if (/guide|chart|usual|finder|recommend|calculator|table|help/i.test(textLower)) {
                    return false;
                }
                
                const textNorm = normalize(textLower);
                if (textNorm === expectedNorm) return true;
                
                if (expectedNorm.length <= 3) {
                    const escaped = expectedLower.replace(/[-\\/\\\\^$*+?.()|[\]{}]/g, '\\$&');
                    const regex = new RegExp("(?:^|[^a-zA-Z0-9'])" + escaped + "(?:$|[^a-zA-Z0-9'])", "i");
                    return regex.test(textLower);
                }
                
                return textNorm.includes(expectedNorm) || expectedNorm.includes(textNorm);
            };
            
            // Find elements with selected/active states
            const selectors = [
                '[aria-selected="true"]',
                '[aria-checked="true"]',
                '[class*="selected"]',
                '[class*="active"]',
                'button[class*="swatch"][class*="selected"]',
                'button[class*="option"][class*="selected"]'
            ];
            
            for (const sel of selectors) {
                const elements = document.querySelectorAll(sel);
                for (const el of elements) {
                    if (isExcludedElement(el)) continue;
                    
                    // Get text from multiple sources
                    const texts = [
                        el.textContent?.trim() || '',
                        el.getAttribute('aria-label') || '',
                        el.getAttribute('title') || '',
                        el.getAttribute('data-value') || '',
                        el.getAttribute('data-color') || '',
                        el.getAttribute('data-colorname') || ''
                    ];
                    
                    for (const text of texts) {
                        if (valueMatches(text)) {
                            return {
                                found: true,
                                text: text,
                                selector: sel
                            };
                        }
                    }
                }
            }
            
            // Also check for variant label text (e.g., "Color: Lavender")
            const variantKeywords = ['color', 'colour', 'size', 'fit', 'style'];
            const bodyText = document.body.innerText;
            const lines = bodyText.split('\\n');
            
            for (let i = 0; i < lines.length; i++) {
                const line = lines[i];
                const lineLower = line.toLowerCase().trim();
                
                // Exclude lines containing guide, chart, usual, finder, etc.
                if (/guide|chart|usual|finder|recommend|calculator|table|help/i.test(lineLower)) {
                    continue;
                }
                
                const hasLabel = variantKeywords.some(keyword => {
                    const regex = new RegExp("\\b" + keyword + "\\b", "i");
                    return regex.test(lineLower);
                });
                
                if (hasLabel) {
                    const parts = line.split(':');
                    if (parts.length >= 2) {
                        const value = parts.slice(1).join(':').trim();
                        if (value && valueMatches(value)) {
                            return {
                                found: true,
                                text: value,
                                selector: 'text_pattern'
                            };
                        }
                    }
                    
                    // Check next line
                    if (i + 1 < lines.length) {
                        const nextLine = lines[i + 1].trim();
                        if (nextLine && valueMatches(nextLine)) {
                            return {
                                found: true,
                                text: nextLine,
                                selector: 'text_pattern_next_line'
                            };
                        }
                    }
                }
            }
            
            return { found: false };
        }
        """
        
        result = await self.page.evaluate(js_code, {
            'variantType': variant_type,
            'expectedValue': expected_value
        })
        
        if result.get('found'):
            return {
                'validated': True,
                'selected_text': result['text'],
                'confidence': 0.90,
                'method': f"dom_fast_validation_{result['selector']}"
            }
        
        return {
            'validated': False,
            'selected_text': None,
            'confidence': 0.0,
            'method': 'dom_validation_no_match'
        }
