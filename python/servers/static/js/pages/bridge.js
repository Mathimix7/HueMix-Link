let currentBridgeIP = null;
let pairingAttempts = 0;
let pairingInterval = null;
let countdownInterval = null;
let secondsRemaining = 30;

// Load current configuration on page load
window.addEventListener('DOMContentLoaded', function() {
    loadBridgeConfig();
});

function loadBridgeConfig() {
    // First, quickly check if config exists (no connection test)
    fetch('/bridge/api/bridge/config/exists')
        .then(response => response.json())
        .then(data => {
            if (data.success && data.configured) {
                // Config exists, show configured section with "Checking..." status
                showConfiguredSectionWithLoading(data.config);
                // Then check the actual connection status
                checkBridgeStatus();
            } else {
                // No config, show setup section
                showSetupSection();
            }
        })
        .catch(err => {
            console.error('Error checking config:', err);
            showSetupSection();
        });
}

function checkBridgeStatus() {
    // Get full config with connection status
    fetch('/bridge/api/bridge/config')
        .then(response => response.json())
        .then(data => {
            if (data.success && data.configured) {
                showConfiguredSection(data.config);
            }
        })
        .catch(err => {
            console.error('Error checking bridge status:', err);
        });
}

function showConfiguredSectionWithLoading(config) {
    document.getElementById('loading-section').classList.add('hidden');
    document.getElementById('configured-section').classList.remove('hidden');
    document.getElementById('setup-section').classList.add('hidden');
    
    document.getElementById('bridge-ip').textContent = config.ip;
    document.getElementById('bridge-id').textContent = 'N/A';
    document.getElementById('bridge-name').textContent = 'N/A';
    
    // Show "Checking..." status
    const statusEl = document.getElementById('connection-status');
    statusEl.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-blue-100 text-blue-800';
    statusEl.innerHTML = '<i class="fas fa-spinner fa-spin mr-1"></i>Checking...';
}

function showConfiguredSection(config) {
    document.getElementById('configured-section').classList.remove('hidden');
    document.getElementById('setup-section').classList.add('hidden');
    
    document.getElementById('bridge-ip').textContent = config.ip;
    document.getElementById('bridge-id').textContent = config.bridgeid || 'N/A';
    document.getElementById('bridge-name').textContent = config.name || 'Hue Bridge';
    
    const statusEl = document.getElementById('connection-status');
    if (config.connected) {
        statusEl.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-green-100 text-green-800';
        statusEl.innerHTML = '<i class="fas fa-check-circle mr-1"></i>Connected';
    } else {
        statusEl.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-red-100 text-red-800';
        statusEl.innerHTML = '<i class="fas fa-times-circle mr-1"></i>Disconnected';
    }
}

function showSetupSection() {
    document.getElementById('loading-section').classList.add('hidden');
    document.getElementById('configured-section').classList.add('hidden');
    document.getElementById('setup-section').classList.remove('hidden');
}

function showStep(stepName) {
    document.querySelectorAll('.setup-step').forEach(el => el.classList.add('hidden'));
    document.getElementById(`step-${stepName}`).classList.remove('hidden');
}

function showLoading(message) {
    document.getElementById('loading-text').textContent = message;
    document.getElementById('loading-overlay').classList.remove('hidden');
}

function hideLoading() {
    document.getElementById('loading-overlay').classList.add('hidden');
}

function discoverBridges() {
    showLoading('Discovering bridges...');
    
    fetch('/bridge/api/bridge/discover')
        .then(response => response.json())
        .then(data => {
            hideLoading();
            
            if (data.success && data.bridges && data.bridges.length > 0) {
                showDiscoveryResults(data.bridges);
            } else {
                showToast('No Bridges Found', 'Please enter your bridge IP manually', 'warning');
            }
        })
        .catch(err => {
            hideLoading();
            console.error('Discovery error:', err);
            showToast('Discovery Failed', 'Please enter your bridge IP manually', 'error');
        });
}

