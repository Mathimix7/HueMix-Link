let statusTimer = null;
let deleteDeviceId = null;
let deleteDeviceMac = null;
let deleteDeviceName = null;
let deleteDeviceType = null;
let renameDeviceId = null;
let renameDeviceType = null;
let renameDeviceName = null;
let isInitialLoad = true;

// Initialize page
document.addEventListener('DOMContentLoaded', () => {
    updatePairingStatus();
    loadPairedDevices();
    
    // Set up event listeners
    document.getElementById('start-long-range-btn').addEventListener('click', startLongRangePairing);
    document.getElementById('stop-long-range-btn').addEventListener('click', stopPairing);
    
    // Poll for status updates every second
    statusTimer = setInterval(() => {
        updatePairingStatus();
        loadPairedDevices();
    }, 1000);
});

// Start long range pairing mode (fixed to 60 seconds)
async function startLongRangePairing() {
    const type = document.getElementById('pairing-type').value;
    const types = type === 'all' ? null : [type];
    
    try {
        const response = await fetch('/api/pairing/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ duration: 60, types })
        });
        
        const data = await response.json();
        
        if (data.success) {
            updatePairingStatus();
        } else {
            showToast('Error', 'Failed to start long range pairing: ' + data.error, 'error');
        }
    } catch (error) {
        console.error('Error starting long range pairing:', error);
        showToast('Error', 'Failed to start long range pairing mode', 'error');
    }
}

