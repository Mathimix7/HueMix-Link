let pendingConfirmAction = null;
let pendingRestart = false;

async function refreshPlugins() {
    const refreshIcon = document.getElementById('refresh-icon');
    refreshIcon.classList.add('rotate-360');
    setTimeout(() => {
        refreshIcon.classList.remove('rotate-360');
    }, 500);
    await loadPlugins();
    checkAllUpdates();
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('refresh-plugins-btn').addEventListener('click', refreshPlugins);
    document.getElementById('check-updates-btn').addEventListener('click', checkAllUpdates);
    document.getElementById('install-plugin-form').addEventListener('submit', onInstallSubmit);
    const restartButton = document.getElementById('restart-server-btn');
    if (restartButton) {
        restartButton.addEventListener('click', restartServerFromPluginsPage);
    }
    loadPlugins();
    loadOfficialPlugins();
    setTimeout(checkAllUpdates, 500);
});

function showRestartBanner() {
    pendingRestart = true;
    const section = document.getElementById('restart-section');
    if (section) {
        section.classList.remove('hidden');
    }
}

function hideRestartBanner() {
    pendingRestart = false;
    const section = document.getElementById('restart-section');
    if (section) {
        section.classList.add('hidden');
    }
    const info = document.getElementById('restart-service-info');
    if (info) {
        info.classList.add('hidden');
    }
}

async function loadPlugins() {
    const listEl = document.getElementById('plugins-list');
    const emptyEl = document.getElementById('plugins-empty');

    listEl.innerHTML = '<div class="p-4 text-sm text-gray-500">Loading plugins...</div>';

    try {
        const response = await fetch('/admin/api/plugins');
        const data = await response.json();

        if (!data.success) {
            throw new Error(data.error || 'Failed to load plugins');
        }

        const plugins = Array.isArray(data.plugins) ? data.plugins : [];

        if (!plugins.length) {
            listEl.innerHTML = '';
            emptyEl.classList.remove('hidden');
            return;
        }

        emptyEl.classList.add('hidden');
        listEl.innerHTML = plugins.map(renderPluginCard).join('');
    } catch (error) {
        console.error('Error loading plugins:', error);
        listEl.innerHTML = `<div class="p-4 text-sm text-red-600">${escapeHtml(error.message || 'Failed to load plugins')}</div>`;
    }
}

async function loadOfficialPlugins() {
    const listEl = document.getElementById('official-plugins-list');
    if (!listEl) return;

    try {
        const response = await fetch('/admin/api/plugins/official');
        const data = await response.json();

        if (!data.success) {
            throw new Error(data.error || 'Failed to load official plugins');
        }

        const plugins = Array.isArray(data.plugins) ? data.plugins : [];

        if (!plugins.length) {
            listEl.innerHTML = '<div class="p-4 text-sm text-gray-500">No official plugins available yet.</div>';
            return;
        }

        listEl.innerHTML = plugins.map(renderOfficialPluginCard).join('');
    } catch (error) {
        console.error('Error loading official plugins:', error);
        listEl.innerHTML = `<div class="p-4 text-sm text-red-600">${escapeHtml(error.message || 'Failed to load official plugins')}</div>`;
    }
}

function renderOfficialPluginCard(plugin) {
    const icon = plugin.icon || 'puzzle-piece';
    const color = plugin.color || 'emerald';
    const installed = plugin.installed;

    return `
        <article class="official-plugin-card border border-gray-200 rounded-xl p-4">
            <div class="flex items-start justify-between gap-4">
                <div class="flex items-start gap-3 min-w-0">
                    <div class="w-10 h-10 rounded-lg bg-${color}-100 flex items-center justify-center flex-shrink-0 mt-0.5">
                        <i class="fas fa-${icon} text-${color}-600"></i>
                    </div>
                    <div class="min-w-0">
                        <h3 class="text-base font-semibold text-gray-900">${escapeHtml(plugin.name)}</h3>
                        <p class="text-sm text-gray-600 mt-0.5">${escapeHtml(plugin.description)}</p>
                    </div>
                </div>
                ${installed
                    ? '<span class="badge bg-emerald-100 text-emerald-700 flex-shrink-0">Installed</span>'
                    : `<button onclick="installOfficialPlugin(this, '${escapeAttr(plugin.repo_url)}', '${escapeAttr(plugin.branch || '')}')"
                            class="px-3 py-1.5 rounded-lg text-sm bg-emerald-600 hover:bg-emerald-700 text-white flex-shrink-0 install-official-btn">
                        <i class="fas fa-plus mr-1"></i>Install
                    </button>`
                }
            </div>
        </article>
    `;
}

