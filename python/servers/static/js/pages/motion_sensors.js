let devices = [];
let rooms = [];
let currentDeviceId = null;
let deleteDeviceId = null;
let deleteDeviceName = null;
let deleteTimeSlotIndex = -1;
let lastMotionStates = {};

// Poll for motion sensor state changes
function pollMotionStates() {
    fetch('/motion-sensors/api/motion_states')
        .then(response => response.json())
        .then(data => {
            if (data.success && data.motion_states) {
                const motionStates = data.motion_states;
                for (const [id, lastMotion] of Object.entries(motionStates)) {
                    if (lastMotion && lastMotionStates[id] && lastMotionStates[id] !== lastMotion) {
                        flashSensorRow(id);
                        // Fetch updated sensor data
                        fetch(`/motion-sensors/api/devices/${id}`)
                            .then(resp => resp.json())
                            .then(devData => {
                                if (devData.success && devData.device) {
                                    updateDeviceRow(devData.device);
                                }
                            })
                            .catch(() => loadDevices());
                    }
                    if (!devices.find(d => d.id === id)) {
                        loadDevices();
                    }
                    lastMotionStates[id] = lastMotion;
                }
            }
        })
        .catch(err => {});
}

function flashSensorRow(deviceId) {
    const tbody = document.getElementById('devices-table-body');
    if (!tbody) return;
    const row = Array.from(tbody.children).find(r => r.dataset && r.dataset.deviceId === deviceId);
    if (row) {
        row.classList.add('flash-motion');
        setTimeout(() => row.classList.remove('flash-motion'), 700);
    }
}

function updateDeviceRow(device) {
    const tbody = document.getElementById('devices-table-body');
    if (!tbody) return;
    const row = Array.from(tbody.children).find(r => r.dataset && r.dataset.deviceId === device.id);
    if (!row) return;

    // Update name
    const nameDiv = row.querySelector('td:nth-child(1) .text-sm.font-medium');
    if (nameDiv) nameDiv.textContent = device.name || 'Unnamed Sensor';

    // Update battery cell
    const batteryCell = row.children[2];
    batteryCell.innerHTML = '';
    if (device.battery_percent !== undefined && device.battery_percent !== null) {
        const batteryWrap = document.createElement('div');
        batteryWrap.className = 'flex items-center space-x-2 battery-tooltip';

        const lastUpdated = device.battery_last_updated ? new Date(device.battery_last_updated).toLocaleString() : 'Never';
        const tooltipContent = document.createElement('div');
        tooltipContent.className = 'tooltip-content';
        tooltipContent.innerHTML = `<div><strong>Voltage:</strong> ${device.battery_mv || 'N/A'} mV</div><div><strong>Updated:</strong> ${lastUpdated}</div>`;

        const batteryIcon = document.createElement('i');
        let iconClass, textColor;
        if (device.battery_percent >= 60) {
            iconClass = 'fa-battery-full';
            textColor = 'text-green-600';
        } else if (device.battery_percent >= 20) {
            iconClass = 'fa-battery-half';
            textColor = 'text-yellow-600';
        } else {
            iconClass = 'fa-battery-quarter';
            textColor = 'text-red-600';
        }
        batteryIcon.className = `fas ${iconClass} ${textColor}`;

        const batteryText = document.createElement('span');
        batteryText.className = `text-sm font-medium ${textColor}`;
        batteryText.textContent = `${device.battery_percent}%`;

        batteryWrap.appendChild(batteryIcon);
        batteryWrap.appendChild(batteryText);
        batteryWrap.appendChild(tooltipContent);
        batteryCell.appendChild(batteryWrap);
    } else {
        batteryCell.innerHTML = '<span class="text-sm text-gray-400 flex items-center space-x-2"><i class="fas fa-battery-empty text-gray-400"></i><span>N/A</span></span>';
    }

    // Update light level cell
    const lightCell = row.children[3];
    if (device.light_level !== undefined && device.light_level !== null) {
        lightCell.innerHTML = `<span class="text-sm text-gray-600"><i class="fas fa-sun text-yellow-500 mr-1"></i>${device.light_level}/10</span>`;
    } else {
        lightCell.innerHTML = '<span class="text-sm text-gray-400">N/A</span>';
    }

    // Update status cell
    const statusCell = row.children[4];
    const config = device.config || {};
    const isConfigured = config.room_id ? true : false;
    statusCell.innerHTML = isConfigured ? 
        '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-green-100 text-green-800 items-center"><i class="fas fa-check-circle mr-1"></i> Configured</span>' :
        '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-gray-100 text-gray-800 items-center"><i class="fas fa-circle mr-1"></i> Not Configured</span>';

    const idx = devices.findIndex(d => d.id === device.id);
    if (idx > -1) devices[idx] = device;
}

