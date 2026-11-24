import asyncio
import logging
import os
import json
import re
from pathlib import Path
from typing import Dict, Any, List

class DistractionManager:
    """
    Utility to manage page distractions like pop-ups, cookies, and promotional tokens.
    Enhanced with aggressive multi-tier popup detection and removal.
    """
    
    def __init__(self, page, llm_client=None):
        self.page = page
        self.llm = llm_client
        self.logger = logging.getLogger(__name__)
        
        # Resolve JS assets directory
        try:
            self.root_dir = Path(os.getcwd()).resolve()
            self.js_assets_dir = self.Path(__file__).resolve().parents[1] / 'dom_assets'
        except Exception:
            self.js_assets_dir = Path(__file__).resolve().parents[1] / 'dom_assets'
            
    async def clear_distractions_aggressive(self, max_attempts: int = 3, use_vlm: bool = False) -> Dict[str, Any]:
        """
        Aggressively clear distractions with multiple attempts.
        Useful before critical interactions like size selection.
        """
        self.logger.info(f"🧹 Aggressive distraction clearing (max {max_attempts} attempts)...")
        
        all_results = {
            'attempts': 0,
            'total_closed': [],
            'total_removed': [],
            'success': False,
            'details': []
        }
        
        for attempt in range(max_attempts):
            self.logger.info(f"  Attempt {attempt + 1}/{max_attempts}...")
            
            try:
                # Try JS-based clearing
                result = await self.clear_distractions()
                all_results['attempts'] += 1
                all_results['details'].append(result)
                
                if result.get('details'):
                    closed = result['details'].get('closed', [])
                    removed = result['details'].get('removed', [])
                    hidden = result['details'].get('hidden', [])
                    
                    if closed or removed or hidden:
                        all_results['total_closed'].extend(closed)
                        all_results['total_removed'].extend(removed)
                        self.logger.info(f"    Closed: {len(closed)}, Removed: {len(removed)}, Hidden: {len(hidden)}")
                        all_results['success'] = True
                    else:
                        self.logger.info("    No distractions found to clear. Breaking early to save latency.")
                        break
                else:
                    self.logger.info("    No details returned from distraction clearing. Breaking early.")
                    break
                
                await asyncio.sleep(0.3)
                
            except Exception as e:
                self.logger.warning(f"    Attempt {attempt + 1} failed: {e}")
        
        # Try VLM if JS didn't work and we have LLM and VLM is explicitly enabled
        if not all_results['success'] and self.llm and use_vlm:
            self.logger.info("  Trying VLM detection...")
            vlm_result = await self.vlm_clear_distractions()
            if vlm_result.get('success') and vlm_result.get('cleared'):
                self.logger.info("    VLM closed a popup")
                all_results['success'] = True
                all_results['details'].append(vlm_result)
        
        return all_results
            
    async def clear_distractions(self) -> Dict[str, Any]:
        """
        Execute the distraction removal JS script.
        Returns a summary of actions taken.
        """
        self.logger.debug("Clearing distractions...")
        try:
            path = self.js_assets_dir / 'close_distractions.js'
            if not path.exists():
                self.logger.warning(f"Distraction removal script not found at {path}")
                return {'success': False, 'error': 'Script not found', 'details': {}}
                
            js_code = path.read_text(encoding='utf-8')
            results = await self.page.evaluate(js_code)
            
            self.logger.debug(f"Distraction removal results: {results}")
            await asyncio.sleep(0.3)
            
            return {
                'success': True,
                'details': results
            }
        except Exception as e:
            self.logger.warning(f"Error during distraction removal: {e}")
            return {'success': False, 'error': str(e), 'details': {}}

    async def vlm_clear_distractions(self) -> Dict[str, Any]:
        """
        Use VLM to identify and close distractions.
        """
        if not self.llm:
            return {'success': False, 'error': 'No LLM client'}
            
        self.logger.info("🔍 VLM distraction check...")
        try:
            screenshot_path = "vlm_dist_check.png"
            await self.page.screenshot(path=screenshot_path)
            
            prompt = """Analyze this page and find any pop-up, modal, or dialog blocking the content. 
            If found, provide coordinates [x, y] in 0-1000 scale for the close button (usually X in top-right).
            Return JSON: {"found": bool, "coordinates": [x, y] if found else null, "description": "brief description"}"""
            
            vlm_model = os.getenv('OPENROUTER_MODEL_VLM', "qwen/qwen-2-vl-72b-instruct")
            response = await self.llm.complete_with_vision(prompt, screenshot_path, model=vlm_model)
            
            decision = self._extract_json(response)
            if decision.get('found'):
                coords = decision.get('coordinates', [500, 100])
                if isinstance(coords, list) and len(coords) == 2:
                    viewport = self.page.viewport_size or {'width': 1280, 'height': 800}
                    px = (coords[0] / 1000) * viewport['width']
                    py = (coords[1] / 1000) * viewport['height']
                    
                    self.logger.info(f"  Clicking popup close at ({px:.0f}, {py:.0f})")
                    await self.page.mouse.click(px, py)
                    await asyncio.sleep(0.5)
                    return {'success': True, 'cleared': True, 'description': decision.get('description')}
            
            return {'success': True, 'cleared': False, 'description': decision.get('description', 'No popup detected')}
        except Exception as e:
            self.logger.warning(f"VLM distraction check failed: {e}")
            return {'success': False, 'error': str(e), 'cleared': False}

    def _extract_json(self, text: str) -> Dict:
        """Extract JSON from response"""
        try:
            start = text.find('{')
            end = text.rfind('}')
            if start != -1 and end != -1:
                return json.loads(text[start:end+1])
        except: 
            pass
        return {}

    async def wait_for_clean_viewport(self, max_wait: float = 10.0) -> bool:
        """
        Poll until clean by aggressively clearing distractions.
        """
        self.logger.info(f"⏳ Waiting for clean viewport (max {max_wait}s)...")
        start = asyncio.get_event_loop().time()
        
        while asyncio.get_event_loop().time() - start < max_wait:
            res = await self.clear_distractions_aggressive(max_attempts=2)
            
            # Check if there are any active distractions
            details = res.get('details', [])
            has_distractions = False
            
            for detail in details:
                if isinstance(detail, dict):
                    d = detail.get('details', {})
                    if d.get('closed') or d.get('removed') or d.get('hidden'):
                        has_distractions = True
                        break
            
            if not has_distractions:
                self.logger.info("✅ Viewport is clean")
                return True
            
            await asyncio.sleep(0.5)
        
        self.logger.warning("⚠️ Timeout waiting for clean viewport")
        return False

    async def clear_before_critical_action(self, action_name: str = "critical action") -> bool:
        """
        Clear distractions before critical interactions like variant selection.
        Returns True if viewport is clean.
        """
        self.logger.info(f"🛡️ Clearing distractions before {action_name}...")
        result = await self.clear_distractions_aggressive(max_attempts=3)
        
        if result.get('success'):
            self.logger.info(f"✅ Viewport cleared before {action_name}")
            return True
        else:
            self.logger.warning(f"⚠️ Could not fully clear distractions before {action_name}")
            return False
