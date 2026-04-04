let lightstrips = [];
let rooms = [];
let currentLightstrip = null;
let currentScenes = [];
let colorPickerCallback = null;
let currentPreviewSceneId = null;  // Track if we're in preview mode
let deleteLightstripId = null;
let deleteLightstripName = null;

// Load data on page load
window.addEventListener('DOMContentLoaded', function() {
    loadRooms();
    loadLightstrips();
    
    // Color picker - RGB sliders and number inputs
    ['r', 'g', 'b'].forEach(channel => {
        const slider = document.getElementById(`color-${channel}`);
        const numberInput = document.getElementById(`color-${channel}-value`);
        
        if (slider && numberInput) {
            slider.addEventListener('input', function() {
                numberInput.value = this.value;
                updateColorPreview();
            });
            
            numberInput.addEventListener('input', function() {
                const value = Math.max(0, Math.min(255, parseInt(this.value) || 0));
                this.value = value;
                slider.value = value;
                updateColorPreview();
            });
        }
    });
    
    // HTML5 color picker
    const html5ColorPicker = document.getElementById('color-html5');
    if (html5ColorPicker) {
        html5ColorPicker.addEventListener('input', function() {
            const hex = this.value;
            const r = parseInt(hex.substr(1, 2), 16);
            const g = parseInt(hex.substr(3, 2), 16);
            const b = parseInt(hex.substr(5, 2), 16);
            
            document.getElementById('color-r').value = r;
            document.getElementById('color-r-value').value = r;
            document.getElementById('color-g').value = g;
            document.getElementById('color-g-value').value = g;
            document.getElementById('color-b').value = b;
            document.getElementById('color-b-value').value = b;
            
            updateColorPreview();
        });
    }
});

// Safety: Disable preview mode when page is closed or hidden
let previewModeTimeout = null;

window.addEventListener('beforeunload', function() {
    // Ensure preview mode is disabled when page closes
    if (currentLightstrip) {
        navigator.sendBeacon(
            `/lightstrips/api/lightstrips/${currentLightstrip.id}/preview-mode`,
            JSON.stringify({ enabled: false })
        );
    }
});

document.addEventListener('visibilitychange', function() {
    if (!currentLightstrip) return;
    
    const overridesModal = document.getElementById('overrides-modal');
    const isModalOpen = overridesModal && !overridesModal.classList.contains('hidden');
    
    if (document.hidden) {
        // Disable preview mode when tab becomes hidden (user switched tabs)
        setPreviewMode(false);
    } else if (isModalOpen) {
        // Re-enable preview mode when tab becomes visible again, but only if modal is still open
        setPreviewMode(true);
    }
});