function showDiscoveryResults(bridges) {
    const resultsDiv = document.getElementById('discovery-results');
    resultsDiv.innerHTML = '<h3 class="font-semibold text-gray-900 mb-2">Found Bridges:</h3>';
    
    bridges.forEach(bridge => {
        const bridgeCard = document.createElement('div');
        bridgeCard.className = 'p-4 border border-gray-200 rounded-lg hover:border-blue-500 cursor-pointer transition-colors';
        bridgeCard.innerHTML = `
            <div class="flex items-center justify-between">
                <div>
                    <p class="font-medium text-gray-900">${bridge.id}</p>
                    <p class="text-sm text-gray-500 font-mono">${bridge.internalipaddress}</p>
                </div>
                <button class="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors">
                    Select
                </button>
            </div>
        `;
        
        bridgeCard.onclick = () => selectBridge(bridge.internalipaddress);
        resultsDiv.appendChild(bridgeCard);
    });
    
    resultsDiv.classList.remove('hidden');
}

function verifyManualIP() {
    const ip = document.getElementById('manual-ip').value.trim();
    
    if (!ip) {
        showToast('Invalid IP', 'Please enter an IP address', 'warning');
        return;
    }
    
    showLoading('Verifying bridge...');
    
    fetch('/bridge/api/bridge/verify', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ ip: ip })
    })
    .then(response => response.json())
    .then(data => {
        hideLoading();
        
        if (data.success) {
            showToast('Bridge Found!', 'Bridge verified successfully', 'success');
            selectBridge(ip);
        } else {
            showToast('Verification Failed', 'Not a valid Hue Bridge', 'error');
        }
    })
    .catch(err => {
        hideLoading();
        console.error('Verification error:', err);
        showToast('Verification Failed', 'Unable to connect to this IP address', 'error');
    });
}

function selectBridge(ip) {
    currentBridgeIP = ip;
    document.getElementById('pairing-ip').querySelector('span').textContent = ip;
    showStep('pairing');
}

function goToDiscovery() {
    currentBridgeIP = null;
    showStep('discovery');
}

function pairBridge() {
    if (!currentBridgeIP) {
        showToast('Error', 'No bridge selected', 'error');
        return;
    }
    
    const pairButton = document.getElementById('pair-button');
    pairButton.disabled = true;
    pairButton.innerHTML = '<i class="fas fa-spinner fa-spin mr-2"></i>Waiting for button press...';
    
    const statusDiv = document.getElementById('pairing-status');
    statusDiv.innerHTML = `
        <div class="bg-yellow-50 border border-yellow-200 rounded-lg p-4 text-center">
            <div class="spinner mb-2 mx-auto" style="width: 30px; height: 30px;"></div>
            <p class="text-sm text-yellow-800 font-medium">Press the link button on your bridge now!</p>
            <p class="text-xs text-yellow-600 mt-1">Attempt <span id="attempt-count">0</span> of 10 • <span id="countdown">30</span> seconds remaining</p>
        </div>
    `;
    statusDiv.classList.remove('hidden');
    
    pairingAttempts = 0;
    secondsRemaining = 30;
    
    // Start countdown timer
    countdownInterval = setInterval(() => {
        secondsRemaining--;
        const countdownEl = document.getElementById('countdown');
        if (countdownEl) {
            countdownEl.textContent = secondsRemaining;
        }
        
        if (secondsRemaining <= 0) {
            clearInterval(countdownInterval);
        }
    }, 1000);
    
    attemptPairing();
}

function attemptPairing() {
    pairingAttempts++;
    
    if (pairingAttempts > 10) {
        stopPairing();
        showToast('Pairing Timeout', 'Please try again and press the button', 'error');
        return;
    }
    
    const attemptCountEl = document.getElementById('attempt-count');
    if (attemptCountEl) {
        attemptCountEl.textContent = pairingAttempts;
    }
    
    fetch('/bridge/api/bridge/pair', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            ip: currentBridgeIP,
            app_name: 'hue_mix_link',
            device_name: 'server'
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            stopPairing();
            showToast('Success!', 'Bridge paired successfully', 'success');
            showStep('success');
        } else if (data.button_required) {
            // Keep trying
            setTimeout(attemptPairing, 1000);
        } else {
            stopPairing();
            showToast('Pairing Failed', 'Unable to pair with bridge', 'error');
        }
    })
    .catch(err => {
        stopPairing();
        console.error('Pairing error:', err);
        showToast('Pairing Failed', 'Connection error occurred', 'error');
    });
}

