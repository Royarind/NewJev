"""
Fallback DOM searcher for when vision fails
Simple JavaScript-based element finding
"""

async def find_element_by_text_js(page, target_text: str, element_types: list = None):
    """
    Find element by text using JavaScript
    Fallback when vision/OCR fails
    
    Args:
        page: Playwright page
        target_text: Text to search for (e.g., "38S", "Large")
        element_types: Optional list of element types to search (['button', 'a', 'div'])
    
    Returns:
        Element coordinates or None
    """
    if element_types is None:
        element_types = ['button', 'a', 'span', 'div', 'label']
    
    selectors = ', '.join(element_types)
    
    js_code = f"""
    (targetText) => {{
        const elements = document.querySelectorAll('{selectors}');
        const normalized = targetText.toLowerCase().replace(/[\\s-]/g, '');
        
        for (const elem of elements) {{
            const text = elem.textContent?.toLowerCase().replace(/[\\s-]/g, '') || '';
            const ariaLabel = elem.getAttribute('aria-label')?.toLowerCase().replace(/[\\s-]/g, '') || '';
            
            // Exact normalized match
            if (text === normalized || ariaLabel === normalized) {{
                const rect = elem.getBoundingClientRect();
                return {{
                    x: rect.left + rect.width / 2,
                    y: rect.top + rect.height / 2,
                    text: elem.textContent?.trim(),
                    found: true
                }};
            }}
            
            // Partial match (contains)
            if (text.includes(normalized) || ariaLabel.includes(normalized)) {{
                const rect = elem.getBoundingClientRect();
                return {{
                    x: rect.left + rect.width / 2,
                    y: rect.top + rect.height / 2,
                    text: elem.textContent?.trim(),
                    found: true
                }};
            }}
        }}
        
        return {{found: false}};
    }}
    """
    
    try:
        result = await page.evaluate(js_code, target_text)
        
        if result.get('found'):
            return {
                'coordinates': {'x': result['x'], 'y': result['y']},
                'text': result['text'],
                'center': {'x': result['x'], 'y': result['y']}
            }
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"JS fallback search failed: {e}")
    
    return None