function showSkeletonLoaders() {
    const container = document.getElementById('lightstrips-container');
    container.innerHTML = '';
    
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
                <div class="h-6 w-24 skeleton rounded-full"></div>
            </div>
            
            <div class="grid grid-cols-2 gap-4 mb-4">
                <div>
                    <div class="h-4 w-16 skeleton mb-1"></div>
                    <div class="h-4 w-24 skeleton"></div>
                </div>
                <div>
                    <div class="h-4 w-20 skeleton mb-1"></div>
                    <div class="h-4 w-16 skeleton"></div>
                </div>
                <div>
                    <div class="h-4 w-12 skeleton mb-1"></div>
                    <div class="h-4 w-20 skeleton"></div>
                </div>
                <div>
                    <div class="h-4 w-20 skeleton mb-1"></div>
                    <div class="h-4 w-24 skeleton"></div>
                </div>
                <div>
                    <div class="h-4 w-20 skeleton mb-1"></div>
                    <div class="h-4 w-24 skeleton"></div>
                </div>
            </div>
            
            <div class="border-t border-gray-200 pt-4">
                <div class="flex space-x-2 mb-2">
                    <div class="h-10 skeleton flex-1"></div>
                    <div class="h-10 skeleton flex-1"></div>
                </div>
                <div class="h-10 skeleton"></div>
            </div>
        `;
        container.appendChild(skeletonCard);
    }
}

function loadRooms() {
    const roomSelect = document.getElementById('lightstrip-room');
    const loadingState = document.getElementById('lightstrip-room-loading-state');
    const errorState = document.getElementById('lightstrip-room-error-state');
    
    // Show loading state
    if (loadingState) {
        loadingState.classList.remove('hidden');
    }
    if (errorState) {
        errorState.classList.add('hidden');
    }
    roomSelect.disabled = true;
    
    fetch('/api/rooms')
        .then(response => response.json())
        .then(data => {
            if (loadingState) {
                loadingState.classList.add('hidden');
            }
            roomSelect.disabled = false;
            
            if (data.success) {
                rooms = data.rooms;
                roomSelect.innerHTML = '<option value="">Select a room</option>';
                rooms.forEach(room => {
                    const option = document.createElement('option');
                    option.value = room.id;
                    option.textContent = room.name;
                    roomSelect.appendChild(option);
                });
            } else if (data.needs_config) {
                // Show error state
                if (errorState) {
                    errorState.classList.remove('hidden');
                    document.getElementById('lightstrip-room-error-message').textContent = 
                        'Hue Bridge is not configured. Please configure your bridge to load rooms.';
                }
                roomSelect.disabled = true;
            } else {
                // Show error state
                if (errorState) {
                    errorState.classList.remove('hidden');
                    document.getElementById('lightstrip-room-error-message').textContent = 
                        data.error || 'Failed to load rooms from Hue Bridge';
                }
                roomSelect.disabled = true;
            }
        })
        .catch(err => {
            console.error('Error loading rooms:', err);
            if (loadingState) {
                loadingState.classList.add('hidden');
            }
            if (errorState) {
                errorState.classList.remove('hidden');
                document.getElementById('lightstrip-room-error-message').textContent = 
                    'Failed to connect to server. Please check your connection.';
            }
            roomSelect.disabled = true;
        });
}

function loadLightstrips() {
    showSkeletonLoaders();
    
    fetch('/lightstrips/api/lightstrips')
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                lightstrips = data.lightstrips;
                renderLightstrips();
            }
        })
        .catch(err => {
            console.error('Error loading lightstrips:', err);
            showToast('Error', 'Failed to load lightstrips', 'error');
        });
}

function renderLightstrips() {
    const container = document.getElementById('lightstrips-container');
    container.innerHTML = '';

    if (lightstrips.length === 0) {
        container.innerHTML = `
            <div class="col-span-2 text-center text-gray-500 py-12">
                <p class="text-lg">No lightstrips found</p>
                <p class="text-sm mt-1">Lightstrips will appear here automatically once detected.</p>
            </div>
        `;
        return;
    }

    lightstrips.forEach(strip => {
        const card = createLightstripCard(strip);
        container.appendChild(card);
    });
}

function createLightstripCard(strip) {
    const card = document.createElement('div');
    card.className = 'border border-gray-200 rounded-xl p-6 hover:shadow-lg transition-shadow';
    
    const room = rooms.find(r => r.id === strip.room_id);
    const roomName = room ? room.name : 'Not configured';
    const overrideCount = Object.keys(strip.overrides || {}).length;
    const isConfigured = strip.room_id && strip.room_id !== '';
    
    card.innerHTML = `
        <div class="flex items-start justify-between mb-4">
            <div class="flex items-center space-x-3">
                <div class="w-12 h-12 rounded-lg bg-gradient-to-br from-yellow-500/20 to-yellow-500/5 flex items-center justify-center">
                    <i class="fas fa-lightbulb text-2xl text-yellow-500"></i>
                </div>
                <div>
                    <h3 class="text-lg font-semibold text-gray-900">${strip.name}</h3>
                    <p class="text-sm text-gray-500 font-mono">${strip.mac_address}</p>
                </div>
            </div>
            ${isConfigured ? `<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-green-100 text-green-800 items-center"><i class="fas fa-check-circle mr-1"></i> Configured</span>` : `<span class="px-3 py-1 text-xs font-semibold rounded-full bg-yellow-100 text-yellow-800"><i class="fas fa-circle mr-1"></i>Not Configured</span>`}
        </div>

        <div class="grid grid-cols-2 gap-3 mb-4 text-sm">
            <div>
                <p class="text-gray-500">Room</p>
                <p class="text-gray-900">${roomName}</p>
            </div>
            <div>
                <p class="text-gray-500">Number of LEDs</p>
                <p class="text-gray-900">${strip.number_colors || 'Not Configured'}</p>
            </div>
            <div>
                <p class="text-gray-500">Color Type</p>
                <p class="text-gray-900">${strip.color_type ? strip.color_type.toUpperCase() : 'Not Configured'}</p>
            </div>
            <div>
                <p class="text-gray-500">Mode</p>
                <p class="text-gray-900">${strip.single_color !== undefined ? (strip.single_color ? 'Single Color' : 'Multi Color') : 'Not Configured'}</p>
            </div>
            <div>
                <p class="text-gray-500">Overrides</p>
                <p class="text-gray-900">${overrideCount} scene${overrideCount !== 1 ? 's' : ''}</p>
            </div>
        </div>

        <div class="border-t border-gray-200 pt-4 flex flex-col space-y-2">
            <div class="flex space-x-2">
                <button onclick="configureLightstrip('${strip.id}')" class="flex-1 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors inline-flex items-center justify-center">
                    <i class="fas fa-cog mr-2"></i>Configure
                </button>
                ${isConfigured ? `
                <button onclick="showOverrides('${strip.id}')" class="flex-1 px-4 py-2 bg-yellow-500 text-white rounded-lg hover:bg-yellow-600 transition-colors inline-flex items-center justify-center">
                    <i class="fas fa-palette mr-2"></i>Overrides
                </button>
                ` : ''}
            </div>
            <button onclick="deleteLightstrip('${strip.id}', '${strip.name.replace(/'/g, "\\'")}')" class="w-full px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors inline-flex items-center justify-center">
                <i class="fas fa-trash mr-2"></i>Delete
            </button>
        </div>
    `;
    
    return card;
}

function refreshLightstrips() {
    const refreshIcon = document.getElementById('refresh-icon');
    refreshIcon.classList.add('rotate-360');
    loadLightstrips();
    setTimeout(() => {
        refreshIcon.classList.remove('rotate-360');
    }, 500);
}

// Update slider value displays
document.getElementById('lightstrip-coverage').addEventListener('input', (e) => {
    document.getElementById('coverage-value').textContent = parseFloat(e.target.value).toFixed(1) + 'x';
});

document.getElementById('lightstrip-distortion').addEventListener('input', (e) => {
    document.getElementById('distortion-value').textContent = parseFloat(e.target.value).toFixed(2);
});

function toggleAdvancedSettings() {
    const content = document.getElementById('advanced-settings-content');
    const icon = document.getElementById('advanced-settings-icon');
    
    if (content.classList.contains('hidden')) {
        content.classList.remove('hidden');
        icon.classList.remove('fa-chevron-down');
        icon.classList.add('fa-chevron-up');
    } else {
        content.classList.add('hidden');
        icon.classList.remove('fa-chevron-up');
        icon.classList.add('fa-chevron-down');
    }
}

function hideConfigModal() {
    document.getElementById('lightstrip-modal').classList.add('hidden');
}

function configureLightstrip(stripId) {
    const strip = lightstrips.find(s => s.id === stripId);
    if (!strip) return;
    
    currentLightstrip = strip;
    
    document.getElementById('modal-title').textContent = 'Configure Lightstrip';
    document.getElementById('lightstrip-name').value = strip.name;
    document.getElementById('lightstrip-mac').value = strip.mac_address;
    document.getElementById('lightstrip-room').value = strip.room_id || '';
    document.getElementById('lightstrip-single-color').checked = strip.single_color !== undefined ? strip.single_color : true;
    document.getElementById('lightstrip-num-leds').value = strip.number_colors || 40;
    
    // Advanced settings
    document.getElementById('lightstrip-ignore-third-party').checked = strip.ignore_third_party || false;
    
    const coverage = strip.coverage !== undefined ? strip.coverage : 1.5;
    document.getElementById('lightstrip-coverage').value = coverage;
    document.getElementById('coverage-value').textContent = coverage.toFixed(1) + 'x';
    
    const distortion = strip.distortion !== undefined ? strip.distortion : 0.3;
    document.getElementById('lightstrip-distortion').value = distortion;
    document.getElementById('distortion-value').textContent = distortion.toFixed(2);
    
    document.getElementById('lightstrip-modal').classList.remove('hidden');
}

function saveLightstrip() {
    const btn = document.getElementById('save-lightstrip-btn');
    const spinner = document.getElementById('save-lightstrip-spinner');
    const btnText = document.getElementById('save-lightstrip-text');

    const name = document.getElementById('lightstrip-name').value.trim();
    const roomId = document.getElementById('lightstrip-room').value;
    const singleColor = document.getElementById('lightstrip-single-color').checked;
    const numLeds = parseInt(document.getElementById('lightstrip-num-leds').value);
    const ignoreThirdParty = document.getElementById('lightstrip-ignore-third-party').checked;
    const coverage = parseFloat(document.getElementById('lightstrip-coverage').value);
    const distortion = parseFloat(document.getElementById('lightstrip-distortion').value);

    if (!name || !roomId) {
        showToast('Validation Error', 'Please fill in all required fields', 'warning');
        return;
    }

    // Show loading state on button
    if (btn) {
        btn.disabled = true;
        btn.classList.add('opacity-60', 'cursor-not-allowed');
    }
    if (spinner) spinner.style.display = 'inline-block';
    if (btnText) btnText.textContent = 'Saving...';

    const data = {
        name: name,
        room_id: roomId,
        single_color: singleColor,
        num_leds: numLeds,
        ignore_third_party: ignoreThirdParty,
        coverage: coverage,
        distortion: distortion
    };

    const url = `/lightstrips/api/lightstrips/${currentLightstrip.id}`;

    function restoreButton() {
        if (btn) {
            btn.disabled = false;
            btn.classList.remove('opacity-60', 'cursor-not-allowed');
        }
        if (spinner) spinner.style.display = 'none';
        if (btnText) btnText.innerHTML = '<i class="fas fa-save mr-2"></i>Save';
    }

    fetch(url, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            if (data.warning) {
                showToast('Success!', `Lightstrip saved. ${data.warning}`, 'warning');
            } else {
                showToast('Success!', 'Lightstrip configuration saved and LED count updated', 'success');
            }
            hideConfigModal();
            loadLightstrips();
        } else {
            showToast('Error', data.error || 'Failed to save lightstrip', 'error');
        }
    })
    .catch(err => {
        console.error('Error saving lightstrip:', err);
        showToast('Error', 'Failed to save lightstrip', 'error');
    })
    .finally(() => {
        restoreButton();
    });
}

function showOverrides(stripId) {
    const strip = lightstrips.find(s => s.id === stripId);
    if (!strip) return;
    
    currentLightstrip = strip;
    
    // Enable preview mode to prevent automatic updates
    setPreviewMode(true);
    
    const scenesListContainer = document.getElementById('scenes-list');
    scenesListContainer.innerHTML = '<div class="flex items-center justify-center p-8"><div class="spinner"></div><span class="ml-3 text-gray-600">Loading scenes...</span></div>';
    
    document.getElementById('overrides-modal').classList.remove('hidden');
    
    // Load scenes for the room
    fetch(`/api/rooms/${strip.room_id}/scenes`)
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                currentScenes = data.scenes;
                renderSceneOverrides();
            } else if (data.needs_config) {
                scenesListContainer.innerHTML = `
                    <div class="p-6 bg-red-50 border border-red-200 rounded-lg">
                        <div class="flex items-start space-x-3">
                            <i class="fas fa-exclamation-triangle text-red-600 text-2xl"></i>
                            <div class="flex-1">
                                <h4 class="text-lg font-semibold text-red-900 mb-2">Unable to Load Scenes</h4>
                                <p class="text-sm text-red-700 mb-4">Hue Bridge is not configured. Please configure your bridge to load scenes.</p>
                                <a href="/bridge" class="inline-block px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors">
                                    <i class="fas fa-cog mr-2"></i>Configure Bridge
                                </a>
                            </div>
                        </div>
                    </div>`;
            } else {
                scenesListContainer.innerHTML = `
                    <div class="p-6 bg-red-50 border border-red-200 rounded-lg">
                        <div class="flex items-start space-x-3">
                            <i class="fas fa-exclamation-circle text-red-600 text-2xl"></i>
                            <div class="flex-1">
                                <h4 class="text-lg font-semibold text-red-900 mb-2">Error Loading Scenes</h4>
                                <p class="text-sm text-red-700">${data.error || 'Failed to load scenes from Hue Bridge'}</p>
                            </div>
                        </div>
                    </div>`;
            }
        })
        .catch(err => {
            console.error('Error loading scenes:', err);
            scenesListContainer.innerHTML = `
                <div class="p-6 bg-red-50 border border-red-200 rounded-lg">
                    <div class="flex items-start space-x-3">
                        <i class="fas fa-exclamation-circle text-red-600 text-2xl"></i>
                        <div class="flex-1">
                            <h4 class="text-lg font-semibold text-red-900 mb-2">Connection Error</h4>
                            <p class="text-sm text-red-700">Failed to connect to server. Please check your connection.</p>
                        </div>
                    </div>
                </div>`;
            // Disable preview mode on error
            setPreviewMode(false);
        });
}

function hideOverridesModal() {
    // Disable preview mode to resume automatic updates
    setPreviewMode(false).catch(err => {
        // Failsafe: even if the API call fails, try again
        console.error('Failed to disable preview mode, retrying...', err);
        setTimeout(() => setPreviewMode(false), 1000);
    });
    
    // Clear timeout
    if (previewModeTimeout) {
        clearTimeout(previewModeTimeout);
        previewModeTimeout = null;
    }
    
    document.getElementById('overrides-modal').classList.add('hidden');
    loadLightstrips(); // Refresh to show updated override counts
}

async function setPreviewMode(enabled) {
    // Enable/disable preview mode for the current lightstrip
    if (!currentLightstrip) return;
    
    try {
        const response = await fetch(`/lightstrips/api/lightstrips/${currentLightstrip.id}/preview-mode`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ enabled: enabled })
        });
        
        const data = await response.json();
        if (data.success) {
            console.log(`Preview mode ${enabled ? 'enabled' : 'disabled'} for ${currentLightstrip.name}`);
            
            // Clear any existing timeout
            if (previewModeTimeout) {
                clearTimeout(previewModeTimeout);
                previewModeTimeout = null;
            }
            
            // Set failsafe timeout: auto-disable preview mode after 5 minutes
            if (enabled) {
                previewModeTimeout = setTimeout(() => {
                    console.warn('Preview mode timeout - auto-disabling after 5 minutes');
                    setPreviewMode(false);
                    hideOverridesModal();
                    showToast('Preview Timeout', 'Preview mode was automatically disabled after 5 minutes', 'info');
                }, 5 * 60 * 1000);
            }
        } else {
            console.error('Failed to set preview mode:', data.error);
            // On error, try to ensure preview mode is disabled
            if (enabled) {
                setTimeout(() => setPreviewMode(false), 1000);
            }
        }
    } catch (err) {
        console.error('Error setting preview mode:', err);
        // On error, try to ensure preview mode is disabled
        if (enabled) {
            setTimeout(() => setPreviewMode(false), 1000);
        }
    }
}

function renderSceneOverrides() {
    const container = document.getElementById('scenes-list');
    container.innerHTML = '';
    
    currentScenes.forEach(scene => {
        const override = currentLightstrip.overrides[scene.id] || null;
        const sceneCard = createSceneOverrideCard(scene, override);
        container.appendChild(sceneCard);
    });
}

function createSceneOverrideCard(scene, override) {
    const card = document.createElement('div');
    card.className = 'border border-gray-200 rounded-lg p-4';
    
    let overrideDisplay = '<span class="text-gray-400 text-sm">Automatic</span>';
    
    if (override) {
        if (override.type === 'off') {
            overrideDisplay = '<span class="text-red-600 text-sm font-medium"><i class="fas fa-power-off mr-1"></i>OFF</span>';
        } else if (override.type === 'single_color') {
            const colorStyle = `background-color: rgb(${override.color.r}, ${override.color.g}, ${override.color.b})`;
            overrideDisplay = `<div class="color-preview" style="${colorStyle}"></div>`;
        } else if (override.type === 'multi_color') {
            overrideDisplay = '<div class="flex space-x-2">';
            override.colors.forEach(color => {
                const colorStyle = `background-color: rgb(${color.r}, ${color.g}, ${color.b})`;
                overrideDisplay += `<div class="color-preview" style="${colorStyle}"></div>`;
            });
            overrideDisplay += '</div>';
        }
    }
    
    card.innerHTML = `
        <div class="flex items-center justify-between mb-3">
            <div>
                <h4 class="font-semibold text-gray-900">${scene.name}</h4>
                <p class="text-xs text-gray-500">${scene.id}</p>
            </div>
            <div class="flex items-center space-x-2">
                ${overrideDisplay}
            </div>
        </div>
        
        <div class="flex space-x-2">
            <button onclick="setOverrideOff('${scene.id}')" class="flex-1 px-3 py-2 text-sm ${override?.type === 'off' ? 'bg-red-600 text-white' : 'bg-gray-100 text-gray-700'} rounded hover:bg-red-700 hover:text-white transition-colors">
                <i class="fas fa-power-off mr-1"></i>OFF
            </button>
            <button onclick="setOverrideSingleColor('${scene.id}')" class="flex-1 px-3 py-2 text-sm ${override?.type === 'single_color' ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-700'} rounded hover:bg-blue-700 hover:text-white transition-colors">
                <i class="fas fa-palette mr-1"></i>Color
            </button>
            ${!currentLightstrip.single_color ? `
            <button onclick="setOverrideMultiColor('${scene.id}')" class="flex-1 px-3 py-2 text-sm ${override?.type === 'multi_color' ? 'bg-yellow-500 text-white' : 'bg-gray-100 text-gray-700'} rounded hover:bg-yellow-600 hover:text-white transition-colors">
                <i class="fas fa-brush mr-1"></i>Multi
            </button>
            ` : ''}
            ${override ? `
            <button onclick="clearOverride('${scene.id}')" class="px-3 py-2 text-sm bg-gray-100 text-gray-700 rounded hover:bg-gray-300 transition-colors">
                <i class="fas fa-undo"></i>
            </button>
            ` : ''}
        </div>
    `;
    
    return card;
}

async function setOverrideOff(sceneId) {
    // Activate scene first
    await activateScene(sceneId);
    
    // Preview black color on lightstrip
    await previewColors([{r: 0, g: 0, b: 0}]);
    
    // Save override
    saveOverride(sceneId, { type: 'off' });
}

async function setOverrideSingleColor(sceneId) {
    const override = currentLightstrip.overrides[sceneId];
    let initialColor;
    
    // Save current state for cancel restoration
    previousPreviewColors = null;
    
    // Show modal with loading state
    showColorPickerLoading(sceneId);
    
    // Always activate the scene on the room
    await activateScene(sceneId);
    
    // If we have a saved override, use it
    if (override?.type === 'single_color') {
        initialColor = override.color;
    } else {
        // Wait for colors to update, then get default colors
        const defaultColors = await getDefaultColors(sceneId);
        
        if (defaultColors && defaultColors.length > 0) {
            initialColor = defaultColors[0];  // Use first color for single color mode
        } else {
            initialColor = { r: 255, g: 255, b: 255 };
        }
    }
    
    // Preview the color
    await previewColors([initialColor]);
    
    // Save this as the state to restore on cancel
    previousPreviewColors = [initialColor];
    
    // Update modal with actual colors
    updateColorPickerWithColor(sceneId, initialColor, (color) => {
        saveOverride(sceneId, { type: 'single_color', color: color });
    });
}

async function setOverrideMultiColor(sceneId) {
    const override = currentLightstrip.overrides[sceneId];
    let initialColors;
    
    // Show modal with loading state
    showMultiColorPickerLoading(sceneId);
    
    // Save current state for cancel restoration
    previousPreviewColors = null;
    
    // Always activate the scene on the room
    await activateScene(sceneId);
    
    // If we have a saved override, use it
    if (override?.type === 'multi_color' && override.colors.length > 0) {
        initialColors = override.colors;
    } else {
        // Wait for colors to update, then get default colors
        const defaultColors = await getDefaultColors(sceneId);
        initialColors = defaultColors && defaultColors.length > 0 ? defaultColors : [];
    }
    
    // Preview the colors
    if (initialColors.length > 0) {
        await previewColors(initialColors);
        // Save this as the state to restore on cancel
        previousPreviewColors = initialColors.map(c => ({...c}));
    } else {
        // If no colors, save empty array and turn light off
        previousPreviewColors = [];
        await previewColors([{r: 0, g: 0, b: 0}]);
    }
    
    // Update modal with actual colors
    updateMultiColorPickerWithColors(sceneId, initialColors);
}

async function clearOverride(sceneId) {
    // Clear the override first
    saveOverride(sceneId, null);
    
    // Activate the scene to show default behavior
    await activateScene(sceneId);
    
    // Wait for SSE updates
    await new Promise(resolve => setTimeout(resolve, 1500));
    
    // Get and preview default colors
    const defaultColors = await fetch(`/lightstrips/api/lightstrips/${currentLightstrip.id}/default-colors/${sceneId}`)
        .then(r => r.json())
        .then(data => data.success ? data.colors : [])
        .catch(() => []);
    
    if (defaultColors && defaultColors.length > 0) {
        await previewColors(defaultColors);
    }
}

function saveOverride(sceneId, override) {
    fetch(`/lightstrips/api/lightstrips/${currentLightstrip.id}/overrides`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            scene_id: sceneId,
            override: override
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            currentLightstrip = data.lightstrip;
            // Update in main list
            const index = lightstrips.findIndex(s => s.id === currentLightstrip.id);
            if (index !== -1) {
                lightstrips[index] = currentLightstrip;
            }
            renderSceneOverrides();
            showToast('Success!', 'Override saved', 'success');
        } else {
            showToast('Error', data.error || 'Failed to save override', 'error');
        }
    })
    .catch(err => {
        console.error('Error saving override:', err);
        showToast('Error', 'Failed to save override', 'error');
    });
}

function showColorPicker(initialColor, colorType, callback) {
    colorPickerCallback = callback;
    
    // Set initial values
    document.getElementById('color-r').value = initialColor.r || 255;
    document.getElementById('color-g').value = initialColor.g || 255;
    document.getElementById('color-b').value = initialColor.b || 255;
    document.getElementById('color-r-value').value = initialColor.r || 255;
    document.getElementById('color-g-value').value = initialColor.g || 255;
    document.getElementById('color-b-value').value = initialColor.b || 255;
    
    // Set HTML5 color picker
    const hexColor = `#${((initialColor.r || 255).toString(16).padStart(2, '0'))}${((initialColor.g || 255).toString(16).padStart(2, '0'))}${((initialColor.b || 255).toString(16).padStart(2, '0'))}`;
    document.getElementById('color-html5').value = hexColor;
    
    updateColorPreview();
    document.getElementById('color-picker-modal').classList.remove('hidden');
}

