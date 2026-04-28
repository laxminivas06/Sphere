// UCEF Main Javascript

document.addEventListener('DOMContentLoaded', () => {
    console.log('UCEF Platform Initialized');

    // Smooth scrolling
    document.querySelectorAll('a[href^="#"]').forEach(anchor => {
        anchor.addEventListener('click', function (e) {
            e.preventDefault();
            const target = document.querySelector(this.getAttribute('href'));
            if (target) target.scrollIntoView({ behavior: 'smooth' });
        });
    });
});

// ──────────────────────────────────────────────
//  Global Toast Notification System
// ──────────────────────────────────────────────
(function () {
    // Inject toast container + styles once
    const style = document.createElement('style');
    style.textContent = `
        #ucef-toast-container {
            position: fixed;
            bottom: 2rem;
            right: 2rem;
            z-index: 99999;
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
            pointer-events: none;
        }
        .ucef-toast {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            padding: 1rem 1.5rem;
            border-radius: 14px;
            font-family: 'Inter', sans-serif;
            font-size: 0.95rem;
            font-weight: 500;
            color: #fff;
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid rgba(255,255,255,0.12);
            box-shadow: 0 8px 32px rgba(0,0,0,0.4);
            min-width: 280px;
            max-width: 400px;
            pointer-events: all;
            animation: ucefSlideIn 0.35s cubic-bezier(0.34,1.56,0.64,1) forwards;
        }
        .ucef-toast.hide {
            animation: ucefSlideOut 0.3s ease forwards;
        }
        .ucef-toast.success { background: rgba(16,185,129,0.18); border-color: rgba(16,185,129,0.4); }
        .ucef-toast.error   { background: rgba(239,68,68,0.18);  border-color: rgba(239,68,68,0.4); }
        .ucef-toast.info    { background: rgba(99,102,241,0.18); border-color: rgba(99,102,241,0.4); }
        .ucef-toast.warning { background: rgba(245,158,11,0.18); border-color: rgba(245,158,11,0.4); }
        .ucef-toast .toast-icon { font-size: 1.2rem; flex-shrink: 0; }
        .ucef-toast.success .toast-icon { color: #10b981; }
        .ucef-toast.error   .toast-icon { color: #ef4444; }
        .ucef-toast.info    .toast-icon { color: #6366f1; }
        .ucef-toast.warning .toast-icon { color: #f59e0b; }
        @keyframes ucefSlideIn  { from { opacity:0; transform: translateX(60px); } to { opacity:1; transform: translateX(0); } }
        @keyframes ucefSlideOut { from { opacity:1; transform: translateX(0); }    to { opacity:0; transform: translateX(60px); } }
        @media (max-width: 600px) {
            #ucef-toast-container { bottom:1rem; right:1rem; left:1rem; }
            .ucef-toast { min-width: unset; width: 100%; }
        }
    `;
    document.head.appendChild(style);

    const container = document.createElement('div');
    container.id = 'ucef-toast-container';
    document.body.appendChild(container);
})();

/**
 * Show a toast notification.
 * @param {string} message  - Text to display
 * @param {'success'|'error'|'info'|'warning'} type
 * @param {number} duration - Auto-dismiss after ms (default 4000)
 */
function showToast(message, type = 'success', duration = 4000) {
    const icons = { success: 'fa-check-circle', error: 'fa-times-circle', info: 'fa-info-circle', warning: 'fa-exclamation-triangle' };
    const container = document.getElementById('ucef-toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `ucef-toast ${type}`;
    toast.innerHTML = `<i class="fas ${icons[type] || icons.info} toast-icon"></i><span>${message}</span>`;
    container.appendChild(toast);

    // Auto-dismiss
    const dismissTimer = setTimeout(() => dismiss(toast), duration);
    toast.addEventListener('click', () => { clearTimeout(dismissTimer); dismiss(toast); });

    function dismiss(el) {
        el.classList.add('hide');
        el.addEventListener('animationend', () => el.remove(), { once: true });
    }
}

// Legacy alias
function notify(message, type = 'success') {
    showToast(message, type === 'error' ? 'error' : 'success');
}

