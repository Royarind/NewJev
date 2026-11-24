#!/usr/bin/env python3
"""
Text-Based LLM Form Filler
Uses ONLY text-based LLM (no vision) to reason about form errors and fix them
"""

import logging
import json
import asyncio
from typing import Dict, Any, Optional
from .form_state_extractor import FormStateExtractor
from .autocomplete_handler import AutocompleteHandler
from .state_province_selector import StateProvinceSelector

logger = logging.getLogger(__name__)


class TextLLMFormFixer:
    """
    Intelligent form error recovery using ONLY text-based LLM.
    
    Strategy:
    1. Extract form state as structured text
    2. Pass form summary to text LLM
    3. LLM reasons about what's missing and how to fix it
    4. Execute LLM's recommended actions
    5. Validate and retry if needed
    """
    
    def __init__(self, page, llm_client):
        self.page = page
        self.llm_client = llm_client  # Text-based LLM (OpenRouter, Groq, etc.)
        self.extractor = FormStateExtractor(page)
        self.autocomplete_handler = AutocompleteHandler(page, llm_client)  # For address suggestions
        self.state_selector = StateProvinceSelector(page)  # For state/province dropdowns
        self.logger = logging.getLogger(__name__)
        self.retry_count = 0
        self.max_retries = 5
    
    async def auto_fix_form_errors(self) -> Dict[str, Any]:
        """
        Main entry point: Automatically detect and fix form errors using text LLM.
        
        Returns:
            {
                'success': bool,
                'fixed_fields': [list of field names],
                'remaining_errors': [list of still-unfixed errors],
                'llm_reasoning': str,
                'actions_taken': [list of actions]
            }
        """
        self.logger.info("Starting text-LLM form error recovery...")
        
        fixed_fields = []
        actions_taken = []
        remaining_errors = []
        
        while self.retry_count < self.max_retries:
            self.retry_count += 1
            self.logger.info(f"=== Form fix attempt {self.retry_count}/{self.max_retries} ===")
            
            # 1. Extract current form state
            form_summary = await self.extractor.get_form_summary_for_llm()
            missing_fields = await self.extractor.identify_missing_required_fields()
            
            if not missing_fields:
                self.logger.info("✓ All form fields are valid. Done!")
                return {
                    'success': True,
                    'fixed_fields': fixed_fields,
                    'remaining_errors': [],
                    'actions_taken': actions_taken,
                    'llm_reasoning': 'No errors detected'
                }
            
            # 2. Ask text LLM what to do
            llm_analysis = await self._ask_llm_for_diagnosis(form_summary, missing_fields)
            
            self.logger.info(f"LLM Analysis:\n{llm_analysis['reasoning']}")
            
            # 3. Execute LLM's recommended actions
            if llm_analysis['recommended_actions']:
                for action in llm_analysis['recommended_actions']:
                    self.logger.info(f"Executing action: {action['action_type']}")
                    
                    if action['action_type'] == 'fill_field':
                        success = await self._execute_fill_action(action)
                        if success:
                            fixed_fields.append(action['field_name'])
                            actions_taken.append(action)
                    
                    elif action['action_type'] == 'select_dropdown':
                        success = await self._execute_dropdown_action(action)
                        if success:
                            fixed_fields.append(action['field_name'])
                            actions_taken.append(action)
                    
                    elif action['action_type'] == 'click_element':
                        success = await self._execute_click_action(action)
                        if success:
                            actions_taken.append(action)
                    
                    elif action['action_type'] == 'scroll_to_field':
                        success = await self._execute_scroll_action(action)
                        if success:
                            actions_taken.append(action)
                
                # Wait for page to update
                await asyncio.sleep(1.5)
            
            # 4. Check if we made progress
            remaining_errors = await self._check_remaining_errors()
            
            if not remaining_errors:
                self.logger.info("✓ All errors fixed!")
                return {
                    'success': True,
                    'fixed_fields': fixed_fields,
                    'remaining_errors': [],
                    'actions_taken': actions_taken,
                    'llm_reasoning': llm_analysis['reasoning']
                }
        
        # Exhausted retries
        remaining_errors = await self._check_remaining_errors()
        
        return {
            'success': False,
            'fixed_fields': fixed_fields,
            'remaining_errors': remaining_errors,
            'actions_taken': actions_taken,
            'llm_reasoning': 'Max retries exhausted'
        }
    
    async def _ask_llm_for_diagnosis(self, form_summary: str, missing_fields: list) -> Dict[str, Any]:
        """
        Send form state to text LLM and ask it to diagnose what's wrong and how to fix it.
        """
        missing_details = "\n".join([
            f"  - {item['field_info']['name'] or item['field_info']['id']}: {item['reason']}"
            for item in missing_fields
        ])
        
        prompt = f"""
You are an expert web automation assistant. You need to help fix a checkout form that has errors.

CURRENT FORM STATE:
{form_summary}

MISSING/ERROR FIELDS:
{missing_details}

Your task:
1. Analyze what's wrong with each field
2. Identify what value should be entered or what option should be selected
3. Provide specific, executable actions to fix each error

For each field that needs fixing, provide:
- Field name/id
- Action type: 'fill_field', 'select_dropdown', 'click_element', or 'scroll_to_field'
- Specific value to enter (if applicable)
- Exact option text to select from dropdown (if applicable)

IMPORTANT:
- For dropdown fields (select type), only suggest values that are in the available options list
- If a required field is empty and you don't have data for it, suggest scrolling to see if more context appears
- Be specific and actionable

Return ONLY valid JSON:
{{
    "reasoning": "Your analysis of the form state and errors",
    "recommended_actions": [
        {{
            "action_type": "select_dropdown|fill_field|click_element|scroll_to_field",
            "field_name": "name_or_id_of_field",
            "field_selector": "css_selector_if_known",
            "value": "value_to_enter_or_option_text",
            "reason": "why_this_action"
        }}
    ],
    "confidence": 0.95
}}
"""
        
        try:
            response = await self.llm_client.complete(prompt, temperature=0.3, max_tokens=1024)
            
            # Extract JSON from response
            import re
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                analysis = json.loads(json_match.group(0))
                return analysis
            else:
                self.logger.error(f"Could not extract JSON from LLM response: {response[:200]}")
                return {'reasoning': 'LLM parsing failed', 'recommended_actions': []}
        
        except Exception as e:
            self.logger.error(f"LLM diagnosis failed: {e}")
            return {'reasoning': f'LLM error: {e}', 'recommended_actions': []}
    
    # Semantic field name → list of CSS selectors to try (covers generic, Shopify, common variants)
    _FIELD_SELECTOR_MAP = {
        'email':       ["input[type='email']", "input[name='email']", "input[autocomplete='email']"],
        'first_name':  ["input[name='firstName']", "input[name='first_name']", "input[autocomplete='given-name']"],
        'firstname':   ["input[name='firstName']", "input[name='first_name']", "input[autocomplete='given-name']"],
        'first name':  ["input[name='firstName']", "input[name='first_name']", "input[autocomplete='given-name']"],
        'last_name':   ["input[name='lastName']", "input[name='last_name']", "input[autocomplete='family-name']"],
        'lastname':    ["input[name='lastName']", "input[name='last_name']", "input[autocomplete='family-name']"],
        'last name':   ["input[name='lastName']", "input[name='last_name']", "input[autocomplete='family-name']"],
        'address':     ["input[name='address1']", "input[name='address']", "input[autocomplete='address-line1']"],
        'address1':    ["input[name='address1']", "input[name='address']", "input[autocomplete='address-line1']"],
        'address_line1': ["input[name='address1']", "input[autocomplete='address-line1']"],
        'city':        ["input[name='city']", "input[autocomplete='address-level2']"],
        'state':       ["select[name='zone']", "select[name='state']", "select[name='province']", "input[name='state']"],
        'zone':        ["select[name='zone']", "select[name='state']"],
        'province':    ["select[name='zone']", "select[name='province']", "select[name='state']"],
        'zip_code':    ["input[name='postalCode']", "input[name='zip']", "input[name='postal_code']", "input[autocomplete='postal-code']"],
        'zip':         ["input[name='postalCode']", "input[name='zip']", "input[autocomplete='postal-code']"],
        'postal_code': ["input[name='postalCode']", "input[name='zip']", "input[autocomplete='postal-code']"],
        'postalcode':  ["input[name='postalCode']", "input[autocomplete='postal-code']"],
        'phone':       ["input[name='phone']", "input[type='tel']", "input[autocomplete='tel']"],
        'telephone':   ["input[name='phone']", "input[type='tel']"],
    }

    async def _resolve_field_element(self, field_name: str, field_selector: str):
        """Try to find a form element using the selector, then semantic fallbacks."""
        # 1. Try the provided selector directly
        el = await self.page.query_selector(field_selector)
        if el:
            return el
        # 2. Try semantic fallbacks based on field_name
        key = field_name.lower().strip().replace('-', '_').replace(' ', '_')
        fallbacks = self._FIELD_SELECTOR_MAP.get(key) or self._FIELD_SELECTOR_MAP.get(field_name.lower().strip(), [])
        for sel in fallbacks:
            el = await self.page.query_selector(sel)
            if el:
                self.logger.debug(f"Resolved '{field_name}' via fallback: {sel}")
                return el
        return None

    async def _execute_fill_action(self, action: Dict[str, Any]) -> bool:
        """Fill a text input field with a value, intelligently handling autocomplete"""
        try:
            field_selector = action.get('field_selector') or f"input[name='{action['field_name']}']"
            value = action.get('value', '')
            field_name = action.get('field_name', '')

            self.logger.info(f"Filling {field_selector} with '{value}'")

            element = await self._resolve_field_element(field_name, field_selector)
            if not element:
                self.logger.warning(f"Field not found: {field_selector}")
                return False
            
            # Check if this is an address-related field (likely to have autocomplete)
            address_keywords = ['address', 'street', 'city', 'country', 'postal', 'zip']
            is_address_field = any(kw in field_name.lower() for kw in address_keywords)
            
            if is_address_field and self.autocomplete_handler:
                # Use smart autocomplete handling for address fields
                self.logger.info(f"Using autocomplete-aware filling for address field: {field_name}")
                result = await self.autocomplete_handler.fill_with_autocomplete_handling(field_selector, value)
                
                if result['success']:
                    # Trigger change event to notify form that field was updated
                    await element.evaluate("el => el.dispatchEvent(new Event('change', { bubbles: true }))")
                    self.logger.info(f"✓ Field filled: {result['method']} -> '{result['final_value']}'")
                    return True
                else:
                    self.logger.warning(f"Autocomplete handling failed, falling back to direct fill")
            
            # Fallback: Direct fill (for non-address fields or if autocomplete handler failed)
            await element.fill(value)
            
            # Trigger input event
            await element.evaluate("el => el.dispatchEvent(new Event('input', { bubbles: true }))")
            await element.evaluate("el => el.dispatchEvent(new Event('change', { bubbles: true }))")
            
            self.logger.info(f"✓ Filled successfully")
            return True
        
        except Exception as e:
            self.logger.error(f"Fill action failed: {e}")
            return False
    
    async def _execute_dropdown_action(self, action: Dict[str, Any]) -> bool:
        """Select an option from a dropdown with specialized handling for state/province"""
        try:
            field_name = action.get('field_name', '').lower()
            field_selector = action.get('field_selector') or f"select[name='{action['field_name']}']"
            option_text = action.get('value', '')
            
            self.logger.info(f"Selecting '{option_text}' from {field_selector}")
            
            element = await self._resolve_field_element(field_name, field_selector)
            if not element:
                self.logger.warning(f"Dropdown not found: {field_selector}")
                return False
            
            # SPECIAL HANDLING: State/Province fields
            if 'state' in field_name or 'province' in field_name:
                self.logger.info(f"Using specialized state selector for: {field_name}")
                result = await self.state_selector.select_state(option_text)
                return result.get('success', False)
            
            # GENERAL: Try to select by option text
            options = await element.query_selector_all('option')
            
            for option in options:
                opt_text = await option.text_content()
                if option_text.lower() in opt_text.lower() or opt_text.lower() in option_text.lower():
                    # Use Playwright's built-in selectOption method for reliability
                    try:
                        opt_value = await self.page.evaluate(
                            "(el, txt) => { const opt = Array.from(el.options).find(o => o.text.trim().toLowerCase() === txt.toLowerCase()); return opt ? opt.value : null; }",
                            element, option_text
                        )
                        if opt_value is not None:
                            await element.select_option(value=opt_value)
                            self.logger.info(f"Selected by value='{opt_value}'")
                            return True
                    except Exception:
                        pass

                    options = await element.query_selector_all('option')
                    for opt in options:
                        txt = (await opt.inner_text()).strip()
                        if txt.lower() == option_text.lower():
                            val = await opt.get_attribute('value')
                            await element.select_option(value=val or txt)
                            self.logger.info(f"Selected '{option_text}' by text match")
                            return True

                    self.logger.warning(f"Option '{option_text}' not found in dropdown")
                    return False

        except Exception as e:
            self.logger.error(f"Dropdown action failed: {e}")
            return False

    async def _execute_click_action(self, action: Dict[str, Any]) -> bool:
        """Click an element"""
        try:
            field_selector = action.get('field_selector', '')
            element = await self.page.query_selector(field_selector)
            if not element:
                self.logger.warning(f"Click target not found: {field_selector}")
                return False
            await element.click()
            self.logger.info(f"Clicked {field_selector}")
            return True
        except Exception as e:
            self.logger.error(f"Click action failed: {e}")
            return False

    async def _execute_scroll_action(self, action: Dict[str, Any]) -> bool:
        """Scroll to a field"""
        try:
            field_selector = action.get('field_selector', '')
            element = await self.page.query_selector(field_selector)
            if element:
                await element.scroll_into_view_if_needed()
                await asyncio.sleep(0.5)
                self.logger.info(f"Scrolled to {field_selector}")
                return True
            return False
        except Exception as e:
            self.logger.error(f"Scroll action failed: {e}")
            return False

    async def _check_remaining_errors(self) -> list:
        """Check which errors still remain after fix attempts"""
        extractor = FormStateExtractor(self.page)
        return await extractor.identify_missing_required_fields()