function hideColorPicker() {
    document.getElementById('color-picker-modal').classList.add('hidden');
    
    // If we were editing a color in multi-color mode, restore the full palette preview
    if (multiColorSceneId !== null && multiColorArray.length > 0) {
        previewColorsDebounced(multiColorArray);
    } else if (previousPreviewColors !== null) {
        // Restore previous state if we were in single color mode
        if (previousPreviewColors.length > 0) {
            previewColors(previousPreviewColors);
        } else {
            previewColors([{r: 0, g: 0, b: 0}]);
        }
        previousPreviewColors = null;
    }
    
    colorPickerCallback = null;
    
    // If we were in preview mode and user cancelled, disable preview
    if (currentPreviewSceneId !== null) {
        currentPreviewSceneId = null;
        // Note: Don't disable overall preview mode here, just clear the scene ID
        // The overrides modal's hideOverridesModal() will handle disabling preview mode
    }
}

function updateColorPreview() {
    // Check if we're in preview mode (from override config)
    if (typeof currentPreviewSceneId !== 'undefined' && currentPreviewSceneId !== null) {
        updateColorPreviewWithLightstrip();
    } else {
        // Normal color preview without lightstrip update
        const r = parseInt(document.getElementById('color-r').value);
        const g = parseInt(document.getElementById('color-g').value);
        const b = parseInt(document.getElementById('color-b').value);
        
        const preview = document.getElementById('color-preview');
        preview.style.backgroundColor = `rgb(${r}, ${g}, ${b})`;
        
        // Update HTML5 color picker to match RGB only
        const hexColor = `#${r.toString(16).padStart(2, '0')}${g.toString(16).padStart(2, '0')}${b.toString(16).padStart(2, '0')}`;
        document.getElementById('color-html5').value = hexColor;
    }
}

