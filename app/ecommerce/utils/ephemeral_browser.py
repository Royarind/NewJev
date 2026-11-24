#!/usr/bin/env python3
"""
Ephemeral Browser Manager
Uses Chrome in incognito mode with full cleanup after automation
"""

import os
import asyncio
import logging
import random
from typing import Optional
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Realistic User-Agent pool (Chrome 124-126, Windows/Mac/Linux)
# ─────────────────────────────────────────────────────────────────────────────
_USER_AGENTS = [
    # Chrome 124 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 125 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Chrome 126 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # Chrome 124 Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 125 Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Chrome 126 Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # Chrome 124 Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# ─────────────────────────────────────────────────────────────────────────────
# Stealth init script — patches all major bot-detection vectors
# ─────────────────────────────────────────────────────────────────────────────
_STEALTH_INIT_SCRIPT = """
(() => {
    // ── 1. Hide navigator.webdriver ────────────────────────────────────────
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // ── 2. Realistic chrome object ─────────────────────────────────────────
    if (!window.chrome) {
        window.chrome = {
            runtime: {
                PlatformOs: { MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' },
                PlatformArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' },
                PlatformNaclArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' },
                RequestUpdateCheckStatus: { THROTTLED: 'throttled', NO_UPDATE: 'no_update', UPDATE_AVAILABLE: 'update_available' },
                OnInstalledReason: { INSTALL: 'install', UPDATE: 'update', CHROME_UPDATE: 'chrome_update', SHARED_MODULE_UPDATE: 'shared_module_update' },
                OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
                connect: () => {},
                sendMessage: () => {},
            },
            loadTimes: () => ({
                requestTime: Date.now() / 1000 - Math.random() * 2,
                startLoadTime: Date.now() / 1000 - Math.random() * 1.5,
                commitLoadTime: Date.now() / 1000 - Math.random(),
                finishDocumentLoadTime: Date.now() / 1000 - Math.random() * 0.5,
                finishLoadTime: Date.now() / 1000,
                firstPaintTime: Date.now() / 1000 - Math.random() * 0.3,
                firstPaintAfterLoadTime: 0,
                navigationType: 'Other',
                wasFetchedViaSpdy: false,
                wasNpnNegotiated: false,
                npnNegotiatedProtocol: '',
                wasAlternateProtocolAvailable: false,
                connectionInfo: 'h2',
            }),
            csi: () => ({
                startE: Date.now(),
                onloadT: Date.now() + Math.floor(Math.random() * 300 + 200),
                pageT: Math.random() * 5000 + 1000,
                tran: 15,
            }),
            app: { isInstalled: false, InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }, RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } },
        };
    }

    // ── 3. Realistic navigator.plugins ────────────────────────────────────
    const pluginData = [
        { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Google Chrome PDF Viewer' },
        { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Chromium PDF Viewer' },
        { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Microsoft Edge PDF Viewer' },
        { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: 'WebKit built-in PDF' },
    ];
    const mockPluginArray = pluginData.map((p, i) => {
        const plugin = Object.create(Plugin.prototype);
        Object.defineProperties(plugin, {
            name:        { value: p.name, enumerable: true },
            filename:    { value: p.filename, enumerable: true },
            description: { value: p.description, enumerable: true },
            length:      { value: 0, enumerable: true },
        });
        return plugin;
    });
    Object.defineProperty(navigator, 'plugins', {
        get: () => Object.assign(mockPluginArray, { item: i => mockPluginArray[i], namedItem: n => mockPluginArray.find(p => p.name === n), refresh: () => {} }),
    });

    // ── 4. Languages ───────────────────────────────────────────────────────
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

    // ── 5. Permissions query ───────────────────────────────────────────────
    const _origPermQuery = window.navigator.permissions.query.bind(navigator.permissions);
    window.navigator.permissions.query = params =>
        params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : _origPermQuery(params);

    // ── 6. WebGL vendor/renderer spoof ────────────────────────────────────
    const vendors = [
        ['Intel Inc.', 'Intel(R) Iris(R) Xe Graphics'],
        ['Google Inc. (NVIDIA)', 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)'],
        ['Google Inc. (Intel)', 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)'],
    ];
    const [vendor, renderer] = vendors[Math.floor(Math.random() * vendors.length)];
    const _getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
        if (p === 37445) return vendor;
        if (p === 37446) return renderer;
        return _getParam.call(this, p);
    };
    if (window.WebGL2RenderingContext) {
        const _getParam2 = WebGL2RenderingContext.prototype.getParameter;
        WebGL2RenderingContext.prototype.getParameter = function(p) {
            if (p === 37445) return vendor;
            if (p === 37446) return renderer;
            return _getParam2.call(this, p);
        };
    }

    // ── 7. Canvas fingerprint noise (subtle, per-session) ─────────────────
    const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
    const _toBlob    = HTMLCanvasElement.prototype.toBlob;
    const _getImageData = CanvasRenderingContext2D.prototype.getImageData;
    const _noise = Math.floor(Math.random() * 10);
    HTMLCanvasElement.prototype.toDataURL = function(...args) {
        const ctx = this.getContext('2d');
        if (ctx) {
            const d = ctx.getImageData(0, 0, this.width || 1, this.height || 1);
            d.data[0] = (d.data[0] + _noise) % 256;
            ctx.putImageData(d, 0, 0);
        }
        return _toDataURL.apply(this, args);
    };
    CanvasRenderingContext2D.prototype.getImageData = function(...args) {
        const d = _getImageData.apply(this, args);
        d.data[0] = (d.data[0] + _noise) % 256;
        return d;
    };

    // ── 8. Audio context fingerprint noise ────────────────────────────────
    if (window.AudioContext || window.webkitAudioContext) {
        const AC = window.AudioContext || window.webkitAudioContext;
        const _createOscillator = AC.prototype.createOscillator;
        AC.prototype.createOscillator = function() {
            const osc = _createOscillator.call(this);
            const _connect = osc.connect.bind(osc);
            osc.connect = function(dest, ...rest) {
                // Add imperceptible noise to prevent audio fingerprinting
                const gainNode = this.context.createGain();
                gainNode.gain.value = 1 + (Math.random() * 0.0001 - 0.00005);
                _connect(gainNode);
                gainNode.connect(dest, ...rest);
                return gainNode;
            };
            return osc;
        };
    }

    // ── 9. Screen / window properties ─────────────────────────────────────
    Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
    Object.defineProperty(screen, 'pixelDepth',  { get: () => 24 });

    // ── 10. Hide automation-related properties ─────────────────────────────
    delete window.__playwright;
    delete window.__pwInitScripts;
    delete window._$pjax;

    // ── 11. Consistent hardware concurrency ───────────────────────────────
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });

    // ── 12. Device memory ─────────────────────────────────────────────────
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

})();
"""


class EphemeralBrowserManager:
    """
    Browser manager that supports:
    1. Ephemeral Mode: Fresh incognito Chrome context (default)
    2. Persistent Mode: Uses a persistent profile directory
    3. CDP Mode: Connects to an existing Chrome instance on localhost:9222
    """
    
    def __init__(self, headless: bool = False):
        self.headless = headless
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        
        self.browser_mode = os.environ.get('BROWSER_MODE', 'ephemeral').strip().lower()
        self.cdp_url = os.environ.get('CDP_URL', 'http://127.0.0.1:9222').strip()
        self.persistent_profile_dir = os.environ.get(
            'PERSISTENT_PROFILE_DIR',
            os.path.join(os.environ.get('USERPROFILE', 'C:\\'), '.gemini', 'antigravity', 'chrome_profile')
        )
        self.persistent_profile_dir = os.path.abspath(self.persistent_profile_dir)
    
    async def __aenter__(self):
        """Context manager entry - sets up browser"""
        await self.setup()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - cleans up everything"""
        await self.cleanup()
    
    async def setup(self):
        """
        Setup Chrome browser depending on browser mode (CDP, persistent, or ephemeral)
        """
        try:
            self.playwright = await async_playwright().start()
            
            if self.browser_mode == 'cdp':
                logger.info(f"Connecting to existing Chrome instance via CDP at {self.cdp_url}...")
                self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_url)
                if not self.browser.contexts:
                    self.context = await self.browser.new_context()
                else:
                    self.context = self.browser.contexts[0]
                self.page = await self.context.new_page()
                logger.info("Connected to Chrome via CDP and opened a new page.")
                
            elif self.browser_mode == 'persistent':
                logger.info(f"Launching Chrome in persistent mode with profile at {self.persistent_profile_dir}...")
                os.makedirs(self.persistent_profile_dir, exist_ok=True)
                
                # Remove SingletonLock to prevent Chrome hanging if previous session was killed
                lock_file = os.path.join(self.persistent_profile_dir, 'SingletonLock')
                if os.path.exists(lock_file):
                    try:
                        os.remove(lock_file)
                        logger.info("Removed existing SingletonLock file from persistent profile")
                    except Exception as e:
                        logger.warning(f"Could not remove SingletonLock file: {e}")
                
                _ua = random.choice(_USER_AGENTS)
                logger.info(f"Stealth UA selected (persistent): {_ua[:60]}...")
                self.context = await self.playwright.chromium.launch_persistent_context(
                    user_data_dir=self.persistent_profile_dir,
                    channel='chrome',
                    headless=self.headless,
                    viewport={'width': 1280, 'height': 720},
                    locale='en-US',
                    timezone_id='America/New_York',
                    user_agent=_ua,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--no-sandbox',
                        '--disable-web-security',
                        '--disable-dev-shm-usage',
                        '--disable-gpu',
                        '--disable-extensions',
                        '--disable-plugins',
                        '--disable-sync',
                        '--disable-background-networking',
                        '--disable-translate',
                    ]
                )
                self.browser = None  # Browser is managed implicitly by persistent context
                
                if self.context.pages:
                    self.page = self.context.pages[0]
                else:
                    self.page = await self.context.new_page()
                    
                await self._hide_automation()
                logger.info("Chrome launched with persistent profile context")
                
            else:
                logger.info("Launching Chrome in incognito (ephemeral) mode...")
                self.browser = await self.playwright.chromium.launch(
                    channel='chrome',
                    headless=self.headless,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--no-sandbox',
                        '--disable-web-security',
                        '--disable-dev-shm-usage',
                        '--disable-gpu',
                        '--incognito',
                        '--disable-extensions',
                        '--disable-plugins',
                        '--disable-sync',
                        '--disable-background-networking',
                        '--disable-translate',
                    ]
                )
                
                # Pick a random realistic User-Agent for this session
                _ua = random.choice(_USER_AGENTS)
                logger.info(f"Stealth UA selected: {_ua[:60]}...")
                self.context = await self.browser.new_context(
                    viewport={'width': 1280, 'height': 720},
                    locale='en-US',
                    timezone_id='America/New_York',
                    permissions=[],
                    accept_downloads=False,
                    user_agent=_ua,
                )
                
                self.page = await self.context.new_page()
                await self._hide_automation()
                logger.info("Chrome launched in ephemeral mode")
                
        except Exception as e:
            logger.error(f"Browser setup failed: {e}")
            await self.cleanup()
            raise
    
    async def _hide_automation(self):
        """
        Inject comprehensive stealth patches into every page before any script runs.
        Covers: webdriver flag, chrome object, plugins, WebGL fingerprint,
        canvas noise, audio context noise, screen properties, hardware hints.
        """
        try:
            await self.page.add_init_script(_STEALTH_INIT_SCRIPT)
            logger.debug("Stealth init script injected.")
        except Exception as e:
            logger.warning(f"Could not inject stealth init script: {e}")
    
    async def human_delay(self, min_ms: int = 80, max_ms: int = 250):
        """
        Pause for a randomised human-like delay between min_ms and max_ms milliseconds.
        Use this between interactions to avoid robotic constant-interval timing.
        """
        delay = random.randint(min_ms, max_ms) / 1000.0
        await asyncio.sleep(delay)

    async def navigate(self, url: str, wait_until: str = 'domcontentloaded'):
        """Navigate to URL"""
        if not self.page:
            raise RuntimeError("Browser not initialized. Call setup() first.")
        
        logger.info(f"Navigating to: {url}")
        await self.page.goto(url, wait_until=wait_until, timeout=30000)
        await asyncio.sleep(1)  # Let page settle
    
    async def clear_cart(self):
        """
        Attempt to clear cart items before closing
        Common strategies across e-commerce sites
        """
        if not self.page:
            return
        
        try:
            logger.info("Attempting to clear cart...")
            
            # Strategy 1: Look for "Remove" or "Delete" buttons in cart
            remove_selectors = [
                'button[aria-label*="remove" i]',
                'button[aria-label*="delete" i]',
                'a[aria-label*="remove" i]',
                '.remove-item',
                '.delete-item',
                '[data-action="remove"]',
                '[data-action="delete"]',
            ]
            
            for selector in remove_selectors:
                try:
                    elements = await self.page.query_selector_all(selector)
                    for element in elements:
                        await element.click()
                        await asyncio.sleep(0.3)
                except:
                    continue
            
            # Strategy 2: Clear localStorage and sessionStorage
            await self.page.evaluate("""
                () => {
                    try {
                        localStorage.clear();
                        sessionStorage.clear();
                    } catch (e) {}
                }
            """)
            
            logger.info("Cart cleanup attempted")
            
        except Exception as e:
            logger.warning(f"Cart cleanup failed (non-critical): {e}")
    
    async def cleanup(self):
        """
        Complete cleanup for all browser modes (CDP, Persistent, Ephemeral)
        to ensure no orphan Chrome processes are left running and the cart is emptied.
        """
        try:
            logger.info(f"Starting cleanup for browser mode: {self.browser_mode}...")
            
            # 1. Clear cart
            if self.page and not self.page.is_closed():
                await self.clear_cart()
            
            # 2. Clear cookies
            if self.context:
                try:
                    await self.context.clear_cookies()
                    logger.info("Cookies cleared")
                except Exception as e:
                    logger.warning(f"Could not clear cookies: {e}")
                    
            # 3. Clear storage (localStorage, sessionStorage, indexedDB)
            if self.page and not self.page.is_closed():
                try:
                    await self.page.evaluate("""
                        async () => {
                            try { localStorage.clear(); } catch(e) {}
                            try { sessionStorage.clear(); } catch(e) {}
                            try {
                                const databases = await indexedDB.databases();
                                databases.forEach(db => {
                                    indexedDB.deleteDatabase(db.name);
                                });
                            } catch(e) {}
                            try {
                                if ('caches' in window) {
                                    const cacheNames = await caches.keys();
                                    await Promise.all(
                                        cacheNames.map(name => caches.delete(name))
                                    );
                                }
                            } catch(e) {}
                        }
                    """)
                    logger.info("Storage cleared")
                except Exception as e:
                    logger.warning(f"Could not clear storage: {e}")
                    
            # 4. Close page
            if self.page and not self.page.is_closed():
                await self.page.close()
                logger.info("Page closed")
                
            # 5. Close context / browser (this kills the Chrome session)
            if self.context and self.browser_mode != 'cdp':
                try:
                    await self.context.close()
                    logger.info("Browser context closed")
                except Exception as e:
                    logger.warning(f"Could not close context: {e}")
                    
            if self.browser:
                try:
                    await self.browser.close()
                    logger.info("Browser closed")
                except Exception as e:
                    logger.warning(f"Could not close browser: {e}")
                    
            # 6. Stop playwright
            if self.playwright:
                await self.playwright.stop()
                logger.info("Playwright stopped")
                
            logger.info("Complete cleanup finished - Chrome process killed and data cleared")
            
        except Exception as e:
            logger.error(f"Cleanup error (non-critical): {e}")
    
    async def take_screenshot(self, path: str = 'screenshot.png'):
        """Take screenshot for debugging"""
        if self.page:
            await self.page.screenshot(path=path)
            logger.info(f"Screenshot saved: {path}")
    
    def get_page(self) -> Page:
        """Get the current page"""
        if not self.page:
            raise RuntimeError("Browser not initialized")
        return self.page


# Convenience functions

async def run_with_ephemeral_browser(automation_func, headless: bool = False):
    """
    Run automation function with ephemeral browser
    Automatically cleans up after completion
    
    Usage:
        async def my_automation(page):
            await page.goto('...')
            # ... automation code
        
        await run_with_ephemeral_browser(my_automation)
    """
    async with EphemeralBrowserManager(headless=headless) as browser_mgr:
        page = browser_mgr.get_page()
        result = await automation_func(page)
        return result
