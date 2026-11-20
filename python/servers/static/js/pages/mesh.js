let meshData = null;
let currentView = localStorage.getItem('meshView') || 'list';
let filters = { search: '', types: new Set(), status: 'all' };
let renameTarget = null;
let optimizing = false;
let isLoading = false;

const TYPE_META = {
    gateway:    { label: 'Gateway',    icon: 'fa-server',        color: '#0891b2', badge: 'bg-cyan-100 text-cyan-700',   bar: 'bg-cyan-500' },
    light:      { label: 'Light',      icon: 'fa-lightbulb',     color: '#f59e0b', badge: 'bg-amber-100 text-amber-700', bar: 'bg-amber-500' },
    button:     { label: 'Button',     icon: 'fa-circle',        color: '#f97316', badge: 'bg-orange-100 text-orange-700', bar: 'bg-orange-500' },
    remote:     { label: 'Remote',     icon: 'fa-gamepad',       color: '#8b5cf6', badge: 'bg-violet-100 text-violet-700', bar: 'bg-violet-500' },
    motion:     { label: 'Motion',     icon: 'fa-person-walking',color: '#3b82f6', badge: 'bg-blue-100 text-blue-700',   bar: 'bg-blue-500' },
    door:       { label: 'Door',       icon: 'fa-door-open',     color: '#10b981', badge: 'bg-emerald-100 text-emerald-700', bar: 'bg-emerald-500' },
};

// Deep-sleep devices only report during pairing/press events, so signal
// strength and online/offline status only make sense for lights & gateways.
const DEEP_SLEEP_TYPES = new Set(['button', 'remote', 'motion', 'door']);

const SIGNAL_COLORS = { strong: '#10b981', weak: '#f59e0b', poor: '#ef4444', none: '#d1d5db' };

const MANAGE_LINKS = {
    gateway: '/gateways',
    light: '/lightstrips',
    button: '/buttons',
    remote: '/buttons',
    motion: '/motion-sensors',
    door: '/door-sensors',
};

const RENAME_ENDPOINTS = {
    gateway:    { method: 'PUT',    url: (id) => `/gateways/api/gateways/${id}` },
    light:      { method: 'PUT',    url: (id) => `/lightstrips/api/lightstrips/${id}` },
    button:     { method: 'POST',   url: (id) => `/buttons/api/devices/${id}/rename` },
    remote:     { method: 'POST',   url: (id) => `/buttons/api/devices/${id}/rename` },
    motion:     { method: 'POST',   url: (id) => `/motion-sensors/api/devices/${id}/rename` },
    door:       { method: 'POST',   url: (id) => `/door-sensors/api/devices/${id}/rename` },
};

window.addEventListener('DOMContentLoaded', function() {
    initFilterChips();
    setView(currentView);
    loadMeshData();
});

document.addEventListener('keydown', function(e) {
    if (e.key !== 'Escape') return;
    if (!document.getElementById('rename-modal').classList.contains('hidden')) {
        closeRenameModal();
    } else if (!document.getElementById('detail-drawer').classList.contains('translate-x-full')) {
        closeDrawer();
    }
});

/* ------------------------------------------------------------------ */
/* Data loading                                                         */
/* ------------------------------------------------------------------ */

function showSkeletonLoaders() {
    // Summary stat cards
    const statsStrip = document.getElementById('stats-strip');
    statsStrip.innerHTML = Array(4).fill(`
        <div class="bg-white rounded-2xl shadow-lg p-4 flex items-center space-x-3">
            <div class="w-11 h-11 rounded-xl skeleton flex-shrink-0"></div>
            <div class="flex-1 min-w-0">
                <div class="h-5 w-16 skeleton mb-2"></div>
                <div class="h-3 w-20 skeleton mb-1"></div>
                <div class="h-2 w-24 skeleton"></div>
            </div>
        </div>`).join('');

    // Topology map skeleton
    const mapCanvas = document.getElementById('map-canvas');
    mapCanvas.innerHTML = buildMapSkeleton();

    // Device list skeleton cards
    const container = document.getElementById('gateways-container');
    container.innerHTML = '';
    for (let i = 0; i < 2; i++) {
        const card = document.createElement('div');
        card.className = 'bg-white rounded-2xl shadow-lg p-6';
        card.innerHTML = `
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
                <div class="space-y-2">
                    <div class="h-5 w-24 skeleton mb-3"></div>
                    <div class="h-16 skeleton rounded-lg"></div>
                    <div class="h-16 skeleton rounded-lg"></div>
                </div>
                <div class="space-y-2">
                    <div class="h-5 w-32 skeleton mb-3"></div>
                    <div class="h-20 skeleton rounded-lg"></div>
                </div>
            </div>
        `;
        container.appendChild(card);
    }
}

function buildMapSkeleton() {
    // Mimic the real topology layout: gateway hubs in a row with device
    // nodes fanned out below, staggered pulse so it feels alive.
    const gatewayXs = [150, 450, 750];
    const gwY = 70;
    const parts = [];
    let delay = 0;

    gatewayXs.forEach((gx, gi) => {
        const deviceXs = [gx - 90, gx - 32, gx + 32, gx + 90];
        deviceXs.forEach((dx, di) => {
            const dy = di % 2 === 0 ? 150 : 185;
            parts.push(`<line x1="${gx}" y1="${gwY}" x2="${dx}" y2="${dy}" style="animation-delay:${delay.toFixed(2)}s"/>`);
            delay += 0.06;
            parts.push(`<circle cx="${dx}" cy="${dy}" r="16" style="animation-delay:${delay.toFixed(2)}s"/>`);
            delay += 0.06;
        });
        parts.push(`<circle cx="${gx}" cy="${gwY}" r="25" style="animation-delay:${delay.toFixed(2)}s"/>`);
        delay += 0.06;
    });

    return `
        <svg viewBox="0 0 900 260" class="w-full mesh-skeleton-svg" style="min-width:900px" xmlns="http://www.w3.org/2000/svg">
            ${parts.join('')}
        </svg>`;
}

