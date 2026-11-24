#!/usr/bin/env python3
"""
State/Province Selector
Specialized handler for state/province dropdown fields
"""

import asyncio
import logging
from typing import Dict, Optional, List

logger = logging.getLogger(__name__)


class StateProvinceSelector:
    """
    Robust state/province dropdown selector.
    Handles various UI patterns and state name variations.
    """
    
    def __init__(self, page):
        self.page = page
        self.logger = logging.getLogger(__name__)
        
        # State abbrev to full name mapping
        self.state_map = {
            "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
            "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
            "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
            "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
            "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
            "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
            "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
            "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
            "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
            "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"
        }
    
    async def select_state(self, state_value: str) -> Dict[str, bool]:
        """
        Select a state from a dropdown.
        Tries multiple strategies to find and select the state.
        """
        self.logger.info(f"Selecting state: {state_value}")
        
        # Normalize input (handle both abbrev and full names)
        normalized_value = self._normalize_state_name(state_value)
        self.logger.info(f"Normalized state value: {normalized_value}")
        
        # Strategy 1: Find state select by common patterns
        state_selectors = [
            'select[name*="state" i]',
            'select[name*="province" i]',
            'select[id*="state" i]',
            'select[id*="province" i]',
            'select[aria-label*="state" i]',
            'select',  # Last resort: any select
        ]
        
        dropdown = None
        for selector in state_selectors:
            try:
                el = await self.page.query_selector(selector)
                if el and await el.is_visible():
                    dropdown = el
                    self.logger.info(f"Found state dropdown: {selector}")
                    break
            except:
                continue
        
        if not dropdown:
            self.logger.error("Could not find state dropdown")
            return {'success': False, 'error': 'Dropdown not found'}
        
        # Strategy 2: Extract all option texts
        options = await self._get_dropdown_options(dropdown)
        if not options:
            self.logger.error("No options found in dropdown")
            return {'success': False, 'error': 'No options in dropdown'}
        
        self.logger.info(f"Found {len(options)} options: {options[:10]}")
        
        # Strategy 3: Find best matching option
        best_match = self._find_best_option_match(normalized_value, options)
        
        if not best_match:
            self.logger.error(f"No matching option found for: {normalized_value}")
            self.logger.error(f"Available options: {options}")
            return {'success': False, 'error': f'No match for {normalized_value}'}
        
        self.logger.info(f"Best match found: {best_match}")
        
        # Strategy 4: Click the matching option
        success = await self._click_option_in_dropdown(dropdown, best_match)
        
        if success:
            await asyncio.sleep(0.5)
            # Verify selection
            selected = await self._get_selected_option(dropdown)
            self.logger.info(f"✓ Selected state: {selected}")
            return {'success': True, 'selected': selected}
        else:
            return {'success': False, 'error': 'Failed to click option'}
    
    async def _get_dropdown_options(self, dropdown) -> List[str]:
        """Extract all option texts from a dropdown"""
        try:
            options = await dropdown.evaluate("""
            select => {
                return Array.from(select.querySelectorAll('option'))
                    .map(opt => opt.textContent.trim())
                    .filter(text => text.length > 0);
            }
            """)
            return options
        except Exception as e:
            self.logger.error(f"Failed to extract options: {e}")
            return []
    
    async def _get_selected_option(self, dropdown) -> Optional[str]:
        """Get currently selected option text"""
        try:
            selected = await dropdown.evaluate("""
            select => {
                const selected = select.querySelector('option:checked');
                return selected ? selected.textContent.trim() : null;
            }
            """)
            return selected
        except:
            return None
    
    def _normalize_state_name(self, value: str) -> str:
        """Normalize state name (handle abbreviations and variations)"""
        value = value.strip().upper()
        
        # Check if it's an abbreviation
        if value in self.state_map:
            return self.state_map[value]
        
        # Check if it's a full name already
        if value in self.state_map.values():
            return value
        
        # Try case-insensitive match
        for abbrev, full_name in self.state_map.items():
            if full_name.upper() == value:
                return full_name
        
        # Return as-is if no mapping found
        return value
    
    def _find_best_option_match(self, search_value: str, options: List[str]) -> Optional[str]:
        """Find the best matching option from available list"""
        search_lower = search_value.lower().strip()
        
        # Exact match (case-insensitive)
        for opt in options:
            if opt.lower().strip() == search_lower:
                return opt
        
        # Partial match
        for opt in options:
            if search_lower in opt.lower():
                return opt
        
        # Check if any option starts with the search value
        for opt in options:
            if opt.lower().startswith(search_lower):
                return opt
        
        # Last resort: if search is two chars (abbrev), look for matching full name
        if len(search_value) == 2:
            full_name = self.state_map.get(search_value.upper())
            if full_name:
                for opt in options:
                    if full_name.lower() in opt.lower():
                        return opt
        
        return None
    
    async def _click_option_in_dropdown(self, dropdown, option_text: str) -> bool:
        """Click a specific option in the dropdown"""
        try:
            # Method 1: Use Playwright's selectOption (most reliable)
            # First, try to find the option value
            option_value = await dropdown.evaluate(
                f"""
                select => {{
                    const option = Array.from(select.querySelectorAll('option'))
                        .find(opt => opt.textContent.trim() === '{option_text}');
                    return option ? option.value : null;
                }}
                """
            )
            
            if option_value:
                try:
                    await dropdown.select_option(option_value)
                    self.logger.info(f"✓ Selected option via selectOption: {option_value}")
                    return True
                except Exception as e:
                    self.logger.warning(f"selectOption failed: {e}, trying click")
            
            # Method 2: Click the option directly
            result = await dropdown.evaluate(
                f"""
                select => {{
                    const option = Array.from(select.querySelectorAll('option'))
                        .find(opt => opt.textContent.trim() === '{option_text}');
                    if (option) {{
                        option.selected = true;
                        select.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        select.dispatchEvent(new Event('blur', {{ bubbles: true }}));
                        return true;
                    }}
                    return false;
                }}
                """
            )
            
            if result:
                self.logger.info(f"✓ Selected option via element manipulation: {option_text}")
                return True
            
            self.logger.warning(f"Could not find option: {option_text}")
            return False
        
        except Exception as e:
            self.logger.error(f"Failed to click option: {e}")
            return False
