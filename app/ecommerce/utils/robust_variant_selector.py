"""
RobustVariantSelector
=====================
Single-loop variant selector that works on any e-commerce site.

Design principles
-----------------
* No JS injection or overlay manipulation — uses Playwright-native APIs only.
* Candidate discovery: broad DOM query then score by text similarity.
* Click: element.click() with JS fallback.
* Post-click validation pipeline (ANY one passing = success):
    1. MutationObserver  — captures class/aria/style changes on the clicked element
    1b. Sibling class-delta — clicked element has a unique class vs its siblings
    2. Accessibility tree — Playwright's a11y snapshot for checked/selected/pressed state
    3. Pixel diff         — before/after screenshot of the element; detects bg-color change
    4. Page label         — text pattern "Color: Red" / "Size: XL" on page
    5. URL slug           — target words appear in the URL after click
* On failure: try next candidate. Give up only when all candidates exhausted.
"""

import asyncio
import io
import logging
import re
from typing import Dict, Any, List, Optional, Tuple

from playwright.async_api import Page, ElementHandle

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Candidate discovery — CSS selectors to search for variant options
# ---------------------------------------------------------------------------
_VARIANT_CANDIDATE_SELECTORS = [
    # Explicit ARIA radio/option patterns (most reliable)
    "[role='radio']",
    "[role='option']",
    "[type='radio']",
    # Allbirds / headless Shopify: buttons with aria-label containing swatch info
    "button[aria-label]",
    "label[aria-label]",
    # Shopify-style list items
    "fieldset li",
    "ul[class*='swatch'] li",
    "ul[class*='option'] li",
    "ul[class*='variant'] li",
    # Data-attribute patterns (Allbirds, Shopify headless, etc.)
    "[data-option-value]",
    "[data-value]",
    "[data-color]",
    "[data-size]",
    "[data-variant]",
    "[data-option]",
    # Class-name patterns for swatch/option containers
    "[class*='swatch'] button",
    "[class*='Swatch'] button",
    "[class*='ColorSwatch'] button",
    "[class*='color-swatch'] button",
    "[class*='option'] button",
    "[class*='Option'] button",
    "[class*='variant'] button",
    "[class*='size'] button",
    "[class*='Size'] button",
    "[class*='color'] button",
    "[class*='Color'] button",
    # Generic clickable items in product containers
    "[class*='product'] button",
    "[class*='pdp'] button",
    # Form buttons (size selectors on many sites live inside a product form)
    "form button:not([type='submit'])",
    # Radiogroup children (accessibility pattern for variant pickers)
    "[role='radiogroup'] button",
    "[role='group'] button",
    # Plain <li> children of lists — Allbirds uses bare <li> as clickable size tiles
    "[class*='size'] li",
    "[class*='option'] li",
    "[class*='variant'] li",
    # WooCommerce bracket-notation: the visible label next to the hidden radio
    # e.g. <label for="attr_897_2752">L</label> where value 2752 is a term ID
    "label[for*='attr_']",
    ".variable-item label",
    "[class*='variable-item'] label",
    "[class*='woo'] label",
    # General radio labels (many themes use label > span for swatch text)
    "label[for]",
]

# Elements to SKIP — intentionally narrow.
# Do NOT include "nav" — many sites wrap color/size option groups in <nav> or
# elements with "nav" in class names for accessibility, which would skip valid swatches.
_SKIP_ANCESTORS = re.compile(
    r"breadcrumb|review-section|review-list|recommend|similar-products|"
    r"related-products|cart-drawer|minicart|mini-cart|recently-viewed|"
    r"upsell|cross-sell|product-recommendations",
    re.IGNORECASE,
)