function loadMeshData() {
    isLoading = true;
    if (!optimizing) showSkeletonLoaders();

    fetch('/mesh/api/topology')
        .then(response => response.json())
        .then(data => {
            isLoading = false;
            if (data.success) {
                meshData = data;
                renderAll();
            } else {
                showToast('Error', 'Failed to load mesh data', 'error');
            }
        })
        .catch(err => {
            isLoading = false;
            console.error('Error loading mesh:', err);
            showToast('Error', 'Failed to load mesh data', 'error');
        });
}

function renderAll() {
    if (!meshData) return;
    renderStats();
    renderMap();
    renderList();
    updateEmptyState();
}

function updateEmptyState() {
    const hasGateways = meshData.nodes.some(n => n.type === 'gateway');
    document.getElementById('empty-state').classList.toggle('hidden', hasGateways);
    document.getElementById('mesh-map').classList.toggle('hidden', !hasGateways || currentView !== 'map');
    document.getElementById('gateways-container').classList.toggle('hidden', !hasGateways || currentView !== 'list');
}

/* ------------------------------------------------------------------ */
/* Stats strip                                                          */
/* ------------------------------------------------------------------ */

function renderStats() {
    if (!meshData || !meshData.summary) return;
    const s = meshData.summary || {};
    const gw = s.gateways || {};
    const dev = s.devices || {};

    const cards = [
        { icon: 'fa-server', color: 'text-cyan-600', bg: 'bg-cyan-100', label: 'Gateways', value: `${gw.online || 0}/${gw.total || 0}`, sub: `${gw.online || 0} online` },
        { icon: 'fa-lightbulb', color: 'text-amber-600', bg: 'bg-amber-100', label: 'Lights', value: dev.light || 0, sub: `${dev.light || 0} on mesh` },
        { icon: 'fa-circle', color: 'text-orange-600', bg: 'bg-orange-100', label: 'Controls', value: (dev.button || 0) + (dev.remote || 0), sub: `${dev.button || 0} buttons · ${dev.remote || 0} remotes` },
        { icon: 'fa-person-walking', color: 'text-blue-600', bg: 'bg-blue-100', label: 'Sensors', value: (dev.motion || 0) + (dev.door || 0), sub: `${dev.motion || 0} motion · ${dev.door || 0} door` },
    ];

    const strip = document.getElementById('stats-strip');
    strip.innerHTML = cards.map(c => `
        <div class="bg-white rounded-2xl shadow-lg p-4 flex items-center space-x-3 border border-transparent hover:border-gray-200 transition-colors">
            <div class="w-11 h-11 rounded-xl ${c.bg} flex items-center justify-center flex-shrink-0">
                <i class="fas ${c.icon} text-xl ${c.color}"></i>
            </div>
            <div class="min-w-0">
                <p class="text-xl font-bold text-gray-900 leading-tight">${esc(c.value)}</p>
                <p class="text-xs font-medium text-gray-500 truncate">${esc(c.label)}</p>
                <p class="text-[11px] text-gray-400 truncate">${esc(c.sub)}</p>
            </div>
        </div>
    `).join('');
}

/* ------------------------------------------------------------------ */
/* View toggle & filters                                                */
/* ------------------------------------------------------------------ */

function setView(view) {
    currentView = view;
    localStorage.setItem('meshView', view);
    document.getElementById('view-map-btn').classList.toggle('bg-cyan-600', view === 'map');
    document.getElementById('view-map-btn').classList.toggle('text-white', view === 'map');
    document.getElementById('view-map-btn').classList.toggle('text-gray-600', view !== 'map');
    document.getElementById('view-list-btn').classList.toggle('bg-cyan-600', view === 'list');
    document.getElementById('view-list-btn').classList.toggle('text-white', view === 'list');
    document.getElementById('view-list-btn').classList.toggle('text-gray-600', view !== 'list');
    document.getElementById('mesh-map').classList.toggle('hidden', view !== 'map');
    document.getElementById('gateways-container').classList.toggle('hidden', view !== 'list');
}

function initFilterChips() {
    const container = document.getElementById('filter-chips');
    const typeChips = ['all', ...Object.keys(TYPE_META).filter(t => t !== 'gateway')];
    container.innerHTML = `
        ${typeChips.map(t => `
            <button onclick="toggleTypeFilter('${t}')" data-type-chip="${t}"
                class="px-3 py-1.5 text-xs font-semibold rounded-full border transition-all inline-flex items-center">
                <i class="fas ${t === 'all' ? 'fa-border-all' : TYPE_META[t].icon} mr-1.5"></i>${t === 'all' ? 'All' : TYPE_META[t].label}
            </button>`).join('')}
        <span class="w-px h-6 bg-gray-200 mx-1"></span>
        ${['all', 'online', 'offline', 'battery'].map(st => `
            <button onclick="setStatusFilter('${st}')" data-status-chip="${st}"
                class="px-3 py-1.5 text-xs font-semibold rounded-full border transition-all inline-flex items-center">
                ${st === 'all' ? '<i class="fas fa-circle-notch mr-1.5"></i>All status' : st === 'online' ? '<i class="fas fa-circle text-green-500 mr-1.5"></i>Online' : st === 'offline' ? '<i class="fas fa-circle text-gray-400 mr-1.5"></i>Offline' : '<i class="fas fa-battery-three-quarters text-green-600 mr-1.5"></i>Battery'}
            </button>`).join('')}
    `;
    applyFilters();
}

