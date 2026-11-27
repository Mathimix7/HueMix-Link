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
    
    // Support NFC tap-to-pair via URL fragment: /pairing#pin=123456 (optional &type=button&auto=0)
    // Using the hash fragment keeps the 6-digit PIN out of HTTP server logs and Referer headers.
    handleNfcPairingHash();
    window.addEventListener('hashchange', handleNfcPairingHash);
    
    // Poll for status updates every second
    statusTimer = setInterval(() => {
        updatePairingStatus();
        loadPairedDevices();
    }, 1000);
});

// Parse #pin=XXXXXX (& optional &type=... &auto=0/1) from URL fragment and auto-start pairing
async function handleNfcPairingHash() {
    const rawHash = window.location.hash ? window.location.hash.slice(1) : '';
    if (!rawHash) return;

    const params = new URLSearchParams(rawHash);
    const pin = (params.get('pin') || '').trim();
    const type = (params.get('type') || '').trim().toLowerCase();
    const auto = params.get('auto') !== '0';

    if (!pin) return;

    // Immediately remove PIN from the URL bar / browser history so refreshes don't re-trigger
    try {
        history.replaceState(null, '', window.location.pathname + window.location.search);
    } catch (_) {}

    if (!/^\d{6}$/.test(pin)) {
        showToast('Error', 'Invalid NFC pairing PIN (must be 6 digits)', 'error');
        return;
    }

    const typeSelect = document.getElementById('pairing-type');
    if (type && typeSelect) {
        const validOption = Array.from(typeSelect.options).some(opt => opt.value === type);
        if (validOption) {
            typeSelect.value = type;
        }
    }

    const pinInput = document.getElementById('pairing-pin');
    if (pinInput) {
        pinInput.value = pin;
    }

    if (auto) {
        const started = await startLongRangePairing();
        if (started) {
            showToast('NFC Pairing Active', '60-second Security v2 pairing window started with device PIN', 'success');
        }
    }
}

