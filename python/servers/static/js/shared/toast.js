(function () {
    function getEls() {
        const toast = document.getElementById('toast-notification');
        const container = document.getElementById('toast-container');
        const icon = document.getElementById('toast-icon');
        const title = document.getElementById('toast-title');
        const message = document.getElementById('toast-message');
        return { toast, container, icon, title, message };
    }

    function getTypeConfig(type) {
        if (type === 'error') {
            return {
                icon: 'fa-exclamation-circle',
                iconColor: 'text-red-600',
                bg: 'bg-red-50',
                border: 'border-red-500'
            };
        }
        if (type === 'warning') {
            return {
                icon: 'fa-exclamation-triangle',
                iconColor: 'text-yellow-600',
                bg: 'bg-yellow-50',
                border: 'border-yellow-500'
            };
        }
        if (type === 'info') {
            return {
                icon: 'fa-info-circle',
                iconColor: 'text-blue-600',
                bg: 'bg-blue-50',
                border: 'border-blue-500'
            };
        }
        return {
            icon: 'fa-check-circle',
            iconColor: 'text-green-600',
            bg: 'bg-green-50',
            border: 'border-green-500'
        };
    }

    function hideToast() {
        const { toast, container } = getEls();
        if (!toast || !container) return;

        container.classList.remove('translate-x-0', 'opacity-100');
        container.classList.add('translate-x-full', 'opacity-0');

        setTimeout(() => {
            toast.classList.add('hidden');
        }, 300);
    }

    function showToast(title, message, type = 'success') {
        const { toast, container, icon, title: titleEl, message: messageEl } = getEls();
        if (!toast || !container || !icon || !titleEl || !messageEl) return;

        const cfg = getTypeConfig(type);
        icon.className = `fas ${cfg.icon} text-2xl ${cfg.iconColor}`;
        container.className = `rounded-lg shadow-2xl border-l-4 ${cfg.border} p-4 flex items-start space-x-3 transform transition-all duration-300 ease-out ${cfg.bg} translate-x-full opacity-0`;

        titleEl.textContent = title;
        if (typeof message === 'string' && message.indexOf('<') !== -1) {
            messageEl.innerHTML = message;
        } else {
            messageEl.textContent = message;
        }

        toast.classList.remove('hidden');
        requestAnimationFrame(() => {
            requestAnimationFrame(() => {
                container.classList.remove('translate-x-full', 'opacity-0');
                container.classList.add('translate-x-0', 'opacity-100');
            });
        });

        setTimeout(() => {
            hideToast();
        }, 4000);
    }

    window.showToast = showToast;
    window.hideToast = hideToast;
})();
