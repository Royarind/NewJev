"""
Google Shopping Browser Automation Service for CARTMIND Phase 2
Navigates to Google Search -> Clicks the 'Shopping' Tab -> Extracts Live Google Shopping Refinement Filters
(Installation, Brand, Shops, Capacity, Energy Rating, Processor, RAM, Price)
-> Applies Selected Filters -> Narrows Real Google Shopping Results Live.
"""

import asyncio
import logging
import re
import os
import urllib.parse
from typing import Dict, Any, List, Optional
from playwright.async_api import async_playwright, Page, BrowserContext, Browser

logger = logging.getLogger("CARTMIND-GOOGLE-SHOPPING")

FILTER_PRIORITY_ORDER = [
    "price", "budget", "cost",
    "installation", "type", "capacity", "tonnage",
    "brand", "processor", "ram", "memory", "storage", "drive",
    "energy rating", "star rating",
    "seller", "shop", "merchant",
    "color", "colour",
    "size", "fit", "screen",
    "neckline", "sleeve", "material", "fabric",
    "department", "category", "rating"
]


class GoogleShoppingSession:
    def __init__(self, session_id: str, headless: bool = False):
        self.session_id = session_id
        env_headless = os.environ.get("HEADLESS", "false").lower() in ("true", "1")
        self.headless = env_headless if "HEADLESS" in os.environ else headless
        self.query: str = ""
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.applied_filters: Dict[str, Any] = {}
        self.extracted_filters: List[Dict[str, Any]] = []

    async def initialize(self):
        """Initialize Playwright browser context"""
        if not self.playwright:
            self.playwright = await async_playwright().start()
            logger.info(f"Launching Chromium with headless={self.headless}...")
            self.browser = await self.playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                ]
            )
            self.context = await self.browser.new_context(
                viewport={"width": 1366, "height": 768},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                extra_http_headers={
                    "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                }
            )
            self.page = await self.context.new_page()

    async def search(self, query: str) -> Dict[str, Any]:
        """Search Google, navigate to the Shopping tab, and extract refinement options"""
        await self.initialize()
        self.query = query
        self.applied_filters = {}

        encoded_query = urllib.parse.quote(query)
        search_url = f"https://www.google.com/search?q={encoded_query}"
        logger.info(f"Session {self.session_id}: Navigating to Google Search: {search_url}")

        try:
            await self.page.goto(search_url, wait_until="domcontentloaded", timeout=35000)
            await asyncio.sleep(2)

            # Click the 'Shopping' tab on Google to open Google Shopping
            logger.info("Clicking the 'Shopping' tab link on Google Search...")
            shopping_tab = await self.page.query_selector('a:has-text("Shopping"), a[href*="udm=28"], a[href*="tbm=shop"]')
            if shopping_tab:
                await shopping_tab.click()
                await asyncio.sleep(2.5)
                logger.info(f"Navigated to Google Shopping page: {self.page.url}")

            filters = await self.extract_live_google_filters()
            self.extracted_filters = filters

            return {
                "session_id": self.session_id,
                "query": self.query,
                "filters": filters,
                "applied_filters": self.applied_filters,
                "status": "active"
            }
        except Exception as e:
            logger.error(f"Error navigating Google Shopping for '{query}': {e}", exc_info=True)
            filters = self.extract_context_aware_filters(query)
            return {
                "session_id": self.session_id,
                "query": self.query,
                "filters": filters,
                "applied_filters": self.applied_filters,
                "status": "active"
            }

    async def extract_live_google_filters(self) -> List[Dict[str, Any]]:
        """Extract live refinement categories directly from Google Shopping using Vision AI + DOM Fallback"""
        if not self.page:
            return []

        filters: List[Dict[str, Any]] = []

        # 1. Take Base64 Screenshot of live page for Vision-AI model analysis
        try:
            screenshot_bytes = await self.page.screenshot(full_page=False)
            import base64
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

            from app.ecommerce.vision.openrouter_vision import OpenRouterVisionProvider
            vision_provider = OpenRouterVisionProvider()
            vlm_filters = await vision_provider.extract_filters_from_screenshot(screenshot_b64)

            if vlm_filters and len(vlm_filters) >= 2:
                logger.info(f"Using OpenRouter Vision VLM extracted filters ({len(vlm_filters)} groups)")
                return vlm_filters
        except Exception as v_err:
            logger.warning(f"Vision AI extraction fallback triggered: {v_err}")

        # 2. Robust DOM Section Container Parsing (Backup Strategy)
        try:
            raw_filters = await self.page.evaluate("""() => {
                const results = [];
                const pageText = document.body.innerText || '';

                if (pageText.includes('unusual traffic')) {
                    return { isBlocked: true, groups: [] };
                }

                const sectionBlocks = Array.from(document.querySelectorAll('div[data-hveid]'));
                const seenCategories = new Set();

                sectionBlocks.forEach(block => {
                    const headerEl = block.querySelector('div[jsname="tHJJj"], div[role="button"], span.rcFVfb, div.dnXCYb');
                    if (!headerEl) return;

                    const titleText = (headerEl.innerText || '').trim().split('\\n')[0].trim();
                    if (!titleText || titleText.length < 2 || titleText.length > 30) return;

                    const lowerTitle = titleText.toLowerCase();
                    if (lowerTitle.includes('sort') || lowerTitle.includes('rating') || lowerTitle.includes('filter') || lowerTitle.includes('search')) return;
                    if (seenCategories.has(lowerTitle)) return;

                    const optionElements = Array.from(block.querySelectorAll('a, label, div[role="checkbox"], span[jsname="r4nke"]'));
                    const options = [];

                    optionElements.forEach(el => {
                        const txt = (el.innerText || el.getAttribute('aria-label') || '').trim().split('\\n')[0].trim();
                        if (
                            txt &&
                            txt.length >= 1 &&
                            txt.length < 35 &&
                            txt.toLowerCase() !== lowerTitle &&
                            !txt.toLowerCase().includes('refine') &&
                            !options.includes(txt)
                        ) {
                            options.push(txt);
                        }
                    });

                    if (options.length >= 2 && options.length <= 15) {
                        seenCategories.add(lowerTitle);
                        results.push({
                            category: titleText,
                            options: options.slice(0, 10)
                        });
                    }
                });

                return {
                    isBlocked: false,
                    groups: results
                };
            }""")

            if not raw_filters.get("isBlocked") and raw_filters.get("groups"):
                for idx, group in enumerate(raw_filters["groups"]):
                    cat = group["category"]
                    opts = group["options"]
                    options_data = []

                    for o_idx, opt in enumerate(opts):
                        options_data.append({
                            "id": f"opt_{idx}_{o_idx}",
                            "label": opt,
                            "value": opt
                        })

                    cat_lower = cat.lower()
                    filters.append({
                        "id": f"filter_live_{idx}",
                        "category": cat,
                        "isPrice": "price" in cat_lower or "budget" in cat_lower,
                        "isColor": "col" in cat_lower,
                        "isMultiSelect": any(k in cat_lower for k in ["brand", "feature", "shop", "installation", "material"]),
                        "options": options_data
                    })

        except Exception as e:
            logger.warning(f"Error parsing live Google Shopping filters: {e}")

        if len(filters) < 2:
            fallback_filters = self.extract_context_aware_filters(self.query)
            for fb in fallback_filters:
                if not any(f["category"].lower() == fb["category"].lower() for f in filters):
                    filters.append(fb)

        filters.sort(key=self._filter_priority_key)
        return filters

    def _filter_priority_key(self, filter_item: Dict[str, Any]) -> int:
        cat = filter_item["category"].lower()
        for idx, keyword in enumerate(FILTER_PRIORITY_ORDER):
            if keyword in cat:
                return idx
        return 99

    def extract_context_aware_filters(self, query: str) -> List[Dict[str, Any]]:
        """Dynamically build product-agnostic context-aware filter categories matching user query"""
        q = query.lower()
        filters: List[Dict[str, Any]] = []

        # 1. Dynamic Budget / Price Section
        numbers = re.findall(r'\b\d+(?:,\d+)*(?:\.\d+)?\b', query.replace(',', ''))
        target_budget = None
        if numbers:
            try:
                nums = [float(n) for n in numbers if float(n) > 50]
                if nums:
                    target_budget = max(nums)
            except ValueError:
                pass

        if target_budget and target_budget > 1000:
            val = int(target_budget)
            low = int(val * 0.6)
            mid = val
            high = int(val * 1.3)
            filters.append({
                "id": "filter_budget",
                "category": "Price",
                "isPrice": True,
                "isColor": False,
                "isMultiSelect": False,
                "options": [
                    {"id": "b1", "label": f"Under ₹{low:,}", "value": f"Under ₹{low:,}"},
                    {"id": "b2", "label": f"₹{low:,} – ₹{mid:,}", "value": f"₹{low:,} – ₹{mid:,}"},
                    {"id": "b3", "label": f"₹{mid:,} – ₹{high:,}", "value": f"₹{mid:,} – ₹{high:,}"},
                    {"id": "b4", "label": f"Over ₹{high:,}", "value": f"Over ₹{high:,}"},
                ]
            })
        else:
            filters.append({
                "id": "filter_budget",
                "category": "Price",
                "isPrice": True,
                "isColor": False,
                "isMultiSelect": False,
                "options": [
                    {"id": "b1", "label": "Under ₹1,000", "value": "Under ₹1,000"},
                    {"id": "b2", "label": "₹1,000 – ₹3,000", "value": "₹1,000 – ₹3,000"},
                    {"id": "b3", "label": "₹3,000 – ₹7,000", "value": "₹3,000 – ₹7,000"},
                    {"id": "b4", "label": "Over ₹7,000", "value": "Over ₹7,000"},
                ]
            })

        # 2. Product-Agnostic Category & Type Refinement
        words = [w.capitalize() for w in re.findall(r'\b[a-zA-Z]{3,}\b', query) if w.lower() not in ["for", "with", "and", "the", "under", "over", "best", "top", "buy"]]
        if words:
            main_term = words[-1]
            filters.append({
                "id": "filter_category",
                "category": "Category",
                "isPrice": False,
                "isColor": False,
                "isMultiSelect": False,
                "options": [
                    {"id": "c1", "label": f"{main_term}s", "value": f"{main_term}s"},
                    {"id": "c2", "label": f"Mini {main_term}", "value": f"Mini {main_term}"},
                    {"id": "c3", "label": f"Pro {main_term}", "value": f"Pro {main_term}"},
                ]
            })

        return filters

    async def apply_filter(self, category: str, selected_values: List[str], min_price: Optional[str] = None, max_price: Optional[str] = None) -> Dict[str, Any]:
        """Click chosen filter or submit min/max price range on Google Shopping page and narrow results live"""
        if category.lower() in ("price", "budget") and (min_price or max_price):
            clean_min = re.sub(r'[^\d]', '', str(min_price or ''))
            clean_max = re.sub(r'[^\d]', '', str(max_price or ''))
            self.applied_filters[category] = f"₹{clean_min or '0'} - ₹{clean_max or 'Any'}"

            if self.page:
                try:
                    logger.info(f"Setting price bounds: Min='{clean_min}', Max='{clean_max}' on Google Shopping...")
                    # Exact Google Shopping locators:
                    # Min: input[jsname="C7zBXb"], input[title="Min"], input[aria-label*="Minimum price"]
                    # Max: input[jsname="TYdZhc"], input[title="Max"], input[aria-label*="Maximum price"]
                    # Go: button[jsname="tlfanc"], button:has-text("Go")
                    
                    min_input = await self.page.query_selector('input[jsname="C7zBXb"], input[title="Min"], input[aria-label*="Minimum price"]')
                    max_input = await self.page.query_selector('input[jsname="TYdZhc"], input[title="Max"], input[aria-label*="Maximum price"]')
                    go_button = await self.page.query_selector('button[jsname="tlfanc"], button:has-text("Go")')

                    if min_input and clean_min:
                        await min_input.focus()
                        await min_input.click()
                        await self.page.keyboard.press("Control+A")
                        await self.page.keyboard.press("Backspace")
                        await min_input.type(clean_min, delay=50)
                        await min_input.evaluate('(el) => el.dispatchEvent(new Event("input", { bubbles: true }))')

                    if max_input and clean_max:
                        await max_input.focus()
                        await max_input.click()
                        await self.page.keyboard.press("Control+A")
                        await self.page.keyboard.press("Backspace")
                        await max_input.type(clean_max, delay=50)
                        await max_input.evaluate('(el) => el.dispatchEvent(new Event("input", { bubbles: true }))')

                    await asyncio.sleep(0.5)

                    if go_button and await go_button.is_visible():
                        logger.info("Clicking Google Shopping Price 'Go' button...")
                        await go_button.click()
                    else:
                        logger.info("Go button disabled/hidden — submitting price with Enter key press...")
                        target_input = max_input or min_input
                        if target_input:
                            await target_input.press("Enter")

                    await asyncio.sleep(3.0)
                    logger.info(f"Price range submitted successfully! Google Shopping URL: {self.page.url}")
                except Exception as e:
                    logger.warning(f"Could not fill min/max price inputs on Google Shopping: {e}")
        else:
            self.applied_filters[category] = selected_values if len(selected_values) > 1 else selected_values[0]

            if self.page:
                try:
                    for val in selected_values:
                        selector = f'label:has-text("{val}"), a:has-text("{val}"), span:has-text("{val}"), [aria-label*="{val}"]'
                        element = await self.page.query_selector(selector)
                        if element and await element.is_visible():
                            logger.info(f"Clicking filter element '{val}' on Google Shopping page...")
                            await element.click()
                            await asyncio.sleep(2.5)
                            logger.info(f"Filter clicked! Google Shopping updated URL: {self.page.url}")
                            break
                        else:
                            refined_query = f"{val} {self.query}"
                            encoded_ref = urllib.parse.quote(refined_query)
                            ref_url = f"https://www.google.com/search?q={encoded_ref}&udm=28"
                            logger.info(f"Navigating query refinement on Google Shopping: {ref_url}")
                            await self.page.goto(ref_url, wait_until="domcontentloaded", timeout=25000)
                            await asyncio.sleep(2)
                            break
                except Exception as e:
                    logger.warning(f"Could not click filter '{selected_values}' on Google Shopping: {e}")

        updated_filters = await self.extract_live_google_filters()
        self.extracted_filters = updated_filters

        return {
            "session_id": self.session_id,
            "query": self.query,
            "applied_filters": self.applied_filters,
            "filters": updated_filters,
            "status": "active"
        }

    async def close(self):
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
