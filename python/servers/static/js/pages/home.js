// Load status information on page load
window.addEventListener('DOMContentLoaded', function() {
    loadSystemStatus();
});

function loadSystemStatus() {
    // Check Hue Bridge status
    fetch('/api/status/bridge')
        .then(response => response.json())
        .then(data => {
            const statusEl = document.getElementById('bridge-status');
            const infoEl = document.getElementById('bridge-info');
            
            if (data.configured && data.connected) {
                statusEl.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-green-100 text-green-600';
                statusEl.innerHTML = '<i class="fas fa-check-circle mr-1"></i>Connected';
                infoEl.textContent = `${data.ip} - ${data.name || 'Hue Bridge'}`;
                
                // Load counts since bridge is connected
                loadOverviewCounts();
            } else if (data.configured && !data.connected) {
                statusEl.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-red-100 text-red-600';
                statusEl.innerHTML = '<i class="fas fa-times-circle mr-1"></i>Offline';
                infoEl.textContent = `${data.ip} - Bridge not responding`;
            } else {
                statusEl.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-yellow-100 text-yellow-600';
                statusEl.innerHTML = '<i class="fas fa-exclamation-triangle mr-1"></i>Not Configured';
                infoEl.textContent = 'Please configure bridge connection';
            }
        })
        .catch(err => {
            console.error('Error loading bridge status:', err);
            document.getElementById('bridge-status').className = 'px-3 py-1 text-xs font-semibold rounded-full bg-gray-100 text-gray-600';
            document.getElementById('bridge-status').innerHTML = '<i class="fas fa-question-circle mr-1"></i>Unknown';
            document.getElementById('bridge-info').textContent = 'Unable to check status';
        });

    // Check UDP Network status
    fetch('/api/status/udp')
        .then(response => response.json())
        .then(data => {
            const statusEl = document.getElementById('udp-status');
            const infoEl = document.getElementById('udp-info');
            
            if (data.running) {
                statusEl.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-green-100 text-green-600';
                statusEl.innerHTML = '<i class="fas fa-check-circle mr-1"></i>Running';
                infoEl.textContent = `Port ${data.port} • ${data.gateways || 0} gateways`;
            } else {
                statusEl.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-red-100 text-red-600';
                statusEl.innerHTML = '<i class="fas fa-times-circle mr-1"></i>Stopped';
                infoEl.textContent = 'UDP network is not running';
            }
        })
        .catch(err => {
            console.error('Error loading UDP status:', err);
            document.getElementById('udp-status').className = 'px-3 py-1 text-xs font-semibold rounded-full bg-gray-100 text-gray-600';
            document.getElementById('udp-status').innerHTML = '<i class="fas fa-question-circle mr-1"></i>Unknown';
            document.getElementById('udp-info').textContent = 'Unable to check status';
        });

    // Check Automation Engine status
    fetch('/api/status/automation')
        .then(response => response.json())
        .then(data => {
            const statusEl = document.getElementById('automation-status');
            const infoEl = document.getElementById('automation-info');

            if (data.initialized && data.running) {
                statusEl.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-green-100 text-green-600';
                statusEl.innerHTML = '<i class="fas fa-check-circle mr-1"></i>Running';
                infoEl.textContent = data.info || 'Automation engine running';
            } else if (data.initialized && !data.running) {
                statusEl.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-yellow-100 text-yellow-600';
                statusEl.innerHTML = '<i class="fas fa-exclamation-triangle mr-1"></i>Initialized';
                infoEl.textContent = data.info || 'Initialized but stopped';
            } else {
                statusEl.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-red-100 text-red-600';
                statusEl.innerHTML = '<i class="fas fa-times-circle mr-1"></i>Inactive';
                infoEl.textContent = data.info || 'Automation engine not initialized';
            }
        })
        .catch(err => {
            console.error('Error loading automation engine status:', err);
            document.getElementById('automation-status').className = 'px-3 py-1 text-xs font-semibold rounded-full bg-gray-100 text-gray-600';
            document.getElementById('automation-status').innerHTML = '<i class="fas fa-question-circle mr-1"></i>Unknown';
            document.getElementById('automation-info').textContent = 'Unable to check status';
        });
}

function loadOverviewCounts() {
    fetch('/api/overview/counts')
        .then(response => response.json())
        .then(data => {
            if (data.success && data.counts) {
                document.getElementById('room-count').textContent = data.counts.rooms;
                document.getElementById('light-count').textContent = data.counts.lights;
                document.getElementById('scene-count').textContent = data.counts.scenes;
            }
        })
        .catch(err => {
            console.error('Error loading overview counts:', err);
        });
}
