/**
 * close_distractions.js  — v2 (universal)
 * Dismisses cookie banners, newsletter modals, age gates, geo-redirect dialogs,
 * GDPR notices, promotional overlays, and chat widgets before automation runs.
 *
 * Strategy (in order):
 *  1. Known vendor selectors (OneTrust, Klaviyo, Attentive, Privy, Intercom…)
 *  2. Aria-label / role "dialog" close buttons
 *  3. Text-based button scan ("Close", "No thanks", "Accept", etc.)
 *  4. High-z-index viewport-covering overlay heuristic
 *  5. body/html overflow reset + ESC key
 */
() => {
    const results = { closed: [], removed: [], hidden: [], errors: [] };

    /* ── helpers ───────────────────────────────────────────────────── */
    const isVisible = el => {
        if (!el) return false;
        const s = window.getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };

    const robustClick = el => {
        try {
            el.focus();
            el.click();
            ['mousedown','mouseup','click'].forEach(t =>
                el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window }))
            );
            if (['BUTTON','A'].includes(el.tagName) || el.getAttribute('role') === 'button')
                el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
            return true;
        } catch { return false; }
    };

    const tryClick = (selector, label) => {
        try {
            document.querySelectorAll(selector).forEach(el => {
                if (isVisible(el) && robustClick(el))
                    results.closed.push(label || selector);
            });
        } catch(e) { results.errors.push(`${label}: ${e.message}`); }
    };

    const tryRemove = (selector, label) => {
        try {
            document.querySelectorAll(selector).forEach(el => {
                el.remove();
                results.removed.push(label || selector);
            });
        } catch {}
    };

    /* ── 1. Known vendor close / accept buttons ────────────────────── */
    const vendorSelectors = [
        // Cookie / GDPR consent
        '#onetrust-accept-btn-handler',          // OneTrust accept
        '#onetrust-reject-all-handler',
        '.onetrust-close-btn-handler',
        '#onetrust-pc-btn-handler',
        'button#accept-recommended-btn-handler',
        '[id*="cookie"] button[class*="accept" i]',
        '[id*="cookie"] button[class*="close" i]',
        '[class*="cookie-banner"] button',
        '[class*="cookiebanner"] button',
        '[class*="cookie-consent"] button[class*="accept" i]',
        '[class*="gdpr"] button[class*="accept" i]',
        '#CybotCookiebotDialogBodyButtonAccept',  // Cookiebot
        '.cc-btn.cc-accept-all',                  // CookieConsent.js
        '#cookie-law-info-bar button',
        '[aria-label="Accept cookies"]',
        '[aria-label="Accept all cookies"]',

        // Newsletter / marketing modals
        '.klaviyo-close-form',
        '[data-testid="klaviyo-form-close"]',
        '.klaviyo-form-close',
        '.needsclick.kl-private-reset-css-Xuajs1',
        '[class*="attentive"] button[class*="close" i]',
        '#attentive_overlay button[title*="close" i]',
        '[id*="attentive"] button',
        '.privy-dismiss',
        '.privy-close',
        '#privy-popup-x',
        '[data-privy-close]',
        '.popup-close',  '.modal-close', '.dialog-close',
        '.newsletter-close', '#close-newsletter',
        '[class*="closeBtn"]', '[class*="close-btn"]',
        '[class*="closeButton"]', '[class*="CloseButton"]',
        '[class*="dismiss"]',

        // Chat / live-chat widgets
        '[class*="intercom"] button[class*="close" i]',
        '#intercom-container button[aria-label*="close" i]',
        '.drift-widget-close',
        '[class*="livechat"] button[class*="close" i]',
        '[class*="chat-widget"] button[class*="close" i]',
        '[aria-label="Close chat"]',

        // Age-gate "confirm" (I am 18+)
        '[class*="age-gate"] button',
        '[id*="age-gate"] button',
        '[class*="ageGate"] button',
        '[class*="agegate"] button',

        // Generic modal/dialog close
        'button[aria-label="close" i]',
        'button[aria-label="dismiss" i]',
        'button[aria-label="Close dialog" i]',
        'button[aria-label="Close modal" i]',
        'button[data-dismiss="modal"]',
        'button[data-close]',
        '[role="dialog"] button[class*="close" i]',
        '[role="dialog"] button[aria-label*="close" i]',
        'dialog button[class*="close" i]',

        // Geo / country redirect
        '[class*="country-modal"] button[class*="close" i]',
        '[class*="location-modal"] button[class*="close" i]',
        '[class*="geo"] button[class*="close" i]',

        // Shopify-specific
        '.shopify-section button[class*="close" i]',
        '[id*="popup-modal"] button',
        '[id*="PopupModal"] button',
    ];

    vendorSelectors.forEach(sel => tryClick(sel, sel.slice(0, 60)));

    /* ── 2. Text-based scan ────────────────────────────────────────── */
    const CLOSE_TEXTS = new Set([
        'close','x','✕','✖','×','dismiss','cancel','skip','no thanks',
        'no, thanks','not now','maybe later','i\'ll pass','continue without accepting',
        'reject all','decline','deny','got it','okay','ok','i agree','accept',
        'accept all','accept cookies','agree & continue','i am 18+','i am of legal age',
        'enter site','yes, i am','confirm my age',
    ]);

    const CLOSE_ARIA = ['close','dismiss','no thanks','reject','accept cookies'];

    try {
        const candidates = Array.from(document.querySelectorAll(
            'button, a[role="button"], [role="button"], span[data-close], div[data-close]'
        ));
        candidates.forEach(el => {
            if (!isVisible(el)) return;
            const text = (el.textContent || '').trim().toLowerCase();
            const aria = (el.getAttribute('aria-label') || '').toLowerCase();
            // Must be inside or near a modal/overlay/dialog/banner
            const parent = el.closest(
                '[class*="modal" i],[class*="popup" i],[class*="dialog" i],' +
                '[class*="overlay" i],[class*="banner" i],[class*="consent" i],' +
                '[class*="cookie" i],[class*="newsletter" i],[class*="age-gate" i],' +
                '[id*="modal" i],[id*="popup" i],[id*="dialog" i],[id*="overlay" i],' +
                '[role="dialog"],[role="alertdialog"],dialog'
            );
            const isInModal = !!parent;
            const matchesText = CLOSE_TEXTS.has(text) || CLOSE_TEXTS.has(text.slice(0,30));
            const matchesAria = CLOSE_ARIA.some(a => aria.includes(a));
            if ((matchesText || matchesAria) && (isInModal || text === 'x' || text === '✕' || text === '✖' || text === '×')) {
                if (robustClick(el)) results.closed.push(`text:"${text}"`);
            }
        });
    } catch(e) { results.errors.push('text-scan: ' + e.message); }

    /* ── 3. Remove known backdrop elements ─────────────────────────── */
    [
        '.modal-backdrop','.overlay-backdrop','.fade.in',
        '#loading-mask','[class*="modal-backdrop"]','[class*="overlay-background"]',
        '[data-testid="success-overlay"]','[class*="success-overlay" i]',
        '[class*="attentive-overlay"]','[id*="attentive_overlay"]',
    ].forEach(sel => tryRemove(sel, sel));

    /* ── 4. Heuristic: large viewport-covering z-index elements ─────── */
    try {
        const vw = window.innerWidth, vh = window.innerHeight;
        document.querySelectorAll('div,section,dialog,aside').forEach(el => {
            try {
                if (!isVisible(el)) return;
                const s = window.getComputedStyle(el);
                const z = parseInt(s.zIndex, 10);
                if (isNaN(z) || z < 500) return;
                const r = el.getBoundingClientRect();
                if (!(['fixed','absolute','sticky'].includes(s.position))) return;
                if (r.width < vw * 0.35 || r.height < vh * 0.25) return;
                const tag = el.tagName.toLowerCase();
                const id = (el.id || '').toLowerCase();
                const cls = (typeof el.className === 'string' ? el.className : '').toLowerCase();
                const combined = id + ' ' + cls;
                // Skip structural page elements
                if (/product|pdp|gallery|header|footer|nav|sidebar|main|root|app|__next|cart|checkout/.test(combined)) return;
                if (['main','body','html','header','footer','nav'].includes(tag)) return;
                // Try to click a close button inside first
                const closeBtn = el.querySelector('button[class*="close" i],button[aria-label*="close" i],[class*="close" i]');
                if (closeBtn && isVisible(closeBtn)) {
                    robustClick(closeBtn);
                    results.closed.push(`heuristic-close-btn in ${tag}#${el.id}`);
                } else {
                    el.style.setProperty('display', 'none', 'important');
                    results.hidden.push(`${tag}#${el.id || ''}.${cls.split(' ').slice(0,3).join('.')}`);
                }
            } catch {}
        });
    } catch(e) { results.errors.push('heuristic: ' + e.message); }

    /* ── 5. Re-enable scroll + ESC key ─────────────────────────────── */
    try {
        document.body.style.setProperty('overflow', 'auto', 'important');
        document.documentElement.style.setProperty('overflow', 'auto', 'important');
    } catch {}

    try {
        document.dispatchEvent(new KeyboardEvent('keydown', {
            key:'Escape', keyCode:27, which:27, bubbles:true, cancelable:true
        }));
    } catch {}

    return results;
}
