"""Bridge module connecting NewJev agent core, planner, and tools to the Agentic Ecommerce System."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

from .main_orchestrator import AgenticAutomationSystem, AutomationRequest
from .profiles import CheckoutProfile
from .google_shopping import GoogleShoppingSession

logger = logging.getLogger("NewJev-Ecommerce")

# Keywords and patterns that identify an ecommerce/shopping intent
ECOMMERCE_PATTERNS = re.compile(
    r"\b("
    r"buy|purchase|order|checkout|add to cart|add to bag|cart|shopping|"
    r"cheapest|cheaper|best price|price comparison|prices? of|"
    r"amazon(?:\.in)?|flipkart|myntra|meesho|kyliecosmetics|allbirds|nike(?:\.com)?|"
    r"walmart|target|ebay|aliexpress|best buy|shopify"
    r")\b",
    re.I,
)

SHOPPING_SEARCH_HINTS = re.compile(
    r"\b(find|search|compare|prices?|cheapest|where (?:can|to) buy|suggest where|which is cheaper)\b",
    re.I,
)

CHECKOUT_ACTION_HINTS = re.compile(
    r"\b(buy|order|purchase|checkout|add to cart|add to bag|place order)\b",
    re.I,
)


def is_ecommerce_goal(goal: str) -> bool:
    """Determine whether a natural-language goal is an ecommerce shopping/checkout task."""
    if not goal:
        return False
    g = goal.strip().lower()

    # Direct match on common ecommerce keywords
    if ECOMMERCE_PATTERNS.search(g):
        # Exclude general news/knowledge queries unless shopping is explicit
        if "news" in g and not any(k in g for k in ("price", "buy", "shop", "cart", "product")):
            return False
        return True

    return False


def load_default_profile() -> CheckoutProfile:
    """Load default checkout profile from data/checkout_config.json or built-in defaults."""
    cfg_path = Path("data/checkout_config.json")
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            prof_data = data.get("profile") or data
            return CheckoutProfile.from_dict(prof_data)
        except Exception as e:
            logger.warning(f"Failed to load profile from {cfg_path}: {e}")

    # Fallback default test profile (will never charge without review)
    return CheckoutProfile(
        first_name="Customer",
        last_name="User",
        email="customer@example.com",
        phone="9876543210",
        address_line1="123 Main Street",
        city="Mumbai",
        state="Maharashtra",
        postal_code="400001",
        country="IN",
    )


def extract_ecommerce_details(goal: str, llm=None) -> dict[str, Any]:
    """
    Extract product query, target URL, variants, and action intent from user goal.
    """
    url_match = re.search(r"https?://[^\s]+", goal)
    url = url_match.group(0) if url_match else None

    # Variants extraction heuristic
    variants: dict[str, str] = {}
    size_match = re.search(r"\b(?:size|sz)\s*[:=]?\s*([a-zA-Z0-9\.\-]+)\b", goal, re.I)
    if size_match:
        variants["size"] = size_match.group(1).strip()

    color_match = re.search(r"\b(?:in|color|colour)\s+([a-zA-Z\s]{3,15})\b", goal, re.I)
    if color_match and not url:
        col = color_match.group(1).strip()
        if col.lower() not in ("both", "amazon", "flipkart", "stock", "terms", "price", "the", "this"):
            variants["color"] = col

    # Check for storage/ram capacity
    cap_match = re.search(r"\b(\d+\s*(?:gb|tb|ml|oz))\b", goal, re.I)
    if cap_match:
        variants["capacity"] = cap_match.group(1).strip()

    # Determine intent
    is_checkout = bool(CHECKOUT_ACTION_HINTS.search(goal) and (url or "cart" in goal.lower()))

    # Clean query for Google Shopping search if no direct product URL
    clean_query = goal
    for prefix in (
        "can you find out where can i find",
        "can you give me the prices of",
        "find the cheapest",
        "where can i buy",
        "search for",
        "give me the price of",
    ):
        if clean_query.lower().startswith(prefix):
            clean_query = clean_query[len(prefix):].strip()
            break

    clean_query = re.sub(r"\b(?:in both|and suggest where.*|and what would be the price.*)\b", "", clean_query, flags=re.I).strip()

    return {
        "url": url,
        "query": clean_query or goal,
        "variants": variants,
        "quantity": 1,
        "is_checkout": is_checkout,
    }


async def run_google_shopping_search(
    query: str,
    emit_callback: Callable[[str, dict[str, Any]], None] | None = None,
    headless: bool = False,
) -> dict[str, Any]:
    """
    Perform live Google Shopping search and refinement analysis.
    """
    def emit(event: str, data: dict[str, Any]) -> None:
        if emit_callback:
            try:
                emit_callback(event, data)
            except Exception:
                pass

    emit("status", {"message": f"Opening Google Shopping for '{query}'..."})
    emit("step", {"step": 1, "task": f"Google Shopping search: {query}", "mode": "ecommerce"})

    session = GoogleShoppingSession(session_id="newjev_shop", headless=headless)
    try:
        search_res = await session.search(query)
        filters = search_res.get("filters", [])
        page_url = session.page.url if session.page else ""

        # Extract product cards or visible results from the page
        product_results = []
        if session.page:
            try:
                raw_products = await session.page.evaluate("""() => {
                    const items = [];
                    // Google Shopping cards locators
                    const cards = Array.from(document.querySelectorAll('div.sh-dgr__content, div[data-docid], div.xcR77, div.KZmu8e, tr.sh-osd__offer-row'));
                    cards.slice(0, 8).forEach(c => {
                        const titleEl = c.querySelector('h3, a.sh-np__click-target, .tAxLfb, .Xjkr3b');
                        const priceEl = c.querySelector('span.a8Pemb, span[aria-hidden="true"], .kHLEWe, .hn9baf');
                        const merchantEl = c.querySelector('.aULzUe, .IuHnof, .rhsg4, .h1W9cf');
                        const linkEl = c.querySelector('a[href^="/url"], a[href^="http"]');
                        
                        const title = titleEl ? titleEl.innerText.trim() : '';
                        const price = priceEl ? priceEl.innerText.trim() : '';
                        const merchant = merchantEl ? merchantEl.innerText.trim() : '';
                        const link = linkEl ? linkEl.href : '';

                        if (title && (price || merchant)) {
                            items.push({ title, price, merchant, link });
                        }
                    });
                    return items;
                }""")
                product_results = raw_products
            except Exception as e:
                logger.warning(f"Could not extract card details from shopping page: {e}")

        # Build clean markdown comparison report
        lines = [f"### 🛍️ Google Shopping Results: **{query}**\n"]
        if product_results:
            lines.append("| Product | Price | Seller / Merchant | Link |")
            lines.append("|---|---|---|---|")
            for p in product_results:
                t = p.get("title", "")[:60]
                pr = p.get("price", "N/A")
                m = p.get("merchant", "Online Store")
                l = p.get("link", page_url)
                lines.append(f"| {t} | **{pr}** | {m} | [View Offer]({l}) |")
            lines.append("")
        else:
            lines.append(f"Navigated to Google Shopping results: [{query}]({page_url})\n")

        if filters:
            lines.append("**Key Available Refinements:**")
            for f in filters[:5]:
                cat = f.get("category", "")
                opts = [o.get("label", "") for o in f.get("options", [])[:4]]
                if cat and opts:
                    lines.append(f"- **{cat}**: {', '.join(opts)}")

        report_markdown = "\n".join(lines)
        emit("report", {"text": report_markdown, "step": 2})

        return {
            "success": True,
            "status": "done",
            "final_answer": report_markdown,
            "query": query,
            "products": product_results,
            "filters": filters,
            "url": page_url,
        }
    except Exception as exc:
        logger.error(f"Google shopping search error: {exc}", exc_info=True)
        return {
            "success": False,
            "status": "error",
            "error": str(exc),
            "final_answer": f"Google Shopping search failed: {exc}",
        }
    finally:
        try:
            await session.close()
        except Exception:
            pass


async def run_checkout_automation(
    url: str,
    variants: dict[str, str] | None = None,
    quantity: int = 1,
    profile: CheckoutProfile | None = None,
    emit_callback: Callable[[str, dict[str, Any]], None] | None = None,
    headless: bool = False,
) -> dict[str, Any]:
    """
    Execute full product variant selection, add-to-cart, and checkout automation.
    """
    def emit(event: str, data: dict[str, Any]) -> None:
        if emit_callback:
            try:
                emit_callback(event, data)
            except Exception:
                pass

    profile = profile or load_default_profile()
    variants = variants or {}

    emit("status", {"message": f"Starting checkout automation on {url}..."})
    emit("step", {"step": 1, "task": f"Opening {url} and clearing distractions", "mode": "ecommerce"})

    from .utils.ephemeral_browser import EphemeralBrowserManager
    from .utils.distraction_manager import DistractionManager

    req = AutomationRequest(
        url=url,
        variants=variants,
        quantity=quantity,
        profile=profile,
    )

    async with EphemeralBrowserManager(headless=headless) as browser_mgr:
        page = browser_mgr.page
        context = browser_mgr.context

        system = AgenticAutomationSystem()
        await system.initialize(page)

        # Clear distractions
        try:
            emit("status", {"message": "Clearing cookie banners and popups..."})
            dm = DistractionManager(page)
            await dm.clear_distractions_aggressive(max_attempts=3)
        except Exception as e:
            logger.warning(f"Initial distraction clearing: {e}")

        emit("status", {"message": "Executing adaptive variant selection and checkout..."})
        res = await system.automate(req)

        # Build execution summary
        summary_lines = [
            f"### 🛒 Ecommerce Automation Report for {url}\n",
            f"- **Overall Status:** {'✅ SUCCESS' if res.get('success') else '⚠️ COMPLETED WITH WARNINGS'}",
            f"- **Tasks Successful:** {res.get('successful_tasks', 0)} / {res.get('total_tasks', 0)}\n",
            "**Step Details:**",
        ]
        for r in res.get("results", []):
            icon = "✅" if r.get("success") else "❌"
            summary_lines.append(f"- {icon} **{r.get('task')}** ({r.get('method') or 'automated'})")
            if r.get("error"):
                summary_lines.append(f"  *Note: {r.get('error')}*")

        report_markdown = "\n".join(summary_lines)
        emit("report", {"text": report_markdown, "step": len(res.get("results", []))})

        return {
            "success": res.get("success", False),
            "status": "done" if res.get("success") else "unverified",
            "final_answer": report_markdown,
            "results": res.get("results", []),
            "url": url,
        }


def run_ecommerce(
    goal: str,
    emit_callback: Callable[[str, dict[str, Any]], None] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Synchronous entrypoint called by NewJev server / ToolLoop / AgentLoop.
    Dispatches to asyncio event loop.
    """
    details = extract_ecommerce_details(goal)
    headless_env = os.environ.get("HEADLESS", "false").lower() in ("true", "1")

    async def _async_run():
        if details.get("is_checkout") and details.get("url"):
            return await run_checkout_automation(
                url=details["url"],
                variants=details.get("variants"),
                quantity=details.get("quantity", 1),
                emit_callback=emit_callback,
                headless=headless_env,
            )
        else:
            return await run_google_shopping_search(
                query=details.get("query", goal),
                emit_callback=emit_callback,
                headless=headless_env,
            )

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(lambda: asyncio.run(_async_run())).result()
        return loop.run_until_complete(_async_run())
    except Exception as exc:
        logger.error(f"Ecommerce execution failed: {exc}", exc_info=True)
        return {
            "success": False,
            "status": "error",
            "final_answer": f"Ecommerce execution error: {exc}",
        }
