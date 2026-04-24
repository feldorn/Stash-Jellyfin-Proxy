        const state = {
            config: {},
            logs: [],
            streams: [],
            currentPage: 'dashboard',
            configAuthenticated: false
        };

        // Helper to format duration in human-readable format
        function formatDuration(seconds) {
            if (!seconds || seconds < 0) return '';
            const d = Math.floor(seconds / 86400);
            const h = Math.floor((seconds % 86400) / 3600);
            const m = Math.floor((seconds % 3600) / 60);
            const s = Math.floor(seconds % 60);
            if (d > 0) return `${d}d ${h}h ${m}m`;
            if (h > 0) return `${h}h ${m}m`;
            if (m > 0) return `${m}m ${s}s`;
            return `${s}s`;
        }

        // Navigation
        document.querySelectorAll('.nav-item').forEach(item => {
            item.addEventListener('click', () => {
                const page = item.dataset.page;
                showPage(page);
            });
        });

        async function showPage(page) {
            // Check if config page requires authentication
            if (page === 'config' && state.config.REQUIRE_AUTH_FOR_CONFIG && !state.configAuthenticated) {
                const password = prompt('Enter password to access configuration:');
                if (!password) return;

                try {
                    const res = await fetch('/api/auth-config', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ password })
                    });
                    const data = await res.json();
                    if (!data.success) {
                        alert('Incorrect password');
                        return;
                    }
                    state.configAuthenticated = true;
                } catch (e) {
                    alert('Authentication failed');
                    return;
                }
            }

            state.currentPage = page;
            document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
            document.querySelector(`[data-page="${page}"]`).classList.add('active');
            document.querySelectorAll('.page').forEach(p => p.classList.add('hidden'));
            document.getElementById(`page-${page}`).classList.remove('hidden');

            // Refresh data when switching pages
            if (page === 'config') {
                fetchConfig();
            } else if (page === 'logs') {
                fetchLogs();
            }
        }

        // API calls
        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                document.getElementById('proxy-status').textContent = data.running ? 'Running' : 'Stopped';
                document.getElementById('proxy-status').className = 'status-value ' + (data.running ? 'running' : 'stopped');
                document.getElementById('stash-status').textContent = data.stashConnected ? 'Connected' : 'Disconnected';
                document.getElementById('stash-status').className = 'status-value ' + (data.stashConnected ? 'connected' : 'disconnected');
                document.getElementById('stash-version').textContent = data.stashVersion || '-';
                document.getElementById('version').textContent = data.version || 'v6.02';
                document.getElementById('proxy-uptime').textContent = data.uptime ? `Uptime: ${formatDuration(data.uptime)}` : '';
            } catch (e) {
                console.error('Failed to fetch status:', e);
            }
        }

        async function fetchStreams() {
            try {
                const res = await fetch('/api/streams');
                const data = await res.json();
                state.streams = data.streams || [];
                document.getElementById('stream-count').textContent = state.streams.length;
                const list = document.getElementById('streams-list');
                if (state.streams.length === 0) {
                    list.innerHTML = '<div class="empty-state">No active streams</div>';
                } else {
                    list.innerHTML = state.streams.map(s => {
                        const startedAt = s.started ? new Date(s.started * 1000).toLocaleTimeString() : '';
                        const duration = s.started ? formatDuration(Date.now()/1000 - s.started) : '';
                        return `
                        <div class="stream-item">
                            <div class="stream-header">
                                <span class="stream-title">${s.performer ? s.performer + ': ' : ''}${s.title || s.id}</span>
                                <span class="stream-time">${duration}</span>
                            </div>
                            <div class="stream-meta">
                                <span class="stream-meta-item">
                                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"></path></svg>
                                    ${s.user || 'unknown'}
                                </span>
                                <span class="stream-meta-item">
                                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
                                    Started ${startedAt}
                                </span>
                                <span class="stream-meta-item">
                                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9"></path></svg>
                                    ${s.clientIp || 'unknown'}
                                </span>
                                <span class="stream-meta-item">
                                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"></path></svg>
                                    ${s.clientType || 'unknown'}
                                </span>
                            </div>
                        </div>
                    `;}).join('');
                }
            } catch (e) {
                console.error('Failed to fetch streams:', e);
            }
        }

        async function fetchLogs() {
            try {
                const limit = document.getElementById('log-line-count').value || 100;
                const res = await fetch(`/api/logs?limit=${limit}`);
                const data = await res.json();
                state.logs = data.entries || [];
                renderLogs();
            } catch (e) {
                console.error('Failed to fetch logs:', e);
            }
        }

        function renderLogs() {
            const levelFilter = document.getElementById('log-level-filter').value;
            const searchFilter = document.getElementById('log-search').value.toLowerCase();
            let filtered = state.logs;
            if (levelFilter) {
                filtered = filtered.filter(l => l.level === levelFilter);
            }
            if (searchFilter) {
                filtered = filtered.filter(l => l.message.toLowerCase().includes(searchFilter));
            }
            document.getElementById('log-count').textContent = `${filtered.length} entries`;
            const html = filtered.map(l => `<div class="log-entry log-${l.level}">${l.timestamp} [${l.level}] ${l.message}</div>`).join('');
            document.getElementById('full-logs').innerHTML = html || '<div class="empty-state">No logs</div>';
            // Dashboard shows last 10 log entries (not sliced by character count)
            const recentLogs = filtered.slice(-10);
            const dashboardHtml = recentLogs.map(l => `<div class="log-entry log-${l.level}">${l.timestamp} [${l.level}] ${l.message}</div>`).join('');
            document.getElementById('dashboard-logs').innerHTML = dashboardHtml || '<div class="empty-state">No logs</div>';
        }

        async function fetchStats() {
            try {
                const res = await fetch('/api/stats');
                const data = await res.json();

                // Update Stash library stats
                if (data.stash) {
                    document.getElementById('stat-scenes').textContent = data.stash.scenes.toLocaleString();
                    document.getElementById('stat-performers').textContent = data.stash.performers.toLocaleString();
                    document.getElementById('stat-studios').textContent = data.stash.studios.toLocaleString();
                    document.getElementById('stat-tags').textContent = data.stash.tags.toLocaleString();
                    document.getElementById('stat-groups').textContent = data.stash.groups.toLocaleString();
                }

                // Update Proxy usage stats
                if (data.proxy) {
                    document.getElementById('stat-streams-today').textContent = data.proxy.streams_today.toLocaleString();
                    document.getElementById('stat-total-streams').textContent = data.proxy.total_streams.toLocaleString();
                    document.getElementById('stat-unique-ips').textContent = data.proxy.unique_ips_today.toLocaleString();
                    document.getElementById('stat-auth-success').textContent = data.proxy.auth_success.toLocaleString();
                    document.getElementById('stat-auth-failed').textContent = data.proxy.auth_failed.toLocaleString();

                    // Update top played list
                    const topList = document.getElementById('top-played-list');
                    if (data.proxy.top_played && data.proxy.top_played.length > 0) {
                        topList.innerHTML = data.proxy.top_played.map((item, idx) => `
                            <div class="top-played-item">
                                <span class="top-played-rank">${idx + 1}</span>
                                <div class="top-played-info">
                                    <div class="top-played-title">${item.title}</div>
                                    <div class="top-played-performer">${item.performer || 'Unknown'}</div>
                                </div>
                                <span class="top-played-count">${item.count}x</span>
                            </div>
                        `).join('');
                    } else {
                        topList.innerHTML = '<div class="empty-state">No play data yet</div>';
                    }
                }
            } catch (e) {
                console.error('Failed to fetch stats:', e);
            }
        }

        // Default values - if field matches default, show placeholder instead
        const DEFAULTS = {
            STASH_URL: '',
            STASH_API_KEY: '',
            STASH_GRAPHQL_PATH: '/graphql',
            STASH_VERIFY_TLS: false,
            PROXY_BIND: '0.0.0.0',
            PROXY_PORT: 8096,
            UI_PORT: 8097,
            SJS_USER: '',
            SJS_PASSWORD: '',
            SERVER_ID: '',
            SERVER_NAME: 'Stash Media Server',
            TAG_GROUPS: [],
            LATEST_GROUPS: ['Scenes'],
            BANNER_MODE: 'recent',
            BANNER_POOL_SIZE: 200,
            BANNER_TAGS: [],
            STASH_TIMEOUT: 30,
            STASH_RETRIES: 3,
            ENABLE_FILTERS: true,
            ENABLE_IMAGE_RESIZE: true,
            ENABLE_TAG_FILTERS: false,
            ENABLE_ALL_TAGS: false,
            REQUIRE_AUTH_FOR_CONFIG: false,
            IMAGE_CACHE_MAX_SIZE: 1000,
            DEFAULT_PAGE_SIZE: 50,
            MAX_PAGE_SIZE: 200,
            LOG_LEVEL: 'INFO',
            LOG_DIR: '/config',
            LOG_FILE: 'stash_jellyfin_proxy.log',
            LOG_MAX_SIZE_MB: 10,
            LOG_BACKUP_COUNT: 3,
            BAN_THRESHOLD: 10,
            BAN_WINDOW_MINUTES: 15,
            BANNED_IPS: ''
        };

        async function fetchConfig() {
            try {
                const res = await fetch('/api/config');
                const data = await res.json();
                state.config = data.config || data;
                const envFields = data.env_fields || [];
                const definedFields = data.defined_fields || [];

                // Show env fields notice if any exist
                const envNotice = document.getElementById('env-notice');
                if (envFields.length > 0) {
                    envNotice.style.display = 'block';
                    envNotice.querySelector('span').textContent = envFields.join(', ');
                } else {
                    envNotice.style.display = 'none';
                }

                Object.entries(state.config).forEach(([key, value]) => {
                    const input = document.querySelector(`[name="${key}"]`);
                    if (input) {
                        const defaultVal = DEFAULTS[key];
                        const isEnvField = envFields.includes(key);
                        const isDefinedInConfig = definedFields.includes(key);

                        // Mark env fields as read-only with visual indicator
                        if (isEnvField) {
                            if (input.type === 'checkbox') {
                                // For checkboxes, disable and style the label
                                input.disabled = true;
                                const label = input.closest('label');
                                if (label) label.classList.add('env-locked-label');
                            } else {
                                input.readOnly = true;
                                input.disabled = input.tagName === 'SELECT';
                                input.classList.add('env-locked');
                            }
                        }

                        if (input.type === 'checkbox') {
                            input.checked = value === true || value === 'true';
                        } else if (input.tagName === 'SELECT') {
                            // Always set select value (dropdowns should show selection)
                            input.value = value;
                        } else if (Array.isArray(value)) {
                            const valStr = value.join(', ');
                            const defStr = Array.isArray(defaultVal) ? defaultVal.join(', ') : '';
                            // Show value if different from default OR explicitly defined in config
                            input.value = (valStr !== defStr || isDefinedInConfig) ? valStr : '';
                        } else {
                            // Show value if different from default OR explicitly defined in config
                            const strVal = String(value);
                            const strDef = String(defaultVal ?? '');
                            input.value = (strVal !== strDef || isDefinedInConfig) ? value : '';
                        }
                    }
                });
            } catch (e) {
                console.error('Failed to fetch config:', e);
            }
        }

        // Normalize path: ensure leading /, remove trailing /
        function normalizePath(path) {
            if (!path || path.trim() === '') return '/graphql';
            let p = path.trim();
            if (!p.startsWith('/')) p = '/' + p;
            if (p.length > 1 && p.endsWith('/')) p = p.slice(0, -1);
            return p;
        }

        // Form submission
        document.getElementById('config-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            const formData = new FormData(e.target);
            const config = {};
            const intFields = ['PROXY_PORT', 'UI_PORT', 'STASH_TIMEOUT', 'STASH_RETRIES', 'LOG_MAX_SIZE_MB', 'LOG_BACKUP_COUNT', 'DEFAULT_PAGE_SIZE', 'MAX_PAGE_SIZE', 'IMAGE_CACHE_MAX_SIZE', 'BAN_THRESHOLD', 'BAN_WINDOW_MINUTES', 'BANNER_POOL_SIZE'];
            const boolFields = ['ENABLE_FILTERS', 'ENABLE_IMAGE_RESIZE', 'ENABLE_TAG_FILTERS', 'ENABLE_ALL_TAGS', 'REQUIRE_AUTH_FOR_CONFIG', 'STASH_VERIFY_TLS'];

            formData.forEach((value, key) => {
                // If field is empty, use the default value
                const defaultVal = DEFAULTS[key];
                if (key === 'TAG_GROUPS' || key === 'LATEST_GROUPS' || key === 'BANNER_TAGS') {
                    if (value.trim() === '' && Array.isArray(defaultVal)) {
                        config[key] = defaultVal;
                    } else {
                        config[key] = value.split(',').map(s => s.trim()).filter(Boolean);
                    }
                } else if (key === 'STASH_GRAPHQL_PATH') {
                    // Normalize GraphQL path: ensure leading /, remove trailing /
                    config[key] = normalizePath(value);
                } else if (intFields.includes(key)) {
                    config[key] = value.trim() === '' ? defaultVal : (parseInt(value) || 0);
                } else {
                    config[key] = value.trim() === '' ? (defaultVal ?? '') : value;
                }
            });

            // Handle checkboxes (not included in FormData if unchecked)
            boolFields.forEach(key => {
                const checkbox = document.querySelector(`[name="${key}"]`);
                if (checkbox) {
                    config[key] = checkbox.checked;
                }
            });
            try {
                const res = await fetch('/api/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(config)
                });
                if (res.ok) {
                    const result = await res.json();
                    if (result.needs_restart && result.needs_restart.length > 0) {
                        showToast(`Configuration saved. ${result.needs_restart.join(', ')} require restart.`, 'warning');
                    } else if (result.applied_immediately && result.applied_immediately.length > 0) {
                        showToast('Configuration saved and applied!', 'success');
                    } else {
                        showToast('Configuration saved.', 'success');
                    }
                    // Refresh config to reflect new values
                    fetchConfig();
                } else {
                    showToast('Failed to save configuration', 'error');
                }
            } catch (e) {
                showToast('Failed to save configuration', 'error');
            }
        });

        // Restart button
        document.getElementById('restart-btn').addEventListener('click', async () => {
            if (!confirm('Are you sure you want to restart the server? Active streams will be interrupted.')) {
                return;
            }
            try {
                showToast('Restarting server...', 'info');
                const res = await fetch('/api/restart', { method: 'POST' });
                if (res.ok) {
                    // Poll for server to come back up
                    let attempts = 0;
                    const maxAttempts = 30;
                    const checkServer = async () => {
                        attempts++;
                        try {
                            const statusRes = await fetch('/api/status', { cache: 'no-store' });
                            if (statusRes.ok) {
                                showToast('Server restarted successfully!', 'success');
                                setTimeout(() => location.reload(), 1000);
                                return;
                            }
                        } catch (e) {}
                        if (attempts < maxAttempts) {
                            setTimeout(checkServer, 1000);
                        } else {
                            showToast('Server restart timed out. Please refresh manually.', 'error');
                        }
                    };
                    setTimeout(checkServer, 2000);
                } else {
                    showToast('Failed to restart server', 'error');
                }
            } catch (e) {
                // Expected - server is restarting
                setTimeout(() => location.reload(), 3000);
            }
        });

        // Log filters
        document.getElementById('log-level-filter').addEventListener('change', renderLogs);
        document.getElementById('log-search').addEventListener('input', renderLogs);
        document.getElementById('log-line-count').addEventListener('change', fetchLogs);

        // Copy visible logs to clipboard
        document.getElementById('copy-logs').addEventListener('click', () => {
            const levelFilter = document.getElementById('log-level-filter').value;
            const searchFilter = document.getElementById('log-search').value.toLowerCase();
            let filtered = state.logs;
            if (levelFilter) filtered = filtered.filter(l => l.level === levelFilter);
            if (searchFilter) filtered = filtered.filter(l => l.message.toLowerCase().includes(searchFilter));
            const text = filtered.map(l => `${l.timestamp} [${l.level}] ${l.message}`).join('\\n');
            navigator.clipboard.writeText(text).then(() => {
                const btn = document.getElementById('copy-logs');
                btn.textContent = 'Copied!';
                setTimeout(() => { btn.textContent = 'Copy'; }, 2000);
            }).catch(() => {
                const ta = document.createElement('textarea');
                ta.value = text;
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
                const btn = document.getElementById('copy-logs');
                btn.textContent = 'Copied!';
                setTimeout(() => { btn.textContent = 'Copy'; }, 2000);
            });
        });

        // Download logs
        document.getElementById('download-logs').addEventListener('click', async () => {
            try {
                const res = await fetch('/api/logs?limit=10000');
                const data = await res.json();
                const text = data.entries.map(l => `${l.timestamp} [${l.level}] ${l.message}`).join('\\n');
                const blob = new Blob([text], { type: 'text/plain' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = 'stash_jellyfin_proxy.log';
                a.click();
                URL.revokeObjectURL(url);
            } catch (e) {
                showToast('Failed to download logs', 'error');
            }
        });

        // Reset statistics
        document.getElementById('reset-stats-btn').addEventListener('click', async () => {
            if (!confirm('Reset all usage statistics? This will clear stream counts, play history, and auth stats.')) {
                return;
            }
            try {
                const res = await fetch('/api/stats/reset', { method: 'POST' });
                if (res.ok) {
                    showToast('Statistics reset successfully', 'success');
                    fetchStats();
                } else {
                    showToast('Failed to reset statistics', 'error');
                }
            } catch (e) {
                showToast('Failed to reset statistics', 'error');
            }
        });

        function showToast(message, type) {
            const toast = document.createElement('div');
            toast.className = `toast ${type}`;
            toast.textContent = message;
            document.body.appendChild(toast);
            setTimeout(() => toast.remove(), 3000);
        }

        // Polling
        async function poll() {
            if (state.currentPage === 'dashboard') {
                await Promise.all([fetchStatus(), fetchStreams(), fetchLogs(), fetchStats()]);
            } else if (state.currentPage === 'logs') {
                await fetchLogs();
            }
        }

        // Initial load
        fetchStatus();
        fetchStreams();
        fetchLogs();
        fetchStats();
        fetchConfig();
        setInterval(poll, 5000);