function stopPairing() {
    if (countdownInterval) {
        clearInterval(countdownInterval);
        countdownInterval = null;
    }
    
    const pairButton = document.getElementById('pair-button');
    pairButton.disabled = false;
    pairButton.innerHTML = '<i class="fas fa-link mr-2"></i>Pair with Bridge';
    
    const statusDiv = document.getElementById('pairing-status');
    statusDiv.classList.add('hidden');
}

function testBridge() {
    // Show "Checking..." status in the badge
    const statusEl = document.getElementById('connection-status');
    statusEl.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-blue-100 text-blue-800';
    statusEl.innerHTML = '<i class="fas fa-spinner fa-spin mr-1"></i>Checking...';
    
    fetch('/bridge/api/bridge/test', {
        method: 'POST'
    })
    .then(response => response.json())
    .then(data => {
        if (data.success && data.connected) {
            // Show "Connected" status
            statusEl.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-green-100 text-green-800';
            statusEl.innerHTML = '<i class="fas fa-check-circle mr-1"></i>Connected';
            
            const lightCount = data.light_count || 0;
            showToast('Connection Successful!', `Found ${lightCount} light${lightCount !== 1 ? 's' : ''}`, 'success');
        } else {
            // Show "Disconnected" status
            statusEl.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-red-100 text-red-800';
            statusEl.innerHTML = '<i class="fas fa-times-circle mr-1"></i>Disconnected';
            
            showToast('Connection Failed', 'Unable to connect to bridge', 'error');
        }
    })
    .catch(err => {
        // Show "Disconnected" status on error
        statusEl.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-red-100 text-red-800';
        statusEl.innerHTML = '<i class="fas fa-times-circle mr-1"></i>Disconnected';
        
        console.error('Test error:', err);
        showToast('Connection Failed', 'Unable to test connection', 'error');
    });
}

let touchlinkCooldownInterval = null;

function enableTouchlink() {
    const button = document.getElementById('touchlink-button');
    if (button.disabled) return;

    startTouchlinkCooldown(button);

    fetch('/bridge/api/bridge/touchlink', {
        method: 'POST'
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showToast('Touchlink Enabled!', data.message || 'Touchlink is now active for 30 seconds. Bring the lamp close to the bridge.', 'success');
        } else {
            showToast('Touchlink Failed', data.error || 'Unable to enable touchlink', 'error');
        }
    })
    .catch(err => {
        console.error('Touchlink error:', err);
        showToast('Touchlink Failed', 'Connection error occurred', 'error');
    });
}

function startTouchlinkCooldown(button) {
    const originalHtml = button.innerHTML;
    let remaining = 30;

    button.disabled = true;
    button.classList.add('opacity-50', 'cursor-not-allowed');
    button.innerHTML = '<i class="fas fa-bolt mr-2"></i>Enable Touchlink <span id="touchlink-cooldown">30</span>s';

    touchlinkCooldownInterval = setInterval(() => {
        remaining--;
        const countdownEl = document.getElementById('touchlink-cooldown');
        if (countdownEl) {
            countdownEl.textContent = remaining;
        }

        if (remaining <= 0) {
            clearInterval(touchlinkCooldownInterval);
            touchlinkCooldownInterval = null;
            button.disabled = false;
            button.classList.remove('opacity-50', 'cursor-not-allowed');
            button.innerHTML = originalHtml;
        }
    }, 1000);
}

function showReconfigureModal() {
    document.getElementById('reconfigure-modal').classList.remove('hidden');
}

function closeReconfigureModal() {
    document.getElementById('reconfigure-modal').classList.add('hidden');
}

function confirmReconfigure() {
    closeReconfigureModal();
    
    fetch('/bridge/api/bridge/config', {
        method: 'DELETE'
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showToast('Configuration Deleted', 'Please configure a new bridge', 'success');
            window.location.reload();
        } else {
            showToast('Deletion Failed', 'Unable to delete configuration', 'error');
        }
    })
    .catch(err => {
        console.error('Delete error:', err);
        showToast('Deletion Failed', 'An error occurred', 'error');
    });
}
