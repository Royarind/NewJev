/**
 * Semantic Blueprint Generator
 * Extracts interactive elements from the product area to create a text-based map for LLMs.
 */
(function () {
    const INTERACTIVE_SELECTORS = [
        'button', 'a', 'input', 'select',
        '[role="button"]', '[role="checkbox"]', '[role="radio"]', '[role="combobox"]',
        '.swatch', '.color-option', '.size-option', '.variant-option',
        '.color-swatch', '.size-swatch', '.swatch-input', '.swatch-label',
        '.attribute-option', '.product-swatch', '.variant-selector button'
    ].join(', ');
    const IGNORE_STRINGS = ['cart', 'search', 'menu', 'account', 'login', 'policy', 'terms'];

    function getElementText(el) {
        let text = (el.textContent || el.value || el.title || el.getAttribute('aria-label') || '').trim();

        // If it's an input/button, check for associated label
        if (!text && el.id) {
            const label = document.querySelector(`label[for="${el.id}"]`);
            if (label) text = label.textContent.trim();
        }

        // Traverse up to 4 levels to find a title or area-label (common for swatches)
        let current = el;
        let depth = 0;
        while (!text && current && depth < 4) {
            text = (current.getAttribute('title') || current.getAttribute('aria-label') || '').trim();
            current = current.parentElement;
            depth++;
        }

        // Look for color hex or name in data attributes
        if (!text) {
            for (const attr of el.attributes) {
                if (attr.name.includes('color') || attr.name.includes('value')) {
                    if (attr.value.length < 50) {
                        text = attr.value;
                        break;
                    }
                }
            }
        }

        if (!text && el.className && typeof el.className === 'string' && el.className.toLowerCase().includes('color')) {
            text = `Color: ${el.className.split(' ').pop()}`;
        }

        return text.substring(0, 100);
    }

    function isVisible(el) {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 3 && rect.height > 3 && style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
    }

    function findProductArea() {
        // Try to find the main container using common patterns
        const containers = document.querySelectorAll('main, #product-detail, .product-item, .pdp-container, #ppd, .pdp-main-section');
        for (const c of containers) if (isVisible(c)) return c;
        return document.body;
    }

    function generateBlueprint() {
        const root = findProductArea();
        const elements = root.querySelectorAll(INTERACTIVE_SELECTORS);
        const map = [];
        let idCounter = 0;

        // Clean up previous IDs
        document.querySelectorAll('[data-ai-id]').forEach(el => el.removeAttribute('data-ai-id'));

        elements.forEach(el => {
            if (!isVisible(el)) return;

            const text = getElementText(el);
            const className = el.className.toString();

            // Basic filtering to avoid noise
            const lowText = text.toLowerCase();
            if (IGNORE_STRINGS.some(s => lowText.includes(s)) && !lowText.includes('add to')) return;

            // Heuristic to include swatches even if they have no text
            const isSwatch = className.toLowerCase().includes('swatch') || className.toLowerCase().includes('color');
            if (!text && !isSwatch && !['INPUT', 'SELECT'].includes(el.tagName)) return;

            const aiId = `node-${idCounter++}`;
            el.setAttribute('data-ai-id', aiId);

            map.push({
                id: aiId,
                tag: el.tagName.toLowerCase(),
                role: el.getAttribute('role') || el.type || '',
                text: text || `[${el.tagName.toLowerCase()}]`,
                classes: className.substring(0, 50),
                selected: el.classList.contains('active') || el.classList.contains('selected') || el.getAttribute('aria-selected') === 'true' || el.checked === true
            });
        });

        return {
            url: window.location.href,
            title: document.title,
            elements: map
        };
    }

    return generateBlueprint();
})();
