let meshData = null;

// Load mesh data on page load
window.addEventListener('DOMContentLoaded', function() {
    loadMeshData();
});

function showSkeletonLoaders() {
    const container = document.getElementById('gateways-container');
    container.innerHTML = '';
    
    // Create 2 skeleton gateway cards
    for (let i = 0; i < 2; i++) {
        const skeletonCard = document.createElement('div');
        skeletonCard.className = 'bg-white rounded-2xl shadow-lg p-6';
        skeletonCard.innerHTML = `
            <div class="flex items-center justify-between mb-6">
                <div class="flex items-center space-x-3">
                    <div class="w-14 h-14 rounded-xl skeleton"></div>
                    <div>
                        <div class="h-6 w-40 skeleton mb-2"></div>
                        <div class="h-4 w-32 skeleton"></div>
                    </div>
                </div>
                <div class="h-8 w-24 skeleton rounded-full"></div>
            </div>
            
            <div class="grid grid-cols-2 gap-6">
                <div>
                    <div class="h-5 w-24 skeleton mb-3"></div>
                    <div class="space-y-2">
                        <div class="h-16 skeleton rounded-lg"></div>
                        <div class="h-16 skeleton rounded-lg"></div>
                    </div>
                </div>
                <div>
                    <div class="h-5 w-32 skeleton mb-3"></div>
                    <div class="space-y-2">
                        <div class="h-20 skeleton rounded-lg"></div>
                    </div>
                </div>
            </div>
        `;
        container.appendChild(skeletonCard);
    }
}

function loadMeshData() {
    showSkeletonLoaders();
    
    fetch('/mesh/api/topology')
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                meshData = data;
                // Fetch RSSI for each light from their assigned gateway
                fetchLightRSSI();
            } else {
                showToast('Error', 'Failed to load mesh data', 'error');
            }
        })
        .catch(err => {
            console.error('Error loading mesh:', err);
            showToast('Error', 'Failed to load mesh data', 'error');
        });
}

function fetchLightRSSI() {
    const lights = meshData.nodes.filter(n => n.type === 'light');
    
    if (lights.length === 0) {
        renderMesh();
        return;
    }
    
    // Fetch RSSI for each light from their assigned gateway
    const rssiPromises = lights.map(light => 
        fetch(`/lightstrips/api/lightstrips/${light.id}/rssi`)
            .then(response => response.json())
            .then(data => {
                console.log(`RSSI for ${light.id}:`, data);
                if (data.success && data.rssi !== null) {
                    // Store RSSI directly on the light node
                    light.rssi = data.rssi;
                    console.log(`Set RSSI for ${light.label}: ${data.rssi} dBm`);
                } else {
                    console.log(`No RSSI data for ${light.label}`);
                }
            })
            .catch(err => {
                console.error(`Error fetching RSSI for ${light.id}:`, err);
            })
    );
    
    // Wait for all RSSI fetches to complete, then render
    Promise.all(rssiPromises).then(() => {
        renderMesh();
    });
}

function renderMesh() {
    const container = document.getElementById('gateways-container');
    container.innerHTML = '';
    
    // Get gateways
    const gateways = meshData.nodes
        .filter(n => n.type === 'gateway')
        .sort((a, b) => {
            const aSerial = !!a.is_serial;
            const bSerial = !!b.is_serial;
            if (aSerial !== bSerial) return aSerial ? -1 : 1;
            return (a.label || '').localeCompare(b.label || '');
        });
    
    if (gateways.length === 0) {
        container.innerHTML = `
            <div class="text-center py-6 text-gray-500">
                <p class="text-lg">No devices found</p>
                <p class="text-sm mt-1">Devices will appear here automatically once detected.</p>
            </div>
        `;
        return;
    }
    
    gateways.forEach(gateway => {
        const gatewayCard = createGatewayCard(gateway);
        container.appendChild(gatewayCard);
    });
}

