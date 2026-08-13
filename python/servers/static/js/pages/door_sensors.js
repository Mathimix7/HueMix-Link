let devices = [];
let groups = [];
let scenesByRoom = {};
let currentDeviceId = null;
let renameDeviceId = null;
let deleteDeviceId = null;
let deleteDeviceName = null;
let statePollTimer = null;
let lastDoorStates = {};
let currentConfigEnabled = true;
let currentTimeSlots = [];
let editingTimeSlotIndex = -1;
let deleteTimeSlotIndex = -1;

function escapeHtml(text) {
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return String(text || '').replace(/[&<>"']/g, m => map[m]);
}

function formatDate(value) {
    if (!value) return 'Never';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return 'Unknown';
    return date.toLocaleString();
}

function getStateBadge(state, lastActionAt = null) {
    const normalized = (state || 'unknown').toLowerCase();

    if (normalized === 'open') {
        return '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-red-100 text-red-800 items-center"><i class="fas fa-door-open mr-1"></i>Open</span>';
    }

    if (normalized === 'closed') {
        return '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-emerald-100 text-emerald-800 items-center"><i class="fas fa-door-closed mr-1"></i>Closed</span>';
    }

    return '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-gray-100 text-gray-800 items-center"><i class="fas fa-question-circle mr-1"></i>Unknown</span>';
}

function getBatteryDisplay(device) {
    if (device.battery_percent === undefined || device.battery_percent === null) {
        return '<span class="text-sm text-gray-400 flex items-center space-x-2"><i class="fas fa-battery-empty text-gray-400"></i><span>N/A</span></span>';
    }

    let iconClass = 'fa-battery-half';
    let colorClass = 'text-yellow-600';

    if (device.battery_percent >= 60) {
        iconClass = 'fa-battery-full';
        colorClass = 'text-green-600';
    } else if (device.battery_percent < 20) {
        iconClass = 'fa-battery-quarter';
        colorClass = 'text-red-600';
    }

    const lastUpdated = device.battery_last_updated ? formatDate(device.battery_last_updated) : 'Never';
    const batteryTypeRaw = String(device.battery_type || '').toLowerCase();
    const batteryTypeLabel = batteryTypeRaw === 'cr123a' ? 'CR123A' : 'Li-Ion';

    return `
        <div class="flex items-center space-x-2 battery-tooltip">
            <i class="fas ${iconClass} ${colorClass}"></i>
            <span class="text-sm font-medium ${colorClass}">${device.battery_percent}%</span>
            <div class="tooltip-content">
                <div><strong>Voltage:</strong> ${device.battery_mv || 'N/A'} mV</div>
                <div><strong>Updated:</strong> ${escapeHtml(lastUpdated)}</div>
            </div>
        </div>
    `;
}

function getLightDisplay(device) {
    if (device.light_level === undefined || device.light_level === null) {
        return '<span class="text-sm text-gray-400">N/A</span>';
    }
    return `<span class="text-sm text-gray-600"><i class="fas fa-sun text-yellow-500 mr-1"></i>${device.light_level}/10</span>`;
}

function getConfigStatusBadge(config) {
    const isConfigured = Boolean(config && (config.target_id || config.room_id));
    if (isConfigured) {
        return '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-green-100 text-green-800 items-center"><i class="fas fa-check-circle mr-1"></i> Configured</span>';
    }
    return '<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-gray-100 text-gray-800 items-center"><i class="fas fa-circle mr-1"></i> Not Configured</span>';
}

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

    setTimeout(() => hideToast(), 4500);
}

function hideToast() {
    const toast = document.getElementById('toast-notification');
    const container = document.getElementById('toast-container');
    container.classList.add('translate-x-full', 'opacity-0');
    setTimeout(() => toast.classList.add('hidden'), 300);
}

function getConfigTarget(config) {
    const type = (config.target_type || 'room') === 'zone' ? 'zone' : 'room';
    return {
        id: config.target_id || config.room_id || '',
        type: type
    };
}

async function loadGroups() {
    try {
        const response = await fetch('/api/groups');
        const data = await response.json();
        if (data.success) {
            groups = data.groups || [];
        } else {
            groups = [];
        }
        populateRoomSelect();
    } catch (error) {
        console.error('Failed to load groups:', error);
        groups = [];
        populateRoomSelect();
    }
}

function populateRoomSelect() {
    const roomSelect = document.getElementById('room-select');
    roomSelect.innerHTML = '<option value="">-- Select a room or zone --</option>';

    const roomOptgroup = document.createElement('optgroup');
    roomOptgroup.label = 'Rooms';
    const zoneOptgroup = document.createElement('optgroup');
    zoneOptgroup.label = 'Zones';

    groups.forEach(group => {
        const option = document.createElement('option');
        option.value = group.id;
        option.dataset.type = group.type;
        option.textContent = group.name;
        if (group.type === 'zone') {
            zoneOptgroup.appendChild(option);
        } else {
            roomOptgroup.appendChild(option);
        }
    });

    roomSelect.appendChild(roomOptgroup);
    roomSelect.appendChild(zoneOptgroup);
}

async function loadScenesForGroup(groupId) {
    if (!groupId) return [];
    if (scenesByRoom[groupId]) return scenesByRoom[groupId];

    try {
        const response = await fetch(`/api/groups/${groupId}/scenes`);
        const data = await response.json();
        if (data.success) {
            scenesByRoom[groupId] = data.scenes || [];
            return scenesByRoom[groupId];
        }
    } catch (error) {
        console.error('Failed to load scenes:', error);
    }

    scenesByRoom[groupId] = [];
    return [];
}

function populateSceneSelect(selectElement, scenes, selectedId) {
    selectElement.innerHTML = '<option value="">-- Select a scene --</option>';
    scenes.forEach(scene => {
        const option = document.createElement('option');
        option.value = scene.id;
        option.textContent = scene.name;
        selectElement.appendChild(option);
    });

    if (selectedId) {
        selectElement.value = selectedId;
    }
}

function refreshDevices() {
    const icon = document.getElementById('refresh-icon');
    icon.style.transform = 'rotate(360deg)';
    setTimeout(() => {
        icon.style.transform = '';
    }, 500);
    loadDevices();
}

async function loadDevices() {
    try {
        const response = await fetch('/door-sensors/api/devices');
        const data = await response.json();

        if (!data.success) {
            showToast('Error', data.error || 'Failed to load door sensors', 'error');
            return;
        }

        devices = data.devices || [];
        renderDevicesTable();
    } catch (error) {
        console.error('Error loading door sensors:', error);
        showToast('Error', 'Failed to load door sensors', 'error');
    }
}

function renderDevicesTable() {
    const tbody = document.getElementById('devices-table-body');
    tbody.innerHTML = '';

    if (devices.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="8" class="px-6 py-12 text-center text-gray-500">
                    <p class="text-lg">No door sensors found</p>
                    <p class="text-sm mt-1">Door sensors will appear here once detected by a gateway.</p>
                </td>
            </tr>
        `;
        return;
    }

    devices.forEach(device => {
        const config = device.config || {};
        const enabled = config.enabled !== false;
        lastDoorStates[device.id] = {
            state: (device.state || 'unknown').toLowerCase(),
            lastSeen: device.last_seen || null,
            lastActionAt: device.last_action_at || null,
            batteryType: device.battery_type || null,
            batteryPercent: device.battery_percent,
            batteryMv: device.battery_mv,
            batteryLastUpdated: device.battery_last_updated || null,
            lightLevel: device.light_level
        };

        const row = document.createElement('tr');
        row.id = `row-${device.id}`;
        row.className = 'hover:bg-gray-50';

        row.innerHTML = `
            <td class="px-4 py-4 whitespace-nowrap">
                <div class="flex items-center">
                    <i class="fas fa-door-open mr-2 text-red-600"></i>
                    <div class="text-sm font-medium text-gray-900">${escapeHtml(device.name || 'Unnamed Door Sensor')}</div>
                </div>
            </td>
            <td class="px-4 py-4 whitespace-nowrap text-sm text-gray-600 font-mono">${escapeHtml(device.mac_address || 'N/A')}</td>
            <td id="battery-${device.id}" class="px-4 py-4 whitespace-nowrap">${getBatteryDisplay(device)}</td>
            <td id="light-${device.id}" class="px-4 py-4 whitespace-nowrap">${getLightDisplay(device)}</td>
            <td id="state-${device.id}" class="px-4 py-4 whitespace-nowrap">${getStateBadge(device.state, device.last_action_at)}</td>
            <td class="px-4 py-4 whitespace-nowrap">${getConfigStatusBadge(config)}</td>
            <td class="px-4 py-4 whitespace-nowrap">
                <label class="relative inline-flex items-center cursor-pointer">
                    <input type="checkbox" id="toggle-${device.id}" ${enabled ? 'checked' : ''} class="sr-only peer" onchange="toggleSensorEnabled('${device.id}', this.checked)">
                    <div class="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-4 peer-focus:ring-red-300 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-red-600"></div>
                </label>
            </td>
            <td class="px-4 py-4 whitespace-nowrap text-sm font-medium space-x-2">
                <button onclick="openRenameModal('${device.id}')" class="text-teal-600 hover:text-teal-900 inline-flex items-center" title="Rename">
                    <i class="fas fa-edit mr-1"></i> Rename
                </button>
                <button onclick="openConfigModal('${device.id}')" class="text-green-600 hover:text-green-900 inline-flex items-center" title="Configure">
                    <i class="fas fa-cog mr-1"></i> Configure
                </button>
                <button onclick="openDeleteModal('${device.id}')" class="text-red-600 hover:text-red-900 inline-flex items-center" title="Delete">
                    <i class="fas fa-trash mr-1"></i> Delete
                </button>
            </td>
        `;

        tbody.appendChild(row);
    });
}

function flashSensorRow(deviceId) {
    const row = document.getElementById(`row-${deviceId}`);
    if (!row) return;
    row.classList.add('flash-door');
    setTimeout(() => row.classList.remove('flash-door'), 800);
}

async function pollDoorStates() {
    try {
        const response = await fetch('/door-sensors/api/door_states');
        const data = await response.json();
        if (!data.success || !data.door_states) return;

        for (const [deviceId, stateData] of Object.entries(data.door_states)) {
            const stateCell = document.getElementById(`state-${deviceId}`);
            const batteryCell = document.getElementById(`battery-${deviceId}`);
            const lightCell = document.getElementById(`light-${deviceId}`);
            if (!stateCell || !batteryCell || !lightCell) {
                loadDevices();
                return;
            }

            const currentState = (stateData.state || 'unknown').toLowerCase();
            const currentLastSeen = stateData.last_seen || null;
            const currentLastActionAt = stateData.last_action_at || null;
            const currentBatteryType = stateData.battery_type || null;
            const currentBatteryPercent = stateData.battery_percent;
            const currentBatteryMv = stateData.battery_mv;
            const currentBatteryLastUpdated = stateData.battery_last_updated || null;
            const currentLightLevel = stateData.light_level;
            const previous = lastDoorStates[deviceId] || {};

            if (
                (previous.state && previous.state !== currentState) ||
                (previous.lastActionAt && previous.lastActionAt !== currentLastActionAt) ||
                (previous.lastSeen && previous.lastSeen !== currentLastSeen)
            ) {
                flashSensorRow(deviceId);
            }

            stateCell.innerHTML = getStateBadge(stateData.state, stateData.last_action_at);
            batteryCell.innerHTML = getBatteryDisplay(stateData);
            lightCell.innerHTML = getLightDisplay(stateData);
            lastDoorStates[deviceId] = {
                state: currentState,
                lastSeen: currentLastSeen,
                lastActionAt: currentLastActionAt,
                batteryType: currentBatteryType,
                batteryPercent: currentBatteryPercent,
                batteryMv: currentBatteryMv,
                batteryLastUpdated: currentBatteryLastUpdated,
                lightLevel: currentLightLevel
            };
        }
    } catch (error) {
        console.error('Error polling door states:', error);
    }
}

function openRenameModal(deviceId) {
    const device = devices.find(d => d.id === deviceId);
    if (!device) return;

    renameDeviceId = deviceId;
    document.getElementById('device-name').value = device.name || '';
    document.getElementById('rename-modal').classList.remove('hidden');

    setTimeout(() => {
        const input = document.getElementById('device-name');
        input.focus();
        input.select();
    }, 100);
}

function closeRenameModal() {
    document.getElementById('rename-modal').classList.add('hidden');
    renameDeviceId = null;
}

async function saveDeviceName() {
    const newName = document.getElementById('device-name').value.trim();
    if (!newName) {
        showToast('Invalid Name', 'Sensor name cannot be empty', 'error');
        return;
    }

    if (!renameDeviceId) return;

    try {
        const response = await fetch(`/door-sensors/api/devices/${renameDeviceId}/rename`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: newName })
        });
        const data = await response.json();

        if (data.success) {
            showToast('Saved', 'Door sensor renamed successfully', 'success');
            closeRenameModal();
            loadDevices();
            return;
        }

        showToast('Error', data.error || 'Rename failed', 'error');
    } catch (error) {
        console.error('Error renaming sensor:', error);
        showToast('Error', 'Failed to rename sensor', 'error');
    }
}

function openDeleteModal(deviceId) {
    const device = devices.find(d => d.id === deviceId);
    if (!device) return;

    deleteDeviceId = deviceId;
    deleteDeviceName = device.name || 'Door Sensor';
    document.getElementById('delete-device-name').textContent = deleteDeviceName;
    document.getElementById('delete-modal').classList.remove('hidden');
}

function closeDeleteModal() {
    document.getElementById('delete-modal').classList.add('hidden');
    deleteDeviceId = null;
    deleteDeviceName = null;
}

async function confirmDelete() {
    if (!deleteDeviceId) return;

    try {
        const response = await fetch(`/door-sensors/api/devices/${deleteDeviceId}`, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' }
        });
        const data = await response.json();

        if (data.success) {
            showToast('Deleted', `${deleteDeviceName} deleted`, 'success');
            closeDeleteModal();
            loadDevices();
            return;
        }

        showToast('Error', data.error || 'Failed to delete sensor', 'error');
    } catch (error) {
        console.error('Error deleting sensor:', error);
        showToast('Error', 'Failed to delete sensor', 'error');
    }
}

function subtractOneMinute(timeStr) {
    const [hours, minutes] = timeStr.split(':').map(Number);
    let totalMinutes = hours * 60 + minutes - 1;
    if (totalMinutes < 0) {
        totalMinutes = 24 * 60 - 1;
    }
    const newHours = Math.floor(totalMinutes / 60);
    const newMinutes = totalMinutes % 60;
    return `${String(newHours).padStart(2, '0')}:${String(newMinutes).padStart(2, '0')}`;
}

function formatDoorSlotAction(action, sceneName) {
    if (action === 'scene') {
        return sceneName || 'Activate Scene';
    }
    if (action === 'off') {
        return 'Turn Off Room';
    }
    return 'Do Nothing';
}

function normalizeCloseDelaySeconds(value) {
    const parsed = parseInt(value, 10);
    if (!Number.isFinite(parsed)) {
        return 0;
    }
    return Math.max(0, Math.min(86400, parsed));
}

function formatDurationSeconds(totalSeconds) {
    const seconds = normalizeCloseDelaySeconds(totalSeconds);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainderSeconds = seconds % 60;
    const parts = [];

    if (hours > 0) parts.push(`${hours}h`);
    if (minutes > 0) parts.push(`${minutes}m`);
    if (remainderSeconds > 0 || parts.length === 0) parts.push(`${remainderSeconds}s`);

    return parts.join(' ');
}

function initializeLightSensitivityPicker() {
    const container = document.getElementById('light-sensitivity-picker');
    if (!container) return;

    container.innerHTML = '';
    for (let i = 0; i <= 10; i++) {
        const box = document.createElement('button');
        box.type = 'button';
        box.className = 'w-8 h-12 border-2 border-gray-300 rounded-md hover:border-amber-400 hover:bg-amber-50 transition-all cursor-pointer flex items-center justify-center';
        box.dataset.level = String(i);
        box.onclick = () => setLightSensitivity(i);

        const label = document.createElement('span');
        label.className = 'text-xs font-medium text-gray-500';
        label.textContent = String(i);
        box.appendChild(label);

        container.appendChild(box);
    }
}

function setLightSensitivity(level) {
    const normalized = Math.max(0, Math.min(10, Number(level) || 0));
    const boxes = document.querySelectorAll('#light-sensitivity-picker button');

    boxes.forEach(box => {
        const boxLevel = parseInt(box.dataset.level, 10);
        if (boxLevel === normalized) {
            box.className = 'w-8 h-12 border-2 border-amber-500 bg-amber-100 rounded-md shadow-md transition-all cursor-pointer flex items-center justify-center';
            box.querySelector('span').className = 'text-xs font-bold text-amber-700';
        } else {
            box.className = 'w-8 h-12 border-2 border-gray-300 rounded-md hover:border-amber-400 hover:bg-amber-50 transition-all cursor-pointer flex items-center justify-center';
            box.querySelector('span').className = 'text-xs font-medium text-gray-500';
        }
    });
}

function getLightSensitivity() {
    const selected = document.querySelector('#light-sensitivity-picker button.border-amber-500');
    return selected ? parseInt(selected.dataset.level, 10) : 5;
}

function renderTimeSlots() {
    const container = document.getElementById('time-slots-list');
    if (currentTimeSlots.length === 0) {
        container.innerHTML = '<p class="text-sm text-gray-500 italic">No time slots configured. Add a slot to set door open and close actions.</p>';
        return;
    }

    currentTimeSlots.sort((a, b) => (a.start_time || '00:00').localeCompare(b.start_time || '00:00'));
    container.innerHTML = '';

    currentTimeSlots.forEach((slot, index) => {
        const slotDiv = document.createElement('div');
        slotDiv.className = 'border border-gray-200 rounded-lg p-3 hover:bg-gray-50 cursor-pointer transition-colors';
        slotDiv.onclick = () => editTimeSlot(index);

        let endTime;
        if (currentTimeSlots.length === 1) {
            endTime = 'All Day';
        } else {
            const nextIndex = (index + 1) % currentTimeSlots.length;
            endTime = subtractOneMinute(currentTimeSlots[nextIndex].start_time || '00:00');
        }

        const openAction = formatDoorSlotAction(slot.open_action, slot.open_scene_name);
        const closeAction = formatDoorSlotAction(slot.close_action, slot.close_scene_name);
        const closeDelaySeconds = normalizeCloseDelaySeconds(slot.close_delay_seconds);
        const closeDelaySuffix = closeDelaySeconds > 0 ? ` after ${formatDurationSeconds(closeDelaySeconds)}` : '';
        const dndBadge = slot.do_not_disturb
            ? '<span class="ml-2 text-xs bg-purple-100 text-purple-700 px-2 py-0.5 rounded"><i class="fas fa-moon mr-1"></i>DND</span>'
            : '';

        slotDiv.innerHTML = `
            <div class="flex justify-between items-center">
                <div class="flex-1">
                    <div class="flex items-center space-x-2">
                        <i class="fas fa-clock text-red-600"></i>
                        <span class="font-medium text-gray-900">${escapeHtml(slot.start_time || '00:00')} - ${escapeHtml(endTime)}</span>
                        ${dndBadge}
                    </div>
                    <div class="text-sm text-gray-600 mt-1">
                        <i class="fas fa-door-open text-xs mr-1"></i>Door opens -> ${escapeHtml(openAction)}
                    </div>
                    <div class="text-sm text-gray-600">
                        <i class="fas fa-door-closed text-xs mr-1"></i>Door closes -> ${escapeHtml(`${closeAction}${closeDelaySuffix}`)}
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

async function loadTimeSlotScenes() {
    const roomId = document.getElementById('room-select').value;
    const openSceneSelect = document.getElementById('slot-open-scene-select');
    const closeSceneSelect = document.getElementById('slot-close-scene-select');

    openSceneSelect.innerHTML = '<option value="">-- Select a scene --</option>';
    closeSceneSelect.innerHTML = '<option value="">-- Select a scene --</option>';

    if (!roomId) {
        return;
    }

    const scenes = await loadScenesForGroup(roomId);
    scenes.forEach(scene => {
        const optionOpen = document.createElement('option');
        optionOpen.value = scene.id;
        optionOpen.textContent = scene.name;
        openSceneSelect.appendChild(optionOpen);

        const optionClose = document.createElement('option');
        optionClose.value = scene.id;
        optionClose.textContent = scene.name;
        closeSceneSelect.appendChild(optionClose);
    });
}

function updateTimeSlotActionVisibility() {
    const openAction = document.getElementById('slot-open-action').value;
    const closeAction = document.getElementById('slot-close-action').value;

    const openContainer = document.getElementById('slot-open-scene-container');
    const closeContainer = document.getElementById('slot-close-scene-container');
    const closeDelayContainer = document.getElementById('slot-close-delay-container');

    if (openAction === 'scene') {
        openContainer.classList.remove('hidden');
    } else {
        openContainer.classList.add('hidden');
    }

    if (closeAction === 'scene') {
        closeContainer.classList.remove('hidden');
    } else {
        closeContainer.classList.add('hidden');
    }

    if (closeAction === 'nothing') {
        closeDelayContainer.classList.add('hidden');
    } else {
        closeDelayContainer.classList.remove('hidden');
    }
}

function addTimeSlot() {
    const roomId = document.getElementById('room-select').value;
    if (!roomId) {
        showToast('Missing Target', 'Select a room or zone before adding time slots', 'warning');
        return;
    }

    editingTimeSlotIndex = -1;
    document.getElementById('timeslot-modal-title').textContent = 'Add Time Slot';

    document.getElementById('slot-start-time').value = '00:00';
    document.getElementById('slot-open-action').value = 'nothing';
    document.getElementById('slot-close-action').value = 'nothing';
    document.getElementById('slot-open-scene-select').value = '';
    document.getElementById('slot-close-scene-select').value = '';
    document.getElementById('slot-close-delay-seconds').value = '0';
    document.getElementById('slot-dnd').checked = false;

    loadTimeSlotScenes().then(() => {
        updateTimeSlotActionVisibility();
        document.getElementById('timeslot-modal').classList.remove('hidden');
    });
}

function editTimeSlot(index) {
    editingTimeSlotIndex = index;
    const slot = currentTimeSlots[index];
    document.getElementById('timeslot-modal-title').textContent = 'Edit Time Slot';

    loadTimeSlotScenes().then(() => {
        document.getElementById('slot-start-time').value = slot.start_time || '00:00';
        document.getElementById('slot-open-action').value = slot.open_action || 'nothing';
        document.getElementById('slot-close-action').value = slot.close_action || 'nothing';
        document.getElementById('slot-open-scene-select').value = slot.open_scene_id || '';
        document.getElementById('slot-close-scene-select').value = slot.close_scene_id || '';
        document.getElementById('slot-close-delay-seconds').value = String(normalizeCloseDelaySeconds(slot.close_delay_seconds));
        document.getElementById('slot-dnd').checked = slot.do_not_disturb || false;
        updateTimeSlotActionVisibility();
        document.getElementById('timeslot-modal').classList.remove('hidden');
    });
}

function deleteTimeSlot(index) {
    deleteTimeSlotIndex = index;
    document.getElementById('delete-timeslot-modal').classList.remove('hidden');
}

function confirmDeleteTimeSlot() {
    if (deleteTimeSlotIndex >= 0) {
        currentTimeSlots.splice(deleteTimeSlotIndex, 1);
        renderTimeSlots();
    }
    closeDeleteTimeSlotModal();
}

function closeDeleteTimeSlotModal() {
    document.getElementById('delete-timeslot-modal').classList.add('hidden');
    deleteTimeSlotIndex = -1;
}

function saveTimeSlot() {
    const startTime = document.getElementById('slot-start-time').value;
    const openAction = document.getElementById('slot-open-action').value;
    const closeAction = document.getElementById('slot-close-action').value;
    const closeDelayInput = document.getElementById('slot-close-delay-seconds').value;

    const openSceneSelect = document.getElementById('slot-open-scene-select');
    const closeSceneSelect = document.getElementById('slot-close-scene-select');

    const openSceneId = openAction === 'scene' ? (openSceneSelect.value || '') : '';
    const closeSceneId = closeAction === 'scene' ? (closeSceneSelect.value || '') : '';
    const parsedCloseDelay = closeDelayInput === '' ? 0 : parseInt(closeDelayInput, 10);

    if (!startTime) {
        showToast('Missing Time', 'Please set a slot start time', 'error');
        return;
    }

    if (openAction === 'scene' && !openSceneId) {
        showToast('Missing Scene', 'Select a scene for door open action', 'warning');
        return;
    }

    if (closeAction === 'scene' && !closeSceneId) {
        showToast('Missing Scene', 'Select a scene for door close action', 'warning');
        return;
    }

    if (!Number.isInteger(parsedCloseDelay) || parsedCloseDelay < 0 || parsedCloseDelay > 86400) {
        showToast('Invalid Delay', 'Close delay must be a whole number between 0 and 86400 seconds', 'warning');
        return;
    }

    const closeDelaySeconds = closeAction === 'nothing' ? 0 : parsedCloseDelay;

    const timeSlot = {
        start_time: startTime,
        open_action: openAction,
        open_scene_id: openSceneId,
        open_scene_name: openAction === 'scene' && openSceneId ? openSceneSelect.options[openSceneSelect.selectedIndex].text : '',
        close_action: closeAction,
        close_scene_id: closeSceneId,
        close_scene_name: closeAction === 'scene' && closeSceneId ? closeSceneSelect.options[closeSceneSelect.selectedIndex].text : '',
        close_delay_seconds: closeDelaySeconds,
        do_not_disturb: document.getElementById('slot-dnd').checked,
    };

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

async function openConfigModal(deviceId) {
    const device = devices.find(d => d.id === deviceId);
    if (!device) return;

    currentDeviceId = deviceId;
    const config = device.config || {};

    document.getElementById('config-device-name').textContent = device.name || 'Door Sensor';
    currentConfigEnabled = config.enabled !== false;
    initializeLightSensitivityPicker();

    const parsedSensitivity = parseInt(config.light_sensitivity, 10);
    const lightSensitivity = Number.isFinite(parsedSensitivity)
        ? Math.max(0, Math.min(10, parsedSensitivity))
        : 5;
    setLightSensitivity(lightSensitivity);

    const roomSelect = document.getElementById('room-select');
    const target = getConfigTarget(config);
    roomSelect.value = target.id || '';

    currentTimeSlots = Array.isArray(config.time_slots)
        ? config.time_slots.map(slot => ({
            ...slot,
            close_delay_seconds: normalizeCloseDelaySeconds(slot.close_delay_seconds),
        }))
        : [];

    const timeSlotsSection = document.getElementById('time-slots-section');
    let previousTargetId = target.id || '';
    roomSelect.onchange = async function () {
        const groupId = this.value || '';
        if (groupId) {
            timeSlotsSection.classList.remove('hidden');
            if (groupId !== previousTargetId) {
                currentTimeSlots = [];
                renderTimeSlots();
            }
            await loadScenesForGroup(groupId);
            previousTargetId = groupId;
        } else {
            timeSlotsSection.classList.add('hidden');
            currentTimeSlots = [];
            renderTimeSlots();
            previousTargetId = '';
        }
    };

    if (roomSelect.value) {
        timeSlotsSection.classList.remove('hidden');
        await loadScenesForGroup(roomSelect.value);
    } else {
        timeSlotsSection.classList.add('hidden');
    }

    renderTimeSlots();
    document.getElementById('config-modal').classList.remove('hidden');
}

function closeConfigModal() {
    document.getElementById('config-modal').classList.add('hidden');
    currentDeviceId = null;
    currentTimeSlots = [];
    editingTimeSlotIndex = -1;
    closeTimeSlotModal();
    closeDeleteTimeSlotModal();
}

async function saveConfiguration() {
    if (!currentDeviceId) return;

    const roomSelect = document.getElementById('room-select');
    const roomId = roomSelect.value || null;
    const selectedOption = roomId ? roomSelect.options[roomSelect.selectedIndex] : null;
    const roomName = selectedOption ? selectedOption.text : '';
    const targetType = selectedOption?.dataset.type || 'room';

    if (!roomId && currentTimeSlots.length > 0) {
        showToast('Missing Target', 'Select a room or zone before saving time slots', 'warning');
        return;
    }

    const sortedSlots = [...currentTimeSlots].sort((a, b) => (a.start_time || '00:00').localeCompare(b.start_time || '00:00'));

    const payload = {
        device_id: currentDeviceId,
        room_id: roomId,
        room_name: roomName,
        target_id: roomId,
        target_type: targetType,
        enabled: currentConfigEnabled,
        light_sensitivity: getLightSensitivity(),
        time_slots: sortedSlots,
    };

    try {
        const response = await fetch('/door-sensors/api/configure', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const data = await response.json();
        if (data.success) {
            showToast('Saved', 'Door sensor configuration updated', 'success');
            closeConfigModal();
            loadDevices();
            return;
        }

        showToast('Error', data.error || 'Failed to save configuration', 'error');
    } catch (error) {
        console.error('Error saving configuration:', error);
        showToast('Error', 'Failed to save configuration', 'error');
    }
}

async function toggleSensorEnabled(deviceId, enabled) {
    try {
        const response = await fetch(`/door-sensors/api/${deviceId}/toggle`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled })
        });
        const data = await response.json();

        if (!data.success) {
            const checkbox = document.getElementById(`toggle-${deviceId}`);
            if (checkbox) checkbox.checked = !enabled;
            showToast('Error', data.error || 'Failed to update sensor state', 'error');
            return;
        }

        showToast('Updated', `Sensor ${enabled ? 'enabled' : 'disabled'}`, 'success');

        // Keep UI state in sync without re-rendering the table, so toggle animation remains smooth.
        const device = devices.find(d => d.id === deviceId);
        if (device) {
            if (!device.config) {
                device.config = {};
            }
            device.config.enabled = enabled;
        }
    } catch (error) {
        const checkbox = document.getElementById(`toggle-${deviceId}`);
        if (checkbox) checkbox.checked = !enabled;
        console.error('Error toggling sensor:', error);
        showToast('Error', 'Failed to update sensor state', 'error');
    }
}

document.addEventListener('DOMContentLoaded', async () => {
    const slotOpenAction = document.getElementById('slot-open-action');
    const slotCloseAction = document.getElementById('slot-close-action');
    if (slotOpenAction) {
        slotOpenAction.onchange = updateTimeSlotActionVisibility;
    }
    if (slotCloseAction) {
        slotCloseAction.onchange = updateTimeSlotActionVisibility;
    }

    initializeLightSensitivityPicker();
    setLightSensitivity(5);

    await loadGroups();
    await loadDevices();

    statePollTimer = setInterval(pollDoorStates, 2000);
});

window.addEventListener('beforeunload', () => {
    if (statePollTimer) {
        clearInterval(statePollTimer);
    }
});
