"""
DOM Intelligence Tool
=====================
A comprehensive DOM analysis utility that scans the live page to understand its
structure and find the correct clickable elements.  Called automatically after a
first-attempt failure so that subsequent tiers can make evidence-based decisions
instead of guessing.

Usage
-----
    from app.ecommerce.utils.dom_intelligence import DOMIntelligence

    dom = DOMIntelligence(page, logger)
    await dom.analyze()                        # full page scan
    intent = dom.find_best_match("add to cart")  # returns DOMMatch or None
    await dom.click(intent)                    # smart click (mouse + dispatchEvent)
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DOMElement:
    """Represents a candidate clickable element found in the DOM."""
    tag: str
    text: str                    # direct / full text of the element
    x: float                     # viewport centre-x
    y: float                     # viewport centre-y
    w: float                     # bounding-box width
    h: float                     # bounding-box height
    role: str = ""               # ARIA role
    aria_label: str = ""
    href: str = ""               # for <a> tags
    classes: str = ""
    cursor: str = "auto"
    is_visible: bool = True
    is_in_header: bool = False   # True if y < 80  (sticky nav / top bar)
    is_in_footer: bool = False   # True if element is near bottom-of-document
    is_in_sticky: bool = False   # True if CSS position=fixed/sticky
    depth_score: float = 0.0     # higher = more leaf-like (preferred for clicking)
    area: float = 0.0
    intel_id: str = ""           # unique identifier for target clicking

    @property
    def center(self):
        return (self.x, self.y)


@dataclass
class DOMMatch:
    """The best candidate for a given intent, with metadata for logging."""
    element: DOMElement
    intent: str
    score: float
    reason: str


@dataclass
class DOMPageSummary:
    """High-level summary of what was found during a full DOM scan."""
    title: str = ""
    url: str = ""
    has_add_to_cart: bool = False
    has_buy_now: bool = False
    has_go_to_cart: bool = False
    has_size_selector: bool = False
    has_color_selector: bool = False
    has_login_form: bool = False
    has_checkout_form: bool = False
    detected_site: str = ""      # 'flipkart' | 'amazon' | 'generic'
    variant_labels: List[str] = field(default_factory=list)
    cta_buttons: List[DOMElement] = field(default_factory=list)
    nav_links: List[DOMElement] = field(default_factory=list)
    all_elements: List[DOMElement] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Intent patterns
# ─────────────────────────────────────────────────────────────────────────────

_INTENT_PATTERNS: Dict[str, List[str]] = {
    "add_to_cart": [
        "add to cart", "add to bag", "add to basket", "addtocart",
    ],
    "buy_now": [
        "buy now", "buy at", "purchase now",
    ],
    "go_to_cart": [
        "go to cart", "view cart", "go to bag", "view bag",
        "view basket", "go to basket",
    ],
    "proceed_to_checkout": [
        "proceed to checkout", "secure checkout", "checkout now",
        "place order", "proceed to pay", "pay now",
    ],
    "checkout": [
        "checkout", "check out",
    ],
    "continue": [
        "continue", "next", "proceed",
    ],
    "login": [
        "login", "log in", "sign in", "signin",
    ],
    "apply_coupon": [
        "apply", "apply coupon", "use coupon",
    ],
    "confirm_address": [
        "deliver here", "use this address", "confirm address",
    ],
}


def _score_element_for_intent(el: DOMElement, intent: str,
                               patterns: List[str]) -> float:
    """
    Score how well a DOMElement matches the intent.
    Returns 0.0 (no match) … 100.0 (perfect).
    """
    text_lc = el.text.strip().lower()
    score = 0.0

    # ── Exact text match
    if text_lc in patterns:
        score += 60.0

    # ── Partial / contains match
    elif any(p in text_lc for p in patterns):
        score += 30.0

    # ── aria-label match
    if el.aria_label and any(p in el.aria_label.lower() for p in patterns):
        score += 20.0

    if score == 0.0:
        return 0.0

    # ── Prefer actual interactive tags
    if el.tag in ("BUTTON", "A", "INPUT"):
        score += 15.0

    # ── Prefer pointer cursor
    if el.cursor == "pointer":
        score += 10.0

    # ── Prefer role=button
    if el.role in ("button", "link"):
        score += 8.0

    # ── Penalise header / sticky elements for cart-action intents
    if intent in ("add_to_cart", "go_to_cart", "buy_now", "proceed_to_checkout"):
        if el.is_in_header:
            score -= 40.0
        if el.is_in_sticky and el.y < 80:
            score -= 30.0

    # ── Prefer reasonably-sized elements (not tiny labels, not huge containers)
    if 30 <= el.w <= 400 and 20 <= el.h <= 80:
        score += 10.0
    elif el.area > 200_000:          # full-viewport container → penalise
        score -= 20.0

    # ── Prefer leaf-like elements (high depth_score)
    score += el.depth_score * 5.0

    return max(score, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# Main class
# ─────────────────────────────────────────────────────────────────────────────

class DOMIntelligence:
    """
    Analyses a live Playwright page to build a structured understanding of
    its interactive elements.  Call ``analyze()`` once after a failure, then
    use ``find_best_match(intent)`` + ``click()`` for evidence-based retries.
    """

    # JS that extracts every candidate element from the page
    _SCAN_JS = r"""
    () => {
        const VIEWPORT_H = window.innerHeight || document.documentElement.clientHeight;
        const VIEWPORT_W = window.innerWidth  || document.documentElement.clientWidth;
        const DOC_H      = document.documentElement.scrollHeight;

        function directText(el) {
            return Array.from(el.childNodes)
                .filter(n => n.nodeType === 3)
                .map(n => n.textContent.trim())
                .join(' ')
                .trim();
        }

        function fullText(el) {
            return (el.textContent || '').trim().replace(/\s+/g, ' ');
        }

        // Get absolute layout path or stable identifier
        function isFixed(el) {
            let node = el;
            while (node && node !== document.body) {
                const p = window.getComputedStyle(node).position;
                if (p === 'fixed' || p === 'sticky') return true;
                node = node.parentElement;
            }
            return false;
        }

        // Compute a "depth score" — how many children does the element have?
        // Fewer children → more leaf-like → preferred for clicking.
        function depthScore(el) {
            const kids = el.querySelectorAll('*').length;
            if (kids === 0) return 1.0;
            if (kids <= 2) return 0.8;
            if (kids <= 5) return 0.5;
            if (kids <= 15) return 0.2;
            return 0.0;
        }

        const TAGS = ['button','a','input','div','span','li','label','p','h1','h2','h3','section'];
        const all  = document.querySelectorAll(TAGS.join(','));
        const results = [];
        let idCounter = 0;

        for (const el of all) {
            const style   = window.getComputedStyle(el);
            const display = style.display;
            const vis     = style.visibility;
            const opacity = parseFloat(style.opacity || '1');
            if (display === 'none' || vis === 'hidden' || opacity < 0.1) continue;

            const rect  = el.getBoundingClientRect();
            if (rect.width < 2 || rect.height < 2) continue;
            // No off-screen viewport filtering to allow capturing elements anywhere on the page

            const tag   = el.tagName;
            const dt    = directText(el);
            const ft    = fullText(el).substring(0, 200);
            if (!dt && !ft) continue;

            // Generate or retrieve unique attribute ID
            let intelId = el.getAttribute('data-dom-intel-id');
            if (!intelId) {
                intelId = 'intel-' + (++idCounter);
                el.setAttribute('data-dom-intel-id', intelId);
            }

            // Calculate absolute page coordinates (independent of viewport scroll state)
            const cx    = rect.left + window.scrollX + rect.width  / 2;
            const cy    = rect.top  + window.scrollY + rect.height / 2;
            const area  = rect.width * rect.height;

            results.push({
                tag,
                intelId,
                directText : dt.substring(0, 150),
                fullText   : ft,
                x          : Math.round(cx),
                y          : Math.round(cy),
                w          : Math.round(rect.width),
                h          : Math.round(rect.height),
                area       : Math.round(area),
                role       : el.getAttribute('role') || '',
                ariaLabel  : el.getAttribute('aria-label') || '',
                href       : tag === 'A' ? (el.href || '') : '',
                classes    : el.className ? el.className.toString().substring(0, 120) : '',
                cursor     : style.cursor,
                isInHeader : (rect.top + rect.height / 2) < 80,
                isInSticky : isFixed(el),
                depthScore : depthScore(el),
            });
        }

        return results;
    }
    """

    # JS snippet to dispatch a synthetic click (bypasses iframe/overlay intercepts)
    _DISPATCH_JS = """
    ({x, y}) => {
        const el = document.elementFromPoint(x, y);
        if (!el) return false;
        el.dispatchEvent(new MouseEvent('click', {
            bubbles: true, cancelable: true, view: window,
            clientX: x, clientY: y
        }));
        return true;
    }
    """

    def __init__(self, page, logger: Optional[logging.Logger] = None):
        self.page    = page
        self.logger  = logger or logging.getLogger(__name__)
        self.summary = DOMPageSummary()
        self._raw: List[Dict[str, Any]] = []

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    async def analyze(self, scroll_before: bool = True) -> DOMPageSummary:
        """
        Perform a full DOM scan.  Returns a DOMPageSummary and populates
        internal state for subsequent ``find_best_match`` calls.

        Parameters
        ----------
        scroll_before:
            If True, scroll to top before scanning so that sticky elements
            are in their default positions.
        """
        try:
            if scroll_before:
                await self.page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(0.3)

            self._raw = await self.page.evaluate(self._SCAN_JS)
            self.logger.info(
                f"[DOMIntelligence] Raw scan: {len(self._raw)} candidate elements"
            )

            elements = [self._to_dom_element(r) for r in self._raw]
            self.summary = self._build_summary(elements)
            self._log_summary()
            return self.summary

        except Exception as exc:
            self.logger.error(f"[DOMIntelligence] analyze() failed: {exc}", exc_info=True)
            return DOMPageSummary()

    def find_best_match(self, intent: str,
                        min_score: float = 20.0) -> Optional[DOMMatch]:
        """
        Return the single best DOMElement matching the intent keyword.

        ``intent`` can be a free-form string (e.g. ``"add to cart"``) or one of
        the canonical keys in ``_INTENT_PATTERNS``.

        Returns ``None`` if nothing scores above ``min_score``.
        """
        patterns = self._resolve_patterns(intent)
        best: Optional[DOMMatch] = None

        for el in self.summary.all_elements:
            score = _score_element_for_intent(el, intent, patterns)
            if score < min_score:
                continue
            if best is None or score > best.score:
                reason = self._explain(el, patterns)
                best = DOMMatch(element=el, intent=intent,
                                score=score, reason=reason)

        if best:
            self.logger.info(
                f"[DOMIntelligence] Best match for '{intent}': "
                f"{best.element.tag} '{best.element.text[:60]}' "
                f"@ ({best.element.x},{best.element.y}) "
                f"score={best.score:.1f}  reason={best.reason}"
            )
        else:
            self.logger.warning(
                f"[DOMIntelligence] No match found for intent='{intent}'"
            )

        return best

    def find_all_matches(self, intent: str,
                         min_score: float = 10.0) -> List[DOMMatch]:
        """Return all elements matching the intent, sorted by score desc."""
        patterns = self._resolve_patterns(intent)
        matches: List[DOMMatch] = []

        for el in self.summary.all_elements:
            score = _score_element_for_intent(el, intent, patterns)
            if score >= min_score:
                reason = self._explain(el, patterns)
                matches.append(DOMMatch(element=el, intent=intent,
                                        score=score, reason=reason))

        matches.sort(key=lambda m: m.score, reverse=True)
        return matches

    async def click(self, match: DOMMatch,
                    use_dispatch: bool = True,
                    use_mouse: bool = True) -> bool:
        """
        Click the best-matched element.

        Strategy:
        1. Scroll near the element first to center it in viewport.
        2. Query the live bounding box in viewport coordinates using its unique ID.
        3. Dispatch Event click (bypasses iframe / overlay intercepts).
        4. Perform mouse.click at the live coordinate.
        5. Returns True if click was performed.
        """
        el = match.element
        self.logger.info(
            f"[DOMIntelligence] Clicking '{match.intent}' → "
            f"{el.tag} '{el.text[:50]}' (id={el.intel_id}) @ page absolute ({el.x},{el.y})"
        )

        clicked = False

        # 1. Scroll near the element first to make it active/visible in viewport
        try:
            viewport = self.page.viewport_size
            vw = viewport['width'] if viewport else 1280
            vh = viewport['height'] if viewport else 800
            
            scroll_x = max(0, int(el.x - vw / 2))
            scroll_y = max(0, int(el.y - vh / 2))
            
            self.logger.info(f"[DOMIntelligence] Scrolling window to ({scroll_x}, {scroll_y}) to center target")
            await self.page.evaluate(f"window.scrollTo({scroll_x}, {scroll_y})")
            await asyncio.sleep(0.3)
        except Exception as e:
            self.logger.warning(f"[DOMIntelligence] Scroll failed: {e}")

        # 2. Query the live bounding box in viewport coordinates
        live_coords = None
        if el.intel_id:
            try:
                live_coords = await self.page.evaluate("""
                    ({id}) => {
                        const el = document.querySelector(`[data-dom-intel-id="${id}"]`);
                        if (!el) return null;
                        const rect = el.getBoundingClientRect();
                        return {
                            x: Math.round(rect.left + rect.width / 2),
                            y: Math.round(rect.top + rect.height / 2),
                            w: rect.width,
                            h: rect.height
                        };
                    }
                """, {"id": el.intel_id})
            except Exception as e:
                self.logger.warning(f"[DOMIntelligence] Querying live coords failed: {e}")

        # Fallback to viewport coordinates estimated from scroll
        if live_coords:
            vx, vy = live_coords['x'], live_coords['y']
            self.logger.info(f"[DOMIntelligence] Live element found in viewport at ({vx}, {vy}) size={live_coords['w']}x{live_coords['h']}")
        else:
            # Fallback using original absolute coordinates minus scroll
            try:
                scroll_pos = await self.page.evaluate("() => ({x: window.scrollX, y: window.scrollY})")
                actual_scroll_x = scroll_pos['x']
                actual_scroll_y = scroll_pos['y']
            except Exception:
                actual_scroll_x = scroll_x
                actual_scroll_y = scroll_y
            vx = el.x - actual_scroll_x
            vy = el.y - actual_scroll_y
            self.logger.info(f"[DOMIntelligence] Live element query returned null, falling back to estimated viewport coords ({vx}, {vy})")

        # 3. Perform dispatch click
        if use_dispatch and el.intel_id:
            try:
                ok = await self.page.evaluate("""
                    ({id}) => {
                        const el = document.querySelector(`[data-dom-intel-id="${id}"]`);
                        if (!el) return false;
                        el.dispatchEvent(new MouseEvent('click', {
                            bubbles: true, cancelable: true, view: window
                        }));
                        return true;
                    }
                """, {"id": el.intel_id})
                if ok:
                    self.logger.info("[DOMIntelligence] dispatchEvent click sent")
                    clicked = True
                    await asyncio.sleep(0.4)
            except Exception as e:
                self.logger.warning(f"[DOMIntelligence] dispatchEvent failed: {e}")

        # 4. Perform mouse click at coords
        if use_mouse:
            try:
                await self.page.mouse.click(vx, vy)
                self.logger.info(f"[DOMIntelligence] mouse.click sent at viewport ({vx}, {vy})")
                clicked = True
            except Exception as e:
                self.logger.warning(f"[DOMIntelligence] mouse.click failed at ({vx}, {vy}): {e}")

        return clicked

    async def navigate_href(self, match: DOMMatch) -> bool:
        """
        If the matched element is an <A> with a relevant href, navigate
        directly instead of clicking.  Returns True on success.
        """
        el = match.element
        if el.href and el.href.startswith("http"):
            self.logger.info(
                f"[DOMIntelligence] Direct navigation to: {el.href}"
            )
            try:
                await self.page.goto(el.href)
                await asyncio.sleep(2)
                return True
            except Exception as e:
                self.logger.warning(
                    f"[DOMIntelligence] navigate_href failed: {e}"
                )
        return False

    def log_page_report(self) -> None:
        """
        Print a human-readable DOM intelligence report to the logger.
        Useful for debugging failures.
        """
        s = self.summary
        self.logger.info("=" * 70)
        self.logger.info("[DOMIntelligence] PAGE REPORT")
        self.logger.info(f"  Title   : {s.title}")
        self.logger.info(f"  URL     : {s.url}")
        self.logger.info(f"  Site    : {s.detected_site}")
        self.logger.info(f"  ATC     : {s.has_add_to_cart}")
        self.logger.info(f"  BuyNow  : {s.has_buy_now}")
        self.logger.info(f"  GTC     : {s.has_go_to_cart}")
        self.logger.info(f"  Size    : {s.has_size_selector}")
        self.logger.info(f"  Color   : {s.has_color_selector}")
        self.logger.info(f"  Login   : {s.has_login_form}")
        self.logger.info(f"  Labels  : {s.variant_labels}")
        self.logger.info(f"  CTAs    : {len(s.cta_buttons)}")
        for cta in s.cta_buttons[:8]:
            self.logger.info(
                f"    [{cta.tag}] '{cta.text[:50]}' "
                f"@ ({cta.x},{cta.y}) {cta.w}x{cta.h} "
                f"cursor={cta.cursor}"
            )
        self.logger.info("=" * 70)

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _to_dom_element(self, raw: Dict[str, Any]) -> DOMElement:
        text = raw.get("directText") or raw.get("fullText", "")
        return DOMElement(
            tag        = raw.get("tag", ""),
            text       = text[:150],
            x          = raw.get("x", 0),
            y          = raw.get("y", 0),
            w          = raw.get("w", 0),
            h          = raw.get("h", 0),
            role       = raw.get("role", ""),
            aria_label = raw.get("ariaLabel", ""),
            href       = raw.get("href", ""),
            classes    = raw.get("classes", ""),
            cursor     = raw.get("cursor", "auto"),
            is_in_header = raw.get("isInHeader", False),
            is_in_sticky = raw.get("isInSticky", False),
            depth_score  = raw.get("depthScore", 0.0),
            area       = raw.get("area", 0),
            intel_id   = raw.get("intelId", ""),
        )

    def _build_summary(self, elements: List[DOMElement]) -> DOMPageSummary:
        try:
            title = self.page.url  # fallback
        except Exception:
            title = ""

        s = DOMPageSummary(
            title = title,
            url   = title,
            all_elements = elements,
        )

        # Detect site
        url_lc = title.lower()
        if "flipkart" in url_lc:
            s.detected_site = "flipkart"
        elif "amazon" in url_lc:
            s.detected_site = "amazon"
        elif "myntra" in url_lc:
            s.detected_site = "myntra"
        elif "meesho" in url_lc:
            s.detected_site = "meesho"
        else:
            s.detected_site = "generic"

        atc_patterns   = _INTENT_PATTERNS["add_to_cart"]
        buy_patterns   = _INTENT_PATTERNS["buy_now"]
        gtc_patterns   = _INTENT_PATTERNS["go_to_cart"]
        size_patterns  = ["size", "uk", "us", "eu", "inch", "cm", "s", "m", "l", "xl", "xxl"]
        color_patterns = ["color", "colour", "shade"]
        login_patterns = ["login", "sign in", "log in", "otp", "password", "email"]
        checkout_patterns = _INTENT_PATTERNS["proceed_to_checkout"] + \
                            _INTENT_PATTERNS["checkout"]

        variant_label_candidates: List[str] = []

        for el in elements:
            text_lc = el.text.strip().lower()

            # CTAs
            if any(p in text_lc for p in atc_patterns):
                s.has_add_to_cart = True
                if not el.is_in_header:
                    s.cta_buttons.append(el)

            if any(p in text_lc for p in buy_patterns) and not el.is_in_header:
                s.has_buy_now = True
                s.cta_buttons.append(el)

            if any(p in text_lc for p in gtc_patterns):
                s.has_go_to_cart = True

            if any(p in text_lc for p in checkout_patterns) and not el.is_in_header:
                s.cta_buttons.append(el)

            # Variant indicators
            if any(p in text_lc for p in size_patterns):
                s.has_size_selector = True
            if any(p in text_lc for p in color_patterns):
                s.has_color_selector = True

            # Login / checkout forms
            if any(p in text_lc for p in login_patterns):
                s.has_login_form = True
            if any(p in text_lc for p in checkout_patterns):
                s.has_checkout_form = True

            # Detect variant labels like "Selected Color:", "Selected Size:"
            m = re.match(r'^(selected\s+)?(color|colour|size|width|length|style)[\s:]*(.{0,30})',
                         text_lc)
            if m:
                label = m.group(0).strip()
                if label not in variant_label_candidates:
                    variant_label_candidates.append(label)

        s.variant_labels = variant_label_candidates[:10]

        # De-duplicate CTAs by (x, y, text)
        seen = set()
        deduped = []
        for c in s.cta_buttons:
            key = (c.x, c.y, c.text[:40])
            if key not in seen:
                seen.add(key)
                deduped.append(c)
        s.cta_buttons = deduped

        return s

    def _log_summary(self) -> None:
        s = self.summary
        self.logger.info(
            f"[DOMIntelligence] Scan complete — "
            f"site={s.detected_site}  ATC={s.has_add_to_cart}  "
            f"BuyNow={s.has_buy_now}  GTC={s.has_go_to_cart}  "
            f"size={s.has_size_selector}  color={s.has_color_selector}  "
            f"login={s.has_login_form}  "
            f"CTAs={len(s.cta_buttons)}  "
            f"total_els={len(s.summary.all_elements)}"
        ).replace("s.summary.", "s.") if False else None
        self.logger.info(
            f"[DOMIntelligence] Scan → "
            f"site={s.detected_site} | ATC={s.has_add_to_cart} | "
            f"GTC={s.has_go_to_cart} | login={s.has_login_form} | "
            f"CTAs={len(s.cta_buttons)} | total={len(s.all_elements)} els"
        )

    def _resolve_patterns(self, intent: str) -> List[str]:
        """Return the pattern list for an intent string."""
        key = intent.lower().replace(" ", "_").replace("-", "_")
        # Try exact key first
        if key in _INTENT_PATTERNS:
            return _INTENT_PATTERNS[key]
        # Try substring match on keys
        for k, v in _INTENT_PATTERNS.items():
            if k in key or key in k:
                return v
        # Fallback: treat the raw intent as the pattern list
        return [intent.lower()]

    @staticmethod
    def _explain(el: DOMElement, patterns: List[str]) -> str:
        reasons = []
        text_lc = el.text.lower()
        if text_lc in patterns:
            reasons.append("exact-text")
        elif any(p in text_lc for p in patterns):
            reasons.append("partial-text")
        if el.aria_label:
            reasons.append("aria-label")
        if el.tag in ("BUTTON", "A", "INPUT"):
            reasons.append(f"tag={el.tag}")
        if el.cursor == "pointer":
            reasons.append("cursor:pointer")
        if el.is_in_header:
            reasons.append("⚠️ in-header")
        return ", ".join(reasons) or "text-match"


# ─────────────────────────────────────────────────────────────────────────────
# Convenience async function (drop-in replacement for dom_fallback)
# ─────────────────────────────────────────────────────────────────────────────

async def dom_find_and_click(page, intent: str,
                             logger: Optional[logging.Logger] = None,
                             analyze: bool = True) -> Optional[DOMMatch]:
    """
    One-shot helper:
    1. Scan the page DOM.
    2. Find the best element for ``intent``.
    3. Click it (dispatchEvent + mouse).
    4. Return the DOMMatch or None.
    """
    di = DOMIntelligence(page, logger)
    if analyze:
        await di.analyze()
    match = di.find_best_match(intent)
    if match:
        await di.click(match)
    return match