function createGatewayCard(gateway) {
    const card = document.createElement('div');
    card.className = 'bg-white rounded-2xl shadow-lg p-6 border-2 border-transparent hover:border-cyan-200 transition-all';
    
    // Get buttons and lights connected to this gateway
    const buttons = meshData.nodes.filter(n => 
        n.type === 'button' && 
        meshData.edges.some(e => e.from === n.id && e.to === gateway.id)
    );
    
    const lights = meshData.nodes.filter(n => 
        n.type === 'light' && 
        meshData.edges.some(e => e.from === gateway.id && e.to === n.id)
    );

    const serialPort = gateway.serial_port || (gateway.serial_endpoint || '').replace('serial://', '');
    const isOnline = gateway.online === true;
    const uptimeLabel = isOnline ? formatUptime(gateway.uptime) : '';
    
    card.innerHTML = `
        <div class="flex items-center justify-between mb-6">
            <div class="flex items-center space-x-4">
                <div class="w-14 h-14 rounded-xl bg-cyan-500/20 flex items-center justify-center">
                    <i class="fas fa-server text-3xl text-cyan-600"></i>
                </div>
                <div>
                    <h3 class="text-xl font-bold text-gray-900">${gateway.label}</h3>
                    <p class="text-sm text-gray-500 font-mono">${gateway.id}</p>
                    <p class="text-xs text-gray-400">${serialPort || gateway.ip || 'No IP'}</p>
                </div>
            </div>
            <div class="text-right">
                <div class="flex items-center space-x-3">
                    <div class="px-3 py-1 text-xs font-semibold rounded-full ${isOnline ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-800'} inline-flex items-center">
                        <i class="fas ${isOnline ? 'fa-check-circle' : 'fa-circle'} mr-1"></i>
                        ${isOnline ? 'Online' : 'Offline'}
                    </div>
                    <button onclick="toggleGatewayDevices('${gateway.id}')" class="text-gray-400 hover:text-gray-600">
                        <i id="gateway-${gateway.id}-icon" class="fas fa-chevron-down transition-transform"></i>
                    </button>
                </div>
            </div>
        </div>
        
        <div id="gateway-${gateway.id}-devices" class="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <!-- Buttons Section -->
            <div>
                <div class="flex items-center justify-between mb-3">
                    <h4 class="text-sm font-semibold text-gray-700 flex items-center">
                        <i class="fas fa-circle text-orange-500 mr-2"></i>
                        Buttons <span class="ml-2 text-xs bg-orange-100 text-orange-800 px-2 py-0.5 rounded-full">${buttons.length}</span>
                    </h4>
                </div>
                <div class="space-y-2">
                    ${buttons.length === 0 
                        ? '<p class="text-sm text-gray-400 text-center py-4 border border-dashed border-gray-200 rounded-lg">No buttons connected</p>'
                        : buttons.map(button => `
                            <div class="border border-gray-200 rounded-lg p-3 hover:border-orange-300 hover:bg-orange-50/30 transition-all">
                                <div class="flex items-center justify-between">
                                    <div class="flex items-center space-x-2">
                                        <i class="fas fa-circle text-xs ${button.configured ? 'text-green-500' : 'text-gray-400'}"></i>
                                        <span class="text-sm font-medium text-gray-900">${button.label}</span>
                                    </div>
                                </div>
                                <p class="text-xs text-gray-500 mt-1 font-mono">${button.id}</p>
                            </div>
                        `).join('')
                    }
                </div>
            </div>
            
            <!-- Lights Section -->
            <div>
                <div class="flex items-center justify-between mb-3">
                    <h4 class="text-sm font-semibold text-gray-700 flex items-center">
                        <i class="fas fa-lightbulb text-yellow-500 mr-2"></i>
                        Lights <span class="ml-2 text-xs bg-yellow-100 text-yellow-800 px-2 py-0.5 rounded-full">${lights.length}</span>
                    </h4>
                </div>
                <div class="space-y-2">
                    ${lights.length === 0 
                        ? '<p class="text-sm text-gray-400 text-center py-4 border border-dashed border-gray-200 rounded-lg">No lights connected</p>'
                        : lights.map(light => {
                            // Get RSSI stored on the light node
                            const rssi = light.rssi || null;
                            // Convert RSSI to percentage (range: -90 dBm = 0%, -40 dBm = 100%)
                            const rawPercent = ((rssi + 90) / (-40 + 90)) * 100;
                            const rssiPercent = rssi 
                                ? Math.max(0, Math.min(100, Math.round(rawPercent))) 
                                : 0;
                                                        
                            return `
                            <div class="border border-gray-200 rounded-lg p-3 hover:border-yellow-300 hover:bg-yellow-50/30 transition-all">
                                <div class="flex items-center justify-between mb-2">
                                    <span class="text-sm font-medium text-gray-900">${light.label}</span>
                                    <button onclick="optimizeLight('${light.id}')" class="text-xs text-cyan-600 hover:text-cyan-800 font-medium">
                                        <i class="fas fa-search-location mr-1"></i>Find Best
                                    </button>
                                </div>
                                <p class="text-xs text-gray-500 font-mono mb-2">${light.id}</p>
                                ${rssi ? `
                                    <div class="flex items-center space-x-2">
                                        <span class="text-xs text-gray-600">Signal:</span>
                                        <div class="flex-1 bg-gray-200 rounded-full h-2">
                                            <div class="rssi-bar ${getRssiBgColor(rssi)}" style="width: ${rssiPercent}%"></div>
                                        </div>
                                        <span class="text-xs font-mono ${getRssiTextColor(rssi)}">${rssi*-1} dBm</span>
                                    </div>
                                ` : '<p class="text-xs text-gray-400">No signal data</p>'}
                            </div>
                        `;
                        }).join('')
                    }
                </div>
            </div>
        </div>
    `;
    
    return card;
}