async function installOfficialPlugin(button, repoUrl, branch) {
    button.disabled = true;
    button.innerHTML = '<i class="fas fa-spinner fa-spin mr-1"></i>Installing...';

    try {
        const response = await fetch('/admin/api/plugins/install', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                repo_url: repoUrl,
                branch: branch || null,
            }),
        });

        const data = await response.json();
        if (!data.success) {
            throw new Error(data.error || 'Install failed');
        }

        const installId = data.install_id;
        openInstallProgressModal();
        pollInstallStatusForOfficial(installId);
    } catch (error) {
        console.error('Install official plugin failed:', error);
        showToast(error.message || 'Install failed', 'error');
        button.disabled = false;
        button.innerHTML = '<i class="fas fa-plus mr-1"></i>Install';
    }
}

async function pollInstallStatusForOfficial(installId) {
    const statusEl = document.getElementById('install-progress-status');
    const logEl = document.getElementById('install-progress-log');
    try {
        let finished = false;
        while (!finished) {
            const resp = await fetch('/admin/api/plugins/install/status/' + encodeURIComponent(installId));
            const body = await resp.json();
            if (!body.success) {
                statusEl.textContent = 'Error: ' + (body.error || 'unknown');
                finished = true;
                break;
            }
            const s = body.status;
            statusEl.textContent = (s.step || 'unknown') + (s.success === true ? ' — Done' : (s.success === false ? ' — Failed' : ''));
            logEl.textContent = (Array.isArray(s.logs) ? s.logs.join('\n') : '');
            logEl.scrollTop = logEl.scrollHeight;

            if (s.success === true || s.success === false) {
                finished = true;
                document.getElementById('install-progress-close').disabled = false;
                if (s.success === true) {
                    showToast('Install completed', 'success');
                    showRestartBanner();
                    await loadPlugins();
                    await loadOfficialPlugins();
                } else {
                    showToast('Install failed: ' + (s.error || 'See logs'), 'error');
                }
                break;
            }

            await new Promise((r) => setTimeout(r, 1000));
        }
    } catch (err) {
        statusEl.textContent = 'Error: ' + (err.message || err);
        document.getElementById('install-progress-close').disabled = false;
    }
}

function renderPluginCard(plugin) {
    const enabled = plugin.enabled;
    const loaded = plugin.loaded;
    const healthClass = plugin.package_exists ? 'text-emerald-700 bg-emerald-100' : 'text-red-700 bg-red-100';
    const healthLabel = plugin.package_exists ? 'Files OK' : 'Missing Files';
    const source = plugin.source_repo || 'Local / unknown source';
    const version = plugin.version || '0.0.0';
    const safeId = (plugin.plugin_id || plugin.id).replace(/[^a-zA-Z0-9_-]/g, '_');
    const updateId = 'update-' + safeId;

    return `
        <article class="plugin-card border border-gray-200 rounded-xl p-4" id="plugin-${escapeAttr(plugin.plugin_id || plugin.id)}">
            <div class="flex items-start justify-between gap-4">
                <div class="min-w-0">
                    <h3 class="text-lg font-semibold text-gray-900 truncate">${escapeHtml(plugin.name || plugin.id)}</h3>
                    <p class="text-xs text-gray-500 mt-1">${escapeHtml(plugin.id)} • ${escapeHtml(plugin.module || '')} • v${escapeHtml(version)}</p>
                    <div class="mt-3 text-xs text-gray-500 break-all">
                        <div><strong>Repo:</strong> ${escapeHtml(source)}</div>
                        <div><strong>Installed:</strong> ${escapeHtml(new Date(plugin.installed_at).toLocaleString() || '-')}</div>
                    </div>
                </div>
                <div class="flex flex-col items-end gap-2">
                    <span class="badge ${enabled ? 'bg-blue-100 text-blue-700' : 'bg-gray-100 text-gray-600'}">${enabled ? 'Enabled' : 'Disabled'}</span>
                    <span class="badge ${loaded ? 'bg-violet-100 text-violet-700' : 'bg-gray-100 text-gray-600'}">${loaded ? 'Loaded' : 'Not Loaded'}</span>
                    <span id="${escapeAttr(updateId)}" class="update-badge" style="display:none"></span>
                </div>
            </div>

            <div class="mt-4 flex flex-wrap gap-2">
                <button onclick="togglePlugin('${escapeAttr(plugin.plugin_id || plugin.id)}', ${enabled ? 'false' : 'true'})"
                        class="px-3 py-1.5 rounded-lg text-sm ${enabled ? 'bg-emerald-600 hover:bg-emerald-700' : 'bg-gray-700 hover:bg-gray-800'} text-white">
                    <i class="fas ${enabled ? 'fa-toggle-on' : 'fa-toggle-off'} mr-1"></i>${enabled ? 'Enabled' : 'Disabled'}
                </button>
                <button onclick="confirmUninstall('${escapeAttr(plugin.plugin_id || plugin.id)}', '${escapeAttr(plugin.name || plugin.id)}')"
                        class="px-3 py-1.5 rounded-lg text-sm bg-red-600 hover:bg-red-700 text-white">
                    <i class="fas fa-trash mr-1"></i>Uninstall
                </button>
                <div id="update-actions-${safeId}" class="hidden"></div>
            </div>
        </article>
    `;
}

