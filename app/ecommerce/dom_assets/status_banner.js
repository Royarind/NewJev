/**
 * status_banner.js
 * Display onscreen status messages during automation
 */
(args) => {
    const { message, type = 'info', duration = 3000 } = args || {};

    // Create or get existing status container
    let container = document.getElementById('automation-status-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'automation-status-container';
        container.style.cssText = `
            position: fixed;
            top: 10px;
            right: 10px;
            z-index: 999999;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            pointer-events: none;
        `;
        document.body.appendChild(container);
    }

    // Create status message element
    const statusEl = document.createElement('div');
    statusEl.style.cssText = `
        background: ${type === 'success' ? '#10b981' : type === 'error' ? '#ef4444' : '#3b82f6'};
        color: white;
        padding: 12px 20px;
        border-radius: 8px;
        margin-bottom: 8px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        font-size: 14px;
        font-weight: 500;
        opacity: 0;
        transform: translateX(100px);
        transition: all 0.3s ease-out;
        max-width: 350px;
        word-wrap: break-word;
    `;

    // Add icon based on type
    const icon = type === 'success' ? '✓' : type === 'error' ? '✗' : '◉';
    statusEl.innerHTML = `<span style="margin-right: 8px;">${icon}</span>${message}`;

    container.appendChild(statusEl);

    // Animate in
    setTimeout(() => {
        statusEl.style.opacity = '1';
        statusEl.style.transform = 'translateX(0)';
    }, 10);

    // Auto-remove after duration
    if (duration > 0) {
        setTimeout(() => {
            statusEl.style.opacity = '0';
            statusEl.style.transform = 'translateX(100px)';
            setTimeout(() => statusEl.remove(), 300);
        }, duration);
    }

    return { success: true, message: 'Status displayed' };
}
