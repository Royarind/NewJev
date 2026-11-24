#!/usr/bin/env python3
"""
Click Coordinator
Validates and adjusts click coordinates to ensure they're within viewport bounds
"""

import asyncio
import logging
from typing import Dict, Tuple, Optional

logger = logging.getLogger(__name__)


class ClickCoordinator:
    """
    Ensures click coordinates are valid and within viewport.
    Handles negative coordinates, off-screen elements, etc.
    """
    
    def __init__(self, page):
        self.page = page
        self.logger = logging.getLogger(__name__)
    
    async def get_safe_click_coordinates(self, element) -> Optional[Tuple[float, float]]:
        """
        Get safe click coordinates for an element.
        Returns (x, y) if valid, None if element is not clickable.
        """
        try:
            # Get viewport size
            viewport = self.page.viewport_size or {'width': 1280, 'height': 800}
            
            # Get bounding box of element
            box = await element.bounding_box()
            if not box:
                self.logger.warning("Element has no bounding box")
                return None
            
            # Calculate center coordinates
            center_x = box['x'] + box['width'] / 2
            center_y = box['y'] + box['height'] / 2
            
            # Check if coordinates are within viewport
            if center_x < 0 or center_x > viewport['width']:
                self.logger.warning(f"X coordinate {center_x} out of bounds [0, {viewport['width']}]")
                return None
            
            if center_y < 0 or center_y > viewport['height']:
                self.logger.warning(f"Y coordinate {center_y} out of bounds [0, {viewport['height']}]")
                # Try to scroll element into view
                await self._smart_scroll_to_element(element, viewport)
                # Try again after scroll
                box = await element.bounding_box()
                if box:
                    center_x = box['x'] + box['width'] / 2
                    center_y = box['y'] + box['height'] / 2
                    
                    if 0 <= center_x <= viewport['width'] and 0 <= center_y <= viewport['height']:
                        self.logger.info(f"Element scrolled into view. Safe coordinates: ({center_x}, {center_y})")
                        return (center_x, center_y)
            
            # Clamp to viewport bounds just in case
            safe_x = max(0, min(center_x, viewport['width']))
            safe_y = max(0, min(center_y, viewport['height']))
            
            self.logger.info(f"Safe click coordinates: ({safe_x}, {safe_y})")
            return (safe_x, safe_y)
        
        except Exception as e:
            self.logger.error(f"Failed to get safe coordinates: {e}")
            return None
    
    async def _smart_scroll_to_element(self, element, viewport: Dict) -> bool:
        """
        Intelligently scroll element into viewport center.
        """
        try:
            current_scroll = await self.page.evaluate("window.scrollY")
            box = await element.bounding_box()
            
            if not box:
                return False
            
            # Calculate absolute position
            abs_y = current_scroll + box['y']
            
            # Target: center element in viewport
            viewport_h = viewport.get('height', 800)
            target_scroll = max(0, abs_y - (viewport_h / 2))
            
            self.logger.info(f"Scrolling from {current_scroll} to {target_scroll}")
            
            await self.page.evaluate(f"""
            window.scrollTo({{
                top: {target_scroll},
                left: 0,
                behavior: 'smooth'
            }})
            """)
            
            # Wait for scroll to complete
            await asyncio.sleep(0.8)
            
            return True
        
        except Exception as e:
            self.logger.error(f"Smart scroll failed: {e}")
            return False
    
    async def validate_and_click(self, element, description: str = "") -> bool:
        """
        Validate element is clickable and click it with safe coordinates.
        """
        try:
            # Check visibility
            if not await element.is_visible():
                self.logger.warning(f"Element not visible: {description}")
                return False
            
            # Get safe coordinates
            coords = await self.get_safe_click_coordinates(element)
            if not coords:
                self.logger.error(f"Could not get safe coordinates for: {description}")
                return False
            
            x, y = coords
            
            # Visualize before click (optional)
            await self._visualize_click(x, y)
            
            # Click at safe coordinates
            await self.page.mouse.click(x, y)
            self.logger.info(f"✓ Clicked {description} at ({x}, {y})")
            
            return True
        
        except Exception as e:
            self.logger.error(f"Click failed for {description}: {e}")
            return False
    
    async def _visualize_click(self, x: float, y: float):
        """Show blue circle at click location"""
        try:
            from pathlib import Path
            js_file = Path(__file__).resolve().parents[1] / 'dom_assets' / 'visualize_click.js'
            
            if js_file.exists():
                js_code = js_file.read_text()
                await self.page.evaluate(js_code, {'x': x, 'y': y})
        except:
            pass  # Visualization is optional
