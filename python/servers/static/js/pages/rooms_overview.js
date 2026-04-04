let scrollPosition = 0;

    // Create a single tooltip element for room swatches
    const __roomTooltip = document.createElement('div');
    __roomTooltip.className = 'room-tooltip';
    __roomTooltip.innerHTML = '<div class="title"></div><div class="meta"></div>';
    document.body.appendChild(__roomTooltip);

    function __showRoomTooltip(target, pageX, pageY) {
        const title = target.dataset.lightName || '';
        const colorName = target.dataset.colorName || '';
        const brightness = (typeof target.dataset.brightness !== 'undefined' && target.dataset.brightness !== '') ? target.dataset.brightness + '%' : '—';
        __roomTooltip.querySelector('.title').textContent = title;
        __roomTooltip.querySelector('.meta').textContent = `${colorName} · ${brightness}`;
        __roomTooltip.style.left = (pageX + 12) + 'px';
        __roomTooltip.style.top = (pageY - 12) + 'px';
        __roomTooltip.classList.add('visible');
    }

    function __hideRoomTooltip() {
        __roomTooltip.classList.remove('visible');
    }

    // Delegate mouse events to show/hide/update tooltip
    document.addEventListener('mouseover', (ev) => {
        const sw = ev.target.closest('.room-swatch');
        if (sw) __showRoomTooltip(sw, ev.pageX, ev.pageY);
    });
    document.addEventListener('mousemove', (ev) => {
        if (__roomTooltip.classList.contains('visible')) {
            __roomTooltip.style.left = (ev.pageX + 12) + 'px';
            __roomTooltip.style.top = (ev.pageY - 12) + 'px';
        }
    });
    document.addEventListener('mouseout', (ev) => {
        const sw = ev.target.closest('.room-swatch');
        if (sw) __hideRoomTooltip();
    });
    
    async function loadRoomsData() {
        try {
            const response = await fetch('/api/rooms-overview/data');
            const data = await response.json();

            const errorState = document.getElementById('error-state');
            const errorMsg = document.getElementById('error-message');
            const roomsContainer = document.getElementById('rooms-container');

            if (data.success) {
                // Hide error state
                if (errorState) errorState.classList.add('hidden');
                if (errorMsg) errorMsg.textContent = '';
                if (roomsContainer) roomsContainer.classList.remove('hidden');

                if (data.rooms.length === 0) {
                    roomsContainer.innerHTML = '<div class="text-center text-gray-500 py-8">No rooms found</div>';
                    return;
                }

                roomsContainer.innerHTML = '';

                data.rooms.forEach(room => {
                    // Create room card
                    const card = document.createElement('div');
                    card.className = 'bg-white rounded-xl p-4 shadow-lg border-2 border-gray-100 hover:shadow-xl hover:border-blue-200 transition-all duration-300 flex items-center gap-4';
                    
                    // Room name
                    const nameDiv = document.createElement('div');
                    nameDiv.className = 'font-semibold text-gray-900 text-lg min-w-[180px] flex-shrink-0';
                    nameDiv.textContent = room.name;
                    card.appendChild(nameDiv);
                    
                    // Colors container
                    const colorsDiv = document.createElement('div');
                    colorsDiv.className = 'flex gap-2 flex-1 flex-wrap';
                    
                    // Prefer per-light data so we can show lamp name, brightness and color name on hover
                    if (room.lights && room.lights.length > 0) {
                        room.lights.forEach(light => {
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
                    } else if (room.colors && room.colors.length > 0) {
                        room.colors.forEach(color => {
                            const dot = document.createElement('div');
                            dot.className = 'w-7 h-7 rounded-lg border-2 border-white shadow-md hover:scale-110 transition-transform';
                            dot.style.backgroundColor = color;
                            colorsDiv.appendChild(dot);
                        });
                    }
                    card.appendChild(colorsDiv);
                    const sceneDiv = document.createElement('div');
                    sceneDiv.className = 'px-4 py-2 bg-gradient-to-r from-gray-50 to-gray-100 rounded-lg text-sm text-gray-600 font-medium min-w-[140px] text-right flex-shrink-0 border border-gray-200';
                    const sceneName = room.current_scene || room.current_scene_id || '—';
                    const avgBrightness = (typeof room.avg_brightness !== 'undefined' && room.avg_brightness !== null) ? `${room.avg_brightness}%` : '—';
                    if (sceneName && sceneName !== '—') {
                        sceneDiv.textContent = `${sceneName} · ${avgBrightness}`;
                    } else if (avgBrightness && avgBrightness !== '—' && avgBrightness !== '0%') {
                        sceneDiv.textContent = `No scene · ${avgBrightness}`;
                    } else {
                        sceneDiv.textContent = '—';
                        sceneDiv.className = 'px-4 py-2 bg-gray-50 rounded-lg text-sm text-gray-400 font-medium min-w-[140px] text-right flex-shrink-0 border border-gray-100';
                    }
                    card.appendChild(sceneDiv);
                    roomsContainer.appendChild(card);
                });
            } else {
                // Show error state, hide main content
                if (errorState) errorState.classList.remove('hidden');
                if (errorMsg) errorMsg.textContent = data.error || 'Unknown error';
                if (roomsContainer) roomsContainer.classList.add('hidden');
            }
        } catch (error) {
            console.error('Error loading rooms:', error);
            const errorState = document.getElementById('error-state');
            const errorMsg = document.getElementById('error-message');
            const roomsContainer = document.getElementById('rooms-container');
            if (errorState) errorState.classList.remove('hidden');
            if (errorMsg) errorMsg.textContent = 'Failed to load rooms data';
            if (roomsContainer) roomsContainer.classList.add('hidden');
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

    // Initial load
    loadRoomsData();

    // Auto-refresh every 3 seconds
    setInterval(() => {
        saveScrollPosition();
        loadRoomsData().then(restoreScrollPosition);
    }, 3000);
