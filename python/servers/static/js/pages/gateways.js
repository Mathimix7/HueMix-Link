let gateways = [];
let deleteGatewayId = null;
let deleteGatewayName = null;
let renameGatewayId = null;
let renameGatewayName = null;
let clearLedGatewayId = null;
let clearLedGatewayName = null;

// Load gateways on page load
window.addEventListener('DOMContentLoaded', function() {
    loadGateways();
});

function showSkeletonLoaders() {
    const container = document.getElementById('gateways-container');
    container.innerHTML = '';
    
    // Create 2 skeleton cards
    for (let i = 0; i < 2; i++) {
        const skeletonCard = document.createElement('div');
        skeletonCard.className = 'border border-gray-200 rounded-xl p-6';
        skeletonCard.innerHTML = `
            <div class="flex items-start justify-between mb-4">
                <div class="flex items-center space-x-3">
                    <div class="w-12 h-12 rounded-lg skeleton"></div>
                    <div class="flex-1">
                        <div class="h-5 w-32 skeleton mb-2"></div>
                        <div class="h-4 w-40 skeleton"></div>
                    </div>
                </div>
                <div class="h-6 w-20 skeleton rounded-full"></div>
            </div>
            
            <div class="grid grid-cols-2 gap-4 mb-4">
                <div>
                    <div class="h-4 w-20 skeleton mb-1"></div>
                    <div class="h-4 w-28 skeleton"></div>
                </div>
                <div>
                    <div class="h-4 w-16 skeleton mb-1"></div>
                    <div class="h-4 w-24 skeleton"></div>
                </div>
                <div>
                    <div class="h-4 w-20 skeleton mb-1"></div>
                    <div class="h-4 w-28 skeleton"></div>
                </div>
                <div>
                    <div class="h-4 w-16 skeleton mb-1"></div>
                    <div class="h-4 w-16 skeleton"></div>
                </div>
            </div>
            
            <div class="border-t border-gray-200 pt-4 space-y-4">
                <div>
                    <div class="h-4 w-32 skeleton mb-2"></div>
                    <div class="grid grid-cols-2 gap-3">
                        <div class="h-10 skeleton"></div>
                        <div class="h-10 skeleton"></div>
                    </div>
                    <div class="h-10 skeleton mt-2"></div>
                </div>
                
                <div>
                    <div class="h-4 w-24 skeleton mb-2"></div>
                    <div class="h-10 skeleton"></div>
                    <div class="h-10 skeleton mt-2"></div>
                </div>
            </div>
        `;
        container.appendChild(skeletonCard);
    }
}

function loadGateways() {
    showSkeletonLoaders();
    
    fetch('/gateways/api/gateways')
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                gateways = data.gateways;
                renderGateways();
            }
        })
        .catch(err => {
            console.error('Error loading gateways:', err);
            showToast('Error', 'Failed to load gateways', 'error');
        });
}

function renderGateways() {
    const container = document.getElementById('gateways-container');
    container.innerHTML = '';

    if (gateways.length === 0) {
        container.innerHTML = `
            <div class="col-span-2 text-center text-gray-500 py-12">
                <p class="text-lg">No gateways found</p>
                <p class="text-sm mt-1">Gateways will appear here automatically once detected.</p>
            </div>
        `;
        return;
    }

    const sortedGateways = [...gateways].sort((a, b) => {
        const aSerial = !!a.is_serial;
        const bSerial = !!b.is_serial;
        if (aSerial !== bSerial) return aSerial ? -1 : 1;
        return (a.name || '').localeCompare(b.name || '');
    });

    sortedGateways.forEach(gateway => {
        const gatewayCard = createGatewayCard(gateway);
        container.appendChild(gatewayCard);
    });
}

