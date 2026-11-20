window.addEventListener('DOMContentLoaded', function() {
    loadRooms();
    setInterval(() => {
        // Only refresh if no modal is open
        const scrollY = window.scrollY;
        loadRooms();
        setTimeout(() => { window.scrollTo(0, scrollY); }, 0);
        
        // If modal is open, refresh its content every 3s
        const modal = document.getElementById('room-details-modal');
        if (modal) {
            // Try to get the roomId from the modal's toggle button
            const toggleBtn = modal.querySelector('[onclick*="toggleLightInModal"]');
            if (toggleBtn) {
                const onclickAttr = toggleBtn.getAttribute('onclick');
                const match = onclickAttr.match(/'([^']+)',\s*'([^']+)'/);
                if (match && match[2]) {
                    const roomId = match[2];
                    fetch(`/api/rooms/${roomId}`)
                        .then(response => response.json())
                        .then(data => {
                            if (data.success) {
                                displayRoomDetails(data.room);
                            }
                        });
                }
            }
        }
    }, 3000);
});

function loadRooms() {
    fetch('/api/rooms')
        .then(response => response.json())
        .then(data => {
            document.getElementById('loading-state').classList.add('hidden');

            document.getElementById('error-state').classList.add('hidden');

            if (data.success && data.rooms && data.rooms.length > 0) {
                displayRooms(data.rooms);
            } else if (data.needs_config) {
                showError('Hue Bridge not configured');
            } else {
                showError('No rooms found');
            }
        })
        .catch(err => {
            console.error('Error loading rooms:', err);
            document.getElementById('loading-state').classList.add('hidden');
            showError('Connection error - Please check your bridge');
        });
}

function displayRooms(rooms) {
    const container = document.getElementById('rooms-list');
    container.innerHTML = '';
    
    rooms.forEach(room => {
        const roomCard = document.createElement('div');
        const isOn = room.is_on;
        const borderColor = isOn ? 'border-blue-400' : 'border-blue-100';
        const bgGradient = isOn ? 'from-blue-400 to-blue-600' : 'from-blue-300 to-blue-500';
        
        roomCard.className = `bg-white rounded-xl p-6 shadow-lg hover:shadow-xl transition-all duration-300 border-2 ${borderColor}`;
        roomCard.setAttribute('data-room-id', room.id);
        roomCard.setAttribute('data-room-state', isOn ? 'on' : 'off');
        
        roomCard.innerHTML = `
            <div class="flex items-start justify-between mb-4">
                <div class="w-12 h-12 rounded-lg bg-gradient-to-br ${bgGradient} flex items-center justify-center shadow-lg room-icon">
                    <i class="fas fa-door-open text-xl text-white"></i>
                </div>
                <span class="px-2 py-1 text-xs font-semibold rounded-full ${isOn ? 'bg-blue-100 text-blue-800' : 'bg-gray-100 text-gray-800'} room-status-badge">
                    ${isOn ? 'ON' : 'OFF'}
                </span>
            </div>
            <h3 class="text-lg font-bold text-gray-900 mb-2">${room.name}</h3>
            <p class="text-sm text-gray-500 mb-4">
                <i class="fas fa-lightbulb mr-1"></i>${room.light_count} light${room.light_count !== 1 ? 's' : ''}
            </p>
            <div class="flex gap-2">
                <button onclick="toggleRoom('${room.id}')" class="flex-1 px-3 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 transition-colors">
                    <i class="fas fa-power-off mr-1"></i>Toggle
                </button>
                <button onclick="viewRoomDetails('${room.id}', '${room.name.replace(/'/g, "\\'")}')" class="flex-1 px-3 py-2 bg-gray-600 text-white text-sm rounded-lg hover:bg-gray-700 transition-colors">
                    <i class="fas fa-info-circle mr-1"></i>Details
                </button>
            </div>
        `;
        
        container.appendChild(roomCard);
    });
    
    document.getElementById('rooms-container').classList.remove('hidden');
}