function confirmColorPick() {
    if (!colorPickerCallback) return;
    
    const color = {
        r: parseInt(document.getElementById('color-r').value),
        g: parseInt(document.getElementById('color-g').value),
        b: parseInt(document.getElementById('color-b').value)
    };
    
    // Clear previous state since user is saving
    previousPreviewColors = null;
    
    colorPickerCallback(color);
    hideColorPicker();
}

// Multi-Color Picker Functions (Palette Mode)
let multiColorSceneId = null;
let multiColorArray = [];
let previousPreviewColors = null;  // Track state before opening pickers for cancel restoration

function showMultiColorPicker(sceneId, numLeds, existingColors) {
    multiColorSceneId = sceneId;
    
    // Initialize palette with existing colors or start empty
    if (existingColors && existingColors.length > 0) {
        multiColorArray = existingColors.map(c => ({ ...c }));
    } else {
        // Start with empty palette
        multiColorArray = [];
    }
    
    document.getElementById('palette-color-count').value = multiColorArray.length || 0;
    renderMultiColorGrid();
    document.getElementById('multi-color-modal').classList.remove('hidden');
}

function hideMultiColorPicker() {
    // Restore previous preview state on cancel
    if (previousPreviewColors !== null) {
        if (previousPreviewColors.length > 0) {
            previewColors(previousPreviewColors);
        } else {
            // If previous state was empty, turn light off
            previewColors([{r: 0, g: 0, b: 0}]);
        }
        previousPreviewColors = null;
    }
    
    document.getElementById('multi-color-modal').classList.add('hidden');
    // Clear scene-specific tracking but don't disable overall preview mode
    // The overrides modal's hideOverridesModal() will handle that
    multiColorSceneId = null;
    multiColorArray = [];
}