// Stop pairing mode
async function stopPairing() {
    try {
        const response = await fetch('/api/pairing/stop', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        
        const data = await response.json();
        
        if (data.success) {
            updatePairingStatus();
        } else {
            showToast('Error', 'Failed to stop pairing: ' + data.error, 'error');
        }
    } catch (error) {
        console.error('Error stopping pairing:', error);
        showToast('Error', 'Failed to stop pairing mode', 'error');
    }
}

// Update pairing status display
async function updatePairingStatus() {
    try {
        const response = await fetch('/api/pairing/status');
        const data = await response.json();
        
        if (!data.success) return;
        
        const badge = document.getElementById('pairing-status-badge');
        const activeControls = document.getElementById('long-range-controls-active');
        const inactiveControls = document.getElementById('long-range-controls-inactive');
        const shortRangeInfo = document.getElementById('short-range-info');
        const longRangeInfo = document.getElementById('long-range-info');
        const timeRemaining = document.getElementById('time-remaining');
        const lookingFor = document.getElementById('looking-for');
        
        if (data.active) {
            // Long range mode active - replace short range with long range info
            badge.innerHTML = '<span class="bg-blue-500 text-white px-3 py-1 rounded-full animate-pulse">Long Range Active</span>';
            activeControls.classList.remove('hidden');
            inactiveControls.classList.add('hidden');
            shortRangeInfo.classList.add('hidden');
            longRangeInfo.classList.remove('hidden');
            
            // Update timer
            const seconds = data.remaining_seconds || 0;
            const minutes = Math.floor(seconds / 60);
            const secs = seconds % 60;
            timeRemaining.textContent = `${minutes}:${secs.toString().padStart(2, '0')}`;
            
            // Update looking for
            const typeNames = {
                'all': 'All Devices',
                'button': 'Buttons/Remotes Only',
                'light': 'Lightstrips Only',
                'motion': 'Motion Sensors Only',
                'door': 'Door Sensors Only'
            };
            const types = data.allowed_types || ['all'];
            lookingFor.textContent = types.length === 1 ? (typeNames[types[0]] || types[0]) : 'All Devices';
            
        } else {
            // Short range only state
            badge.innerHTML = '<span class="bg-green-100 text-green-700 px-3 py-1 rounded-full">Short Range Active</span>';
            activeControls.classList.add('hidden');
            inactiveControls.classList.remove('hidden');
            shortRangeInfo.classList.remove('hidden');
            longRangeInfo.classList.add('hidden');
        }
        
    } catch (error) {
        console.error('Error updating pairing status:', error);
    }
}

// Show skeleton loaders for paired devices
function showPairedDevicesSkeleton() {
    const devicesList = document.getElementById('devices-list');
    const noDevices = document.getElementById('no-devices');
    
    noDevices.classList.add('hidden');
    devicesList.classList.remove('hidden');
    
    // Create 3 skeleton cards
    const skeletonCards = Array(3).fill(0).map(() => `
        <div class="border border-gray-200 rounded-lg p-4">
            <div class="flex items-center justify-between">
                <div class="flex items-center space-x-3 flex-1">
                    <div class="w-8 h-8 skeleton"></div>
                    <div class="flex-1">
                        <div class="h-5 w-32 skeleton mb-2"></div>
                        <div class="h-4 w-40 skeleton mb-2"></div>
                        <div class="flex items-center gap-2">
                            <div class="h-4 w-16 skeleton"></div>
                            <div class="h-5 w-20 skeleton rounded-full"></div>
                        </div>
                    </div>
                </div>
                <div class="flex items-center gap-3">
                    <div class="text-right mr-2">
                        <div class="h-4 w-24 skeleton mb-1"></div>
                        <div class="h-3 w-20 skeleton"></div>
                    </div>
                    <div class="flex gap-2">
                        <div class="w-8 h-8 skeleton rounded-lg"></div>
                        <div class="w-8 h-8 skeleton rounded-lg"></div>
                        <div class="w-8 h-8 skeleton rounded-lg"></div>
                    </div>
                </div>
            </div>
        </div>
    `).join('');
    
    devicesList.innerHTML = skeletonCards;
}

// Load recently paired devices
async function loadPairedDevices() {
    if (isInitialLoad) {
        showPairedDevicesSkeleton();
        isInitialLoad = false;
    }
    
    try {
        const response = await fetch('/api/pairing/devices');
        const data = await response.json();
        
        if (!data.success) return;
        
        const devicesList = document.getElementById('devices-list');
        const noDevices = document.getElementById('no-devices');
        
        if (data.devices && data.devices.length > 0) {
            noDevices.classList.add('hidden');
            devicesList.classList.remove('hidden');
            
            // Build device list HTML
            devicesList.innerHTML = data.devices.map(device => {
                const typeIcons = {
                    1: '🔌', // Gateway
                    2: '🔘', // Button
                    3: '💡', // Light
                    4: '🎛️',  // Remote
                    5: '📡',  // Motion Sensor
                    6: '🚪'  // Door Sensor
                };
                const icon = typeIcons[device.type] || '❓';
                
                // Format date
                const pairedDate = new Date(device.paired_date);
                const dateStr = pairedDate.toLocaleDateString();
                const timeStr = pairedDate.toLocaleTimeString();
                
                // Mode badge
                const modeBadges = {
                    'short_range': '<span class="px-2 py-1 text-xs bg-green-100 text-green-700 rounded-full">Auto-Paired</span>',
                    'long_range': '<span class="px-2 py-1 text-xs bg-blue-100 text-blue-700 rounded-full">Long Range</span>',
                    'wifi': '<span class="px-2 py-1 text-xs bg-purple-100 text-purple-700 rounded-full">WiFi</span>'
                };
                const modeBadge = modeBadges[device.mode] || '<span class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded-full">Unknown</span>';
                
                // Config page URLs
                const configUrls = {
                    1: '/gateways',  // Gateway
                    2: '/buttons',   // Button
                    3: '/lightstrips', // Light
                    4: '/buttons',   // Remote
                    5: '/motion-sensors', // Motion Sensor
                    6: '/door-sensors' // Door Sensor
                };
                const configUrl = configUrls[device.type] || '#';
                
                return `
                    <div class="border border-gray-200 rounded-lg p-4 hover:border-green-300 transition">
                        <div class="flex items-center justify-between">
                            <div class="flex items-center space-x-3 flex-1">
                                <div class="text-2xl">${icon}</div>
                                <div class="flex-1">
                                    <div class="font-semibold text-gray-800">${device.name}</div>
                                    <div class="font-mono text-xs text-gray-500">${device.mac}</div>
                                    <div class="flex items-center gap-2 mt-1">
                                        <span class="text-xs text-gray-500">${device.type_name}</span>
                                        ${modeBadge}
                                    </div>
                                </div>
                            </div>
                            <div class="flex items-center gap-3">
                                <div class="text-right mr-2">
                                    <div class="text-sm font-semibold text-gray-700">${dateStr}</div>
                                    <div class="text-xs text-gray-500">${timeStr}</div>
                                </div>
                                <div class="flex gap-2">
                                    <button onclick="openRenameModal('${device.id || ''}', '${device.name.replace(/'/g, "\\'").replace(/"/g, '&quot;')}', ${device.type})" 
                                            class="p-2 text-green-600 hover:bg-green-50 rounded-lg transition"
                                            title="Rename device">
                                        <i class="fas fa-edit"></i>
                                    </button>
                                    <a href="${configUrl}" 
                                       class="p-2 text-blue-600 hover:bg-blue-50 rounded-lg transition"
                                       title="Configure device">
                                        <i class="fas fa-cog"></i>
                                    </a>
                                    <button onclick="deletePairedDevice('${device.id || ''}', '${device.mac}', '${device.name.replace(/'/g, "\\'").replace(/"/g, '&quot;')}', ${device.type})" 
                                            class="p-2 text-red-600 hover:bg-red-50 rounded-lg transition"
                                            title="Delete device">
                                        <i class="fas fa-trash"></i>
                                    </button>
                                </div>
                            </div>
                        </div>
                    </div>
                `;
            }).join('');
            
        } else {
            noDevices.classList.remove('hidden');
            devicesList.classList.add('hidden');
        }
        
    } catch (error) {
        console.error('Error loading paired devices:', error);
    }
}

// Rename device modal functions
function openRenameModal(deviceId, name, type) {
    renameDeviceId = deviceId;
    renameDeviceType = type;
    renameDeviceName = name;
    
    document.getElementById('rename-input').value = name;
    document.getElementById('rename-modal').classList.remove('hidden');
    
    // Focus the input
    setTimeout(() => {
        document.getElementById('rename-input').focus();
        document.getElementById('rename-input').select();
    }, 100);
}

function closeRenameModal() {
    document.getElementById('rename-modal').classList.add('hidden');
    renameDeviceId = null;
    renameDeviceType = null;
    renameDeviceName = null;
}

async function confirmRename() {
    const newName = document.getElementById('rename-input').value.trim();
    
    if (!newName) {
        showToast('Invalid Name', 'Device name cannot be empty', 'error');
        return;
    }
    
    if (!renameDeviceId) {
        showToast('Error', 'Device ID not found', 'error');
        return;
    }
    
    try {
        // Determine the endpoint based on device type
        let endpoint = '';
        let method = 'POST';
        let body = {};
        
        if (renameDeviceType === 1) { // Gateway
            endpoint = `/gateways/api/gateways/${renameDeviceId}`;
            method = 'PUT';
            body = { name: newName };
        } else if (renameDeviceType === 2) { // Button
            endpoint = `/buttons/api/devices/${renameDeviceId}/rename`;
            method = 'POST';
            body = { name: newName };
        } else if (renameDeviceType === 3) { // Lightstrip
            endpoint = `/lightstrips/api/lightstrips/${renameDeviceId}`;
            method = 'PUT';
            body = { name: newName };
        } else if (renameDeviceType === 4) { // Remote
            endpoint = `/buttons/api/devices/${renameDeviceId}/rename`;
            method = 'POST';
            body = { name: newName };
        } else if (renameDeviceType === 5) { // Motion Sensor
            endpoint = `/motion-sensors/api/devices/${renameDeviceId}/rename`;
            method = 'POST';
            body = { name: newName };
        } else if (renameDeviceType === 6) { // Door Sensor
            endpoint = `/door-sensors/api/devices/${renameDeviceId}/rename`;
            method = 'POST';
            body = { name: newName };
        } else {
            showToast('Error', `Unsupported device type: ${renameDeviceType}`, 'error');
            return;
        }
        
        const response = await fetch(endpoint, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        
        const data = await response.json();
        
        if (data.success) {
            showToast('Success', `Device renamed to "${newName}"`, 'success');
            closeRenameModal();
            loadPairedDevices();
        } else {
            showToast('Error', 'Failed to rename device: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (error) {
        console.error('Error renaming device:', error);
        showToast('Error', 'Failed to rename device', 'error');
    }
}

// Delete device from system
function deletePairedDevice(deviceId, mac, name, type) {
    deleteDeviceId = deviceId;
    deleteDeviceMac = mac;
    deleteDeviceName = name;
    deleteDeviceType = type;
    
    document.getElementById('delete-device-name').textContent = name;
    document.getElementById('delete-modal').classList.remove('hidden');
}

function closeDeleteModal() {
    document.getElementById('delete-modal').classList.add('hidden');
    deleteDeviceId = null;
    deleteDeviceMac = null;
    deleteDeviceName = null;
    deleteDeviceType = null;
}

async function confirmDelete() {
    if (!deleteDeviceId) {
        showToast('Error', 'Device ID not found', 'error');
        closeDeleteModal();
        return;
    }
    
    try {
        // Determine the endpoint based on device type
        let endpoint = '';
        if (deleteDeviceType === 1) { // Gateway
            endpoint = `/gateways/api/gateways/${deleteDeviceId}`;
        } else if (deleteDeviceType === 2) { // Button
            endpoint = `/buttons/api/devices/${deleteDeviceId}`;
        } else if (deleteDeviceType === 3) { // Lightstrip
            endpoint = `/lightstrips/api/lightstrips/${deleteDeviceId}`;
        } else if (deleteDeviceType === 4) { // Remote
            endpoint = `/buttons/api/devices/${deleteDeviceId}`;
        } else if (deleteDeviceType === 5) { // Motion Sensor
            endpoint = `/motion-sensors/api/devices/${deleteDeviceId}`;
        } else if (deleteDeviceType === 6) { // Door Sensor
            endpoint = `/door-sensors/api/devices/${deleteDeviceId}`;
        } else {
            showToast('Error', `Unsupported device type: ${deleteDeviceType}`, 'error');
            closeDeleteModal();
            return;
        }
        
        const response = await fetch(endpoint, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' }
        });
        
        const data = await response.json();
        
        if (data.success) {
            // Also remove from pairing history
            if (deleteDeviceMac) {
                try {
                    await fetch(`/api/pairing/devices/${deleteDeviceMac}`, {
                        method: 'DELETE'
                    });
                } catch (err) {
                    console.error('Error removing from pairing history:', err);
                    // Don't show error to user, main deletion succeeded
                }
            }
            
            showToast('Success', `${deleteDeviceName} deleted successfully`, 'success');
            closeDeleteModal();
            // Reload the list
            loadPairedDevices();
        } else {
            showToast('Error', 'Failed to delete device: ' + (data.error || 'Unknown error'), 'error');
            closeDeleteModal();
        }
    } catch (error) {
        console.error('Error deleting device:', error);
        showToast('Error', 'Failed to delete device', 'error');
        closeDeleteModal();
    }
}

// Clean up timers on page unload
window.addEventListener('beforeunload', () => {
    if (statusTimer) clearInterval(statusTimer);
});
