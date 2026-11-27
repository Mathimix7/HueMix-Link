let pendingAction = null;
let pendingCancelAction = null;
let securityReadiness = {
    all_gateways_v2_ready: true,
    outdated_gateways: [],
    v1_devices: [],
    v1_device_count: 0,
    v2_devices: [],
    v2_device_count: 0,
};

    // Load initial data
    document.addEventListener('DOMContentLoaded', function() {
        loadSettings();
        loadSerialGatewayPorts();
        loadSerialGatewaySettings();
        loadSecuritySettings();
        loadSavedSecurityPins();
        loadBackupList();
    });

    function loadSecuritySettings() {
        fetch('/api/security/flags')
            .then(r => r.json())
            .then(data => {
                if (!data.success) return;
                applySecurityState(data.flags || {}, data.readiness || securityReadiness);
            })
            .catch(err => {
                console.error('Error loading security flags:', err);
            });
    }

    function applySecurityState(flags, readiness) {
        securityReadiness = readiness || securityReadiness;
        const v2On = Boolean(flags.security_v2_pair && flags.security_v2_send);
        const gatewaysReady = Boolean(securityReadiness.all_gateways_v2_ready);

        const v2Toggle = document.getElementById('security-v2-enabled');
        const reqV2Toggle = document.getElementById('security-require-v2');
        const allowV1Toggle = document.getElementById('security-allow-v1');
        const gwBanner = document.getElementById('security-v2-gateway-banner');
        const gwListEl = document.getElementById('security-v2-outdated-list');
        const v1HintEl = document.getElementById('security-v1-count-hint');

        if (!v2Toggle || !reqV2Toggle || !allowV1Toggle) return;

        v2Toggle.checked = v2On;
        // Prevent enabling v2 while any gateway is < 4.0.0 (still allow disabling if already on)
        v2Toggle.disabled = !gatewaysReady && !v2On;

        reqV2Toggle.checked = Boolean(flags.require_v2);
        reqV2Toggle.disabled = !v2On;

        allowV1Toggle.checked = flags.allow_v1 !== false;

        if (!gatewaysReady && (securityReadiness.outdated_gateways || []).length > 0) {
            const desc = securityReadiness.outdated_gateways
                .map(g => `${g.name} (${g.role} v${g.version})`)
                .join(', ');
            gwListEl.textContent = `Outdated gateway(s): ${desc}`;
            gwBanner.classList.remove('hidden');
        } else {
            gwBanner.classList.add('hidden');
        }

        const v1Count = securityReadiness.v1_device_count || 0;
        if (v1Count > 0) {
            v1HintEl.textContent = `${v1Count} device(s) still using legacy v1`;
            v1HintEl.classList.remove('hidden');
        } else {
            v1HintEl.classList.add('hidden');
        }
    }

    function updateSecurityFlags(payload, onErrorRevert) {
        fetch('/api/security/flags', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                applySecurityState(data.flags || {}, data.readiness || securityReadiness);
                showToast('Security settings updated', 'success');
            } else {
                if (onErrorRevert) onErrorRevert();
                if (data.readiness) {
                    loadSecuritySettings();
                }
                showToast(data.error || 'Failed to update security settings', 'error');
            }
        })
        .catch(err => {
            console.error('Error updating security flags:', err);
            if (onErrorRevert) onErrorRevert();
            showToast('Error updating security settings', 'error');
        });
    }

    function onSecurityV2Toggle(checkbox) {
        const enabling = checkbox.checked;
        if (enabling && !securityReadiness.all_gateways_v2_ready) {
            checkbox.checked = false;
            showToast('Update all gateways to firmware 4.0.0+ before enabling Security v2', 'error');
            return;
        }
        if (!enabling && (securityReadiness.v2_device_count || 0) > 0) {
            const list = (securityReadiness.v2_devices || [])
                .slice(0, 8)
                .map(d => `${d.name} (${d.mac})`)
                .join(', ');
            const extra = securityReadiness.v2_device_count > 8
                ? ` and ${securityReadiness.v2_device_count - 8} more`
                : '';
            showModal(
                'Disable Security v2?',
                `You currently have ${securityReadiness.v2_device_count} Security v2 device(s) (${list}${extra}). Disabling Security v2 will stop outbound commands (lightstrips, OTA, config) to those devices until Security v2 is turned back on. Continue?`,
                () => updateSecurityFlags(
                    { security_v2_pair: false, security_v2_send: false, require_v2: false },
                    () => { checkbox.checked = true; }
                ),
                () => { checkbox.checked = true; }
            );
            return;
        }
        const payload = enabling
            ? { security_v2_decode: true, security_v2_pair: true, security_v2_send: true }
            : { security_v2_pair: false, security_v2_send: false, require_v2: false };
        updateSecurityFlags(payload, () => { checkbox.checked = !enabling; });
    }

    let savedSecurityPins = [];
    let securityPinsFilter = 'all';

    function escapeHtml(str) {
        return String(str ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function openSecurityPinsModal() {
        const modal = document.getElementById('security-pins-modal');
        if (modal) modal.classList.remove('hidden');
        const search = document.getElementById('security-pins-search');
        if (search) search.value = '';
        setSecurityPinsFilter('all');
        loadSavedSecurityPins();
    }

    function closeSecurityPinsModal() {
        const modal = document.getElementById('security-pins-modal');
        if (modal) modal.classList.add('hidden');
    }

    function setSecurityPinsFilter(mode) {
        securityPinsFilter = mode;
        ['all', 'paired', 'revoked'].forEach(key => {
            const btn = document.getElementById(`pins-filter-${key}`);
            if (!btn) return;
            const active = key === mode;
            btn.className = active
                ? `px-3 py-2 bg-blue-600 text-white${key !== 'all' ? ' border-l border-gray-300' : ''}`
                : `px-3 py-2 bg-white text-gray-700 hover:bg-gray-50${key !== 'all' ? ' border-l border-gray-300' : ''}`;
        });
        renderSavedSecurityPins();
    }

    function loadSavedSecurityPins() {
        fetch('/api/security/pins')
            .then(r => r.json())
            .then(data => {
                savedSecurityPins = (data.success && Array.isArray(data.pins)) ? data.pins : [];
                updateSavedPinsSummary();
                renderSavedSecurityPins();
            })
            .catch(err => {
                console.error('Error loading saved security PINs:', err);
            });
    }

    function updateSavedPinsSummary() {
        const summaryEl = document.getElementById('security-pins-summary');
        const btnLabel = document.getElementById('manage-pins-btn-label');
        const total = savedSecurityPins.length;
        const revoked = savedSecurityPins.filter(p => p.revoked).length;
        if (btnLabel) {
            btnLabel.textContent = total > 0 ? `Manage PINs (${total})` : 'Manage PINs';
        }
        if (summaryEl) {
            if (total === 0) {
                summaryEl.textContent = 'No saved PINs yet';
            } else {
                const paired = total - revoked;
                summaryEl.textContent = `${paired} paired, ${revoked} revoked/removed`;
            }
        }
    }

    function renderSavedSecurityPins() {
        const listEl = document.getElementById('security-pins-list');
        if (!listEl) return;

        const query = (document.getElementById('security-pins-search')?.value || '').trim().toLowerCase();
        const filtered = savedSecurityPins.filter(entry => {
            if (securityPinsFilter === 'paired' && entry.revoked) return false;
            if (securityPinsFilter === 'revoked' && !entry.revoked) return false;
            if (!query) return true;
            const haystack = `${entry.name || ''} ${entry.mac || ''} ${entry.pin || ''}`.toLowerCase();
            return haystack.includes(query);
        });

        if (filtered.length === 0) {
            listEl.innerHTML = `<p class="text-sm text-gray-500 text-center py-6">${
                savedSecurityPins.length === 0 ? 'No saved device PINs' : 'No PINs match your filter'
            }</p>`;
            return;
        }

        listEl.innerHTML = filtered.map((entry, idx) => {
            const badge = entry.revoked
                ? '<span class="ml-2 px-2 py-0.5 text-xs font-medium rounded-full bg-amber-100 text-amber-800">Revoked / Removed</span>'
                : '<span class="ml-2 px-2 py-0.5 text-xs font-medium rounded-full bg-green-100 text-green-800">Paired</span>';
            const displayName = escapeHtml(entry.name || entry.mac);
            const safeMac = escapeHtml(entry.mac);
            const safePin = escapeHtml(entry.pin);
            return `
                <div class="flex items-center justify-between p-3 bg-gray-50 rounded-lg border border-gray-100">
                    <div class="flex-1 min-w-0">
                        <div class="flex items-center flex-wrap">
                            <span class="font-medium text-gray-900 truncate">${displayName}</span>
                            ${badge}
                        </div>
                        <div class="text-xs text-gray-500 font-mono mt-0.5">${safeMac} &bull; PIN: <span class="font-semibold text-gray-800">${safePin}</span></div>
                    </div>
                    <div class="flex items-center gap-2 ml-3 shrink-0">
                        <button type="button" data-pin="${safePin}" onclick="copySavedPin(this.dataset.pin)"
                                class="px-3 py-1.5 bg-blue-600 text-white text-xs rounded-lg hover:bg-blue-700 transition-colors">
                            <i class="fas fa-copy mr-1"></i>Copy
                        </button>
                        <button type="button" data-mac="${safeMac}" data-name="${displayName}" data-revoked="${entry.revoked ? 'true' : 'false'}"
                                onclick="confirmDeleteSavedPin(this.dataset.mac, this.dataset.name, this.dataset.revoked === 'true')"
                                class="px-3 py-1.5 bg-red-600 text-white text-xs rounded-lg hover:bg-red-700 transition-colors">
                            <i class="fas fa-trash-alt mr-1"></i>Delete
                        </button>
                    </div>
                </div>
            `;
        }).join('');
    }

    function copySavedPin(pin) {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(pin)
                .then(() => showToast(`Copied PIN ${pin}`, 'success'))
                .catch(() => showToast(`PIN: ${pin}`, 'info'));
        } else {
            showToast(`PIN: ${pin}`, 'info');
        }
    }

    function confirmDeleteSavedPin(mac, name, isRevoked) {
        const msg = isRevoked
            ? `Permanently delete the saved 6-digit PIN for "${name}" (${mac})? You will need the physical label or a USB re-flash to re-pair this device later.`
            : `Remove the stored copy of the 6-digit PIN for active device "${name}" (${mac}) from this server? The device itself keeps its write-once PIN in flash.`;
        showModal(
            'Delete Saved Device PIN?',
            msg,
            () => deleteSavedPin(mac)
        );
    }

    function deleteSavedPin(mac) {
        fetch(`/api/security/pins/${encodeURIComponent(mac)}`, { method: 'DELETE' })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    showToast('Saved PIN deleted', 'success');
                    loadSavedSecurityPins();
                } else {
                    showToast(data.error || 'Failed to delete saved PIN', 'error');
                }
            })
            .catch(err => {
                console.error('Error deleting saved PIN:', err);
                showToast('Error deleting saved PIN', 'error');
            });
    }

    function onRequireV2Toggle(checkbox) {
        const val = checkbox.checked;
        updateSecurityFlags({ require_v2: val }, () => { checkbox.checked = !val; });
    }

    function onAllowV1Toggle(checkbox) {
        const allowing = checkbox.checked;
        if (!allowing && (securityReadiness.v1_device_count || 0) > 0) {
            const list = (securityReadiness.v1_devices || [])
                .slice(0, 8)
                .map(d => `${d.name} (${d.mac})`)
                .join(', ');
            const extra = securityReadiness.v1_device_count > 8
                ? ` and ${securityReadiness.v1_device_count - 8} more`
                : '';
            showModal(
                'Disable Legacy v1 Devices?',
                `You still have ${securityReadiness.v1_device_count} device(s) on legacy v1 (${list}${extra}). Disabling legacy v1 will block those devices from communicating until they are upgraded to Security v2. Continue?`,
                () => updateSecurityFlags({ allow_v1: false }, () => { checkbox.checked = true; }),
                () => { checkbox.checked = true; }
            );
            return;
        }
        updateSecurityFlags({ allow_v1: allowing }, () => { checkbox.checked = !allowing; });
    }

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

    function confirmResetEverything() {
        showModal(
            'Reset Everything?',
            'This will permanently delete ALL devices, sensors, bridges, plugins, and configurations. A backup will be downloaded before the reset. This action cannot be undone! Are you sure you want to proceed?',
            () => resetEverything()
        );
    }

    function resetEverything() {
        showToast('Creating backup and resetting everything...', 'info');
        fetch('/admin/api/reset', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({})
        })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                if (data.backup_filename) {
                    downloadBackup(data.backup_filename);
                }
                window.location.href = '/admin/restarting';
            } else {
                showToast('Failed to reset: ' + (data.error || 'Unknown error'), 'error');
            }
        })
        .catch(err => {
            showToast('Error resetting everything', 'error');
        });
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

    function showModal(title, message, onConfirm, onCancel = null) {
        document.getElementById('modal-title').textContent = title;
        document.getElementById('modal-message').textContent = message;
        pendingAction = onConfirm;
        pendingCancelAction = onCancel;
        document.getElementById('confirm-modal').classList.remove('hidden');
    }

    function closeModal(confirmed = false) {
        document.getElementById('confirm-modal').classList.add('hidden');
        const cancelCb = pendingCancelAction;
        pendingAction = null;
        pendingCancelAction = null;
        if (!confirmed && cancelCb) {
            cancelCb();
        }
    }

    function confirmAction() {
        const actionCb = pendingAction;
        closeModal(true);
        if (actionCb) {
            actionCb();
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
                <p class="text-sm ${textColor}">${escapeHtml(message)}</p>
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