function renderMultiColorGrid() {
    const grid = document.getElementById('multi-color-grid');
    grid.innerHTML = '';
    
    // Check if we're in preview mode
    const inPreviewMode = (typeof multiColorSceneId !== 'undefined' && multiColorSceneId !== null);
    
    multiColorArray.forEach((color, index) => {
        const colorRow = document.createElement('div');
        colorRow.className = 'flex items-center space-x-3 p-3 bg-gray-50 rounded-lg hover:bg-gray-100 transition-colors';
        
        const rgbColor = `rgb(${color.r}, ${color.g}, ${color.b})`;
        
        const editFunction = inPreviewMode ? `editPaletteColorWithPreview(${index})` : `editPaletteColor(${index})`;
        const removeFunction = inPreviewMode ? `removePaletteColorWithPreview(${index})` : `removePaletteColor(${index})`;
        
        colorRow.innerHTML = `
            <div class="flex items-center space-x-3 flex-1">
                <div class="w-16 h-16 rounded-lg border-2 border-gray-300 shadow-sm cursor-pointer hover:border-yellow-500 transition-all" 
                     style="background-color: ${rgbColor}"
                     onclick="${editFunction}">
                </div>
                <div class="flex-1">
                    <div class="text-sm font-medium text-gray-700">Color ${index + 1}</div>
                    <div class="text-xs text-gray-500 font-mono">RGB(${color.r}, ${color.g}, ${color.b})</div>
                </div>
            </div>
            <button onclick="${removeFunction}" class="px-3 py-2 text-red-600 hover:bg-red-50 rounded-lg transition-colors">
                <i class="fas fa-trash"></i>
            </button>
        `;
        
        grid.appendChild(colorRow);
    });
    
    // Add "Add Color" button
    const addButton = document.createElement('div');
    addButton.className = 'flex items-center justify-center p-4 border-2 border-dashed border-gray-300 rounded-lg hover:border-yellow-500 hover:bg-yellow-50 cursor-pointer transition-all';
    addButton.innerHTML = `
        <i class="fas fa-plus mr-2 text-gray-400"></i>
        <span class="text-gray-600">Add Color to Palette</span>
    `;
    addButton.onclick = inPreviewMode ? addPaletteColorWithPreview : addPaletteColor;
    grid.appendChild(addButton);
}

