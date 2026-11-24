(args) => {
    const { variantType } = args;

    const normalizeStr = (text) => text ? text.toLowerCase().trim().replace(/[^a-z0-9\s]/g, '') : '';
    const normType = normalizeStr(variantType);

    // Clear existing overlays
    document.querySelectorAll('.automation-variant-candidate').forEach(el => el.remove());
    const existingContainer = document.getElementById('automation-variant-candidates-container');
    if (existingContainer) existingContainer.remove();

    // Keywords for each variant type
    const variantKeywords = {
        'color':    ['color', 'colour', 'shade', 'pattern', 'finish', 'manufacturer color'],
        'size':     ['size', 'waist', 'length', 'chest', 'dimension'],
        'fit':      ['fit'],
        'material': ['material', 'fabric'],
        'style':    ['style'],
    };
    const myKeywords = variantKeywords[normType] || [normType];

    // For color searches, anything that looks like a clothing size is NOT a color option
    const CLOTHING_SIZE_RE = /^(xs|s|m|l|xl|xxl|2xl|3xl|4xl|\d{1,2}(\.\d)?w?|one size)$/i;

    // Text that strongly indicates a non-swatch link or control we should skip
    const EXCLUSION_PHRASES = [
        'details', 'size guide', 'size chart', 'fit guide', 'returns',
        'add to bag', 'add to cart', 'buy now', 'checkout', 'registry',
        'wish list', 'wishlist', 'share', 'shipping', 'store pickup',
        'find in store', 'part of a collection', 'pair it with', 'offers',
        'perks', 'zoom', 'play', 'next', 'prev', 'previous', 'more colors',
        'select color', 'select size', 'select fit'
    ];

    const isExcludedText = (text) => {
        const t = text.toLowerCase().trim();
        return EXCLUSION_PHRASES.some(p => t === p || t.startsWith(p));
    };

    // Find variant label elements (e.g., "Color: Polo Black")
    const findVariantLabels = () => {
        const labels = [];
        const allEl = document.querySelectorAll('*');
        for (const el of allEl) {
            // Only look at leaf-ish text nodes with reasonable content
            const directText = Array.from(el.childNodes)
                .filter(n => n.nodeType === Node.TEXT_NODE)
                .map(n => n.textContent.trim())
                .join(' ')
                .toLowerCase();
            if (!directText) continue;
            if (directText.length > 80) continue;

            // Must contain one of our keywords, optionally followed by a colon
            const matches = myKeywords.some(kw => {
                const re = new RegExp(`\\b${kw}\\b`, 'i');
                return re.test(directText);
            });
            // Avoid "size guide", "color guide" etc.
            const isBad = isExcludedText(directText);
            if (matches && !isBad) {
                labels.push(el);
            }
        }
        return labels;
    };

    // Given a label element, walk up to find the variant SECTION container,
    // stopping if we hit a section for a DIFFERENT variant type.
    const findSectionContainer = (labelEl) => {
        const otherKeywords = Object.entries(variantKeywords)
            .filter(([k]) => k !== normType)
            .flatMap(([, v]) => v);

        let container = labelEl.parentElement;
        for (let i = 0; i < 5; i++) {
            if (!container || container.tagName === 'BODY') break;

            // Check if this parent also contains a DIFFERENT variant label
            // If yes, back off one level - we've crossed a boundary
            const containerText = container.textContent.toLowerCase();
            const hasDifferentVariant = otherKeywords.some(kw => {
                const re = new RegExp(`\\b${kw}\\b`, 'i');
                return re.test(containerText);
            });

            // Also stop if the container itself seems to be a page-level wrapper
            const rect = container.getBoundingClientRect();
            const isFullWidth = rect.width > window.innerWidth * 0.9;

            if (hasDifferentVariant || isFullWidth) {
                // Use the PREVIOUS (child) level
                break;
            }
            container = container.parentElement;
        }
        return container || labelEl.parentElement;
    };

    // Helper: extract a human-readable label from an anchor href URL slug.
    // e.g. ".../integriti-polo-lavender-t-shirt/p/.." -> "lavender"
    // e.g. ".../polo-plus-size-xxl-shirt/p/.." -> "xxl"
    const extractLabelFromHref = (href) => {
        if (!href) return '';
        try {
            // Grab the path segment before /p/ or the last meaningful slug
            const match = href.match(/\/([^\/]+)\/p\//i);
            const slug = match ? match[1] : href.split('/').filter(Boolean).slice(-2, -1)[0] || '';
            // Clean up slug: remove numbers-only tokens, dashes become spaces
            const parts = slug.split('-').filter(p => p.length > 1 && !/^\d+$/.test(p));
            // Deduplicate while keeping order
            const seen = new Set();
            return parts.filter(p => { const k = p.toLowerCase(); if (seen.has(k)) return false; seen.add(k); return true; }).join(' ');
        } catch (e) { return ''; }
    };

    const getElementText = (el) => {
        let text = '';
        
        // 1. Direct attributes
        text = el.getAttribute('aria-label') || 
               el.getAttribute('title') || 
               el.getAttribute('data-color') || 
               el.getAttribute('data-colorname') || 
               el.getAttribute('data-value') || 
               el.getAttribute('alt') || '';
               
        // 2. If it's a LABEL, try associated input (via for) or nested input
        if (!text.trim() && el.tagName === 'LABEL') {
            let input = null;
            const forId = el.getAttribute('for');
            if (forId) {
                input = document.getElementById(forId);
            }
            if (!input) {
                input = el.querySelector('input');
            }
            if (input) {
                text = input.getAttribute('aria-label') || 
                       input.getAttribute('value') || 
                       input.getAttribute('title') || 
                       input.getAttribute('data-color') || 
                       input.getAttribute('data-colorname') || '';
            }
        }
        
        // 3. If it contains an image, check image attributes
        if (!text.trim()) {
            const img = el.tagName === 'IMG' ? el : el.querySelector('img');
            if (img) {
                text = img.getAttribute('aria-label') || 
                       img.getAttribute('title') || 
                       img.getAttribute('alt') || 
                       img.getAttribute('data-color') || 
                       img.getAttribute('data-colorname') || '';
            }
        }
        
        // 4. Fallback to text content
        if (!text.trim()) {
            text = (el.textContent || '').trim();
        }
        
        // 5. URL slug extraction for links
        if (!text.trim() || text.toLowerCase() === 'image') {
            const anchor = (el.tagName === 'A') ? el : el.closest('a');
            if (anchor && anchor.href) {
                text = extractLabelFromHref(anchor.href);
            }
        }
        
        return text.trim();
    };

    // Collect clickable swatch/option elements inside a section container
    const findSwatchesInSection = (section) => {
        const swatchSelectors = [
            'a',                          // plain anchor links (Flipkart/Myntra color nav)
            'button',
            'label',                      // <label> tags (Gymshark, Shopify, etc.)
            'a[role="button"]',
            'input[type="radio"]',
            'input[type="checkbox"]',
            '[role="radio"]',
            '[role="checkbox"]',
            '[role="option"]',
            '[role="button"]',
            'li',                         // plain list items (highly common for dropdowns/drawers)
            'div[class*="swatch" i]', 'div[class*="option" i]', 'div[class*="size" i]', 'div[class*="item" i]', 'div[class*="value" i]',
            'span[class*="swatch" i]', 'span[class*="option" i]', 'span[class*="size" i]', 'span[class*="item" i]', 'span[class*="value" i]',
            'img[class*="swatch" i]',
        ];
        const seen = new Set();
        const results = [];

        for (const sel of swatchSelectors) {
            section.querySelectorAll(sel).forEach(el => {
                if (seen.has(el)) return;
                seen.add(el);

                // Visibility check
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return;
                const rect = el.getBoundingClientRect();
                if (rect.width < 4 || rect.height < 4) return;

                // Too large = probably not a swatch
                if (rect.width > 500 || rect.height > 250) return;

                // Get best text label using our helper
                let text = getElementText(el);

                if (!text) return; // Still no label — skip

                // Exclude non-swatch text
                if (isExcludedText(text)) return;

                // For color searches: skip if text looks like a clothing size
                if (normType === 'color' && CLOTHING_SIZE_RE.test(text.trim())) return;

                // For size searches: skip if the element is extremely small (likely icon, not swatch)
                if (normType === 'size') {
                    const isTooSmall = rect.width < 6 || rect.height < 6;
                    if (isTooSmall) return;
                }

                // Skip aria-labels starting with "Quick Add"
                const ariaLabel = (el.getAttribute('aria-label') || '').trim();
                if (/^quick[\s-]?add/i.test(ariaLabel)) return;

                results.push({ el, text, rect });
            });
        }
        return results;
    };

    // MAIN LOGIC
    let candidateSets = [];

    // Phase 1: Label-anchored search
    const labels = findVariantLabels();
    for (const label of labels) {
        const section = findSectionContainer(label);
        const swatches = findSwatchesInSection(section);
        if (swatches.length > 0) {
            candidateSets.push(swatches);
            break; // Use first valid label match
        }
    }

    // Phase 2: If no results, try broad swatch attribute selectors
    if (candidateSets.length === 0) {
        const broadMap = {
            'color': [
                '[class*="color-swatch" i]', '[class*="colorswatch" i]',
                '[class*="swatch" i]',
                'button[class*="swatch" i]', 'img[class*="swatch" i]',
                'label[class*="color" i]', 'label[class*="swatch" i]',
                '[data-colorname]', '[data-color]',
                // Flipkart/Myntra specific: anchor links with color in URL
                'a[href*="/p/"]', 'li a[href*="/p/"]'
            ],
            'size': [
                'button[class*="size" i]', '[class*="size-swatch" i]',
                '[class*="size-option" i]', '[class*="sizeswatch" i]',
                'label[class*="size" i]', '[class*="size_size" i]',
                'div[class*="size" i]', 'div[class*="option" i]',
                'div[class*="item" i]', 'div[class*="value" i]',
                'span[class*="size" i]', 'span[class*="option" i]',
                'span[class*="item" i]', 'span[class*="value" i]',
                'button', '[role="button"]', '[role="radio"]',
                'li', 'span', '[role="option"]'
            ],
        };
        const broadSelectors = (broadMap[normType] || ['button[class*="swatch" i]']).join(',');
        const broadSet = [];
        const seen = new Set();
        document.querySelectorAll(broadSelectors).forEach(el => {
            if (seen.has(el)) return;
            seen.add(el);
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') return;
            const rect = el.getBoundingClientRect();
            if (rect.width < 5 || rect.height < 5 || rect.width > 500 || rect.height > 250) return;
            const ariaLabel = (el.getAttribute('aria-label') || '').trim();
            if (/^quick[\s-]?add/i.test(ariaLabel)) return;
            
            let text = getElementText(el);
            
            if (isExcludedText(text)) return;
            if (normType === 'color' && CLOTHING_SIZE_RE.test(text.trim())) return;
            
            // For size searches, if we matched a generic button/role, make sure the text looks like a size/number
            if (normType === 'size') {
                const trimmedText = text.trim();
                const isSizeLike = CLOTHING_SIZE_RE.test(trimmedText) || 
                                   /^(xs|s|m|l|xl|xxl|2xl|3xl|4xl|one size)\b/i.test(trimmedText) ||
                                   /^\d{1,2}(\.\d)?w?\b/i.test(trimmedText) ||
                                   /^(us|uk|eu|jp)?\s*\d{1,2}(\.\d)?\b/i.test(trimmedText) ||
                                   /size\s*(xs|s|m|l|xl|xxl|2xl|3xl|4xl|\d{1,2})/i.test(trimmedText);
                if (!isSizeLike) return;
            }
            
            broadSet.push({ el, text, rect });
        });
        if (broadSet.length > 0) candidateSets.push(broadSet);
    }

    if (candidateSets.length === 0) {
        return { success: false, candidatesCount: 0, candidates: [], error: 'No candidates found' };
    }

    const swatches = candidateSets[0];

    // Sort top-to-bottom, left-to-right
    swatches.sort((a, b) => {
        if (Math.abs(a.rect.top - b.rect.top) > 15) return a.rect.top - b.rect.top;
        return a.rect.left - b.rect.left;
    });

    // Create overlay container
    const overlayContainer = document.createElement('div');
    overlayContainer.id = 'automation-variant-candidates-container';
    overlayContainer.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:2147483647;';
    document.body.appendChild(overlayContainer);

    const scrollX = window.scrollX || window.pageXOffset;
    const scrollY = window.scrollY || window.pageYOffset;

    const results = swatches.map(({ el, text, rect }, idx) => {
        el.setAttribute('data-automation-variant-target', idx);
        
        const absX = rect.left + scrollX + rect.width / 2;
        const absY = rect.top + scrollY + rect.height / 2;

        const overlay = document.createElement('div');
        overlay.className = 'automation-variant-candidate';
        overlay.setAttribute('data-candidate-index', idx);
        overlay.style.cssText = `
            position:absolute;
            left:${rect.left + scrollX}px;
            top:${rect.top + scrollY}px;
            width:${rect.width}px;
            height:${rect.height}px;
            border:3px solid #d500f9;
            background:rgba(213,0,249,0.15);
            pointer-events:none;
            font-family:monospace;
            font-size:11px;
            font-weight:bold;
            color:#d500f9;
            display:flex;
            align-items:center;
            justify-content:center;
            z-index:2147483647;
            box-sizing:border-box;
        `;
        overlay.textContent = `#${idx + 1}`;
        overlayContainer.appendChild(overlay);

        return {
            index: idx,
            tagName: el.tagName,
            text: text.substring(0, 40),
            absX: Math.round(absX),
            absY: Math.round(absY),
            width: Math.round(rect.width),
            height: Math.round(rect.height),
        };
    });

    return { success: results.length > 0, candidatesCount: results.length, candidates: results };
}
