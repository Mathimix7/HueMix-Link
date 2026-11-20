// Load status information on page load
window.addEventListener('DOMContentLoaded', function() {
    loadSystemStatus();
    setInterval(loadSystemStatus, 1000);
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

    // Check system metrics status
    fetch('/api/status/system')
        .then(response => response.json())
        .then(data => {
            const statusEl = document.getElementById('system-status');
            const infoEl = document.getElementById('system-info');

            if (data.success) {
                const cpu = typeof data.cpu_percent === 'number' ? `${data.cpu_percent.toFixed(1)}%` : '--';
                const ram = typeof data.ram_percent === 'number' ? `${data.ram_percent.toFixed(1)}%` : '--';
                const temp = typeof data.temperature_c === 'number' ? `${data.temperature_c.toFixed(1)} C` : 'N/A';

                statusEl.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-green-100 text-green-600';
                statusEl.innerHTML = '<i class="fas fa-check-circle mr-1"></i>Live';
                infoEl.textContent = `CPU ${cpu} • RAM ${ram} • Temp ${temp}`;
            } else {
                statusEl.className = 'px-3 py-1 text-xs font-semibold rounded-full bg-yellow-100 text-yellow-600';
                statusEl.innerHTML = '<i class="fas fa-exclamation-triangle mr-1"></i>Unavailable';
                infoEl.textContent = data.error || 'System metrics unavailable';
            }
        })
        .catch(err => {
            console.error('Error loading system metrics:', err);
            document.getElementById('system-status').className = 'px-3 py-1 text-xs font-semibold rounded-full bg-gray-100 text-gray-600';
            document.getElementById('system-status').innerHTML = '<i class="fas fa-question-circle mr-1"></i>Unknown';
            document.getElementById('system-info').textContent = 'Unable to check status';
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
