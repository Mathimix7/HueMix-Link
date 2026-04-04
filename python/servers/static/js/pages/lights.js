window.addEventListener('DOMContentLoaded', function() {
    loadLights();
    setInterval(() => {
        // Only refresh if no modal is open (if you add modals in the future, adjust this check)
        const scrollY = window.scrollY;
        loadLights();
        setTimeout(() => { window.scrollTo(0, scrollY); }, 0);
    }, 3000);
});

function loadLights() {
    fetch('/api/lights/all')
        .then(response => response.json())
        .then(data => {
            document.getElementById('loading-state').classList.add('hidden');

            document.getElementById('error-state').classList.add('hidden');

            if (data.success && data.lights && data.lights.length > 0) {
                displayLights(data.lights);
            } else if (data.needs_config) {
                showError('Hue Bridge not configured');
            } else {
                showError('No lights found');
            }
        })
        .catch(err => {
            console.error('Error loading lights:', err);
            document.getElementById('loading-state').classList.add('hidden');
            showError('Connection error - Please check your bridge');
        });
}

function displayLights(lights) {
    const container = document.getElementById('lights-list');
    container.innerHTML = '';
    
    // Group lights by room
    const lightsByRoom = {};
    lights.forEach(light => {
        const roomName = light.room_name || 'Unassigned';
        if (!lightsByRoom[roomName]) {
            lightsByRoom[roomName] = [];
        }
        lightsByRoom[roomName].push(light);
    });
    
    // Sort room names
    const sortedRoomNames = Object.keys(lightsByRoom).sort();
    
    // Create sections for each room
    sortedRoomNames.forEach(roomName => {
        const roomSection = document.createElement('div');
        roomSection.className = 'col-span-full mb-6';
        
        const roomLights = lightsByRoom[roomName];
        const roomHeader = document.createElement('div');
        roomHeader.className = 'flex items-center mb-4';
        roomHeader.innerHTML = `
            <h2 class="text-xl font-bold text-gray-900 mr-3">
                <i class="fas fa-door-open text-amber-600 mr-2"></i>${roomName}
            </h2>
            <span class="px-3 py-1 text-xs font-semibold rounded-full bg-amber-100 text-amber-800">
                ${roomLights.length} light${roomLights.length !== 1 ? 's' : ''}
            </span>
        `;
        roomSection.appendChild(roomHeader);
        
        const lightsGrid = document.createElement('div');
        lightsGrid.className = 'grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4';
        
        roomLights.forEach(light => {
            const lightCard = document.createElement('div');
            const isOn = light.on;
            const borderColor = isOn ? 'border-amber-400' : 'border-gray-200';
            const bgGradient = isOn ? 'from-amber-400 to-amber-600' : 'from-gray-400 to-gray-600';

            lightCard.className = `bg-white rounded-xl p-6 shadow-lg hover:shadow-xl transition-all duration-300 border-2 ${borderColor}`;

            lightCard.innerHTML = `
                <div class="flex items-start justify-between mb-4">
                    <div class="w-12 h-12 rounded-lg bg-gradient-to-br ${bgGradient} flex items-center justify-center shadow-lg transition-all duration-300 light-icon" data-light-id="${light.id}">
                        <i class="fas fa-lightbulb text-xl text-white"></i>
                    </div>
                    <span class="px-2 py-1 text-xs font-semibold rounded-full ${isOn ? 'bg-amber-100 text-amber-800' : 'bg-gray-100 text-gray-800'} transition-all duration-300 light-status-badge" data-light-id="${light.id}">
                        ${isOn ? 'ON' : 'OFF'}
                    </span>
                </div>
                <h3 class="text-lg font-bold text-gray-900 mb-1">${light.name}</h3>
                ${light.brightness !== null && light.brightness !== undefined ? `
                    <p class="text-sm text-gray-500 mb-3">
                        <i class="fas fa-sun mr-1"></i>${Math.round(light.brightness)}% brightness
                    </p>
                ` : '<div class="mb-3"></div>'}
                <button onclick="toggleLight('${light.id}')" class="w-full px-3 py-2 bg-amber-600 text-white text-sm rounded-lg hover:bg-amber-700 transition-colors">
                    <i class="fas fa-power-off mr-1"></i>Toggle
                </button>
            `;

            lightsGrid.appendChild(lightCard);
        });
        
        roomSection.appendChild(lightsGrid);
        container.appendChild(roomSection);
    });
    
    document.getElementById('lights-container').classList.remove('hidden');
}

function toggleLight(lightId) {
    fetch(`/api/lights/${lightId}/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            // Find the light card elements for feedback only after success
            const iconDiv = document.querySelector(`.light-icon[data-light-id="${lightId}"]`);
            const statusBadge = document.querySelector(`.light-status-badge[data-light-id="${lightId}"]`);
            let cardDiv = iconDiv ? iconDiv.closest('.rounded-xl') : null;
            let isCurrentlyOn = false;
            if (iconDiv && statusBadge && cardDiv) {
                isCurrentlyOn = iconDiv.classList.contains('from-amber-400');
                if (isCurrentlyOn) {
                    // Animate to OFF
                    iconDiv.classList.remove('from-amber-400', 'to-amber-600');
                    iconDiv.classList.add('from-gray-400', 'to-gray-600');
                    statusBadge.classList.remove('bg-amber-100', 'text-amber-800');
                    statusBadge.classList.add('bg-gray-100', 'text-gray-800');
                    statusBadge.textContent = 'OFF';
                    cardDiv.classList.remove('border-amber-400');
                    cardDiv.classList.add('border-gray-200');
                } else {
                    // Animate to ON
                    iconDiv.classList.remove('from-gray-400', 'to-gray-600');
                    iconDiv.classList.add('from-amber-400', 'to-amber-600');
                    statusBadge.classList.remove('bg-gray-100', 'text-gray-800');
                    statusBadge.classList.add('bg-amber-100', 'text-amber-800');
                    statusBadge.textContent = 'ON';
                    cardDiv.classList.remove('border-gray-200');
                    cardDiv.classList.add('border-amber-400');
                }
            }
            // After a short delay, reload all lights to sync brightness
            setTimeout(() => { loadLights(); }, 800);
        } else {
            showToast('Error', 'Failed to toggle light: ' + (data.error || 'Unknown error'), 'error');
        }
    })
    .catch(err => {
        console.error('Error toggling light:', err);
        showToast('Error', 'Failed to toggle light', 'error');
    });
}

function showError(message) {
    document.getElementById('error-message').textContent = message;
    document.getElementById('error-state').classList.remove('hidden');
    const lightsContainer = document.getElementById('lights-container');
    if (lightsContainer) lightsContainer.classList.add('hidden');
    const loadingState = document.getElementById('loading-state');
    if (loadingState) loadingState.classList.add('hidden');
}