function refreshFilterChips() {
    document.querySelectorAll('[data-type-chip]').forEach(chip => {
        const type = chip.dataset.typeChip;
        const active = type === 'all' ? filters.types.size === 0 : filters.types.has(type);
        chip.classList.toggle('bg-cyan-600', active);
        chip.classList.toggle('text-white', active);
        chip.classList.toggle('border-cyan-600', active);
        chip.classList.toggle('bg-white', !active);
        chip.classList.toggle('text-gray-600', !active);
        chip.classList.toggle('border-gray-300', !active);
    });
    document.querySelectorAll('[data-status-chip]').forEach(chip => {
        const active = chip.dataset.statusChip === filters.status;
        chip.classList.toggle('bg-cyan-600', active);
        chip.classList.toggle('text-white', active);
        chip.classList.toggle('border-cyan-600', active);
        chip.classList.toggle('bg-white', !active);
        chip.classList.toggle('text-gray-600', !active);
        chip.classList.toggle('border-gray-300', !active);
    });
}

function toggleTypeFilter(type) {
    if (type === 'all') {
        filters.types.clear();
    } else if (filters.types.has(type)) {
        filters.types.delete(type);
    } else {
        filters.types.add(type);
    }
    refreshFilterChips();
    renderList();
}

function setStatusFilter(status) {
    filters.status = status;
    refreshFilterChips();
    renderList();
}

function applyFilters() {
    filters.search = document.getElementById('search-input').value.trim().toLowerCase();
    refreshFilterChips();
    renderList();
}

function nodeStatus(node) {
    if (node.type === 'gateway') return node.online ? 'online' : 'offline';
    if (DEEP_SLEEP_TYPES.has(node.type)) return 'battery';
    return node.rssi !== null && node.rssi !== undefined ? 'online' : 'offline';
}

function matchesFilters(node) {
    if (filters.types.size > 0 && !filters.types.has(node.type)) return false;

    if (filters.status === 'online' && nodeStatus(node) !== 'online') return false;
    if (filters.status === 'offline' && nodeStatus(node) !== 'offline') return false;
    if (filters.status === 'battery' && nodeStatus(node) !== 'battery') return false;

    if (filters.search) {
        const haystack = `${node.label} ${node.mac || ''} ${node.room_name || ''} ${node.ip || ''}`.toLowerCase();
        if (!haystack.includes(filters.search)) return false;
    }
    return true;
}

/* ------------------------------------------------------------------ */
/* Topology map                                                         */
/* ------------------------------------------------------------------ */

function renderMap() {
    if (!meshData) return;
    const canvas = document.getElementById('map-canvas');
    const gateways = meshData.nodes.filter(n => n.type === 'gateway')
        .sort((a, b) => {
            if (!!a.is_serial !== !!b.is_serial) return a.is_serial ? -1 : 1;
            if (a.online !== b.online) return a.online ? -1 : 1;
            return (a.label || '').localeCompare(b.label || '');
        });

    if (gateways.length === 0) {
        canvas.innerHTML = '';
        return;
    }

    const gwY = 78;
    const positions = {};

    // Ring radius depends on how many devices each gateway hosts; make sure
    // the canvas is big enough that outer devices never clip.
    const ringRadiusFor = (gw) => Math.max(95, Math.min(280, meshData.nodes.filter(n => n.gateway === gw.id).length * 13 + 45));
    const maxRadius = Math.max(0, ...gateways.map(ringRadiusFor));
    const pad = 90 + maxRadius;
    const svgW = Math.max(900, gateways.length * 300 + maxRadius * 2);
    const gwStep = gateways.length > 1 ? (svgW - pad * 2) / (gateways.length - 1) : 0;

    // Gateway x positions
    gateways.forEach((gw, i) => {
        const gx = gateways.length > 1 ? pad + i * gwStep : svgW / 2;
        positions[gw.id] = { x: gx, y: gwY, gw: true };
    });

    // Device ring positions per gateway
    gateways.forEach(gw => {
        const devices = meshData.nodes.filter(n => n.gateway === gw.id);
        const radius = ringRadiusFor(gw);
        devices.forEach((dev, i) => {
            const angle = devices.length === 1 ? Math.PI / 2 : (Math.PI * (15 + 150 * i / (devices.length - 1))) / 180;
            const cx = positions[gw.id].x + radius * Math.cos(angle);
            const cy = positions[gw.id].y + radius * Math.sin(angle);
            positions[dev.id] = { x: cx, y: cy };
        });
    });

    // Unassigned devices in a bottom band
    const band = meshData.nodes.filter(n => n.type !== 'gateway' && !positions[n.id]);
    const hasBand = band.length > 0;
    const bandY = gwY + maxRadius + 90;

    const svgH = bandY + (hasBand ? 80 : 20);
    let parts = [];

    // Edges: signal strength coloring only applies to light routes; links to
    // deep-sleep devices are neutral since their RSSI is only refreshed at pairing.
    meshData.edges.forEach(edge => {
        const from = positions[edge.from];
        const to = positions[edge.to];
        if (!from || !to) return;
        const color = edge.type === 'route' ? (SIGNAL_COLORS[edge.signal] || SIGNAL_COLORS.none) : '#cbd5e1';
        parts.push(
            `<line x1="${from.x}" y1="${from.y}" x2="${to.x}" y2="${to.y}" stroke="${color}" stroke-width="1.8" opacity="0.55"/>`
        );
    });

    // Unassigned band line
    if (hasBand) {
        parts.push(`<text x="${pad}" y="${bandY - 22}" class="mesh-band-label">Unassigned devices</text>`);
        band.forEach((dev, i) => {
            const x = pad + 30 + i * 130;
            const y = bandY + 10;
            positions[dev.id] = { x, y };
        });
    }

    // Nodes
    meshData.nodes.forEach(node => {
        const pos = positions[node.id];
        if (!pos) return;
        const meta = TYPE_META[node.type] || TYPE_META.gateway;
        const isGw = node.type === 'gateway';
        const r = isGw ? 27 : 21;
        const offline = node.type === 'gateway'
            ? !node.online
            : (node.type === 'light' && (node.rssi === null || node.rssi === undefined));
        const stroke = offline ? '#9ca3af' : meta.color;
        const fill = offline ? '#f3f4f6' : '#ffffff';

        parts.push(`
            <g data-node="${node.id}" class="mesh-node">
                <line x1="${pos.x - r - 6}" y1="${pos.y}" x2="${pos.x + r + 6}" y2="${pos.y}" stroke="${stroke}" stroke-width="${isGw ? 3 : 1.5}" stroke-dasharray="2 5" opacity="0.5"/>
                <circle cx="${pos.x}" cy="${pos.y}" r="${r}" fill="${fill}" stroke="${stroke}" stroke-width="${isGw ? 3 : 2}" class="mesh-node-circle"/>
                <foreignObject x="${pos.x - 16}" y="${pos.y - 16}" width="32" height="32">
                    <div xmlns="http://www.w3.org/1999/xhtml" class="mesh-node-icon">
                        <i class="fas ${meta.icon}" style="color:${stroke}"></i>
                    </div>
                </foreignObject>
                ${isGw && node.online ? `<circle cx="${pos.x + r - 5}" cy="${pos.y - r + 5}" r="4.5" fill="#10b981" stroke="#fff" stroke-width="1.5"/>` : ''}
                <text x="${pos.x}" y="${pos.y + r + 16}" class="mesh-node-label">${esc(truncate(node.label, 20))}</text>
                ${isGw ? `<text x="${pos.x}" y="${pos.y + r + 30}" class="mesh-node-sub">${node.online ? 'Online' : 'Offline'}</text>` : ''}
                <title>${esc(node.label)} (${meta.label})</title>
            </g>
        `);
    });

    canvas.innerHTML = `
        <svg viewBox="0 0 ${svgW} ${svgH}" class="w-full" style="min-width:${svgW}px" xmlns="http://www.w3.org/2000/svg">
            ${parts.join('')}
        </svg>
    `;

    canvas.querySelectorAll('[data-node]').forEach(el => {
        el.addEventListener('click', () => {
            const node = meshData.nodes.find(n => n.id === el.dataset.node);
            if (node) openDrawer(node);
        });
    });
}

