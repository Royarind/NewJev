(args) => {
    const { val, containerSelector } = args;
    const normalize = (text) => text ? text.toLowerCase().trim().replace(/[^a-z0-9\s]/g, '') : '';
    const normalizedVal = normalize(val);

    const match = (text) => {
        if (!text) return false;
        const t = normalize(text);

        // Exact match
        if (t === normalizedVal) return true;

        const searchWords = normalizedVal.split(/\s+/).filter(w => w.length > 0);
        const textWords = t.split(/\s+/).filter(w => w.length > 0);

        // Single-word match (e.g. "black" or "tall" or "xxl")
        if (searchWords.length === 1) {
            const word = searchWords[0];
            // Check if it's present as a standalone word in the text words
            const hasWord = textWords.some(textWord => textWord === word);
            if (hasWord) return true;
            // Fallback: check if the normalized text contains the search word as a substring
            // and the search word is not extremely short to prevent random partial matching
            if (word.length >= 3 && t.includes(word)) return true;
        }

        // Multi-word match: check if all words from search value exist in text
        if (searchWords.length >= 2) {
            // First try exact phrase match
            if (t.includes(normalizedVal)) {
                return true;
            }

            // Then try all words present match
            const hasAllWords = searchWords.every(word =>
                textWords.some(textWord => textWord === word || textWord.includes(word) || word.includes(textWord))
            );
            if (hasAllWords) return true;
        }

        return false;
    };

    // Clear existing overlays
    document.querySelectorAll('.automation-overlay').forEach(el => el.remove());

    // Create overlay container
    const overlayContainer = document.createElement('div');
    overlayContainer.id = 'automation-overlays';
    overlayContainer.style.cssText = 'position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; z-index: 999999;';
    document.body.appendChild(overlayContainer);

    let elementIndex = 0;
    const indexedElements = [];

    // Scope search to container if provided
    let root = containerSelector ? document.querySelector(containerSelector) : document;
    if (!root) return { found: false, phase: 'overlay', error: 'Container not found' };

    // Function to create overlay for element
    const createOverlay = (element, index, color = '#ff0000', label = '') => {
        const rect = element.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return null;

        const overlay = document.createElement('div');
        overlay.className = 'automation-overlay';
        overlay.setAttribute('data-element-index', index);
        // FIX: Use position:absolute (relative to body) + scroll offsets so it is immune
        // to CSS containing block bugs when parents have transforms/filters.
        // It also scrolls naturally with the page.
        const scrollX = window.scrollX || window.pageXOffset;
        const scrollY = window.scrollY || window.pageYOffset;
        overlay.style.cssText = `
            position: absolute;
            left: ${rect.left + scrollX}px;
            top: ${rect.top + scrollY}px;
            width: ${rect.width}px;
            height: ${rect.height}px;
            border: 3px solid ${color};
            background: ${color}33;
            pointer-events: none;
            font-family: monospace;
            font-size: 11px;
            font-weight: bold;
            color: ${color};
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 2147483646;
        `;
        overlay.textContent = label || `#${index}`;
        overlayContainer.appendChild(overlay);
        return overlay;
    };

    // Index all clickable elements with overlays
    const clickableSelectors = [
        // Variant swatch / option buttons - PRIORITY (exclude quick-add: they appear in carousels)
        'button[class*="swatch"], button[class*="option"]',
        'button[aria-label*="color" i], button[aria-label*="size" i], button[aria-label*="fit" i]',
        
        // Size/Color/Fit buttons - Dillards, Gap, BR
        'button[name="Size"], button[name="Color"], button[name="Fit"]', 
        'button[class*="size-option"], button[class*="color-option"], button[class*="fit-option"]',
        
        // Radio inputs
        'input[type="radio"][name*="Shade"], input[type="radio"][name*="option"], input[type="radio"][name*="Color"]',
        'input[type="radio"][name*="size"], input[type="radio"][name*="color"], input[type="radio"][name*="fit"]',
        
        // Quantity controls
        'input[type="number"], input[name*="quantity"], input[id*="quantity"], input[class*="quantity"]',
        'select[name*="quantity"], select[id*="quantity"], select[class*="quantity"]',
        'button[class*="quantity"], button[aria-label*="quantity"], button[class*="plus"], button[class*="minus"]',
        
        // Cart/Add buttons
        'button[name="add"], button[class*="add-to-cart"], button[class*="add-to-bag"], .action--add-to-cart', 
        
        // Generic variant/swatch elements
        '.swatch, .color-swatch, .size-swatch, .fit-swatch, .variant-option, .attribute-option, .product-swatch', 
        '.swatch-input, .swatch-label, .variant-selector button',
        
        // Catch-alls
        'button', 'a', 'select', 'input[type="button"]', 'input[type="submit"]',
        '[role="button"]', '[role="radio"]', '[role="checkbox"]', '[role="combobox"]',
        '[onclick]', '.clickable', '.selectable'
    ];

    // Exclusion guard: skip Quick Add buttons — their aria-labels embed the product name
    // + colour (e.g. "Quick Add … Washed Black"), causing false matches when the agent is
    // searching for a colour swatch on the current product.
    //
    // We intentionally do NOT walk ancestor elements here: BR's size swatch row lives inside
    // a "carousel" scroll container, so any ancestor check for "carousel" would wrongly
    // exclude real XXL/Tall/etc. size buttons.  The "Quick Add" aria-label pattern is
    // highly specific and sufficient on its own.
    const isCarouselOrQuickAdd = (el) => {
        // Direct quick-add class
        if (el.matches('[class*="quick-add"]')) return true;
        // aria-label starts with "Quick Add" — specific to BR/Gap product-card buttons
        const aria = (el.getAttribute('aria-label') || '').trim();
        if (/^quick[\s-]?add/i.test(aria)) return true;
        return false;
    };

    // Helper to collect all elements including Shadow DOM
    const collectElements = (root, selectors) => {
        const elements = [];
        const seen = new Set();

        const traverse = (node) => {
            if (!node) return;

            // Check if node matches any selector
            let matches = node.matches && selectors.some(s => node.matches(s));

            // Custom addition: check if it's a div, span, p, or li whose direct text matches the search target
            if (!matches && node.tagName && ['DIV', 'SPAN', 'P', 'LI'].includes(node.tagName.toUpperCase())) {
                const directText = Array.from(node.childNodes)
                    .filter(n => n.nodeType === 3) // Node.TEXT_NODE
                    .map(n => n.textContent.trim().toLowerCase())
                    .join(' ');
                
                if (directText && normalize(directText).includes(normalizedVal)) {
                    matches = true;
                }
            }

            if (matches) {
                if (!seen.has(node)) {
                    elements.push(node);
                    seen.add(node);
                }
            }

            // Traverse children
            const children = node.children || node.childNodes;
            for (const child of children) {
                traverse(child);
            }

            // Traverse Shadow DOM
            if (node.shadowRoot) {
                traverse(node.shadowRoot);
            }
        };

        traverse(root);
        return elements;
    };

    let matchedIndex = null;

    // Use deep traversal instead of simple querySelectorAll
    let allPotentialElements = collectElements(root, clickableSelectors);

    // FALLBACK: If the scoped container returned 0 elements, fallback to the entire document.
    // This makes the element detection extremely robust and website-agnostic!
    if (allPotentialElements.length === 0 && root !== document) {
        root = document;
        allPotentialElements = collectElements(root, clickableSelectors);
    }

    const matches_found = [];

    for (const element of allPotentialElements) {
        // Skip Quick Add / carousel elements — they embed product names that contain colour
        // words and produce false-positive matches (e.g. "Washed Black" in a carousel).
        if (isCarouselOrQuickAdd(element)) continue;

        // Legacy exclusion hook (kept for backwards compatibility)
        if (typeof isInExcludedSection === 'function' && isInExcludedSection(element)) {
            continue;
        }

        // Skip hidden or non-interactive elements
        const style = window.getComputedStyle(element);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;

        const rect = element.getBoundingClientRect();
        if (rect.width < 3 || rect.height < 3) continue;

        const currentIndex = elementIndex++;
        let isMatch = false;
        let score = 1; // Default priority score

        const tag = element.tagName.toUpperCase();
        const textLower = (element.textContent || '').trim().toLowerCase();
        const ariaLabel = (element.getAttribute('aria-label') || '').toLowerCase();

        // 1. Calculate base priority score
        if (['BUTTON', 'SELECT', 'INPUT'].includes(tag)) score = 5;
        else if (['A', 'LABEL'].includes(tag)) score = 4;
        else if (['LI', 'SPAN'].includes(tag)) score = 3;
        else if (tag === 'IMG') score = 2;

        // 2. Check for match
        const textsToMatch = [
            textLower,
            ariaLabel,
            (element.getAttribute('title') || '').toLowerCase(),
            (element.getAttribute('alt') || '').toLowerCase(),
            (element.getAttribute('value') || '').toLowerCase(),
            // Banana Republic / Gap swatch attributes
            (element.getAttribute('data-colorname') || '').toLowerCase(),
            (element.getAttribute('data-color') || '').toLowerCase(),
            (element.getAttribute('data-value') || '').toLowerCase(),
            (element.getAttribute('data-swatch-value') || '').toLowerCase(),
            (element.getAttribute('data-label') || '').toLowerCase(),
            (element.getAttribute('data-option-value') || '').toLowerCase()
        ];

        if (normalizedVal.length < 3) {
            // Short token (M, S, L, 32): Exact match only!
            isMatch = textsToMatch.some(t => normalize(t) === normalizedVal);
        } else {
            // Longer token: use existing match heuristic
            isMatch = textsToMatch.some(t => match(t));
        }

        if (isMatch) {
            matches_found.push({
                index: currentIndex,
                element: element,
                score: score,
                tag: tag,
                text: textLower
            });
        }

        // Always create a visual marker for debugging (optional/existing behavior)
        createOverlay(element, currentIndex, isMatch ? '#00ff00' : '#ffdd00', isMatch ? `✓ ${currentIndex}` : `${currentIndex}`);
    }

    if (matches_found.length > 0) {
        // Sort by score (desc) then by index (asc - preserve first found for ties)
        matches_found.sort((a, b) => b.score - a.score || a.index - b.index);
        const best = matches_found[0];

        return {
            found: true,
            action: ['SELECT', 'INPUT'].includes(best.tag) ? 'select' : 'click',
            elementIndex: best.index,
            phase: 'overlay',
            bestMatch: { tag: best.tag, text: best.text, score: best.score }
        };
    }

    return { found: false, phase: 'overlay' };
};
