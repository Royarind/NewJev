#!/usr/bin/env python3
"""
Smart Page Section Detector
Uses DOM analysis to find actual sections, not fixed percentages
"""

import asyncio
from pathlib import Path
from playwright.async_api import Page
import cv2
import numpy as np
from typing import Dict, List
import logging

logger = logging.getLogger(__name__)


async def detect_real_page_sections(page: Page) -> List[Dict]:
    """
    Detect actual page sections using DOM analysis
    Returns list of sections with real bounding boxes
    """
    
    js_code = """
    () => {
        const sections = [];
        
        // Find main product image container
        const imageSelectors = [
            '#landingImage', '#imgTagWrapperId', '[id*="imageBlock"]',
            '[class*="imageThumbnail"]', '[data-a-image-name]',
            'img[data-old-hires]'
        ];
        
        for (const sel of imageSelectors) {
            const elem = document.querySelector(sel);
            if (elem) {
                const container = elem.closest('div[id], div[class]') || elem.parentElement;
                const rect = container.getBoundingClientRect();
                if (rect.width > 200 && rect.height > 200) {
                    sections.push({
                        name: 'Product Image',
                        bbox: [rect.left, rect.top, rect.right, rect.bottom],
                        color: [0, 255, 0],
                        confidence: 'high'
                    });
                    break;
                }
            }
        }
        
        // Find color/size variant section
        const variantSelectors = [
            '#variation_color_name', '[id*="color"]', '[id*="variation"]',
            '[class*="swatches"]', '[class*="variation"]'
        ];
        
        for (const sel of variantSelectors) {
            const elem = document.querySelector(sel);
            if (elem) {
                // Get parent container
                let container = elem;
                for (let i = 0; i < 5; i++) {
                    const parent = container.parentElement;
                    if (!parent) break;
                    const parentRect = parent.getBoundingClientRect();
                    if (parentRect.width > 300 && parentRect.height > 100) {
                        container = parent;
                    } else {
                        break;
                    }
                }
                
                const rect = container.getBoundingClientRect();
                if (rect.width > 100 && rect.height > 50) {
                    sections.push({
                        name: 'Product Variants',
                        bbox: [rect.left, rect.top, rect.right, rect.bottom],
                        color: [0, 0, 255],
                        confidence: 'high'
                    });
                    break;
                }
            }
        }
        
        // Find price/buy box
        const priceSelectors = [
            '#corePrice_desktop', '[id*="price"]', '#buy-now-button',
            '#add-to-cart-button', '[class*="buybox"]'
        ];
        
        for (const sel of priceSelectors) {
            const elem = document.querySelector(sel);
            if (elem) {
                let container = elem;
                for (let i = 0; i < 5; i++) {
                    const parent = container.parentElement;
                    if (!parent) break;
                    const parentRect = parent.getBoundingClientRect();
                    if (parentRect.width > 200) {
                        container = parent;
                    } else {
                        break;
                    }
                }
                
                const rect = container.getBoundingClientRect();
                sections.push({
                    name: 'Buy Box / Price',
                    bbox: [rect.left, rect.top, rect.right, rect.bottom],
                    color: [255, 255, 0],
                    confidence: 'high'
                });
                break;
            }
        }
        
        // Find header
        const header = document.querySelector('header, [id*="navbar"], [id*="nav-main"]');
        if (header) {
            const rect = header.getBoundingClientRect();
            sections.push({
                name: 'Header',
                bbox: [rect.left, rect.top, rect.right, rect.bottom],
                color: [255, 0, 0],
                confidence: 'high'
            });
        }
        
        // Product title/details
        const titleSelectors = ['#productTitle', '[id*="title"]', 'h1'];
        for (const sel of titleSelectors) {
            const elem = document.querySelector(sel);
            if (elem && elem.textContent.trim().length > 10) {
                const rect = elem.getBoundingClientRect();
                sections.push({
                    name: 'Product Title',
                    bbox: [rect.left, rect.top, rect.right, rect.bottom],
                    color: [255, 165, 0],
                    confidence: 'medium'
                });
                break;
            }
        }
        
        return sections;
    }
    """
    
    sections = await page.evaluate(js_code)
    return sections


async def create_smart_annotated_screenshot(page: Page, output_path: str = "smart_sections_annotated.jpg"):
    """
    Create annotated screenshot using real DOM-detected sections
    """
    logger.info("Taking screenshot and detecting real sections")
    
    # Take screenshot
    screenshot_path = "temp_for_annotation.png"
    await page.screenshot(path=screenshot_path, full_page=False)  # Visible area only
    
    # Detect sections
    sections = await detect_real_page_sections(page)
    
    logger.info(f"Detected {len(sections)} real sections")
    
    # Load image
    img = cv2.imread(screenshot_path)
    if img is None:
        return None
    
    height, width = img.shape[:2]
    annotated = img.copy()
    
    # Draw each section
    for idx, section in enumerate(sections, 1):
        x1, y1, x2, y2 = [int(v) for v in section['bbox']]
        name = section['name']
        color = tuple(section['color'])
        
        # Ensure coordinates are within image bounds
        x1 = max(0, min(x1, width))
        x2 = max(0, min(x2, width))
        y1 = max(0, min(y1, height))
        y2 = max(0, min(y2, height))
        
        if x2 <= x1 or y2 <= y1:
            continue
        
        # Draw rectangle
        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 3)
        
        # Add label
        label = f"{idx}. {name}"
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.7
        thickness = 2
        
        (text_width, text_height), _ = cv2.getTextSize(label, font, font_scale, thickness)
        
        # Label background
        label_y = max(y1 - 10, text_height + 10)
        cv2.rectangle(annotated, 
                     (x1, label_y - text_height - 10),
                     (x1 + text_width + 10, label_y),
                     color, -1)
        
        # Label text
        cv2.putText(annotated, label,
                   (x1 + 5, label_y - 5),
                   font, font_scale, (255, 255, 255), thickness)
    
    # Title
    cv2.putText(annotated, "DOM-BASED SECTION DETECTION",
               (20, 40),
               cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 4)
    cv2.putText(annotated, "DOM-BASED SECTION DETECTION",
               (20, 40),
               cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2)
    
    # Save
    cv2.imwrite(output_path, annotated)
    logger.info(f"Saved annotated screenshot: {output_path}")
    
    # Log summary
    logger.info("DOM-Based Section Detection Results")
    for idx, section in enumerate(sections, 1):
        x1, y1, x2, y2 = section['bbox']
        logger.info("[%d] %s - BBox: (%.0f, %.0f) to (%.0f, %.0f), Size: %.0fx%.0f, Confidence: %s",
                   idx, section['name'], x1, y1, x2, y2, x2-x1, y2-y1, section['confidence'])
    
    return output_path


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
    
    from app.ecommerce.utils.ephemeral_browser import EphemeralBrowserManager
    
    async def test():
        url = "https://www.amazon.com/LEE-Womens-Relaxed-Straight-Meridian/dp/B01CID4ELM"
        
        async with EphemeralBrowserManager(headless=False) as browser:
            page = browser.get_page()
            await browser.navigate(url)
            await asyncio.sleep(3)
            
            result = await create_smart_annotated_screenshot(page)
            logger.info("Annotated screenshot: %s", result)
            logger.info("Browser stays open for 10 seconds")
            await asyncio.sleep(10)
    
    asyncio.run(test())