function toggleRoom(roomId) {
    // Find the room card
    const roomCard = document.querySelector(`[data-room-id="${roomId}"]`);
    if (!roomCard) return;
    
    const currentState = roomCard.getAttribute('data-room-state');
    const newState = currentState === 'on' ? 'off' : 'on';
    
    fetch(`/api/rooms/${roomId}/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            // Update UI immediately
            roomCard.setAttribute('data-room-state', newState);
            
            const statusBadge = roomCard.querySelector('.room-status-badge');
            const iconDiv = roomCard.querySelector('.room-icon');
            
            if (newState === 'on') {
                // Update to ON state
                roomCard.classList.remove('border-blue-100');
                roomCard.classList.add('border-blue-400');
                
                iconDiv.classList.remove('from-blue-300', 'to-blue-500');
                iconDiv.classList.add('from-blue-400', 'to-blue-600');
                
                statusBadge.classList.remove('bg-gray-100', 'text-gray-800');
                statusBadge.classList.add('bg-blue-100', 'text-blue-800');
                statusBadge.textContent = 'ON';
            } else {
                // Update to OFF state
                roomCard.classList.remove('border-blue-400');
                roomCard.classList.add('border-blue-100');
                
                iconDiv.classList.remove('from-blue-400', 'to-blue-600');
                iconDiv.classList.add('from-blue-300', 'to-blue-500');
                
                statusBadge.classList.remove('bg-blue-100', 'text-blue-800');
                statusBadge.classList.add('bg-gray-100', 'text-gray-800');
                statusBadge.textContent = 'OFF';
            }
        } else {
            showToast('Error', 'Failed to toggle room: ' + (data.error || 'Unknown error'), 'error');
        }
    })
    .catch(err => {
        console.error('Error toggling room:', err);
        showToast('Error', 'Failed to toggle room', 'error');
    });
}

function viewRoomDetails(roomId, roomName) {
    // Create modal for room details
    const modal = document.createElement('div');
    modal.className = 'fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4';
    modal.id = 'room-details-modal';
    
    modal.innerHTML = `
        <div class="bg-white rounded-2xl shadow-2xl max-w-2xl w-full max-h-[80vh] overflow-y-auto">
            <div class="p-6 border-b border-gray-200">
                <div class="flex items-center justify-between">
                    <h2 class="text-2xl font-bold text-gray-900">
                        <i class="fas fa-door-open text-blue-600 mr-2"></i>${roomName}
                    </h2>
                    <button onclick="closeRoomDetails()" class="text-gray-500 hover:text-gray-700">
                        <i class="fas fa-times text-2xl"></i>
                    </button>
                </div>
            </div>
            <div id="room-details-content" class="p-6">
                <div class="flex items-center justify-center py-12">
                    <div class="spinner"></div>
                </div>
            </div>
        </div>
    `;
    
    document.body.appendChild(modal);
    
    // Load room details
    fetch(`/api/rooms/${roomId}`)
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                displayRoomDetails(data.room);
            } else {
                document.getElementById('room-details-content').innerHTML = `
                    <p class="text-red-600 text-center">${data.error || 'Failed to load room details'}</p>
                `;
            }
        })
        .catch(err => {
            console.error('Error loading room details:', err);
            document.getElementById('room-details-content').innerHTML = `
                <p class="text-red-600 text-center">Failed to load room details</p>
            `;
        });
}

function displayRoomDetails(room) {
    const content = document.getElementById('room-details-content');
    
    let html = `
        <div class="mb-6">
            <div class="flex items-center justify-between mb-4">
                <h3 class="text-lg font-semibold text-gray-900">
                    <i class="fas fa-lightbulb mr-2 text-amber-600"></i>Lights (${room.lights.length})
                </h3>
            </div>
            ${room.lights.length > 0 ? `
                <div class="space-y-2">
                    ${room.lights.map(light => `
                        <div class="flex items-center justify-between p-3 bg-gray-50 rounded-lg">
                            <div class="flex items-center">
                                <div class="w-8 h-8 rounded-lg bg-gradient-to-br ${light.on ? 'from-amber-400 to-amber-600' : 'from-gray-400 to-gray-600'} flex items-center justify-center mr-3 transition-all duration-300">
                                    <i class="fas fa-lightbulb text-sm text-white"></i>
                                </div>
                                <div>
                                    <p class="font-medium text-gray-900">${light.name}</p>
                                    ${light.brightness !== null && light.brightness !== undefined ? `
                                        <p class="text-xs text-gray-500">Brightness: ${Math.round(light.brightness)}%</p>
                                    ` : ''}
                                </div>
                            </div>
                            <div class="flex items-center gap-2">
                                <span class="px-2 py-1 text-xs font-semibold rounded-full ${light.on ? 'bg-amber-100 text-amber-800' : 'bg-gray-100 text-gray-800'}">
                                    ${light.on ? 'ON' : 'OFF'}
                                </span>
                                <button onclick="toggleLightInModal('${light.id}', '${room.id}')" class="px-3 py-1 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 transition-colors">
                                    Toggle
                                </button>
                            </div>
                        </div>
                    `).join('')}
                </div>
            ` : '<p class="text-gray-500 text-center py-4">No lights in this room</p>'}
        </div>
        
        <div>
            <div class="flex items-center justify-between mb-4">
                <h3 class="text-lg font-semibold text-gray-900">
                    <i class="fas fa-palette mr-2 text-pink-600"></i>Scenes (${room.scenes.length})
                </h3>
            </div>
            ${room.scenes.length > 0 ? `
                <div class="grid grid-cols-2 gap-2">
                    ${room.scenes.map(scene => `
                        <button onclick="activateSceneInModal('${scene.id}', '${scene.name.replace(/'/g, "\\'")}')" class="p-3 bg-gradient-to-br from-pink-50 to-white border-2 border-pink-100 hover:border-pink-400 rounded-lg text-left transition-all hover:shadow-md">
                            <div class="flex items-center">
                                <div class="w-8 h-8 rounded-lg bg-gradient-to-br from-pink-400 to-pink-600 flex items-center justify-center mr-2">
                                    <i class="fas fa-palette text-sm text-white"></i>
                                </div>
                                <p class="font-medium text-gray-900 text-sm">${scene.name}</p>
                            </div>
                        </button>
                    `).join('')}
                </div>
            ` : '<p class="text-gray-500 text-center py-4">No scenes in this room</p>'}
        </div>
    `;
    
    content.innerHTML = html;
}

function closeRoomDetails() {
    const modal = document.getElementById('room-details-modal');
    if (modal) {
        modal.remove();
    }
}

function toggleLightInModal(lightId, roomId) {
    fetch(`/api/lights/${lightId}/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            // Reload room details
            fetch(`/api/rooms/${roomId}`)
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        displayRoomDetails(data.room);
                    }
                });
        } else {
                showToast('Error', 'Failed to toggle light: ' + (data.error || 'Unknown error'), 'error');
        }
    })
    .catch(err => {
        console.error('Error toggling light:', err);
            showToast('Error', 'Failed to toggle light', 'error');
    });
}