function updatePaletteCount() {
    const newCount = parseInt(document.getElementById('palette-color-count').value);
    
    if (newCount < 0) {
        document.getElementById('palette-color-count').value = 0;
        return;
    }
    
    if (newCount > 10) {
        document.getElementById('palette-color-count').value = 10;
        return;
    }
    
    // Adjust array size
    if (newCount > multiColorArray.length) {
        // Add more colors
        while (multiColorArray.length < newCount) {
            multiColorArray.push({ r: 255, g: 255, b: 255 });
        }
    } else if (newCount < multiColorArray.length) {
        // Remove colors
        multiColorArray = multiColorArray.slice(0, newCount);
    }
    
    renderMultiColorGrid();
}

function editPaletteColor(index) {
    const currentColor = multiColorArray[index];
    
    showColorPicker(currentColor, currentLightstrip.color_type, (newColor) => {
        multiColorArray[index] = newColor;
        renderMultiColorGrid();
    });
}

function addPaletteColor() {
    if (multiColorArray.length >= 10) {
        showToast('Limit Reached', 'Maximum 10 colors in palette', 'warning');
        return;
    }
    
    showColorPicker({ r: 255, g: 255, b: 255 }, currentLightstrip.color_type, (newColor) => {
        multiColorArray.push(newColor);
        document.getElementById('palette-color-count').value = multiColorArray.length;
        renderMultiColorGrid();
    });
}