function formatTime24Hour(timeValue) {
    if (!timeValue) return '';
    
    // If already in HH:MM or HH:MM:SS format, validate and return
    if (typeof timeValue === 'string' && timeValue.includes(':')) {
        const parts = timeValue.split(':');
        const hours = parseInt(parts[0]);
        const minutes = parseInt(parts[1] || 0);
        
        if (!isNaN(hours) && !isNaN(minutes) && hours >= 0 && hours <= 23 && minutes >= 0 && minutes <= 59) {
            return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}`;
        }
    }
    
    // If it's just a number (hour), format as HH:00
    const numValue = parseInt(timeValue);
    if (!isNaN(numValue) && numValue >= 0 && numValue <= 23) {
        return `${String(numValue).padStart(2, '0')}:00`;
    }
    
    // Invalid format, return empty
    return '';
}

function createGatewayCard(gateway) {
    const card = document.createElement('div');
    card.className = 'border border-gray-200 rounded-xl p-6 hover:shadow-lg transition-shadow';
    
    const isOnline = gateway.status === 'online';
    const statusBadge = isOnline 
        ? '<span class="px-3 py-1 text-xs font-semibold rounded-full bg-green-100 text-green-800 inline-flex items-center"><i class="fas fa-check-circle mr-1 text-xs"></i> Online</span>'
        : '<span class="px-3 py-1 text-xs font-semibold rounded-full bg-red-100 text-red-800 inline-flex items-center"><i class="fas fa-circle mr-1 text-xs"></i> Offline</span>';
    
    // Format uptime if available
    const uptime = gateway.uptime ? formatUptime(gateway.uptime) : 'N/A';
    const lastUsed = gateway.last_used ? formatTimeAgo(gateway.last_used) : 'Never';
    const isSerial = !!gateway.is_serial;
    const serialPort = gateway.serial_port || (gateway.serial_endpoint || '').replace('serial://', '');
    
    // Format LED times to proper 24-hour HH:MM format
    const ledOnTime = formatTime24Hour(gateway.led_on_time);
    const ledOffTime = formatTime24Hour(gateway.led_off_time);
    
    card.innerHTML = `
        <div class="flex items-start justify-between mb-4">
            <div class="flex items-center space-x-3">
                <div class="w-12 h-12 rounded-lg bg-purple-100 flex items-center justify-center">
                    <i class="fas fa-server text-2xl text-purple-600"></i>
                </div>
                <div>
                    <h3 class="text-lg font-semibold text-gray-900">${gateway.name}</h3>
                    <p class="text-sm text-gray-500 font-mono">${gateway.mac_address}</p>
                </div>
            </div>
            ${statusBadge}
        </div>

            <div class="grid grid-cols-2 gap-4 mb-4 text-sm">
            <div>
                <p class="text-gray-500">${isSerial ? 'Serial' : 'IP Address'}</p>
                <p class="font-mono text-gray-900">${serialPort || gateway.ip_address}</p>
            </div>
            ${isOnline ? `
            <div>
                <p class="text-gray-500">Uptime</p>
                <p class="text-gray-900">${uptime}</p>
            </div>`: ''}
            <div>
                <p class="text-gray-500">Last Used</p>
                <p class="text-gray-900">${lastUsed}</p>
            </div>

        </div>
        ${isOnline ? `
        <div class="border-t border-gray-200 pt-4 space-y-4">
            <!-- LED Times -->
            <div>
                <div class="flex items-center justify-between mb-2">
                    <label class="block text-sm font-medium text-gray-700">
                        <i class="fas fa-lightbulb mr-1"></i>LED Schedule
                    </label>
                    ${ledOffTime && ledOnTime ? `
                    <button onclick="openClearLedModal('${gateway.id}', '${gateway.name.replace(/'/g, "\\'").replace(/"/g, '&quot;')}')" 
                            class="text-red-600 hover:text-red-800 transition-colors" title="Clear LED Schedule">
                        <i class="fas fa-trash"></i>
                    </button>
                    ` : ''}
                </div>
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="text-xs text-gray-500">Off Time</label>
                        <input type="number" id="led-off-${gateway.id}" value="${ledOffTime ? parseInt(ledOffTime.split(':')[0]) : ''}" 
                               placeholder="0-23" min="0" max="23"
                               class="mt-1 block w-full border border-gray-300 rounded-md shadow-sm py-2 px-3 focus:outline-none focus:ring-purple-500 focus:border-purple-500 text-sm">
                    </div>
                    <div>
                        <label class="text-xs text-gray-500">On Time</label>
                        <input type="number" id="led-on-${gateway.id}" value="${ledOnTime ? parseInt(ledOnTime.split(':')[0]) : ''}" 
                               placeholder="0-23" min="0" max="23"
                               class="mt-1 block w-full border border-gray-300 rounded-md shadow-sm py-2 px-3 focus:outline-none focus:ring-purple-500 focus:border-purple-500 text-sm">
                    </div>
                </div>
                <button onclick="updateLedTimes('${gateway.id}')" id="led-btn-${gateway.id}" 
                        class="mt-2 w-full px-3 py-2 bg-purple-600 text-white text-sm rounded-md hover:bg-purple-700 transition-colors inline-flex items-center justify-center">
                    <i class="fas fa-save mr-2"></i>Save LED Times
                    <span id="led-spinner-${gateway.id}" class="spinner ml-2" style="display: none;"></span>
                </button>
            </div>



            <!-- Firmware Upload 
            <!-- <div>
                <label class="block text-sm font-medium text-gray-700 mb-2">
                    <i class="fas fa-upload mr-1"></i>Update Firmware
                </label>
                <input type="file" id="firmware-${gateway.id}" accept=".bin" 
                       class="block w-full text-sm text-gray-500 file:mr-4 file:py-2 file:px-4 file:rounded-md file:border-0 file:text-sm file:font-semibold file:bg-purple-50 file:text-purple-700 hover:file:bg-purple-100">
                <button onclick="uploadFirmware('${gateway.id}', '${gateway.ip_address}')" id="upload-btn-${gateway.id}"
                        class="mt-2 w-full px-3 py-2 bg-purple-600 text-white text-sm rounded-md hover:bg-purple-700 transition-colors inline-flex items-center justify-center">
                    <i class="fas fa-cloud-upload-alt mr-2"></i>Upload Firmware
                    <span id="upload-spinner-${gateway.id}" class="spinner ml-2" style="display: none;"></span>
                </button>
            </div> -->
        </div>
        ` : '<div class="border-t border-gray-200 pt-4 text-center text-gray-500 text-sm">Gateway is offline - Configuration unavailable</div>'}
        
        <!-- Action Buttons -->
        <div class="border-t border-gray-200 pt-4 mt-4">
            <div class="grid grid-cols-2 gap-3">
                <button onclick="openRenameModal('${gateway.id}', '${gateway.name.replace(/'/g, "\\'").replace(/"/g, '&quot;')}')" 
                        class="px-3 py-2 bg-blue-600 text-white text-sm rounded-md hover:bg-blue-700 transition-colors inline-flex items-center justify-center">
                    <i class="fas fa-edit mr-2"></i>Rename
                </button>
                <button onclick="deleteGateway('${gateway.id}', '${gateway.name.replace(/'/g, "\\'").replace(/"/g, '&quot;')}')" 
                        class="px-3 py-2 bg-red-600 text-white text-sm rounded-md hover:bg-red-700 transition-colors inline-flex items-center justify-center">
                    <i class="fas fa-trash mr-2"></i>Delete
                </button>
            </div>
        </div>
    `;
    
    return card;
}

function formatUptime(seconds) {
    if (!seconds || seconds < 0) return 'N/A';
    
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    
    if (days > 0) {
        return `${days}d ${hours}h`;
    } else if (hours > 0) {
        return `${hours}h ${minutes}m`;
    } else {
        return `${minutes}m`;
    }
}

function formatTimeAgo(isoString) {
    const date = new Date(isoString);
    const now = new Date();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / 60000);
    
    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    
    const diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24) return `${diffHours}h ago`;
    
    const diffDays = Math.floor(diffHours / 24);
    return `${diffDays}d ago`;
}

function refreshGateways() {
    const refreshIcon = document.getElementById('refresh-icon');
    refreshIcon.classList.add('rotate-360');
    loadGateways();
    setTimeout(() => {
        refreshIcon.classList.remove('rotate-360');
    }, 500);
}

function setButtonLoading(buttonId, spinnerId, isLoading) {
    const button = document.getElementById(buttonId);
    const spinner = document.getElementById(spinnerId);
    
    if (isLoading) {
        button.disabled = true;
        button.classList.add('opacity-75', 'cursor-not-allowed');
        spinner.style.display = 'inline-block';
    } else {
        button.disabled = false;
        button.classList.remove('opacity-75', 'cursor-not-allowed');
        spinner.style.display = 'none';
    }
}

function updateLedTimes(gatewayId) {
    const ledOffInput = document.getElementById(`led-off-${gatewayId}`);
    const ledOnInput = document.getElementById(`led-on-${gatewayId}`);
    const ledOffTime = parseInt(ledOffInput.value);
    const ledOnTime = parseInt(ledOnInput.value);
    
    if (isNaN(ledOffTime) || isNaN(ledOnTime) || ledOffTime < 0 || ledOffTime > 23 || ledOnTime < 0 || ledOnTime > 23) {
        showToast('Validation Error', 'Please enter valid hours (0-23)', 'warning');
        return;
    }
    
    setButtonLoading(`led-btn-${gatewayId}`, `led-spinner-${gatewayId}`, true);
    
    fetch(`/gateways/api/gateways/${gatewayId}/led_times`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            led_off_time: ledOffTime,
            led_on_time: ledOnTime
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showToast('Success!', 'LED schedule updated successfully', 'success');
            // Reload gateways to show trash icon
            setTimeout(() => loadGateways(), 1000);
        } else {
            showToast('Error', data.error || 'Failed to update LED schedule', 'error');
        }
    })
    .catch(err => {
        console.error('Error updating LED times:', err);
        showToast('Error', 'Failed to update LED schedule', 'error');
    })
    .finally(() => {
        setTimeout(() => {
            setButtonLoading(`led-btn-${gatewayId}`, `led-spinner-${gatewayId}`, false);
        }, 1500);
    });
}

function openClearLedModal(gatewayId, gatewayName) {
    clearLedGatewayId = gatewayId;
    clearLedGatewayName = gatewayName;
    document.getElementById('clear-led-gateway-name').textContent = gatewayName;
    document.getElementById('clear-led-modal').classList.remove('hidden');
}

function closeClearLedModal() {
    document.getElementById('clear-led-modal').classList.add('hidden');
    clearLedGatewayId = null;
    clearLedGatewayName = null;
}

function confirmClearLedSchedule() {
    if (!clearLedGatewayId) return;
    
    fetch(`/gateways/api/gateways/${clearLedGatewayId}/led_times`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            led_off_time: null,
            led_on_time: null
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showToast('Success!', 'LED schedule cleared', 'success');
            closeClearLedModal();
            // Reload gateways to reflect changes
            setTimeout(() => loadGateways(), 1000);
        } else {
            showToast('Error', data.error || 'Failed to clear LED schedule', 'error');
        }
    })
    .catch(err => {
        console.error('Error clearing LED times:', err);
        showToast('Error', 'Failed to clear LED schedule', 'error');
    });
}

function updatePort(gatewayId) {
    const portInput = document.getElementById(`port-${gatewayId}`);
    const port = parseInt(portInput.value);
    
    if (isNaN(port) || port < 0 || port > 65535) {
        showToast('Validation Error', 'Invalid port number (0-65535)', 'warning');
        return;
    }
    
    setButtonLoading(`port-btn-${gatewayId}`, `port-spinner-${gatewayId}`, true);
    
    fetch(`/gateways/api/gateways/${gatewayId}/port`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ port: port })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showToast('Success!', `Port updated to ${port}`, 'success');
        } else {
            showToast('Error', data.error || 'Failed to update port', 'error');
        }
    })
    .catch(err => {
        console.error('Error updating port:', err);
        showToast('Error', 'Failed to update port', 'error');
    })
    .finally(() => {
        setTimeout(() => {
            setButtonLoading(`port-btn-${gatewayId}`, `port-spinner-${gatewayId}`, false);
        }, 1500);
    });
}

function uploadFirmware(gatewayId, ipAddress) {
    const fileInput = document.getElementById(`firmware-${gatewayId}`);
    const file = fileInput.files[0];
    
    if (!file) {
        showToast('Validation Error', 'Please select a firmware file', 'warning');
        return;
    }
    
    if (!file.name.toLowerCase().endsWith('.bin')) {
        showToast('Validation Error', 'Only .bin files are allowed', 'warning');
        return;
    }
    
    setButtonLoading(`upload-btn-${gatewayId}`, `upload-spinner-${gatewayId}`, true);
    showToast('Uploading...', 'Firmware update started', 'info');
    
    const formData = new FormData();
    formData.append('update', file);
    
    const otaURL = `http://${ipAddress}/update`;
    
    fetch(otaURL, {
        method: 'POST',
        body: formData
    })
    .then(response => {
        if (response.ok) {
            return response.text();
        } else {
            throw new Error('Upload failed');
        }
    })
    .then(responseText => {
        if (responseText === 'OK' || response.ok) {
            showToast('Success!', 'Firmware updated successfully', 'success');
            fileInput.value = '';
        } else {
            showToast('Error', 'Failed to update firmware', 'error');
        }
    })
    .catch(err => {
        console.error('Error uploading firmware:', err);
        showToast('Error', 'Failed to upload firmware. Check if device is reachable.', 'error');
    })
    .finally(() => {
        setTimeout(() => {
            setButtonLoading(`upload-btn-${gatewayId}`, `upload-spinner-${gatewayId}`, false);
        }, 2000);
    });
}

