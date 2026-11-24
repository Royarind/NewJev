import asyncio
import logging
import math
import random

logger = logging.getLogger(__name__)

class SmartScroller:
    """
    Utility to smartly scroll elements into view directly to the center of the viewport
    using human-like smooth scrolling.
    """
    
    def __init__(self, page):
        self.page = page
        
    async def scroll_to_element(self, element, center: bool = True):
        """
        Scrolls the element into view, preferably centering it.
        Uses smooth scrolling.
        """
        if not element:
            return
            
        try:
            # Check if element is already in viewport
            is_visible = await element.is_visible()
            box = await element.bounding_box()
            
            if is_visible and box:
                # Check if it's centrally located (roughly)
                viewport = self.page.viewport_size
                if viewport:
                    vh = viewport['height']
                    # If element is within the middle 60% of the screen, don't scroll
                    if vh * 0.2 < box['y'] < vh * 0.8:
                        logger.debug("Element already in good viewport position.")
                        return

            await self._smooth_scroll_to_element(element)
            logger.info(f"Performing smart scroll to center element...")
            
            # Wait for any lazy-loaded content or floating headers to settle
            await asyncio.sleep(0.5)
            
        except Exception as e:
            logger.warning(f"Smart scroll failed: {e}")
            # Fallback to standard scrollIntoView if fancy scroll fails
            try:
                await element.scroll_into_view_if_needed()
            except: pass

    async def _smooth_scroll_to_element(self, element):
        """
        Performs a human-like smooth scroll to the element.
        """
        # Get target position
        box = await element.bounding_box()
        if not box:
            await element.scroll_into_view_if_needed()
            return

        target_y = box['y'] + box['height'] / 2
        
        # Calculate scroll amount to center the element
        viewport = self.page.viewport_size
        if not viewport:
            await element.scroll_into_view_if_needed()
            return
            
        vh = viewport['height']
        current_scroll = await self.page.evaluate("window.scrollY")
        
        # Target scroll position (window.scrollY + element.y - height/2)
        # Note: box['y'] is relative to the viewport, so we need absolute page coordinates
        absolute_element_y = current_scroll + box['y']
        target_scroll_y = max(0, absolute_element_y - (vh / 2))
        
        # Determine scroll steps
        distance = target_scroll_y - current_scroll
        steps = 10  # Number of steps
        
        if abs(distance) < 50: # If very close, just snap
            await self.page.evaluate(f"window.scrollTo(0, {target_scroll_y})")
            return

        # Perform smooth scroll with variable steps
        for i in range(steps):
            progress = (i + 1) / steps
            # Ease-out function for smoother feel
            ease = 1 - math.pow(1 - progress, 3) 
            
            next_pos = current_scroll + (distance * ease)
            await self.page.evaluate(f"window.scrollTo(0, {next_pos})")
            
            # Random tiny delay between frames for "human" feel
            await asyncio.sleep(random.uniform(0.01, 0.03)) 