function removePaletteColor(index) {
    multiColorArray.splice(index, 1);
    document.getElementById('palette-color-count').value = multiColorArray.length;
    renderMultiColorGrid();
}

function confirmMultiColorPick() {
    if (!multiColorSceneId) return;
    
    // Clear previous state since user is saving
    previousPreviewColors = null;
    
    saveOverride(multiColorSceneId, {
        type: 'multi_color',
        colors: multiColorArray
    });
    
    hideMultiColorPicker();
}

// ===== Preview and Scene Activation Helper Functions =====

let previewDebounceTimer = null;

async function activateScene(sceneId) {
    try {
        const response = await fetch(`/api/scenes/${sceneId}/activate`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'}
        });
        
        const data = await response.json();
        if (!data.success) {
            console.error('Failed to activate scene:', data.error);
        }
    } catch (err) {
        console.error('Error activating scene:', err);
    }
}

async function getDefaultColors(sceneId) {
    try {
        // Wait 2 seconds for SSE events to update the state manager after scene activation
        // This ensures we get the NEW scene's colors, not the old one's
        await new Promise(resolve => setTimeout(resolve, 1500));
        
        const response = await fetch(`/lightstrips/api/lightstrips/${currentLightstrip.id}/default-colors/${sceneId}`);
        const data = await response.json();
        
        if (data.success) {
            return data.colors;
        } else {
            console.error('Failed to get default colors:', data.error);
            return [];
        }
    } catch (err) {
        console.error('Error getting default colors:', err);
        return [];
    }
}

async function previewColors(colors) {
    if (!colors || colors.length === 0) return;
    
    try {
        const response = await fetch(`/lightstrips/api/lightstrips/${currentLightstrip.id}/preview-colors`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ colors: colors })
        });
        
        const data = await response.json();
        if (!data.success) {
            console.error('Failed to preview colors:', data.error);
        }
    } catch (err) {
        console.error('Error previewing colors:', err);
    }
}

function previewColorsDebounced(colors) {
    if (previewDebounceTimer) {
        clearTimeout(previewDebounceTimer);
    }
    
    previewDebounceTimer = setTimeout(() => {
        previewColors(colors);
    }, 150);  // 150ms debounce
}

// ===== Enhanced Color Pickers with Preview =====

function showColorPickerLoading(sceneId) {
    currentPreviewSceneId = sceneId;
    
    // Show modal first
    const modal = document.getElementById('color-picker-modal');
    modal.classList.remove('hidden');
    
    // Disable controls and show loading state
    const colorPreview = document.getElementById('color-preview');
    
    // Clear any background and show loading spinner
    colorPreview.style.backgroundColor = '#f3f4f6';
    colorPreview.innerHTML = '<div style="display: flex; align-items: center; justify-content: center; height: 100%; width: 100%;"><div class="spinner"></div></div>';
    
    // Disable all inputs
    ['color-r', 'color-g', 'color-b', 'color-r-value', 'color-g-value', 'color-b-value', 'color-html5'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = true;
    });
}

function updateColorPickerWithColor(sceneId, initialColor, callback) {
    colorPickerCallback = callback;
    currentPreviewSceneId = sceneId;
    
    // Clear loading state
    const colorPreview = document.getElementById('color-preview');
    colorPreview.innerHTML = '';
    
    // Enable all inputs
    ['color-r', 'color-g', 'color-b', 'color-r-value', 'color-g-value', 'color-b-value', 'color-html5'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = false;
    });
    
    // Set values
    document.getElementById('color-r').value = initialColor.r || 255;
    document.getElementById('color-g').value = initialColor.g || 255;
    document.getElementById('color-b').value = initialColor.b || 255;
    document.getElementById('color-r-value').value = initialColor.r || 255;
    document.getElementById('color-g-value').value = initialColor.g || 255;
    document.getElementById('color-b-value').value = initialColor.b || 255;
    
    // Set HTML5 color picker
    const hexColor = `#${((initialColor.r || 255).toString(16).padStart(2, '0'))}${((initialColor.g || 255).toString(16).padStart(2, '0'))}${((initialColor.b || 255).toString(16).padStart(2, '0'))}`;
    document.getElementById('color-html5').value = hexColor;
    
    updateColorPreviewWithLightstrip();
}