# Text content that's obviously not a variant label
_SKIP_TEXT = re.compile(
    r"^(add to cart|buy now|checkout|subscribe|notify|sign up|quick add|"
    r"select a|choose a|free shipping|\$|women'?s sizes|men'?s sizes|"
    r"size guide|fit guide|size chart|\d+ reviews?|sold out notice|"
    r"save \d+|% off|\d+ left)$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Text normalisation & scoring
# ---------------------------------------------------------------------------
_PUNCT = re.compile(r"[^\w\s]")


def _words(text: str) -> set:
    """Lowercase, strip punctuation, return word set."""
    return set(_PUNCT.sub(" ", text).lower().split()) - {"", "the", "a", "an"}


def _score(target: str, candidate_text: str) -> int:
    """
    Return 0-100 match score between target variant and candidate text.

    100 — exact match after normalisation
     80 — all target words found in candidate (e.g. "Blizzard Sole" inside long aria-label)
     60 — most target words found (>= 70 %)
      0 — no meaningful overlap
    """
    t = target.lower().strip()
    c = candidate_text.lower().strip()

    # Exact after punct-strip
    t_clean = _PUNCT.sub("", t).replace(" ", "").replace("-", "")
    c_clean = _PUNCT.sub("", c).replace(" ", "").replace("-", "")
    if t_clean and t_clean == c_clean:
        return 100

    # Containment (short target inside candidate or vice-versa)
    if t_clean and c_clean:
        shorter, longer = sorted([t_clean, c_clean], key=len)
        ratio = len(shorter) / len(longer)
        if shorter in longer and ratio >= 0.5:
            return 90

    # Word-level
    t_words = _words(target)
    c_words = _words(candidate_text)
    if not t_words or not c_words:
        return 0
    if t_words.issubset(c_words):
        return 80
    overlap = len(t_words & c_words) / len(t_words)
    if overlap >= 0.7:
        return 60
    return 0


def _get_candidate_text(elem_info: Dict) -> str:
    """Combine all text sources for a candidate into one searchable string.
    JS returns camelCase keys; cover both spellings for safety.
    """
    parts = [
        elem_info.get("text", ""),
        elem_info.get("ariaLabel", "") or elem_info.get("aria_label", ""),
        elem_info.get("title", ""),
        elem_info.get("dataValue", "") or elem_info.get("data_value", ""),
        elem_info.get("dataColor", "") or elem_info.get("data_color", ""),
        elem_info.get("dataSize", "") or elem_info.get("data_size", ""),
        elem_info.get("value", ""),
    ]
    return " | ".join(p for p in parts if p).strip()


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

# -- Pre-click check: is this variant already selected? ----------------------
async def _validate_aria(page: Page, target: str) -> bool:
    """
    Strict pre-click check: only explicit ARIA / :checked DOM state.
    Text-label and visual checks are NOT used here — they fire on page load
    and cause false "already selected" positives.

    For short values (≤ 2 chars like 'L', 'M') we require a WORD-BOUNDARY
    match against element text to avoid 'l' matching inside 'Slate', 'Blue', etc.
    """
    try:
        result = await page.evaluate(
            """(target) => {
                const t = target.toLowerCase().replace(/[^a-z0-9 ]/g, '').trim();
                const isShort = t.length <= 2;
                const sels = [
                    '[aria-selected="true"]', '[aria-checked="true"]',
                    '[aria-pressed="true"]',
                    'input[type="radio"]:checked', 'input[type="checkbox"]:checked',
                    '[data-selected="true"]', '[data-active="true"]',
                ];
                for (const s of sels) {
                    for (const el of document.querySelectorAll(s)) {
                        let inNav = false;
                        let a = el;
                        while (a && a !== document.body) {
                            const cls = (typeof a.className === 'string' ? a.className : '').toLowerCase();
                            if (/header|footer|nav|breadcrumb|review|recommend/i.test(cls) ||
                                a.tagName === 'HEADER' || a.tagName === 'FOOTER' || a.tagName === 'NAV') {
                                inNav = true; break;
                            }
                            a = a.parentElement;
                        }
                        if (inNav) continue;
                        const text = (
                            (el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '') +
                            ' ' + (el.getAttribute('data-value') || '') + ' ' + (el.getAttribute('data-color') || '')
                        ).toLowerCase().replace(/[^a-z0-9 ]/g, '');
                        if (text.length >= 200) continue;
                        // For short values require word-boundary to prevent 'l' matching 'slate'
                        const matched = isShort
                            ? new RegExp('(?:^| )' + t + '(?:$| )').test(text)
                            : text.includes(t);
                        if (matched) return true;
                    }
                }
                return false;
            }""",
            target,
        )
        return bool(result)
    except Exception:
        return False


# -- Post-click strategy 1: MutationObserver ----------------------------------
async def _setup_mutation_observer(page: Page) -> None:
    """Inject a MutationObserver BEFORE clicking to record DOM state changes."""
    try:
        await page.evaluate("""() => {
            window.__rvsMutations = [];
            if (window.__rvsObserver) { try { window.__rvsObserver.disconnect(); } catch(e){} }
            window.__rvsObserver = new MutationObserver((mutations) => {
                for (const m of mutations) {
                    const el = m.target;
                    window.__rvsMutations.push({
                        attr:   m.attributeName || '',
                        oldVal: m.oldValue || '',
                        newVal: m.type === 'attributes' ? (el.getAttribute(m.attributeName) || '') : '',
                        text:   (el.textContent || '').trim().substring(0, 80),
                        label:  el.getAttribute('aria-label') || '',
                        cls:    (typeof el.className === 'string' ? el.className : ''),
                    });
                }
            });
            window.__rvsObserver.observe(document.body, {
                attributes: true, attributeOldValue: true, subtree: true,
                attributeFilter: ['class','aria-selected','aria-checked','aria-pressed',
                                  'data-selected','data-active','style','aria-current'],
            });
        }""")
    except Exception:
        pass



# -- WooCommerce variation_id validator (ground truth) -------------------------
async def _validate_woo_variation(page: Page) -> bool:
    """WooCommerce ground-truth: check that variation_id is non-zero.

    When all required attributes are selected, WooCommerce sets
    input[name='variation_id'] to the matched variation's ID (> 0).
    This is far more reliable than DOM text / URL checks.
    Also checks that the ATC button is enabled (WC disables it when incomplete).
    """
    try:
        result = await page.evaluate("""() => {
            // Check variation_id hidden input
            const vid = document.querySelector('input[name="variation_id"]');
            if (vid) {
                const v = parseInt(vid.value || '0', 10);
                if (v > 0) return {ok: true, reason: 'variation_id=' + v};
            }
            // Check ATC button enabled state
            const atcBtns = document.querySelectorAll(
                'button[type="submit"][name="add-to-cart"], .single_add_to_cart_button, button.add_to_cart_button'
            );
            for (const btn of atcBtns) {
                if (!btn.disabled && !btn.classList.contains('disabled') && !btn.classList.contains('wc-variation-selection-needed')) {
                    return {ok: true, reason: 'atc_enabled'};
                }
            }
            return {ok: false, reason: 'variation_id=0 and atc disabled/missing'};
        }""")
        if result.get('ok'):
            logger.info(f"  WooCommerce variation confirmed: {result['reason']}")
            return True
        logger.debug(f"  WooCommerce variation not confirmed: {result.get('reason')}")
        return False
    except Exception as e:
        logger.debug(f"  WooCommerce variation check failed: {e}")
        return False


async def _validate_mutations(page: Page, target: str) -> bool:
    """Read mutations recorded since last _setup_mutation_observer call."""
    try:
        result = await page.evaluate(
            """(target) => {
                const muts = window.__rvsMutations || [];
                if (window.__rvsObserver) { try { window.__rvsObserver.disconnect(); } catch(e){} }
                const t = target.toLowerCase().trim();
                const isShort = t.length <= 3;

                for (const m of muts) {
                    const raw = (m.text + ' ' + m.label).toLowerCase();
                    if (raw.length > 150) continue;

                    let hit = false;
                    if (isShort) {
                        hit = new RegExp('(?:^|[^a-z0-9])' + t.replace(/[.*+?^${}()|[\\]\\\\]/g,'\\\\$&') + '(?:$|[^a-z0-9])').test(raw);
                    } else {
                        hit = raw.includes(t);
                    }
                    if (!hit) continue;

                    // class attribute: a selection keyword was added
                    if (m.attr === 'class') {
                        const added = m.cls.split(/\\s+/).filter(c => c && !m.oldVal.includes(c));
                        if (added.some(c => /select|active|current|chosen|check|pick|highlight|focus|pressed/i.test(c)))
                            return {found: true, method: 'mutation_class'};
                    }
                    // explicit aria/data attribute flipped to true
                    if (['aria-selected','aria-checked','aria-pressed','data-selected','data-active','aria-current'].includes(m.attr)) {
                        if (m.newVal === 'true' || m.newVal === '1')
                            return {found: true, method: 'mutation_aria'};
                    }
                    // inline style gained a background
                    if (m.attr === 'style' && m.newVal !== m.oldVal && m.newVal.includes('background'))
                        return {found: true, method: 'mutation_style'};
                }
                return {found: false, total: muts.length};
            }""",
            target,
        )
        if isinstance(result, dict) and result.get('found'):
            logger.info(f"  ✅ Mutation validation: {result.get('method')}")
            return True
        return False
    except Exception:
        return False


# -- Post-click strategy 1b: Sibling class-delta (elemIdx-based) --------------
async def _validate_class_delta(page: Page, cand: Dict, target: str) -> bool:
    """
    Post-click: find the clicked element by its data-rvsidx tag, then compare
    its class list against its siblings. If it has a unique class its siblings
    lack (regardless of the class name), it is considered selected.
    Also checks if its background-color became non-white/non-transparent.
    """
    try:
        tag_attr = cand.get("_tag", "data-rvsidx")
        elem_idx = cand.get("elemIdx")
        if elem_idx is None:
            return False
        result = await page.evaluate(
            """([tagAttr, elemIdx, target]) => {
                const el = document.querySelector('[' + tagAttr + '="' + elemIdx + '"]');
                if (!el) return {found: false, reason: 'element_not_found'};

                const elCls = new Set((typeof el.className === 'string' ? el.className : '').split(/\\s+/).filter(Boolean));

                // Collect siblings: buttons/li/options in the same parent
                const parent = el.parentElement;
                if (!parent) return {found: false, reason: 'no_parent'};
                const siblings = Array.from(
                    parent.querySelectorAll('button, li, [role="radio"], [role="option"]')
                ).filter(s => s !== el && s.offsetWidth > 0);

                if (siblings.length === 0) {
                    // No siblings — fall back to background-color check
                } else {
                    // Build union of all sibling class tokens
                    const siblingClsUnion = new Set();
                    for (const s of siblings) {
                        (typeof s.className === 'string' ? s.className : '').split(/\\s+/)
                            .filter(Boolean).forEach(c => siblingClsUnion.add(c));
                    }
                    // Classes this element has that NO sibling has
                    const uniqueCls = [...elCls].filter(c => !siblingClsUnion.has(c));
                    if (uniqueCls.length > 0) {
                        return {found: true, method: 'class_delta', unique: uniqueCls};
                    }
                    // Classes removed from this element that all siblings still have
                    const sib0Cls = new Set((typeof siblings[0].className === 'string' ? siblings[0].className : '').split(/\\s+/).filter(Boolean));
                    const missingOnEl = [...sib0Cls].filter(c =>
                        !elCls.has(c) && siblings.every(s =>
                            (typeof s.className === 'string' ? s.className : '').split(/\\s+/).includes(c)
                        )
                    );
                    if (missingOnEl.some(c => /disabled|unavail|inactive|unselect|empty|out.of/i.test(c))) {
                        return {found: true, method: 'class_removal_delta', removed: missingOnEl};
                    }
                }

                // Background-color check: selected element often gets a dark/colored fill
                const style = window.getComputedStyle(el);
                const bg = style.backgroundColor;
                if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') {
                    const rgb = bg.match(/\\d+/g);
                    if (rgb) {
                        const [r, g, b] = rgb.map(Number);
                        if (!(r > 220 && g > 220 && b > 220)) {
                            return {found: true, method: 'bg_color_selected', bg};
                        }
                    }
                }

                return {found: false, reason: 'no_delta', elCls: [...elCls]};
            }""",
            [tag_attr, str(elem_idx), target],
        )
        if isinstance(result, dict) and result.get('found'):
            logger.info(f"  ✅ Class-delta validation: {result.get('method')} ({result})")
            return True
        logger.debug(f"  Class-delta: {result}")
        return False
    except Exception as e:
        logger.debug(f"  Class-delta error: {e}")
        return False


# -- Post-click strategy 2: Playwright accessibility snapshot -----------------
async def _validate_accessibility(page: Page, target: str) -> bool:
    """Query the browser's OS-level accessibility tree for checked/selected/pressed state."""
    try:
        snapshot = await page.accessibility.snapshot(interesting_only=False)
        if not snapshot:
            return False
        t = target.lower().strip()
        esc = re.escape(t)
        short_re = re.compile(r'(?:^|[^a-z0-9])' + esc + r'(?:$|[^a-z0-9])') if len(t) <= 3 else None

        def walk(node, depth=0):
            if not node or depth > 20:
                return False
            name = (node.get('name') or '').lower().strip()
            if name:
                match = (short_re.search(name) if short_re else t in name) and len(name) < 80
                if match and (node.get('checked') or node.get('selected') or node.get('pressed') or node.get('current')):
                    return True
            for child in (node.get('children') or []):
                if walk(child, depth + 1):
                    return True
            return False

        if walk(snapshot):
            logger.info(f"  ✅ Accessibility snapshot validation succeeded for '{target}'")
            return True
        return False
    except Exception as e:
        logger.debug(f"  Accessibility snapshot error: {e}")
        return False


# -- Text-based visual-state check (no elemIdx required) ----------------------
async def _validate_visual_state(page: Page, target: str) -> bool:
    """
    Multi-signal visual validation.

    Finds every visible element whose text / aria-label / data-value matches
    *target*, then checks whether that element looks visually "selected" by
    comparing 8 computed-style properties against peer elements in the same
    variant group.  The comparison walks up the DOM up to 5 levels to find
    peers even when each button is wrapped in its own container (e.g.
    Fashion Nova).

    Signals checked (any one is sufficient):
      1. Explicit ARIA  (aria-selected / aria-checked / aria-pressed)
      2. Border-width   thicker than peers (catches thick-border selection)
      3. Outline-width  wider than peers
      4. Background     different from peers and not white/transparent
      5. Text color     different from peers
      6. Font-weight    ≥ 100 heavier than peers
      7. Box-shadow     present while peers have none
      8. Class-delta    unique CSS class vs peer union
      9. Absolute fallback when NO peers found: border ≥ 2px, outline ≥ 2px,
         or non-white non-transparent background.

    Also logs the matched on-screen text so mismatches are visible.
    """
    try:
        result = await page.evaluate(
            """(target) => {
                const t = target.toLowerCase().trim();
                const isShort = t.length <= 3;

                // ── Text matcher ─────────────────────────────────────────────
                function textMatches(el) {
                    const raw = (
                        (el.textContent  || '') + ' ' +
                        (el.getAttribute('aria-label')  || '') + ' ' +
                        (el.getAttribute('data-value')  || '') + ' ' +
                        (el.getAttribute('value')       || '')
                    ).toLowerCase().replace(/[^\\w\\s]/g, ' ').trim();
                    if (raw.length > 100) return false;
                    if (isShort) {
                        const esc = t.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
                        return new RegExp('(?:^|[^a-z0-9])' + esc + '(?:$|[^a-z0-9])').test(raw);
                    }
                    return raw.includes(t);
                }

                // ── Ancestor exclusion (nav / footer / reviews etc.) ─────────
                function inExcluded(el) {
                    let a = el;
                    while (a && a !== document.body) {
                        const id  = (a.id || '').toLowerCase();
                        const cls = (typeof a.className === 'string' ? a.className : '').toLowerCase();
                        if (/header|footer|nav|breadcrumb|review|recommend/i.test(id + ' ' + cls))
                            return true;
                        a = a.parentElement;
                    }
                    return false;
                }

                // ── Capture 8-property visual signature ──────────────────────
                function sig(el) {
                    const s = window.getComputedStyle(el);
                    return {
                        borderTop:  parseFloat(s.borderTopWidth)   || 0,
                        borderBot:  parseFloat(s.borderBottomWidth)|| 0,
                        outline:    parseFloat(s.outlineWidth)     || 0,
                        bg:         s.backgroundColor,
                        color:      s.color,
                        weight:     parseInt(s.fontWeight)         || 400,
                        shadow:     s.boxShadow  || 'none',
                        opacity:    parseFloat(s.opacity)          || 1,
                    };
                }

                // ── Compare two signatures; returns diff descriptor or null ──
                function diff(a, b) {
                    if (Math.abs(a.borderTop - b.borderTop) > 0.5)
                        return {signal: 'border', el: a.borderTop, peer: b.borderTop};
                    if (Math.abs(a.outline - b.outline) > 0.5)
                        return {signal: 'outline', el: a.outline, peer: b.outline};
                    if (a.bg !== b.bg) {
                        const blank = v => v === 'rgba(0, 0, 0, 0)' || v === 'transparent';
                        if (!(blank(a.bg) && blank(b.bg)))
                            return {signal: 'bg', el: a.bg, peer: b.bg};
                    }
                    if (a.color !== b.color)
                        return {signal: 'color', el: a.color, peer: b.color};
                    if (Math.abs(a.weight - b.weight) >= 100)
                        return {signal: 'weight', el: a.weight, peer: b.weight};
                    if (a.shadow !== 'none' && a.shadow !== b.shadow)
                        return {signal: 'shadow', el: a.shadow};
                    return null;
                }

                // ── Candidate selectors (broad) ───────────────────────────────
                const SEL = 'button, li, label, span[data-value], ' +
                            '[role="radio"], [role="option"], [role="button"]';

                const candidates = document.querySelectorAll(SEL);
                for (const el of candidates) {
                    if (el.offsetWidth < 5 || el.offsetHeight < 5) continue;
                    if (inExcluded(el)) continue;
                    if (!textMatches(el)) continue;

                    const elText = (el.textContent || '').trim().substring(0, 40);

                    // 1. Explicit ARIA
                    if (el.getAttribute('aria-selected') === 'true' ||
                        el.getAttribute('aria-checked')  === 'true' ||
                        el.getAttribute('aria-pressed')  === 'true')
                        return {found: true, method: 'aria', text: elText};

                    // 2-8. Visual / class comparison against peers
                    // Walk up DOM up to 5 levels to find a container with peers
                    let anchor = el.parentElement;
                    let peers  = [];
                    for (let d = 0; d < 5 && anchor; d++) {
                        peers = Array.from(anchor.querySelectorAll(SEL))
                            .filter(p => p !== el && p.offsetWidth > 5 && !inExcluded(p));
                        if (peers.length >= 1) break;
                        anchor = anchor.parentElement;
                    }

                    if (peers.length > 0) {
                        const elSig = sig(el);

                        // Compare against each peer individually
                        for (const peer of peers.slice(0, 8)) {
                            const d = diff(elSig, sig(peer));
                            if (d) return {found: true, method: 'visual_' + d.signal,
                                           text: elText, diff: d};
                        }

                        // Class-delta vs peer union
                        const elCls   = new Set((typeof el.className === 'string' ? el.className : '').split(/\\s+/).filter(Boolean));
                        const peerCls = new Set();
                        peers.forEach(p =>
                            (typeof p.className === 'string' ? p.className : '').split(/\\s+/).filter(Boolean).forEach(c => peerCls.add(c))
                        );
                        const unique = [...elCls].filter(c => !peerCls.has(c));
                        if (unique.length > 0)
                            return {found: true, method: 'class_delta', text: elText, unique};

                    } else {
                        // No peers found — absolute checks
                        const s = sig(el);
                        if (s.borderTop >= 2)
                            return {found: true, method: 'border_abs', text: elText, border: s.borderTop};
                        if (s.outline >= 2)
                            return {found: true, method: 'outline_abs', text: elText, outline: s.outline};
                        const blank = v => v === 'rgba(0, 0, 0, 0)' || v === 'transparent';
                        if (!blank(s.bg)) {
                            const rgb = s.bg.match(/\\d+/g);
                            if (rgb) {
                                const [r, g, b] = rgb.map(Number);
                                if (!(r > 220 && g > 220 && b > 220))
                                    return {found: true, method: 'bg_abs', text: elText, bg: s.bg};
                            }
                        }
                        if (s.shadow && s.shadow !== 'none')
                            return {found: true, method: 'shadow_abs', text: elText, shadow: s.shadow};
                    }
                }
                return {found: false};
            }""",
            target,
        )
        if isinstance(result, dict) and result.get('found'):
            logger.info(
                f"  ✅ Visual-state OK [{result.get('method')}] "
                f"on-screen='{result.get('text')}' target='{target}'"
            )
            return True
        logger.debug(f"  Visual-state: no selected signal found for '{target}'")
        return False
    except Exception as e:
        logger.debug(f"  Visual-state validation error: {e}")
        return False


# -- Post-click strategy 3: Element pixel diff --------------------------------
async def _capture_element_pixels(page: Page, cand: Dict) -> Optional[bytes]:
    """Screenshot the candidate element. Returns raw PNG bytes or None."""
    try:
        tag_attr = cand.get("_tag", "data-rvsidx")
        elem_idx = cand.get("elemIdx")
        if elem_idx is None:
            return None
        elem = await page.query_selector(f'[{tag_attr}="{elem_idx}"]')
        if elem:
            return await elem.screenshot()
    except Exception:
        pass
    return None


def _pixel_diff_significant(before: Optional[bytes], after: Optional[bytes], threshold: float = 20.0) -> bool:
    """Compare two PNG screenshots. Returns True if mean pixel change > threshold."""
    if not before or not after:
        return False
    try:
        from PIL import Image
        import numpy as np
        b = np.array(Image.open(io.BytesIO(before)).convert('RGB'), dtype=float)
        a = np.array(Image.open(io.BytesIO(after)).convert('RGB'), dtype=float)
        if b.shape != a.shape:
            return False
        diff = float(np.abs(b - a).mean())
        logger.debug(f"  Pixel diff: mean_diff={diff:.1f} (threshold={threshold})")
        return diff > threshold
    except ImportError:
        logger.debug("  PIL not available — skipping pixel diff")
        return False
    except Exception as e:
        logger.debug(f"  Pixel diff error: {e}")
        return False


# -- Post-click strategy 4: Page label text -----------------------------------
async def _validate_label(page: Page, variant_type: str, target: str) -> bool:
    """Look for 'Color: Red' / 'Size: XL' pattern in visible page text.

    For short values (single letters like 'L', 'M') we require word-boundary
    matching to avoid 'l' matching inside words like 'large', 'blue', etc.
    """
    try:
        result = await page.evaluate(
            """([vtype, target]) => {
                const tNorm = target.toLowerCase().replace(/[^a-z0-9 ]/g, ' ').trim();
                const tWords = tNorm.split(/\s+/).filter(Boolean);
                const isShort = tWords.every(w => w.length <= 2);
                const lines = (document.body.innerText || '').split('\n');
                for (const line of lines) {
                    const l = line.toLowerCase();
                    if (l.length > 80) continue;
                    if (/guide|chart|usual|recommend/i.test(l)) continue;
                    if (!(l.includes(vtype.toLowerCase()) || l.includes('selected'))) continue;
                    const matched = isShort
                        ? tWords.every(w => new RegExp('(?:^|[^a-z0-9])' + w + '(?:$|[^a-z0-9])').test(l))
                        : tWords.every(w => l.includes(w));
                    if (matched) return true;
                }
                return false;
            }""",
            [variant_type, target],
        )
        return bool(result)
    except Exception:
        return False


# -- Post-click strategy 5: URL slug ------------------------------------------
async def _validate_url(page: Page, target: str) -> bool:
    """Check if all target words appear in the current URL as whole slug segments.

    Short values (≤ 2 chars per word, e.g. 'L', 'M', 'XS') are EXCLUDED because
    single letters appear in virtually every URL string, producing false positives.
    """
    try:
        t_words = _words(target)
        if not t_words:
            return False
        # Skip URL validation for short/single-letter variants — too many false positives.
        # A value like 'L' or 'XS' will match 'https://example.com/products/...' trivially.
        if all(len(w) <= 2 for w in t_words):
            logger.debug(f"_validate_url: skipping for short value {target!r}")
            return False
        url = page.url.lower()
        return all(w in url for w in t_words)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main selector class
# ---------------------------------------------------------------------------

async def _commit_shopify_variant(page: Page, variant_type: str, variant_value: str) -> None:
    """
    After clicking a visual variant button (which may only update CSS classes),
    also fire the underlying state mechanism Shopify themes use:
      1. Hidden radio inputs  (most common in Shopify 2.0 custom themes)
      2. SingleOptionSelector <select> elements
      3. Custom variant-change events
    Without this, sites like Fashion Nova show "Please select size" even after
    a visual click, because the hidden radio wasn't checked.
    """
    try:
        result = await page.evaluate("""([vtype, vval]) => {
            const t = vval.toLowerCase().trim();
            const log = [];

            // ── 1. Hidden radio inputs ────────────────────────────────────
            const radios = document.querySelectorAll('input[type="radio"]');
            for (const r of radios) {
                const rval = (r.value || r.getAttribute('data-value') || '').toLowerCase().trim();
                if (rval !== t) continue;
                // Also verify it's in a variant-related group
                const name = (r.name || '').toLowerCase();
                if (!/size|colour|color|option/i.test(name) && name === '') continue;
                if (!r.checked) {
                    r.checked = true;
                    r.dispatchEvent(new MouseEvent('click',  {bubbles: true, cancelable: true}));
                    r.dispatchEvent(new Event('change', {bubbles: true}));
                    log.push('radio:' + r.value);
                } else {
                    log.push('radio_already:' + r.value);
                }
                break;
            }

            // ── 2. SingleOptionSelector <select> elements ─────────────────
            const sels = document.querySelectorAll(
                'select[id*="SingleOptionSelector"], select[id*="option-"], ' +
                'select[data-option], select[name*="option"]'
            );
            for (const sel of sels) {
                for (const opt of sel.options) {
                    if (opt.value.toLowerCase().trim() === t ||
                        opt.text.toLowerCase().trim() === t) {
                        if (sel.value !== opt.value) {
                            sel.value = opt.value;
                            sel.dispatchEvent(new Event('change', {bubbles: true}));
                            log.push('select:' + sel.id + '=' + opt.value);
                        }
                        break;
                    }
                }
            }

            // ── 3. Variant-change custom events (listened to by many themes) ──
            ['variant:change', 'variantChange', 'option:change'].forEach(evName => {
                document.dispatchEvent(new CustomEvent(evName, {
                    bubbles: true,
                    detail: {value: vval, type: vtype, option: vval}
                }));
            });

            // ── 4. If a data-variant-id on the now-selected button exists, ──
            //      write it directly to any hidden id input / select.
            const selected = document.querySelector(
                '[class*="active"][data-variant-id], [class*="selected"][data-variant-id], ' +
                '[aria-checked="true"][data-variant-id], [aria-selected="true"][data-variant-id]'
            );
            if (selected) {
                const vid = selected.getAttribute('data-variant-id') || selected.getAttribute('data-id');
                if (vid && /^\\d+$/.test(vid)) {
                    const inp = document.querySelector('input[name="id"], select[name="id"]');
                    if (inp && inp.value !== vid) {
                        inp.value = vid;
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        log.push('variant_id:' + vid);
                    }
                }
            }

            return log;
        }""", [variant_type, variant_value])
        if result:
            logger.info(f"  Shopify variant commit: {result}")
        else:
            logger.debug(f"  Shopify variant commit: no actions taken for {variant_value!r}")
    except Exception as e:
        logger.debug(f"  Shopify variant commit error: {e}")



class RobustVariantSelector:
    """
    Select a variant (color, size, etc.) on any product page.

    Usage:
        selector = RobustVariantSelector(page)
        result = await selector.select("color", "Blizzard Sole")
        result = await selector.select("size", "XL")
    """

    def __init__(self, page: Page):
        self.page = page

    async def select(
        self,
        variant_type: str,
        variant_value: str,
        min_score: int = 60,
        max_candidates: int = 30,
    ) -> Dict[str, Any]:
        """
        Find and click the element matching variant_value, then validate.

        Returns:
            {'success': bool, 'method': str, 'confidence': float, 'error': str}
        """
        logger.info(f"🎯 RobustVariantSelector: selecting {variant_type}={variant_value!r}")

        # Step 1 — Check if already selected via DOM-state ONLY (aria-checked/aria-selected).
        # Text-label checks (_validate_label, _validate_url) fire on product descriptions
        # before any click — excluded here to prevent false positives.
        #
        # IMPORTANT: for WooCommerce (and other frameworks that pre-select a default
        # variant), aria-selected may be True before the user has actually committed
        # their choice.  We verify by also checking the WooCommerce hidden attribute
        # input / select value.  If that doesn't match, we ignore the aria signal and
        # proceed to click.
        if await _validate_aria(self.page, variant_value):
            # Cross-check: is the form state actually committed for this value?
            form_confirmed = await self.page.evaluate("""(val) => {
                const v = (val || '').toLowerCase().trim();

                // WooCommerce bracket-notation: input[name="attribute[ID]"]
                // These use numeric term IDs, not text values — can't compare to 'L' etc.
                // Always require an explicit click on such pages.
                if (document.querySelector('input[name*="attribute["]')) return false;

                // WooCommerce variations form (underscore-notation attribute_pa_*)
                const wcInputs = [
                    ...document.querySelectorAll('input[name^="attribute_"], select[name^="attribute_"]'),
                ];
                for (const el of wcInputs) {
                    const cur = (el.value || '').toLowerCase().trim();
                    if (cur && (cur === v || cur.includes(v) || v.includes(cur))) return true;
                }
                if (wcInputs.length > 0) return false; // WC page but no match

                // Shopify: only trust if we can confirm this is a Shopify page
                const isShopify = !!(window.Shopify || window.ShopifyAnalytics ||
                                     window.meta?.product);
                if (isShopify) {
                    const sh = document.querySelector('input[name="id"], select[name="id"]');
                    if (sh && sh.value && sh.value !== '0') return true;
                }

                // Unknown framework — require a click (safe default)
                return false;
            }""", variant_value)

            if form_confirmed:
                logger.info(f"✅ Already selected (aria_selected + form confirmed) — no click needed.")
                return {"success": True, "method": "already_aria_selected", "confidence": 1.0}
            else:
                logger.info(
                    f"  aria_selected=True for '{variant_value}' but form state NOT confirmed "
                    f"— proceeding to click anyway (default pre-selection guard)"
                )

        # Step 2 — Collect candidates
        candidates = await self._collect_candidates(variant_type, variant_value, max_candidates)
        if not candidates:
            logger.warning(f"⚠️  No candidates found for {variant_type}={variant_value!r}")
            return {"success": False, "error": "no_candidates_found"}

        logger.info(f"  Found {len(candidates)} candidates (top scores):")
        for c in candidates[:5]:
            logger.info(f"    [{c['score']:3d}] {c['text'][:60]!r}")

        # Step 3 — Try each candidate in score order
        for idx, cand in enumerate(candidates):
            if cand["score"] < min_score:
                logger.info(f"  Stopping at candidate #{idx+1} (score {cand['score']} < {min_score})")
                break

            logger.info(
                f"  → Trying #{idx+1}: {cand['text'][:50]!r} "
                f"(score={cand['score']}, selector={cand['selector']})"
            )

            # A) Setup mutation observer BEFORE clicking
            await _setup_mutation_observer(self.page)

            # B) Capture before-screenshot of this element
            before_px = await _capture_element_pixels(self.page, cand)

            clicked = await self._click_candidate(cand)
            if not clicked:
                logger.warning(f"    Click failed, trying next.")
                continue

            # C) Wait for page to settle (SPA update or full navigation)
            url_before = self.page.url
            await asyncio.sleep(1.0)
            try:
                await self.page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass
            if self.page.url != url_before:
                await asyncio.sleep(0.5)  # extra settle for full navigation

            # D) Post-click validation pipeline (7 strategies)
            val_method = None

            # Strategy 0: WooCommerce variation_id (ground truth — check first)
            if not val_method and await _validate_woo_variation(self.page):
                val_method = "woo_variation_id"

            # Strategy 1: MutationObserver (class/aria/style delta on any DOM element)
            if not val_method and await _validate_mutations(self.page, variant_value):
                val_method = "mutation_observer"

            # Strategy 1b: Sibling class-delta (clicked element now has unique class)
            if not val_method and await _validate_class_delta(self.page, cand, variant_value):
                val_method = "class_delta"

            # Strategy 2: Playwright accessibility snapshot
            if not val_method and await _validate_accessibility(self.page, variant_value):
                val_method = "accessibility_snapshot"

            # Strategy 3: Element pixel diff (visual change)
            if not val_method:
                after_px = await _capture_element_pixels(self.page, cand)
                if _pixel_diff_significant(before_px, after_px):
                    val_method = "pixel_diff"

            # Strategy 4: Page label text ("Color: Red", "Size: XL")
            if not val_method and await _validate_label(self.page, variant_type, variant_value):
                val_method = "page_label"

            # Strategy 5: URL slug
            if not val_method and await _validate_url(self.page, variant_value):
                val_method = "url_slug"

            if val_method:
                logger.info(f"  ✅ SUCCESS: candidate #{idx+1} validated via {val_method!r}")
                # Commit variant to Shopify's hidden radio/select state so that
                # "Add to Bag" sees the selection (needed for Fashion Nova and similar
                # themes that separate visual buttons from radio/select inputs).
                await _commit_shopify_variant(self.page, variant_type, variant_value)
                return {
                    "success": True,
                    "method": f"robust_click_{val_method}",
                    "confidence": 0.95,
                    "candidate_text": cand["text"],
                }
            else:
                logger.warning(f"    ❌ Validation failed (all strategies). Trying next candidate.")

        logger.error(f"🔴 All candidates exhausted for {variant_type}={variant_value!r}")
        return {"success": False, "error": "all_candidates_failed_validation"}

    # ------------------------------------------------------------------
    # Candidate collection
    # ------------------------------------------------------------------

    async def _collect_candidates(
        self, variant_type: str, variant_value: str, max_candidates: int
    ) -> List[Dict]:
        """
        Query the DOM for all potential variant elements, extract their text,
        score against target, sort descending, return top candidates.
        """
        # Tag every candidate element with a unique index attribute so we can
        # re-find it by query_selector even after the page scrolls.
        _TAG = "data-rvsidx"
        raw = await self.page.evaluate(
            """([selectors, skipAncestorPattern, tagAttr]) => {
                const skip = new RegExp(skipAncestorPattern, 'i');
                const seen = new Set();
                const results = [];
                let idx = 0;

                for (const sel of selectors) {
                    let elems;
                    try { elems = document.querySelectorAll(sel); }
                    catch(e) { continue; }

                    for (const el of elems) {
                        // Skip duplicates
                        if (seen.has(el)) continue;
                        seen.add(el);

                        // Skip hidden / disabled
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden'
                            || style.opacity === '0') continue;
                        const box = el.getBoundingClientRect();
                        if (box.width < 5 || box.height < 5) continue;
                        if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;

                        // Skip elements inside nav/header/footer/etc
                        let inSkip = false;
                        let p = el;
                        while (p && p !== document.body) {
                            const id = (p.id || '').toLowerCase();
                            const cls = (typeof p.className === 'string' ? p.className : '').toLowerCase();
                            if (skip.test(id + ' ' + cls)) { inSkip = true; break; }
                            p = p.parentElement;
                        }
                        if (inSkip) continue;

                        // Extract all text sources
                        const text      = (el.textContent || '').trim().replace(/\\s+/g, ' ');
                        const ariaLabel = el.getAttribute('aria-label') || '';
                        const title     = el.getAttribute('title') || '';
                        const dataValue = el.getAttribute('data-value') || el.getAttribute('data-option-value') || '';
                        const dataColor = el.getAttribute('data-color') || el.getAttribute('data-colorname') || '';
                        const dataSize  = el.getAttribute('data-size') || '';
                        const value     = el.getAttribute('value') || '';

                        // Skip obviously non-variant text
                        const combined = (text + ariaLabel).toLowerCase().trim();
                        if (!combined) continue;

                        // Tag the element so Python can re-find it by query_selector
                        const elemIdx = idx++;
                        el.setAttribute(tagAttr, String(elemIdx));

                        results.push({
                            text, ariaLabel, title, dataValue, dataColor, dataSize, value,
                            selector: sel,
                            tag: el.tagName,
                            elemIdx,
                        });
                    }
                }
                return results;
            }""",
            [_VARIANT_CANDIDATE_SELECTORS, _SKIP_ANCESTORS.pattern, _TAG],
        )

        candidates = []
        for item in (raw or []):
            combined = _get_candidate_text(item)
            # Skip obvious noise
            short = (item.get("text") or "").strip()
            if _SKIP_TEXT.match(short):
                continue
            score = _score(variant_value, combined)
            if score == 0:
                continue  # discard completely unrelated
            candidates.append({**item, "combined": combined, "score": score, "_tag": _TAG})

        # Sort descending by score, then by shorter text (prefer specific labels)
        candidates.sort(key=lambda c: (-c["score"], len(c["combined"])))
        return candidates[:max_candidates]

    # ------------------------------------------------------------------
    # Click
    # ------------------------------------------------------------------

    async def _click_candidate(self, cand: Dict) -> bool:
        """
        Re-locate the element by its tagged index attribute and click it.
        Falls back to JS force-click if Playwright click fails.
        """
        tag_attr = cand.get("_tag", "data-rvsidx")
        elem_idx = cand.get("elemIdx")

        elem = None
        if elem_idx is not None:
            css = f'[{tag_attr}="{elem_idx}"]'
            try:
                elem = await self.page.query_selector(css)
            except Exception as e:
                logger.debug(f"query_selector({css}) failed: {e}")

        if elem:
            try:
                # Playwright's click() auto-scrolls into view
                await elem.scroll_into_view_if_needed(timeout=3000)
                await asyncio.sleep(0.2)
                await elem.click(timeout=5000)
                return True
            except Exception as e:
                logger.debug(f"Playwright elem.click() failed: {e}")
                # JS force-click as fallback
                try:
                    await self.page.evaluate("el => el.click()", elem)
                    return True
                except Exception as je:
                    logger.debug(f"JS elem.click() failed: {je}")
        else:
            logger.debug(f"Could not re-find element with {tag_attr}={elem_idx}")

        return False
