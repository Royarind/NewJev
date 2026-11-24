"""
Address Autofill Module
Dynamically detects and fills address form fields
"""
import asyncio
import logging
import random
from typing import Dict, Optional, List, Tuple
from .profiles import CheckoutProfile, Country
from .smart_form_filler import SmartFormFiller
from .vision import EnhancedVisionSystem

class AddressAutofill:
    """
    Intelligent address form filler with:
    - Dynamic field detection
    - VLM-based next-action detection
    - Progressive one-field-at-a-time filling
    - Country-specific handling
    """
    
    def __init__(self, page, llm_client=None, status_display=None):
        self.page = page
        self.llm_client = llm_client
        self.status = status_display
        self.logger = logging.getLogger(__name__)
        self.vision = EnhancedVisionSystem(debug_mode=False)
        
        # Loop prevention: Track field interaction history
        self.field_attempts = {}  # field_name -> {'count': int, 'strategies': [str], 'last_value': str, 'success': bool}
        self.loop_detection_threshold = 3
        
        # Field mapping patterns (priority ordered)
        self.field_patterns = {
            'first_name': [
                'input[name*="first" i][name*="name" i]',
                'input[id*="first" i][id*="name" i]',
                'input[placeholder*="first name" i]',
                'input[autocomplete="given-name"]',
                'input[name="firstName"]',
                'input[id="firstName"]'
            ],
            'last_name': [
                'input[name*="last" i][name*="name" i]',
                'input[id*="last" i][id*="name" i]',
                'input[placeholder*="last name" i]',
                'input[autocomplete="family-name"]',
                'input[name="lastName"]',
                'input[id="lastName"]'
            ],
            'full_name': [
                'input[name="name"]',
                'input[name*="fullname" i]',
                'input[name*="full_name" i]',
                'input[id="name"]',
                'input[id*="fullname" i]',
                'input[id*="full_name" i]',
                'input[placeholder*="full name" i]',
                'input[placeholder*="name" i][placeholder*="full" i]',
                'input[autocomplete="name"]'
            ],
            'email': [
                'input[type="email"]',
                'input[name*="email" i]',
                'input[id*="email" i]',
                'input[autocomplete="email"]',
                'input[placeholder*="email" i]',
                'input[placeholder*="mail" i]',
                'input[aria-label*="email" i]',
                'input[aria-label*="mail" i]'
            ],
            'phone': [
                'input[type="tel"]',
                'input[name*="phone" i]',
                'input[id*="phone" i]',
                'input[name*="mobile" i]',
                'input[id*="mobile" i]',
                'input[placeholder*="mobile" i]',
                'input[placeholder*="phone" i]',
                'input[placeholder*="10-digit" i]',
                'input[placeholder*="number" i]',
                'input[autocomplete="tel"]',
                'input[aria-label*="phone" i]',
                'input[aria-label*="mobile" i]'
            ],
            'address_line1': [
                'input[name*="address1" i]',
                'input[name*="address" i][name*="line" i][name*="1" i]',
                'input[id*="address1" i]',
                'input[id*="Address" i]',
                'input[autocomplete="address-line1"]',
                'input[placeholder*="Address" i]',
                'input[placeholder*="street" i]',
                'textarea[name*="address" i]'
            ],
            'address_line2': [
                'input[name*="address2" i]',
                'input[name*="address" i][name*="line" i][name*="2" i]',
                'input[id*="address2" i]',
                'input[autocomplete="address-line2"]',
                'input[placeholder*="apartment" i]',
                'input[placeholder*="suite" i]'
            ],
            'city': [
                'input[name*="city" i]',
                'input[id*="city" i]',
                'input[autocomplete="address-level2"]',
                'input[placeholder*="city" i]',
                '[role="combobox"][name*="city" i]',
                '[role="combobox"][id*="city" i]',
                'button[id*="city" i]',
                'button[class*="city" i]'
            ],
            'state': [
                'select[name*="state" i]',
                'select[name*="province" i]',
                'select[id*="state" i]',
                'input[name*="state" i]',
                'input[autocomplete="address-level1"]',
                '[role="combobox"][name*="state" i]',
                '[role="combobox"][id*="state" i]',
                'button[id*="state" i]',
                'button[class*="state" i]'
            ],
            'postal_code': [
                'input[name*="zip" i]',
                'input[name*="postal" i]',
                'input[name*="postcode" i]',
                'input[name*="pin" i]',
                'input[id*="zip" i]',
                'input[id*="pin" i]',
                'input[autocomplete="postal-code"]',
                'input[placeholder*="zip" i]',
                'input[placeholder*="postal" i]',
                'input[placeholder*="pin" i]',
                'input[placeholder*="pincode" i]'
            ],
            'country': [
                'select[name*="country" i]',
                'select[id*="country" i]',
                'input[name*="country" i]',
                'input[autocomplete="country"]',
                '[role="combobox"][name*="country" i]',
                '[role="combobox"][id*="country" i]',
                'button[id*="country" i]',
                'button[class*="country" i]'
            ]
        }
        
        # State/Province Mapping (Abbreviation -> Full Name)
        self.state_mappings = {
            Country.USA: {
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
            },
            Country.INDIA: {
                "AN": "Andaman and Nicobar Islands", "AP": "Andhra Pradesh", "AR": "Arunachal Pradesh",
                "AS": "Assam", "BR": "Bihar", "CH": "Chandigarh", "CT": "Chhattisgarh", 
                "DN": "Dadra and Nagar Haveli and Daman and Diu", "DL": "Delhi", "GA": "Goa",
                "GJ": "Gujarat", "HR": "Haryana", "HP": "Himachal Pradesh", "JK": "Jammu and Kashmir",
                "JH": "Jharkhand", "KA": "Karnataka", "KL": "Kerala", "LA": "Ladakh", "LD": "Lakshadweep",
                "MP": "Madhya Pradesh", "MH": "Maharashtra", "MN": "Manipur", "ML": "Meghalaya",
                "MZ": "Mizoram", "NL": "Nagaland", "OR": "Odisha", "PY": "Puducherry", "PB": "Punjab",
                "RJ": "Rajasthan", "SK": "Sikkim", "TN": "Tamil Nadu", "TG": "Telangana",
                "TR": "Tripura", "UP": "Uttar Pradesh", "UT": "Uttarakhand", "WB": "West Bengal"
            }
        }
        
        # Country Mapping (Code -> Common Variations)
        self.country_mappings = {
            "US": ["United States", "USA", "United States of America", "U.S.A.", "United States of America (USA)"],
            "IN": ["India"],
            "GB": ["United Kingdom", "UK", "Great Britain", "England", "U.K."],
            "AE": ["United Arab Emirates", "UAE", "Dubai", "Abu Dhabi", "U.A.E."],
            "CA": ["Canada"],
            "AU": ["Australia"],
            "PK": ["Pakistan"]
        }
    
    async def fill_address(self, profile: CheckoutProfile, max_retries: int = 5) -> Dict:
        """
        Fill address form. Tries SmartFormFiller (DOM-first, validated) first.
        Falls back to the VLM loop if critical fields fail.
        """
        self.logger.info(f"Starting address fill for {profile.country.name}")

        # Build field map for SmartFormFiller
        profile_data = {
            'email':        profile.email,
            'first_name':   profile.first_name,
            'last_name':    profile.last_name,
            'phone':        profile.phone,
            'address1':     profile.address_line1,
            'address2':     profile.address_line2 or '',
            'city':         profile.city,
            'state':        profile.state,
            'postal_code':  profile.postal_code,
            'country':      profile.country.value,
        }

        filler = SmartFormFiller(self.page, status_display=self.status)
        smart_result = await filler.fill_form(profile_data, max_retries=3)
        self.logger.info(f"SmartFormFiller result: {smart_result}")

        # If all fields OK, we are done
        if smart_result['success']:
            return {
                'success': True,
                'method': 'smart_form_filler',
                'fields': smart_result['results'],
            }

        failed_fields = smart_result.get('failed', [])
        self.logger.warning(
            f"SmartFormFiller failed for: {failed_fields} — falling back to VLM loop"
        )

        # ── VLM loop fallback (original logic below) ─────────────────────────
        # Re-define profile_data with old key names expected by VLM loop
        profile_data = {
            'email': profile.email,
            'first_name': profile.first_name,
            'last_name': profile.last_name,
            'full_name': f"{profile.first_name} {profile.last_name}".strip(),
            'phone': profile.phone,
            'address_line1': profile.address_line1,
            'address_line2': profile.address_line2,
            'city': profile.city,
            'state': profile.state,
            'postal_code': profile.postal_code,
            'country': profile.country.value
        }

        # Check if first_name field is the only name field on the page
        has_first_name_el = False
        has_last_name_el = False
        for pat in self.field_patterns['first_name']:
            try:
                if await self.page.query_selector(pat):
                    has_first_name_el = True
                    break
            except: pass
        for pat in self.field_patterns['last_name']:
            try:
                if await self.page.query_selector(pat):
                    has_last_name_el = True
                    break
            except: pass
        
        treat_first_as_full = has_first_name_el and not has_last_name_el

        filled_fields = set()
        # Initialize filled_fields with elements that are already populated on the page and match profile
        for field_name in profile_data:
            target_value = profile_data[field_name]
            if not target_value:
                continue
                
            if not await self._is_field_empty(field_name):
                current_val = await self._get_field_value(field_name)
                if current_val:
                    matches = await self._field_value_matches(field_name, current_val, target_value, profile.country)
                    if matches:
                        self.logger.info(f"Field '{field_name}' is already populated with matching value '{current_val}'. Marking as filled.")
                        filled_fields.add(field_name)
                        continue
                self.logger.info(f"Field '{field_name}' is prefilled with mismatching or empty value. Will refill/correct.")

        attempted_fields = set()
        field_failures = {} # field_name -> count
        loop_count = 0
        max_loops = 25
        
        # NEW: Global loop detection
        consecutive_no_progress_loops = 0
        last_filled_count = 0
        
        while loop_count < max_loops:
            loop_count += 1
            self.logger.info(f"--- VLM Loop {loop_count} ---")
            
            if self.status:
                await self.status.show(f"👁️ Scanning page (step {loop_count})...", "info", 1500)
            
            # PAYMENT MODAL GUARD: Stop ONLY when we have reached a standalone payment page
            # (no address fields visible). On Shopify single-page checkout, card inputs and
            # address fields coexist on the same page — do NOT stop in that case.
            try:
                payment_only_indicators = [
                    "iframe[title*='payment' i]",
                    "input[name*='card' i]",
                    "input[name*='cc-' i]",
                    "input[id*='card' i]",
                    "input[placeholder*='card number' i]",
                    "input[placeholder*='MM / YY' i]",
                    "input[placeholder*='CVV' i]",
                ]
                address_field_selectors = [
                    "input[name='firstName']", "input[name='first_name']",
                    "input[name='lastName']", "input[name='last_name']",
                    "input[name='address1']", "input[name='address']",
                    "input[autocomplete='given-name']", "input[autocomplete='address-line1']",
                    "input[name='email']", "input[type='email']",
                ]
                payment_visible = False
                for pay_sel in payment_only_indicators:
                    try:
                        els = await self.page.query_selector_all(pay_sel)
                        for el in els:
                            if await el.is_visible():
                                payment_visible = True
                                break
                    except:
                        pass
                    if payment_visible:
                        break

                if payment_visible:
                    # Check if address fields are ALSO present (single-page checkout)
                    address_present = False
                    for addr_sel in address_field_selectors:
                        try:
                            a_el = await self.page.query_selector(addr_sel)
                            if a_el and await a_el.is_visible():
                                address_present = True
                                break
                        except:
                            pass

                    if not address_present:
                        # Truly on a standalone payment page — stop
                        self.logger.info("🛑 PAYMENT PAGE DETECTED (no address fields visible). Stopping address fill loop.")
                        if self.status:
                            await self.status.show("🛑 Payment stage reached — stopping form fill.", "warning", 3000)
                        return {
                            "state": "address_filled",
                            "filled_fields": list(filled_fields),
                            "errors": ["Stopped at payment page"]
                        }
                    else:
                        self.logger.debug("Payment fields visible but address fields also present — single-page checkout, continuing fill.")
            except Exception as pm_err:
                self.logger.debug(f"Payment modal check error: {pm_err}")
            
            # 1. Map current layout
            vlm_map = await self._map_form_fields_with_vlm()
            fields_on_page = vlm_map.get('fields', {})
            next_button_coords = vlm_map.get('next_button')
            
            # Hallucination guard: if ≥3 fields share the EXACT same coordinates,
            # the VLM is making up data. Discard all coords so DOM fallback kicks in.
            if len(fields_on_page) >= 3:
                unique_coords = set(tuple(v) if isinstance(v, list) else v for v in fields_on_page.values())
                if len(unique_coords) == 1:
                    self.logger.warning(f"⚠️ VLM hallucination detected: {len(fields_on_page)} fields all at same coords {list(fields_on_page.values())[0]}. Discarding VLM coords.")
                    fields_on_page = {}
            
            # Sync filled_fields with actual page state
            for field_name in list(filled_fields):
                if await self._is_field_empty(field_name):
                    self.logger.info(f"Field {field_name} was marked as filled but is empty on page. Resetting filled status.")
                    filled_fields.remove(field_name)
                    continue
                    
                target_value = profile_data.get(field_name)
                if target_value:
                    current_val = await self._get_field_value(field_name)
                    if current_val:
                        matches = await self._field_value_matches(field_name, current_val, target_value, profile.country)
                        if not matches:
                            self.logger.info(f"Field {field_name} value changed to mismatching '{current_val}' (target '{target_value}'). Resetting filled status.")
                            filled_fields.remove(field_name)

            # Priority 1: Country filling/correcting first
            if 'country' in fields_on_page and 'country' not in filled_fields:
                coords = fields_on_page['country']
                value = profile_data['country']
                self.logger.info(f"Vision fill (PRIORITY 1): country at {coords}")
                attempted_fields.add('country')
                success = await self._fill_at_coordinates('country', value, coords, profile.country)
                if success:
                    filled_fields.add('country')
                    self.logger.info("Successfully filled country. Waiting 3s for form reload/update...")
                    await asyncio.sleep(3.0)
                    # Reset address fields to force re-verification (contact fields are unaffected by country reload)
                    address_fields = ('address_line1', 'address_line2', 'city', 'state', 'postal_code')
                    for f in list(filled_fields):
                        if f in address_fields:
                            filled_fields.remove(f)
                    continue
                    
            # Priority 2: State filling/correcting second
            if 'state' in fields_on_page and 'state' not in filled_fields:
                coords = fields_on_page['state']
                value = profile_data['state']
                self.logger.info(f"Vision fill (PRIORITY 2): state at {coords}")
                attempted_fields.add('state')
                success = await self._fill_at_coordinates('state', value, coords, profile.country)
                if success:
                    filled_fields.add('state')
                    self.logger.info("Successfully filled state. Waiting 3s for form reload/update...")
                    await asyncio.sleep(3.0)
                    # Reset remaining address fields except country and state
                    address_fields_to_reset = ('address_line1', 'address_line2', 'city', 'postal_code')
                    for f in list(filled_fields):
                        if f in address_fields_to_reset:
                            filled_fields.remove(f)
                    continue
            
            # 3. Fill EVERYTHING visible and not already successfully filled
            fields_filled_this_loop = 0
            for field_name, coords in fields_on_page.items():
                if field_name in profile_data and field_name not in filled_fields:
                    value = profile_data[field_name]
                    if not value: continue
                    
                    # Handle name field consolidation
                    if field_name == 'first_name' and treat_first_as_full:
                        value = profile_data['full_name']
                        self.logger.info("Treating 'first_name' field as 'full_name' since no 'last_name' field is present.")
                    
                    # Prevent retrying the same failure too many times in a row
                    if field_failures.get(field_name, 0) >= 2:
                        self.logger.warning(f"Skipping {field_name} due to repeated failures.")
                        continue
 
                    self.logger.info(f"Vision fill: {field_name} at {coords}")
                    attempted_fields.add(field_name)
                    success = await self._fill_at_coordinates(field_name, value, coords, profile.country)
                    
                    if success:
                        filled_fields.add(field_name)
                        fields_filled_this_loop += 1
                        field_failures[field_name] = 0 # reset
                        await asyncio.sleep(random.uniform(0.1, 0.3))
                    else:
                        field_failures[field_name] = field_failures.get(field_name, 0) + 1
            
            # NEW: Detect global stagnation
            current_filled_count = len(filled_fields)
            if current_filled_count == last_filled_count:
                consecutive_no_progress_loops += 1
                self.logger.warning(f"⚠️ No progress for {consecutive_no_progress_loops} consecutive loops")
                
                if consecutive_no_progress_loops >= 3:
                    self.logger.error("🚨 GLOBAL LOOP DETECTED: No progress for 3+ loops. Attempting recovery...")
                    await self._global_loop_recovery(profile_data, filled_fields)
                    consecutive_no_progress_loops = 0  # Reset after recovery
            else:
                consecutive_no_progress_loops = 0  # Reset on progress
            
            last_filled_count = current_filled_count
            
            # 4. DECISION: Should we scroll, click continue, or fallback?
            mandatory = ['email', 'first_name', 'last_name', 'address_line1', 'city', 'postal_code']
            if profile.state:
                mandatory.append('state')
            missing_mandatory = [f for f in mandatory if f not in filled_fields and profile_data.get(f)]
            
            # Split missing mandatory into: those visible on page vs those not visible at all
            missing_visible = [f for f in missing_mandatory if f in fields_on_page]
            missing_not_visible = [f for f in missing_mandatory if f not in fields_on_page]
            
            # Refined Decision Logic:
            # - If we see a 'Continue' button:
            #   - AND we still have missing mandatory fields that are NOT visible on the page -> SCROLL
            #   - AND all mandatory fields are either filled OR visible-but-failed -> CLICK CONTINUE
            
            if next_button_coords:
                # Only count fields that are visible AND not yet filled
                unfilled_visible = [f for f in fields_on_page if f not in filled_fields]
                has_unfilled_visible = len(unfilled_visible) > 0

                if missing_not_visible:
                    # Final layer of pragmatism: If we've scrolled several times and still don't see it,
                    # just click the button. It might be on the next page or misidentified.
                    if loop_count > 5:
                        self.logger.info(f"Patience exceeded for {missing_not_visible}. Clicking 'Continue' anyway.")
                    else:
                        self.logger.info(f"Fields {missing_not_visible} not visible. Scrolling...")
                        await self.page.mouse.wheel(0, 400)
                        await asyncio.sleep(1)
                        continue
                else:
                    # All visible fields are already filled (or none were visible) — proceed to next step
                    if not has_unfilled_visible:
                        self.logger.info("All visible fields are already filled. Clicking 'Continue' to advance to next section.")

                # If we've attempted everything visible (even if some failed), click continue
                self.logger.info(f"Proceeding: Fields filled={filled_fields}, visible-but-missing={missing_visible}")
                if self.status:
                    await self.status.show("🖱️ Proceeding to next step...", "info", 1000)
                
                await self._click_continue_button({'coordinates': next_button_coords})
                await asyncio.sleep(3.0) 
                continue 
            
            # 5. Fallback if VLM missed elements or no button found
            if fields_filled_this_loop == 0:
                self.logger.info("VLM found no new interactive elements. Trying DOM fallback...")
                
                # Try clicking any 'Continue' button found via selectors
                clicked = await self._click_continue_button({})
                if clicked:
                    await asyncio.sleep(2.0)
                    continue
                
                # Fill remaining via traditional selectors
                # Enforce Priority 1 (Country) and Priority 2 (State) in DOM fallback as well
                if 'country' not in filled_fields and profile_data.get('country'):
                    res = await self._fill_field('country', profile_data['country'], profile.country)
                    if res['success']:
                        filled_fields.add('country')
                        self.logger.info("Successfully filled country via DOM fallback. Waiting 3s for form reload/update...")
                        await asyncio.sleep(3.0)
                        address_fields = ('address_line1', 'address_line2', 'city', 'state', 'postal_code')
                        for f in list(filled_fields):
                            if f in address_fields:
                                filled_fields.remove(f)
                        continue
                        
                if 'state' not in filled_fields and profile_data.get('state'):
                    res = await self._fill_field('state', profile_data['state'], profile.country)
                    if res['success']:
                        filled_fields.add('state')
                        self.logger.info("Successfully filled state via DOM fallback. Waiting 3s for form reload/update...")
                        await asyncio.sleep(3.0)
                        address_fields_to_reset = ('address_line1', 'address_line2', 'city', 'postal_code')
                        for f in list(filled_fields):
                            if f in address_fields_to_reset:
                                filled_fields.remove(f)
                        continue

                dom_filled = 0
                for field_name, value in profile_data.items():
                    if field_name not in filled_fields and value:
                        # Don't try DOM if vision just failed twice
                        if field_failures.get(field_name, 0) >= 2: continue
                        
                        actual_value = value
                        if field_name == 'first_name' and treat_first_as_full:
                            actual_value = profile_data['full_name']
                            self.logger.info("DOM Fallback: Treating 'first_name' field as 'full_name' since no 'last_name' field is present.")
                        
                        res = await self._fill_field(field_name, actual_value, profile.country)
                        if res['success']:
                            filled_fields.add(field_name)
                            dom_filled += 1
                
                if dom_filled == 0:
                    self.logger.info("No more progress possible. Finishing loop.")
                    break
            
            await asyncio.sleep(0.5)


        if self.status:
            await self.status.show("✓ Checkout form processed!", "success", 2000)
        
        return {
            "state": "address_filled",
            "filled_fields": list(filled_fields),
            "errors": []
        }

    async def _is_field_empty(self, field_name: str) -> bool:
        """Checks if a field is empty on the page"""
        patterns = self.field_patterns.get(field_name, [])
        for pattern in patterns:
            try:
                el = await self.page.query_selector(pattern)
                if el and await el.is_visible():
                    val = await el.evaluate("el => el.value || el.innerText || el.textContent || ''")
                    val_str = val.strip().lower()
                    
                    # Common placeholder keywords that represent an unselected state/option
                    placeholders = ('select', 'choose', 'state', 'province', 'country', 'select a state', 'select state', 'select country', 'select a country')
                    if not val_str or any(p in val_str for p in placeholders) or val_str == field_name:
                        return True
                    else:
                        return False # Visible and has value, so not empty
            except:
                pass
        return False # Not visible or not found, do not treat as empty
    
    async def _fill_all_fields(self, profile: CheckoutProfile) -> Dict:
        """Fill all detected fields"""
        filled = []
        errors = []
        
        # Map profile to field values
        field_values = {
            'first_name': profile.first_name,
            'last_name': profile.last_name,
            'email': profile.email,
            'phone': profile.phone,
            'address_line1': profile.address_line1,
            'address_line2': profile.address_line2,
            'city': profile.city,
            'state': profile.state,
            'postal_code': profile.postal_code,
            'country': profile.country.value
        }
        
        for field_name, value in field_values.items():
            if not value:  # Skip empty fields
                continue
                
            if self.status:
                await self.status.show(f"📝 Filling {field_name.replace('_', ' ').title()}...", "info", 1000)
            
            result = await self._fill_field(field_name, value, profile.country)
            
            if result['success']:
                filled.append(field_name)
                self.logger.info(f"✓ Filled {field_name}")
            else:
                errors.append(f"Failed to fill {field_name}: {result.get('error')}")
                self.logger.warning(f"✗ Failed to fill {field_name}")
        
        return {'filled': filled, 'errors': errors}
    
    async def _fill_field(self, field_name: str, value: str, country: Country) -> Dict:
        """Fill a single field with human-like interaction"""
        patterns = self.field_patterns.get(field_name, [])
        
        for pattern in patterns:
            try:
                element = await self.page.query_selector(pattern)
                if not element or not await element.is_visible():
                    continue
                
                # Human-like: Scroll into view
                await element.scroll_into_view_if_needed()
                
                # Get element box for mouse movement
                box = await element.bounding_box()
                if box:
                    # Move mouse to center of field
                    cx, cy = box['x'] + box['width']/2, box['y'] + box['height']/2
                    await self.page.mouse.move(cx, cy, steps=3)
                    await asyncio.sleep(0.05)
                
                # Determine if it's a select or input
                tag_name = await element.evaluate('el => el.tagName.toLowerCase()')
                
                is_dropdown = await self._is_dropdown_element(element)
                if is_dropdown or field_name in ('state', 'country'):
                    success = await self._fill_dropdown(element, value, field_name, country)
                else:
                    # Handle input/textarea with human-like typing
                    await element.click()
                    await asyncio.sleep(0.05)
                    
                    # Faster typing: 20-40ms per char
                    await element.fill('')  # Clear
                    for char in value:
                        await element.type(char, delay=random.randint(20, 40))
                    
                    await asyncio.sleep(0.1)  # Faster pause
                    success = True
                
                if success:
                    return {'success': True, 'selector': pattern}
                    
            except Exception as e:
                self.logger.debug(f"Pattern {pattern} failed: {e}")
                continue
        
        return {'success': False, 'error': f'No visible field found for {field_name}'}
    
    async def _fill_dropdown(self, element, value: str, field_name: str, country: Country) -> bool:
        """Fill a native select or custom dropdown trigger with intelligence and fallbacks"""
        try:
            # 1. Native <select> handling
            is_native = await element.evaluate('el => el.tagName.toLowerCase() === "select"')

            # ── Guard: skip phone-country-code selects when filling 'country' ──
            # Phone code selects have options like "United States (+1)" — the
            # presence of "(+" in option text is the giveaway.
            if field_name == 'country' and is_native:
                is_phone_code_select = await element.evaluate("""el => {
                    const opts = Array.from(el.options).slice(0, 10).map(o => o.text);
                    return opts.some(t => /\(\+\d/.test(t));
                }""")
                if is_phone_code_select:
                    self.logger.info(f"Skipping phone-code <select> for field '{field_name}'")
                    return False

            # Get variations
            variations = [value]
            if field_name == 'state':
                variations = self._get_state_variations(value, country)
            elif field_name == 'country':
                variations = self._get_country_variations(value)
                
            self.logger.info(f"Filling dropdown for {field_name} (native={is_native}) with target values: {variations}")
            
            if is_native:
                options = await element.evaluate('''
                    (el) => Array.from(el.options).map(opt => ({
                        value: opt.value,
                        text: opt.text
                    }))
                ''')
                
                variations_lower = [v.lower() for v in variations]
                for opt in options:
                    opt_val = opt['value'].lower()
                    opt_text = opt['text'].lower()
                    
                    matched = False
                    for var in variations_lower:
                        if opt_val == var or opt_text == var:
                            matched = True
                        elif len(var) > 3 and (var in opt_text or opt_text in var):
                            matched = True
                            
                        if matched:
                            chosen_value = opt['value']
                            await element.select_option(value=chosen_value)
                            self.logger.info(f"Selected native option match for {field_name}: {opt['text']}")

                            # React-compatible commit: use the native HTMLSelectElement value
                            # setter (bypasses React's controlled-component lock) then fire
                            # the full event chain + invoke the React fiber's onChange directly.
                            await element.evaluate("""(el, val) => {
                                // 1. Native setter — lets React see a "real" value change
                                const nativeSetter = Object.getOwnPropertyDescriptor(
                                    window.HTMLSelectElement.prototype, 'value'
                                ).set;
                                nativeSetter.call(el, val);

                                // 2. Full event chain
                                ['input', 'change', 'blur'].forEach(type => {
                                    el.dispatchEvent(new Event(type, { bubbles: true }));
                                });

                                // 3. React fiber onChange (works for both React 16 and 17+)
                                const fiberKey = Object.keys(el).find(
                                    k => k.startsWith('__reactFiber') ||
                                         k.startsWith('__reactInternalInstance')
                                );
                                if (fiberKey) {
                                    const fiber = el[fiberKey];
                                    const props = fiber?.memoizedProps || fiber?.pendingProps;
                                    if (props?.onChange) {
                                        try {
                                            props.onChange({ target: el, currentTarget: el,
                                                            type: 'change', bubbles: true,
                                                            preventDefault: () => {} });
                                        } catch(e) {}
                                    }
                                }
                            }""", chosen_value)
                            return True
                
                # ── Keyboard navigation fallback ──────────────────────────────
                # Focus the select and repeatedly press the first letter of the
                # target until the selected option matches, then commit.
                self.logger.info(f"Native option match failed for {field_name} — trying keyboard navigation")
                try:
                    first_char = variations[0][0].lower() if variations else ''
                    if first_char:
                        await element.scroll_into_view_if_needed()
                        await element.click()
                        await asyncio.sleep(0.1)
                        for _attempt in range(30):  # max 30 presses
                            await element.press(first_char)
                            await asyncio.sleep(0.05)
                            sel_text = await element.evaluate(
                                "el => el.options[el.selectedIndex]?.text || ''"
                            )
                            sel_text_lower = sel_text.lower()
                            if any(v.lower() in sel_text_lower or sel_text_lower in v.lower()
                                   for v in variations):
                                self.logger.info(f"Keyboard navigation matched '{sel_text}' for {field_name}")
                                # Commit with React-compatible events
                                chosen_kb = await element.evaluate("el => el.value")
                                await element.evaluate("""(el, val) => {
                                    const ns = Object.getOwnPropertyDescriptor(
                                        window.HTMLSelectElement.prototype, 'value').set;
                                    ns.call(el, val);
                                    ['input','change','blur'].forEach(t =>
                                        el.dispatchEvent(new Event(t, {bubbles:true})));
                                    const fk = Object.keys(el).find(k =>
                                        k.startsWith('__reactFiber') ||
                                        k.startsWith('__reactInternalInstance'));
                                    if (fk) {
                                        const props = el[fk]?.memoizedProps || el[fk]?.pendingProps;
                                        if (props?.onChange) {
                                            try { props.onChange({target:el,currentTarget:el,
                                                type:'change',bubbles:true,preventDefault:()=>{}}); }
                                            catch(e) {}
                                        }
                                    }
                                }""", chosen_kb)
                                return True
                        self.logger.warning(f"Keyboard navigation exhausted for {field_name}")
                except Exception as _kb_err:
                    self.logger.warning(f"Keyboard navigation error for {field_name}: {_kb_err}")

            # 2. Custom Dropdown Selection
            self.logger.info(f"Opening custom dropdown trigger for {field_name}")
            await element.scroll_into_view_if_needed()
            await element.click()
            await asyncio.sleep(0.5)
            
            # Find options on the page using JavaScript, matching variations
            js_select = """
            (targetValues) => {
                const norm = (s) => s.toLowerCase().trim().replace(/\\s+/g, ' ');
                const targetsNorm = targetValues.map(v => norm(v));
                
                const containers = Array.from(document.querySelectorAll('[role="listbox"], [role="menu"], [class*="dropdown" i], [class*="menu" i], [class*="select" i]'))
                    .filter(el => {
                        const style = window.getComputedStyle(el);
                        return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null;
                    });
                    
                let candidates = [];
                for (const container of containers) {
                    const items = Array.from(container.querySelectorAll('[role="option"], [role="menuitem"], li, div, a, button'))
                        .filter(el => {
                            const style = window.getComputedStyle(el);
                            return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null;
                        });
                    candidates.push(...items);
                }
                
                if (candidates.length === 0) {
                    candidates = Array.from(document.querySelectorAll('[role="option"], [role="menuitem"], li, div[class*="option" i], button[class*="option" i]'))
                        .filter(el => {
                            const style = window.getComputedStyle(el);
                            return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null;
                        });
                }
                
                let best = null;
                let bestScore = 0;
                for (const cand of candidates) {
                    const text = cand.textContent || cand.innerText || '';
                    if (!text.trim()) continue;
                    
                    const candNorm = norm(text);
                    for (const targetNorm of targetsNorm) {
                        if (candNorm === targetNorm) {
                            best = cand;
                            bestScore = 1.0;
                            break;
                        }
                        if (candNorm.includes(targetNorm) || targetNorm.includes(candNorm)) {
                            const score = Math.min(candNorm.length, targetNorm.length) / Math.max(candNorm.length, targetNorm.length);
                            if (score > bestScore) {
                                best = cand;
                                bestScore = score;
                            }
                        }
                    }
                    if (bestScore === 1.0) break;
                }
                
                if (best) {
                    best.scrollIntoView({ block: 'center' });
                    best.click();
                    best.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                    return { success: true, text: best.textContent.trim(), score: bestScore };
                }
                return { success: false };
            }
            """
            
            res = None
            for attempt in range(3):
                res = await self.page.evaluate(js_select, variations)
                if res and res.get('success'):
                    break
                await asyncio.sleep(0.3)
                
            if res and res.get('success'):
                self.logger.info(f"Custom dropdown click succeeded for {field_name}: selected '{res.get('text')}' (score: {res.get('score'):.2f})")
                return True
                
            # 3. KEYBOARD FALLBACK (For searchable inputs or if option click failed)
            self.logger.info(f"Falling back to keyboard-input selection for {field_name}")
            await element.click(click_count=3)
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Backspace")
            
            type_val = variations[0]
            await self.page.keyboard.type(type_val, delay=25)
            await asyncio.sleep(0.5)
            
            # Press ArrowDown then Enter only if a suggestion dropdown is visible
            suggestions_selectors = [
                '[role="listbox"]:visible',
                '[role="menu"]:visible',
                '[class*="dropdown"]:visible',
                '[class*="menu"]:visible',
                '[class*="select"]:visible',
                '.pac-container:visible',
                '[class*="autocomplete"]:visible'
            ]
            
            dropdown_visible = False
            for sel in suggestions_selectors:
                try:
                    el = await self.page.query_selector(sel)
                    if el and await el.is_visible():
                        dropdown_visible = True
                        break
                except:
                    pass
                    
            if dropdown_visible:
                self.logger.info("Dropdown suggestions visible. Pressing ArrowDown and Enter.")
                await self.page.keyboard.press("ArrowDown")
                await asyncio.sleep(0.2)
                await self.page.keyboard.press("Enter")
                await asyncio.sleep(0.2)
            else:
                self.logger.info("No dropdown suggestions visible after typing.")
            
            await self.page.keyboard.press("Tab")
            
            # 4. VERIFY VALUE CHANGED
            new_val = await element.evaluate('el => el.value || el.innerText || el.textContent || ""')
            self.logger.info(f"Verified value for {field_name} after keyboard-fill: '{new_val.strip()}'")
            return True
            
        except Exception as e:
            self.logger.error(f"Dropdown fill failed for {field_name}: {e}")
            return False

    def _get_state_variations(self, value: str, country: Country) -> List[str]:
        """Get possible state/province names/abbreviations"""
        variations = [value]
        
        # Check country mapping
        mapping = self.state_mappings.get(country, {})
        
        # If input is abbreviation, add full name
        if value.upper() in mapping:
            variations.append(mapping[value.upper()])
        
        # If input is full name, add abbreviation
        for abbr, name in mapping.items():
            if name.lower() == value.lower():
                variations.append(abbr)
                break
                
        return variations
        
    def _get_country_variations(self, value: str) -> List[str]:
        """Get possible country names/codes"""
        variations = [value]
        # Check mapping (e.g., US -> "United States")
        mapping = self.country_mappings.get(value.upper(), [])
        variations.extend(mapping)
        return variations
    
    async def _detect_validation_errors(self) -> List[str]:
        """Detect validation error messages on page"""
        errors = []
        
        error_selectors = [
            '.error', '.field-error', '.validation-error',
            '[class*="error" i]', '[role="alert"]',
            '.invalid-feedback', '.form-error'
        ]
        
        for selector in error_selectors:
            try:
                elements = await self.page.query_selector_all(selector)
                for el in elements:
                    if await el.is_visible():
                        text = await el.text_content()
                        if text and text.strip():
                            errors.append(text.strip())
            except:
                continue
        
        return errors

    async def _visualize_click(self, x: float, y: float):
        """Show a blue circle at the click coordinates."""
        try:
            # Resolve JS assets directory relative to project root
            root_dir = Path(os.getcwd()).resolve()
            js_path = Path(__file__).resolve().parent / 'dom_assets' / 'visualize_click.js'
            
            if not js_path.exists():
                return
                
            js_visualize = js_path.read_text(encoding='utf-8')
            await self.page.evaluate(js_visualize, {'x': x, 'y': y})
        except: pass

    async def _dismiss_overlays(self):
        """Forcibly dismiss common checkout overlays (Autocomplete, Popups)"""
        try:
            # 1. Simple escape
            await self.page.keyboard.press("Escape")
            
            # 2. JS Removal of common blocking elements
            await self.page.evaluate("""() => {
                // Remove Google Autocomplete specifically
                document.querySelectorAll('.pac-container').forEach(el => el.remove());
                
                // Hide common popups (only if fixed/high z-index)
                document.querySelectorAll('div').forEach(el => {
                    const z = window.getComputedStyle(el).zIndex;
                    if (z !== 'auto' && parseInt(z) > 1000) {
                        el.style.display = 'none';
                    }
                });
            }""")
            await asyncio.sleep(0.3)
        except: pass

    async def _detect_autocomplete_type(self, element) -> str:
        """Detect what type of autocomplete is present on this field"""
        try:
            # Check for Google Places API
            has_google_places = await self.page.evaluate("""
                () => {
                    return typeof google !== 'undefined' && 
                           typeof google.maps !== 'undefined' && 
                           typeof google.maps.places !== 'undefined';
                }
            """)
            
            if has_google_places:
                # Check if this specific element has autocomplete
                autocomplete_attr = await element.get_attribute('autocomplete')
                classes = await element.get_attribute('class') or ''
                
                if 'pac-target-input' in classes or autocomplete_attr == 'off':
                    return 'google_places'
            
            # Check for generic autocomplete dropdown
            await asyncio.sleep(0.3)
            dropdown_visible = await self.page.query_selector('.pac-container:visible, [class*="autocomplete"]:visible')
            if dropdown_visible:
                return 'generic_autocomplete'
                
            return 'none'
        except:
            return 'unknown'

    def _is_looping(self, field_name: str, current_value: str) -> bool:
        """Detect if we're stuck in a loop on this field"""
        if field_name not in self.field_attempts:
            self.field_attempts[field_name] = {
                'count': 0, 
                'strategies': [], 
                'last_value': None,
                'success': False
            }
        
        attempt_info = self.field_attempts[field_name]
        
        # Check if we're attempting the same field repeatedly
        if attempt_info['count'] >= self.loop_detection_threshold:
            self.logger.warning(f"🔄 Loop detected on {field_name}: {attempt_info['count']} attempts")
            return True
        
        # Check if value hasn't changed despite multiple attempts
        if attempt_info['last_value'] == current_value and attempt_info['count'] > 1:
            self.logger.warning(f"🔄 Value unchanged for {field_name} after {attempt_info['count']} attempts")
            return True
        
        return False

    def _record_attempt(self, field_name: str, strategy: str, value: str, success: bool):
        """Record an attempt for loop detection"""
        if field_name not in self.field_attempts:
            self.field_attempts[field_name] = {
                'count': 0, 
                'strategies': [], 
                'last_value': None,
                'success': False
            }
        
        self.field_attempts[field_name]['count'] += 1
        self.field_attempts[field_name]['strategies'].append(strategy)
        self.field_attempts[field_name]['last_value'] = value
        self.field_attempts[field_name]['success'] = success
        
        if success:
            # Reset counter on success
            self.field_attempts[field_name]['count'] = 0
            self.logger.info(f"✓ {field_name} filled successfully with strategy: {strategy}")
        else:
            self.logger.warning(f"✗ {field_name} attempt {self.field_attempts[field_name]['count']} failed with strategy: {strategy}")

    async def _global_loop_recovery(self, profile_data: dict, filled_fields: set):
        """Emergency recovery when stuck in global loop"""
        self.logger.info("🚨 Executing global loop recovery...")
        
        # 1. Clear all autocomplete state
        try:
            await self.page.evaluate("""
                () => {
                    document.querySelectorAll('.pac-container').forEach(el => el.remove());
                    document.querySelectorAll('input').forEach(input => {
                        input.setAttribute('autocomplete', 'off');
                    });
                }
            """)
        except:
            pass
        
        # 2. Reset field attempt counters
        self.field_attempts = {}
        
        # 3. Scroll to reset viewport
        try:
            await self.page.mouse.wheel(0, -500)
            await asyncio.sleep(1)
            await self.page.mouse.wheel(0, 500)
            await asyncio.sleep(1)
        except:
            pass
        
        # 4. Try direct injection for all remaining mandatory fields
        mandatory = ['email', 'first_name', 'last_name', 'address_line1', 'city', 'postal_code']
        if profile_data.get('state'):
            mandatory.append('state')
        for field in mandatory:
            if field not in filled_fields and field in profile_data and profile_data[field]:
                self.logger.info(f"🔧 Recovery: Direct injection for {field}")
                patterns = self.field_patterns.get(field, [])
                for pattern in patterns:
                    try:
                        elem = await self.page.query_selector(pattern)
                        if elem and await elem.is_visible():
                            await self._strategy_direct_injection(elem, profile_data[field])
                            filled_fields.add(field)
                            break
                    except:
                        continue
        
        self.logger.info("✓ Recovery complete")



    async def _strategy_accept_suggestion(self, field_name: str) -> bool:
        """Wait for autocomplete and click first suggestion"""
        try:
            self.logger.info(f"📋 Strategy: Accepting first autocomplete suggestion for {field_name}")
            
            # Wait for dropdown to appear
            await asyncio.sleep(0.6)
            
            # Find suggestions
            suggestions = await self.page.query_selector_all(
                ".pac-item, .pac-item-query, [class*='autocomplete'] .item, .address-suggestion"
            )
            
            if suggestions and len(suggestions) > 0:
                self.logger.info(f"Found {len(suggestions)} suggestions, clicking first")
                await suggestions[0].click()
                await asyncio.sleep(1.5)  # Wait for autofill
                await self.page.keyboard.press("Escape")
                return True
            else:
                self.logger.warning("No autocomplete suggestions found")
                await self.page.keyboard.press("Escape")
                return False
                
        except Exception as e:
            self.logger.error(f"Accept suggestion strategy failed: {e}")
            return False

    async def _strategy_dismiss_autocomplete(self, field_name: str) -> bool:
        """Aggressively dismiss autocomplete and move on"""
        try:
            self.logger.info(f"🚫 Strategy: Dismissing autocomplete for {field_name}")
            
            # Multiple dismissal attempts
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
            
            # Click away from the field
            await self.page.mouse.click(100, 100)  # Click top-left corner
            await asyncio.sleep(0.3)
            
            # Remove dropdown via JavaScript
            await self.page.evaluate("""
                () => {
                    document.querySelectorAll('.pac-container').forEach(el => el.remove());
                    document.querySelectorAll('[class*="autocomplete"]').forEach(el => {
                        if (el.style.position === 'absolute' || el.style.position === 'fixed') {
                            el.style.display = 'none';
                        }
                    });
                }
            """)
            
            await asyncio.sleep(0.2)
            await self.page.keyboard.press("Tab")  # Move to next field
            return True
            
        except Exception as e:
            self.logger.error(f"Dismiss strategy failed: {e}")
            return False

    async def _strategy_disable_autocomplete(self, element, value: str) -> bool:
        """Disable autocomplete attribute and refill"""
        try:
            self.logger.info("⚙️ Strategy: Disabling autocomplete attribute")
            
            # Disable autocomplete on the element
            await element.evaluate("""
                (el) => {
                    el.setAttribute('autocomplete', 'off');
                    el.setAttribute('autocomplete', 'new-password'); // Chrome hack
                    
                    // Disable Google Places if attached
                    if (el.getAttribute('class')?.includes('pac-target-input')) {
                        el.classList.remove('pac-target-input');
                    }
                }
            """)
            
            await asyncio.sleep(0.2)
            
            # Clear and refill
            await element.click()
            await element.fill('')
            await element.type(value, delay=random.randint(20, 40))
            await asyncio.sleep(0.3)
            await self.page.keyboard.press("Tab")
            
            return True
            
        except Exception as e:
            self.logger.error(f"Disable autocomplete strategy failed: {e}")
            return False

    async def _strategy_direct_injection(self, element, value: str) -> bool:
        """Bypass typing entirely - set value directly via JavaScript"""
        try:
            self.logger.info("💉 Strategy: Direct value injection (bypassing autocomplete)")
            
            # Set value directly
            await element.evaluate(
                """(el, val) => {
                    el.value = val;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                }""",
                value
            )
            
            await asyncio.sleep(0.2)
            
            # Verify value was set
            current_value = await element.evaluate('el => el.value')
            if current_value == value:
                self.logger.info(f"Direct injection successful: {value}")
                await self.page.keyboard.press("Tab")
                return True
            else:
                self.logger.warning(f"Direct injection verification failed: expected '{value}', got '{current_value}'")
                return False
                
        except Exception as e:
            self.logger.error(f"Direct injection strategy failed: {e}")
            return False

    async def _handle_autocomplete_smart(self, field_name: str, element, value: str) -> bool:
        """
        Intelligent autocomplete handler with multiple fallback strategies
        """
        autocomplete_type = await self._detect_autocomplete_type(element)
        
        if autocomplete_type == 'none':
            return True  # No autocomplete, proceed normally
        
        self.logger.info(f"🔍 Detected {autocomplete_type} on {field_name}")
        
        # Get attempt history
        attempt_count = self.field_attempts.get(field_name, {}).get('count', 0)
        strategies_tried = self.field_attempts.get(field_name, {}).get('strategies', [])
        
        # Strategy selection based on attempt count
        if attempt_count == 0:
            strategy = 'accept_first_suggestion'
        elif attempt_count == 1:
            strategy = 'dismiss_and_continue'
        elif attempt_count == 2:
            strategy = 'disable_autocomplete'
        else:
            strategy = 'direct_value_injection'
        
        self.logger.info(f"🎯 Using strategy '{strategy}' for {field_name} (attempt {attempt_count + 1})")
        
        success = False
        
        if strategy == 'accept_first_suggestion':
            success = await self._strategy_accept_suggestion(field_name)
        elif strategy == 'dismiss_and_continue':
            success = await self._strategy_dismiss_autocomplete(field_name)
        elif strategy == 'disable_autocomplete':
            success = await self._strategy_disable_autocomplete(element, value)
        elif strategy == 'direct_value_injection':
            success = await self._strategy_direct_injection(element, value)
        
        return success

    async def _map_form_fields_locally(self) -> Dict:
        """
        Local Spatial-DOM Correlation Engine.
        Finds visible form fields and correlates them using keyword matching and spatial proximity.
        Runs entirely in JS in the browser (zero API cost, zero network latency).
        """
        js_script = """
        () => {
            const fieldMapping = {};
            const inputs = Array.from(document.querySelectorAll('input, select, textarea, button, [role="combobox"], [aria-haspopup="listbox"]'));
            
            const normalizeCoords = (rect) => {
                const x = Math.round((rect.left + rect.width / 2) / window.innerWidth * 1000);
                const y = Math.round((rect.top + rect.height / 2) / window.innerHeight * 1000);
                return [x, y];
            };
            
            const labelRules = {
                'email': [/email/i, /e-mail/i, /mail/i],
                'phone': [/phone/i, /mobile/i, /contact/i, /telephone/i, /number/i],
                'first_name': [/first\\s*name/i, /given\\s*name/i, /forename/i],
                'last_name': [/last\\s*name/i, /family\\s*name/i, /surname/i],
                'full_name': [/full\\s*name/i, /^name$/i, /^name\\s+/i, /billing\\s*name/i, /shipping\\s*name/i],
                'address_line1': [/address\\s*line\\s*1/i, /street\\s*address/i, /^address$/i, /address\\s*\\(street/i],
                'address_line2': [/address\\s*line\\s*2/i, /apartment/i, /suite/i, /unit/i, /building/i],
                'city': [/city/i, /town/i, /suburb/i],
                'state': [/state/i, /province/i, /region/i, /territory/i],
                'postal_code': [/zip/i, /postal/i, /postcode/i, /pin\\s*code/i, /pincode/i],
                'country': [/country/i, /location/i]
            };
            
            const nextButtonKeywords = [
                /continue/i, /next/i, /proceed/i, /deliver/i, /ship/i, /pay/i, /checkout/i, /save/i
            ];
            
            const isVisible = (el) => {
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || el.offsetParent === null) return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 2 && rect.height > 2 && rect.bottom >= 0 && rect.top <= window.innerHeight;
            };
            
            const getLabelsForElement = (el) => {
                const labels = [];
                
                if (el.placeholder) labels.push(el.placeholder);
                if (el.name) labels.push(el.name);
                if (el.id) {
                    labels.push(el.id);
                    const linkedLabel = document.querySelector(`label[for="${el.id}"]`);
                    if (linkedLabel) labels.push(linkedLabel.textContent);
                }
                if (el.getAttribute('aria-label')) labels.push(el.getAttribute('aria-label'));
                if (el.getAttribute('aria-labelledby')) {
                    const labelledby = document.getElementById(el.getAttribute('aria-labelledby'));
                    if (labelledby) labels.push(labelledby.textContent);
                }
                
                let runner = el;
                for (let i = 0; i < 3; i++) {
                    if (!runner) break;
                    const runnerText = runner.innerText || runner.textContent;
                    if (runnerText) {
                        labels.push(runnerText.substring(0, 100));
                    }
                    runner = runner.parentElement;
                }
                
                return labels.join(' | ');
            };
            
            inputs.forEach(input => {
                if (!isVisible(input)) return;
                
                const labelText = getLabelsForElement(input);
                const rect = input.getBoundingClientRect();
                
                for (const [field, regexes] of Object.entries(labelRules)) {
                    if (fieldMapping[field]) continue;
                    
                    const matches = regexes.some(regex => regex.test(labelText));
                    if (matches) {
                        fieldMapping[field] = normalizeCoords(rect);
                        break;
                    }
                }
            });
            
            let nextButtonCoords = null;
            const buttons = Array.from(document.querySelectorAll('button, a, input[type="submit"], input[type="button"]'));
            let bestBtnScore = -1;
            
            buttons.forEach(btn => {
                if (!isVisible(btn)) return;
                const btnText = btn.textContent || btn.value || btn.innerText || '';
                if (!btnText) return;
                
                nextButtonKeywords.forEach((regex, idx) => {
                    if (regex.test(btnText)) {
                        let score = (10 - idx);
                        if (btn.type === 'submit') score += 5;
                        if (score > bestBtnScore) {
                            bestBtnScore = score;
                            nextButtonCoords = normalizeCoords(btn.getBoundingClientRect());
                        }
                    }
                });
            });
            
            return {
                "fields": fieldMapping,
                "next_button": nextButtonCoords
            };
        }
        """
        try:
            return await self.page.evaluate(js_script)
        except Exception as e:
            self.logger.warning(f"JS evaluation for local form mapping failed: {e}")
            return {}

    async def _map_form_fields_with_vlm(self) -> Dict:
        """Scan entire form once to map all field coordinates"""
        # Try local Spatial-DOM Correlation Engine first
        try:
            local_map = await self._map_form_fields_locally()
            fields = local_map.get('fields', {})
            self.logger.info(f"Local Spatial-DOM mapping found {len(fields)} fields: {list(fields.keys())}")
            
            # If we successfully found at least 3 fields, skip LLM calls entirely!
            if len(fields) >= 3:
                self.logger.info("Found >= 3 fields via local Spatial-DOM engine. Skipping LLM vision API call.")
                return local_map
            else:
                self.logger.info(f"Local mapping only found {len(fields)} fields. Falling back to VLM...")
        except Exception as e:
            self.logger.warning(f"Local Spatial-DOM mapping failed: {e}. Falling back to VLM...")

        if not self.llm_client: return {}
        
        screenshot_path = 'temp_form_map.png'
        await self._dismiss_overlays()
        await self.page.screenshot(path=screenshot_path, timeout=60000)
        
        prompt = """SCAN THIS CHECKOUT FORM. I need to find the COORDINATES for the following fields to complete the purchase. 
        Look for any visible input boxes, dropdowns, or clickable areas associated with these labels.
        
        Required fields to find (if visible):
        - email (Contact email)
        - phone (digits only)
        - first_name
        - last_name
        - address_line1 (Street number and name)
        - address_line2 (Apartment, suite, etc.)
        - city
        - state (Dropdown or input)
        - postal_code (Zip code)
        - country (Dropdown)
        
        Also identify the 'Continue to shipping', 'Continue to payment', or 'Pay now' button.
        
        Use a 0-1000 scale for [x, y] coordinates. Return valid JSON only:
        {"fields": {"field_name": [x, y], ...}, "next_button": [x, y]}
        
        If a field is already filled or not present, omit it from the JSON.
        """
        
        try:
            response = await self.llm_client.complete_with_vision(prompt, screenshot_path)
            self.logger.info(f"VLM Mapping Response: {response}")
            
            # Detect failure (API error or empty response)
            if not response or '"error"' in response or '429' in response or '402' in response:
                self.logger.warning("VLM Vision failed/limited. Falling back to DOM Analysis...")
                return await self._map_form_fields_with_dom()
            
            import json
            if '{' in response:
                json_str = response[response.find('{'):response.rfind('}')+1]
                return json.loads(json_str)
        except Exception as e:
            self.logger.warning(f"VLM mapping failed: {e}. Trying DOM fallback...")
            return await self._map_form_fields_with_dom()
        
        return await self._map_form_fields_with_dom() # Final fallback

    async def _map_form_fields_with_dom(self) -> Dict:
        """Fallback: Scan DOM and use text-only LLM to map fields"""
        if not self.llm_client: return {}
        
        self.logger.info("Extracting visible form fields from DOM for text-based analysis...")
        
        # 1. Extract visible interactive elements
        elements_data = await self.page.evaluate("""
            () => {
                const results = [];
                const interactive = document.querySelectorAll('input, select, textarea, button, [role="combobox"], [aria-haspopup="listbox"]');
                
                interactive.forEach((el, index) => {
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' || el.offsetParent === null) return;
                    
                    // Skip inputs inside header, nav, footer, or search bars
                    if (el.closest('header, nav, footer, #nav-belt, #navbar, #nav-bar, #nav-main, #nav-tools, #g-navigation, .global-header, .global-footer')) {
                        return;
                    }
                    const elName = (el.name || '').toLowerCase();
                    const elId = (el.id || '').toLowerCase();
                    const elType = (el.type || '').toLowerCase();
                    if (elName === 'url' || elName === 'field-keywords' || elName === 'site-search' || elType === 'search' || elName.includes('search') || elId.includes('search')) {
                        return;
                    }
                    
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 2 || rect.height < 2) return;
                    
                    // CRITICAL: Only include elements reasonably within the viewport
                    if (rect.bottom < 0 || rect.top > window.innerHeight) return;
                    
                    // Normalize to 0-1000
                    const x = Math.round((rect.left + rect.width/2) / window.innerWidth * 1000);
                    const y = Math.round((rect.top + rect.height/2) / window.innerHeight * 1000);
                    
                    // Get label or placeholder
                    let label = "";
                    if (el.id) {
                        const lbl = document.querySelector(`label[for="${el.id}"]`);
                        if (lbl) label = lbl.innerText;
                    }
                    if (!label && el.placeholder) label = el.placeholder;
                    if (!label && el.name) label = el.name;
                    if (!label && el.innerText) label = el.innerText;
                    
                    results.push({
                        id: el.id || '',
                        name: el.name || '',
                        type: el.tagName.toLowerCase(),
                        placeholder: el.placeholder || '',
                        label: label.trim(),
                        coords: [x, y],
                        value: el.value || ''
                    });
                });
                return results;
            }
        """)
        
        if not elements_data:
            self.logger.warning("No visible interactive elements found in DOM.")
            return {}
            
        # 2. Build Text Context for LLM
        context_str = "\\n".join([
            f"- [{i}] <{e['type']}> label: '{e['label']}' | name: '{e['name']}' | placeholder: '{e['placeholder']}' | coords: {e['coords']}"
            for i, e in enumerate(elements_data)
        ])
        
        prompt = f"""ANALYSIS OF CHECKOUT FORM ELEMENTS.
        Identify which of these DOM elements corresponds to standard checkout fields.
        
        Fields to find:
        - email, phone, first_name, last_name, address_line1, address_line2, city, state, postal_code, country
        - next_button (Submit, Continue, Next, Pay)
        
        Form Elements found:
        {context_str}
        
        Return ONLY valid JSON:
        {{"fields": {{"field_name": [x, y], ...}}, "next_button": [x, y]}}
        
        Use the exact [x, y] coordinates provided in the element list.
        """
        
        try:
            # Use text completion (Higher rate limits)
            response = await self.llm_client.complete(prompt, temperature=0.1)
            self.logger.info(f"DOM Mapping Response: {response}")
            
            import json
            if '{' in response:
                json_str = response[response.find('{'):response.rfind('}')+1]
                return json.loads(json_str)
        except Exception as e:
            self.logger.error(f"DOM mapping LLM failed: {e}")
            
        return {}

    async def _fill_at_coordinates(self, field_name: str, value: str, coords: List[int], country: Country) -> bool:
        """Click and fill field at specific vision coordinates with loop prevention"""
        try:
            # Check for loops BEFORE attempting
            if self._is_looping(field_name, value):
                self.logger.error(f"🔄 LOOP DETECTED for {field_name}. Skipping to prevent infinite loop.")
                return False
            
            viewport = self.page.viewport_size
            if not viewport: viewport = {'width': 1280, 'height': 720}
            
            # Normalize 0-1000 coordinates to absolute pixels (supports [x, y] or [xmin, ymin, xmax, ymax] bounding boxes)
            x, y = coords[0], coords[1]
            if len(coords) == 4:
                x = (coords[0] + coords[2]) / 2
                y = (coords[1] + coords[3]) / 2
            pixel_x = (x / 1000) * viewport['width']
            pixel_y = (y / 1000) * viewport['height']
            
            self.logger.info(f"Vision fill: moving to pixel {pixel_x}, {pixel_y} for {field_name}")
            await self._visualize_click(pixel_x, pixel_y)
            
            # 1. Smooth move and click
            await self.page.mouse.move(pixel_x, pixel_y, steps=10)
            await self.page.mouse.click(pixel_x, pixel_y)
            await asyncio.sleep(0.2)
            
            # 2. Get the actual interactive element at that point (or near it)
            # Find the best element to interact with
            element_handle = await self.page.evaluate_handle("""
                ([x, y]) => {
                    let el = document.elementFromPoint(x, y);
                    if (!el) return null;
                    
                    // If we hit a label or decorative element, find the associated input/select
                    if (el.tagName === 'LABEL') {
                        let target = el.getAttribute('for') ? document.getElementById(el.getAttribute('for')) : el.querySelector('input, select, textarea');
                        if (target) return target;
                    }
                    
                    // Walk up to find common wrapper patterns
                    let runner = el;
                    for(let i=0; i<3; i++) {
                        if (['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON'].includes(runner.tagName)) return runner;
                        if (runner.getAttribute('role') === 'combobox' || runner.getAttribute('aria-haspopup') === 'listbox') return runner;
                        let inner = runner.querySelector('input, select, textarea, button, [role="combobox"], [aria-haspopup="listbox"]');
                        if (inner) return inner;
                        runner = runner.parentElement;
                        if (!runner) break;
                    }
                    
                    return el;
                }
            """, [pixel_x, pixel_y])
            
            if not element_handle:
                self._record_attempt(field_name, 'coordinate_click', value, False)
                return False
                
            element = element_handle.as_element()
            if not element:
                self._record_attempt(field_name, 'coordinate_click', value, False)
                return False

            tag_name = await element.evaluate('el => el.tagName.toLowerCase()')
            self.logger.info(f"Targeting <{tag_name}> at {pixel_x}, {pixel_y}")
            
            # Ensure focused
            try:
                await element.focus()
            except: pass
            
            # 3. Handle by type
            is_dropdown = await self._is_dropdown_element(element)
            if is_dropdown or field_name in ('state', 'country'):
                success = await self._fill_dropdown(element, value, field_name, country)
                self._record_attempt(field_name, 'dropdown_fill', value, success)
                return success
            else:
                # Robust Filling Strategy
                # 1. Click to focus
                await element.click(delay=50)
                
                # 2. Clear existing
                await element.click(click_count=3) # Select all
                await self.page.keyboard.press("Control+A") # Backup select all
                await self.page.keyboard.press("Backspace")
                
                # 3. Human-like typing
                for char in str(value):
                    await self.page.keyboard.type(char, delay=random.randint(5, 20))
                
                # 4. NEW: Smart autocomplete handling for address fields
                if field_name in ['address_line1', 'address_line2', 'city']:
                    success = await self._handle_autocomplete_smart(field_name, element, value)
                else:
                    # Non-address fields: just dismiss any overlays
                    await self.page.keyboard.press("Escape")
                    await self.page.keyboard.press("Tab")
                    success = True
                
                await asyncio.sleep(0.2)
                self._record_attempt(field_name, 'type_and_autocomplete', value, success)
                return success
                
        except Exception as e:
            self.logger.warning(f"Coordinate fill failed for {field_name}: {e}")
            self._record_attempt(field_name, 'exception', value, False)
            return False


    async def _click_continue_button(self, vlm_data: Dict) -> bool:
        """Find and click continue button using VLM coordinates or selectors"""
        try:
            # Record initial page state to detect transitions
            initial_url = self.page.url
            initial_visible_fields = await self.page.evaluate("""
                () => Array.from(document.querySelectorAll('input, select, textarea'))
                    .filter(el => {
                        const style = window.getComputedStyle(el);
                        return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null;
                    }).map(el => el.name || el.id || el.placeholder || '')
            """)
            
            clicked = False
            # 1. Try VLM Coordinates if provided
            coords = vlm_data.get('coordinates')
            if coords:
                viewport = self.page.viewport_size
                if not viewport: viewport = {'width': 1280, 'height': 720}
                
                # Normalize 0-1000 coordinates to absolute pixels (supports [x, y] or [xmin, ymin, xmax, ymax] bounding boxes)
                x, y = coords[0], coords[1]
                if len(coords) == 4:
                    x = (coords[0] + coords[2]) / 2
                    y = (coords[1] + coords[3]) / 2
                pixel_x = (x / 1000) * viewport['width']
                pixel_y = (y / 1000) * viewport['height']
                
                self.logger.info(f"Clicking Continue at pixel: {pixel_x}, {pixel_y}")
                await self._visualize_click(pixel_x, pixel_y)
                await self.page.mouse.move(pixel_x, pixel_y, steps=10)
                
                # Find the actual element to click
                element_handle = await self.page.evaluate_handle("""
                    ([x, y]) => {
                        let el = document.elementFromPoint(x, y);
                        if (!el) return null;
                        let runner = el;
                        for(let i=0; i<5; i++) {
                            if (['BUTTON', 'A', 'INPUT'].includes(runner.tagName)) return runner;
                            let inner = runner.querySelector('button, a, input[type="submit"]');
                            if (inner) return inner;
                            runner = runner.parentElement;
                            if (!runner) break;
                        }
                        return el;
                    }
                """, [pixel_x, pixel_y])
                
                if element_handle:
                    element = element_handle.as_element()
                    if element:
                        self.logger.info("Found button element via coordinates. Clicking...")
                        await element.click(delay=100, force=True)
                        # Also dispatch events for picky buttons
                        await element.evaluate("(el) => { el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true})); el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true})); el.click(); }")
                        clicked = True

                if not clicked:
                    # Fallback to direct mouse click if no handle
                    await self.page.mouse.click(pixel_x, pixel_y)
                    clicked = True
                
            if not clicked:
                # 2. Selector-based Fallback with expanded selectors
                button_selectors = [
                    'button:has-text("Continue to payment")',
                    'button:has-text("Continue to Payment")',
                    'button:has-text("Proceed to Payment")',
                    'button:has-text("Deliver to this address")',
                    'button:has-text("Deliver here")',
                    'button:has-text("Proceed to shipping")',
                    'button:has-text("Continue to shipping")',
                    'button:has-text("Use this address")',
                    'button:has-text("Ship to this address")',
                    'button:has-text("Save & Continue")',
                    'button:has-text("Save and Continue")',
                    'button:has-text("Continue")',
                    'button:has-text("Next")',
                    'button:has-text("Proceed")',
                    'button[type="submit"]',
                    '.checkout-button',
                    'a:has-text("Continue")',
                    '[data-testid*="continue" i]',
                    '[data-testid*="submit" i]',
                    '[id*="continue" i]',
                    '[class*="continue" i]'
                ]
                
                for selector in button_selectors:
                    try:
                        element = await self.page.query_selector(selector)
                        if element and await element.is_visible():
                            self.logger.info(f"Clicking Continue via selector: {selector}")
                            await element.scroll_into_view_if_needed()
                            await element.click(delay=100, force=True)
                            # Backup event dispatch
                            await element.evaluate("(el) => { el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true})); el.click(); }")
                            clicked = True
                            break
                    except:
                        continue
            
            if clicked:
                # Wait for potential AJAX / page loads
                await asyncio.sleep(2.5)
                
                # Verify transition
                current_url = self.page.url
                if current_url != initial_url:
                    self.logger.info(f"Page transition confirmed: URL changed from {initial_url} to {current_url}")
                    return True
                
                # Check for new visible fields (e.g. shipping options, or credit card inputs)
                current_visible_fields = await self.page.evaluate("""
                    () => Array.from(document.querySelectorAll('input, select, textarea'))
                        .filter(el => {
                            const style = window.getComputedStyle(el);
                            return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null;
                        }).map(el => el.name || el.id || el.placeholder || '')
                """)
                
                # Check if fields changed (accordion expansion/collapse)
                new_fields = set(current_visible_fields) - set(initial_visible_fields)
                if new_fields:
                    self.logger.info(f"Accordion transition confirmed! New visible fields detected: {list(new_fields)}")
                    return True
                
                removed_fields = set(initial_visible_fields) - set(current_visible_fields)
                if removed_fields:
                    self.logger.info(f"Accordion transition confirmed! Fields disappeared: {list(removed_fields)}")
                    return True
                
                self.logger.info("Button clicked, but page URL and visible form fields remained the same.")
                return True
            
            return False
        except Exception as e:
            self.logger.warning(f"Continue button click failed: {e}")
            return False

    async def _is_dropdown_element(self, element) -> bool:
        """Determines if the element behaves as a native or custom dropdown/combobox"""
        try:
            tag_name = await element.evaluate('el => el.tagName.toLowerCase()')
            if tag_name == 'select':
                return True
            
            # Check for combobox role or popup listbox or aria-autocomplete
            role = await element.get_attribute('role')
            aria_haspopup = await element.get_attribute('aria-haspopup')
            aria_autocomplete = await element.get_attribute('aria-autocomplete')
            
            if role == 'combobox' or aria_haspopup in ('listbox', 'true') or aria_autocomplete in ('list', 'both'):
                return True
                
            # Check classes/ids/names for select/dropdown/combobox/listbox keywords
            cls = await element.get_attribute('class') or ''
            el_id = await element.get_attribute('id') or ''
            name = await element.get_attribute('name') or ''
            
            cls_lower = cls.lower()
            el_id_lower = el_id.lower()
            name_lower = name.lower()
            
            if any(k in cls_lower or k in el_id_lower or k in name_lower for k in ('select', 'dropdown', 'combobox', 'listbox')):
                return True
                
        except Exception as e:
            self.logger.debug(f"Error checking if element is dropdown: {e}")
            
        return False

    async def _field_value_matches(self, field_name: str, current_val: str, target_value: str, country: Country) -> bool:
        """
        Compare current field value against target, handling aliases for country,
        state, phone, and plain text fields.
        """
        cur = (current_val or '').strip().lower()
        tgt = (target_value or '').strip().lower()
        if not cur or not tgt:
            return False

        # Exact match first
        if cur == tgt:
            return True

        if field_name == 'country':
            # Accept any alias in the country variations list
            variations = [v.lower() for v in self._get_country_variations(target_value)]
            return cur in variations or any(v in cur for v in variations)

        if field_name == 'state':
            variations = [v.lower() for v in self._get_state_variations(target_value, country)]
            return cur in variations or any(v in cur for v in variations)

        if field_name == 'phone':
            # Strip all non-digit characters and compare
            cur_digits = ''.join(c for c in cur if c.isdigit())
            tgt_digits = ''.join(c for c in tgt if c.isdigit())
            # Allow suffix match (target digits appear at end of current, for +1 prefix etc.)
            return cur_digits == tgt_digits or cur_digits.endswith(tgt_digits) or tgt_digits.endswith(cur_digits)

        # General: strip punctuation/whitespace and compare
        import re
        cur_clean = re.sub(r'[^a-z0-9]', '', cur)
        tgt_clean = re.sub(r'[^a-z0-9]', '', tgt)
        return cur_clean == tgt_clean or tgt_clean in cur_clean


    async def _get_field_value(self, field_name: str) -> Optional[str]:
        """Gets the current value of a field from the page"""
        patterns = self.field_patterns.get(field_name, [])
        for pattern in patterns:
            try:
                el = await self.page.query_selector(pattern)
                if el and await el.is_visible():
                    val = await el.evaluate("""el => {
                        if (el.tagName.toLowerCase() === 'select') {
                            return el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : '';
                        }
                        return el.value || el.textContent || '';
                    }""")
                    if val:
                        return str(val)
            except Exception:
                pass
        return ''
