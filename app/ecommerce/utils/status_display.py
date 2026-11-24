"""
Status Display Manager
Shows onscreen messages during automation for better visibility
"""
import logging
from pathlib import Path
from typing import Optional

class StatusDisplay:
    """Displays automation status messages onscreen"""
    
    def __init__(self, page):
        self.page = page
        self.logger = logging.getLogger(__name__)
        self.js_code = None
        self._load_js()
        
    def _load_js(self):
        """Load the status banner JavaScript"""
        try:
            js_path = Path(__file__).resolve().parents[1] / 'dom_assets' / 'status_banner.js'
            if js_path.exists():
                self.js_code = js_path.read_text(encoding='utf-8')
            else:
                self.logger.warning(f"Status banner JS not found at {js_path}")
        except Exception as e:
            self.logger.error(f"Failed to load status banner JS: {e}")
    
    async def show(self, message: str, type: str = 'info', duration: int = 3000):
        """
        Display a status message onscreen
        
        Args:
            message: The message to display
            type: 'info', 'success', or 'error'
            duration: How long to show (ms), 0 = permanent
        """
        if not self.js_code:
            self.logger.warning("Status display not available")
            return
            
        try:
            await self.page.evaluate(self.js_code, {
                'message': message,
                'type': type,
                'duration': duration
            })
        except Exception as e:
            self.logger.warning(f"Failed to display status: {e}")