function toggleGatewayDevices(gatewayId) {
    const devicesSection = document.getElementById(`gateway-${gatewayId}-devices`);
    const icon = document.getElementById(`gateway-${gatewayId}-icon`);
    
    if (devicesSection.style.display === 'none') {
        devicesSection.style.display = 'grid';
        icon.classList.remove('fa-chevron-right');
        icon.classList.add('fa-chevron-down');
    } else {
        devicesSection.style.display = 'none';
        icon.classList.remove('fa-chevron-down');
        icon.classList.add('fa-chevron-right');
    }
}

function getRssiColor(rssi) {
    if (!rssi) return 'bg-gray-100 text-gray-600';
    if (rssi >= -50) return 'bg-green-100 text-green-700';
    if (rssi >= -70) return 'bg-yellow-100 text-yellow-700';
    return 'bg-red-100 text-red-700';
}

function getRssiBgColor(rssi) {
    if (!rssi) return 'bg-gray-400';
    if (rssi >= -50) return 'bg-green-500';
    if (rssi >= -70) return 'bg-yellow-500';
    return 'bg-red-500';
}

function getRssiTextColor(rssi) {
    if (!rssi) return 'text-gray-600';
    if (rssi >= -50) return 'text-green-700';
    if (rssi >= -70) return 'text-yellow-700';
    return 'text-red-700';
}

function formatUptime(totalSeconds) {
    if (totalSeconds === null || totalSeconds === undefined) {
        return '';
    }

    const seconds = Math.max(0, Number(totalSeconds) || 0);
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);

    if (days > 0) {
        return `Uptime ${days}d ${hours}h`;
    }
    if (hours > 0) {
        return `Uptime ${hours}h ${minutes}m`;
    }
    return `Uptime ${minutes}m`;
}

function refreshMesh() {
    const refreshIcon = document.getElementById('refresh-icon');
    refreshIcon.classList.add('rotate-360');
    
    loadMeshData();
    
    setTimeout(() => {
        refreshIcon.classList.remove('rotate-360');
    }, 500);
}

function optimizeLight(lightMac) {
    showToast('Optimizing...', `Finding best gateway for light`, 'info');
    
    fetch(`/lightstrips/api/lightstrips/${lightMac}/ping`, {
        method: 'POST'
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            const bestGateway = data.best_gateway;
            const gatewayName = bestGateway.name || bestGateway.gateway_mac;
            const rssi = bestGateway.rssi !== undefined ? bestGateway.rssi : (bestGateway.rssi_dbm || 'unknown');
            showToast('Optimization Complete', 
                `Best gateway: ${gatewayName} (${rssi} dBm)`, 
                'success');
            // Refresh to show updated routing
            setTimeout(() => loadMeshData(), 1500);
        } else {
            showToast('Optimization Failed', data.error || 'Could not find best gateway', 'error');
        }
    })
    .catch(err => {
        console.error('Error optimizing light:', err);
        showToast('Error', 'Failed to optimize light routing', 'error');
    });
}

function optimizeAllLights() {
    const optimizeBtn = document.getElementById('optimize-btn');
    const lights = meshData.nodes.filter(n => n.type === 'light');
    
    if (lights.length === 0) {
        showToast('No Lights', 'No lights found to optimize', 'warning');
        return;
    }
    
    optimizeBtn.disabled = true;
    optimizeBtn.innerHTML = '<span class="spinner mr-2"></span>Optimizing...';
    
    showToast('Optimizing Network', `Testing ${lights.length} lights across all gateways...`, 'info');
    
    // Optimize each light sequentially
    let completed = 0;
    const optimizeNext = (index) => {
        if (index >= lights.length) {
            optimizeBtn.disabled = false;
            optimizeBtn.innerHTML = '<i class="fas fa-network-wired mr-2"></i>Optimize All Lights';
            showToast('Optimization Complete', `Successfully optimized ${completed} lights`, 'success');
            setTimeout(() => loadMeshData(), 1000);
            return;
        }
        
        const light = lights[index];
        fetch(`/lightstrips/api/lightstrips/${light.id}/ping`, {
            method: 'POST'
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                completed++;
            }
            optimizeNext(index + 1);
        })
        .catch(err => {
            console.error(`Error optimizing ${light.label}:`, err);
            optimizeNext(index + 1);
        });
    };
    
    optimizeNext(0);
}