// Toast notification
function showToast(title, message, type = 'success') {
    const toast = document.getElementById('toast-notification');
    const container = document.getElementById('toast-container');
    const icon = document.getElementById('toast-icon');
    const titleEl = document.getElementById('toast-title');
    const messageEl = document.getElementById('toast-message');

    const types = {
        success: { bg: 'bg-green-50', border: 'border-green-500', icon: 'fa-check-circle', iconColor: 'text-green-600' },
        error: { bg: 'bg-red-50', border: 'border-red-500', icon: 'fa-exclamation-circle', iconColor: 'text-red-600' },
        warning: { bg: 'bg-yellow-50', border: 'border-yellow-500', icon: 'fa-exclamation-triangle', iconColor: 'text-yellow-600' },
        info: { bg: 'bg-blue-50', border: 'border-blue-500', icon: 'fa-info-circle', iconColor: 'text-blue-600' }
    };

    const config = types[type] || types.success;
    container.className = `rounded-lg shadow-2xl border-l-4 ${config.border} p-4 flex items-start space-x-3 transform transition-all duration-300 ease-out ${config.bg}`;
    icon.className = `fas ${config.icon} text-2xl ${config.iconColor}`;
    titleEl.textContent = title;
    messageEl.textContent = message;

    toast.classList.remove('hidden');
    setTimeout(() => {
        container.classList.remove('translate-x-full', 'opacity-0');
    }, 10);

    setTimeout(() => hideToast(), 5000);
}

function hideToast() {
    const toast = document.getElementById('toast-notification');
    const container = document.getElementById('toast-container');
    container.classList.add('translate-x-full', 'opacity-0');
    setTimeout(() => toast.classList.add('hidden'), 300);
}

// Load devices
function refreshDevices() {
    const icon = document.getElementById('refresh-icon');
    icon.style.transform = 'rotate(360deg)';
    setTimeout(() => {
        icon.style.transform = '';
    }, 500);
    loadDevices();
}

function loadDevices() {
    return fetch('/motion-sensors/api/devices')
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                devices = data.devices;
                renderDevicesTable();
            }
        })
        .catch(error => {
            console.error('Error loading devices:', error);
            showToast('Error', 'Failed to load motion sensors', 'error');
        });
}

