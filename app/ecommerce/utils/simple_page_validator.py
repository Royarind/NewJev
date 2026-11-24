#!/usr/bin/env python3
"""
Simple Page Label Validator
Looks for "Color: Jaded" or "Size: 8 Short" pattern on page
"""

import logging
from typing import Dict, Any
from playwright.async_api import Page

logger = logging.getLogger(__name__)


class SimplePageValidator:
    """
    Simple validator that looks for label patterns like:
    - "Color: Jaded"
    - "Size: 8 Short"
    """
    
    def __init__(self, page: Page):
        self.page = page
    
    async def validate(self, variant_type: str, expected_value: str) -> Dict[str, Any]:
        """
        Validate by finding "Type: Value" pattern on page
        
        Args:
            variant_type: "color" or "size"
            expected_value: "Jaded" or "8 Short"
        
        Returns:
            Dict with validated, selected_text, confidence, method
        """
        logger.info(f"🔍 VALIDATING: Looking for '{variant_type}: {expected_value}' on page")
        
        # Normalize expected value
        expected_lower = expected_value.lower().strip()
        expected_normalized = expected_lower.replace(' ', '').replace('-', '').replace('_', '')
        
        js_code = r"""
        ({variantType, expectedValue}) => {
            // Look for patterns like "Color: Jaded" or "Size: 8 Short"
            const labels = [variantType, variantType + ':', variantType.toLowerCase(), 
                           variantType.toLowerCase() + ':', 'selected ' + variantType.toLowerCase(),
                           'selected ' + variantType.toLowerCase() + ':'];
            
            if (variantType.toLowerCase() === 'color' || variantType.toLowerCase() === 'colour') {
                labels.push('color', 'color:', 'selected color', 'selected color:',
                            'colour', 'colour:', 'selected colour', 'selected colour:');
            }
            
            // Get all text content
            const bodyText = document.body.innerText;
            const lines = bodyText.split('\\n');
            
            const expectedLower = expectedValue.toLowerCase();
            const expectedNorm = expectedLower.replace(/[\\s_-]/g, '');
            const isNumeric = /^\\d+$/.test(expectedNorm);

            // Helper: does a value string match the expected?
            const valueMatches = (value) => {
                const valueLower = value.toLowerCase().trim();
                if (!valueLower) return false;
                if (isNumeric) return value.replace(/[^0-9]/g, '') === expectedNorm;
                
                // For very short strings (e.g. S, M, L, XL, 8), require exact word matching (excluding possessive apostrophes)
                if (expectedNorm.length <= 3) {
                    const escaped = expectedLower.replace(/[-\\/\\\\^$*+?.()|[\]{}]/g, '\\$&');
                    const regex = new RegExp("(?:^|[^a-zA-Z0-9'])" + escaped + "(?:$|[^a-zA-Z0-9'])", "i");
                    return regex.test(valueLower);
                }
                
                const valueNorm = valueLower.replace(/[\\s_-]/g, '');
                // Only match if the found text CONTAINS the expected value (not the reverse).
                // Reverse containment ("13.5".includes("13") = true) causes false positives.
                // Also require found text is at most 3x longer than expected (filters aria-labels
                // like "Natural Black (Blizzard Sole) swatch" wrongly matching color "Blizzard").
                const lengthRatio = expectedNorm.length / (valueNorm.length || 1);
                return valueNorm.includes(expectedNorm) && lengthRatio > 0.3;
            };
            
            // Strategy 0: Find explicit "Type: Value" label pattern (colon required).
            // We do NOT match lines that merely contain both the type word and the value
            // word — that fires on product titles and descriptions before any selection.
            for (let i = 0; i < lines.length; i++) {
                const line = lines[i];
                const lineLower = line.toLowerCase().trim();

                if (/guide|chart|usual|finder|recommend|calculator|table|help/i.test(lineLower)) {
                    continue;
                }
                if (lineLower.length >= 80) continue;

                const typeLower = variantType.toLowerCase();
                const expectedLower = expectedValue.toLowerCase();
                const expectedNorm = expectedLower.replace(/[\s_-]/g, '');

                // STRICT: require "Type: Value" with explicit colon separator
                const colonIdx = lineLower.indexOf(':');
                if (colonIdx !== -1) {
                    const beforeColon = lineLower.substring(0, colonIdx).trim();
                    const afterColon = lineLower.substring(colonIdx + 1).trim();
                    if (beforeColon.includes(typeLower) &&
                        (afterColon.includes(expectedLower) || afterColon.replace(/[\s_-]/g, '').includes(expectedNorm))) {
                        return { found: true, pattern: line.trim(), extracted_value: expectedValue, method: 'inline_mashed' };
                    }
                }
            }

            // Strategy 1: Find "Label: Value" pattern (same line OR next line)
            for (let i = 0; i < lines.length; i++) {
                const line = lines[i];
                const lineLower = line.toLowerCase().trim();
                
                // Exclude lines containing guide, chart, usual, finder, etc. to prevent false positive validation
                if (/guide|chart|usual|finder|recommend|calculator|table|help/i.test(lineLower)) {
                    continue;
                }
                
                const hasLabel = labels.some(label => {
                    const cleanLabel = label.replace(/:$/, "").trim();
                    const regex = new RegExp("\\b" + cleanLabel.replace(/[-\\/\\\\^$*+?.()|[\]{}]/g, '\\$&') + "\\b", "i");
                    return regex.test(lineLower) && (label.endsWith(':') ? lineLower.includes(label.toLowerCase()) : true);
                });
                
                if (hasLabel) {
                    // Check value on same line (after colon)
                    const parts = line.split(':');
                    if (parts.length >= 2) {
                        const value = parts.slice(1).join(':').trim();
                        if (value && valueMatches(value)) {
                            return { found: true, pattern: line.trim(), extracted_value: value, method: 'label_pattern' };
                        }
                    }
                    
                    // Also check if expected value appears after the colon as a distinct word.
                    // Require colon so we don't match product descriptions that happen to
                    // contain both the variant-type word and the target value.
                    if (line.includes(':')) {
                        const afterColon = line.substring(line.indexOf(':') + 1);
                        const escaped = expectedLower.replace(/[-\\/\\\\^$*+?.()|[\]{}]/g, '\\$&');
                        const regex = new RegExp("(?:^|[^a-zA-Z0-9'])" + escaped + "(?:$|[^a-zA-Z0-9'])", "i");
                        if (regex.test(afterColon.toLowerCase())) {
                            return { found: true, pattern: line.trim(), extracted_value: expectedValue, method: 'label_inline' };
                        }
                    }

                    // === NEW: Check value on NEXT line (Flipkart Selected Color and NAVY on next line) ===
                    if (i + 1 < lines.length) {
                        const nextLine = lines[i + 1].trim();
                        if (nextLine && valueMatches(nextLine)) {
                            return {
                                found: true,
                                pattern: line.trim() + ' ' + nextLine,
                                extracted_value: nextLine,
                                method: 'label_next_line'
                            };
                        }
                    }
                }
            }

            // === NEW Strategy 1b: URL slug validation (Flipkart navigates to a new product URL per colour) ===
            {
                const href = window.location.href;
                const match = href.match(/\/([^\/]+)\/p\//i);
                if (match) {
                    const slug = match[1].toLowerCase();
                    const slugNorm = slug.replace(/[-_]/g, '');
                    // STRICT: Only match if slug contains expected value
                    if (slugNorm.includes(expectedNorm)) {
                        return {
                            found: true,
                            pattern: 'URL: ' + href,
                            extracted_value: expectedValue,
                            method: 'url_slug'
                        };
                    }
                }
            }
            
            // Strategy 2: Find selected element with border/highlight (VISUAL DETECTION)
            const selectedElements = document.querySelectorAll(`
                [aria-selected="true"],
                [aria-checked="true"],
                .selected,
                [class*="selected"],
                [class*="active"],
                button, div[role="button"], li
            `);
            
            for (const elem of selectedElements) {
                // Only skip buttons whose aria-label literally starts with "Quick Add" —
                // these are carousel product cards, not variant swatches.
                // We intentionally do NOT walk ancestors here: BR's size selector itself
                // uses a "carousel" scroll container, so ancestor checks would wrongly
                // exclude the real XXL/Tall/etc. swatch buttons.
                const ariaLabelRaw = elem.getAttribute('aria-label') || '';
                if (/^quick[\\s-]?add/i.test(ariaLabelRaw.trim())) continue;

                const text = elem.textContent?.trim() || ariaLabelRaw || '';
                const textLower = text.toLowerCase().trim();
                if (!textLower) continue;

                // CRITICAL: Variant swatches have short labels. Anything > 300 chars is a
                // modal, popup, or descriptive container — skip it unconditionally.
                if (text.length > 300) continue;

                // Walk ancestors — skip elements inside modals/popups/dialogs/header/footer/nav
                let isExcluded = false;
                let parent = elem;
                while (parent && parent !== document.body) {
                    const tag = parent.tagName;
                    const cls = (parent.className || '').toString();
                    const role = (parent.getAttribute('role') || '').toLowerCase();
                    if (tag === 'HEADER' || tag === 'FOOTER' || tag === 'NAV' ||
                        role === 'dialog' || role === 'alertdialog' ||
                        /header|footer|nav|menu|review|recommend|carousel|modal|popup|dialog|overlay|lightbox|bx-campaign|bxc\b/i.test(cls)) {
                        isExcluded = true;
                        break;
                    }
                    parent = parent.parentElement;
                }
                if (isExcluded) continue;

                // Exclude links/labels like size guide, size chart, usual size
                if (/guide|chart|usual|finder|recommend|calculator|table|help/i.test(textLower)) {
                    continue;
                }
                
                let matches = false;
                if (expectedNorm.length <= 3) {
                    const escaped = expectedLower.replace(/[-\\/\\\\^$*+?.()|[\]{}]/g, '\\$&');
                    const regex = new RegExp("(?:^|[^a-zA-Z0-9'])" + escaped + "(?:$|[^a-zA-Z0-9'])", "i");
                    matches = regex.test(textLower);
                } else {
                    const textNorm = textLower.replace(/[\\s_-]/g, '');
                    // Same guard as valueMatches: no reverse containment, require 30% length ratio.
                    // This prevents long aria-labels (e.g. "Natural Black Blizzard Sole swatch")
                    // from falsely validating a short color target ("Blizzard").
                    const lengthRatio = expectedNorm.length / (textNorm.length || 1);
                    matches = textNorm.includes(expectedNorm) && lengthRatio > 0.3;
                }
                
                // Check if text matches (and is not empty)
                if (matches) {
                    // Check if element has visual selection indicators
                    const style = window.getComputedStyle(elem);
                    const borderWidth = parseFloat(style.borderWidth) || 0;
                    const borderColor = style.borderColor;
                    const boxShadow = style.boxShadow;
                    const outline = style.outline;
                    
                    // Consider it selected if:
                    // 1. Has thick border (>2px)
                    // 2. Has box-shadow
                    // 3. Has outline
                    // 4. Has aria/class indicators
                    const hasVisualIndicator = 
                        borderWidth > 2 ||
                        boxShadow !== 'none' ||
                        outline !== 'none' ||
                        elem.hasAttribute('aria-selected') ||
                        elem.hasAttribute('aria-checked') ||
                        elem.className.includes('selected') ||
                        elem.className.includes('active');
                    
                    if (hasVisualIndicator) {
                        return {
                            found: true,
                            pattern: `Selected with border: ${text}`,
                            extracted_value: text,
                            method: 'border_detection',
                            visual_indicator: {
                                borderWidth: borderWidth,
                                hasBorder: borderWidth > 2,
                                hasBoxShadow: boxShadow !== 'none',
                                hasOutline: outline !== 'none'
                            }
                        };
                    }
                }
                
                // Check child images — but ONLY swatch-sized images (< 120px).
                // The main product photo also has the colour in its alt text and
                // often has a border/shadow from the carousel frame, causing a
                // false-positive that says the variant is already selected.
                const img = elem.querySelector('img');
                if (img) {
                    const imgRect = img.getBoundingClientRect();
                    const isSwatchSize = imgRect.width < 120 && imgRect.height < 120;
                    if (isSwatchSize) {
                        const alt = img.getAttribute('alt') || '';
                        const altNorm = alt.toLowerCase().replace(/[\\s_-]/g, '');
                        if (altNorm.includes(expectedNorm) || expectedNorm.includes(altNorm)) {
                            const style = window.getComputedStyle(elem);
                            const borderWidth = parseFloat(style.borderWidth) || 0;
                            if (borderWidth > 2 || style.boxShadow !== 'none') {
                                return {
                                    found: true,
                                    pattern: `Selected image with border: ${alt}`,
                                    extracted_value: alt,
                                    method: 'border_image_detection'
                                };
                            }
                        }
                    }
                }
            }
            
            return {found: false};
        }
        """
        
        try:
            result = await self.page.evaluate(js_code, {
                'variantType': variant_type.capitalize(),
                'expectedValue': expected_value
            })
            
            if result['found']:
                logger.info(f"✅ VALIDATED: Found '{result['pattern']}'")
                logger.info(f"   Method: {result['method']}")
                
                # Log visual indicators if detected
                if 'visual_indicator' in result:
                    vi = result['visual_indicator']
                    indicators = []
                    if vi.get('hasBorder'):
                        indicators.append(f"border={vi['borderWidth']}px")
                    if vi.get('hasBoxShadow'):
                        indicators.append("box-shadow")
                    if vi.get('hasOutline'):
                        indicators.append("outline")
                    if indicators:
                        logger.info(f"   Visual indicators: {', '.join(indicators)}")
                
                return {
                    'validated': True,
                    'selected_text': result['extracted_value'],
                    'confidence': 0.95,
                    'method': f"simple_page_check_{result['method']}"
                }
            else:
                logger.warning(f"\u274c NOT VALIDATED: '{variant_type}: {expected_value}' not found on page")
                return {
                    'validated': False,
                    'selected_text': None,
                    'confidence': 0.0,
                    'method': 'none'
                }

        except Exception as e:
            logger.error(f"Validation error: {e}")
            return {
                'validated': False,
                'selected_text': None,
                'confidence': 0.0,
                'method': 'error'
            }
