"""
OpenRouter Multimodal Vision-AI Refinement Provider for CARTMIND
Uses OpenRouter free VLM models (e.g. nvidia/nemotron-nano-12b-v2-vl:free, google/gemma-3-12b-it:free, openrouter/free)
to analyze Google Shopping sidebar screenshots, separate criteria & options, locate "See more", and extract coordinates.
"""

import os
import json
import logging
import httpx
from typing import Dict, Any, List, Optional
from app.ecommerce.vision.base_vision import BaseVisionProvider

logger = logging.getLogger("CARTMIND-OPENROUTER-VISION")

PRIMARY_VLM_MODEL = os.environ.get("OPENROUTER_VLM_MODEL", "nvidia/nemotron-nano-12b-v2-vl:free")
FALLBACK_VLM_MODEL = os.environ.get("OPENROUTER_FALLBACK_MODEL", "openrouter/free")


class OpenRouterVisionProvider(BaseVisionProvider):
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.endpoint = "https://openrouter.ai/api/v1/chat/completions"

    async def extract_filters_from_screenshot(
        self, screenshot_base64: str, page_text: str = ""
    ) -> List[Dict[str, Any]]:
        if not self.api_key:
            logger.warning("OPENROUTER_API_KEY not configured. Falling back to local OCR parsing.")
            return []

        prompt_text = """
You are analyzing a screenshot of a Google Shopping search result page sidebar ("Refine results").
Your task is to identify all distinct refinement filter categories and their exact option choices.

CRITICAL INSTRUCTIONS:
1. Identify distinct category headings ONLY (e.g. "Size", "Category", "Cooking Method", "Price", "Brand", "Features", "Shops").
2. DO NOT mix or jumble options from different headings together.
3. Skip junk text like "Sort by", "Relevance", "Product rating", "Nearby", "On sale".
4. For the "Price" category, include price ranges or mark isPrice: true.
5. If a "See more" or "More" button is visible under a category, note hasSeeMore: true.

Respond strictly in valid JSON format:
{
  "filters": [
    {
      "category": "Size",
      "isPrice": false,
      "hasSeeMore": false,
      "options": ["2-slice", "4-slice", "6-slice"]
    },
    {
      "category": "Category",
      "isPrice": false,
      "hasSeeMore": false,
      "options": ["Toaster Ovens", "Sandwich Makers", "Microwaves"]
    },
    {
      "category": "Price",
      "isPrice": true,
      "hasSeeMore": false,
      "options": ["Under ₹10,000", "Over ₹10,000"]
    }
  ]
}
"""

        payload = {
            "model": PRIMARY_VLM_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{screenshot_base64}"
                            }
                        }
                    ]
                }
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://cartmind.ai",
            "X-Title": "CARTMIND Shopping Assistant",
            "Content-Type": "application/json"
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                logger.info(f"Calling OpenRouter Vision model: {PRIMARY_VLM_MODEL}...")
                resp = await client.post(self.endpoint, json=payload, headers=headers)
                
                if resp.status_code != 200:
                    logger.warning(f"OpenRouter VLM failed ({resp.status_code}), retrying with fallback: {FALLBACK_VLM_MODEL}")
                    payload["model"] = FALLBACK_VLM_MODEL
                    resp = await client.post(self.endpoint, json=payload, headers=headers)

                if resp.status_code == 200:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    parsed = json.loads(content)
                    
                    filters = []
                    raw_groups = parsed.get("filters", [])
                    for idx, group in enumerate(raw_groups):
                        cat = group.get("category", "")
                        opts = group.get("options", [])
                        if cat and opts:
                            filters.append({
                                "id": f"filter_vlm_{idx}",
                                "category": cat,
                                "isPrice": group.get("isPrice", "price" in cat.lower()),
                                "isColor": "color" in cat.lower() or "colour" in cat.lower(),
                                "isMultiSelect": any(k in cat.lower() for k in ["brand", "feature", "shop", "installation"]),
                                "hasSeeMore": group.get("hasSeeMore", False),
                                "options": [
                                    {"id": f"opt_{idx}_{o_idx}", "label": opt, "value": opt}
                                    for o_idx, opt in enumerate(opts)
                                ]
                            })
                    logger.info(f"OpenRouter Vision successfully extracted {len(filters)} clean filter categories!")
                    return filters

        except Exception as e:
            logger.error(f"Error calling OpenRouter Vision API: {e}", exc_info=True)

        return []

    async def locate_element_coordinates(
        self, screenshot_base64: str, target_label: str, section_category: str
    ) -> Optional[Dict[str, float]]:
        """
        Asks OpenRouter VLM for exact visual coordinate ratio (0.0 to 1.0) of target label on page
        """
        if not self.api_key:
            return None

        prompt_text = f"""
Find the target text "{target_label}" inside the "{section_category}" section on this Google Shopping screenshot.
Return the normalized relative center coordinates (x_ratio, y_ratio) from 0.0 to 1.0.

Respond strictly in JSON:
{{
  "target": "{target_label}",
  "found": true,
  "x_ratio": 0.15,
  "y_ratio": 0.42
}}
"""

        payload = {
            "model": PRIMARY_VLM_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_base64}"}}
                    ]
                }
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(self.endpoint, json=payload, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    res = json.loads(data["choices"][0]["message"]["content"])
                    if res.get("found"):
                        return {"x_ratio": res["x_ratio"], "y_ratio": res["y_ratio"]}
        except Exception as e:
            logger.warning(f"Coordinate detection via OpenRouter Vision failed: {e}")

        return None