function renderDevicesTable() {
    const tbody = document.getElementById('devices-table-body');
    tbody.innerHTML = '';

    if (devices.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="7" class="px-6 py-12 text-center text-gray-500">
                    <p class="text-lg">No motion sensors found</p>
                    <p class="text-sm mt-1">Motion sensors will appear here automatically once detected.</p>
                </td>
            </tr>
        `;
        return;
    }

    devices.forEach(device => {
        const row = document.createElement('tr');
        row.dataset.deviceId = device.id;
        row.className = 'hover:bg-gray-50';

        const config = device.config || {};
        const enabled = config.enabled !== false;

        // Name
        const nameCell = document.createElement('td');
        nameCell.className = 'px-6 py-4 whitespace-nowrap';
        nameCell.innerHTML = `
            <div class="flex items-center">
                <div class="flex items-center">
                    <i class="fas fa-running mr-2 text-teal-600 flex-shrink-0"></i>
                </div>
                <div class="text-sm font-medium text-gray-900">${device.name || 'Unnamed Sensor'}</div>
            </div>
        `;

        // MAC Address
        const macCell = document.createElement('td');
        macCell.className = 'px-6 py-4 whitespace-nowrap text-sm text-gray-600 font-mono';
        macCell.textContent = device.mac_address || 'N/A';

        // Battery
        const batteryCell = document.createElement('td');
        batteryCell.className = 'px-6 py-4 whitespace-nowrap';
        if (device.battery_percent !== undefined && device.battery_percent !== null) {
            const lastUpdated = device.battery_last_updated ? new Date(device.battery_last_updated).toLocaleString() : 'Never';
            let iconClass, textColor;
            if (device.battery_percent >= 60) {
                iconClass = 'fa-battery-full';
                textColor = 'text-green-600';
            } else if (device.battery_percent >= 20) {
                iconClass = 'fa-battery-half';
                textColor = 'text-yellow-600';
            } else {
                iconClass = 'fa-battery-quarter';
                textColor = 'text-red-600';
            }
            batteryCell.innerHTML = `
                <div class="flex items-center space-x-2 battery-tooltip">
                    <i class="fas ${iconClass} ${textColor}"></i>
                    <span class="text-sm font-medium ${textColor}">${device.battery_percent}%</span>
                    <div class="tooltip-content">
                        <div><strong>Voltage:</strong> ${device.battery_mv || 'N/A'} mV</div>
                        <div><strong>Updated:</strong> ${lastUpdated}</div>
                    </div>
                </div>
            `;
        } else {
            batteryCell.innerHTML = '<span class="text-sm text-gray-400 flex items-center space-x-2"><i class="fas fa-battery-empty text-gray-400"></i><span>N/A</span></span>';
        }

        // Light Level
        const lightCell = document.createElement('td');
        lightCell.className = 'px-6 py-4 whitespace-nowrap';
        if (device.light_level !== undefined && device.light_level !== null) {
            lightCell.innerHTML = `<span class="text-sm text-gray-600"><i class="fas fa-sun text-yellow-500 mr-1"></i>${device.light_level}/10</span>`;
        } else {
            lightCell.innerHTML = '<span class="text-sm text-gray-400">N/A</span>';
        }

        // Status
        const statusCell = document.createElement('td');
        statusCell.className = 'px-6 py-4 whitespace-nowrap';
        const isConfigured = config.room_id ? true : false;
        if (isConfigured) {
            statusCell.innerHTML = '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-green-100 text-green-800 items-center"><i class="fas fa-check-circle mr-1"></i> Configured</span>';
        } else {
            statusCell.innerHTML = '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-gray-100 text-gray-800 items-center"><i class="fas fa-circle mr-1"></i> Not Configured</span>';
        }

        // Enabled Toggle Switch
        const enabledCell = document.createElement('td');
        enabledCell.className = 'px-6 py-4 whitespace-nowrap';
        const toggleId = `toggle-${device.id}`;
        const toggleSwitch = document.createElement('label');
        toggleSwitch.className = 'relative inline-flex items-center cursor-pointer';
        toggleSwitch.innerHTML = `
            <input type="checkbox" id="${toggleId}" ${enabled ? 'checked' : ''} class="sr-only peer" onchange="toggleSensorEnabled('${device.id}', this.checked)">
            <div class="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-teal-300 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-teal-600"></div>
        `;
        enabledCell.appendChild(toggleSwitch);

        // Actions
        const actionsCell = document.createElement('td');
        actionsCell.className = 'px-6 py-4 whitespace-nowrap text-sm font-medium space-x-2';
        
        const renameBtn = document.createElement('button');
        renameBtn.className = 'text-teal-600 hover:text-teal-900 inline-flex items-center';
        renameBtn.innerHTML = '<i class="fas fa-edit mr-1"></i> Rename';
        renameBtn.addEventListener('click', () => openRenameModal(device.id, device.name || ''));
        
        const configBtn = document.createElement('button');
        configBtn.className = 'text-green-600 hover:text-green-900 inline-flex items-center';
        configBtn.innerHTML = '<i class="fas fa-cog mr-1"></i> Configure';
        configBtn.addEventListener('click', () => openConfigModal(device.id));
        
        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'text-red-600 hover:text-red-900 inline-flex items-center';
        deleteBtn.innerHTML = '<i class="fas fa-trash mr-1"></i> Delete';
        deleteBtn.addEventListener('click', () => openDeleteModal(device.id, device.name || 'Unnamed Sensor'));
        
        actionsCell.appendChild(renameBtn);
        actionsCell.appendChild(configBtn);
        actionsCell.appendChild(deleteBtn);

        row.appendChild(nameCell);
        row.appendChild(macCell);
        row.appendChild(batteryCell);
        row.appendChild(lightCell);
        row.appendChild(statusCell);
        row.appendChild(enabledCell);
        row.appendChild(actionsCell);
        tbody.appendChild(row);
    });
}

// Rename modal
function openRenameModal(deviceId, currentName) {
    currentDeviceId = deviceId;
    document.getElementById('device-name').value = currentName;
    document.getElementById('rename-modal').classList.remove('hidden');
}

function closeRenameModal() {
    document.getElementById('rename-modal').classList.add('hidden');
    currentDeviceId = null;
}

function saveDeviceName() {
    const newName = document.getElementById('device-name').value.trim();
    if (!newName) {
        showToast('Error', 'Please enter a name', 'error');
        return;
    }

    fetch(`/motion-sensors/api/devices/${currentDeviceId}/rename`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: newName })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showToast('Success', 'Sensor renamed successfully', 'success');
            loadDevices();
            closeRenameModal();
        } else {
            showToast('Error', data.error || 'Failed to rename sensor', 'error');
        }
    })
    .catch(error => {
        console.error('Error renaming:', error);
        showToast('Error', 'Network error', 'error');
    });
}

// Configuration modal
let currentTimeSlots = [];
let editingTimeSlotIndex = -1;
let allScenes = [];

function openConfigModal(deviceId) {
    currentDeviceId = deviceId;
    const device = devices.find(d => d.id === deviceId);
    if (!device) return;

    document.getElementById('config-device-name').textContent = device.name || 'Unnamed Sensor';
    const config = device.config || {};

    // Load rooms
    fetch('/api/rooms')
        .then(response => response.json())
        .then(data => {
            if (data.success && data.rooms) {
                rooms = data.rooms;
                const roomSelect = document.getElementById('room-select');
                roomSelect.innerHTML = '<option value="">-- Select a room --</option>';
                rooms.forEach(room => {
                    const option = document.createElement('option');
                    option.value = room.id;
                    option.textContent = room.name;
                    if (config.room_id === room.id) {
                        option.selected = true;
                        loadScenesForRoom(room.id);
                    }
                    roomSelect.appendChild(option);
                });
                
                // Add change listener for room selection
                let previousRoomId = config.room_id;
                roomSelect.onchange = function() {
                    const timeSlotsSection = document.getElementById('time-slots-section');
                    if (this.value) {
                        // Reset time slots if room changed
                        if (this.value !== previousRoomId) {
                            currentTimeSlots = [];
                            renderTimeSlots();
                            previousRoomId = this.value;
                        }
                        timeSlotsSection.classList.remove('hidden');
                        loadScenesForRoom(this.value);
                    } else {
                        timeSlotsSection.classList.add('hidden');
                        currentTimeSlots = [];
                        renderTimeSlots();
                        previousRoomId = null;
                    }
                };
                
                // Show time slots section if room is already selected
                if (config.room_id) {
                    document.getElementById('time-slots-section').classList.remove('hidden');
                }
            }
        });

    // Load time slots
    currentTimeSlots = config.time_slots || [];
    renderTimeSlots();

    // Load settings
    document.getElementById('cooldown-input').value = config.cooldown_seconds || 30;
    updateCooldownDisplay();
    
    // Initialize light sensitivity picker
    initializeLightSensitivityPicker();
    const lightLevel = config.light_sensitivity !== undefined ? config.light_sensitivity : 5;
    setLightSensitivity(lightLevel);
    
    document.getElementById('config-modal').classList.remove('hidden');
}

function updateCooldownDisplay() {
    const input = document.getElementById('cooldown-input');
    const value = parseInt(input.value) || 30;
    const tooltip = document.getElementById('cooldown-slider-tooltip');
    const tooltipValue = document.getElementById('cooldown-tooltip-value');
    
    // Update tooltip value
    tooltipValue.textContent = value;
    
    // Calculate tooltip position based on slider value
    const min = parseInt(input.min) || 5;
    const max = parseInt(input.max) || 60;
    const percentage = ((value - min) / (max - min));
    
    // Adjust for range slider thumb positioning (accounts for thumb width at edges)
    // Range sliders have thumb centered, so we need to scale from ~5% to ~95% instead of 0% to 100%
    const adjustedPercentage = 1 + (percentage * 98);
    tooltip.style.left = `${adjustedPercentage}%`;
}

function adjustCooldown(delta) {
    const input = document.getElementById('cooldown-input');
    let value = parseInt(input.value) || 30;
    value = Math.max(5, Math.min(60, value + delta));
    input.value = value;
    updateCooldownDisplay();
}

function initializeLightSensitivityPicker() {
    const container = document.getElementById('light-sensitivity-picker');
    container.innerHTML = '';
    
    for (let i = 0; i <= 10; i++) {
        const box = document.createElement('button');
        box.type = 'button';
        box.className = 'w-8 h-12 border-2 border-gray-300 rounded-md hover:border-yellow-400 hover:bg-yellow-50 transition-all cursor-pointer flex items-center justify-center';
        box.dataset.level = i;
        box.onclick = () => setLightSensitivity(i);
        
        const label = document.createElement('span');
        label.className = 'text-xs font-medium text-gray-500';
        label.textContent = i;
        box.appendChild(label);
        
        container.appendChild(box);
    }
}

function setLightSensitivity(level) {
    const boxes = document.querySelectorAll('#light-sensitivity-picker button');
    boxes.forEach(box => {
        const boxLevel = parseInt(box.dataset.level);
        if (boxLevel === level) {
            box.className = 'w-8 h-12 border-2 border-yellow-500 bg-yellow-100 rounded-md shadow-md transition-all cursor-pointer flex items-center justify-center';
            box.querySelector('span').className = 'text-xs font-bold text-yellow-700';
        } else {
            box.className = 'w-8 h-12 border-2 border-gray-300 rounded-md hover:border-yellow-400 hover:bg-yellow-50 transition-all cursor-pointer flex items-center justify-center';
            box.querySelector('span').className = 'text-xs font-medium text-gray-500';
        }
    });
}

function getLightSensitivity() {
    const selected = document.querySelector('#light-sensitivity-picker button.border-yellow-500');
    return selected ? parseInt(selected.dataset.level) : 5;
}

function loadScenesForRoom(roomId) {
    if (!roomId) {
        allScenes = [];
        return;
    }
    fetch(`/api/rooms/${roomId}/scenes`)
        .then(response => response.json())
        .then(data => {
            if (data.success && data.scenes) {
                allScenes = data.scenes;
            }
        })
        .catch(err => {
            console.error('Error loading scenes:', err);
            allScenes = [];
        });
}

function renderTimeSlots() {
    const container = document.getElementById('time-slots-list');
    if (currentTimeSlots.length === 0) {
        container.innerHTML = '<p class="text-sm text-gray-500 italic">No time slots configured. Add a time slot to automate lighting based on time of day.</p>';
        return;
    }
    
    // Sort time slots by start time
    currentTimeSlots.sort((a, b) => (a.start_time || '00:00').localeCompare(b.start_time || '00:00'));
    
    container.innerHTML = '';
    currentTimeSlots.forEach((slot, index) => {
        const slotDiv = document.createElement('div');
        slotDiv.className = 'border border-gray-200 rounded-lg p-3 hover:bg-gray-50 cursor-pointer transition-colors';
        slotDiv.onclick = () => editTimeSlot(index);
        
        // Calculate end time as 1 minute before next slot's start time (with wrapping)
        let endTime;
        if (currentTimeSlots.length === 1) {
            // If only one slot, it runs all day
            endTime = 'All Day';
        } else {
            const nextIndex = (index + 1) % currentTimeSlots.length;
            const nextStartTime = currentTimeSlots[nextIndex].start_time;
            endTime = subtractOneMinute(nextStartTime);
        }
        
        const motionAction = slot.motion_action === 'scene' ? (slot.scene_name || 'Scene') : 'Do Nothing';
        const afterAction = slot.after_action === 'off' ? 'Turn Off' : (slot.after_action === 'scene' ? (slot.after_scene_name || 'Scene') : 'Do Nothing');
        const dndBadge = slot.do_not_disturb ? '<span class="ml-2 text-xs bg-purple-100 text-purple-700 px-2 py-0.5 rounded"><i class="fas fa-moon mr-1"></i>DND</span>' : '';
        const durationSeconds = getSlotAfterDurationSeconds(slot);
        
        slotDiv.innerHTML = `
            <div class="flex justify-between items-center">
                <div class="flex-1">
                    <div class="flex items-center space-x-2">
                        <i class="fas fa-clock text-teal-600"></i>
                        <span class="font-medium text-gray-900">${slot.start_time} - ${endTime}</span>
                        ${dndBadge}
                    </div>
                    <div class="text-sm text-gray-600 mt-1">
                        <i class="fas fa-running text-xs mr-1"></i>Motion → ${motionAction}
                    </div>
                    <div class="text-sm text-gray-600">
                        <i class="fas fa-hourglass-half text-xs mr-1"></i>After ${durationSeconds}s → ${afterAction}
                    </div>
                </div>
                <button onclick="deleteTimeSlot(${index}); event.stopPropagation();" class="text-red-600 hover:text-red-800 p-1 inline-flex items-center">
                    <i class="fas fa-trash"></i>
                </button>
            </div>
        `;
        container.appendChild(slotDiv);
    });
}

function getSlotAfterDurationSeconds(slot) {
    const explicitSeconds = Number(slot.after_duration_seconds);
    if (Number.isFinite(explicitSeconds) && explicitSeconds > 0) {
        return Math.floor(explicitSeconds);
    }

    // Backward compatibility: legacy configs stored this field in minutes.
    const legacyMinutes = Number(slot.after_duration);
    if (Number.isFinite(legacyMinutes) && legacyMinutes > 0) {
        return Math.floor(legacyMinutes * 60);
    }

    return 300;
}

function subtractOneMinute(timeStr) {
    // Parse time string HH:MM
    const [hours, minutes] = timeStr.split(':').map(Number);
    
    // Convert to total minutes
    let totalMinutes = hours * 60 + minutes;
    
    // Subtract 1 minute
    totalMinutes -= 1;
    
    // Handle wrapping (if it goes negative, wrap to end of day)
    if (totalMinutes < 0) {
        totalMinutes = 24 * 60 - 1; // 23:59
    }
    
    // Convert back to hours and minutes
    const newHours = Math.floor(totalMinutes / 60);
    const newMinutes = totalMinutes % 60;
    
    // Format as HH:MM
    return `${String(newHours).padStart(2, '0')}:${String(newMinutes).padStart(2, '0')}`;
}

function addTimeSlot() {
    editingTimeSlotIndex = -1;
    document.getElementById('timeslot-modal-title').textContent = 'Add Time Slot';
    
    // Reset form
    document.getElementById('slot-start-time').value = '00:00';
    document.getElementById('slot-motion-action').value = 'scene';
    document.getElementById('slot-scene-select').value = '';
    document.getElementById('slot-after-duration').value = '300';
    document.getElementById('slot-after-action').value = 'off';
    document.getElementById('slot-after-scene-select').value = '';
    document.getElementById('slot-dnd').checked = false;
    
    loadTimeSlotScenes();
    updateTimeSlotActionVisibility();
    
    document.getElementById('timeslot-modal').classList.remove('hidden');
}

function editTimeSlot(index) {
    editingTimeSlotIndex = index;
    const slot = currentTimeSlots[index];
    document.getElementById('timeslot-modal-title').textContent = 'Edit Time Slot';
    
    // Load scenes first, then set values after scenes are loaded
    loadTimeSlotScenes().then(() => {
        // Set form values after scenes are loaded
        document.getElementById('slot-start-time').value = slot.start_time || '00:00';
        document.getElementById('slot-motion-action').value = slot.motion_action || 'scene';
        document.getElementById('slot-scene-select').value = slot.scene_id || '';
        document.getElementById('slot-after-duration').value = getSlotAfterDurationSeconds(slot);
        document.getElementById('slot-after-action').value = slot.after_action || 'off';
        document.getElementById('slot-after-scene-select').value = slot.after_scene_id || '';
        document.getElementById('slot-dnd').checked = slot.do_not_disturb || false;
        
        // Update visibility after all values are set
        updateTimeSlotActionVisibility();
    });
    
    document.getElementById('timeslot-modal').classList.remove('hidden');
}

function deleteTimeSlot(index) {
    deleteTimeSlotIndex = index;
    document.getElementById('delete-timeslot-modal').classList.remove('hidden');
}

function confirmDeleteTimeSlot() {
    if (deleteTimeSlotIndex >= 0) {
        currentTimeSlots.splice(deleteTimeSlotIndex, 1);
        renderTimeSlots();
        deleteTimeSlotIndex = -1;
    }
    closeDeleteTimeSlotModal();
}

function closeDeleteTimeSlotModal() {
    document.getElementById('delete-timeslot-modal').classList.add('hidden');
    deleteTimeSlotIndex = -1;
}

function loadTimeSlotScenes() {
    return new Promise((resolve) => {
        const sceneSelect = document.getElementById('slot-scene-select');
        const afterSceneSelect = document.getElementById('slot-after-scene-select');
        
        sceneSelect.innerHTML = '<option value="">-- Select a scene --</option>';
        afterSceneSelect.innerHTML = '<option value="">-- Select a scene --</option>';
        
        allScenes.forEach(scene => {
            const option1 = document.createElement('option');
            option1.value = scene.id;
            option1.textContent = scene.name;
            sceneSelect.appendChild(option1);
            
            const option2 = document.createElement('option');
            option2.value = scene.id;
            option2.textContent = scene.name;
            afterSceneSelect.appendChild(option2);
        });
        
        resolve();
    });
}

function updateTimeSlotActionVisibility() {
    const motionAction = document.getElementById('slot-motion-action').value;
    const afterAction = document.getElementById('slot-after-action').value;
    
    document.getElementById('slot-scene-container').style.display = motionAction === 'scene' ? 'block' : 'none';
    document.getElementById('slot-after-scene-container').style.display = afterAction === 'scene' ? 'block' : 'none';
}

// Add event listeners for action dropdowns
document.addEventListener('DOMContentLoaded', () => {
    const motionActionSelect = document.getElementById('slot-motion-action');
    const afterActionSelect = document.getElementById('slot-after-action');
    if (motionActionSelect) motionActionSelect.onchange = updateTimeSlotActionVisibility;
    if (afterActionSelect) afterActionSelect.onchange = updateTimeSlotActionVisibility;
});

function saveTimeSlot() {
    const motionAction = document.getElementById('slot-motion-action').value;
    const afterAction = document.getElementById('slot-after-action').value;
    const rawAfterDuration = parseInt(document.getElementById('slot-after-duration').value, 10);
    const afterDurationSeconds = Number.isFinite(rawAfterDuration)
        ? Math.max(1, Math.min(3600, rawAfterDuration))
        : 300;
    
    const timeSlot = {
        start_time: document.getElementById('slot-start-time').value,
        motion_action: motionAction,
        after_duration_seconds: afterDurationSeconds,
        after_action: afterAction,
        do_not_disturb: document.getElementById('slot-dnd').checked
    };
    
    // Only include scene_id and scene_name if motion_action is 'scene'
    if (motionAction === 'scene') {
        timeSlot.scene_id = document.getElementById('slot-scene-select').value;
        const selectedOption = document.getElementById('slot-scene-select').selectedOptions[0];
        if (selectedOption && selectedOption.value) {
            timeSlot.scene_name = selectedOption.textContent;
        } else {
            timeSlot.scene_id = '';
            timeSlot.scene_name = '';
        }
    } else {
        timeSlot.scene_id = '';
        timeSlot.scene_name = '';
    }
    
    // Only include after_scene_id and after_scene_name if after_action is 'scene'
    if (afterAction === 'scene') {
        timeSlot.after_scene_id = document.getElementById('slot-after-scene-select').value;
        const selectedAfterOption = document.getElementById('slot-after-scene-select').selectedOptions[0];
        if (selectedAfterOption && selectedAfterOption.value) {
            timeSlot.after_scene_name = selectedAfterOption.textContent;
        } else {
            timeSlot.after_scene_id = '';
            timeSlot.after_scene_name = '';
        }
    } else {
        timeSlot.after_scene_id = '';
        timeSlot.after_scene_name = '';
    }
    
    if (!timeSlot.start_time) {
        showToast('Error', 'Please set a start time', 'error');
        return;
    }
    
    if (editingTimeSlotIndex >= 0) {
        currentTimeSlots[editingTimeSlotIndex] = timeSlot;
    } else {
        currentTimeSlots.push(timeSlot);
    }
    
    renderTimeSlots();
    closeTimeSlotModal();
}

function closeTimeSlotModal() {
    document.getElementById('timeslot-modal').classList.add('hidden');
    editingTimeSlotIndex = -1;
}

function closeConfigModal() {
    document.getElementById('config-modal').classList.add('hidden');
    currentDeviceId = null;
    currentTimeSlots = [];
    allScenes = [];
}

function saveConfiguration() {
    const roomId = document.getElementById('room-select').value;
    const cooldown = parseInt(document.getElementById('cooldown-input').value) || 60;
    const lightSensitivity = getLightSensitivity();

    fetch('/motion-sensors/api/configure', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            device_id: currentDeviceId,
            room_id: roomId || null,
            cooldown_seconds: cooldown,
            light_sensitivity: lightSensitivity,
            time_slots: currentTimeSlots
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showToast('Success', 'Configuration saved successfully', 'success');
            loadDevices();
            closeConfigModal();
        } else {
            showToast('Error', data.error || 'Failed to save configuration', 'error');
        }
    })
    .catch(error => {
        console.error('Error saving config:', error);
        showToast('Error', 'Network error', 'error');
    });
}

function toggleSensorEnabled(deviceId, enabled) {
    fetch(`/motion-sensors/api/${deviceId}/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: enabled })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showToast('Success', `Sensor ${enabled ? 'enabled' : 'disabled'}`, 'success');
            // Update local device state
            const device = devices.find(d => d.id === deviceId);
            if (device) {
                if (!device.config) device.config = {};
                device.config.enabled = enabled;
            }
        } else {
            showToast('Error', data.error || 'Failed to toggle sensor', 'error');
            // Revert toggle
            document.getElementById(`toggle-${deviceId}`).checked = !enabled;
        }
    })
    .catch(error => {
        console.error('Error toggling sensor:', error);
        showToast('Error', 'Network error', 'error');
        // Revert toggle
        document.getElementById(`toggle-${deviceId}`).checked = !enabled;
    });
}

// Delete modal
function openDeleteModal(deviceId, deviceName) {
    deleteDeviceId = deviceId;
    deleteDeviceName = deviceName;
    document.getElementById('delete-device-name').textContent = deviceName;
    document.getElementById('delete-modal').classList.remove('hidden');
}

function closeDeleteModal() {
    document.getElementById('delete-modal').classList.add('hidden');
    deleteDeviceId = null;
    deleteDeviceName = null;
}

function confirmDelete() {
    fetch(`/motion-sensors/api/devices/${deleteDeviceId}`, {
        method: 'DELETE'
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showToast('Success', 'Motion sensor deleted successfully', 'success');
            loadDevices();
            closeDeleteModal();
        } else {
            showToast('Error', data.error || 'Failed to delete sensor', 'error');
        }
    })
    .catch(error => {
        console.error('Error deleting:', error);
        showToast('Error', 'Network error', 'error');
    });
}

// Initialize
document.addEventListener('DOMContentLoaded', function() {
    loadDevices();
    setInterval(pollMotionStates, 2000); // Poll every 2 seconds
});
