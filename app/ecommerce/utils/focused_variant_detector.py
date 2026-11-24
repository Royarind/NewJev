#!/usr/bin/env python3
"""
Focused Variant Detector
Detects variant section first, then only searches within that area
This eliminates random clicks on navigation and other non-variant elements
"""

import logging
from typing import Dict, Any, Optional, List
from playwright.async_api import Page
from pathlib import Path
import asyncio
import cv2
import numpy as np
from pathlib import Path

logger = logging.getLogger(__name__)


class FocusedVariantDetector:
    """
    Focused variant detection using section-based approach
    1. Detect variant section bbox
    2. Only search for elements inside that bbox
    3. Crop screenshot to section for better OCR
    4. Validate within section only
    """
    
    def __init__(self, page: Page):
        self.page = page
        self.variant_section_bbox = None
        self.detected_variants = []
        self.detected_variants = []
        try:
            self.root_dir = Path(__file__).resolve().parents[4]
            self.js_assets_dir = self.Path(__file__).resolve().parents[1] / 'dom_assets'
        except Exception:
            self.js_assets_dir = Path(__file__).resolve().parents[1] / 'dom_assets'

    async def _visualize_click(self, x: float, y: float):
        """Show a blue circle at the click coordinates."""
        try:
            path = self.js_assets_dir / 'visualize_click.js'
            if not path.exists():
                logger.warning(f"JS Visualizer not found at {path}")
                return
                
            js_visualize = path.read_text(encoding='utf-8')
            logger.info(f"Visualizing click at ({x}, {y}) using {path}")
            await self.page.evaluate(js_visualize, {'x': x, 'y': y})
        except Exception as e:
            logger.warning(f"Click visualization failed: {e}")
    
    async def detect_variant_section(self) -> Optional[Dict]:
        """
        Detect the variant section bbox using DOM analysis
        
        Returns:
            Dictionary with bbox coordinates or None
        """
        logger.info("🔍 Detecting variant section bbox...")
        
        js_code = """
        () => {
            // STEP 1: Find main product detail container first
            const mainProductContainer = (() => {
                // Look for common product detail container patterns
                const selectors = [
                    '[class*="product-detail"]',
                    '[class*="product-info"]',
                    '[id*="product-detail"]',
                    '[id*="product-info"]',
                    'main [class*="product"]',
                    '[role="main"] [class*="product"]'
                ];
                
                for (const sel of selectors) {
                    const elem = document.querySelector(sel);
                    if (elem) return elem;
                }
                
                // Fallback: find the largest container with product-related content
                const candidates = document.querySelectorAll('div, section, main');
                let best = null;
                let maxScore = 0;
                
                for (const elem of candidates) {
                    const text = elem.textContent?.toLowerCase() || '';
                    let score = 0;
                    
                    // Must have variant-related keywords
                    if (text.includes('color') || text.includes('size')) score += 2;
                    if (text.includes('add to cart') || text.includes('add to bag')) score += 2;
                    
                    // Must have multiple interactive elements
                    const buttons = elem.querySelectorAll('button, a[role="button"]');
                    if (buttons.length >= 3) score += 1;
                    
                    // Penalize if too large (likely full page)
                    const rect = elem.getBoundingClientRect();
                    if (rect.width > window.innerWidth * 0.95) score -= 2;
                    if (rect.height > window.innerHeight * 2) score -= 2;
                    
                    // Penalize if in header/footer
                    if (rect.top < 100) score -= 3;
                    if (rect.top > window.innerHeight * 1.5) score -= 1;
                    
                    if (score > maxScore) {
                        maxScore = score;
                        best = elem;
                    }
                }
                
                return best;
            })();
            
            if (!mainProductContainer) {
                return null;
            }
            
            // STEP 2: Find variant section WITHIN main product container
            const strategies = [
                // Strategy 0: High-confidence Amazon/Target IDs
                {
                    name: 'platform_specific',
                    selectors: [
                        '#inline-twister-row-color_name', '#inline-twister-row-size_name',
                        '#twister_variation_section_color_name', '#twister_variation_section_size_name',
                        '#variation_color_name', '#variation_size_name', '#twister',
                        '[id*="select-"][id*="qty"]'
                    ]
                },
                
                // Strategy 1: site-specific broad selectors
                {
                    name: 'site_specific',
                    selectors: [
                        '[id*="variation"]', '[id*="twister"]', '[data-feature-name*="variation"]'
                    ]
                },
                
                // Strategy 2: Generic class patterns
                {
                    name: 'generic_classes',
                    selectors: [
                        '[class*="variation"]', '[class*="variant"]',
                        '[class*="product-options"]', '[class*="swatch"]',
                        '[class*="color-select"]', '[class*="size-select"]'
                    ]
                },
                
                // Strategy 3: Text-based detection
                {
                    name: 'text_based',
                    find: () => {
                        const keywords = ['color:', 'colour:', 'size:', 'select color', 'select size'];
                        const allElements = mainProductContainer.querySelectorAll('div, section, form, label');
                        for (const elem of allElements) {
                            const text = elem.textContent.toLowerCase();
                            if (keywords.some(kw => text.includes(kw))) {
                                if (elem.querySelectorAll('button, a, li, label, img').length > 1) {
                                    return elem;
                                }
                            }
                        }
                        return null;
                    }
                }
            ];
            
            // Try each strategy
            for (const strategy of strategies) {
                let elem = null;
                let foundSelector = '';
                
                if (strategy.selectors) {
                    for (const sel of strategy.selectors) {
                        const candidate = mainProductContainer.querySelector(sel);
                        if (candidate && candidate.offsetParent !== null) {
                            elem = candidate;
                            foundSelector = sel;
                            break;
                        }
                    }
                } else if (strategy.find) {
                    elem = strategy.find();
                    foundSelector = 'text-based';
                }
                
                if (elem) {
                    let container = elem;
                    // Expand to parent container (capped at 5 levels) but stay within mainProductContainer
                    for (let i = 0; i < 5; i++) {
                        const parent = container.parentElement;
                        if (!parent || parent.tagName === 'BODY' || parent === mainProductContainer) break;
                        const rect = parent.getBoundingClientRect();
                        if (rect.width > window.innerWidth * 0.95 || rect.height > window.innerHeight * 0.9) break;
                        container = parent;
                    }
                    
                    // Scroll to center
                    container.scrollIntoView({ behavior: 'auto', block: 'center' });
                    
                    // Measure
                    const r = container.getBoundingClientRect();
                    return {
                        x: Math.round(r.left),
                        y: Math.round(r.top),
                        width: Math.round(r.width),
                        height: Math.round(r.height),
                        x2: Math.round(r.right),
                        y2: Math.round(r.bottom),
                        selector: foundSelector,
                        strategy: strategy.name
                    };
                }
            }
            return null;
        }
        """
        
        try:
            bbox = await self.page.evaluate(js_code)
            
            if bbox:
                self.variant_section_bbox = bbox
                logger.info(f"✅ Found and SCROLLED variant section via '{bbox['strategy']}' Strategy")
                logger.info(f"  BBox: ({bbox['x']}, {bbox['y']}) size {bbox['width']}x{bbox['height']}")
                return bbox
            else:
                logger.warning("✗ Could not detect variant section")
                return None
                
        except Exception as e:
            logger.error(f"Error detecting variant section: {e}")
            return None
    
    async def detect_variants_in_section(self, variant_type: str = "all") -> List[Dict]:
        """
        Detect variants ONLY within the variant section bbox
        
        Args:
            variant_type: "color", "size", or "all"
        
        Returns:
            List of detected variants with reference IDs
        """
        if not self.variant_section_bbox:
            logger.warning("No variant section detected, detecting now...")
            await self.detect_variant_section()
        
        if not self.variant_section_bbox:
            logger.error("Cannot detect variants without variant section")
            return []
        
        bbox = self.variant_section_bbox
        logger.info(f"🔍 Detecting {variant_type} variants within section bbox...")
        
        js_code = """
        ({bbox, variantType}) => {
            const variants = [];
            let refId = 1;

            // ── Helper: extract colour/size label from an anchor href slug ──────────
            // e.g. ".../integriti-polo-lavender-t-shirt/p/.." → "lavender t shirt"
            const extractLabelFromHref = (href) => {
                if (!href) return '';
                try {
                    const match = href.match(/\\/([^\\/]+)\\/p\\//i);
                    const slug = match ? match[1] : href.split('/').filter(Boolean).slice(-2, -1)[0] || '';
                    const parts = slug.split('-').filter(p => p.length > 1 && !/^\\d+$/.test(p));
                    const seen = new Set();
                    return parts.filter(p => { const k = p.toLowerCase(); if (seen.has(k)) return false; seen.add(k); return true; }).join(' ');
                } catch (e) { return ''; }
            };

            // Get ALL potentially clickable elements ONLY within the variant section bbox
            const allClickables = [];
            
            // First, find all elements in the entire page
            const allElements = document.querySelectorAll(
                'button, a, span, div, li, img, label, input'
            );
            
            // Filter to only those INSIDE the variant section bbox
            for (const elem of allElements) {
                const rect = elem.getBoundingClientRect();
                
                // CRITICAL: Must be inside section bbox
                if (rect.left < bbox.x || rect.right > bbox.x2 ||
                    rect.top < bbox.y || rect.bottom > bbox.y2) {
                    continue;
                }
                
                // Must be visible
                if (rect.width === 0 || rect.height === 0) continue;
                if (rect.width < 8 || rect.height < 8) continue;
                
                const style = window.getComputedStyle(elem);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                
                // CRITICAL: Skip product images and thumbnails
                const className = (typeof elem.className === 'string' ? elem.className : (elem.className?.baseVal || '')).toLowerCase();
                const id = (elem.id || '').toLowerCase();
                const parentClass = (typeof elem.parentElement?.className === 'string' ? elem.parentElement.className : (elem.parentElement?.className?.baseVal || '')).toLowerCase();
                
                // Skip if it's clearly a product image
                if (className.includes('product-image') || 
                    className.includes('thumbnail') ||
                    className.includes('gallery') ||
                    id.includes('product-image') ||
                    id.includes('thumbnail') ||
                    parentClass.includes('product-image') ||
                    parentClass.includes('thumbnail') ||
                    parentClass.includes('gallery')) {
                    continue;
                }
                
                // Skip if it's an image with product-related alt text
                if (elem.tagName === 'IMG') {
                    const alt = (elem.getAttribute('alt') || '').toLowerCase();
                    const src = (elem.getAttribute('src') || '').toLowerCase();
                    
                    // Skip main product images (usually large)
                    if (rect.width > 200 || rect.height > 200) continue;
                    
                    // Skip if alt/src suggests it's a product photo, not a swatch
                    if (alt.includes('product') || alt.includes('view') || 
                        src.includes('product') || src.includes('/large/') || src.includes('/zoom/')) {
                        continue;
                    }
                }
                
                allClickables.push(elem);
            }
            
            // Process filtered clickables
            for (const elem of allClickables) {
                const rect = elem.getBoundingClientRect();
                
                const className = (typeof elem.className === 'string' ? elem.className : (elem.className?.baseVal || '')).toLowerCase();
                const id = (elem.id || '').toLowerCase();
                
                // Build text from multiple attribute sources
                const childImg = (elem.tagName !== 'IMG') ? elem.querySelector('img') : null;
                let text = [
                    elem.getAttribute('aria-label') || '',
                    elem.getAttribute('title') || '',
                    elem.getAttribute('data-color') || '',
                    elem.getAttribute('data-colorname') || '',
                    elem.getAttribute('data-value') || '',
                    elem.getAttribute('alt') || '',
                    childImg ? (childImg.getAttribute('aria-label') || childImg.getAttribute('title') || childImg.getAttribute('data-color') || childImg.getAttribute('alt') || '') : '',
                    (elem.textContent || '').trim(),
                ].find(t => t.trim().length > 0 && t.trim().toLowerCase() !== 'image') || '';

                // === Flipkart/Myntra style: anchor with swatch image, color in href ===
                if (!text || text.toLowerCase() === 'image') {
                    const anchor = (elem.tagName === 'A') ? elem : elem.closest('a');
                    if (anchor && anchor.href) {
                        text = extractLabelFromHref(anchor.href);
                    }
                }

                if (!text) continue;
                
                // Skip very long text (likely descriptions, not variant options)
                if (text.length > 100) continue;
                
                // CRITICAL: Skip prices and currency
                const textUpper = text.toUpperCase();
                if (textUpper.includes('INR') || textUpper.includes('RS') || 
                    textUpper.includes('$') || textUpper.includes('€') || 
                    textUpper.includes('£') || textUpper.includes('¥') ||
                    textUpper.includes('PRICE') || textUpper.includes('COST')) {
                    continue;
                }
                
                // Skip common non-variant text
                const skipWords = ['add', 'cart', 'buy', 'checkout', 'details', 'shipping', 
                                  'quantity', 'stock', 'available', 'delivery', 'total'];
                if (skipWords.some(word => textUpper.includes(word.toUpperCase()))) continue;
                
                // Determine type based on context
                const combinedContext = (className + ' ' + id + ' ' + (elem.parentElement?.textContent?.toLowerCase() || '')).toLowerCase();
                let detectedType = 'unknown';
                
                if (combinedContext.includes('color') || combinedContext.includes('colour')) {
                    detectedType = 'color';
                } else if (combinedContext.includes('size') || 
                          /\\b(xs|s|m|l|xl|xxl|\\d+)\\b/i.test(text)) {
                    detectedType = 'size';
                }
                
                // Tag-based priority scoring (higher is better)
                let score = 1;
                const tag = elem.tagName.toUpperCase();
                if (['BUTTON', 'LABEL', 'INPUT'].includes(tag)) score = 5;
                else if (['A', 'LI'].includes(tag)) score = 4;
                else if (['SPAN', 'DIV'].includes(tag)) score = 3;
                else if (tag === 'IMG') score = 2;
                
                // Boost if it has explicit variant attributes
                if (className.includes('variant') || className.includes('swatch') || 
                    id.includes('variant') || id.includes('swatch')) {
                    score += 2;
                }
                
                variants.push({
                    ref_id: refId++,
                    score: score,
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
                        id: elem.id || ''
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
            
            variants.forEach((v, idx) => v.ref_id = idx + 1);
            
            return variants;
        }
        """
        
        try:
            # Pass arguments as a single object
            variants = await self.page.evaluate(js_code, {'bbox': bbox, 'variantType': variant_type})
            
            self.detected_variants = variants
            logger.info(f"Detected {len(variants)} variants within section")
            
            for v in variants[:10]:
                logger.info(f"  [{v['ref_id']:02d}] {v['type']:8s} '{v['text'][:30]:30s}' at ({v['bbox']['center_x']}, {v['bbox']['center_y']})")
            
            if len(variants) > 10:
                logger.info(f"  ... and {len(variants) - 10} more")
            
            return variants
            
        except Exception as e:
            logger.error(f"Error detecting variants in section: {e}")
            return []
    
    async def find_matching_ref_id(self, target_value: str) -> Optional[int]:
        """
        Find variant ref_id matching target value with STRICT matching for numbers
        """
        target_lower = target_value.lower().strip()
        target_normalized = target_lower.replace(' ', '').replace('-', '').replace('_', '')
        
        # Check if target is numeric (for waist, length, size numbers)
        is_numeric = target_normalized.isdigit()
        
        logger.info(f"🔍 Searching for '{target_value}' (numeric={is_numeric}) in {len(self.detected_variants)} variants...")
        
        matches = []
        
        for variant in self.detected_variants:
            text_lower = variant['value']
            text_normalized = text_lower.replace(' ', '').replace('-', '').replace('_', '')
            
            match_score = 0
            match_type = None
            
            # STRICT NUMERIC MATCHING - must be exact!
            if is_numeric:
                # For numbers, only accept EXACT match
                if target_normalized == text_normalized:
                    # Collect all matches to pick the one with better tag priority later
                    matches.append(variant)
                continue
            
            # NON-NUMERIC: Use fuzzy strategies
            # Strategy 1: Exact match (best)
            if target_normalized == text_normalized:
                matches.append(variant)
                continue
            
            # Strategy 2: Target contained in text
            if target_normalized in text_normalized:
                match_score = 0.9
                match_type = "contains"
            
            # Strategy 3: Text contained in target
            elif text_normalized in target_normalized and len(text_normalized) > 3:
                match_score = 0.85
                match_type = "contained_by"
            
            if match_score >= 0.5:
                # We store the match score to help decide if tags are equal
                variant['_match_score'] = match_score
                matches.append(variant)
        
        if not matches:
            logger.warning(f"  ✗ NO MATCH for '{target_value}'")
            return None
            
        # Prioritize matches by element score (tag priority) then by match quality
        # We want the highest variant['score'] (tag priority)
        # If scores are equal, we want the best match quality
        matches.sort(key=lambda x: (x.get('score', 0), x.get('_match_score', 1.0)), reverse=True)
        
        best_variant = matches[0]
        logger.info(f"  Matched '{target_value}' -> [{best_variant['ref_id']:02d}] '{best_variant['text']}' (Score: {best_variant.get('score')})")
        return best_variant['ref_id']
    
    async def click_by_ref_id(self, ref_id: int) -> Dict[str, Any]:
        """Click variant by reference ID with Smart Scroll and Coordinate Refresh"""
        variant = next((v for v in self.detected_variants if v['ref_id'] == ref_id), None)
        
        if not variant:
            return {'success': False, 'error': f'Ref ID {ref_id} not found'}
        
        logger.info(f"🖱️  Clicking [{ref_id:02d}]: '{variant['text']}'")
        
        try:
            # 1. Initialize Scroller if not present (creating simple instance here to avoid init signature change issues)
            # Ideally this should be passed in __init__, but for quick fix we creating it here or importing
            from .smart_scroller import SmartScroller
            scroller = SmartScroller(self.page)
            
            # 2. Scroll to the element
            # We need a selector or handled element. We have `variant['element']` info but not a handle.
            # We can re-query the element or scroll to the specific coordinate Y
            # Since we have bbox, we can use window.scrollTo to center that Y position
            
            target_y = variant['bbox']['center_y']
            viewport_h = self.page.viewport_size['height'] if self.page.viewport_size else 720
            
            # Current scroll
            current_scroll = await self.page.evaluate("window.scrollY")
            
            # Smart scroll to that absolute Y (our bbox y is roughly absolute if from getBoundingClientRect relative to viewport + scrollY... wait, getBoundingClientRect is Viewport relative)
            # variant['bbox'] was calculated from `variants` array which used `rect.left` etc.
            # `detect_variants_in_section` uses `rect` from `getBoundingClientRect` which is Viewport Relative.
            # BUT wait, if we scroll, `rect` changes.
            
            # Let's scroll based on the CURRENT viewport position
            # We want to move the element to the center.
            # Delta = element_y - screen_center_y
            delta_y = variant['bbox']['center_y'] - (viewport_h / 2)
            
            # We can use the scroller to scroll by that delta
            # But safer is to find the element again and scroll to it using the clean SmartScroller
            
            # Re-find element to get a fresh handle for scrolling
            # Construct a specific selector or use evaluate handle
            # Falling back to direct scroll calculation for speed and robustness
            
            target_scroll_pos = current_scroll + delta_y
            
            # Use SmartScroller's logic manually or try to get a handle
            if variant['element']['id']:
                handle = await self.page.query_selector(f"#{variant['element']['id']}")
                if handle: await scroller.scroll_to_element(handle)
            else:
                 # Fallback: simple smooth scroll to position
                 await self.page.evaluate(f"""
                     window.scrollTo({{
                        top: {target_scroll_pos},
                        behavior: 'smooth'
                     }});
                 """)
                 logger.info("Performing smart scroll to variant position...")
                 await asyncio.sleep(0.5)

            # 3. RE-CALCULATE coordinates after scroll
            # Since we scrolled, the viewport-relative coordinates of the element have changed!
            # We need to find the element again visually or logically
            
            # Just click the center of the viewport? No, element might not be exactly center.
            # Best way: Re-run detection or use JS to click the specific element found previously
            
            # Let's use JS click which is robust to coordinates if we have a reference
            # But we want to simulate mouse click.
            
            # Re-detect position of this specific variant text in the new viewport
            # This is complex. 
            
            # alternative: Use the 'element' handle if possible.
            # Let's try to query by text and class close to original position?
            # Or just use the element handle if we can get it.
            
            # Let's try to find the element by text matching in the section again (fast)
            # Actually, `detect_variants_in_section` is fast.
            # Let's just re-run it?? No, overhead.
            
            # Hack: We know the absolute page coordinates (scrollTop + viewportY).
            # The absolute Y of the element on the page doesn't change (mostly).
            # AbsY = current_scroll (before scroll) + variant['bbox']['center_y']
            abs_x = variant['bbox']['center_x'] # X usually doesn't change with vertical scroll
            abs_y = current_scroll + variant['bbox']['center_y']
            
            # After scroll, new viewport Y = new_scroll_Y + relative_Y
            # so relative_Y = AbsY - new_scroll_Y
            
            new_scroll = await self.page.evaluate("window.scrollY")
            new_relative_y = abs_y - new_scroll
            
            # Click at new coordinates
            logger.info(f"Mouse click at absolute ({abs_x}, {abs_y}) -> Viewport ({abs_x}, {new_relative_y})")
            await self._visualize_click(abs_x, new_relative_y)
            await self.page.mouse.click(abs_x, new_relative_y)
            
            await asyncio.sleep(1)
            
            return {
                'success': True,
                'ref_id': ref_id,
                'clicked_variant': variant
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}