function showColorPickerWithPreview(sceneId, initialColor, callback) {
    colorPickerCallback = callback;
    currentPreviewSceneId = sceneId;
    
    // Set initial values
    document.getElementById('color-r').value = initialColor.r || 255;
    document.getElementById('color-g').value = initialColor.g || 255;
    document.getElementById('color-b').value = initialColor.b || 255;
    document.getElementById('color-r-value').value = initialColor.r || 255;
    document.getElementById('color-g-value').value = initialColor.g || 255;
    document.getElementById('color-b-value').value = initialColor.b || 255;
    
    // Set HTML5 color picker
    const hexColor = `#${((initialColor.r || 255).toString(16).padStart(2, '0'))}${((initialColor.g || 255).toString(16).padStart(2, '0'))}${((initialColor.b || 255).toString(16).padStart(2, '0'))}`;
    document.getElementById('color-html5').value = hexColor;
    
    updateColorPreviewWithLightstrip();
    document.getElementById('color-picker-modal').classList.remove('hidden');
}

function updateColorPreviewWithLightstrip() {
    const r = parseInt(document.getElementById('color-r').value);
    const g = parseInt(document.getElementById('color-g').value);
    const b = parseInt(document.getElementById('color-b').value);
    
    const preview = document.getElementById('color-preview');
    preview.style.backgroundColor = `rgb(${r}, ${g}, ${b})`;
    
    // Update HTML5 color picker to match RGB
    const hexColor = `#${r.toString(16).padStart(2, '0')}${g.toString(16).padStart(2, '0')}${b.toString(16).padStart(2, '0')}`;
    document.getElementById('color-html5').value = hexColor;
    
    // Send preview to lightstrip (debounced)
    previewColorsDebounced([{r, g, b}]);
}

function showMultiColorPickerLoading(sceneId) {
    multiColorSceneId = sceneId;
    multiColorArray = [];
    
    const modal = document.getElementById('multi-color-modal');
    const grid = document.getElementById('multi-color-grid');
    
    // Show modal first
    modal.classList.remove('hidden');
    
    // Show loading state with better styling
    grid.innerHTML = '<div class="flex items-center justify-center p-12"><div class="spinner"></div><span class="ml-3 text-gray-600 font-medium">Loading scene colors...</span></div>';
    
    // Disable color count input
    document.getElementById('palette-color-count').disabled = true;
    document.getElementById('palette-color-count').value = 0;
}

function updateMultiColorPickerWithColors(sceneId, colors) {
    multiColorSceneId = sceneId;
    
    // Initialize palette with colors
    if (colors && colors.length > 0) {
        multiColorArray = colors.map(c => ({ ...c }));
    } else {
        multiColorArray = [];
    }
    
    // Enable color count input
    document.getElementById('palette-color-count').disabled = false;
    document.getElementById('palette-color-count').value = multiColorArray.length || 0;
    
    renderMultiColorGridWithPreview();
}

function showMultiColorPickerWithPreview(sceneId, existingColors) {
    multiColorSceneId = sceneId;
    
    // Initialize palette with existing colors
    if (existingColors && existingColors.length > 0) {
        multiColorArray = existingColors.map(c => ({ ...c }));
    } else {
        multiColorArray = [];
    }
    
    document.getElementById('palette-color-count').value = multiColorArray.length || 0;
    renderMultiColorGridWithPreview();
    document.getElementById('multi-color-modal').classList.remove('hidden');
}

function renderMultiColorGridWithPreview() {
    renderMultiColorGrid();
    
    // Preview current palette on lightstrip
    if (multiColorArray.length > 0) {
        previewColorsDebounced(multiColorArray);
    } else {
        // If palette is empty, turn light off
        previewColorsDebounced([{r: 0, g: 0, b: 0}]);
    }
}

function editPaletteColorWithPreview(index) {
    const currentColor = multiColorArray[index];
    
    showColorPickerWithPreview(multiColorSceneId, currentColor, (newColor) => {
        multiColorArray[index] = newColor;
        renderMultiColorGridWithPreview();
    });
}

function addPaletteColorWithPreview() {
    if (multiColorArray.length >= 10) {
        showToast('Limit Reached', 'Maximum 10 colors in palette', 'warning');
        return;
    }
    
    showColorPickerWithPreview(multiColorSceneId, { r: 255, g: 255, b: 255 }, (newColor) => {
        multiColorArray.push(newColor);
        document.getElementById('palette-color-count').value = multiColorArray.length;
        renderMultiColorGridWithPreview();
    });
}

function removePaletteColorWithPreview(index) {
    multiColorArray.splice(index, 1);
    document.getElementById('palette-color-count').value = multiColorArray.length;
    renderMultiColorGridWithPreview();
    
    // If we removed the last color, the renderMultiColorGridWithPreview will turn light off
}

function deleteLightstrip(lightstripId, lightstripName) {
    deleteLightstripId = lightstripId;
    deleteLightstripName = lightstripName;
    document.getElementById('delete-lightstrip-name').textContent = lightstripName;
    document.getElementById('delete-modal').classList.remove('hidden');
}

function closeDeleteModal() {
    document.getElementById('delete-modal').classList.add('hidden');
    deleteLightstripId = null;
    deleteLightstripName = null;
}

function confirmDelete() {
    if (!deleteLightstripId) return;
    
    fetch(`/lightstrips/api/lightstrips/${deleteLightstripId}`, {
        method: 'DELETE'
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showToast('Lightstrip Deleted', `${deleteLightstripName} has been removed`, 'success');
            loadLightstrips();
            closeDeleteModal();
        } else {
            showToast('Error', data.error || 'Failed to delete lightstrip', 'error');
        }
    })
    .catch(err => {
        console.error('Error deleting lightstrip:', err);
        showToast('Error', 'Failed to delete lightstrip', 'error');
    });
}
