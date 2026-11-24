"""
smart_form_filler.py — Clean, validated field-by-field form filler.

Design principles:
  1. Discover the real field element via CSS selectors (skips phone-code selects)
  2. For <select>: find the matching option text first, then keyboard-cycle using
     the FIRST LETTER OF THAT OPTION'S DISPLAY TEXT (not the profile value code).
     e.g. target="US" → option="United States" → press "U" (not "U" from "US")
  3. Validate each field after filling — log pass/fail clearly.
  4. Retry per field up to max_retries before marking failed.
"""

import asyncio
import re
import logging
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


class SmartFormFiller:
    """
    Fill checkout form fields one at a time with per-field validation.
    Replaces the VLM loop in address_autofill.py with a direct DOM-first approach.
    """

    # ── Field → CSS selector priority list ───────────────────────────────────
    FIELD_SELECTORS: Dict[str, List[str]] = {
        "first_name": [
            'input[name="firstName"]', 'input[name="first_name"]',
            'input[autocomplete="given-name"]',
            'input[id*="first"][type!="hidden"]',
            'input[placeholder*="first" i]',
        ],
        "last_name": [
            'input[name="lastName"]', 'input[name="last_name"]',
            'input[autocomplete="family-name"]',
            'input[id*="last"][type!="hidden"]',
            'input[placeholder*="last" i]',
        ],
        "email": [
            'input[type="email"]', 'input[name="email"]',
            'input[autocomplete="email"]', 'input[id*="email"]',
        ],
        "phone": [
            'input[type="tel"]', 'input[name="phone"]',
            'input[autocomplete="tel"]', 'input[id*="phone"]',
        ],
        "address1": [
            'input[name="address1"]', 'input[name="address"]',
            'input[autocomplete="address-line1"]', 'input[id*="address1"]',
            'input[placeholder*="street" i]', 'input[placeholder*="address" i]',
        ],
        "address2": [
            'input[name="address2"]', 'input[autocomplete="address-line2"]',
            'input[id*="address2"]',
            'input[placeholder*="apt" i]', 'input[placeholder*="suite" i]',
        ],
        "city": [
            'input[name="city"]', 'input[autocomplete="address-level2"]',
            'input[id*="city"]', 'input[placeholder*="city" i]',
        ],
        "state": [
            'select[name="provinceCode"]',
            'select[name="province"]',
            'select[name="state"]',
            'select[autocomplete="address-level1"]',
            'select[id*="provinceCode"]',
            'select[id*="province"]',
            'select[id*="state"]',
            'input[name="province"]', 'input[name="state"]',
        ],
        "postal_code": [
            'input[name="zip"]', 'input[name="postal_code"]',
            'input[name="postalCode"]', 'input[autocomplete="postal-code"]',
            'input[id*="zip"]', 'input[id*="postal"]',
            'input[placeholder*="zip" i]', 'input[placeholder*="postal" i]',
        ],
        "country": [
            # Shopify checkout uses countryCode; generic sites use country
            # NOTE: phone-code selects are filtered out in _find_element()
            'select[name="countryCode"]',
            'select[name="country"]',
            'select[autocomplete="country"]',
            'select[id*="CountryCode"]',
            'select[id*="country"]',
            'select[data-field*="country" i]',
        ],
    }

    # ── Country code → all acceptable display strings ─────────────────────────
    COUNTRY_ALIASES: Dict[str, List[str]] = {
        "US": ["United States", "United States of America", "USA", "U.S.A.", "US"],
        "GB": ["United Kingdom", "UK", "Britain", "Great Britain", "England"],
        "CA": ["Canada"],
        "AU": ["Australia"],
        "DE": ["Germany", "Deutschland"],
        "FR": ["France"],
        "IN": ["India"],
        "AE": ["United Arab Emirates", "UAE"],
        "MX": ["Mexico"],
        "JP": ["Japan"],
        "PK": ["Pakistan"],
    }

    # ── US state abbreviation → full name ─────────────────────────────────────
    STATE_ALIASES: Dict[str, List[str]] = {
        "AL": ["Alabama"], "AK": ["Alaska"], "AZ": ["Arizona"],
        "AR": ["Arkansas"], "CA": ["California"], "CO": ["Colorado"],
        "CT": ["Connecticut"], "DE": ["Delaware"], "FL": ["Florida"],
        "GA": ["Georgia"], "HI": ["Hawaii"], "ID": ["Idaho"],
        "IL": ["Illinois"], "IN": ["Indiana"], "IA": ["Iowa"],
        "KS": ["Kansas"], "KY": ["Kentucky"], "LA": ["Louisiana"],
        "ME": ["Maine"], "MD": ["Maryland"], "MA": ["Massachusetts"],
        "MI": ["Michigan"], "MN": ["Minnesota"], "MS": ["Mississippi"],
        "MO": ["Missouri"], "MT": ["Montana"], "NE": ["Nebraska"],
        "NV": ["Nevada"], "NH": ["New Hampshire"], "NJ": ["New Jersey"],
        "NM": ["New Mexico"], "NY": ["New York"], "NC": ["North Carolina"],
        "ND": ["North Dakota"], "OH": ["Ohio"], "OK": ["Oklahoma"],
        "OR": ["Oregon"], "PA": ["Pennsylvania"], "RI": ["Rhode Island"],
        "SC": ["South Carolina"], "SD": ["South Dakota"], "TN": ["Tennessee"],
        "TX": ["Texas"], "UT": ["Utah"], "VT": ["Vermont"],
        "VA": ["Virginia"], "WA": ["Washington"], "WV": ["West Virginia"],
        "WI": ["Wisconsin"], "WY": ["Wyoming"], "DC": ["District of Columbia"],
    }

    def __init__(self, page, status_display=None):
        self.page = page
        self.status = status_display
        self.log = logger

    # ─────────────────────────────────────────────────────────────────────────
    # Public entry point
    # ─────────────────────────────────────────────────────────────────────────

    async def fill_form(
        self,
        profile_data: Dict[str, str],
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """
        Fill all fields present in profile_data.
        Returns {'success': bool, 'results': {field: 'ok'|'failed'}, 'failed': [...]}.
        Country is filled first so the state dropdown populates correctly.
        """
        results: Dict[str, str] = {}
        failed: List[str] = []

        fill_order = [
            "country", "first_name", "last_name", "email",
            "phone", "address1", "address2", "city", "state", "postal_code",
        ]

        # Fields whose text input triggers address-autocomplete dropdowns on
        # Shopify / Google Places. We press Escape + wait after filling these
        # so the dropdown is gone before the next field is clicked.
        AUTOCOMPLETE_FIELDS = {"address1", "address2", "city"}

        for field in fill_order:
            value = profile_data.get(field, "").strip()
            if not value:
                continue

            if self.status:
                await self.status.show(f"Filling {field}…", "info", 800)

            self.log.info(f"[SmartFormFiller] Filling '{field}' → '{value}'")
            success = False

            for attempt in range(1, max_retries + 1):
                ok = await self._fill_and_validate(field, value)
                if ok:
                    self.log.info(f"  ✅ {field} confirmed (attempt {attempt})")
                    results[field] = "ok"
                    success = True
                    break
                self.log.warning(f"  ⚠️  {field} attempt {attempt} failed — retrying")
                await asyncio.sleep(0.8)

            if not success:
                self.log.error(f"  ❌ {field} failed after {max_retries} attempts")
                results[field] = "failed"
                failed.append(field)

            # Dismiss any address-autocomplete dropdown before moving to next field
            if field in AUTOCOMPLETE_FIELDS:
                await self._dismiss_autocomplete()

        return {"success": len(failed) == 0, "results": results, "failed": failed}

    # ─────────────────────────────────────────────────────────────────────────
    # Fill + validate one field
    # ─────────────────────────────────────────────────────────────────────────

    async def _fill_and_validate(self, field: str, value: str) -> bool:
        el = await self._find_element(field)
        if not el:
            self.log.warning(f"  Element not found for '{field}'")
            return False

        tag = await el.evaluate("el => el.tagName.toLowerCase()")
        is_select = tag == "select"

        if is_select:
            ok = await self._fill_select(el, field, value)
        else:
            ok = await self._fill_text_input(el, field, value)

        if not ok:
            return False

        return await self._validate_field(el, field, value, is_select)

    # ─────────────────────────────────────────────────────────────────────────
    # Element discovery
    # ─────────────────────────────────────────────────────────────────────────

    async def _find_element(self, field: str):
        """Return the first visible, enabled element matching the field selectors."""
        for sel in self.FIELD_SELECTORS.get(field, []):
            try:
                elements = await self.page.query_selector_all(sel)
                for el in elements:
                    if not await el.is_visible():
                        continue
                    if not await el.is_enabled():
                        continue
                    # Skip phone-country-code selects (options like "United States (+1)")
                    if field == "country":
                        is_phone_code = await el.evaluate("""el => {
                            const opts = Array.from(el.options || []).slice(0, 15);
                            return opts.some(o => /\\(\\+\\d/.test(o.text));
                        }""")
                        if is_phone_code:
                            self.log.debug(f"  Skipping phone-code <select> at selector '{sel}'")
                            continue
                    return el
            except Exception:
                pass

        # ── Country fallback: find any large visible <select> with a "United States"
        #    option — catches themes that use non-standard name/id attributes.
        if field == "country":
            try:
                info = await self.page.evaluate("""() => {
                    for (const el of document.querySelectorAll('select')) {
                        if (el.offsetWidth < 5 || el.offsetHeight < 5) continue;
                        const s = window.getComputedStyle(el);
                        if (s.display === 'none' || s.visibility === 'hidden') continue;
                        if (el.options.length < 50) continue;
                        const isPhone = Array.from(el.options).slice(0, 15)
                            .some(o => /\\(\\+\\d/.test(o.text));
                        if (isPhone) continue;
                        const hasUS = Array.from(el.options)
                            .some(o => /united states/i.test(o.text) || o.value === 'US');
                        if (!hasUS) continue;
                        return {
                            name: el.getAttribute('name') || '',
                            id:   el.getAttribute('id')   || '',
                            opts: el.options.length,
                        };
                    }
                    return null;
                }""")
                if info:
                    self.log.info(
                        f"  Country fallback: large select name='{info['name']}' "
                        f"id='{info['id']}' opts={info['opts']}"
                    )
                    direct = (f"select[name=\"{info['name']}\"]" if info['name']
                              else f"select[id=\"{info['id']}\"]" if info['id'] else None)
                    if direct:
                        el = await self.page.query_selector(direct)
                        if el and await el.is_visible():
                            return el
            except Exception as _fe:
                self.log.debug(f"  Country fallback error: {_fe}")

        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Text input fill
    # ─────────────────────────────────────────────────────────────────────────

    async def _dismiss_autocomplete(self):
        """Press Escape + click body to close any address-autocomplete dropdown.

        Shopify checkout and Google Places both show a suggestion list when the
        user types into address/city fields.  If that dropdown is still open when
        we try to click the next field the click is intercepted and times out.
        """
        try:
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
            # Also try clicking away from the input to blur it
            await self.page.evaluate("""() => {
                const el = document.activeElement;
                if (el && el !== document.body) el.blur();
                // Click a neutral spot (top-left corner of main content area)
                document.body.click();
            }""")
            await asyncio.sleep(0.25)
        except Exception:
            pass

    async def _fill_text_input(self, el, field: str, value: str) -> bool:
        try:
            await el.scroll_into_view_if_needed()
            # Try normal click first; if it times out (autocomplete overlay),
            # fall back to force=True which bypasses actionability checks.
            try:
                await el.click(timeout=5000)
            except Exception:
                self.log.debug(f"  Normal click failed for '{field}' — using force click")
                await el.click(force=True, timeout=5000)
            await asyncio.sleep(0.15)
            # Clear
            await el.evaluate("el => { el.value = ''; }")
            await el.fill(value)
            # Fire events
            await el.evaluate("""el => {
                ['input', 'change', 'blur'].forEach(t =>
                    el.dispatchEvent(new Event(t, { bubbles: true })));
            }""")
            return True
        except Exception as e:
            self.log.warning(f"  Text fill error for '{field}': {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Select fill — keyboard-first with the CORRECT first letter
    # ─────────────────────────────────────────────────────────────────────────

    async def _fill_select(self, el, field: str, value: str) -> bool:
        """
        Steps:
        1. Collect all options from the <select>
        2. Find the option whose text best matches our target (using alias tables)
        3. Extract the FIRST LETTER of that option's display text
           e.g. target="US" → option text="United States" → first_letter="u"
        4. Keyboard-cycle (press that letter repeatedly) until the option is selected
        5. Commit using React-compatible native setter + event chain
        6. Fallback to programmatic select_option if cycling exhausts 50 presses
        """
        try:
            options = await el.evaluate("""el => Array.from(el.options).map(o => ({
                value: o.value,
                text: o.text.trim(),
                index: o.index
            }))""")

            target_opt = self._find_matching_option(options, field, value)
            if not target_opt:
                sample = [o["text"] for o in options[:12]]
                self.log.warning(
                    f"  No option matching '{value}' found in '{field}' select. "
                    f"Available (first 12): {sample}"
                )
                return False

            display_text = target_opt["text"]
            option_value = target_opt["value"]
            # THE KEY FIX: first letter comes from the DISPLAY TEXT, not the profile value
            first_letter = display_text[0].lower() if display_text else ""

            self.log.info(
                f"  Select '{field}': target option='{display_text}' "
                f"value='{option_value}' → pressing key='{first_letter}'"
            )

            if not first_letter:
                return False

            # ── Keyboard cycle ────────────────────────────────────────────────
            await el.scroll_into_view_if_needed()
            await el.focus()
            await asyncio.sleep(0.15)

            for press_num in range(50):
                await el.press(first_letter)
                await asyncio.sleep(0.06)
                current_text = await el.evaluate(
                    "el => (el.options[el.selectedIndex]?.text || '').trim()"
                )
                if current_text.lower() == display_text.lower():
                    self.log.info(
                        f"  Keyboard matched '{current_text}' after {press_num + 1} press(es)"
                    )
                    await self._commit_select(el, option_value)
                    return True

            # ── Programmatic fallback ────────────────────────────────────────
            self.log.warning(
                f"  Keyboard cycle exhausted for '{field}' — "
                f"falling back to programmatic select_option"
            )
            await el.select_option(value=option_value)
            await self._commit_select(el, option_value)
            return True

        except Exception as e:
            self.log.warning(f"  Select fill error for '{field}': {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Option matching
    # ─────────────────────────────────────────────────────────────────────────

    def _find_matching_option(
        self, options: List[Dict], field: str, value: str
    ) -> Optional[Dict]:
        """Find the <option> whose text/value best matches our target value."""
        norm = lambda s: (s or "").lower().strip()
        val_n = norm(value)

        # Build alias list for this field+value
        aliases = [val_n]
        if field == "country":
            val_upper = value.upper()
            for code, names in self.COUNTRY_ALIASES.items():
                all_forms = [code] + names
                if value in all_forms or val_upper == code or val_n in [norm(x) for x in all_forms]:
                    aliases = [norm(x) for x in all_forms]
                    break
        elif field == "state":
            val_upper = value.upper()
            for abbr, names in self.STATE_ALIASES.items():
                all_forms = [abbr] + names
                if val_upper == abbr or val_n in [norm(x) for x in all_forms]:
                    aliases = [norm(x) for x in all_forms]
                    break

        # Pass 1: exact value attr or exact text
        for opt in options:
            if norm(opt["value"]) in aliases or norm(opt["text"]) in aliases:
                return opt

        # Pass 2: substring containment (longer than 1 char to avoid spurious matches)
        for opt in options:
            ot = norm(opt["text"])
            ov = norm(opt["value"])
            if any(len(a) > 1 and (a in ot or ot in a) for a in aliases):
                return opt
            if any(len(a) > 1 and (a in ov or ov in a) for a in aliases):
                return opt

        return None

    # ─────────────────────────────────────────────────────────────────────────
    # React-compatible select commit
    # ─────────────────────────────────────────────────────────────────────────

    async def _commit_select(self, el, value: str):
        """Set value via native setter + full event chain + React fiber onChange."""
        await el.evaluate("""(el, val) => {
            // 1. Native value setter (bypasses React's controlled-component lock)
            const ns = Object.getOwnPropertyDescriptor(
                window.HTMLSelectElement.prototype, 'value'
            ).set;
            ns.call(el, val);

            // 2. Full event chain
            ['input', 'change', 'blur'].forEach(t =>
                el.dispatchEvent(new Event(t, { bubbles: true })));

            // 3. React fiber direct onChange
            const fk = Object.keys(el).find(
                k => k.startsWith('__reactFiber') ||
                     k.startsWith('__reactInternalInstance')
            );
            if (fk) {
                const props = el[fk]?.memoizedProps || el[fk]?.pendingProps;
                if (props?.onChange) {
                    try {
                        props.onChange({
                            target: el, currentTarget: el,
                            type: 'change', bubbles: true,
                            preventDefault: () => {},
                        });
                    } catch (e) {}
                }
            }
        }""", value)

    # ─────────────────────────────────────────────────────────────────────────
    # Field validation
    # ─────────────────────────────────────────────────────────────────────────

    async def _validate_field(
        self, el, field: str, target: str, is_select: bool
    ) -> bool:
        """
        Read the field back from the DOM and compare to target.
        Re-finds the element fresh — React re-renders detach the original node.
        Waits up to 1.5 s (3 × 0.5 s) for the value to settle before giving up.
        """
        for settle_attempt in range(3):
            await asyncio.sleep(0.5)
            try:
                # Always re-find — stale handles read empty/wrong after React renders
                fresh = await self._find_element(field)
                read_el = fresh if fresh else el
                is_sel = await read_el.evaluate("el => el.tagName.toLowerCase() === 'select'")
                if is_sel:
                    actual = await read_el.evaluate(
                        "el => (el.options[el.selectedIndex]?.text || '').trim()"
                    )
                else:
                    actual = await read_el.evaluate("el => (el.value || '').trim()")

                matched = self._values_match(field, actual, target)
                if matched:
                    self.log.info(f"  ✅ Validated '{field}': '{actual}'")
                    return True
                self.log.debug(
                    f"  Settle {settle_attempt+1}/3 '{field}': got='{actual}' want='{target}'"
                )
            except Exception as e:
                self.log.debug(f"  Validation read error for '{field}': {e}")

        self.log.warning(f"  ❌ Validation failed '{field}': want='{target}'")
        return False

    def _values_match(self, field: str, actual: str, target: str) -> bool:
        norm = lambda s: (s or "").lower().strip()
        a, t = norm(actual), norm(target)

        if not a or not t:
            return False
        if a == t or t in a or a in t:
            return True

        if field == "phone":
            a_d = re.sub(r"\D", "", a)
            t_d = re.sub(r"\D", "", t)
            return bool(a_d and t_d and (
                a_d == t_d or a_d.endswith(t_d) or t_d.endswith(a_d)
            ))

        if field == "country":
            t_up = target.upper()
            for code, names in self.COUNTRY_ALIASES.items():
                all_forms = [norm(code)] + [norm(n) for n in names]
                if a in all_forms and (t in all_forms or t_up == code):
                    return True

        if field == "state":
            t_up = target.upper()
            for abbr, names in self.STATE_ALIASES.items():
                all_forms = [norm(abbr)] + [norm(n) for n in names]
                if a in all_forms and (t in all_forms or t_up == abbr):
                    return True

        a_c = re.sub(r"[^a-z0-9]", "", a)
        t_c = re.sub(r"[^a-z0-9]", "", t)
        return bool(a_c and t_c and a_c == t_c)
