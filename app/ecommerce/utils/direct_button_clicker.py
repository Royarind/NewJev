#!/usr/bin/env python3
"""
Direct Button Clicker V2
Finds buttons by exact text and uses label proximity to pick the right one
"""

import logging
from typing import Dict, Any
from playwright.async_api import Page
import asyncio

logger = logging.getLogger(__name__)


class DirectButtonClicker:
    """
    Finds and clicks buttons by exact text match
    Uses label proximity to distinguish between same values in different sections
    """
    
    def __init__(self, page: Page):
        self.page = page
    
    async def find_and_click_by_text(self, target_text: str, variant_type: str = None) -> Dict[str, Any]:
        """
        Find button with exact text in the correct variant section
        
        Strategy:
        1. Find the label for the variant type (e.g., "Length:")
        2. Find ALL buttons matching the target text
        3. Pick the one closest to (and below) the label
        """
        logger.info(f"🔍 DirectButtonClicker: Finding '{target_text}' for '{variant_type}'")
        
        js_code = """
        ({targetText, variantType}) => {
            const target = targetText.trim();
            const targetLower = target.toLowerCase();
            const isNumeric = /^\\d+$/.test(target);
            const variantLower = (variantType || '').toLowerCase();
            
            // Step 1: Find the label for this variant type
            let labelY = 0;
            if (variantType) {
                const labels = document.body.querySelectorAll('span, div, label, p, h3, h4');
                for (const label of labels) {
                    const text = label.textContent?.trim().toLowerCase() || '';
                    
                    // Look for "Waist:" or "Waist " or just "Waist" at start
                    if (text.startsWith(variantLower + ':') || 
                        text.startsWith(variantLower + ' ') ||
                        text === variantLower) {
                        
                        const rect = label.getBoundingClientRect();
                        if (rect.height > 0 && rect.width > 0) {
                            labelY = rect.top;
                            break;
                        }
                    }
                }
            }
            
            // Step 2: Find ALL matching buttons (including radio inputs with aria-label)
            const candidates = [];
            const elements = document.querySelectorAll(
                'button, div[role="button"], li, a, span[role="button"], label, input[type="radio"]'
            );
            
            for (const elem of elements) {
                const text = elem.textContent?.trim() || '';
                const textLower = text.toLowerCase();
                
                // Check for match in text OR aria-label
                const ariaLabel = elem.getAttribute('aria-label') || '';
                const ariaLower = ariaLabel.toLowerCase();
                
                let matched = false;
                if (isNumeric || target.length < 3) {
                    // For numbers and short tokens, exact match
                    matched = textLower === targetLower || ariaLower.includes(': ' + targetLower);
                } else {
                    // For text, allow partial match
                    matched = textLower === targetLower || 
                             textLower.includes(targetLower) ||
                             ariaLower.includes(targetLower);
                }
                
                if (matched) {
                    const rect = elem.getBoundingClientRect();
                    
                    // Must be visible
                    if (rect.width < 10 || rect.height < 10) continue;
                    
                    const style = window.getComputedStyle(elem);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    
                    candidates.push({
                        text: text,
                        x: Math.round(rect.left + rect.width / 2),
                        y: Math.round(rect.top + rect.height / 2),
                        top: rect.top,
                        tag: elem.tagName,
                        id: elem.id || ''
                    });
                }
            }
            
            if (candidates.length === 0) {
                return {found: false, reason: 'no_candidates'};
            }
            
            // Step 3: Pick the best candidate
            let best = null;
            
            if (labelY > 0 && candidates.length > 1) {
                // Pick the one closest to (and below) the label
                let minDistance = Infinity;
                for (const c of candidates) {
                    // Must be below the label
                    if (c.top >= labelY - 10) {
                        const distance = c.top - labelY;
                        if (distance < minDistance) {
                            minDistance = distance;
                            best = c;
                        }
                    }
                }
            }
            
            // Fallback: just pick the first one
            if (!best) {
                best = candidates[0];
            }
            
            return {
                found: true,
                text: best.text,
                x: best.x,
                y: best.y,
                top: best.top,
                id: best.id,
                section: variantType || 'any',
                totalCandidates: candidates.length,
                labelY: labelY
            };
        }
        """
        
        try:
            result = await self.page.evaluate(js_code, {'targetText': target_text, 'variantType': variant_type})
            
            if result['found']:
                logger.info(f"Found '{result['text']}' for '{result['section']}' at ({result['x']}, {result['y']})")
                logger.info(f"   (picked from {result['totalCandidates']} candidates, labelY={result['labelY']})")
                
                # Click it
                handle = await self.page.query_selector(f"#{result['id']}") if result.get('id') else None
                if not handle:
                    # Fallback to coordinate click but scroll first
                    # scrollY + rect.top gives the absolute Y position on the page
                    await self.page.evaluate(f"window.scrollTo({{top: window.scrollY + {result['top']} - 200, behavior: 'auto'}})")
                    await asyncio.sleep(0.3)
                    
                    # Coordinate refresh: Since we scrolled, the new relative Y is (old_abs_y - new_scroll_y)
                    # For simplicity, we targeted a scroll that puts it at Y=200 relative to viewport
                    result['y'] = 200 + (result['y'] - result['top']) 
                    # result['top'] was rect.top, and result['y'] was rect.top + rect.height/2
                    # so (result['y'] - result['top']) is height/2
                else:
                    # Use playwright's built-in scroll which handles everything
                    await handle.scroll_into_view_if_needed()
                    await asyncio.sleep(0.2)
                    # Re-get coordinates after scroll
                    box = await handle.bounding_box()
                    if box:
                        result['x'] = box['x'] + box['width'] / 2
                        result['y'] = box['y'] + box['height'] / 2
                
                # Re-verify position or just click
                await self.page.mouse.click(result['x'], result['y'])
                await asyncio.sleep(1)
                
                return {
                    'success': True,
                    'clicked_text': result['text'],
                    'coordinates': {'x': result['x'], 'y': result['y']},
                    'method': 'direct_button_click',
                    'section': result['section']
                }
            else:
                logger.warning(f"✗ Button '{target_text}' not found for '{variant_type}'")
                return {'success': False, 'error': f"Button '{target_text}' not found"}
                
        except Exception as e:
            logger.error(f"Error finding button: {e}")
            return {'success': False, 'error': str(e)}
