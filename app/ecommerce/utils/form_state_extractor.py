#!/usr/bin/env python3
"""
Form State Extractor
Extracts structured form metadata and errors via DOM (no vision needed)
Provides text-based LLM with actionable field information
"""

import json
import logging
from typing import Dict, List, Any

logger = logging.getLogger(__name__)


class FormStateExtractor:
    """
    Extracts ALL form field metadata (labels, types, required status, errors, options)
    Enables text-based LLM to reason about form state without vision
    """
    
    def __init__(self, page):
        self.page = page
        self.logger = logging.getLogger(__name__)
    
    async def extract_form_state(self) -> Dict[str, Any]:
        """
        Extract complete form state as structured JSON.
        Returns field metadata, error messages, and current values.
        """
        form_state = await self.page.evaluate("""
        () => {
            const fields = [];
            
            // Get all form inputs, selects, textareas
            const inputs = document.querySelectorAll('input, select, textarea');
            
            inputs.forEach((el, idx) => {
                // Skip inputs that are inside global header, nav, footer, or common navigation/search containers
                if (el.closest('header, nav, footer, #nav-belt, #navbar, #nav-bar, #nav-main, #nav-tools, #g-navigation, .global-header, .global-footer')) {
                    return;
                }
                
                // Skip inputs that are search inputs or part of search bars
                const elName = (el.name || '').toLowerCase();
                const elId = (el.id || '').toLowerCase();
                if (elName === 'url' || elName === 'field-keywords' || elName === 'site-search' || el.type === 'search' || elName.includes('search') || elId.includes('search')) {
                    return;
                }

                const fieldInfo = {
                    index: idx,
                    type: el.tagName.toLowerCase(),
                    inputType: el.type || null,
                    name: el.name || null,
                    id: el.id || null,
                    value: el.value || '',
                    placeholder: el.placeholder || null,
                    required: el.required || el.hasAttribute('required') || el.getAttribute('aria-required') === 'true',
                    disabled: el.disabled,
                    visible: el.offsetHeight > 0 && el.offsetWidth > 0,
                    label: null,
                    errorMessage: null,
                    options: [],
                    ariaLabel: el.getAttribute('aria-label'),
                    ariaDescribedBy: el.getAttribute('aria-describedby')
                };
                
                // Find associated label (for accessibility)
                if (el.id) {
                    const label = document.querySelector(`label[for="${el.id}"]`);
                    if (label) fieldInfo.label = label.textContent.trim();
                }
                
                // Check for error messages
                if (fieldInfo.ariaDescribedBy) {
                    const errorEl = document.getElementById(fieldInfo.ariaDescribedBy);
                    if (errorEl) {
                        fieldInfo.errorMessage = errorEl.textContent.trim();
                    }
                }
                
                // Look for error in nearby parent divs
                if (!fieldInfo.errorMessage) {
                    const parentDiv = el.closest('div[class*="error" i], div[class*="invalid" i]');
                    if (parentDiv) {
                        const errText = parentDiv.querySelector('[class*="error" i], [role="alert"]');
                        if (errText) fieldInfo.errorMessage = errText.textContent.trim();
                    }
                }
                
                // Check element's own styling for red border (error indication)
                const style = window.getComputedStyle(el);
                if (style.borderColor === 'rgb(220, 53, 69)' || style.borderColor === 'rgb(255, 0, 0)') {
                    fieldInfo.hasRedBorder = true;
                }
                
                // For select elements, capture options
                if (el.tagName === 'SELECT') {
                    const options = Array.from(el.querySelectorAll('option')).map(opt => ({
                        value: opt.value,
                        text: opt.textContent.trim(),
                        selected: opt.selected
                    }));
                    fieldInfo.options = options;
                    
                    // Detect if select is "empty" (showing placeholder or default option)
                    const selectedOption = el.querySelector('option:checked');
                    const selectedText = selectedOption ? selectedOption.textContent.trim().toLowerCase() : '';
                    
                    // Mark as empty if showing placeholder text
                    const placeholders = ['select', 'choose', 'pick', 'select a', 'option', '---', ''];
                    const isPlaceholder = placeholders.some(ph => selectedText.includes(ph));
                    
                    if (isPlaceholder || !selectedOption || selectedOption.value === '' || selectedOption.value === '-1') {
                        fieldInfo.isEmpty = true;  // New flag for empty selects
                    }
                }
                
                fields.push(fieldInfo);
            });
            
            return { 
                fields: fields,
                timestamp: new Date().toISOString()
            };
        }
        """)
        
        return form_state
    
    async def identify_missing_required_fields(self) -> List[Dict[str, Any]]:
        """
        Identify which required fields are empty or have errors.
        Returns list of fields that need attention.

        NOTE: Payment-section fields (radio buttons for card type, etc.) are
        intentionally excluded — they are not part of the contact/shipping
        stage and should not block those stages from completing.
        """
        state = await self.extract_form_state()
        missing = []

        # Names/IDs that belong to the payment section — skip them entirely
        PAYMENT_FIELD_NAMES = {
            'payment_method', 'payment-method', 'payment_type',
            'creditCard', 'credit_card', 'card_number', 'cc_number',
            'cardNumber', 'card-number', 'expiry', 'cvv', 'cvc',
            'basic',   # Shopify "basic" payment radio
        }
        PAYMENT_FIELD_ID_KEYWORDS = [
            'payment', 'card', 'credit', 'cvv', 'cvc', 'expiry'
        ]

        def _is_payment_field(field: dict) -> bool:
            name = (field.get('name') or '').lower()
            fid  = (field.get('id')   or '').lower()
            if name in PAYMENT_FIELD_NAMES:
                return True
            if any(kw in fid for kw in PAYMENT_FIELD_ID_KEYWORDS):
                return True
            # Radio buttons whose value is 'on' and sit inside a payment block
            if field.get('inputType') == 'radio' and field.get('value') == 'on':
                return True
            return False

        for field in state['fields']:
            # Skip hidden fields
            if not field['visible']:
                continue

            # Skip disabled fields
            if field['disabled']:
                continue

            # Skip payment-section fields — handled separately at Stage 7
            if _is_payment_field(field):
                continue

            # Check if required and empty/unselected
            if field['required'] and (not field['value'] or field.get('isEmpty')):
                missing.append({
                    'field_info': field,
                    'reason': 'required_empty'
                })

            # Check if has error message
            if field['errorMessage']:
                missing.append({
                    'field_info': field,
                    'reason': 'has_error',
                    'error': field['errorMessage']
                })

            # Check for red border (visual error indicator)
            if field.get('hasRedBorder'):
                missing.append({
                    'field_info': field,
                    'reason': 'red_border'
                })

        return missing
    
    async def get_field_by_name(self, field_name: str) -> Dict[str, Any]:
        """Get info for a specific field by name"""
        state = await self.extract_form_state()
        
        for field in state['fields']:
            if field['name'] == field_name or field['id'] == field_name:
                return field
        
        return None
    
    async def get_form_summary_for_llm(self) -> str:
        """
        Create a human-readable form summary for text-based LLM to reason over.
        This is the key function for text-LLM decision making.
        """
        state = await self.extract_form_state()
        missing = await self.identify_missing_required_fields()
        
        summary = "FORM STATE ANALYSIS:\n"
        summary += "=" * 60 + "\n\n"
        
        # Section 1: Required fields with issues
        if missing:
            summary += "🚨 FIELDS REQUIRING ATTENTION:\n"
            for item in missing:
                field = item['field_info']
                reason = item['reason']
                
                label = field['label'] or field['ariaLabel'] or field['name'] or f"Field {field['index']}"
                summary += f"\n  • {label}\n"
                summary += f"    Type: {field['inputType'] or field['type']}\n"
                summary += f"    Current value: '{field['value']}'\n"
                summary += f"    Required: {field['required']}\n"
                summary += f"    Reason: {reason}\n"
                
                if item.get('error'):
                    summary += f"    Error message: {item['error']}\n"
                
                if field['options']:
                    summary += f"    Available options: {[opt['text'] for opt in field['options']]}\n"
        else:
            summary += "✓ All required fields appear to be filled.\n"
        
        summary += "\n" + "=" * 60 + "\n"
        summary += "ALL FORM FIELDS:\n"
        
        for field in state['fields']:
            if not field['visible']:
                continue
            
            label = field['label'] or field['ariaLabel'] or field['name'] or f"Field {field['index']}"
            summary += f"\n  {label}:\n"
            summary += f"    value='{field['value']}', required={field['required']}, type={field['inputType']}\n"
            
            if field['options']:
                summary += f"    Options: {[opt['text'] for opt in field['options']]}\n"
        
        return summary
    
    async def fill_field_by_selector_and_value(self, selector: str, value: str) -> bool:
        """
        Fill a field using selector and value.
        Returns whether it succeeded.
        """
        try:
            element = await self.page.query_selector(selector)
            if not element:
                return False
            
            el_type = await element.evaluate("el => el.tagName.toLowerCase()")
            
            if el_type == 'select':
                # For dropdowns, try to find and select the matching option
                await element.select_option(value)
                return True
            elif el_type in ['input', 'textarea']:
                # Clear and fill
                await element.fill(value)
                return True
            
            return False
        except Exception as e:
            self.logger.error(f"Failed to fill field {selector} with {value}: {e}")
            return False
    
    async def submit_form_if_ready(self) -> bool:
        """
        Check if form is ready and submit.
        Returns whether we should proceed.
        """
        missing = await self.identify_missing_required_fields()
        
        if not missing:
            self.logger.info("All required fields satisfied. Ready to proceed.")
            return True
        
        self.logger.warning(f"Form still has {len(missing)} fields with issues. Not ready.")
        return False