// Start long range pairing mode (fixed to 60 seconds)
async function startLongRangePairing() {
    const type = document.getElementById('pairing-type').value;
    const types = type === 'all' ? null : [type];
    const pinInput = document.getElementById('pairing-pin');
    const pin = pinInput ? pinInput.value.trim() : '';

    if (pin && !/^\d{6}$/.test(pin)) {
        showToast('Error', 'Security PIN must be exactly 6 digits (or leave blank for legacy devices)', 'error');
        return false;
    }
    
    try {
        const body = { duration: 60, types };
        if (pin) body.pin = pin;
        const response = await fetch('/api/pairing/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        
        const data = await response.json();
        
        if (data.success) {
            if (pinInput) pinInput.value = '';
            updatePairingStatus();
            return true;
        } else {
            showToast('Error', 'Failed to start long range pairing: ' + data.error, 'error');
            return false;
        }
    } catch (error) {
        console.error('Error starting long range pairing:', error);
        showToast('Error', 'Failed to start long range pairing mode', 'error');
        return false;
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
        const gwWarn = document.getElementById('legacy-gateway-warning');
        const gwWarnText = document.getElementById('legacy-gateway-warning-text');

        if (gwWarn && data.security_flags && data.security_flags.security_v2_pair && data.legacy_gateways && data.legacy_gateways.length > 0) {
            gwWarn.classList.remove('hidden');
            const names = data.legacy_gateways.map(g => `${g.name} (${g.version_radio})`).join(', ');
            gwWarnText.textContent = `Upgrade legacy gateways before pairing Security v2 devices: ${names}`;
        } else if (gwWarn) {
            gwWarn.classList.add('hidden');
        }
        
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
                'door': 'Door Sensors Only',
                'plugin': 'Plugin Devices Only'
            };
            const types = data.allowed_types || ['all'];
            const typeLabel = types.length === 1 ? (typeNames[types[0]] || types[0]) : 'All Devices';
            lookingFor.textContent = data.pin_active ? `${typeLabel} (🔒 6-digit PIN active)` : typeLabel;
            
        } else {
            // Pairing window closed — reflect whether secure v2 Known-PIN short-range auto-pair is enabled
            const v2PairOn = Boolean(data.security_flags && data.security_flags.security_v2_pair);
            const autoPairOn = Boolean(data.allow_rssi_auto_pair) && v2PairOn;
            const reqV2 = Boolean(data.security_flags && data.security_flags.require_v2);
            const srToggle = document.getElementById('short-range-toggle');
            if (srToggle) {
                srToggle.checked = autoPairOn;
                srToggle.disabled = !v2PairOn;
            }
            if (autoPairOn) {
                badge.innerHTML = '<span class="bg-green-100 text-green-700 px-3 py-1 rounded-full">Short Range Active (v2 PIN)</span>';
                shortRangeInfo.className = 'bg-green-50 border border-green-200 rounded-lg p-4 mb-4';
                const icon = document.getElementById('short-range-icon');
                const title = document.getElementById('short-range-title');
                const desc = document.getElementById('short-range-desc');
                if (icon) icon.className = 'fas fa-shield-alt text-green-600 mr-2';
                if (title) { title.className = 'font-semibold text-green-800'; title.textContent = 'Short Range Auto-Pairing Active (v2 PIN Only)'; }
                if (desc) { desc.className = 'text-sm text-green-700'; desc.textContent = 'Non-revoked Security v2 devices with a saved 6-digit PIN closer than -50 dBm will auto-pair. Legacy v1 and TOFU devices are blocked.'; }
            } else {
                badge.innerHTML = reqV2
                    ? '<span class="bg-blue-100 text-blue-700 px-3 py-1 rounded-full">Pairing Closed (v2 Required)</span>'
                    : '<span class="bg-gray-100 text-gray-700 px-3 py-1 rounded-full">Pairing Closed</span>';
                shortRangeInfo.className = 'bg-gray-50 border border-gray-200 rounded-lg p-4 mb-4';
                const icon = document.getElementById('short-range-icon');
                const title = document.getElementById('short-range-title');
                const desc = document.getElementById('short-range-desc');
                if (icon) icon.className = 'fas fa-lock text-gray-500 mr-2';
                if (title) { title.className = 'font-semibold text-gray-800'; title.textContent = 'Short Range Auto-Pairing (Off)'; }
                if (desc) {
                    desc.className = 'text-sm text-gray-600';
                    desc.textContent = v2PairOn
                        ? 'Auto-pairing is off. Start Pairing Mode below, or toggle this on to allow non-revoked v2 devices with a saved PIN closer than -50 dBm to auto-pair.'
                        : 'Auto-pairing is off. Enable Security v2 in Settings to use Short Range v2 PIN Auto-Pairing.';
                }
            }
            activeControls.classList.add('hidden');
            inactiveControls.classList.remove('hidden');
            shortRangeInfo.classList.remove('hidden');
            longRangeInfo.classList.add('hidden');
        }

        // Reflect security_v2_pair / require_v2 on the PIN input field
        const pinInput = document.getElementById('pairing-pin');
        if (pinInput && data.security_flags) {
            const v2PairOn = Boolean(data.security_flags.security_v2_pair);
            const reqV2 = Boolean(data.security_flags.require_v2);
            pinInput.disabled = !v2PairOn;
            pinInput.placeholder = !v2PairOn
                ? 'Enable Security v2 in Settings to use PIN pairing'
                : (reqV2 ? '6-digit PIN (required for PIN devices; v1 disabled)' : 'e.g. 482910');
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
                // Use icon from API response if available (plugin devices), otherwise use type mapping
                const icon = device.icon || typeIcons[device.type] || '❓';
                
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

                // Security v2 badge (gateways do not use PINs, so v2-unverified renders as green v2)
                const isGateway = Number(device.type) === 1;
                const secBadges = {
                    'v2': '<span class="px-2 py-1 text-xs bg-emerald-100 text-emerald-800 rounded-full font-medium">🔒 v2 Verified</span>',
                    'v2-unverified': isGateway
                        ? '<span class="px-2 py-1 text-xs bg-emerald-100 text-emerald-800 rounded-full font-medium">🔒 v2</span>'
                        : '<span class="px-2 py-1 text-xs bg-amber-100 text-amber-800 rounded-full font-medium">⚠️ v2 Unverified</span>',
                    'v1': '<span class="px-2 py-1 text-xs bg-gray-100 text-gray-600 rounded-full">Legacy v1</span>'
                };
                const secBadge = secBadges[device.crypto_mode || 'v1'] || secBadges['v1'];
                const safeDeviceName = String(device.name || '')
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;')
                    .replace(/'/g, '&#39;');
                
                // Config page URLs
                const configUrls = {
                    1: '/gateways',  // Gateway
                    2: '/buttons',   // Button
                    3: '/lightstrips', // Light
                    4: '/buttons',   // Remote
                    5: '/motion-sensors', // Motion Sensor
                    6: '/door-sensors' // Door Sensor
                };
                let configUrl = configUrls[device.type] || '#';
                
                // For plugin devices with device_capabilities, extract plugin path
                if (device.device_capabilities && device.device_capabilities.configure_endpoint) {
                    configUrl = device.device_capabilities.configure_endpoint;
                }
                
                return `
                    <div class="border border-gray-200 rounded-lg p-4 hover:border-green-300 transition">
                        <div class="flex items-center justify-between">
                            <div class="flex items-center space-x-3 flex-1">
                                <div class="text-2xl">${icon}</div>
                                <div class="flex-1">
                                    <div class="font-semibold text-gray-800">${safeDeviceName}</div>
                                    <div class="font-mono text-xs text-gray-500">${device.mac}</div>
                                    <div class="flex items-center gap-2 mt-1">
                                        <span class="text-xs text-gray-500">${device.type_name}</span>
                                        ${modeBadge}
                                        ${secBadge}
                                    </div>
                                </div>
                            </div>
                            <div class="flex items-center gap-3">
                                <div class="text-right mr-2">
                                    <div class="text-sm font-semibold text-gray-700">${dateStr}</div>
                                    <div class="text-xs text-gray-500">${timeStr}</div>
                                </div>
                                <div class="flex gap-2">
                                    <button onclick="openRenameModal('${device.id || ''}', '${device.name.replace(/'/g, "\\'").replace(/"/g, '&quot;')}', '${device.type}')" 
                                            class="p-2 ${configUrl === '#' ? 'text-green-600/50 cursor-not-allowed' : "text-green-600 hover:bg-green-50"} rounded-lg transition"
                                            title="Rename device">
                                        <i class="fas fa-edit"></i>
                                    </button>
                                    <a href="${configUrl === '#' ? "javascript:void(0);" : configUrl}" 
                                       class="p-2 ${configUrl === '#' ? 'text-blue-600/50 cursor-not-allowed' : "text-blue-600 hover:bg-blue-50"} rounded-lg transition"
                                       title="Configure device">
                                        <i class="fas fa-cog"></i>
                                    </a>
                                    <button onclick="deletePairedDevice('${device.id || ''}', '${device.mac}', '${device.name.replace(/'/g, "\\'").replace(/"/g, '&quot;')}', '${device.type}')" 
                                            class="p-2 ${configUrl === '#' ? 'text-red-600/50 cursor-not-allowed' : "text-red-600 hover:bg-red-50"} rounded-lg transition"
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
        // First, find the device object to check for plugin capabilities
        const response = await fetch('/api/pairing/devices');
        const data = await response.json();
        let device = null;
        
        if (data.success && data.devices) {
            device = data.devices.find(d => d.id === renameDeviceId);
        }
        
        // Determine the endpoint based on device capabilities or type
        let endpoint = '';
        let method = 'POST';
        let body = {};
        
        // Check if device has plugin capabilities
        if (device && device.device_capabilities && device.device_capabilities.rename_endpoint) {
            endpoint = device.device_capabilities.rename_endpoint.replace('{device_id}', renameDeviceId);
            method = 'POST';
            body = { name: newName };
        } else if (renameDeviceType === '1') { // Gateway
            endpoint = `/gateways/api/gateways/${renameDeviceId}`;
            method = 'PUT';
            body = { name: newName };
        } else if (renameDeviceType === '2') { // Button
            endpoint = `/buttons/api/devices/${renameDeviceId}/rename`;
            method = 'POST';
            body = { name: newName };
        } else if (renameDeviceType === '3') { // Lightstrip
            endpoint = `/lightstrips/api/lightstrips/${renameDeviceId}`;
            method = 'PUT';
            body = { name: newName };
        } else if (renameDeviceType === '4') { // Remote
            endpoint = `/buttons/api/devices/${renameDeviceId}/rename`;
            method = 'POST';
            body = { name: newName };
        } else if (renameDeviceType === '5') { // Motion Sensor
            endpoint = `/motion-sensors/api/devices/${renameDeviceId}/rename`;
            method = 'POST';
            body = { name: newName };
        } else if (renameDeviceType === '6') { // Door Sensor
            endpoint = `/door-sensors/api/devices/${renameDeviceId}/rename`;
            method = 'POST';
            body = { name: newName };
        } else {
            showToast('Error', `Unsupported device type: ${renameDeviceType}`, 'error');
            return;
        }
        
        const renameResponse = await fetch(endpoint, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        
        const renameData = await renameResponse.json();
        
        if (renameData.success) {
            showToast('Success', `Device renamed to "${newName}"`, 'success');
            closeRenameModal();
            loadPairedDevices();
        } else {
            showToast('Error', 'Failed to rename device: ' + (renameData.error || 'Unknown error'), 'error');
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
        // First, find the device object to check for plugin capabilities
        const response = await fetch('/api/pairing/devices');
        const data = await response.json();
        let device = null;
        
        if (data.success && data.devices) {
            device = data.devices.find(d => d.id === deleteDeviceId);
        }
        
        // Determine the endpoint based on device capabilities or type
        let endpoint = '';
        
        // Check if device has plugin capabilities
        if (device && device.device_capabilities && device.device_capabilities.delete_endpoint) {
            endpoint = device.device_capabilities.delete_endpoint.replace('{device_id}', deleteDeviceId);
        } else if (deleteDeviceType === '1') { // Gateway
            endpoint = `/gateways/api/gateways/${deleteDeviceId}`;
        } else if (deleteDeviceType === '2') { // Button
            endpoint = `/buttons/api/devices/${deleteDeviceId}`;
        } else if (deleteDeviceType === '3') { // Lightstrip
            endpoint = `/lightstrips/api/lightstrips/${deleteDeviceId}`;
        } else if (deleteDeviceType === '4') { // Remote
            endpoint = `/buttons/api/devices/${deleteDeviceId}`;
        } else if (deleteDeviceType === '5') { // Motion Sensor
            endpoint = `/motion-sensors/api/devices/${deleteDeviceId}`;
        } else if (deleteDeviceType === '6') { // Door Sensor
            endpoint = `/door-sensors/api/devices/${deleteDeviceId}`;
        } else {
            showToast('Error', `Unsupported device type: ${deleteDeviceType}`, 'error');
            closeDeleteModal();
            return;
        }
        
        const deleteResponse = await fetch(endpoint, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' }
        });
        
        const deleteData = await deleteResponse.json();
        
        if (deleteData.success) {
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
            showToast('Error', 'Failed to delete device: ' + (deleteData.error || 'Unknown error'), 'error');
            closeDeleteModal();
        }
    } catch (error) {
        console.error('Error deleting device:', error);
        showToast('Error', 'Failed to delete device', 'error');
        closeDeleteModal();
    }
}

async function toggleShortRangeAutoPair(checkbox) {
    const enabled = Boolean(checkbox.checked);
    checkbox.disabled = true;
    try {
        const resp = await fetch('/api/pairing/short-range', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled })
        });
        const data = await resp.json();
        if (!resp.ok || !data.success) {
            checkbox.checked = !enabled;
            showToast('Error', data.error || 'Failed to update short-range auto-pairing', 'error');
        } else {
            showToast(
                'Success',
                enabled
                    ? 'Short Range Auto-Pairing enabled (v2 saved PINs only, > -50 dBm)'
                    : 'Short Range Auto-Pairing disabled',
                'success'
            );
        }
    } catch (error) {
        checkbox.checked = !enabled;
        console.error('Error toggling short-range auto-pairing:', error);
        showToast('Error', 'Failed to update short-range auto-pairing', 'error');
    } finally {
        checkbox.disabled = false;
        await updatePairingStatus();
    }
}

// Clean up timers on page unload
window.addEventListener('beforeunload', () => {
    if (statusTimer) clearInterval(statusTimer);
});