/* ------------------------------------------------------------------ */
/* Device list                                                          */
/* ------------------------------------------------------------------ */

function renderList() {
    if (!meshData) return;
    const container = document.getElementById('gateways-container');
    const gateways = meshData.nodes.filter(n => n.type === 'gateway')
        .sort((a, b) => {
            if (!!a.is_serial !== !!b.is_serial) return a.is_serial ? -1 : 1;
            if (a.online !== b.online) return a.online ? -1 : 1;
            return (a.label || '').localeCompare(b.label || '');
        });

    const cards = [];

    gateways.forEach(gw => {
        const devices = meshData.nodes.filter(n => n.type !== 'gateway' && n.gateway === gw.id);
        const shownDevices = devices.filter(d => matchesFilters(d) || matchesFilters(gw));
        if (shownDevices.length === 0 && !matchesFilters(gw)) return;
        cards.push(createGatewayCard(gw, devices.filter(d => matchesFilters(d))));
    });

    // Unassigned devices (configured but no gateway route)
    const unassigned = meshData.nodes.filter(n => n.type !== 'gateway' && !n.gateway && matchesFilters(n));
    if (unassigned.length > 0) {
        cards.push(createMiscCard('Unassigned devices', 'fa-question-circle', 'text-gray-600', 'bg-gray-100', unassigned));
    }

    container.innerHTML = cards.join('') ||
        `<div class="text-center py-10 text-gray-500 bg-white rounded-2xl shadow-lg">
            <p class="text-lg">No devices match your filters</p>
            <p class="text-sm mt-1">Try adjusting the search or filter chips.</p>
        </div>`;
}