async function onInstallSubmit(event) {
    event.preventDefault();

    const button = document.getElementById('install-plugin-btn');
    const repoUrl = document.getElementById('repo-url').value.trim();
    const branch = document.getElementById('repo-branch').value.trim();

    if (!repoUrl) {
        showToast('Repository URL is required', 'warning');
        return;
    }

    button.disabled = true;
    button.innerHTML = '<i class="fas fa-spinner fa-spin mr-2"></i>Installing...';

    try {
        const response = await fetch('/admin/api/plugins/install', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                repo_url: repoUrl,
                branch: branch || null,
            }),
        });

        const data = await response.json();
        if (!data.success) {
            throw new Error(data.error || 'Install failed');
        }

        const installId = data.install_id;
        openInstallProgressModal();
        pollInstallStatus(installId);
    } catch (error) {
        console.error('Install plugin failed:', error);
        showToast(error.message || 'Install failed', 'error');
    } finally {
        button.disabled = false;
        button.innerHTML = '<i class="fas fa-plus mr-2"></i>Install Plugin';
    }
}

function confirmUninstall(pluginId, pluginName) {
    pendingConfirmAction = () => uninstallPlugin(pluginId);
    document.getElementById('confirm-title').textContent = 'Uninstall Plugin';
    document.getElementById('confirm-message').textContent = `Remove ${pluginName}? This deletes the plugin folder, removes registry entries, and cleans related pairing history.`;
    document.getElementById('confirm-action-btn').onclick = runConfirmAction;
    document.getElementById('confirm-modal').classList.remove('hidden');
}

function closeConfirmModal() {
    document.getElementById('confirm-modal').classList.add('hidden');
    pendingConfirmAction = null;
}

function runConfirmAction() {
    if (typeof pendingConfirmAction === 'function') {
        pendingConfirmAction();
    }
    closeConfirmModal();
}

async function uninstallPlugin(pluginId) {
    try {
        const response = await fetch('/admin/api/plugins/uninstall', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ plugin_id: pluginId }),
        });

        const data = await response.json();
        if (!data.success) {
            throw new Error(data.error || 'Uninstall failed');
        }

        showToast(data.message || 'Plugin uninstalled', 'success');
        showRestartBanner();
        await loadPlugins();
        await loadOfficialPlugins();
    } catch (error) {
        console.error('Uninstall plugin failed:', error);
        showToast(error.message || 'Uninstall failed', 'error');
    }
}

async function togglePlugin(pluginId, enabled) {
    try {
        const response = await fetch('/admin/api/plugins/enable', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ plugin_id: pluginId, enabled }),
        });

        const data = await response.json();
        if (!data.success) {
            throw new Error(data.error || 'Failed to update plugin state');
        }

        showToast(data.message || 'Plugin updated', 'success');
        showRestartBanner();
        await loadPlugins();
    } catch (error) {
        console.error('Toggle plugin failed:', error);
        showToast(error.message || 'Failed to update plugin', 'error');
    }
}

