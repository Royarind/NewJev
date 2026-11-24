#!/usr/bin/env python3
"""
Autocomplete Handler
Intelligently manages address autocomplete suggestions using text-LLM
"""

import asyncio
import logging
import json
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)


class AutocompleteHandler:
    """
    Handles browser autocomplete/suggestions in address fields.
    
    Strategy:
    1. Type the address
    2. Detect if autocomplete dropdown appears
    3. Extract available suggestions from DOM
    4. Use text-LLM to pick the best matching suggestion
    5. Click the suggestion or dismiss and use typed value
    """
    
    def __init__(self, page, llm_client=None):
        self.page = page
        self.llm_client = llm_client
        self.logger = logging.getLogger(__name__)
    
    async def fill_with_autocomplete_handling(self, field_selector: str, value: str) -> Dict[str, Any]:
        """
        Fill a field intelligently handling autocomplete suggestions.
        
        Args:
            field_selector: CSS selector for the input field
            value: Value to type (address line, city, etc.)
        
        Returns:
            {
                'success': bool,
                'method': 'typed_only' | 'suggestion_selected' | 'suggestion_dismissed',
                'selected_suggestion': str or None,
                'final_value': str
            }
        """
        try:
            element = await self.page.query_selector(field_selector)
            if not element:
                self.logger.error(f"Field not found: {field_selector}")
                return {'success': False, 'error': 'Field not found'}
            
            # Clear any existing value
            await element.focus()
            await element.click(click_count=3)  # Select all
            await self.page.keyboard.press('Delete')
            await asyncio.sleep(0.2)
            
            # Type the value character by character (natural typing pattern)
            for char in value:
                await element.type(char)
                await asyncio.sleep(0.05)  # Small delay between keystrokes
            
            # Wait for autocomplete to potentially appear
            await asyncio.sleep(0.8)
            
            # Check if autocomplete dropdown appeared
            suggestions = await self._extract_autocomplete_suggestions(field_selector)
            
            if suggestions:
                self.logger.info(f"Found {len(suggestions)} autocomplete suggestions")
                
                # Use text-LLM to pick the best matching suggestion
                if self.llm_client:
                    best_match = await self._select_best_suggestion(value, suggestions)
                    
                    if best_match:
                        self.logger.info(f"Selected suggestion: {best_match}")
                        success = await self._click_suggestion(field_selector, best_match)
                        
                        if success:
                            # Wait for suggestion to populate the field
                            await asyncio.sleep(0.5)
                            final_value = await element.input_value()
                            
                            return {
                                'success': True,
                                'method': 'suggestion_selected',
                                'selected_suggestion': best_match,
                                'final_value': final_value
                            }
                else:
                    self.logger.info("No LLM available, checking for exact match in suggestions")
                    # Try to find exact or close match without LLM
                    exact_match = next((s for s in suggestions if s.lower() == value.lower()), None)
                    close_match = next((s for s in suggestions if value.lower() in s.lower()), None)
                    
                    best_match = exact_match or close_match
                    if best_match:
                        success = await self._click_suggestion(field_selector, best_match)
                        if success:
                            await asyncio.sleep(0.5)
                            final_value = await element.input_value()
                            return {
                                'success': True,
                                'method': 'suggestion_selected',
                                'selected_suggestion': best_match,
                                'final_value': final_value
                            }
            
            # No autocomplete, or LLM found no good match - just use the typed value
            self.logger.info(f"Using typed value (no suggestion selected): {value}")
            
            # Dismiss any lingering autocomplete by pressing Escape
            await self.page.keyboard.press('Escape')
            await asyncio.sleep(0.3)
            
            final_value = await element.input_value()
            return {
                'success': True,
                'method': 'typed_only',
                'selected_suggestion': None,
                'final_value': final_value
            }
        
        except Exception as e:
            self.logger.error(f"Autocomplete handling failed: {e}")
            return {'success': False, 'error': str(e)}
    
    async def _extract_autocomplete_suggestions(self, field_selector: str) -> List[str]:
        """
        Extract visible autocomplete suggestions from the page.
        Targets common UI patterns: dropdown, menu, list, popup
        """
        try:
            suggestions = await self.page.evaluate("""
            (fieldSelector) => {
                const input = document.querySelector(fieldSelector);
                if (!input) return [];
                
                // Common autocomplete container selectors
                const containers = [
                    '.autocomplete',
                    '[role="listbox"]',
                    '.dropdown-menu',
                    '.suggestions',
                    '.autocomplete-results',
                    '.ac-results',
                    'ul[class*="auto"]',
                    'div[class*="dropdown"][class*="open"]',
                    '[aria-expanded="true"]',
                    // Google Places specific
                    '.pac-container',
                    // Common patterns
                    'div[class*="suggestion"]',
                    '.results-container'
                ];
                
                let dropdown = null;
                
                // Find which container is visible
                for (let selector of containers) {
                    const el = document.querySelector(selector);
                    if (el && el.offsetHeight > 0) {
                        dropdown = el;
                        break;
                    }
                }
                
                if (!dropdown) return [];
                
                // Extract suggestion items
                const items = dropdown.querySelectorAll(
                    'li, div[role="option"], [class*="item"], span[class*="suggestion"]'
                );
                
                const suggestions = [];
                items.forEach(item => {
                    const text = item.textContent.trim();
                    if (text && text.length > 0 && suggestions.length < 10) {  // Limit to 10
                        suggestions.push(text);
                    }
                });
                
                return [...new Set(suggestions)];  // Remove duplicates
            }
            """, field_selector)
            
            return suggestions if suggestions else []
        
        except Exception as e:
            self.logger.warning(f"Could not extract autocomplete suggestions: {e}")
            return []
    
    async def _select_best_suggestion(self, typed_value: str, suggestions: List[str]) -> Optional[str]:
        """
        Use text-LLM to intelligently select the best matching suggestion.
        """
        if not self.llm_client or not suggestions:
            return None
        
        prompt = f"""
You are helping select an address from autocomplete suggestions.

USER TYPED: "{typed_value}"

AVAILABLE SUGGESTIONS:
{chr(10).join(f"  - {s}" for s in suggestions[:10])}

TASK:
Which suggestion is the best match for what the user typed?

RULES:
- Return ONLY the exact suggestion text (or "NONE" if no good match)
- Prioritize exact or near-exact matches
- Consider partial matches if they make sense
- Return the suggestion text exactly as it appears in the list

Response format:
SELECTED: <suggestion_text or NONE>
REASON: <brief explanation>
"""
        
        try:
            response = await self.llm_client.complete(prompt, temperature=0.3, max_tokens=256)
            
            # Parse response
            if "SELECTED:" in response:
                lines = response.split('\n')
                for line in lines:
                    if line.startswith("SELECTED:"):
                        selection = line.replace("SELECTED:", "").strip()
                        if selection != "NONE" and selection in suggestions:
                            self.logger.info(f"LLM selected: {selection}")
                            return selection
            
            return None
        
        except Exception as e:
            self.logger.warning(f"LLM suggestion selection failed: {e}")
            return None
    
    async def _click_suggestion(self, field_selector: str, suggestion_text: str) -> bool:
        """
        Click on a specific autocomplete suggestion by text.
        """
        try:
            # Try multiple strategies to find and click the suggestion
            
            # Strategy 1: XPath with text matching
            xpath = f"//li[contains(text(), '{suggestion_text}')]/ancestor::*[1] | //*[text()='{suggestion_text}']"
            element = await self.page.query_selector(f"xpath={xpath}")
            if element:
                await element.click()
                self.logger.info(f"Clicked suggestion via XPath")
                return True
            
            # Strategy 2: Generic autocomplete container traversal
            result = await self.page.evaluate("""
            (suggestionText) => {
                const containers = [
                    '.autocomplete',
                    '[role="listbox"]',
                    '.dropdown-menu',
                    '.suggestions',
                    '.pac-container'
                ];
                
                for (let selector of containers) {
                    const container = document.querySelector(selector);
                    if (!container) continue;
                    
                    const items = container.querySelectorAll('li, div[role="option"], span');
                    for (let item of items) {
                        if (item.textContent.includes(suggestionText)) {
                            item.click();
                            return true;
                        }
                    }
                }
                return false;
            }
            """, suggestion_text)
            
            if result:
                self.logger.info(f"Clicked suggestion via DOM traversal")
                return True
            
            self.logger.warning(f"Could not locate suggestion to click: {suggestion_text}")
            return False
        
        except Exception as e:
            self.logger.error(f"Failed to click suggestion: {e}")
            return False