function createGatewayCard(gateway, devices) {
    const meta = TYPE_META.gateway;
    const serialPort = gateway.serial_port || (gateway.serial_endpoint || '').replace('serial://', '');
    const isOnline = gateway.online === true;
    const counts = {};
    devices.forEach(d => { counts[d.type] = (counts[d.type] || 0) + 1; });
    const sections = ['light', 'button', 'remote', 'motion', 'door']
        .filter(t => counts[t])
        .map(t => `
            <div>
                <div class="flex items-center justify-between mb-3">
                    <h4 class="text-sm font-semibold text-gray-700 flex items-center">
                        <i class="fas ${TYPE_META[t].icon} mr-2" style="color:${TYPE_META[t].color}"></i>
                        ${TYPE_META[t].label}s
                        <span class="ml-2 text-xs ${TYPE_META[t].badge} px-2 py-0.5 rounded-full">${counts[t]}</span>
                    </h4>
                </div>
                <div class="space-y-2">
                    ${devices.filter(d => d.type === t).map(d => createDeviceRow(d)).join('')}
                </div>
            </div>
        `).join('');

    return `
        <div class="bg-white rounded-2xl shadow-lg border border-transparent hover:border-cyan-200 transition-all">
            <div onclick="openDrawer(getNodeById('${gateway.id}'))" title="Click for gateway details"
                 class="flex items-center justify-between p-6 pb-4 cursor-pointer hover:bg-cyan-50/40 transition-colors rounded-t-2xl">
                <div class="flex items-center space-x-4 min-w-0">
                    <div class="w-14 h-14 rounded-xl ${isOnline ? 'bg-cyan-500/20' : 'bg-gray-100'} flex items-center justify-center flex-shrink-0">
                        <i class="fas fa-server text-3xl ${isOnline ? 'text-cyan-600' : 'text-gray-400'}"></i>
                    </div>
                    <div class="min-w-0">
                        <div class="flex items-center space-x-2">
                            <h3 class="text-xl font-bold text-gray-900 truncate">${esc(gateway.label)}</h3>
                            ${gateway.is_serial ? '<span class="text-[10px] font-bold uppercase bg-purple-100 text-purple-700 px-2 py-0.5 rounded-full">Serial</span>' : ''}
                        </div>
                        <p class="text-sm text-gray-500 font-mono">${esc(gateway.id)}</p>
                        <p class="text-xs text-gray-400">${esc(serialPort || gateway.ip || 'No IP')}</p>
                    </div>
                </div>
                <div class="flex items-center space-x-3 flex-shrink-0">
                    <div class="text-right mr-2 hidden sm:block">
                        <div class="text-xs font-semibold ${isOnline ? 'text-green-600' : 'text-gray-500'}">
                            ${isOnline ? `Uptime ${formatUptime(gateway.uptime)}` : 'Offline'}
                        </div>
                        <div class="text-[11px] text-gray-400 mt-0.5">${Object.entries(counts).map(([t, c]) => `${c} ${TYPE_META[t].label.toLowerCase()}${c > 1 ? 's' : ''}`).join(' · ') || 'No devices'}</div>
                    </div>
                    <div class="px-3 py-1 text-xs font-semibold rounded-full ${isOnline ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-800'} inline-flex items-center">
                        <i class="fas ${isOnline ? 'fa-check-circle' : 'fa-circle'} mr-1"></i>
                        ${isOnline ? 'Online' : 'Offline'}
                    </div>
                    <button onclick="event.stopPropagation(); toggleGatewayDevices('${gateway.id}')"
                        class="text-gray-400 hover:text-gray-600 px-3 py-3 -my-3 rounded-xl hover:bg-gray-100 transition-colors" title="Collapse / expand devices">
                        <i id="gateway-${gateway.id}-icon" class="fas fa-chevron-down transition-transform"></i>
                    </button>
                </div>
            </div>
            <div id="gateway-${gateway.id}-devices" class="grid grid-cols-1 lg:grid-cols-2 gap-6 px-6 pb-6 pt-2">
                ${sections || '<p class="text-sm text-gray-400 text-center py-4 border border-dashed border-gray-200 rounded-lg">No devices connected</p>'}
            </div>
        </div>
    `;
}

function createMiscCard(title, icon, iconColor, iconBg, devices) {
    return `
        <div class="bg-white rounded-2xl shadow-lg border border-transparent hover:border-gray-200 transition-all">
            <div class="flex items-center justify-between p-6 pb-4">
                <div class="flex items-center space-x-4">
                    <div class="w-14 h-14 rounded-xl ${iconBg} flex items-center justify-center">
                        <i class="fas ${icon} text-3xl ${iconColor}"></i>
                    </div>
                    <div>
                        <h3 class="text-xl font-bold text-gray-900">${title}</h3>
                        <p class="text-xs text-gray-400">${devices.length} device${devices.length === 1 ? '' : 's'} · no gateway route yet</p>
                    </div>
                </div>
            </div>
            <div class="px-6 pb-6">
                <div class="space-y-2">
                    ${devices.map(d => createDeviceRow(d)).join('')}
                </div>
            </div>
        </div>
    `;
}

function createDeviceRow(device) {
    const meta = TYPE_META[device.type] || TYPE_META.gateway;
    const isLight = device.type === 'light';
    const rssi = isLight ? device.rssi : null;
    const rssiPercent = rssi !== null && rssi !== undefined
        ? Math.max(0, Math.min(100, Math.round(((rssi + 90) / 50) * 100)))
        : 0;

    const battery = device.battery_percent;
    const supportsBattery = DEEP_SLEEP_TYPES.has(device.type);
    const hasBattery = supportsBattery && battery !== null && battery !== undefined;
    const batteryBadge = `
        <span class="battery-tooltip inline-flex items-center text-xs font-medium ${hasBattery ? (battery >= 50 ? 'text-green-600' : battery >= 20 ? 'text-amber-600' : 'text-red-600') : 'text-gray-400'}">
            <i class="fas fa-battery-${hasBattery ? (battery >= 75 ? 'full' : battery >= 50 ? 'three-quarters' : battery >= 25 ? 'half' : battery >= 10 ? 'quarter' : 'empty') : 'half'} mr-1"></i>${hasBattery ? battery + '%' : 'N/A'}
            <span class="tooltip-content">${hasBattery ? (device.battery_mv ? `${device.battery_mv} mV` : 'Battery level') + (device.battery_last_updated ? ` · ${formatLastSeen(device.battery_last_updated)} ago` : '') : (supportsBattery ? 'No battery data' : 'Mains powered')}</span>
        </span>`;

    const signal = rssi !== null && rssi !== undefined ? `
        <div class="flex items-center space-x-2 flex-1 min-w-[100px]">
            <div class="flex-1 bg-gray-200 rounded-full h-2">
                <div class="rssi-bar ${rssiBarClass(rssi)}" style="width: ${rssiPercent}%"></div>
            </div>
            <span class="text-xs font-mono ${rssiTextClass(rssi)}">${rssi * -1} dBm</span>
        </div>` : '<span class="text-xs text-gray-400">No signal data</span>';
    const doorState = device.type === 'door' && device.state ? `
        <span class="text-[11px] font-medium px-2 py-0.5 rounded-full ${device.state === 'open' ? 'bg-green-100 text-green-700' : device.state === 'closed' ? 'bg-gray-100 text-gray-600' : 'bg-gray-100 text-gray-400'}">
            ${device.state === 'open' ? 'Open' : device.state === 'closed' ? 'Closed' : 'Unknown'}
        </span>` : '';

    return `
        <div class="border border-gray-200 rounded-lg p-3 hover:shadow-md transition-all hover:border-gray-300 cursor-pointer" onclick="openDrawer(getNodeById('${device.id}'))">
            <div class="flex items-center justify-between mb-2 gap-3">
                <div class="flex items-center space-x-2 min-w-0">
                    <i class="fas ${meta.icon}" style="color:${meta.color}"></i>
                    <span class="text-sm font-medium text-gray-900 truncate">${esc(device.label)}</span>
                    ${doorState}
                    ${device.type === 'light' ? `
                        <button onclick="event.stopPropagation(); findBestGateway('${device.id}')" class="text-xs text-cyan-600 hover:text-cyan-800 font-medium flex-shrink-0">
                            <i class="fas fa-search-location mr-1"></i>Find Best
                        </button>` : ''}
                </div>
                <div class="flex items-center space-x-3 flex-shrink-0">${batteryBadge}</div>
            </div>
            <div class="flex items-center justify-between gap-3">
                <div class="min-w-0">
                    <p class="text-xs text-gray-500 font-mono truncate">${esc(device.id)}</p>
                    <p class="text-[11px] text-gray-400 truncate">
                        ${device.room_name ? `<i class="fas fa-house mr-1"></i>${esc(device.room_name)}` : ''}
                        ${device.last_seen ? `${device.room_name ? ' · ' : ''}${formatLastSeen(device.last_seen)} ago` : ''}
                    </p>
                </div>
                ${isLight ? signal : ''}
            </div>
        </div>
    `;
}

