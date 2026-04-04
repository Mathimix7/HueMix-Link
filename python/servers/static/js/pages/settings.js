let pendingAction = null;

    // Load initial data
    document.addEventListener('DOMContentLoaded', function() {
        loadSettings();
        loadSerialGatewayPorts();
        loadSerialGatewaySettings();
        loadBackupList();
    });

    function loadSerialGatewayPorts(preferredPort = null) {
        const select = document.getElementById('serial-gateway-port');
        const currentValue = preferredPort !== null ? preferredPort : (select.value || '');

        fetch('/admin/api/serial-ports')
            .then(r => r.json())
            .then(data => {
                if (!data.success) {
                    throw new Error(data.error || 'Failed to load serial ports');
                }

                const ports = Array.isArray(data.ports) ? data.ports : [];
                const configuredPort = data.configured_port || '';
                const selectedPort = currentValue || configuredPort;

                select.innerHTML = '';

                if (!ports.length) {
                    const option = document.createElement('option');
                    option.value = '';
                    option.textContent = 'No serial ports detected';
                    select.appendChild(option);
                    return;
                }

                const placeholder = document.createElement('option');
                placeholder.value = '';
                placeholder.textContent = 'Select a serial port';
                select.appendChild(placeholder);

                ports.forEach(port => {
                    const option = document.createElement('option');
                    const device = port.device || '';
                    option.value = device;

                    const baseLabel = `${device}${port.description ? ` - ${port.description}` : ''}`;
                    option.textContent = port.available ? baseLabel : `${baseLabel}`;

                    select.appendChild(option);
                });

                if (selectedPort) {
                    select.value = selectedPort;
                }
            })
            .catch(err => {
                console.error('Error loading serial ports:', err);
                select.innerHTML = '<option value="">Failed to load ports</option>';
            });
    }

    function loadSettings() {
        fetch('/api/config')
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    document.getElementById('udp-port-input').value = data.config.udp_port || 7777;
                }
            })
            .catch(err => {
                console.error('Error loading settings:', err);
            });
        
        // Load dev mode setting
        fetch('/admin/api/dev-mode')
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    document.getElementById('dev-mode-toggle').checked = data.dev_mode;
                }
            })
            .catch(err => {
                console.error('Error loading dev mode:', err);
            });
    }

    function loadSerialGatewaySettings() {
        fetch('/admin/api/serial-gateway')
            .then(r => r.json())
            .then(data => {
                if (data.success && data.serial_gateway) {
                    const cfg = data.serial_gateway;
                    document.getElementById('serial-gateway-enabled').checked = cfg.enabled || false;
                    document.getElementById('serial-gateway-baudrate').value = cfg.baudrate || 460800;
                    loadSerialGatewayPorts(cfg.port || '');
                    
                    // Update UI visibility
                    updateSerialGatewayUI();
                }
            })
            .catch(err => {
                console.error('Error loading serial gateway settings:', err);
            });
    }

    function onSerialGatewayEnabledChange() {
        if (document.getElementById('serial-gateway-enabled').checked) {
            loadSerialGatewayPorts();
        }
        updateSerialGatewayUI();
    }

    function updateSerialGatewayUI() {
        const enabled = document.getElementById('serial-gateway-enabled').checked;
        document.getElementById('serial-port-container').classList.toggle('hidden', !enabled);
        // document.getElementById('serial-baudrate-container').classList.toggle('hidden', !enabled);
    }

    function saveSerialGatewaySettings() {
        const enabled = document.getElementById('serial-gateway-enabled').checked;
        const port = document.getElementById('serial-gateway-port').value.trim();
        const baudrate = parseInt(document.getElementById('serial-gateway-baudrate').value);

        // Validate
        if (enabled && !port) {
            showToast('Serial port is required when enabling USB serial gateway', 'error');
            return;
        }

        showToast('Saving serial gateway settings...', 'info');

        fetch('/admin/api/serial-gateway', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                enabled: enabled,
                port: port,
                baudrate: baudrate
            })
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                loadSerialGatewayPorts(data.serial_gateway?.port || null);
                showToast(data.message || 'Serial gateway settings applied.', 'success');
            } else {
                showToast('Error: ' + (data.error || 'Failed to save settings'), 'error');
            }
        })
        .catch(err => {
            console.error('Error saving serial gateway settings:', err);
            showToast('Error saving settings', 'error');
        });
    }

    function saveAndRestartSettings() {
        const udpPort = parseInt(document.getElementById('udp-port-input').value);
        const devModeEnabled = document.getElementById('dev-mode-toggle').checked;
        
        if (isNaN(udpPort) || udpPort < 1 || udpPort > 65535) {
            showSettingsStatus('UDP port must be between 1 and 65535', 'error');
            return;
        }
        
        showToast('Saving settings and restarting servers...', 'info');
        
        // Save dev mode first
        fetch('/admin/api/dev-mode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                enabled: devModeEnabled
            })
        })
        .then(response => response.json())
        .then(devData => {
            if (!devData.success) {
                showToast('Error saving dev mode: ' + (devData.error || 'Unknown error'), 'error');
                return;
            }
            
            // Then save UDP port
            fetch('/api/config/restart', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    udp_port: udpPort
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showToast(data.message || 'Settings saved successfully!', 'success');
                    setTimeout(() => window.location.reload(), 2000);
                } else {
                    showToast('Error: ' + (data.error || 'Failed to save settings'), 'error');
                }
            })
            .catch(err => {
                console.error('Error saving settings:', err);
                showToast('Error saving settings', 'error');
            });
        })
        .catch(err => {
            console.error('Error saving dev mode:', err);
            showToast('Error saving dev mode', 'error');
        });
    }

    function showSettingsStatus(message, type = 'info') {
        showToast(message, type);
    }

    function loadBackupList() {
        fetch('/admin/backups')
            .then(r => r.json())
            .then(data => {
                const listEl = document.getElementById('backup-list');
                if (data.success && data.backups && data.backups.length > 0) {
                    listEl.innerHTML = data.backups.map(b => `
                        <div class="flex items-center justify-between p-3 bg-gray-50 rounded-lg hover:bg-gray-100 transition-colors">
                            <div class="flex-1">
                                <div class="font-medium text-gray-900">${b.name}</div>
                                <div class="text-sm text-gray-600">${formatSize(b.size || 0)} • ${formatDate(b.mtime)}</div>
                            </div>
                            <div class="flex gap-2">
                                <button onclick="downloadBackup('${b.name}')" 
                                        class="px-3 py-1.5 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 transition-colors">
                                    <i class="fas fa-download mr-1"></i>Download
                                </button>
                                <button onclick="confirmRestoreBackup('${b.name}')" 
                                        class="px-3 py-1.5 bg-gray-600 text-white text-sm rounded-lg hover:bg-gray-700 transition-colors">
                                    <i class="fas fa-undo mr-1"></i>Restore
                                </button>
                                <button onclick="confirmDeleteBackup('${b.name}')" 
                                        class="px-3 py-1.5 bg-red-600 text-white text-sm rounded-lg hover:bg-red-700 transition-colors">
                                    <i class="fas fa-trash-alt mr-1"></i>Delete
                                </button>
                            </div>
                        </div>
                    `).join('');
                } else {
                    listEl.innerHTML = '<p class="text-sm text-gray-600 text-center py-4">No backups available</p>';
                }
            })
            .catch(err => {
                showToast('Failed to load backups', 'error');
            });
    }

    function createBackup() {
        showToast('Creating backup...', 'info');
        fetch('/admin/backups', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({})
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                showToast('Backup created successfully!', 'success');
                loadBackupList();
                const filename = data.path.split('/').pop().split('\\').pop();
                setTimeout(() => downloadBackup(filename), 500);
            } else {
                showToast('Failed to create backup: ' + (data.error || 'Unknown error'), 'error');
            }
        })
        .catch(err => {
            showToast('Error creating backup', 'error');
        });
    }

    function downloadBackup(filename) {
        window.location.href = `/admin/backups/download/${filename}`;
    }

    function handleBackupFileSelected(input) {
        if (input.files && input.files[0]) {
            const file = input.files[0];
            confirmRestoreFromFile(file);
        }
    }

    function confirmRestoreFromFile(file) {
        showModal(
            'Restore from Uploaded Backup?',
            `This will replace all current settings with data from "${file.name}". All current devices and settings will be lost. Are you sure?`,
            () => restoreFromFile(file)
        );
    }

    function restoreFromFile(file) {
        showToast('Uploading and restoring backup...', 'info');
        const formData = new FormData();
        formData.append('file', file);
        
        fetch('/admin/backups/upload-restore', {
            method: 'POST',
            body: formData
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                showToast('Backup restored successfully!', 'success');
                setTimeout(() => location.reload(), 3000);
            } else {
                showToast('Failed to restore: ' + (data.error || 'Unknown error'), 'error');
            }
        })
        .catch(err => {
            showToast('Error restoring backup', 'error');
        });
    }

    function confirmRestoreBackup(filename) {
        showModal(
            'Restore Backup?',
            `This will replace all current settings with data from "${filename}". All current devices and settings will be lost. Are you sure?`,
            () => restoreBackup(filename)
        );
    }

    function restoreBackup(filename) {
        showToast('Restoring backup...', 'info');
        fetch('/admin/backups/restore', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({filename: filename})
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                showToast('Backup restore successfully!', 'info');
                setTimeout(() => location.reload(), 3000);
            } else {
                showToast('Failed to restore: ' + (data.error || 'Unknown error'), 'error');
            }
        })
        .catch(err => {
            showToast('Error restoring backup', 'error');
        });
    }

    function confirmDeleteBackup(filename) {
        showModal(
            'Delete Backup?',
            `Are you sure you want to delete the backup file "${filename}"? This action cannot be undone!`,
            () => deleteBackup(filename)
        );
    }

    function deleteBackup(filename) {
        showToast('Deleting backup...', 'info');
        fetch('/admin/backups/delete', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({filename: filename})
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                showToast('Backup deleted successfully!', 'success');
                loadBackupList();
            } else {
                showToast('Failed to delete: ' + (data.error || 'Unknown error'), 'error');
            }
        })
        .catch(err => {
            showToast('Error deleting backup', 'error');
        });
    }

    function showModal(title, message, onConfirm) {
        document.getElementById('modal-title').textContent = title;
        document.getElementById('modal-message').textContent = message;
        pendingAction = onConfirm;
        document.getElementById('confirm-modal').classList.remove('hidden');
    }

    function closeModal() {
        document.getElementById('confirm-modal').classList.add('hidden');
        pendingAction = null;
    }

    function confirmAction() {
        setTimeout(closeModal, 100);
        if (pendingAction) {
            pendingAction();
        }
    }

    function showToast(message, type = 'success') {
        const toast = document.getElementById('toast');
        let bgColor, textColor, icon;
        
        if (type === 'success') {
            bgColor = 'bg-green-50 border-green-200';
            textColor = 'text-green-800';
            icon = 'fa-check-circle text-green-600';
        } else if (type === 'error') {
            bgColor = 'bg-red-50 border-red-200';
            textColor = 'text-red-800';
            icon = 'fa-exclamation-circle text-red-600';
        } else if (type === 'info') {
            bgColor = 'bg-blue-50 border-blue-200';
            textColor = 'text-blue-800';
            icon = 'fa-info-circle text-blue-600';
        } else if (type === 'warning') {
            bgColor = 'bg-yellow-50 border-yellow-200';
            textColor = 'text-yellow-800';
            icon = 'fa-exclamation-triangle text-yellow-600';
        }
        
        toast.className = `fixed top-4 right-4 z-50 max-w-sm ${bgColor} border rounded-lg shadow-lg p-4`;
        toast.innerHTML = `
            <div class="flex items-center">
                <i class="fas ${icon} mr-3"></i>
                <p class="text-sm ${textColor}">${message}</p>
            </div>
        `;
        toast.classList.remove('hidden');
        setTimeout(() => {
            toast.classList.add('hidden');
        }, 3000);
    }

    function formatSize(bytes) {
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
    }

    function formatDate(timestamp) {
        if (!timestamp) return 'Unknown';
        const date = new Date(timestamp * 1000);
        return date.toLocaleString();
    }