function deleteGateway(gatewayId, gatewayName) {
    deleteGatewayId = gatewayId;
    deleteGatewayName = gatewayName;
    document.getElementById('delete-gateway-name').textContent = gatewayName;
    document.getElementById('delete-modal').classList.remove('hidden');
}

function closeDeleteModal() {
    document.getElementById('delete-modal').classList.add('hidden');
    deleteGatewayId = null;
    deleteGatewayName = null;
}

function confirmDelete() {
    if (!deleteGatewayId) return;
    
    fetch(`/gateways/api/gateways/${deleteGatewayId}`, {
        method: 'DELETE'
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showToast('Gateway Deleted', `${deleteGatewayName} has been removed`, 'success');
            loadGateways();
            closeDeleteModal();
        } else {
            showToast('Error', data.error || 'Failed to delete gateway', 'error');
        }
    })
    .catch(err => {
        console.error('Error deleting gateway:', err);
        showToast('Error', 'Failed to delete gateway', 'error');
    });
}

// Rename modal functions
function openRenameModal(gatewayId, currentName) {
    renameGatewayId = gatewayId;
    renameGatewayName = currentName;
    
    document.getElementById('rename-input').value = currentName;
    document.getElementById('rename-modal').classList.remove('hidden');
    
    // Focus the input
    setTimeout(() => {
        document.getElementById('rename-input').focus();
        document.getElementById('rename-input').select();
    }, 100);
}