// ── Update checking ─────────────────────────────────────────────────────

async function checkAllUpdates() {
    const btn = document.getElementById('check-updates-btn');
    const icon = document.getElementById('check-updates-icon');
    const textEl = document.getElementById('check-updates-text');
    if (!btn || !icon || !textEl) return;
    btn.disabled = true;
    icon.className = 'fas fa-spinner fa-spin mr-2';
    textEl.textContent = 'Checking...';

    // Show checking state on all plugin cards
    document.querySelectorAll('.update-badge').forEach(el => {
        el.style.display = 'inline-flex';
        el.className = 'update-badge checking';
        el.textContent = 'Checking...';
    });

    try {
        const resp = await fetch('/admin/api/plugins/updates');
        const data = await resp.json();
        if (!data.success) throw new Error(data.error || 'Failed to check updates');

        const results = Array.isArray(data.results) ? data.results : [];
        for (const result of results) {
            const pluginId = result.plugin_id;
            const updateId = 'update-' + pluginId.replace(/[^a-zA-Z0-9_-]/g, '_');
            const badge = document.getElementById(updateId);
            if (!badge) continue;

            badge.style.display = 'inline-flex';
            if (result.error) {
                badge.className = 'update-badge error';
                badge.textContent = 'Error';
                badge.title = result.error;
            } else if (result.update_available) {
                badge.className = 'update-badge available';
                badge.textContent = 'v' + escapeHtml(result.latest_version) + ' available';
                // Show update action
                const actionsEl = document.getElementById('update-actions-' + pluginId.replace(/[^a-zA-Z0-9_-]/g, '_'));
                if (actionsEl) {
                    actionsEl.className = '';
                    actionsEl.innerHTML = `
                        <button onclick="applyPluginUpdate('${escapeAttr(pluginId)}')"
                                class="px-3 py-1.5 rounded-lg text-sm bg-amber-600 hover:bg-amber-700 text-white">
                            <i class="fas fa-download mr-1"></i>Update to v${escapeHtml(result.latest_version)}
                        </button>
                    `;
                }
            } else {
                badge.className = 'update-badge latest';
                badge.textContent = 'v' + escapeHtml(result.installed_version) + ' (latest)';
            }
        }
        showToast('Update check completed', 'success');
    } catch (error) {
        console.error('Update check failed:', error);
        showToast(error.message || 'Update check failed', 'error');
        document.querySelectorAll('.update-badge.checking').forEach(el => {
            el.className = 'update-badge error';
            el.textContent = 'Check failed';
        });
    } finally {
        btn.disabled = false;
        icon.className = 'fas fa-cloud-arrow-down mr-2';
        textEl.textContent = 'Check Updates';
    }
}

async function applyPluginUpdate(pluginId) {
    // Reuse the install progress modal for updates
    openInstallProgressModal();
    document.getElementById('install-progress-title').textContent = 'Updating Plugin';

    try {
        const resp = await fetch('/admin/api/plugins/update', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ plugin_id: pluginId }),
        });
        const data = await resp.json();
        if (!data.success) throw new Error(data.error || 'Update failed');

        const sessionId = data.install_id;
        await pollUpdateStatus(sessionId);
    } catch (error) {
        console.error('Update failed:', error);
        const statusEl = document.getElementById('install-progress-status');
        statusEl.textContent = 'Error: ' + (error.message || 'Update failed');
        document.getElementById('install-progress-close').disabled = false;
        showToast(error.message || 'Update failed', 'error');
    }
}