/* ------------------------------------------------------------------ */
/* Detail drawer                                                        */
/* ------------------------------------------------------------------ */

function getNodeById(id) {
    return meshData.nodes.find(n => n.id === id);
}

function openDrawer(node) {
    if (!node) return;
    const meta = TYPE_META[node.type] || TYPE_META.gateway;
    const isOnline = node.type === 'gateway' ? node.online : (node.type === 'light' && node.rssi !== null && node.rssi !== undefined);

    const iconEl = document.getElementById('drawer-icon');
    iconEl.className = `w-12 h-12 rounded-xl flex items-center justify-center text-2xl ${isOnline ? 'bg-white' : 'bg-gray-100'}`;
    iconEl.innerHTML = `<i class="fas ${meta.icon}" style="color:${isOnline ? meta.color : '#9ca3af'}"></i>`;

    document.getElementById('drawer-name').textContent = node.label;
    document.getElementById('drawer-mac').textContent = node.id;

    const rows = [];

    if (node.type === 'gateway') {
        rows.push(detailRow('Status', node.online ? 'Online' : 'Offline', node.online ? 'fa-circle-check text-green-500' : 'fa-circle text-gray-400'));
        if (node.uptime !== null && node.uptime !== undefined) rows.push(detailRow('Uptime', formatUptime(node.uptime), 'fa-clock'));
        rows.push(detailRow('IP address', node.ip, 'fa-network-wired'));
        if (node.wifi_mac) rows.push(detailRow('WiFi MAC', node.wifi_mac, 'fa-wifi'));
        if (node.serial_port) rows.push(detailRow('Serial port', node.serial_port, 'fa-plug'));
        if (node.version_net) rows.push(detailRow('Net firmware', `${node.is_serial ? '' : 'v'}${node.version_net}`, 'fa-microchip'));
        if (node.version_radio) rows.push(detailRow('Radio firmware', `${node.is_serial ? '' : 'v'}${node.version_radio}`, 'fa-microchip'));
        if (node.last_used) rows.push(detailRow('Last used', formatLastSeen(node.last_used) + ' ago', 'fa-clock-rotate-left'));
    } else {
        if (node.type === 'light') {
            rows.push(detailRow('Status', node.rssi !== null ? 'Online' : 'Offline', node.rssi !== null ? 'fa-circle-check text-green-500' : 'fa-circle text-gray-400'));
            rows.push(detailRow('Signal', rssiLabel(node.rssi), node.rssi !== null ? 'fa-signal' : 'fa-signal-slash'));
            rows.push(detailRow('LEDs', `${node.num_leds}`, 'fa-bars'));
        }
        if (node.room_name) rows.push(detailRow('Room', node.room_name, 'fa-house'));
        if (node.configured !== undefined) rows.push(detailRow('Configured', node.configured ? 'Yes' : 'No', node.configured ? 'fa-circle-check text-green-500' : 'fa-circle-xmark text-gray-400'));
        if (node.type === 'remote' && node.button_count) rows.push(detailRow('Buttons', `${node.button_count}`, 'fa-circle'));
        if (node.type === 'door') rows.push(detailRow('State', node.state || 'Unknown', node.state === 'open' ? 'fa-door-open text-green-500' : 'fa-door-closed'));
        if (node.type === 'motion' && node.light_level !== undefined && node.light_level !== null) rows.push(detailRow('Light level', `${node.light_level}`, 'fa-sun'));
        if (node.battery_percent !== null && node.battery_percent !== undefined) {
            rows.push(detailRow('Battery', `${node.battery_percent}%${node.battery_mv ? ` (${node.battery_mv} mV)` : ''}`, batteryIconClass(node.battery_percent)));
        }
        if (node.version) rows.push(detailRow('Firmware', `v${node.version}`, 'fa-microchip'));
        if (node.platform) rows.push(detailRow('Platform', node.platform, 'fa-microchip'));
        if (node.last_seen) rows.push(detailRow('Last seen', `${formatLastSeen(node.last_seen)} ago`, 'fa-clock-rotate-left'));
        if (node.gateway) {
            const gw = getNodeById(node.gateway);
            rows.push(detailRow('Via gateway', gw ? gw.label : node.gateway, 'fa-server'));
        }
    }

    document.getElementById('drawer-body').innerHTML = rows.join('') ||
        '<p class="text-sm text-gray-400">No details available</p>';

    // Actions
    const actions = document.getElementById('drawer-actions');
    let buttons = '';

    if (RENAME_ENDPOINTS[node.type] && node.device_id) {
        buttons += `<button onclick="openRenameModal('${node.type}', '${escAttr(node.device_id)}', '${escAttr(node.label)}')" class="px-4 py-2 border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50 transition-colors text-sm inline-flex items-center">
            <i class="fas fa-edit mr-2"></i>Rename
        </button>`;
    }
    if (node.type === 'light') {
        buttons += `<button onclick="findBestGateway('${node.id}')" class="px-4 py-2 bg-cyan-600 text-white rounded-lg hover:bg-cyan-700 transition-colors text-sm inline-flex items-center">
            <i class="fas fa-search-location mr-2"></i>Find Best
        </button>`;
    }
    if (MANAGE_LINKS[node.type]) {
        buttons += `<a href="${MANAGE_LINKS[node.type]}" class="px-4 py-2 border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50 transition-colors text-sm inline-flex items-center">
            <i class="fas fa-external-link-alt mr-2"></i>Manage
        </a>`;
    }

    actions.innerHTML = buttons || '';

    document.getElementById('drawer-overlay').classList.remove('hidden');
    document.getElementById('detail-drawer').classList.remove('translate-x-full');
}

