#!/usr/bin/env python3
"""
BBOX Variant Detector
Detects all variants on page, assigns reference IDs, stores coordinates
Enables precise clicking and validation by reference number
"""

import logging
from typing import Dict, Any, Optional, List, Tuple
from playwright.async_api import Page
from pathlib import Path
import json
import asyncio

logger = logging.getLogger(__name__)


class BBoxVariantDetector:
    """
    Detects product variants using bounding box detection
    Assigns reference IDs for precise selection
    """
    
    def __init__(self, page: Page):
        self.page = page
        self.detected_variants = []  # List of detected variant BBoxes
        self.variant_map = {}  # ref_id -> variant_info
    
    async def detect_all_variants(self, variant_type: str = "all") -> List[Dict]:
        """
        Detect all variant elements and create BBox map
        
        Args:
            variant_type: "color", "size", or "all"
        
        Returns:
            List of detected variants with reference IDs
        """
        logger.info(f"🔍 Detecting all {variant_type} variants with BBOX...")
        
        # JavaScript to detect and create BBoxes
        js_code = r"""
        (variantType) => {
            const variants = [];
            let refId = 1;
            
            // FLEXIBLE: Cast a wider net for Amazon
            let selectors = [];
            
            if (variantType === 'color' || variantType === 'all') {
                selectors.push(
                    // Specific color selectors
                    'button[class*="color"]', 'div[class*="color"]',
                    'a[class*="color"]', 'span[class*="color"]',
                    // Generic swatch selectors  
                    'button[class*="swatch"]', 'div[class*="swatch"]',
                    'li[class*="swatch"]', 'span[class*="swatch"]',
                    // Amazon-specific patterns
                    'li[class*="imageThumbnail"]', 'li[class*="swatchselect"]',
                    'span[class*="variation"]', 'div[id*="variation"]',
                    // Generic clickable near "Color:"
                    'div[id*="color"] button', 'div[id*="color"] li',
                    'div[id*="color"] span[class*="selection"]'
                );
            }
            
            if (variantType === 'size' || variantType === 'all') {
                selectors.push(
                    'button[class*="size"]', 'div[class*="size"]',
                    'select[id*="size"]', 'select[name*="size"]',
                    'div[id*="size"] button', 'div[id*="size"] span'
                );
            }
            
            // Fallback: Just get ALL clickable elements in product area
            if (selectors.length === 0 || variantType === 'all') {
                selectors.push(
                    'button:not([class*="nav"]):not([class*="menu"])',
                    'div[role="button"]:not([class*="nav"])',
                    'li[role="radio"]', 'span[role="radio"]'
                );
            }
            
            const allElements = document.querySelectorAll(selectors.join(', '));
            
            // Less strict exclusion
            const excludeTexts = [
                'add to cart', 'buy now', 'checkout', 'search', 'menu',
                'deliver to', 'sign in', 'returns', 'gift', 'list'
            ];
            
            for (const elem of allElements) {
                // Skip navigation (still important)
                let current = elem;
                let isNav = false;
                for (let i = 0; i < 3 && current; i++) {
                    const tag = current.tagName;
                    const className = (current.className || '').toLowerCase();
                    if (['NAV', 'HEADER', 'FOOTER'].includes(tag) || 
                        className.includes('navbar') || className.includes('navigation')) {
                        isNav = true;
                        break;
                    }
                    current = current.parentElement;
                }
                if (isNav) continue;
                
                const rect = elem.getBoundingClientRect();
                
                // More lenient visibility check
                if (rect.width === 0 || rect.height === 0) continue;
                if (rect.top < 50) continue;  // Less strict top limit
                if (rect.top > window.innerHeight * 1.5) continue;
                
                const style = window.getComputedStyle(elem);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                
                // More lenient size check
                if (rect.width < 15 || rect.height < 15) continue;
                
                const text = elem.textContent?.trim() || elem.getAttribute('aria-label') || '';
                const textLower = text.toLowerCase();
                
                // Skip obvious non-variants
                if (excludeTexts.some(skip => textLower.includes(skip))) continue;
                
                // Skip if it's just a long paragraph
                if (text.length > 100) continue;
                
                // Determine type
                const className = (elem.className || '').toLowerCase();
                const id = (elem.id || '').toLowerCase();
                let detectedType = 'variant';  // Default to variant
                
                if (className.includes('color') || id.includes('color') || textLower.includes('color')) {
                    detectedType = 'color';
                } else if (className.includes('size') || id.includes('size') || 
                          /\b(xs|s|m|l|xl|xxl|\d+)\b/i.test(text)) {
                    detectedType = 'size';
                }
                
                variants.push({
                    ref_id: refId++,
                    bbox: {
                        x: Math.round(rect.left),
                        y: Math.round(rect.top),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height),
                        center_x: Math.round(rect.left + rect.width / 2),
                        center_y: Math.round(rect.top + rect.height / 2)
                    },
                    text: text,
                    value: text.toLowerCase().trim(),
                    type: detectedType,
                    element: {
                        tag: elem.tagName,
                        class: elem.className || '',
                        id: elem.id || '',
                        aria_label: elem.getAttribute('aria-label') || ''
                    },
                    properties: {
                        is_selected: elem.hasAttribute('aria-selected') && 
                                    elem.getAttribute('aria-selected') === 'true',
                        is_checked: elem.hasAttribute('aria-checked') && 
                                   elem.getAttribute('aria-checked') === 'true',
                        has_selected_class: className.includes('selected') || 
                                          className.includes('active')
                    }
                });
            }
            
            // Sort by position
            variants.sort((a, b) => {
                if (Math.abs(a.bbox.y - b.bbox.y) > 50) {
                    return a.bbox.y - b.bbox.y;
                }
                return a.bbox.x - b.bbox.x;
            });
            
            // Reassign IDs
            variants.forEach((v, idx) => v.ref_id = idx + 1);
            
            return variants;
        }
        """
        
        try:
            variants = await self.page.evaluate(js_code, variant_type)
            
            self.detected_variants = variants
            self.variant_map = {v['ref_id']: v for v in variants}
            
            logger.info(f"Detected {len(variants)} variants with BBoxes")
            
            # Log summary
            for v in variants[:10]:  # Show first 10
                logger.info(f"  [{v['ref_id']:02d}] {v['type']:8s} '{v['text'][:30]:30s}' at ({v['bbox']['center_x']}, {v['bbox']['center_y']})")
            
            if len(variants) > 10:
                logger.info(f"  ... and {len(variants) - 10} more")
            
            return variants
            
        except Exception as e:
            logger.error(f"Error detecting variants: {e}")
            return []
    
    async def click_by_ref_id(self, ref_id: int) -> Dict[str, Any]:
        """
        Click a variant by its reference ID
        
        Args:
            ref_id: Reference ID of the variant to click
        
        Returns:
            Result dictionary
        """
        if ref_id not in self.variant_map:
            return {
                'success': False,
                'error': f'Reference ID {ref_id} not found in variant map'
            }
        
        variant = self.variant_map[ref_id]
        
        logger.info(f"🖱️  Clicking variant [{ref_id:02d}]: '{variant['text']}'")
        logger.debug(f"   Type: {variant['type']}, Position: ({variant['bbox']['center_x']}, {variant['bbox']['center_y']})")
        
        try:
            # Click at center of bounding box
            await self.page.mouse.click(
                variant['bbox']['center_x'],
                variant['bbox']['center_y']
            )
            
            await asyncio.sleep(1)  # Wait for page update
            
            return {
                'success': True,
                'ref_id': ref_id,
                'clicked_variant': variant
            }
            
        except Exception as e:
            logger.error(f"Error clicking variant [{ref_id}]: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    async def find_matching_ref_id(self, target_value: str, variant_type: Optional[str] = None) -> Optional[int]:
        """
        Find the reference ID of a variant that matches the target value
        
        Args:
            target_value: Value to match (e.g., "Jaded", "8 Short")
            variant_type: Optional type filter ("color", "size")
        
        Returns:
            Reference ID if found, None otherwise
        """
        target_lower = target_value.lower().strip()
        target_normalized = target_lower.replace(' ', '').replace('-', '')
        target_words = set(w for w in target_lower.split() if len(w) > 2)
        
        best_match = None
        best_score = 0
        
        for variant in self.detected_variants:
            # Filter by type if specified
            if variant_type and variant['type'] != variant_type and variant['type'] != 'variant':
                continue
            
            text_lower = variant['value']
            text_normalized = text_lower.replace(' ', '').replace('-', '')
            
            # Exact match
            if target_normalized == text_normalized:
                return variant['ref_id']
            
            # Contains match
            if target_normalized in text_normalized or text_normalized in target_normalized:
                if len(text_normalized) > 2:
                    score = 0.9
                    if score > best_score:
                        best_match = variant['ref_id']
                        best_score = score
            
            # Word overlap
            if target_words:
                text_words = set(w for w in text_lower.split() if len(w) > 2)
                if text_words:
                    overlap = target_words & text_words
                    if overlap:
                        score = len(overlap) / len(target_words)
                        if score > best_score and score >= 0.6:
                            best_match = variant['ref_id']
                            best_score = score
        
        if best_match:
            variant = self.variant_map[best_match]
            logger.info(f"Matched '{target_value}' to [{best_match:02d}] '{variant['text']}' (score: {best_score:.2f})")
        else:
            logger.warning(f"✗ No match found for '{target_value}'")
        
        return best_match
    
    def get_variant_info(self, ref_id: int) -> Optional[Dict]:
        """Get variant information by reference ID"""
        return self.variant_map.get(ref_id)
    
    def export_bbox_map(self, filepath: str = "variant_bbox_map.json"):
        """Export the BBOX map to JSON for debugging"""
        try:
            with open(filepath, 'w') as f:
                json.dump({
                    'total_variants': len(self.detected_variants),
                    'variants': self.detected_variants
                }, f, indent=2)
            logger.info(f"📄 Exported BBOX map to {filepath}")
        except Exception as e:
            logger.error(f"Error exporting BBOX map: {e}")