function closeRenameModal() {
    document.getElementById('rename-modal').classList.add('hidden');
    renameGatewayId = null;
    renameGatewayName = null;
}

async function confirmRename() {
    const newName = document.getElementById('rename-input').value.trim();
    
    if (!newName) {
        showToast('Invalid Name', 'Gateway name cannot be empty', 'error');
        return;
    }
    
    if (!renameGatewayId) {
        showToast('Error', 'Gateway ID not found', 'error');
        return;
    }
    
    try {
        const response = await fetch(`/gateways/api/gateways/${renameGatewayId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: newName })
        });
        
        const data = await response.json();
        
        if (data.success) {
            showToast('Success', `Gateway renamed to "${newName}"`, 'success');

            // Update the local array
            gateways = gateways.map(gw => {
                if (gw.id === renameGatewayId) {
                    return { ...gw, name: newName };
                }
                return gw;
            });
            
            // Update just the name in the DOM without re-rendering everything
            const gatewayCards = document.querySelectorAll('#gateways-container > div');
            gatewayCards.forEach(card => {
                const nameElement = card.querySelector('h3');
                if (nameElement && nameElement.textContent === renameGatewayName) {
                    nameElement.textContent = newName;
                }
            });
            closeRenameModal();
        } else {
            showToast('Error', 'Failed to rename gateway: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        console.error('Error renaming gateway:', error);
        showToast('Error', 'Failed to rename gateway', 'error');
    }
}