function closeDrawer() {
    document.getElementById('drawer-overlay').classList.add('hidden');
    document.getElementById('detail-drawer').classList.add('translate-x-full');
}

function detailRow(label, value, icon) {
    if (value === null || value === undefined || value === '') return '';
    return `
        <div class="flex items-start justify-between gap-4">
            <span class="text-sm text-gray-500 flex items-center"><i class="fas ${icon || 'fa-circle-info'} mr-2 w-4 text-gray-400"></i>${label}</span>
            <span class="text-sm font-medium text-gray-900 text-right">${esc(String(value))}</span>
        </div>`;
}

/* ------------------------------------------------------------------ */
/* Rename                                                               */
/* ------------------------------------------------------------------ */

function openRenameModal(type, id, currentName) {
    renameTarget = { type, id };
    document.getElementById('rename-input').value = currentName;
    document.getElementById('rename-modal').classList.remove('hidden');
    setTimeout(() => document.getElementById('rename-input').focus(), 100);
}

function closeRenameModal() {
    document.getElementById('rename-modal').classList.add('hidden');
    renameTarget = null;
}

function confirmRename() {
    const name = document.getElementById('rename-input').value.trim();
    if (!renameTarget || !name) return;

    const ep = RENAME_ENDPOINTS[renameTarget.type];
    const btn = document.querySelector('#rename-modal button');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner mr-2" style="--spinner-size:16px"></span>Renaming...';

    fetch(ep.url(renameTarget.id), {
        method: ep.method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name })
    })
    .then(response => response.json())
    .then(data => {
        closeRenameModal();
        if (data.success) {
            showToast('Renamed', 'Device name updated', 'success');
            loadMeshData();
        } else {
            showToast('Rename Failed', data.error || 'Could not rename device', 'error');
        }
    })
    .catch(err => {
        closeRenameModal();
        showToast('Error', 'Failed to rename device', 'error');
    });
}

/* ------------------------------------------------------------------ */
/* Optimization                                                         */
/* ------------------------------------------------------------------ */

function findBestGateway(lightId) {
    showToast('Optimizing...', 'Finding best gateway for light', 'info');
    fetch('/mesh/api/mesh/optimize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mac: lightId })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success && data.best_gateway && data.best_gateway.rssi !== null && data.best_gateway.rssi !== undefined) {
            const best = data.best_gateway;
            showToast('Optimization Complete',
                `${best.name} (${best.rssi * -1} dBm)${data.routing_updated ? ' — routing updated' : ''}`,
                'success');
        } else if (data.success) {
            showToast('No Response', 'Light did not respond to any gateway', 'warning');
        } else {
            showToast('Optimization Failed', data.error || 'Could not find best gateway', 'error');
        }
        setTimeout(() => loadMeshData(), 1200);
    })
    .catch(err => {
        console.error('Error optimizing light:', err);
        showToast('Error', 'Failed to optimize light routing', 'error');
    });
}