function activateSceneInModal(sceneId, sceneName) {
    // Get the current room ID from the modal
    const modal = document.getElementById('room-details-modal');
    const roomDetailsContent = document.getElementById('room-details-content');
    let currentRoomId = null;
    
    // Extract room ID from the modal context (look for button with room ID in onclick)
    const toggleButtons = modal?.querySelectorAll('[onclick*="toggleLightInModal"]');
    if (toggleButtons && toggleButtons.length > 0) {
        const onclickAttr = toggleButtons[0].getAttribute('onclick');
        const match = onclickAttr.match(/'([^']+)',\s*'([^']+)'/);
        if (match && match[2]) {
            currentRoomId = match[2];
        }
    }
    
    fetch(`/api/scenes/${sceneId}/activate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showToast('Scene Activated', `Scene "${sceneName}" has been activated`, 'success');
            
            if (currentRoomId) {
                setTimeout(() => {
                    fetch(`/api/rooms/${currentRoomId}`)
                        .then(response => response.json())
                        .then(data => {
                            if (data.success) {
                                displayRoomDetails(data.room);
                            }
                        })
                        .catch(err => {
                            console.error('Error reloading room details:', err);
                        });
                }, 800);
            }
        } else {
            showToast('Error', 'Failed to activate scene: ' + (data.error || 'Unknown error'), 'error');
        }
    })
    .catch(err => {
        console.error('Error activating scene:', err);
        showToast('Error', 'Failed to activate scene', 'error');
    });
}

function showError(message) {
    document.getElementById('error-message').textContent = message;
    document.getElementById('error-state').classList.remove('hidden');
    const roomsContainer = document.getElementById('rooms-container');
    if (roomsContainer) roomsContainer.classList.add('hidden');
    const loadingState = document.getElementById('loading-state');
    if (loadingState) loadingState.classList.add('hidden');

    const modal = document.getElementById('room-details-modal');
    if (modal) {
        modal.remove();
    }
}