async function pollUpdateStatus(sessionId) {
    const statusEl = document.getElementById('install-progress-status');
    const logEl = document.getElementById('install-progress-log');
    try {
        let finished = false;
        while (!finished) {
            const resp = await fetch('/admin/api/plugins/install/status/' + encodeURIComponent(sessionId));
            const body = await resp.json();
            if (!body.success) {
                statusEl.textContent = 'Error: ' + (body.error || 'unknown');
                finished = true;
                break;
            }
            const s = body.status;
            statusEl.textContent = (s.step || 'unknown') + (s.success === true ? ' — Done' : (s.success === false ? ' — Failed' : ''));
            logEl.textContent = (Array.isArray(s.logs) ? s.logs.join('\n') : '');
            logEl.scrollTop = logEl.scrollHeight;

            if (s.success === true || s.success === false) {
                finished = true;
                document.getElementById('install-progress-close').disabled = false;
                if (s.success === true) {
                    showToast('Update completed', 'success');
                    showRestartBanner();
                    await loadPlugins();
                    await loadOfficialPlugins();
                } else {
                    showToast('Update failed: ' + (s.error || 'See logs'), 'error');
                }
                document.getElementById('install-progress-title').textContent = 'Plugin Operation';
                break;
            }

            await new Promise((r) => setTimeout(r, 1000));
        }
    } catch (err) {
        statusEl.textContent = 'Error: ' + (err.message || err);
        document.getElementById('install-progress-close').disabled = false;
        document.getElementById('install-progress-title').textContent = 'Plugin Operation';
    }
}

function escapeHtml(value) {
    return String(value || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function escapeAttr(value) {
    return String(value || '').replace(/'/g, "\\'");
}

function openInstallProgressModal() {
    document.getElementById('install-progress-modal').classList.remove('hidden');
    document.getElementById('install-progress-status').textContent = 'Starting...';
    document.getElementById('install-progress-log').textContent = '';
    document.getElementById('install-progress-close').disabled = true;
}

function closeInstallProgressModal() {
    document.getElementById('install-progress-modal').classList.add('hidden');
}

async function pollInstallStatus(installId) {
    const statusEl = document.getElementById('install-progress-status');
    const logEl = document.getElementById('install-progress-log');
    try {
        let finished = false;
        while (!finished) {
            const resp = await fetch('/admin/api/plugins/install/status/' + encodeURIComponent(installId));
            const body = await resp.json();
            if (!body.success) {
                statusEl.textContent = 'Error: ' + (body.error || 'unknown');
                finished = true;
                break;
            }
            const s = body.status;
            statusEl.textContent = (s.step || 'unknown') + (s.success === true ? ' — Done' : (s.success === false ? ' — Failed' : ''));
            logEl.textContent = (Array.isArray(s.logs) ? s.logs.join('\n') : '');
            logEl.scrollTop = logEl.scrollHeight;

            if (s.success === true || s.success === false) {
                finished = true;
                document.getElementById('install-progress-close').disabled = false;
                if (s.success === true) {
                    showToast('Install completed', 'success');
                    showRestartBanner();
                    await loadPlugins();
                    await loadOfficialPlugins();
                } else {
                    showToast('Install failed: ' + (s.error || 'See logs'), 'error');
                }
                break;
            }

            await new Promise((r) => setTimeout(r, 1000));
        }
    } catch (err) {
        statusEl.textContent = 'Error: ' + (err.message || err);
        document.getElementById('install-progress-close').disabled = false;
    }
}

async function restartServerFromPluginsPage() {
    const button = document.getElementById('restart-server-btn');
    const infoEl = document.getElementById('restart-service-info');
    if (!button) return;

    button.disabled = true;
    button.innerHTML = '<i class="fas fa-spinner fa-spin mr-2"></i>Checking...';

    try {
        const checkResp = await fetch('/admin/api/service/check');
        const checkData = await checkResp.json();

        if (!checkData.success || !checkData.can_restart) {
            if (infoEl) infoEl.classList.remove('hidden');
            button.disabled = false;
            button.innerHTML = '<i class="fas fa-rotate-right mr-2"></i>Restart Server';
            showToast('Cannot restart: service not detected. Please restart manually.', 'error');
            return;
        }

        if (infoEl) infoEl.classList.add('hidden');

        button.innerHTML = '<i class="fas fa-spinner fa-spin mr-2"></i>Restarting...';
        showToast('Restarting server...', 'info');

        const restartResponse = await fetch('/api/server/restart', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
        });

        const restartData = await restartResponse.json();
        if (!restartData.success) {
            throw new Error(restartData.error || 'Restart failed');
        }

        showToast(restartData.message || 'Restart requested', 'success');
        setTimeout(() => window.location.reload(), 2000);
    } catch (error) {
        console.error('Restart server failed:', error);
        showToast(error.message || 'Failed to restart server', 'error');
        button.disabled = false;
        button.innerHTML = '<i class="fas fa-rotate-right mr-2"></i>Restart Server';
    }
}