function optimizeAllLights() {
    if (optimizing) return;
    const lights = meshData.nodes.filter(n => n.type === 'light');
    if (lights.length === 0) {
        showToast('No Lights', 'No lights found to optimize', 'warning');
        return;
    }

    optimizing = true;
    document.getElementById('optimize-results').innerHTML = '';
    document.getElementById('optimize-progress').style.width = '0%';
    document.getElementById('optimize-status').textContent = `Testing ${lights.length} lights across all gateways...`;
    document.getElementById('optimize-modal').classList.remove('hidden');

    let completed = 0;
    let succeeded = 0;

    const optimizeNext = (index) => {
        if (index >= lights.length) {
            document.getElementById('optimize-progress').style.width = '100%';
            document.getElementById('optimize-status').textContent =
                `Complete — ${succeeded} of ${lights.length} lights optimized`;
            optimizing = false;
            showToast('Optimization Complete', `Successfully optimized ${succeeded} lights`, 'success');
            setTimeout(() => loadMeshData(), 1200);
            return;
        }

        const light = lights[index];
        document.getElementById('optimize-status').textContent = `Testing ${light.label} (${index + 1}/${lights.length})...`;

        fetch('/mesh/api/mesh/optimize', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mac: light.id })
        })
        .then(response => response.json())
        .then(data => {
            const row = document.createElement('div');
            if (data.success && data.best_gateway && data.best_gateway.rssi !== null && data.best_gateway.rssi !== undefined) {
                succeeded++;
                const best = data.best_gateway;
                row.className = 'flex items-center justify-between p-3 border border-gray-200 rounded-lg';
                row.innerHTML = `
                    <div class="flex items-center space-x-2 min-w-0">
                        <i class="fas fa-lightbulb text-amber-500 flex-shrink-0"></i>
                        <span class="text-sm font-medium text-gray-900 truncate">${esc(light.label)}</span>
                    </div>
                    <div class="flex items-center space-x-3 flex-shrink-0">
                        <span class="text-xs text-gray-500">${esc(best.name)}</span>
                        <span class="text-xs font-mono font-semibold ${rssiTextClass(best.rssi)}">${best.rssi * -1} dBm</span>
                        ${data.routing_updated
                            ? '<span class="text-[10px] font-bold uppercase bg-green-100 text-green-700 px-2 py-0.5 rounded-full">Updated</span>'
                            : '<span class="text-[10px] font-bold uppercase bg-gray-100 text-gray-600 px-2 py-0.5 rounded-full">Kept</span>'}
                    </div>`;
            } else {
                row.className = 'flex items-center justify-between p-3 border border-red-200 bg-red-50 rounded-lg';
                row.innerHTML = `
                    <div class="flex items-center space-x-2 min-w-0">
                        <i class="fas fa-lightbulb text-gray-400 flex-shrink-0"></i>
                        <span class="text-sm font-medium text-gray-700 truncate">${esc(light.label)}</span>
                    </div>
                    <span class="text-xs text-red-500 flex-shrink-0">${data.success ? 'No response' : (data.error || 'Failed')}</span>`;
            }
            document.getElementById('optimize-results').appendChild(row);
            completed++;
            document.getElementById('optimize-progress').style.width = `${Math.round((completed / lights.length) * 100)}%`;
            optimizeNext(index + 1);
        })
        .catch(err => {
            console.error(`Error optimizing ${light.label}:`, err);
            const row = document.createElement('div');
            row.className = 'flex items-center justify-between p-3 border border-red-200 bg-red-50 rounded-lg';
            row.innerHTML = `
                <div class="flex items-center space-x-2 min-w-0">
                    <i class="fas fa-lightbulb text-gray-400 flex-shrink-0"></i>
                    <span class="text-sm font-medium text-gray-700 truncate">${esc(light.label)}</span>
                </div>
                <span class="text-xs text-red-500 flex-shrink-0">Error</span>`;
            document.getElementById('optimize-results').appendChild(row);
            completed++;
            document.getElementById('optimize-progress').style.width = `${Math.round((completed / lights.length) * 100)}%`;
            optimizeNext(index + 1);
        });
    };

    optimizeNext(0);
}

function closeOptimizeModal() {
    if (optimizing) {
        showToast('Optimization', 'Optimization is still running in the background', 'info');
        return;
    }
    document.getElementById('optimize-modal').classList.add('hidden');
}

/* ------------------------------------------------------------------ */
/* Small helpers                                                        */
/* ------------------------------------------------------------------ */

function toggleGatewayDevices(gatewayId) {
    const devicesSection = document.getElementById(`gateway-${gatewayId}-devices`);
    const icon = document.getElementById(`gateway-${gatewayId}-icon`);
    if (!devicesSection || !icon) return;

    if (devicesSection.classList.contains('hidden')) {
        devicesSection.classList.remove('hidden');
        icon.classList.remove('fa-chevron-right');
        icon.classList.add('fa-chevron-down');
    } else {
        devicesSection.classList.add('hidden');
        icon.classList.remove('fa-chevron-down');
        icon.classList.add('fa-chevron-right');
    }
}

function refreshMesh() {
    const refreshIcon = document.getElementById('refresh-icon');
    refreshIcon.classList.add('rotate-360');
    loadMeshData();
    setTimeout(() => refreshIcon.classList.remove('rotate-360'), 500);
}

function rssiLabel(rssi) {
    if (rssi === null || rssi === undefined) return 'No data';
    return `${rssi * -1} dBm (${rssi >= -55 ? 'strong' : rssi >= -70 ? 'weak' : 'poor'})`;
}

function rssiBarClass(rssi) {
    if (rssi >= -55) return 'bg-green-500';
    if (rssi >= -70) return 'bg-yellow-500';
    return 'bg-red-500';
}

function rssiTextClass(rssi) {
    if (rssi >= -55) return 'text-green-700';
    if (rssi >= -70) return 'text-yellow-700';
    return 'text-red-700';
}

function batteryIconClass(percent) {
    if (percent >= 50) return 'fa-battery-three-quarters text-green-600';
    if (percent >= 20) return 'fa-battery-half text-amber-600';
    return 'fa-battery-quarter text-red-600';
}

function formatUptime(totalSeconds) {
    if (totalSeconds === null || totalSeconds === undefined) return '';
    const seconds = Math.max(0, Number(totalSeconds) || 0);
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days > 0) return `${days}d ${hours}h`;
    if (hours > 0) return `${hours}h ${minutes}m`;
    return `${minutes}m`;
}

function formatLastSeen(iso) {
    if (!iso) return '';
    const date = new Date(iso);
    if (isNaN(date.getTime())) return '';
    const diff = Math.max(0, (Date.now() - date.getTime()) / 1000);
    if (diff < 60) return 'just now';
    if (diff < 3600) return `${Math.floor(diff / 60)}m`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
    if (diff < 86400 * 30) return `${Math.floor(diff / 86400)}d`;
    return date.toLocaleDateString();
}

function truncate(str, max) {
    str = String(str || '');
    return str.length > max ? str.slice(0, max - 1) + '…' : str;
}

function esc(value) {
    return String(value === null || value === undefined ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function escAttr(value) {
    return esc(value).replace(/\n/g, '\\n');
}