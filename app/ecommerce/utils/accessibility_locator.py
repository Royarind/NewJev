#!/usr/bin/env python3
"""
Playwright Accessibility Tree Locator
Locates product variants using accessibility parameters (ARIA roles, labels, and relationships)
Highly site-agnostic because it mirrors how screen readers perceive the page.
"""

import logging
from typing import List, Dict, Any, Optional
from playwright.async_api import Page

logger = logging.getLogger(__name__)


class AccessibilityLocator:
    """
    Finds and maps variant options based on web accessibility standards
    Bypasses dynamic or obfuscated DOM class names entirely.
    """

    def __init__(self, page: Page):
        self.page = page

    async def locate_variants(self, variant_type: str) -> List[Dict[str, Any]]:
        """
        Locates variant controls (swatches, buttons, dropdowns) for a given variant type.
        
        Args:
            variant_type: The type of variant to locate (e.g., 'color', 'size', 'fit')
            
        Returns:
            List of dictionaries containing candidate details:
            {
                'text': str (label of the option),
                'role': str (e.g. 'radio', 'button'),
                'selected': bool,
                'x': float,
                'y': float,
                'width': float,
                'height': float
            }
        """
        logger.info(f"♿ Locating variants for '{variant_type}' using Accessibility Tree strategy")
        
        # We run a comprehensive script inside the browser context to perform tree traversal
        # and coordinate resolution relative to the viewport.
        js_code = """
        ({variantType}) => {
            const normalize = (t) => t ? t.toLowerCase().trim().replace(/[^a-z0-9]/g, '') : '';
            const targetTypeNorm = normalize(variantType);
            
            // 1. Identify keywords for the variant type
            const keywords = {
                'color': ['color', 'colour', 'shade', 'pattern', 'finish'],
                'size': ['size', 'waist', 'length', 'chest', 'dimension', 'fit'],
                'fit': ['fit'],
                'material': ['material', 'fabric'],
                'style': ['style']
            }[targetTypeNorm] || [targetTypeNorm];
            
            // 2. Find the variant section or label element
            // We search for elements whose text matches the variant type label
            const allElements = document.querySelectorAll('div, section, label, span, h1, h2, h3, h4, h5, h6');
            let bestLabelElement = null;
            let bestLabelText = '';
            
            for (const el of allElements) {
                // Focus on leaf-ish text nodes
                const text = Array.from(el.childNodes)
                    .filter(n => n.nodeType === Node.TEXT_NODE)
                    .map(n => n.textContent.trim())
                    .join(' ')
                    .trim();
                
                if (!text || text.length > 50) continue;
                
                const textNorm = text.toLowerCase();
                const matches = keywords.some(kw => {
                    const re = new RegExp(`\\\\b${kw}\\\\b`, 'i');
                    return re.test(textNorm);
                });
                
                if (matches) {
                    // Avoid helper labels like "size guide", "color details", etc.
                    if (textNorm.includes('guide') || textNorm.includes('chart') || textNorm.includes('detail') || textNorm.includes('info')) {
                        continue;
                    }
                    bestLabelElement = el;
                    bestLabelText = text;
                    break;
                }
            }
            
            if (!bestLabelElement) {
                // Fallback: search broad body inner text to check if it exists at all
                const bodyText = document.body.innerText.toLowerCase();
                const exists = keywords.some(kw => bodyText.includes(kw));
                if (!exists) {
                    return { success: false, error: 'No matching variant label found on page' };
                }
            }
            
            // 3. Find the container bounding this variant section
            // We walk up from the label element to find a logical grouping container
            let container = bestLabelElement ? bestLabelElement.parentElement : null;
            if (container) {
                for (let i = 0; i < 5; i++) {
                    if (!container.parentElement || container.parentElement.tagName === 'BODY') break;
                    
                    // Stop if container spans the entire screen width (too broad)
                    const rect = container.getBoundingClientRect();
                    if (rect.width > window.innerWidth * 0.9) break;
                    
                    // Stop if container seems to contain another variant group (boundaries crossed)
                    const otherKeywords = ['color', 'colour', 'size', 'fit'].filter(k => !keywords.includes(k));
                    const containerText = container.textContent.toLowerCase();
                    const hasOther = otherKeywords.some(kw => {
                        const re = new RegExp(`\\\\b${kw}\\\\b`, 'i');
                        return re.test(containerText);
                    });
                    if (hasOther) break;
                    
                    container = container.parentElement;
                }
            }
            
            // If no specific container found, scope search to the full visible viewport area
            const searchScope = container || document.body;
            
            // 4. Find all interactive controls using standard accessibility roles & inputs
            const selectors = [
                'button',
                'a[href]',
                'input[type="radio"]',
                'input[type="checkbox"]',
                '[role="radio"]',
                '[role="checkbox"]',
                '[role="button"]',
                '[role="option"]',
                'li[role="presentation"] button',
                'div[role="option"]'
            ];
            
            const candidates = [];
            const seen = new Set();
            
            for (const sel of selectors) {
                searchScope.querySelectorAll(sel).forEach(el => {
                    if (seen.has(el)) return;
                    seen.add(el);
                    
                    // Check visibility and size
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 5 || rect.height < 5) return;
                    
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return;
                    
                    // Extract accessible label/name
                    const img = el.tagName === 'IMG' ? el : el.querySelector('img');
                    let name = [
                        el.getAttribute('aria-label') || '',
                        el.getAttribute('title') || '',
                        el.getAttribute('data-value') || '',
                        el.getAttribute('data-color') || '',
                        el.getAttribute('data-colorname') || '',
                        el.getAttribute('alt') || '',
                        img ? (img.getAttribute('aria-label') || img.getAttribute('title') || img.getAttribute('data-color') || img.getAttribute('alt') || '') : '',
                        (el.textContent || '').trim()
                    ].find(t => t.trim().length > 0 && t.trim().toLowerCase() !== 'image') || '';
                    
                    if (!name) {
                        // Special support for Flipkart-style variant anchors containing slugs in URL
                        const anchor = el.tagName === 'A' ? el : el.closest('a');
                        if (anchor && anchor.href) {
                            try {
                                const match = anchor.href.match(/\\/([^\\/]+)\\/p\\//i);
                                const slug = match ? match[1] : '';
                                if (slug) {
                                    name = slug.split('-').filter(p => p.length > 1 && !/^\\d+$/.test(p)).join(' ');
                                }
                            } catch (err) {}
                        }
                    }
                    
                    if (!name) return; // Skip if no label found
                    if (name.length > 60) return; // Skip overly long text (descriptions, not options)
                    
                    // Exclude general cart actions
                    const lowerName = name.toLowerCase();
                    const skipList = ['add to cart', 'add to bag', 'buy now', 'checkout', 'size guide', 'size chart', 'shipping', 'returns', 'details'];
                    if (skipList.some(term => lowerName.includes(term))) return;
                    
                    // Check active/selected state using accessibility parameters and classes
                    const isSelected = 
                        el.getAttribute('aria-selected') === 'true' ||
                        el.getAttribute('aria-checked') === 'true' ||
                        el.getAttribute('aria-pressed') === 'true' ||
                        el.className.includes('selected') ||
                        el.className.includes('active') ||
                        (el.tagName === 'INPUT' && el.checked) ||
                        // Check parent/ancestor classes for selected state
                        (el.parentElement && (el.parentElement.className.includes('selected') || el.parentElement.className.includes('active')));
                    
                    candidates.push({
                        text: name.trim(),
                        role: el.getAttribute('role') || el.tagName.toLowerCase(),
                        selected: !!isSelected,
                        bbox: {
                            x: Math.round(rect.left),
                            y: Math.round(rect.top),
                            width: Math.round(rect.width),
                            height: Math.round(rect.height),
                            center_x: Math.round(rect.left + rect.width / 2),
                            center_y: Math.round(rect.top + rect.height / 2)
                        }
                    });
                });
            }
            
            // Sort top-to-bottom, left-to-right
            candidates.sort((a, b) => {
                if (Math.abs(a.bbox.y - b.bbox.y) > 15) return a.bbox.y - b.bbox.y;
                return a.bbox.x - b.bbox.x;
            });
            
            return {
                success: true,
                labelFound: !!bestLabelElement,
                labelText: bestLabelText,
                candidatesCount: candidates.length,
                candidates: candidates
            };
        }
        """
        
        try:
            result = await self.page.evaluate(js_code, {'variantType': variant_type})
            
            if not result or not result.get('success'):
                logger.warning(f"Accessibility locator returned no success: {result.get('error', 'unknown error')}")
                return []
                
            candidates = result.get('candidates', [])
            logger.info(f"♿ Accessibility tree found {len(candidates)} candidates for {variant_type}:")
            for idx, c in enumerate(candidates[:10]):
                status = " [SELECTED]" if c['selected'] else ""
                logger.info(f"  #{idx+1:02d}: '{c['text']}' ({c['role']}){status} at ({c['bbox']['center_x']}, {c['bbox']['center_y']})")
                
            return candidates
        except Exception as e:
            logger.error(f"Error in accessibility tree traversal: {e}", exc_info=True)
            return []
