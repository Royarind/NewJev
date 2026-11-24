#!/usr/bin/env python3
"""
Variant Selection Agent
Specialized agent for selecting product variants (color, size, etc.)
"""

from .base_agent import BaseAgent, Task, Result
from typing import Dict, Any, Optional
import json
import logging
import asyncio
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class VariantSelectionAgent(BaseAgent):
    """
    Agent specialized in selecting product variants
    Uses multi-tier approach: DOM → Vision → LLM reasoning
    WITH post-click validation to ensure correctness
    """
    
    def __init__(self, page, llm_client=None, memory=None):
        super().__init__("VariantSelectionAgent", llm_client, memory)
        self.page = page
        
        # Import vision systems
        from ..vision import EnhancedVisionSystem, ColorDetector
        from ..utils.selection_validator import SelectionValidator
        from ..utils.dropdown_selector import DropdownSelector
        from ..utils.focused_variant_detector import FocusedVariantDetector
        from ..utils.smart_scroller import SmartScroller
        from ..utils.distraction_manager import DistractionManager
        from ..utils.quantity_selector import QuantitySelector
        
        self.vision = EnhancedVisionSystem(debug_mode=False)
        self.color_detector = ColorDetector(debug_mode=False)
        self.validator = SelectionValidator(page)
        self.dropdown_selector = DropdownSelector(page)
        self.focused_detector = FocusedVariantDetector(page)
        self.scroller = SmartScroller(page)
        self.distraction_manager = DistractionManager(page)
        self.quantity_selector = QuantitySelector(page)
        # Root directory is the parent of 'src'
        try:
            self.root_dir = Path(__file__).resolve().parents[4]
            self.js_assets_dir = self.Path(__file__).resolve().parents[1] / 'dom_assets'
        except Exception:
            # Fallback for relative paths if resolve fails in some envs
            self.js_assets_dir = Path(__file__).resolve().parents[1] / 'dom_assets'

    # ── Variant synonym map ──────────────────────────────────────────────────
    # Maps every common alias to a single canonical token.
    # Used to normalise both the target and the candidate before comparing.
    _SIZE_SYNONYMS: Dict[str, str] = {
        # Extra Small
        "xs": "xs", "xsmall": "xs", "extrasmall": "xs", "x-small": "xs",
        "extra-small": "xs", "extra small": "xs",
        # Small
        "s": "s", "small": "s", "sm": "s",
        # Medium
        "m": "m", "medium": "m", "med": "m",
        # Large
        "l": "l", "large": "l", "lg": "l",
        # XL
        "xl": "xl", "xlarge": "xl", "x-large": "xl", "extra-large": "xl",
        "extra large": "xl", "extralarge": "xl",
        # XXL / 2XL
        "xxl": "xxl", "2xl": "xxl", "2xlarge": "xxl", "xx-large": "xxl",
        "double extra large": "xxl", "doubleextralarge": "xxl",
        # 3XL
        "3xl": "3xl", "xxxl": "3xl", "3x-large": "3xl",
        # 4XL / 5XL
        "4xl": "4xl", "xxxxl": "4xl", "5xl": "5xl",
        # One Size
        "onesize": "os", "one size": "os", "one size fits all": "os", "os": "os", "osfa": "os",
        # Standard clothing
        "regular": "regular", "reg": "regular",
        "plus": "plus", "plus size": "plus",
        "petite": "petite", "pet": "petite",
        "tall": "tall",
    }

    _COLOR_SYNONYMS: Dict[str, str] = {
        # Grays
        "slate": "slate", "slate gray": "slate", "slate grey": "slate",
        "slategray": "slate", "slategrey": "slate",
        "gray": "gray", "grey": "gray",
        "charcoal": "charcoal", "charcoal gray": "charcoal",
        "heather gray": "heathergray", "heather grey": "heathergray",
        # Blues
        "navy": "navy", "navy blue": "navy", "dark blue": "navy",
        "royal blue": "royalblue", "sky blue": "skyblue",
        # Blacks / whites
        "black": "black", "jet black": "black", "onyx": "black",
        "white": "white", "off white": "white", "ivory": "ivory",
        # Reds / pinks
        "red": "red", "crimson": "red", "burgundy": "burgundy",
        "pink": "pink", "rose": "rose", "blush": "blush",
        # Greens
        "green": "green", "olive": "olive", "forest green": "green",
        "sage": "sage",
        # Browns / tans
        "brown": "brown", "tan": "tan", "camel": "camel", "khaki": "khaki",
        "beige": "beige",
        # Purples / oranges / yellows
        "purple": "purple", "lavender": "lavender", "violet": "violet",
        "orange": "orange", "coral": "coral",
        "yellow": "yellow", "gold": "gold", "mustard": "mustard",
    }

    def _normalize_variant_text(self, text: str, variant_type: str = "") -> str:
        """
        Normalise a variant label to a canonical form for comparison.
        Steps:
          1. Lowercase + strip
          2. Remove punctuation noise (/, \\, dots, parentheses)
          3. Collapse whitespace
          4. Look up in synonym map (size first, then color)
          5. Return the canonical token (or the cleaned text if no synonym)
        """
        import re as _re
        cleaned = text.lower().strip()
        cleaned = _re.sub(r"[/()\\.,']+", " ", cleaned)
        cleaned = _re.sub(r"\s+", " ", cleaned).strip()
        # Remove noise words that add no info
        for noise in ("size", "colour", "color"):
            cleaned = _re.sub(rf"\b{noise}\b", "", cleaned).strip()
        cleaned = _re.sub(r"\s+", " ", cleaned).strip()
        key = cleaned.replace(" ", "").replace("-", "")

        # Try size synonyms
        if variant_type.lower() in ("size", "fit", "") or variant_type == "":
            if cleaned in self._SIZE_SYNONYMS:
                return self._SIZE_SYNONYMS[cleaned]
            if key in {k.replace(" ", "").replace("-", ""): v for k, v in self._SIZE_SYNONYMS.items()}:
                lookup = {k.replace(" ", "").replace("-", ""): v for k, v in self._SIZE_SYNONYMS.items()}
                return lookup[key]

        # Try color synonyms
        if variant_type.lower() in ("color", "colour", "") or variant_type == "":
            if cleaned in self._COLOR_SYNONYMS:
                return self._COLOR_SYNONYMS[cleaned]
            if key in {k.replace(" ", "").replace("-", ""): v for k, v in self._COLOR_SYNONYMS.items()}:
                lookup = {k.replace(" ", "").replace("-", ""): v for k, v in self._COLOR_SYNONYMS.items()}
                return lookup[key]

        return cleaned  # fall back to cleaned text

    def _variant_match_score(self, target: str, candidate: str, variant_type: str = "") -> int:
        """
        Return a match score 0-100 between target and candidate variant labels.
        Uses synonym normalisation so XL matches X-Large, Slate matches Slate Gray, etc.
        A score < 30 means no meaningful match — caller should NOT select this candidate.
        """
        t_raw = target.lower().strip().replace(" ", "").replace("-", "")
        c_raw = candidate.lower().strip().replace(" ", "").replace("-", "")

        t_norm = self._normalize_variant_text(target, variant_type)
        c_norm = self._normalize_variant_text(candidate, variant_type)
        t_key  = t_norm.replace(" ", "").replace("-", "")
        c_key  = c_norm.replace(" ", "").replace("-", "")

        # 100 — canonical tokens match exactly (synonym-aware exact match)
        if t_key == c_key:
            return 100

        # 95 — raw strings match after simple normalisation
        if t_raw == c_raw:
            return 95

        # 80 — one raw contains the other (e.g. "slategray" in "slate gray xl")
        if t_key and c_key:
            if t_key in c_key or c_key in t_key:
                # Require at least 60% overlap to avoid "S" matching "XS"
                shorter = min(len(t_key), len(c_key))
                longer  = max(len(t_key), len(c_key))
                if shorter / longer >= 0.6:
                    return 80

        # 60 — word-level overlap (punctuation-stripped)
        # Strips parentheses/punctuation so "sole)" == "sole", then checks:
        # (a) all target words are a subset of candidate words → score 60
        # (b) word overlap ratio ≥ 60% → score proportional
        import re as _re
        _strip_punct = lambda s: _re.sub(r"[^\w\s]", " ", s)
        t_words = set(_strip_punct(t_norm).lower().split())
        c_words = set(_strip_punct(c_norm).lower().split())
        t_words.discard(""); c_words.discard("")
        if t_words and c_words:
            # All target words found in candidate (e.g. "Blizzard Sole" inside long aria-label)
            if t_words.issubset(c_words):
                return 60
            overlap = len(t_words & c_words) / max(len(t_words), len(c_words))
            if overlap >= 0.6:
                return int(60 * overlap)

        return 0  # No meaningful match — do NOT select

    async def _visualize_click(self, x: float, y: float):
        """Show a blue circle at the click coordinates."""
        try:
            path = self.js_assets_dir / 'visualize_click.js'
            if not path.exists():
                self.logger.warning(f"JS Visualizer not found at {path}")
                return
            
            js_visualize = path.read_text(encoding='utf-8')
            self.logger.info(f"Visualizing click at ({x}, {y}) using {path}")
            await self.page.evaluate(js_visualize, {'x': x, 'y': y})
        except Exception as e:
            self.logger.warning(f"Click visualization failed: {e}")
    
    async def can_handle(self, task: Task) -> float:
        """
        Check if this task is about variant selection
        """
        if task.type in ['select_variant', 'color', 'size', 'quantity', 'add_to_cart']:
            return 0.95
        
        # Check description for variant keywords
        keywords = ['select', 'choose', 'color', 'size', 'variant', 'option']
        description_lower = task.description.lower()
        
        matches = sum(1 for kw in keywords if kw in description_lower)
        return min(matches * 0.2, 0.8)
    
    async def execute(self, task: Task) -> Result:
        """
        Execute variant selection, quantity selection, or add-to-cart
        """
        if task.type == 'add_to_cart':
            return await self._add_to_cart(task)
        
        # Handle quantity selection with dedicated QuantitySelector
        if task.type == 'quantity':
            variant_value = task.context.get('variant_value', '')
            if not variant_value:
                return Result(success=False, task=task, error="No quantity value provided")
            
            try:
                quantity = int(variant_value)
                self.logger.info(f"🔢 Setting quantity to {quantity}")
                result = await self.quantity_selector.select_quantity(quantity)
                
                return Result(
                    success=result.get('success', False),
                    task=task,
                    data=result,
                    reasoning=f"Quantity set to {quantity}" if result.get('success') else result.get('error', 'Unknown error'),
                    confidence=0.9 if result.get('success') else 0.0
                )
            except Exception as e:
                self.logger.error(f"Quantity selection failed: {e}")
                return Result(success=False, task=task, error=str(e))
            
        variant_type = task.context.get('variant_type', task.type)
        variant_value = task.context.get('variant_value', '')
        
        if not variant_value:
            return Result(
                success=False,
                task=task,
                error="No variant_value provided in task context"
            )
            
        # Auto-detect variant type if it's generic/custom but the value is size-like (e.g. Xs, S, M, L)
        import re
        CLOTHING_SIZE_RE = re.compile(r'^(xs|s|m|l|xl|xxl|2xl|3xl|4xl|5xl|small|medium|large|extra\s*small|extra\s*large|double\s*extra\s*large|one\s*size|\d{1,2}(\.\d)?w?)$', re.IGNORECASE)
        if variant_type.lower() not in ['size', 'color'] and CLOTHING_SIZE_RE.match(variant_value.strip()):
            self.logger.info(f"💡 Auto-detected variant value '{variant_value}' as a size variant. Treating type as 'size'.")
            variant_type = 'size'
        
        self.logger.info(f"Selecting {variant_type} = {variant_value}")
        
        # Try multi-tier selection
        result = await self._multi_tier_selection(variant_type, variant_value)
        
        return Result(
            success=result['success'],
            task=task,
            data=result,
            reasoning=result.get('reasoning', ''),
            confidence=result.get('confidence', 0.7)
        )
    
    async def _multi_tier_selection(self, variant_type: str, 
                                   variant_value: str) -> Dict[str, Any]:
        """
        Enhanced multi-tier selection strategy with overlay-based detection:
        Tier 0: Sequential Click & Text Change Validation (NEW - PRIORITY)
        Tier 0.5: Overlay-based detection (Direct text search)
        Tier 1: DOM-based (fastest) - OPTIONAL
        Tier 1.5: Focused Section Detection
        Tier 2: Dropdown/Select
        Tier 3: Vision + OCR
        Tier 3.5: VLM Reasoning
        Tier 4: LLM reasoning
        """
        
        # CRITICAL: Clear any popups/modals that might block the variant section
        self.logger.info(f"🛡️ Clearing distractions before selecting {variant_type}={variant_value}")
        try:
            await self.distraction_manager.clear_before_critical_action(
                action_name=f"variant selection: {variant_type}={variant_value}"
            )
        except Exception as e:
            self.logger.warning(f"⚠️ Distraction clearing failed (continuing anyway): {e}")

        # Wait a brief moment for the page to settle
        await asyncio.sleep(0.1)

        # Store initial cart count before performing selection
        try:
            initial_cart_count = await self.validator._get_cart_count()
            self.validator.set_initial_cart_count(initial_cart_count)
        except Exception as e:
            self.logger.warning(f"Could not set initial cart count: {e}")

        # ── ROBUST SELECTOR (primary method) ────────────────────────────────
        # Simple single-loop: find visible clickable elements, score by text,
        # click best match, validate via aria/label/url, retry on fail.
        self.logger.info("RobustVariantSelector: starting primary selection...")
        try:
            from ..utils.robust_variant_selector import RobustVariantSelector
            robust = RobustVariantSelector(self.page)
            robust_result = await robust.select(variant_type, variant_value)
            if robust_result.get('success'):
                self.logger.info(f"✅ RobustVariantSelector succeeded via {robust_result.get('method')}")
                return {
                    'success': True,
                    'method': robust_result.get('method', 'robust'),
                    'confidence': robust_result.get('confidence', 0.95),
                    'reasoning': f"RobustVariantSelector: clicked {robust_result.get('candidate_text', variant_value)!r}",
                }
            else:
                self.logger.warning(f"RobustVariantSelector failed: {robust_result.get('error')} — falling through to legacy tiers")
        except Exception as _robust_exc:
            self.logger.error(f"RobustVariantSelector exception: {_robust_exc}", exc_info=True)

        # TIER 0: Sequential Click & Text Change Validation (NEW PRIORITY)
        self.logger.info("Tier 0: Trying sequential click & text change validation...")
        try:
            seq_result = await self._sequential_text_change_selection(variant_type, variant_value)
            if seq_result.get('success'):
                self.logger.info("Tier 0 (Sequential click) succeeded")
                return seq_result
            else:
                self.logger.warning(f"Tier 0 (Sequential click) failed: {seq_result.get('error', 'unknown')}")
        except Exception as e:
            self.logger.error(f"Tier 0 (Sequential click) exception: {e}", exc_info=True)
            
        # TIER 0.5: Overlay-based detection (Direct text search)
        self.logger.info("Tier 0.5: Trying overlay-based detection...")
        try:
            overlay_result = await self._overlay_based_selection(variant_type, variant_value)
            self.logger.info(f"Tier 0.5 result: {overlay_result}")
            if overlay_result.get('success'):
                self.logger.info("Tier 0.5 (Overlay) succeeded")
                return overlay_result
            else:
                self.logger.warning(f"Tier 0.5 (Overlay) failed: {overlay_result.get('error', 'unknown')}")
        except Exception as e:
            self.logger.error(f"Tier 0.5 (Overlay) exception: {e}", exc_info=True)
        
        # TIER 1: Try DOM search (if available)
        try:
            # Try to import existing DOM finder
            from dom.service import UniversalDOMFinder
            
            finder = UniversalDOMFinder(self.page)
            dom_result = await finder.find_variant(variant_type, variant_value)
            
            if dom_result.get('success'):
                self.logger.info("Tier 1 (DOM) succeeded")
                return {
                    'success': True,
                    'method': 'dom',
                    'confidence': 0.9,
                    'reasoning': 'Found via DOM search'
                }
        except ImportError:
            self.logger.info("Tier 1 (DOM) skipped - module not available")
        except Exception as e:
            self.logger.warning(f"Tier 1 (DOM) failed: {e}")
        
        # TIER 1.5: Focused Section Detection (NEW!)
        self.logger.info("Tier 1.5: Focused section-based variant detection...")
        try:
            # Step 1: Detect variant section bbox
            section_bbox = await self.focused_detector.detect_variant_section()
            
            if section_bbox:
                # Step 2: Detect variants ONLY within that section
                variants = await self.focused_detector.detect_variants_in_section(variant_type)
                
                if variants:
                    # Step 3: Find matching variant
                    ref_id = await self.focused_detector.find_matching_ref_id(variant_value)
                    
                    if ref_id:
                        # Step 4: Click by reference ID
                        click_result = await self.focused_detector.click_by_ref_id(ref_id)
                        
                        if click_result['success']:
                            # Step 5: Validate
                            await asyncio.sleep(1)
                            validation = await self.validator.validate_any_selection(variant_type, variant_value)
                            
                            if validation['validated']:
                                self.logger.info(f"Tier 1.5 (Focused) succeeded with ref [{ref_id:02d}]")
                                return {
                                    'success': True,
                                    'method': 'focused_section_validated',
                                    'confidence': validation.get('confidence', 0.95),
                                    'reasoning': f"Detected section, found and validated variant [{ref_id:02d}]",
                                    'ref_id': ref_id
                                }
                            else:
                                self.logger.warning(f"Focused detection click [{ref_id}] failed validation")
                    else:
                        self.logger.warning(f"No match found in section for '{variant_value}'")
                else:
                    self.logger.warning("No variants found in detected section")
            else:
                self.logger.warning("Could not detect variant section")
        except Exception as e:
            self.logger.warning(f"Tier 1.5 (Focused) failed: {e}")
        
        # TIER 1.6: Direct Button Clicker (for numeric values like waist/length)
        self.logger.info("Tier 1.6: Trying direct button click...")
        try:
            from ..utils.direct_button_clicker import DirectButtonClicker
            clicker = DirectButtonClicker(self.page)
            # Pass variant_type so it finds the button in the correct section
            click_result = await clicker.find_and_click_by_text(variant_value, variant_type)
            
            if click_result.get('success'):
                # Clear any popups that might appear AFTER clicking
                await asyncio.sleep(0.5)
                try:
                    await self.distraction_manager.clear_before_critical_action("post-click stabilization")
                except Exception as e:
                    self.logger.debug(f"Post-click distraction clear: {e}")
                
                # Validate the selection
                await asyncio.sleep(1)
                validation = await self.validator.validate_any_selection(variant_type, variant_value)
                
                if validation['validated']:
                    self.logger.info(f"Tier 1.6 (Direct Button) succeeded and validated")
                    return {
                        'success': True,
                        'method': 'direct_button_validated',
                        'confidence': validation.get('confidence', 0.90),
                        'reasoning': f"Found and clicked button '{click_result['clicked_text']}'"
                    }
                else:
                    self.logger.warning("Direct button click succeeded but validation failed")
        except Exception as e:
            self.logger.warning(f"Tier 1.6 (Direct Button) failed: {e}")
        
        # TIER 2: Try Dropdown/Select
        self.logger.info("Tier 2: Trying dropdown selection...")
        try:
            dropdown_result = await self.dropdown_selector.select_variant(variant_type, variant_value)
            
            if dropdown_result.get('success'):
                # Validate the selection
                await asyncio.sleep(1)
                validation = await self.validator.validate_any_selection(variant_type, variant_value)
                
                if validation['validated']:
                    self.logger.info(f"Tier 2 (Dropdown) succeeded and validated")
                    return {
                        'success': True,
                        'method': 'dropdown_validated',
                        'confidence': dropdown_result.get('confidence', 0.85),
                        'reasoning': f"Selected from dropdown: {dropdown_result.get('selected_text', variant_value)}"
                    }
                else:
                    self.logger.warning("Dropdown selection failed validation")
        except Exception as e:
            self.logger.warning(f"Tier 2 (Dropdown) failed: {e}")
        
        # TIER 3: Vision + OCR (visual swatches)
        self.logger.info("Tier 3: Trying vision + OCR...")
        try:
            vision_result = await self._vision_search(variant_type, variant_value)
            if vision_result['success']:
                self.logger.info("Tier 3 (Vision) succeeded")
                return vision_result
        except Exception as e:
            self.logger.warning(f"Tier 3 (Vision) failed: {e}")
        
        # ALL NON-AI TIERS FAILED
        self.logger.error(f"All non-AI tiers failed for {variant_type}={variant_value}")
        return {
            'success': False,
            'method': 'none',
            'error': f'All tiers failed to find {variant_type}={variant_value}. Overlay found {overlay_count if "overlay_count" in locals() else 0} elements but no match.'
        }
    
    async def _vision_search(self, variant_type: str, 
                            variant_value: str) -> Dict[str, Any]:
        """
        Vision-based variant search using OpenCV + OCR
        WITH POST-CLICK VALIDATION and retry logic
        """
        # Take screenshot
        screenshot_path = 'temp_variant.png'
        await self.page.screenshot(path=screenshot_path)
        
        # Get full visual analysis (includes text elements)
        analysis = await self.vision.analyze_page(screenshot_path)
        
        # Special handling for colors with validation
        if variant_type.lower() == 'color':
            # Detect color swatches visually
            swatches = self.color_detector.detect_color_swatches(screenshot_path)
            
            if not swatches:
                self.logger.warning("No color swatches detected")
                return {'success': False, 'error': 'No swatches found'}
            
            # Sort swatches by score (if scored) or try all
            swatches_to_try = swatches[:5]  # Try top 5 candidates
            
            # Match by color name with text context
            scored_matches = []
            for swatch in swatches_to_try:
                # This returns None or swatch with match_confidence
                matched = self.color_detector.match_color_name(
                    variant_value, 
                    [swatch],  # Pass single swatch
                    text_elements=analysis.get('text_elements', [])
                )
                
                if matched:
                    scored_matches.append(matched)
            
            # If no scored matches, try all swatches as fallback
            if not scored_matches:
                self.logger.warning("No confident matches found, trying all swatches")
                scored_matches = swatches_to_try
            
            # Try each candidate with validation
            for attempt, swatch in enumerate(scored_matches):
                coords = swatch['coordinates']
                
                # HEADER PROTECTION: Skip elements likely in the header (nav links, search icons)
                if coords['y'] < 160:
                    self.logger.warning(f"Skipping candidate Y={coords['y']} (Header Zone Protection)")
                    continue
                
                # PRODUCT IMAGE PROTECTION: Skip large images (likely main product photos, not swatches)
                if swatch.get('width', 0) > 200 or swatch.get('height', 0) > 200:
                    self.logger.warning(f"Skipping large image {swatch.get('width')}x{swatch.get('height')} (Product Image Protection)")
                    continue
                
                # THUMBNAIL GALLERY PROTECTION: Skip if X position suggests it's in a thumbnail strip
                # Thumbnails are usually on the left side (X < 150) or in a vertical strip
                if coords['x'] < 150 and swatch.get('height', 0) > 80:
                    self.logger.warning(f"Skipping left-side image at X={coords['x']} (Thumbnail Gallery Protection)")
                    continue
                
                self.logger.info(f"Attempt {attempt + 1}/{len(scored_matches)}: "
                               f"Trying swatch at ({coords['x']}, {coords['y']}) "
                               f"color={swatch.get('color_name', 'unknown')}")
                
                # VIEWPORT-AWARE SCROLL & CLICK (Vision-based)
                # coords come from screenshot analysis so element is already in viewport;
                # only scroll if the element happens to be outside visible bounds.
                try:
                    current_scroll = await self.page.evaluate("window.scrollY")
                    viewport_h = self.page.viewport_size['height'] if self.page.viewport_size else 720
                    abs_y = current_scroll + coords['y']
                    already_visible = 0 <= coords['y'] <= viewport_h

                    if not already_visible:
                        target_scroll = max(0, abs_y - (viewport_h / 2))
                        await self.page.evaluate(f"window.scrollTo({{top: {target_scroll}, behavior: 'smooth'}})")
                        await asyncio.sleep(0.5)
                        new_scroll = await self.page.evaluate("window.scrollY")
                        click_y = abs_y - new_scroll
                    else:
                        click_y = coords['y']

                    self.logger.info(f"Vision Click: Clicking ({coords['x']}, {click_y:.0f})")
                    await self._visualize_click(coords['x'], click_y)
                    await self.page.mouse.click(coords['x'], click_y)
                except Exception as e:
                    self.logger.warning(f"Scroll-click failed, trying direct click: {e}")
                    await self._visualize_click(coords['x'], coords['y'])
                    await self.page.mouse.click(coords['x'], coords['y'])
                
                # Wait for page to update
                await asyncio.sleep(1.5)
                
                # VALIDATE the selection
                validation = await self.validator.validate_color_selection(variant_value)
                
                if validation['validated']:
                    confidence = validation['confidence']
                    self.logger.info(f"Validated: {validation['selected_text']} "
                                   f"(confidence: {confidence:.0%}, method: {validation['method']})")
                    
                    return {
                        'success': True,
                        'method': 'visual_color_match_validated',
                        'confidence': confidence,
                        'reasoning': f"Matched and validated color {swatch.get('color_name')} at ({coords['x']}, {coords['y']}). "
                                   f"DOM confirms: {validation['selected_text']}"
                    }
                else:
                    self.logger.warning(f"Validation failed for swatch at ({coords['x']}, {coords['y']})")
                    # Continue to next swatch
            
            # All attempts failed
            return {
                'success': False,
                'error': f"Tried {len(scored_matches)} swatches but validation failed for all"
            }
        
        # General visual search for other variants (sizes, etc.)
        matched_element = self._find_matching_element(variant_value, analysis)
        
        if matched_element:
            coords = matched_element['center']
            
            # HEADER PROTECTION (General)
            if coords['y'] < 160:
                 self.logger.warning(f"Skipping element Y={coords['y']} (Header Zone Protection)")
                 return {'success': False, 'error': 'Best match was in header zone'}

            # VIEWPORT-AWARE SCROLL & CLICK (General)
            try:
                current_scroll = await self.page.evaluate("window.scrollY")
                viewport_h = self.page.viewport_size['height'] if self.page.viewport_size else 720
                abs_y = current_scroll + coords['y']
                already_visible = 0 <= coords['y'] <= viewport_h

                if not already_visible:
                    target_scroll = max(0, abs_y - (viewport_h / 2))
                    await self.page.evaluate(f"window.scrollTo({{top: {target_scroll}, behavior: 'smooth'}})")
                    await asyncio.sleep(0.5)
                    new_scroll = await self.page.evaluate("window.scrollY")
                    click_y = abs_y - new_scroll
                else:
                    click_y = coords['y']

                self.logger.info(f"Vision Click: Clicking ({coords['x']}, {click_y:.0f})")
                await self._visualize_click(coords['x'], click_y)
                await self.page.mouse.click(coords['x'], click_y)
            except:
                await self._visualize_click(coords['x'], coords['y'])
                await self.page.mouse.click(coords['x'], coords['y'])
            
            # Wait and validate
            await asyncio.sleep(1)
            validation = await self.validator.validate_any_selection(variant_type, variant_value)
            
            if validation['validated']:
                return {
                    'success': True,
                    'method': 'visual_match_validated',
                    'confidence': validation['confidence'],
                    'reasoning': f"Matched and validated element with text '{matched_element.get('text', '')}'"
                }
        
        # Fallback: Try JavaScript-based text search
        self.logger.info(f"Vision search failed for '{variant_value}', trying JS fallback...")
        from ..utils.dom_fallback import find_element_by_text_js
        
        js_result = await find_element_by_text_js(self.page, variant_value)
        
        if js_result:
            coords = js_result['coordinates']
            self.logger.info(f"JS fallback found: '{js_result['text']}' at ({coords['x']}, {coords['y']})")
            
            await self._visualize_click(coords['x'], coords['y'])
            await self.page.mouse.click(coords['x'], coords['y'])
            await asyncio.sleep(1)
            
            # Validate
            validation = await self.validator.validate_any_selection(variant_type, variant_value)
            
            if validation['validated']:
                return {
                    'success': True,
                    'method': 'js_fallback_validated',
                    'confidence': validation['confidence'],
                    'reasoning': f"Found via JS fallback: '{js_result['text']}' and validated"
                }
        
        return {'success': False, 'error': f'Could not find element for {variant_type}={variant_value}'}
    
    def _find_matching_element(self, target_value: str, 
                               analysis: Dict) -> Optional[Dict]:
        """
        Find matching element in visual analysis data
        Improved to handle sizes, text variations, and prioritization
        """
        self.logger.info(f"Looking for element matching: '{target_value}'")
        candidates = []

        def _score_element(el_dict, base_priority: int):
            text = el_dict.get('text', '')
            score = self._variant_match_score(target_value, text)
            if score >= 60:
                candidates.append({
                    'el': el_dict,
                    'priority': base_priority + (1 if score == 100 else 0),
                    'score': score / 100.0,
                })

        for swatch in analysis.get('swatches', []):
            _score_element(swatch, 4)
        for button in analysis.get('buttons', []):
            _score_element(button, 5)
        for text_elem in analysis.get('text_elements', []):
            _score_element(text_elem, 2)

        if not candidates:
            self.logger.warning(
                f"No matching element found for '{target_value}' "
                f"(synonym-aware threshold 60 not met)"
            )
            return None
            
        # Sort by priority then by score
        candidates.sort(key=lambda x: (x['priority'], x['score']), reverse=True)
        best = candidates[0]['el']
        
        self.logger.info(f"Selected best match: '{best.get('text')}' (Priority: {candidates[0]['priority']})")
        return best
    
    async def _llm_visual_reasoning(self, variant_type: str, 
                                   variant_value: str) -> Dict[str, Any]:
        """
        Use LLM to reason over structured visual data
        """
        if not self.llm:
            return {'success': False, 'error': 'No LLM client available'}
        
        # Get visual analysis
        screenshot_path = 'temp_llm.png'
        await self.page.screenshot(path=screenshot_path)
        analysis = await self.vision.analyze_page(screenshot_path)
        
        # Build prompt for LLM
        prompt = f"""
        You are helping select a product variant on an e-commerce page.
        
        GOAL: Find and click {variant_type} = "{variant_value}"
        
        AVAILABLE VISUAL ELEMENTS:
        
        Swatches (likely variants):
        {json.dumps(analysis['swatches'][:10], indent=2)}
        
        Buttons:
        {json.dumps(analysis['buttons'][:10], indent=2)}  
        
        Text Elements:
        {json.dumps(analysis['text_elements'][:20], indent=2)}
        
        TASK:
        1. Which element is most likely to be the {variant_type} = "{variant_value}"?
        2. What are the click coordinates?
        
        Return ONLY valid JSON:
        {{
            "found": true/false,
            "coordinates": {{"x": <int>, "y": <int>}},
            "confidence": <float 0-1>,
            "reasoning": "<explanation>"
        }}
        """
        
        try:
            response = await self.llm.complete(prompt)
            
            # Extract JSON from response
            decision = self._extract_json(response)
            
            if decision.get('found') and 'coordinates' in decision:
                coords = decision['coordinates']
                await self._visualize_click(coords['x'], coords['y'])
                await self.page.mouse.click(coords['x'], coords['y'])
                
                return {
                    'success': True,
                    'method': 'llm_reasoning',
                    'confidence': decision.get('confidence', 0.7),
                    'reasoning': decision.get('reasoning', 'LLM-based selection')
                }
        except Exception as e:
            self.logger.error(f"LLM reasoning failed: {e}")
        
        return {'success': False}
    
    async def _vlm_selection(self, variant_type: str, 
                             variant_value: str) -> Dict[str, Any]:
        """
        Use Multimodal VLM (Qwen2.5-VL) to directly locate elements on screenshot
        """
        if not self.llm:
            return {'success': False, 'error': 'No LLM client available'}
        
        screenshot_path = 'temp_vlm.png'
        
        # Close overlays/popups before taking screenshot
        await self._close_overlays()
        await asyncio.sleep(1)  # Let page settle after closing popup
        
        await self.page.screenshot(path=screenshot_path)
        
        # Get OCR data to help anchor the VLM
        analysis = await self.vision.analyze_page(screenshot_path)
        ocr_text = " ".join([t['text'] for t in analysis.get('text_elements', [])[:30]])
        
        # Qwen2.5-VL specific prompt for UI localization
        prompt = f"""
        You are an expert at web UI automation and visual reasoning.
        
        CONTEXT FROM OCR:
        "{ocr_text}..."
        
        GOAL: Find the exact click coordinates for choosing {variant_type} = "{variant_value}".
        
        VISUAL CLUES:
        1. Look for a section labeled "{variant_type}" or similar.
        2. Identify the swatches, buttons, or dropdown options in that section.
        3. **PRIORITY**: Choose a BUTTON or a LABEL over a simple DIV or IMG.
        4. **AVOID THUMBNAILS**: Do not click on the large product images or small thumbnail galleries that show different angles. Focus ONLY on the variant selection area (swatches/size buttons).
        5. Even if the text "{variant_value}" is not explicitly visible inside the element, look for the element that visually represents it (e.g., a color swatch that looks like "{variant_value}").
        
        THINK STEP BY STEP:
        - Locate the product name and current variant labels.
        - Find the list of available swatches/buttons for "{variant_type}".
        - Identify which one matches "{variant_value}".
        
        Return the center coordinates (x, y) relative to the 1280x720 image.
        
        Return ONLY valid JSON in this format:
        {{
            "reasoning": "your step-by-step logic",
            "found": true,
            "coordinates": {{"x": <int>, "y": <int>}},
            "element_description": "short description of what you found",
            "confidence": <float 0-1>
        }}
        """
        
        try:
            # Determine VLM model based on provider
            if self.llm.provider == 'gemini':
                vlm_model = os.getenv('GEMINI_MODEL', "gemini-1.5-flash")
            else:
                # Use a verified powerful vision model on OpenRouter
                vlm_model = os.getenv('OPENROUTER_MODEL', "qwen/qwen-2-vl-72b-instruct")
            
            response = await self.llm.complete_with_vision(
                prompt, 
                screenshot_path, 
                model=vlm_model
            )
            
            # Log response to file to avoid terminal garbling
            with open('vlm_last_response.txt', 'w', encoding='utf-8') as f:
                f.write(response)
            
            self.logger.info(f"VLM Response length: {len(response)} (saved to vlm_last_response.txt)")
            
            # Check for error in response
            if '"error":' in response:
                error_data = json.loads(response)
                return {'success': False, 'error': f"LLM Client error: {error_data.get('error')}"}
                
            decision = self._extract_json(response)
            
            if decision.get('found') and 'coordinates' in decision:
                coords = decision['coordinates']
                self.logger.info(f"VLM found {variant_type}={variant_value} at {coords}")
                
                # Perform the click
                await self._visualize_click(coords['x'], coords['y'])
                await self.page.mouse.click(coords['x'], coords['y'])
                
                # Wait and validate
                await asyncio.sleep(1.5)
                
                validation = await self.validator.validate_any_selection(variant_type, variant_value)
                
                if validation['validated']:
                    return {
                        'success': True,
                        'method': 'vlm_localization_validated',
                        'confidence': decision.get('confidence', 0.8),
                        'reasoning': f"VLM localized element: {decision.get('element_description')}"
                    }
                else:
                    self.logger.warning(f"VLM click failed validation: {validation.get('error')}")
                    return {'success': False, 'error': f"Validation failed: {validation.get('error')}"}
            else:
                self.logger.warning(f"VLM did not find element: {decision.get('reasoning')}")
                return {'success': False, 'error': 'VLM did not find element'}
        except Exception as e:
            self.logger.error(f"VLM selection failed: {e}")
            return {'success': False, 'error': str(e)}
            
        return {'success': False, 'error': 'Unknown failure in _vlm_selection'}

    async def _add_to_cart(self, task: Task) -> Result:
        """
        Robust Add to Cart implementation with VLM fallback
        INCLUDES: Quantity selection, final validation, and retries.
        """
        # 0. QUANTITY SELECTION (NEW)
        quantity = task.context.get('quantity')
        if quantity and int(quantity) > 1:
            self.logger.info(f"Detected quantity request: {quantity}")
            q_res = await self.quantity_selector.select_quantity(int(quantity))
            if not q_res.get('success'):
                self.logger.warning(f"Quantity selection failed: {q_res.get('error')}. Continuing anyway...")
            else:
                self.logger.info(f"Quantity {quantity} selected successfully")

        # Check if the product has already been added to the cart (e.g. via immediate size click)
        try:
            current_cart_count = await self.validator._get_cart_count()
            target_quantity = int(quantity) if quantity else 1
            if current_cart_count is not None and current_cart_count >= target_quantity:
                self.logger.info(f"🛒 Cart count is {current_cart_count} (expected >= {target_quantity}). Product already added to cart. Bypassing pre-add validation and click.")
                return Result(success=True, task=task, data={'method': 'bypass_already_in_cart'})
        except Exception as e:
            self.logger.warning(f"Error checking cart count bypass in _add_to_cart: {e}")

        # 1. FINAL VALIDATION & RETRY (Pre-Click)
        expected_variants = task.context.get('expected_variants', {})
        if expected_variants:
            self.logger.info("Performing final pre-click validation with retries...")
            
            retry_count = 0
            max_retries = 3
            validation_passed = False
            
            while retry_count <= max_retries:
                validation_errors = []
                missing_variants = {} # Store specific missing items for retry
                
                import re
                CLOTHING_SIZE_RE = re.compile(r'^(xs|s|m|l|xl|xxl|2xl|3xl|4xl|5xl|small|medium|large|extra\s*small|extra\s*large|double\s*extra\s*large|one\s*size|\d{1,2}(\.\d)?w?)$', re.IGNORECASE)
                for v_type, v_val in expected_variants.items():
                    if v_type.lower() == 'quantity': continue
                    
                    val_type = v_type
                    if val_type.lower() not in ['size', 'color'] and CLOTHING_SIZE_RE.match(v_val.strip()):
                        val_type = 'size'
                        
                    res = await self.validator.validate_any_selection(val_type, v_val)
                    if not res.get('validated'):
                        # Fallback: WooCommerce variation_id first, then visual/aria/label
                        try:
                            from ..utils.robust_variant_selector import (
                                _validate_label, _validate_aria, _validate_visual_state,
                                _validate_woo_variation,
                            )
                            visual_ok = False
                            # WooCommerce ground-truth: variation_id set + ATC button enabled
                            try:
                                visual_ok = await _validate_woo_variation(self.page)
                                if visual_ok:
                                    self.logger.info(f"Validation OK (WooCommerce variation_id): {v_type}={v_val}")
                            except Exception:
                                visual_ok = False
                            if not visual_ok:
                                visual_ok = (
                                    await _validate_visual_state(self.page, v_val) or
                                    await _validate_aria(self.page, v_val) or
                                    await _validate_label(self.page, val_type, v_val)
                                )
                            # Shopify fallback: only on Shopify pages (not WooCommerce)
                            if not visual_ok and val_type.lower() == 'size':
                                try:
                                    vid = await self.page.evaluate("""() => {
                                        const isShopify = !!(window.Shopify || window.ShopifyAnalytics || window.meta?.product);
                                        if (!isShopify) return null;
                                        const inp = document.querySelector('input[name="id"], input[name="variant_id"]');
                                        return inp ? (inp.value || null) : null;
                                    }""")
                                    if vid:
                                        self.logger.info(f"Validation OK (Shopify variant ID={vid}): {v_type}={v_val}")
                                        visual_ok = True
                                except Exception:
                                    pass
                            if visual_ok:
                                self.logger.info(f"Validation OK (visual fallback): {v_type}={v_val}")
                            else:
                                validation_errors.append(f"{v_type}={v_val}")
                                missing_variants[v_type] = v_val
                        except Exception as _ve:
                            self.logger.warning(f"Visual fallback exception for {v_type}={v_val}: {_ve}")
                            validation_errors.append(f"{v_type}={v_val}")
                            missing_variants[v_type] = v_val
                    else:
                        self.logger.info(f"Validation OK: {v_type}={v_val} (as {val_type})")
                
                if not validation_errors:
                    validation_passed = True
                    break
                
                # If we have errors and retries left
                if retry_count < max_retries:
                    self.logger.warning(f"Validation failed (Attempt {retry_count+1}/{max_retries+1}). Retrying missing variants: {missing_variants}")
                    
                    # RETRY LOGIC: Re-attempt selection for missing keys
                    for m_type, m_val in missing_variants.items():
                        self.logger.info(f"RETRYING selection for {m_type}={m_val}")
                        # Ensure we scroll to viewport first (implicitly handled by execute->methods but good to enforce)
                        
                        # Call internal selection again
                        # We use execute recursively or call _multi_tier_selection directly
                        # _multi_tier_selection is better to avoid full task overhead
                        await self._multi_tier_selection(m_type, m_val)
                        await asyncio.sleep(1) # Wait for UI update
                    
                    retry_count += 1
                else:
                    self.logger.error("Max retries reached. Validation still failing.")
                    break

            if not validation_passed:
                # Non-blocking: variants were already validated during selection (Task 2).
                # Log the warning but proceed — the ATC attempt + post-click validation
                # will confirm whether the item actually lands in the cart.
                self.logger.warning(
                    f"Pre-add visual re-check failed for: {', '.join(validation_errors)} "
                    f"— proceeding anyway (variants confirmed during selection step)"
                )

        self.logger.info("Attempting Add to Cart...")

        # ── TIER 0: Shopify JSON variant API (most reliable) ──────────────────
        # Extracts the full variant list from the page's embedded product JSON,
        # matches our selections (all variant keys), and POSTs to /cart/add.js
        # directly — bypasses all UI interaction and hidden-input complications.
        expected_variants = task.context.get('expected_variants', {})
        _atc_variants = {k: v for k, v in expected_variants.items() if k.lower() != 'quantity'}
        if _atc_variants:
            try:
                _json_result = await self.page.evaluate("""async (variants) => {
                    let allVars = null;
                    const log = [];

                    // ── Method 1: /products/<handle>.js  (most reliable) ─────
                    try {
                        const handle = window.location.pathname
                            .replace(/^\/products\//, '').split('?')[0].split('#')[0];
                        if (handle) {
                            const r = await fetch(`/products/${handle}.js`,
                                                  {credentials: 'include'});
                            if (r.ok) {
                                const d = await r.json();
                                allVars = d.variants;
                                log.push('source:products_js handle=' + handle +
                                         ' vars=' + allVars.length);
                            }
                        }
                    } catch(e) { log.push('products_js_err:' + e.message); }

                    // ── Method 2: <script type="application/json"> tag ───────
                    if (!allVars) {
                        for (const sel of [
                            'script[type="application/json"][id*="ProductJson"]',
                            'script[type="application/json"][id*="product-json"]',
                            'script[type="application/json"][data-product-json]',
                            '#ProductJson-product-template',
                        ]) {
                            const el = document.querySelector(sel);
                            if (!el) continue;
                            try {
                                allVars = JSON.parse(el.textContent).variants;
                                if (allVars) { log.push('source:script_tag sel=' + sel); break; }
                            } catch(e) {}
                        }
                    }

                    // ── Method 3: window globals ─────────────────────────────
                    if (!allVars) {
                        allVars = window.meta?.product?.variants ||
                                  window.ShopifyAnalytics?.meta?.product?.variants ||
                                  window.theme?.product?.variants || null;
                        if (allVars) log.push('source:window_global');
                    }

                    // ── Method 4: Inline <script> scan ───────────────────────
                    if (!allVars) {
                        for (const s of document.querySelectorAll('script:not([src])')) {
                            const txt = s.textContent;
                            if (!txt.includes('"variants"') || txt.length > 800000) continue;
                            const m = txt.match(/"variants"\s*:\s*(\[\{[\s\S]*?\}\])/);
                            if (m) {
                                try { allVars = JSON.parse(m[1]); log.push('source:script_scan'); break; }
                                catch(e) {}
                            }
                        }
                    }

                    if (!allVars || !Array.isArray(allVars))
                        return {ok: false, error: 'no_variant_data', log};

                    // ── Match our selections (fuzzy) ─────────────────────────
                    // Normalise: strip whitespace, lowercase, collapse inner spaces
                    const norm = s => (s || '').toLowerCase().trim().replace(/\s+/g,' ');
                    const keys = Object.keys(variants);
                    let matchId = null, matchTitle = null;

                    for (const v of allVars) {
                        // Collect all option strings for this variant
                        const optStrs = [
                            v.option1, v.option2, v.option3,
                            ...(v.options || [])
                        ].filter(Boolean).map(norm);

                        const allMatch = keys.every(k => {
                            const val = norm(variants[k]);
                            // Exact match OR the option string CONTAINS our value
                            return optStrs.some(o => o === val || o.includes(val) || val.includes(o));
                        });

                        if (allMatch) {
                            matchId    = v.id;
                            matchTitle = v.title;
                            break;
                        }
                    }

                    if (!matchId) {
                        const sample = allVars.slice(0, 8).map(v => v.title);
                        return {ok: false, error: 'variant_not_found',
                                total: allVars.length, sample, log};
                    }

                    log.push('matched:' + matchTitle + ' id=' + matchId);

                    // ── POST to /cart/add.js ──────────────────────────────────
                    const r = await fetch('/cart/add.js', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        credentials: 'include',
                        body: JSON.stringify({id: matchId, quantity: 1})
                    });
                    const d = await r.json();
                    if (r.ok && d.id)
                        return {ok: true, id: d.id, title: d.title,
                                variantId: matchId, log};
                    return {ok: false, error: d.description || d.message || 'api_error',
                            status: r.status, variantId: matchId, log};
                }""", _atc_variants)

                self.logger.info(f"Shopify JSON strategy result: {_json_result}")
                if _json_result and _json_result.get('ok'):
                    self.logger.info(
                        f"✅ Shopify JSON add succeeded: '{_json_result.get('title')}' "
                        f"(variant {_json_result.get('variantId')})"
                    )
                    return Result(success=True, task=task,
                                  data={'method': 'shopify_json_api', 'confidence': 0.98})
            except Exception as _je:
                self.logger.warning(f"Shopify JSON strategy exception: {_je}")

        # PRE-ATC: Log Shopify variant ID state for diagnostics
        try:
            _vid_diag = await self.page.evaluate("""() => {
                const inp = document.querySelector('input[name="id"], select[name="id"], input[name="variant_id"]');
                const formId = inp ? (inp.value || '(empty)') : '(not found)';
                const shopAna = window.ShopifyAnalytics?.meta?.selectedVariantId || null;
                const meta = window.meta?.product?.selected_variant?.id || null;
                // Check radio inputs for selected size
                const checkedRadio = [...document.querySelectorAll('input[type="radio"]:checked')]
                    .map(r => ({name: r.name, value: r.value}));
                // Check SingleOptionSelector selects
                const optSels = [...document.querySelectorAll('select[id*="SingleOptionSelector"], select[id*="option-"]')]
                    .map(s => ({id: s.id, value: s.value}));
                return {form_id: formId, shopify_analytics: shopAna, meta_selected: meta,
                        checked_radios: checkedRadio, option_selects: optSels};
            }""")
            self.logger.info(f"Pre-ATC Shopify variant state: {_vid_diag}")
        except Exception as _diag_e:
            self.logger.debug(f"Pre-ATC variant state diagnostic error: {_diag_e}")

        # Tier 1: DOM Click
        add_to_cart_selectors = [
            # Flipkart specific
            'button._2KpZ6l._2U9uOA._3v1-ww',
            'button[class*="_2KpZ6l"]',
            'li._2KpZ6l button',
            # Exact text matches
            'button:has-text("ADD TO CART")',
            'button:has-text("Add to Cart")',
            'button:has-text("Add to Bag")',
            'button:has-text("ADD TO BAG")',
            'button:has-text("Buy Now")',
            'button:has-text("BUY NOW")',
            # Allbirds: before size selected shows "SELECT A SIZE"
            'button:has-text("SELECT A SIZE")',
            'button:has-text("Select a Size")',
            'button:has-text("CHOOSE A SIZE")',
            # Generic patterns
            'button[id*="add-to-bag" i]',
            '#add-to-bag', # Macy's specific
            '#Button_addToCartDrawer_pdp', # Costco specific
            'button[id*="add-to-cart" i]',
            'button[class*="add-to-cart" i]',
            'button[name="add" i]',
            'input[type="submit"][value*="Add" i]',
            '.add-to-cart',
            '.pdp-add-to-bag', # Myntra specific
            '.add-to-bag',
            # Allbirds: product form submit
            'form[action*="cart"] button[type="submit"]',
            'form[action*="cart"] input[type="submit"]',
        ]
        
        for selector in add_to_cart_selectors:
            try:
                self.logger.info(f"Trying Add to Cart selector: {selector}")
                btn = await self.page.query_selector(selector)
                if btn and await btn.is_visible():
                    self.logger.info(f"Found visible button with selector: {selector}")
                    
                    # 1. SMART SCROLL
                    await self.scroller.scroll_to_element(btn)
                    
                    # 2. Visualize click before action
                    box = await btn.bounding_box()
                    if box:
                        await self._visualize_click(box['x'] + box['width']/2, box['y'] + box['height']/2)

                    # 3. CLICK - Use btn.click() which handles visibility/scrolling natively in Playwright
                    await btn.click()
                    self.logger.info(f"Clicked Add to Cart via btn.click(): {selector}")
                    
                    # 4. POST-CLICK VALIDATION — 3.5s for SPA/drawer animation
                    await asyncio.sleep(3.5)

                    cart_added = False

                    # 4a. Shopify/generic cart API — retry 3× for slow SPAs
                    for _atc_attempt in range(3):
                        try:
                            cart_api_count = await self.page.evaluate("""async () => {
                                try {
                                    const r = await fetch('/cart.js', {credentials: 'include'});
                                    if (r.ok) { const d = await r.json(); return d.item_count || 0; }
                                } catch(e) {}
                                try {
                                    const r2 = await fetch('/wc-api/v3/cart', {credentials: 'include'});
                                    if (r2.ok) { const d2 = await r2.json(); return (d2.items || []).length; }
                                } catch(e) {}
                                return null;
                            }""")
                            if cart_api_count and int(cart_api_count) > 0:
                                self.logger.info(f"4a: cart API count={cart_api_count} ✅")
                                cart_added = True
                                break
                        except Exception:
                            pass
                        if not cart_added and _atc_attempt < 2:
                            await asyncio.sleep(1)

                    # 4b. Cart count badge (visible header badge)
                    if not cart_added:
                        try:
                            badge_count = await self.page.evaluate("""() => {
                                const sels = [
                                    '[data-cart-count]', '[data-item-count]',
                                    '.cart-count', '#cart-count', '.bag-count',
                                    '[class*="cart-count"]', '[class*="CartCount"]',
                                    '[aria-label*="cart"]', '[aria-label*="bag"]',
                                ];
                                for (const s of sels) {
                                    const el = document.querySelector(s);
                                    if (!el) continue;
                                    const txt = (el.textContent || el.getAttribute('data-cart-count') || '').trim();
                                    const n = parseInt(txt);
                                    if (!isNaN(n) && n > 0) return n;
                                }
                                return 0;
                            }""")
                            if badge_count and int(badge_count) > 0:
                                self.logger.info(f"4b: cart badge={badge_count} ✅")
                                cart_added = True
                        except Exception:
                            pass

                    # 4c. "Please select" / "Select a size" error → size not registered → fail
                    if not cart_added:
                        try:
                            has_size_error = await self.page.evaluate("""() => {
                                const txt = (document.body.innerText || '').toLowerCase();
                                return /please select (a )?(size|color|colour|option)|select (a )?(size|color)|choose (a )?(size|color)/i.test(txt);
                            }""")
                            if has_size_error:
                                self.logger.warning("4c: 'Please select size' error detected — size not registered")
                                return Result(success=False, task=task, error="add_to_cart_size_not_selected")
                        except Exception:
                            pass

                    # 4d. Visible cart drawer / mini-cart (expanded selectors incl. Fashion Nova)
                    if not cart_added:
                        try:
                            drawer_ok = await self.page.evaluate("""() => {
                                const sels = [
                                    '#v3CartDrawer', '[id*="cartDrawer"]', '[id*="v3Cart"]',
                                    '[class*="cart-drawer"]', '[class*="CartDrawer"]',
                                    '[class*="minicart"]', '[class*="mini-cart"]',
                                    '[class*="cart-modal"]', '[id*="cart-modal"]',
                                    '[class*="side-cart"]', '[class*="slide-cart"]',
                                    '[class*="cart-popup"]', '[class*="cart-flyout"]',
                                ];
                                for (const s of sels) {
                                    const el = document.querySelector(s);
                                    if (!el) continue;
                                    const style = window.getComputedStyle(el);
                                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                                    const rect = el.getBoundingClientRect();
                                    if (rect.width < 10 || rect.height < 10) continue;
                                    // Drawer is open — check for checkout/view cart text or just has a product
                                    const txt = (el.textContent || '').toLowerCase();
                                    if (/view cart|go to cart|view bag|checkout|proceed|remove|quantity/i.test(txt)) return true;
                                }
                                return false;
                            }""")
                            if drawer_ok:
                                self.logger.info("4d: cart drawer open ✅")
                                cart_added = True
                        except Exception:
                            pass

                    # 4e. Shopify direct API fallback — read variant ID from page and POST /cart/add.js
                    if not cart_added:
                        try:
                            variant_id = await self.page.evaluate("""() => {
                                // Hidden form input (most reliable for Shopify)
                                const inp = document.querySelector('input[name="id"], input[name="variant_id"], [name="variant_id"]');
                                if (inp && inp.value) return inp.value;
                                // Shopify Analytics global
                                if (window.ShopifyAnalytics?.meta?.selectedVariantId)
                                    return String(window.ShopifyAnalytics.meta.selectedVariantId);
                                // Theme global variants object
                                if (window.meta?.product?.selected_variant?.id)
                                    return String(window.meta.product.selected_variant.id);
                                // data-variant-id on selected UI element
                                const sel = document.querySelector(
                                    '[aria-checked="true"][data-variant-id], [aria-selected="true"][data-variant-id], ' +
                                    '[data-selected="true"][data-variant-id], .selected[data-variant-id]'
                                );
                                if (sel) return sel.getAttribute('data-variant-id') || sel.getAttribute('data-id') || null;
                                return null;
                            }""")
                            if variant_id:
                                self.logger.info(f"4e: Found variant ID={variant_id}, posting to /cart/add.js")
                                add_result = await self.page.evaluate("""async (vid) => {
                                    try {
                                        const r = await fetch('/cart/add.js', {
                                            method: 'POST',
                                            headers: {'Content-Type': 'application/json'},
                                            credentials: 'include',
                                            body: JSON.stringify({id: parseInt(vid), quantity: 1})
                                        });
                                        const d = await r.json();
                                        return {ok: r.ok, status: r.status, id: d.id, title: d.title, error: d.description || d.message};
                                    } catch(e) { return {ok: false, error: String(e)}; }
                                }""", str(variant_id))
                                self.logger.info(f"4e: /cart/add.js response: {add_result}")
                                if add_result and add_result.get('ok') and add_result.get('id'):
                                    self.logger.info(f"4e: Direct Shopify add succeeded: {add_result.get('title')} ✅")
                                    cart_added = True
                            else:
                                self.logger.debug("4e: No variant ID found on page — skipping direct API fallback")
                        except Exception as _e4e:
                            self.logger.warning(f"4e: Direct API fallback error: {_e4e}")

                    if cart_added:
                        return Result(success=True, task=task, data={'method': 'dom_btn_click_validated', 'confidence': 0.95})

                    self.logger.warning("Post-click validation UNCERTAIN: could not confirm cart update. Continuing anyway.")
                    return Result(success=True, task=task, data={'method': 'dom_btn_click_unvalidated', 'confidence': 0.0})
                else:
                    self.logger.info(f"Selector {selector} not found or not visible")
            except Exception as e:
                self.logger.warning(f"Error while trying selector {selector}: {e}")
                continue
            
        # Tier 2: Search by Text (Bag/Cart)
        self.logger.info("Tier 2: Searching for buttons by text...")
        text_selectors = [
            "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'add to cart')]",
            "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'add to bag')]",
            "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'add to cart')]",
            "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'add to bag')]",
            "//li//button[contains(text(), 'ADD TO CART')]",
            "//button[contains(text(), 'ADD TO CART')]"
        ]
        
        for xpath in text_selectors:
            try:
                btn = await self.page.query_selector(f"xpath={xpath}")
                if btn and await btn.is_visible():
                    self.logger.info(f"Found button via text search: {xpath}")
                    await self.scroller.scroll_to_element(btn)
                    box = await btn.bounding_box()
                    if box:
                        center_x = box['x'] + box['width']/2
                        center_y = box['y'] + box['height']/2
                        await self._visualize_click(center_x, center_y)
                        await self.page.mouse.click(center_x, center_y)
                        await asyncio.sleep(2)
                        return Result(success=True, task=task, data={'method': 'text_coordinate_click'})
            except: continue
        
        # Tier 2.5: Use overlay system (same as variants)
        self.logger.info("Tier 2.5: Using overlay system to find Add to Cart...")
        try:
            overlay_js_path = self.js_assets_dir / 'overlay_search.js'
            if overlay_js_path.exists():
                overlay_js = overlay_js_path.read_text(encoding='utf-8')
                
                # Search for "ADD TO CART" or "Add to Cart"
                for search_text in ['ADD TO CART', 'Add to Cart', 'add to cart', 'GO TO CART', 'BUY NOW']:
                    self.logger.info(f"Overlay searching for: '{search_text}'")
                    result = await self.page.evaluate(overlay_js, {'val': search_text, 'containerSelector': None})
                    
                    self.logger.info(f"Overlay result for '{search_text}': {result}")
                    
                    if result.get('found'):
                        element_index = result['elementIndex']
                        self.logger.info(f"✅ Overlay found Add to Cart button at element #{element_index}")
                        
                        coords = await self.page.evaluate("""
                        (index) => {
                            const overlay = document.querySelector(`[data-element-index="${index}"]`);
                            if (!overlay) return null;
                            const rect = overlay.getBoundingClientRect();
                            return {
                                x: Math.round(rect.left + rect.width / 2),
                                y: Math.round(rect.top + rect.height / 2)
                            };
                        }
                        """, element_index)
                        
                        if coords:
                            self.logger.info(f"Clicking Add to Cart at ({coords['x']}, {coords['y']})")
                            await self._visualize_click(coords['x'], coords['y'])
                            await self.page.mouse.click(coords['x'], coords['y'])
                            
                            # Clean up overlays
                            await self.page.evaluate("document.querySelectorAll('.automation-overlay').forEach(el => el.remove())")
                            
                            await asyncio.sleep(3)
                            return Result(success=True, task=task, data={'method': 'overlay_add_to_cart'})
                        else:
                            self.logger.warning(f"Could not get coordinates for element #{element_index}")
                
                # Clean up overlays
                await self.page.evaluate("document.querySelectorAll('.automation-overlay').forEach(el => el.remove())")
                self.logger.warning("Overlay system did not find Add to Cart button")
            else:
                self.logger.warning(f"overlay_search.js not found at {overlay_js_path}")
        except Exception as e:
            self.logger.error(f"Overlay Add to Cart failed: {e}", exc_info=True)
        
        # Tier 2.6: Simple JS text search and click
        self.logger.info("Tier 2.6: Simple JS text search for Add to Cart...")
        try:
            js_result = await self.page.evaluate("""
            () => {
                // Priority order: most specific first
                const searchTerms = [
                    'add to cart', 'add to bag', 'add to basket',
                    'buy now', 'buy it now',
                    'select a size',   // Allbirds shows this when size not selected
                    'choose a size',
                    'go to cart',
                ];
                const vph = window.innerHeight;
                const allButtons = document.querySelectorAll('button, input[type="submit"]');

                const buttonTexts = [];
                for (const btn of allButtons) {
                    const text = (btn.textContent || btn.value || '').trim();
                    if (text && text.length > 2 && text.length < 100) buttonTexts.push(text.substring(0, 50));
                }

                for (const term of searchTerms) {
                    for (const btn of allButtons) {
                        const text = (btn.textContent || btn.value || '').toLowerCase().trim();
                        if (!text.includes(term)) continue;
                        if (text.length < 3) continue;  // skip badge counters like '0', '1'

                        // Check not in header/nav (top 120px of page)
                        const rect = btn.getBoundingClientRect();
                        if (rect.width < 10 || rect.height < 10) continue;
                        if (rect.y < 0 || rect.y > vph) continue;
                        if (rect.y < 120 && rect.height < 50) continue; // probably a header icon

                        const style = window.getComputedStyle(btn);
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
                        if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') continue;

                        return {
                            found: true,
                            text: text.substring(0, 50),
                            x: Math.round(rect.left + rect.width / 2),
                            y: Math.round(rect.top + rect.height / 2)
                        };
                    }
                }
                return { found: false, debug_buttons: buttonTexts.slice(0, 20) };
            }
            """)
            
            if js_result.get('found'):
                self.logger.info(f"✅ JS search found button: '{js_result['text']}' at ({js_result['x']}, {js_result['y']})")
                await self._visualize_click(js_result['x'], js_result['y'])
                await self.page.mouse.click(js_result['x'], js_result['y'])
                await asyncio.sleep(3)
                return Result(success=True, task=task, data={'method': 'js_text_search'})
            else:
                self.logger.warning("JS text search did not find Add to Cart button")
                if 'debug_buttons' in js_result:
                    self.logger.info(f"Sample button texts on page: {js_result['debug_buttons'][:10]}")
        except Exception as e:
            self.logger.error(f"JS text search failed: {e}", exc_info=True)
            
        # Tier 3.5: VLM Fallback
        self.logger.info("DOM Add to Cart failed, invoking VLM...")
        
        # We can reuse _vlm_selection generically or create a prompt here
        vlm_res = await self._vlm_selection("button", "Add to Cart")
        
        if vlm_res['success']:
            return Result(success=True, task=task, data={'method': 'vlm_click'})
            
        return Result(success=False, task=task, error="Could not click Add to Cart")

    async def _close_overlays(self):
        """
        Use the unified DistractionManager to clear pop-ups and modals.
        """
        await self.distraction_manager.clear_distractions()

    async def _cleanup_candidate_overlays(self):
        """Remove candidate overlays from page"""
        try:
            await self.page.evaluate("""() => {
                const el = document.getElementById('automation-variant-candidates-container');
                if (el) el.remove();
            }""")
        except Exception:
            pass

    async def _try_reveal_variants(self, variant_type: str) -> bool:
        """
        Attempts to find and click a reveal button (e.g. 'ADD', 'Select Size', 'Choose Options')
        to reveal hidden variant options. Uses robust JS-based matching.
        """
        self.logger.info(f"🔍 Variant search returned zero candidates. Attempting to click reveal button for {variant_type}...")
        
        v_lower = variant_type.lower()
        
        js_code = """
        (variantType) => {
            const vLower = variantType.toLowerCase();
            const isColor = vLower === 'color';
            
            // Gather elements that could act as a button or option reveal trigger
            const elements = document.querySelectorAll('button, a, [role="button"], div, span, li');
            const candidates = [];
            
            const isInsideExcludedSection = (el) => {
                let parent = el.parentElement;
                while (parent && parent.tagName !== 'BODY') {
                    const id = (parent.id || '').toLowerCase();
                    const className = (parent.className || '').toLowerCase();
                    const combined = id + ' ' + className;
                    
                    if (combined.includes('recommend') || 
                        combined.includes('similar') || 
                        combined.includes('carousel') || 
                        combined.includes('related') || 
                        combined.includes('frequent') || 
                        combined.includes('upsell') || 
                        combined.includes('suggestion') ||
                        combined.includes('may-like')) {
                        return true;
                    }
                    parent = parent.parentElement;
                }
                return false;
            };
            
            for (const el of elements) {
                // Check visibility and size limits
                const rect = el.getBoundingClientRect();
                if (rect.width < 12 || rect.height < 12) continue;
                if (rect.width > 500 || rect.height > 150) continue;
                if (rect.top < 100) continue; // Skip header elements
                
                // Exclude elements inside recommendation sections
                if (isInsideExcludedSection(el)) continue;
                
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
                
                // Get clean text or attribute value
                let text = (el.textContent || '').trim().toLowerCase();
                if (!text) {
                    text = (el.getAttribute('aria-label') || '').trim().toLowerCase();
                }
                if (!text) {
                    text = (el.getAttribute('title') || '').trim().toLowerCase();
                }
                if (!text) continue;
                
                // Keep length reasonable
                if (text.length > 50) continue;
                
                let score = 0;
                
                // Color task restriction: Do NOT click generic "ADD" triggers for color task
                if (isColor && (text === 'add' || text.startsWith('add to') || text.includes('bag') || text.includes('cart'))) {
                    continue;
                }
                
                if (vLower === 'size') {
                    if (text === 'add' || text === 'add to bag' || text === 'add to cart' || text === 'add to basket') {
                        score += 100;
                    } else if (text.startsWith('add to') || text.startsWith('add ')) {
                        score += 85;
                    } else if (text.includes('select size') || text.includes('choose size') || text.includes('select a size')) {
                        score += 90;
                    } else if (text === 'size' || text === 'sizes' || text.includes('size-swatch') || text.includes('size-option')) {
                        score += 70;
                    } else if (text === 'select' || text === 'choose') {
                        score += 50;
                    }
                } else if (vLower === 'color') {
                    if (text.includes('select color') || text.includes('choose color') || text.includes('select shade')) {
                        score += 90;
                    } else if (text === 'color' || text === 'colour' || text === 'colors') {
                        score += 70;
                    }
                } else {
                    // Other variant types
                    if (text.includes(`select ${vLower}`) || text.includes(`choose ${vLower}`)) {
                        score += 90;
                    } else if (text === vLower || text.includes(vLower)) {
                        score += 70;
                    }
                }
                
                if (score > 0) {
                    const className = el.className || '';
                    if (className.toLowerCase().includes('btn') || className.toLowerCase().includes('button')) {
                        score += 10;
                    }
                    if (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button') {
                        score += 10;
                    }
                    
                    candidates.push({
                        id: el.id || '',
                        tagName: el.tagName,
                        text: text,
                        score: score,
                        rect: {
                            left: rect.left,
                            top: rect.top,
                            width: rect.width,
                            height: rect.height
                        }
                    });
                }
            }
            
            if (candidates.length === 0) return null;
            
            // Sort by score descending, then by Y coordinate ascending (prefer higher up on page)
            candidates.sort((a, b) => {
                if (b.score !== a.score) return b.score - a.score;
                return a.rect.top - b.rect.top;
            });
            
            // Assign temporary ID if needed
            const best = candidates[0];
            let targetId = best.id;
            if (!targetId) {
                // Find the index of matching element in document
                const matchedEl = Array.from(elements).find(currEl => {
                    const r = currEl.getBoundingClientRect();
                    return currEl.tagName === best.tagName && 
                           Math.abs(r.top - best.rect.top) < 2 && 
                           Math.abs(r.left - best.rect.left) < 2;
                });
                if (matchedEl) {
                    if (!matchedEl.id) {
                        matchedEl.id = 'automation-temp-reveal-btn-' + Math.random().toString(36).substr(2, 9);
                    }
                    targetId = matchedEl.id;
                }
            }
            
            return {
                found: true,
                id: targetId,
                text: best.text,
                score: best.score,
                x: Math.round(best.rect.left + best.rect.width / 2),
                y: Math.round(best.rect.top + best.rect.height / 2),
                top: Math.round(best.rect.top)
            };
        }
        """
        
        try:
            result = await self.page.evaluate(js_code, v_lower)
            if result and result.get('found'):
                self.logger.info(f"👉 JS reveal found candidate: '{result['text']}' (score: {result['score']}) at ({result['x']}, {result['y']})")
                
                # Retrieve the element handle if ID is available
                elem = None
                if result.get('id'):
                    elem = await self.page.query_selector(f"#{result['id']}")
                
                # Perform the click
                if elem:
                    try:
                        await elem.scroll_into_view_if_needed()
                        await asyncio.sleep(0.3)
                        box = await elem.bounding_box()
                        if box:
                            result['x'] = box['x'] + box['width'] / 2
                            result['y'] = box['y'] + box['height'] / 2
                        await self._visualize_click(result['x'], result['y'])
                        await elem.click(timeout=3000)
                    except Exception as click_err:
                        self.logger.warning(f"Direct click on reveal element failed: {click_err}. Trying coordinate click...")
                        await self._visualize_click(result['x'], result['y'])
                        await self.page.mouse.click(result['x'], result['y'])
                else:
                    # Scroll to coordinate only if element is outside viewport
                    current_scroll = await self.page.evaluate("window.scrollY")
                    viewport_h = self.page.viewport_size['height'] if self.page.viewport_size else 720
                    already_visible = 0 <= result['y'] <= viewport_h

                    if not already_visible:
                        abs_y = current_scroll + result['y']
                        target_scroll = max(0, abs_y - (viewport_h / 2))
                        await self.page.evaluate(f"window.scrollTo({{top: {target_scroll}, behavior: 'smooth'}})")
                        await asyncio.sleep(0.5)
                        new_scroll = await self.page.evaluate("window.scrollY")
                        relative_y = abs_y - new_scroll
                    else:
                        relative_y = result['y']

                    await self._visualize_click(result['x'], relative_y)
                    await self.page.mouse.click(result['x'], relative_y)
                
                return True
        except Exception as e:
            self.logger.warning(f"Error executing robust JS reveal detection: {e}", exc_info=True)

    async def _sequential_text_change_selection(self, variant_type: str, variant_value: str) -> Dict[str, Any]:
        """
        Tier 0: Find all candidates of variant_type, overlay them, click sequentially,
        and validate text change on screen against variant_value.
        """
        self.logger.info(f"🔄 Starting Tier 0 Sequential Click & Text Change Validation for {variant_type}={variant_value}")

        try:
            # 0. Check if the target variant is ALREADY selected — skip clicking if so.
            # STRICT: only trust DOM-state signals (aria-checked, aria-selected, .selected
            # class, border detection).  Text-on-page patterns (inline_mashed, label_inline,
            # label_pattern, url_slug) fire on product descriptions before any click and
            # cause false positives — we ignore them here.
            _DOM_STATE_METHODS = {
                'simple_page_check_border_detection',
                'simple_page_check_border_image_detection',
                'simple_page_check_aria_selected',   # future-proof
                'dropdown selected option',
                'cart_count_increase',
                'cart_drawer_visible',
            }
            pre_check = await self.validator.validate_any_selection(variant_type, variant_value)
            if pre_check.get('validated'):
                pre_method = pre_check.get('method', '')
                if pre_method in _DOM_STATE_METHODS:
                    selected_text = pre_check.get('selected_text', '')
                    pre_score = self._variant_match_score(variant_value, selected_text, variant_type)
                    if pre_score >= 60:
                        self.logger.info(f"✅ '{variant_type}={variant_value}' already selected (pre-check/{pre_method}). Skipping click.")
                        return {
                            'success': True,
                            'method': 'already_selected',
                            'confidence': pre_check.get('confidence', 0.95),
                            'reasoning': f"Pre-click DOM check confirmed '{selected_text}' already matches '{variant_value}'",
                            'candidate_index': -1
                        }
                else:
                    self.logger.debug(f"Pre-check method '{pre_method}' is text-based — proceeding with click to confirm.")

            # 1. Load and execute variant_overlay_handler.js
            handler_js_path = self.js_assets_dir / 'variant_overlay_handler.js'
            if not handler_js_path.exists():
                self.logger.warning(f"variant_overlay_handler.js not found at {handler_js_path}")
                return {'success': False, 'error': 'variant_overlay_handler.js not found'}
                
            handler_js = handler_js_path.read_text(encoding='utf-8')
            res = await self.page.evaluate(handler_js, {'variantType': variant_type})
            
            # If no candidates are found, or all candidates have a match score of 0
            # (which means none of them have any textual similarity to our target value)
            candidates = res.get('candidates') if res else None
            has_no_good_candidates = True
            
            def calculate_scores(cands):
                nonlocal has_no_good_candidates
                for candidate in cands:
                    score = self._variant_match_score(
                        variant_value, candidate['text'], variant_type
                    )
                    candidate['match_score'] = score
                    if score >= 30:   # Only count as a good candidate if meaningful match
                        has_no_good_candidates = False

            if res and res.get('success') and candidates:
                calculate_scores(candidates)
                
            if not res or not res.get('success') or not candidates or has_no_good_candidates:
                if has_no_good_candidates and candidates:
                    self.logger.warning("Candidates found but all have 0 match score. They are likely irrelevant page elements. Attempting to click reveal button...")
                else:
                    self.logger.warning("No candidate elements found by variant_overlay_handler.js initially.")
                
                # Try to click a reveal button to open size selector dropdown/drawer
                revealed = await self._try_reveal_variants(variant_type)
                if revealed:
                    self.logger.info("Reveal button clicked. Waiting 1.5 seconds and scanning again...")
                    await asyncio.sleep(1.5)
                    # Re-scan candidates
                    res = await self.page.evaluate(handler_js, {'variantType': variant_type})
                    if res and res.get('success') and res.get('candidates'):
                        candidates = res['candidates']
                        has_no_good_candidates = True
                        calculate_scores(candidates)
            
            if not res or not res.get('success') or not candidates:
                self.logger.warning("No candidate elements found by variant_overlay_handler.js after reveal check.")
                return {'success': False, 'error': 'No candidates found'}
                
            self.logger.info(f"Found {len(candidates)} candidates for variant type '{variant_type}'")
            
            # Sort by match score (highest first), then by original index
            candidates.sort(key=lambda c: (-c.get('match_score', 0), c['index']))
            
            self.logger.info(f"Sorted candidates by match score for '{variant_value}':")
            for i, c in enumerate(candidates[:5]):
                self.logger.info(f"  {i+1}. [{c['index']+1:02d}] '{c['text']}' (score: {c.get('match_score', 0)})")
            
            # Take a debug screenshot showing all candidates
            debug_screenshot_path = f"overlay_candidates_{variant_type}.png"
            try:
                await self.page.screenshot(path=debug_screenshot_path)
                self.logger.info(f"Screenshot of candidates saved to {debug_screenshot_path}")
            except Exception as se:
                self.logger.warning(f"Could not take candidates screenshot: {se}")
                
            # Iterate through candidates (now sorted by match score)
            for idx, candidate in enumerate(candidates):
                self.logger.info(f"👉 Testing candidate #{candidate['index'] + 1}/{len(candidates)}: {candidate['tagName']} (text: '{candidate['text']}', match_score: {candidate.get('match_score', 0)})")
                
                # Hard stop: never click a candidate with no meaningful match
                if candidate.get('match_score', 0) < 30:
                    self.logger.info(
                        f"⛔ Skipping candidate #{candidate['index'] + 1} "
                        f"(text='{candidate['text']}', score={candidate.get('match_score',0)}) "
                        f"— below match threshold for target '{variant_value}'."
                    )
                    continue
                
                # Scroll element into viewport only if not already visible
                try:
                    viewport_h = self.page.viewport_size['height'] if self.page.viewport_size else 720
                    current_scroll = await self.page.evaluate("window.scrollY")
                    elem_top = candidate['absY']
                    elem_bottom = candidate['absY'] + candidate.get('height', 30)
                    already_visible = (elem_top >= current_scroll) and (elem_bottom <= current_scroll + viewport_h)

                    if not already_visible:
                        target_scroll = max(0, candidate['absY'] - (viewport_h / 2))
                        await self.page.evaluate(f"window.scrollTo({{top: {target_scroll}, behavior: 'smooth'}})")
                        await asyncio.sleep(0.5)

                    # Calculate relative coordinates after potential scroll
                    new_scroll = await self.page.evaluate("window.scrollY")
                    relative_y = candidate['absY'] - new_scroll
                    relative_x = candidate['absX'] # X generally doesn't change with vertical scroll
                    
                    # Update overlay visual to highlight currently selected candidate
                    await self.page.evaluate(f"""
                        (index) => {{
                            const overlay = document.querySelector(`.automation-variant-candidate[data-candidate-index="${{index}}"]`);
                            if (overlay) {{
                                overlay.style.borderColor = '#00e676';
                                overlay.style.backgroundColor = 'rgba(0, 230, 118, 0.25)';
                                overlay.style.boxShadow = '0 0 15px #00e676';
                            }}
                        }}
                    """, candidate['index'])
                    
                    # Attempt direct click first
                    clicked = False
                    try:
                        elem = await self.page.query_selector(f'[data-automation-variant-target="{candidate["index"]}"]')
                        if elem and await elem.is_visible():
                            self.logger.info(f"Clicking candidate #{candidate['index'] + 1} directly using element.click()")
                            await elem.scroll_into_view_if_needed()
                            await asyncio.sleep(0.2)
                            box = await elem.bounding_box()
                            if box:
                                relative_x = box['x'] + box['width'] / 2
                                relative_y = box['y'] + box['height'] / 2
                            await self._visualize_click(relative_x, relative_y)
                            await elem.click(timeout=3000)
                            clicked = True
                    except Exception as click_err:
                        self.logger.warning(f"Direct click on candidate #{candidate['index'] + 1} failed: {click_err}. Falling back to coordinate click.")
                    
                    if not clicked:
                        # Visualize click and perform mouse click
                        await self._visualize_click(relative_x, relative_y)
                        await self.page.mouse.click(relative_x, relative_y)
                    
                    # Wait for UI to update — color swatches on some sites (e.g. Allbirds)
                    # trigger a full page navigation; wait for networkidle before validating.
                    url_before_click = self.page.url
                    await asyncio.sleep(1.2)
                    try:
                        await self.page.wait_for_load_state("networkidle", timeout=4000)
                    except Exception:
                        pass
                    # If page navigated, give extra settle time
                    if self.page.url != url_before_click:
                        self.logger.info(f"Page navigated after click: {url_before_click} → {self.page.url}")
                        await asyncio.sleep(0.8)

                    # Check validation - MUST match the target value, not just any selection
                    validation = await self.validator.validate_any_selection(variant_type, variant_value)
                    
                    if validation.get('validated'):
                        # Post-click validation: must be a meaningful synonym match
                        selected_text = validation.get('selected_text', '')
                        post_score = self._variant_match_score(variant_value, selected_text, variant_type)
                        if post_score >= 60:   # Synonym-aware — XL must match X-Large etc.
                            self.logger.info(f"✅ SUCCESS: Candidate #{idx + 1} ('{candidate['text']}') matched target '{variant_value}'! Validated as: '{selected_text}' (score={post_score}). Method: {validation.get('method')}")
                            
                            # Mark selected overlay as verified (green check)
                            await self.page.evaluate(f"""
                                (index) => {{
                                    const overlay = document.querySelector(`.automation-variant-candidate[data-candidate-index="${{index}}"]`);
                                    if (overlay) {{
                                        overlay.style.borderColor = '#00e676';
                                        overlay.style.backgroundColor = 'rgba(0, 230, 118, 0.1)';
                                        overlay.textContent = '✓';
                                    }}
                                }}
                            """, candidate['index'])
                            
                            # Wait a moment to show success indicator
                            await asyncio.sleep(0.05)
                            
                            # Clean up candidates container
                            await self._cleanup_candidate_overlays()
                            
                            return {
                                'success': True,
                                'method': 'sequential_click_text_change',
                                'confidence': validation.get('confidence', 0.95),
                                'reasoning': f"Clicked candidate #{idx + 1} and validated via '{validation.get('method')}'",
                                'candidate_index': idx
                            }
                        else:
                            self.logger.warning(
                                f"❌ Candidate #{idx + 1} ('{candidate['text']}') post-score too low "
                                f"(score={post_score} < 60, selected_text='{selected_text}'). "
                                f"Trying next candidate..."
                            )
                    else:
                        self.logger.warning(
                            f"❌ Candidate #{idx + 1} ('{candidate['text']}') failed validation — "
                            f"page does not show '{variant_value}' as selected. Trying next candidate..."
                        )
                        
                        # Mark rejected overlay as failed (red cross / grey)
                        await self.page.evaluate(f"""
                            (index) => {{
                                const overlay = document.querySelector(`.automation-variant-candidate[data-candidate-index="${{index}}"]`);
                                if (overlay) {{
                                    overlay.style.borderColor = '#ff1744';
                                    overlay.style.backgroundColor = 'rgba(255, 23, 68, 0.1)';
                                    overlay.style.boxShadow = 'none';
                                    overlay.textContent = '✗';
                                }}
                            }}
                        """, candidate['index'])
                        
                except Exception as click_err:
                    self.logger.error(f"Error clicking candidate #{idx + 1}: {click_err}")
            
            # Clean up if none succeeded
            await self._cleanup_candidate_overlays()
            return {'success': False, 'error': 'All candidates failed validation'}
            
        except Exception as e:
            self.logger.error(f"Sequential selection error: {e}", exc_info=True)
            await self._cleanup_candidate_overlays()
            return {'success': False, 'error': str(e)}

    async def _overlay_based_selection(self, variant_type: str, variant_value: str) -> Dict[str, Any]:
        """
        Use overlay system to find and click elements
        1. Load overlay_search.js
        2. Execute it to create numbered boxes on all clickable elements
        3. Find matching element by text/DOM
        4. Click on the coordinate of the matched element
        """
        self.logger.info(f"🔍 Overlay detection for {variant_type}={variant_value}")
        
        try:
            # Load overlay search JavaScript
            overlay_js_path = self.js_assets_dir / 'overlay_search.js'
            if not overlay_js_path.exists():
                return {'success': False, 'error': 'overlay_search.js not found'}
            
            overlay_js = overlay_js_path.read_text(encoding='utf-8')
            
            # First, detect the main product container to scope the search
            container_selector = None
            try:
                # Try to find main product container
                container_result = await self.page.evaluate("""
                () => {
                    const selectors = [
                        '[class*="product-detail"]',
                        '[class*="product-info"]',
                        '[id*="product-detail"]',
                        'main [class*="product"]'
                    ];
                    
                    for (const sel of selectors) {
                        const elem = document.querySelector(sel);
                        if (elem) {
                            // Return a unique selector
                            if (elem.id) return '#' + elem.id;
                            if (elem.className) {
                                const classes = elem.className.split(' ').filter(c => c.length > 0);
                                if (classes.length > 0) return '.' + classes[0];
                            }
                        }
                    }
                    return null;
                }
                """)
                container_selector = container_result
            except:
                pass
            
            # Execute overlay search
            result = await self.page.evaluate(
                overlay_js,
                {'val': variant_value, 'containerSelector': container_selector}
            )
            
            self.logger.info(f"Overlay result: {result}")
            
            # Take a screenshot to see the overlays
            try:
                await self.page.screenshot(path='temp_overlay_debug.png')
                self.logger.info("Overlay screenshot saved to temp_overlay_debug.png")
            except:
                pass
            
            # Check if overlays were created
            overlay_count = await self.page.evaluate("""
            () => {
                const overlays = document.querySelectorAll('.automation-overlay');
                return overlays.length;
            }
            """)
            self.logger.info(f"Total overlays created: {overlay_count}")
            
            # Debug: Get sample of what text the overlays found
            if overlay_count > 0:
                sample_texts = await self.page.evaluate("""
                () => {
                    const overlays = Array.from(document.querySelectorAll('.automation-overlay'));
                    return overlays.slice(0, 20).map((o, i) => {
                        const index = o.getAttribute('data-element-index');
                        const text = o.textContent || '';
                        return `[${index}]: ${text}`;
                    });
                }
                """)
                self.logger.info(f"Sample overlay texts: {sample_texts}")
            
            # Wait 0.05 seconds so we can see the overlays in the browser
            if overlay_count > 0:
                self.logger.info("Waiting 0.05 seconds to display overlays...")
                await asyncio.sleep(0.05)
            
            # Log what elements were detected on the page
            detected_info = await self.page.evaluate("""
            (searchVal) => {
                const allButtonsWithText = [];
                document.querySelectorAll('button').forEach((btn, idx) => {
                    const text = (btn.textContent || '').trim();
                    const ariaLabel = btn.getAttribute('aria-label') || '';
                    const title = btn.getAttribute('title') || '';
                    const combined = [text, ariaLabel, title].join(' | ');
                    if (combined && combined.length > 0) {
                        allButtonsWithText.push({
                            index: idx,
                            text: text.substring(0, 50),
                            ariaLabel: ariaLabel.substring(0, 50),
                            title: title.substring(0, 50),
                            visible: btn.offsetHeight > 0 && btn.offsetWidth > 0,
                            classes: btn.className.substring(0, 100)
                        });
                    }
                });
                return {
                    totalButtons: document.querySelectorAll('button').length,
                    buttonsWithText: allButtonsWithText.slice(0, 20)
                };
            }
            """, variant_value)

            self.logger.debug(f"Detected elements info: {json.dumps(detected_info, indent=2)}")
            safe_val = variant_value.replace('/', '_').replace('\\', '_').replace(':', '_')
            debug_screenshot = f"overlay_debug_{variant_type}_{safe_val}.png"
            await self.page.screenshot(path=debug_screenshot)
            self.logger.info(f"📸 Overlay visualization saved to: {debug_screenshot}")

            # Wait so overlay is visible on screen for debugging
            await asyncio.sleep(0.05)

            if result.get('found'):
                element_index = result['elementIndex']
                self.logger.info(f"Found element #{element_index} matching '{variant_value}'")

                # Get the element's coordinates from the overlay
                coords = await self.page.evaluate("""
                (index) => {
                    const overlay = document.querySelector(`[data-element-index="${index}"]`);
                    if (!overlay) return null;

                    const rect = overlay.getBoundingClientRect();
                    return {
                        x: Math.round(rect.left + rect.width / 2),
                        y: Math.round(rect.top + rect.height / 2)
                    };
                }
                """, element_index)

                if coords:
                    self.logger.info(f"Clicking overlay element at ({coords['x']}, {coords['y']})")

                    # Visualize and click
                    await self._visualize_click(coords['x'], coords['y'])
                    await self.page.mouse.click(coords['x'], coords['y'])

                    # Wait and validate
                    await asyncio.sleep(1.5)
                    validation = await self.validator.validate_any_selection(variant_type, variant_value)

                    # Clean up overlays
                    await self.page.evaluate("document.querySelectorAll('.automation-overlay').forEach(el => el.remove())")

                    if validation['validated']:
                        return {
                            'success': True,
                            'method': 'overlay_validated',
                            'confidence': validation.get('confidence', 0.95),
                            'reasoning': f"Found via overlay system element #{element_index}"
                        }
                    else:
                        self.logger.warning(f"Overlay click succeeded but validation failed")
                        return {'success': False, 'error': 'Validation failed after overlay click'}
                else:
                    return {'success': False, 'error': 'Could not get coordinates for overlay element'}
            else:
                self.logger.info(f"Overlay search did not find '{variant_value}'")
                # Clean up overlays
                await self.page.evaluate("document.querySelectorAll('.automation-overlay').forEach(el => el.remove())")
                return {'success': False, 'error': 'overlay_not_found'}
        except Exception as e:
            self.logger.error(f"Overlay-based selection error: {e}")
            # Clean up overlays on error
            try:
                await self.page.evaluate("document.querySelectorAll('.automation-overlay').forEach(el => el.remove())")
            except Exception:
                pass
            return {'success': False, 'error': str(e)}

    def _extract_json(self, text: str) -> dict:
        """Extract JSON from text response"""
        import re
        import json
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(1))
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
        return {}
