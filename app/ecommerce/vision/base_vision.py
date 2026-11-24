"""
Pluggable Vision Refinement Provider Interface for CARTMIND
Allows switching seamlessly between OpenRouter VLM, Gemini Vision, Local OCR, or Custom Providers.
"""

import abc
from typing import Dict, Any, List, Optional


class BaseVisionProvider(abc.ABC):
    @abc.abstractmethod
    async def extract_filters_from_screenshot(
        self, screenshot_base64: str, page_text: str = ""
    ) -> List[Dict[str, Any]]:
        """
        Takes a base64 encoded screenshot of the Google Shopping page sidebar
        and returns structured filter groups with categories, options, and coordinate bounding info.
        """
        pass

    @abc.abstractmethod
    async def locate_element_coordinates(
        self, screenshot_base64: str, target_label: str, section_category: str
    ) -> Optional[Dict[str, float]]:
        """
        Locates the exact center [x, y] coordinates for clicking a filter option,
        'See more' button, or price input boxes.
        """
        pass
