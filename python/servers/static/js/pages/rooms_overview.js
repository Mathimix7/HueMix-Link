let scrollPosition = 0;
let activeTab = 'rooms';

    // Create a single tooltip element for room swatches
    const __roomTooltip = document.createElement('div');
    __roomTooltip.className = 'room-tooltip';
    __roomTooltip.innerHTML = '<div class="title"></div><div class="meta"></div>';
    document.body.appendChild(__roomTooltip);

    function __positionRoomTooltip(clientX, clientY) {
        const margin = 8;
        const offset = 12;

        // Start near cursor, then clamp inside viewport so tooltip never jumps off-screen.
        let left = clientX + offset;
        let top = clientY - offset;

        const tooltipWidth = __roomTooltip.offsetWidth || 0;
        const tooltipHeight = __roomTooltip.offsetHeight || 0;

        if (left + tooltipWidth + margin > window.innerWidth) {
            left = window.innerWidth - tooltipWidth - margin;
        }
        if (left < margin) {
            left = margin;
        }

        if (top + tooltipHeight + margin > window.innerHeight) {
            top = window.innerHeight - tooltipHeight - margin;
        }
        if (top < margin) {
            top = margin;
        }

        __roomTooltip.style.left = left + 'px';
        __roomTooltip.style.top = top + 'px';
    }

    function __showRoomTooltip(target, clientX, clientY) {
        const title = target.dataset.lightName || '';
        const colorName = target.dataset.colorName || '';
        const brightness = (typeof target.dataset.brightness !== 'undefined' && target.dataset.brightness !== '') ? target.dataset.brightness + '%' : '—';
        __roomTooltip.querySelector('.title').textContent = title;
        __roomTooltip.querySelector('.meta').textContent = `${colorName} · ${brightness}`;
        __roomTooltip.classList.add('visible');
        __positionRoomTooltip(clientX, clientY);
    }

    function __hideRoomTooltip() {
        __roomTooltip.classList.remove('visible');
    }

    // Delegate mouse events to show/hide/update tooltip
    document.addEventListener('mouseover', (ev) => {
        const sw = ev.target.closest('.room-swatch');
        if (sw) __showRoomTooltip(sw, ev.clientX, ev.clientY);
    });
    document.addEventListener('mousemove', (ev) => {
        if (__roomTooltip.classList.contains('visible')) {
            __positionRoomTooltip(ev.clientX, ev.clientY);
        }
    });
    document.addEventListener('mouseout', (ev) => {
        const sw = ev.target.closest('.room-swatch');
        if (sw) __hideRoomTooltip();
    });
    
    // ---- Tab switching (Rooms | Zones) ----
    function setActiveTab(tab) {
        activeTab = tab;
        const roomsTab = document.getElementById('tab-rooms');
        const zonesTab = document.getElementById('tab-zones');
        const roomsContainer = document.getElementById('rooms-container');
        const zonesContainer = document.getElementById('zones-container');

        if (roomsTab) {
            roomsTab.classList.toggle('active', tab === 'rooms');
            roomsTab.setAttribute('aria-selected', String(tab === 'rooms'));
        }
        if (zonesTab) {
            zonesTab.classList.toggle('active', tab === 'zones');
            zonesTab.setAttribute('aria-selected', String(tab === 'zones'));
        }
        if (roomsContainer) roomsContainer.classList.toggle('hidden', tab !== 'rooms');
        if (zonesContainer) zonesContainer.classList.toggle('hidden', tab !== 'zones');
    }

    // ---- Card builder (identical markup for rooms and zones) ----
    function buildGroupCard(group) {
        const card = document.createElement('div');
        card.className = 'bg-white rounded-xl p-4 shadow-lg border-2 border-gray-100 hover:shadow-xl hover:border-blue-200 transition-all duration-300 flex items-center gap-4';

        // Group name
        const nameDiv = document.createElement('div');
        nameDiv.className = 'font-semibold text-gray-900 text-lg min-w-[180px] flex-shrink-0';
        nameDiv.textContent = group.name;
        card.appendChild(nameDiv);

        // Colors container
        const colorsDiv = document.createElement('div');
        colorsDiv.className = 'flex gap-2 flex-1 flex-wrap';

        // Prefer per-light data so we can show lamp name, brightness and color name on hover
        if (group.lights && group.lights.length > 0) {
            group.lights.forEach(light => {
                if (!light.is_on || !light.color_hex) return;
                const dot = document.createElement('div');
                dot.className = 'w-7 h-7 rounded-lg border-2 border-white shadow-md hover:scale-110 transition-transform room-swatch';
                dot.style.backgroundColor = light.color_hex;
                dot.dataset.lightName = light.name || '';
                dot.dataset.colorName = light.color_name || '';
                dot.dataset.brightness = (typeof light.brightness !== 'undefined' && light.brightness !== null) ? light.brightness : '';
                dot.setAttribute('role', 'img');
                dot.setAttribute('aria-label', `${light.name} ${light.color_name || ''} ${light.brightness || ''}`);
                colorsDiv.appendChild(dot);
            });
        } else if (group.colors && group.colors.length > 0) {
            group.colors.forEach(color => {
                const dot = document.createElement('div');
                dot.className = 'w-7 h-7 rounded-lg border-2 border-white shadow-md hover:scale-110 transition-transform';
                dot.style.backgroundColor = color;
                colorsDiv.appendChild(dot);
            });
        }
        card.appendChild(colorsDiv);

        // Scene + brightness pill
        const sceneDiv = document.createElement('div');
        sceneDiv.className = 'px-4 py-2 bg-gradient-to-r from-gray-50 to-gray-100 rounded-lg text-sm text-gray-600 font-medium min-w-[140px] text-right flex-shrink-0 border border-gray-200';
        const sceneName = group.current_scene || group.current_scene_id || '—';
        const avgBrightness = (typeof group.avg_brightness !== 'undefined' && group.avg_brightness !== null) ? `${group.avg_brightness}%` : '—';
        if (sceneName && sceneName !== '—') {
            sceneDiv.textContent = `${sceneName} · ${avgBrightness}`;
        } else if (avgBrightness && avgBrightness !== '—' && avgBrightness !== '0%') {
            sceneDiv.textContent = `No scene · ${avgBrightness}`;
        } else {
            sceneDiv.textContent = '—';
            sceneDiv.className = 'px-4 py-2 bg-gray-50 rounded-lg text-sm text-gray-400 font-medium min-w-[140px] text-right flex-shrink-0 border border-gray-100';
        }
        card.appendChild(sceneDiv);

        return card;
    }

    function renderGroups(container, groups, emptyMessage) {
        if (!container) return;
        if (!groups || groups.length === 0) {
            container.innerHTML = `<div class="text-center text-gray-500 py-8">${emptyMessage}</div>`;
            return;
        }
        container.innerHTML = '';
        groups.forEach(group => container.appendChild(buildGroupCard(group)));
    }

    async function loadOverviewData() {
        try {
            __hideRoomTooltip();
            const response = await fetch('/api/rooms-overview/data');
            const data = await response.json();

            const errorState = document.getElementById('error-state');
            const errorMsg = document.getElementById('error-message');
            const roomsContainer = document.getElementById('rooms-container');
            const zonesContainer = document.getElementById('zones-container');
            const tabsBar = document.getElementById('overview-tabs');
            const subtitle = document.getElementById('page-subtitle');

            if (data.success) {
                // Hide error state
                if (errorState) errorState.classList.add('hidden');
                if (errorMsg) errorMsg.textContent = '';

                const rooms = data.rooms || [];
                const zones = data.zones || [];

                renderGroups(roomsContainer, rooms, 'No rooms found');
                renderGroups(zonesContainer, zones, 'No zones found');

                // Zones are optional: hide the tabs entirely when none exist
                const hasZones = zones.length > 0;
                if (tabsBar) tabsBar.classList.toggle('hidden', !hasZones);
                if (subtitle) {
                    subtitle.textContent = hasZones
                        ? 'All your smart home rooms and zones at a glance'
                        : 'All your smart home rooms at a glance';
                }
                if (!hasZones && activeTab !== 'rooms') activeTab = 'rooms';
                setActiveTab(activeTab);
            } else {
                // Show error state, hide main content
                if (errorState) errorState.classList.remove('hidden');
                if (errorMsg) errorMsg.textContent = data.error || 'Unknown error';
                if (roomsContainer) roomsContainer.classList.add('hidden');
                if (zonesContainer) zonesContainer.classList.add('hidden');
                if (tabsBar) tabsBar.classList.add('hidden');
            }
        } catch (error) {
            console.error('Error loading overview data:', error);
            const errorState = document.getElementById('error-state');
            const errorMsg = document.getElementById('error-message');
            const roomsContainer = document.getElementById('rooms-container');
            const zonesContainer = document.getElementById('zones-container');
            const tabsBar = document.getElementById('overview-tabs');
            if (errorState) errorState.classList.remove('hidden');
            if (errorMsg) errorMsg.textContent = 'Failed to load rooms and zones data';
            if (roomsContainer) roomsContainer.classList.add('hidden');
            if (zonesContainer) zonesContainer.classList.add('hidden');
            if (tabsBar) tabsBar.classList.add('hidden');
        }
    }

    // Save scroll position before refresh
    function saveScrollPosition() {
        scrollPosition = window.scrollY;
    }

    // Restore scroll position after refresh
    function restoreScrollPosition() {
        window.scrollTo(0, scrollPosition);
    }

    // Tab click handlers
    const tabRoomsBtn = document.getElementById('tab-rooms');
    const tabZonesBtn = document.getElementById('tab-zones');
    if (tabRoomsBtn) tabRoomsBtn.addEventListener('click', () => setActiveTab('rooms'));
    if (tabZonesBtn) tabZonesBtn.addEventListener('click', () => setActiveTab('zones'));

    // Initial load
    loadOverviewData();

    // Auto-refresh every 2 seconds
    setInterval(() => {
        saveScrollPosition();
        loadOverviewData().then(restoreScrollPosition);
    }, 2000);
