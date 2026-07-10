// GrugThink Multi-Bot Dashboard
// Complete implementation for memory management and bot control

class Dashboard {
    constructor() {
        this.websocket = null;
        this.currentMemoryBotId = null;
        this.currentBotId = null;
        this.selectedServerId = null;
        this.availableServers = [];
        this.memories = [];
        this.filteredMemories = [];
        this.memoryPage = 1;
        this.memoryPageSize = 20;
        this.bots = [];
        this.currentLogs = [];
        this.filteredLogs = [];
        this.currentLogsBotId = null;
        this.setupWebSocket();
        this.setupURLRouting();
        this.loadInitialData();
    }

    // Simple polling instead of WebSocket (simplified approach)
    setupWebSocket() {
        // Set initial status as connected since we're using polling
        document.getElementById('connection-status').innerHTML = 
            '<i class="bi bi-circle-fill text-success me-1"></i><span class="d-none d-md-inline">Connected</span>';
        
        // Start polling for updates every 10 seconds
        this.startPolling();
    }
    
    startPolling() {
        // Poll for bot status updates every 10 seconds
        setInterval(() => {
            this.loadBots();
        }, 10000);
        
        // Poll for log updates every 5 seconds
        setInterval(() => {
            this.updateLogs();
        }, 5000);
    }

    setupURLRouting() {
        // Handle URL hash changes to preserve tab state
        const handleHashChange = () => {
            const hash = window.location.hash;
            if (hash && hash.length > 1) {
                const targetTab = hash.substring(1);
                const tabElement = document.querySelector(`a[href="${hash}"]`);
                if (tabElement) {
                    const tab = new bootstrap.Tab(tabElement);
                    tab.show();
                }
            }
        };

        // Handle initial load
        handleHashChange();

        // Listen for hash changes
        window.addEventListener('hashchange', handleHashChange);

        // Update URL when tabs are changed
        document.querySelectorAll('a[data-bs-toggle="tab"]').forEach(tab => {
            tab.addEventListener('shown.bs.tab', (event) => {
                const targetHash = event.target.getAttribute('href');
                if (targetHash !== window.location.hash) {
                    history.replaceState(null, null, targetHash);
                }
            });
        });
    }

    handleWebSocketMessage(data) {
        if (data.type === 'bot_status_update') {
            this.loadBots();
        } else if (data.type === 'log_update') {
            this.updateLogs();
        }
    }

    // Initial Data Loading
    async loadInitialData() {
        await this.loadUser();
        await this.loadBots();
        await this.loadTemplates();
        await this.loadTokens();
        await this.loadApiKeys();
        this.updateDashboardStats();
        // Load system logs if on monitoring page
        this.updateLogs();
        // Setup form event listeners
        this.setupFormListeners();
    }

    // User Management
    async loadUser() {
        try {
            const response = await fetch('/api/user');
            if (response.ok) {
                const user = await response.json();
                document.getElementById('username').textContent = user.username;
            }
        } catch (error) {
            console.error('Failed to load user:', error);
        }
    }

    // Theme Management
    toggleTheme() {
        const html = document.documentElement;
        const currentTheme = html.getAttribute('data-bs-theme');
        const newTheme = currentTheme === 'light' ? 'dark' : 'light';
        
        html.setAttribute('data-bs-theme', newTheme);
        localStorage.setItem('theme', newTheme);
        
        const themeIcon = document.getElementById('theme-icon');
        const themeText = document.getElementById('theme-text');
        
        if (newTheme === 'dark') {
            themeIcon.className = 'bi bi-sun-fill';
            themeText.textContent = 'Light';
        } else {
            themeIcon.className = 'bi bi-moon-fill';
            themeText.textContent = 'Dark';
        }
    }

    // Bot Management
    async loadBots() {
        try {
            const response = await fetch('/api/bots');
            if (response.ok) {
                const bots = await response.json();
                this.bots = bots; // Store bot data for use in other functions
                this.populateBotsTable(bots);
                this.updateDashboardStats();
            }
        } catch (error) {
            console.error('Failed to load bots:', error);
        }
    }

    populateBotsTable(bots) {
        const tbody = document.querySelector('#bots-table tbody');
        tbody.innerHTML = '';
        
        bots.forEach(bot => {
            const row = document.createElement('tr');
            
            // Fix status - use 'status' field instead of 'runtime_status'
            const statusBadge = this.getStatusBadge(bot.status);
            const serverCount = bot.guild_count || 0;
            const uptime = bot.uptime || 'N/A';
            
            // Get chat frequency (will be loaded asynchronously)
            const chatFreqCell = `<td id="chat-freq-${bot.bot_id}">Loading...</td>`;
            
            row.innerHTML = `
                <td>${bot.name}</td>
                <td>${statusBadge}</td>
                <td>${bot.personality || 'Default'}</td>
                <td>${serverCount}</td>
                <td>${uptime}</td>
                ${chatFreqCell}
                <td>
                    <div class="btn-group btn-group-sm" role="group">
                        ${this.getBotActionButtons(bot)}
                    </div>
                </td>
            `;
            
            // Load chat frequency asynchronously with bot data
            this.loadBotChatFrequency(bot.bot_id, bot);
            tbody.appendChild(row);
        });
    }

    getStatusBadge(status) {
        const badges = {
            'running': '<span class="badge bg-success">Running</span>',
            'stopped': '<span class="badge bg-secondary">Stopped</span>',
            'starting': '<span class="badge bg-warning">Starting</span>',
            'stopping': '<span class="badge bg-warning">Stopping</span>',
            'error': '<span class="badge bg-danger">Error</span>'
        };
        return badges[status] || '<span class="badge bg-secondary">Unknown</span>';
    }

    // Setup form event listeners
    setupFormListeners() {
        // Set up form listeners when tabs become visible
        const configTab = document.querySelector('a[data-bs-target="#configuration"]');
        if (configTab) {
            configTab.addEventListener('shown.bs.tab', () => {
                this.setupConfigurationForms();
            });
        }
        
        // Also try to set up immediately in case we're already on the tab
        this.setupConfigurationForms();
    }
    
    setupConfigurationForms() {
        // Remove form submission listeners since we're using onclick handlers now
        // This prevents conflicts between preventDefault() and onclick handlers
        console.log('Configuration forms setup - using onclick handlers instead of form submission');
    }

    getBotActionButtons(bot) {
        const buttons = [];
        
        // Fix status check - use 'status' field instead of 'runtime_status'
        if (bot.status === 'running') {
            buttons.push(`<button class="btn btn-outline-warning" onclick="dashboard.stopBot('${bot.bot_id}')" title="Stop Bot"><i class="bi bi-stop-fill"></i></button>`);
            buttons.push(`<button class="btn btn-outline-info" onclick="dashboard.restartBot('${bot.bot_id}')" title="Restart Bot"><i class="bi bi-arrow-clockwise"></i></button>`);
        } else {
            buttons.push(`<button class="btn btn-outline-success" onclick="dashboard.startBot('${bot.bot_id}')" title="Start Bot"><i class="bi bi-play-fill"></i></button>`);
        }
        
        buttons.push(`<button class="btn btn-outline-primary" onclick="dashboard.editBot('${bot.bot_id}')" title="Edit Bot"><i class="bi bi-pencil"></i></button>`);
        buttons.push(`<button class="btn btn-outline-secondary" onclick="dashboard.viewBotLogs('${bot.bot_id}')" title="View Logs"><i class="bi bi-journal-text"></i></button>`);
        buttons.push(`<button class="btn btn-outline-success" onclick="dashboard.openChatSettings('${bot.bot_id}', '${bot.name}')" title="Chat Settings"><i class="bi bi-chat-dots"></i></button>`);
        buttons.push(`<button class="btn btn-outline-info" onclick="dashboard.openMemoryManagement('${bot.bot_id}', '${bot.name}')" title="Manage Memory"><i class="bi bi-memory"></i></button>`);
        buttons.push(`<button class="btn btn-outline-danger" onclick="dashboard.deleteBot('${bot.bot_id}')" title="Delete Bot"><i class="bi bi-trash"></i></button>`);
        
        return buttons.join(' ');
    }

    async startBot(botId) {
        try {
            const response = await fetch(`/api/bots/${botId}/start`, { method: 'POST' });
            if (response.ok) {
                this.showAlert('Bot started successfully', 'success');
                this.loadBots();
            } else {
                const error = await response.json();
                this.showAlert(`Failed to start bot: ${error.detail}`, 'danger');
            }
        } catch (error) {
            this.showAlert('Failed to start bot', 'danger');
        }
    }

    async stopBot(botId) {
        try {
            const response = await fetch(`/api/bots/${botId}/stop`, { method: 'POST' });
            if (response.ok) {
                this.showAlert('Bot stopped successfully', 'success');
                this.loadBots();
            } else {
                const error = await response.json();
                this.showAlert(`Failed to stop bot: ${error.detail}`, 'danger');
            }
        } catch (error) {
            this.showAlert('Failed to stop bot', 'danger');
        }
    }

    async restartBot(botId) {
        try {
            const response = await fetch(`/api/bots/${botId}/restart`, { method: 'POST' });
            if (response.ok) {
                this.showAlert('Bot restarted successfully', 'success');
                this.loadBots();
            } else {
                const error = await response.json();
                this.showAlert(`Failed to restart bot: ${error.detail}`, 'danger');
            }
        } catch (error) {
            this.showAlert('Failed to restart bot', 'danger');
        }
    }

    async editBot(botId) {
        try {
            // Get bot data
            const response = await fetch(`/api/bots/${botId}`);
            if (!response.ok) {
                this.showAlert('Failed to load bot data', 'danger');
                return;
            }
            
            const bot = await response.json();
            
            // Populate edit form
            document.getElementById('edit-bot-id').value = bot.bot_id;
            document.getElementById('edit-bot-name').value = bot.name;
            document.getElementById('edit-bot-personality').value = bot.personality || '';
            document.getElementById('edit-bot-template').value = bot.template_id || '';
            document.getElementById('edit-bot-discord-token').value = bot.discord_token_id || '';
            document.getElementById('edit-bot-log-level').value = bot.log_level || 'INFO';
            document.getElementById('edit-bot-enabled').checked = bot.enabled || false;
            document.getElementById('edit-bot-auto-start').checked = bot.auto_start || false;
            document.getElementById('edit-bot-load-embedder').checked = bot.load_embedder || false;
            
            // Show modal
            const modal = new bootstrap.Modal(document.getElementById('editBotModal'));
            modal.show();
        } catch (error) {
            console.error('Failed to edit bot:', error);
            this.showAlert('Failed to load bot for editing', 'danger');
        }
    }

    async saveEditBot() {
        try {
            const botId = document.getElementById('edit-bot-id').value;
            const updates = {
                name: document.getElementById('edit-bot-name').value,
                personality: document.getElementById('edit-bot-personality').value,
                template_id: document.getElementById('edit-bot-template').value,
                discord_token_id: document.getElementById('edit-bot-discord-token').value,
                log_level: document.getElementById('edit-bot-log-level').value,
                enabled: document.getElementById('edit-bot-enabled').checked,
                auto_start: document.getElementById('edit-bot-auto-start').checked,
                load_embedder: document.getElementById('edit-bot-load-embedder').checked
            };

            const response = await fetch(`/api/bots/${botId}`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(updates)
            });

            if (response.ok) {
                this.showAlert('Bot updated successfully', 'success');
                
                // Close modal
                const modal = bootstrap.Modal.getInstance(document.getElementById('editBotModal'));
                modal.hide();
                
                // Refresh bot list
                this.loadBots();
            } else {
                const error = await response.json();
                this.showAlert(`Failed to update bot: ${error.detail}`, 'danger');
            }
        } catch (error) {
            console.error('Failed to update bot:', error);
            this.showAlert('Failed to update bot', 'danger');
        }
    }

    async deleteBot(botId) {
        if (!confirm('Are you sure you want to delete this bot?')) {
            return;
        }
        
        try {
            const response = await fetch(`/api/bots/${botId}`, { method: 'DELETE' });
            if (response.ok) {
                this.showAlert('Bot deleted successfully', 'success');
                this.loadBots();
            } else {
                const error = await response.json();
                this.showAlert(`Failed to delete bot: ${error.detail}`, 'danger');
            }
        } catch (error) {
            this.showAlert('Failed to delete bot', 'danger');
        }
    }

    // Memory Management
    async openMemoryManagement(botId, botName) {
        this.currentMemoryBotId = botId;
        document.getElementById('memory-bot-name').textContent = botName;
        
        const modal = new bootstrap.Modal(document.getElementById('memoryManagementModal'));
        modal.show();
        
        await this.loadMemories();
    }

    async loadMemories() {
        if (!this.currentMemoryBotId) return;
        
        try {
            let apiUrl = `/api/bots/${this.currentMemoryBotId}/memories?limit=1000`;
            if (this.selectedServerId) {
                apiUrl += `&server_id=${encodeURIComponent(this.selectedServerId)}`;
            }
            
            const response = await fetch(apiUrl);
            if (response.ok) {
                const data = await response.json();
                this.memories = data.memories || [];
                
                // Update available servers list if we got aggregated data
                if (data.servers && !this.selectedServerId) {
                    this.availableServers = data.servers;
                    this.populateServerFilter();
                }
                
                this.filteredMemories = this.memories;
                this.updateMemoryDisplay();
            } else {
                this.showAlert('Failed to load memories', 'danger');
            }
        } catch (error) {
            console.error('Failed to load memories:', error);
            this.showAlert('Failed to load memories', 'danger');
        }
    }

    populateServerFilter() {
        const select = document.getElementById('memory-server-filter');
        if (!select) {
            // Create server filter if it doesn't exist
            const filterRow = document.querySelector('#memoryManagementModal .row.mb-3');
            if (filterRow) {
                const serverCol = document.createElement('div');
                serverCol.className = 'col-md-4';
                serverCol.innerHTML = `
                    <select class="form-select" id="memory-server-filter" onchange="dashboard.filterByServer(this.value)">
                        <option value="">All Servers</option>
                    </select>
                `;
                filterRow.appendChild(serverCol);
            }
        }
        
        const serverSelect = document.getElementById('memory-server-filter');
        if (serverSelect) {
            serverSelect.innerHTML = '<option value="">All Servers</option>';
            this.availableServers.forEach(server => {
                const option = document.createElement('option');
                option.value = server.server_id;
                option.textContent = `${server.server_name} (${server.memory_count})`;
                serverSelect.appendChild(option);
            });
        }
    }

    filterByServer(serverId) {
        this.selectedServerId = serverId;
        this.loadMemories();
    }

    updateMemoryDisplay() {
        document.getElementById('total-memories-count').textContent = this.memories.length;
        document.getElementById('filtered-memories-count').textContent = this.filteredMemories.length;
        
        this.renderMemoryList();
        this.renderMemoryPagination();
    }

    renderMemoryList() {
        const container = document.getElementById('memory-list');
        container.innerHTML = '';
        
        if (this.filteredMemories.length === 0) {
            container.innerHTML = '<div class="text-center py-3 text-muted">No memories found</div>';
            return;
        }
        
        const startIndex = (this.memoryPage - 1) * this.memoryPageSize;
        const endIndex = Math.min(startIndex + this.memoryPageSize, this.filteredMemories.length);
        
        for (let i = startIndex; i < endIndex; i++) {
            const memory = this.filteredMemories[i];
            const memoryCard = document.createElement('div');
            memoryCard.className = 'card mb-2';
            memoryCard.innerHTML = `
                <div class="card-body">
                    <div class="d-flex justify-content-between align-items-start">
                        <div class="flex-grow-1">
                            <p class="card-text">${this.escapeHtml(memory.content)}</p>
                            <small class="text-muted">
                                Added: ${new Date(memory.timestamp).toLocaleString()}
                                ${memory.server_name ? `• Server: ${memory.server_name}` : ''}
                            </small>
                        </div>
                        <button class="btn btn-outline-danger btn-sm ms-2" onclick="dashboard.deleteMemory('${memory.id}')" title="Delete Memory">
                            <i class="bi bi-trash"></i>
                        </button>
                    </div>
                </div>
            `;
            container.appendChild(memoryCard);
        }
    }

    renderMemoryPagination() {
        const totalPages = Math.ceil(this.filteredMemories.length / this.memoryPageSize);
        const paginationContainer = document.getElementById('memory-pagination');
        const paginationInfo = document.getElementById('memory-pagination-info');
        
        if (totalPages <= 1) {
            paginationContainer.innerHTML = '';
            paginationInfo.textContent = '';
            return;
        }
        
        const startItem = (this.memoryPage - 1) * this.memoryPageSize + 1;
        const endItem = Math.min(this.memoryPage * this.memoryPageSize, this.filteredMemories.length);
        paginationInfo.textContent = `Showing ${startItem}-${endItem} of ${this.filteredMemories.length}`;
        
        let paginationHTML = '';
        
        // Previous button
        paginationHTML += `<li class="page-item ${this.memoryPage === 1 ? 'disabled' : ''}">
            <a class="page-link" href="#" onclick="dashboard.goToMemoryPage(${this.memoryPage - 1})">Previous</a>
        </li>`;
        
        // Page numbers
        for (let i = 1; i <= totalPages; i++) {
            if (i === 1 || i === totalPages || (i >= this.memoryPage - 2 && i <= this.memoryPage + 2)) {
                paginationHTML += `<li class="page-item ${i === this.memoryPage ? 'active' : ''}">
                    <a class="page-link" href="#" onclick="dashboard.goToMemoryPage(${i})">${i}</a>
                </li>`;
            } else if (i === this.memoryPage - 3 || i === this.memoryPage + 3) {
                paginationHTML += '<li class="page-item disabled"><span class="page-link">...</span></li>';
            }
        }
        
        // Next button
        paginationHTML += `<li class="page-item ${this.memoryPage === totalPages ? 'disabled' : ''}">
            <a class="page-link" href="#" onclick="dashboard.goToMemoryPage(${this.memoryPage + 1})">Next</a>
        </li>`;
        
        paginationContainer.innerHTML = paginationHTML;
    }

    goToMemoryPage(page) {
        const totalPages = Math.ceil(this.filteredMemories.length / this.memoryPageSize);
        if (page >= 1 && page <= totalPages) {
            this.memoryPage = page;
            this.renderMemoryList();
            this.renderMemoryPagination();
        }
    }

    searchMemories(searchTerm) {
        if (!searchTerm.trim()) {
            this.filteredMemories = this.memories;
        } else {
            const term = searchTerm.toLowerCase();
            this.filteredMemories = this.memories.filter(memory =>
                memory.content.toLowerCase().includes(term)
            );
        }
        this.memoryPage = 1;
        this.updateMemoryDisplay();
    }

    showAddMemoryForm() {
        document.getElementById('add-memory-form').style.display = 'block';
        document.getElementById('new-memory-content').focus();
    }

    hideAddMemoryForm() {
        document.getElementById('add-memory-form').style.display = 'none';
        document.getElementById('new-memory-content').value = '';
    }

    async addMemory() {
        const content = document.getElementById('new-memory-content').value.trim();
        if (!content) {
            this.showAlert('Please enter memory content', 'warning');
            return;
        }
        
        try {
            const payload = {
                content: content
            };
            
            // Include server_id if a specific server is selected
            if (this.selectedServerId) {
                payload.server_id = this.selectedServerId;
            }
            
            const response = await fetch(`/api/bots/${this.currentMemoryBotId}/memories`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(payload)
            });
            
            if (response.ok) {
                this.showAlert('Memory added successfully', 'success');
                this.hideAddMemoryForm();
                await this.loadMemories();
            } else {
                const error = await response.json();
                this.showAlert(`Failed to add memory: ${error.detail}`, 'danger');
            }
        } catch (error) {
            console.error('Failed to add memory:', error);
            this.showAlert('Failed to add memory', 'danger');
        }
    }

    async deleteMemory(memoryId) {
        if (!confirm('Are you sure you want to delete this memory?')) {
            return;
        }
        
        try {
            const response = await fetch(`/api/bots/${this.currentMemoryBotId}/memories/${memoryId}`, {
                method: 'DELETE'
            });
            
            if (response.ok) {
                this.showAlert('Memory deleted successfully', 'success');
                await this.loadMemories();
            } else {
                const error = await response.json();
                this.showAlert(`Failed to delete memory: ${error.detail}`, 'danger');
            }
        } catch (error) {
            console.error('Failed to delete memory:', error);
            this.showAlert('Failed to delete memory', 'danger');
        }
    }

    refreshMemories() {
        this.loadMemories();
    }

    // Helper function to get bot's Discord server ID
    getBotServerId(botId) {
        // Try to get it from the cached bot data first
        if (this.bots) {
            const bot = this.bots.find(b => b.bot_id === botId);
            if (bot && bot.guild_ids && bot.guild_ids.length > 0) {
                return bot.guild_ids[0].toString();
            }
        }
        // Fallback to admin if we can't get the guild ID
        return 'admin';
    }

    // Chat Settings Management
    async loadBotChatFrequency(botId, botData = null) {
        try {
            // Get the bot's server ID from passed data or cached data
            let serverId = 'admin'; // default fallback
            if (botData && botData.guild_ids && botData.guild_ids.length > 0) {
                serverId = botData.guild_ids[0].toString();
            } else {
                serverId = this.getBotServerId(botId);
            }
            
            const response = await fetch(`/api/bots/${botId}/chat-settings?server_id=${serverId}`);
            if (response.ok) {
                const data = await response.json();
                const cell = document.getElementById(`chat-freq-${botId}`);
                if (cell) {
                    cell.innerHTML = `${data.chat_frequency}%`;
                }
            } else {
                const cell = document.getElementById(`chat-freq-${botId}`);
                if (cell) {
                    cell.innerHTML = 'N/A';
                }
            }
        } catch (error) {
            const cell = document.getElementById(`chat-freq-${botId}`);
            if (cell) {
                cell.innerHTML = 'Error';
            }
        }
    }

    async openChatSettings(botId, botName) {
        this.currentChatBotId = botId;
        document.getElementById('chat-bot-name').textContent = botName;
        
        const modal = new bootstrap.Modal(document.getElementById('chatSettingsModal'));
        modal.show();
        
        await this.loadChatSettings();
    }

    async loadChatSettings() {
        if (!this.currentChatBotId) return;
        
        try {
            // Get the bot's server ID from cached data
            const serverId = this.getBotServerId(this.currentChatBotId);
            const response = await fetch(`/api/bots/${this.currentChatBotId}/chat-settings?server_id=${serverId}`);
            if (response.ok) {
                const data = await response.json();
                
                // Update chat frequency input
                document.getElementById('chat-frequency-input').value = data.chat_frequency || 0;
                
                // Update activity data display
                this.renderChatActivityData(data.activity_data);
                
                // Update server info
                document.getElementById('chat-server-id').textContent = data.server_id || 'admin';
                
            } else {
                this.showAlert('Failed to load chat settings', 'danger');
            }
        } catch (error) {
            console.error('Failed to load chat settings:', error);
            this.showAlert('Failed to load chat settings', 'danger');
        }
    }

    renderChatActivityData(activityData) {
        const container = document.getElementById('chat-activity-data');
        if (!activityData || Object.keys(activityData).length === 0) {
            container.innerHTML = '<div class="text-muted">No activity data available</div>';
            return;
        }

        let html = '<div class="row">';
        let count = 0;
        for (const [channelId, activity] of Object.entries(activityData)) {
            const lastHuman = activity.last_human_message ? 
                new Date(activity.last_human_message * 1000).toLocaleString() : 'Never';
            const lastBot = activity.last_bot_message ? 
                new Date(activity.last_bot_message * 1000).toLocaleString() : 'Never';
            
            html += `
                <div class="col-md-6 mb-3">
                    <div class="card border-secondary">
                        <div class="card-body p-2">
                            <h6 class="card-title mb-1">Channel: ${channelId}</h6>
                            <small class="text-muted d-block">Messages: ${activity.message_count || 0}</small>
                            <small class="text-muted d-block">Last Human: ${lastHuman}</small>
                            <small class="text-muted d-block">Last Bot: ${lastBot}</small>
                        </div>
                    </div>
                </div>
            `;
            count++;
            if (count >= 4) break; // Limit to 4 channels to prevent overflow
        }
        html += '</div>';
        
        if (count === 0) {
            html = '<div class="text-muted">No channel activity data available</div>';
        }
        
        container.innerHTML = html;
    }

    async saveChatFrequency() {
        const frequency = parseInt(document.getElementById('chat-frequency-input').value);
        
        if (isNaN(frequency) || frequency < 0 || frequency > 100) {
            this.showAlert('Please enter a valid frequency between 0 and 100', 'warning');
            return;
        }
        
        try {
            // Get the bot's server ID from cached data
            const serverId = this.getBotServerId(this.currentChatBotId);
            const response = await fetch(`/api/bots/${this.currentChatBotId}/chat-frequency`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    server_id: serverId,
                    frequency: frequency
                })
            });
            
            if (response.ok) {
                this.showAlert('Chat frequency updated successfully', 'success');
                // Refresh the bot table to show updated frequency
                this.loadBotChatFrequency(this.currentChatBotId);
                // Close modal
                const modal = bootstrap.Modal.getInstance(document.getElementById('chatSettingsModal'));
                modal.hide();
            } else {
                const error = await response.json();
                this.showAlert(`Failed to update chat frequency: ${error.detail}`, 'danger');
            }
        } catch (error) {
            console.error('Failed to update chat frequency:', error);
            this.showAlert('Failed to update chat frequency', 'danger');
        }
    }

    async resetBotActivity() {
        if (!confirm('Are you sure you want to reset all activity data for this bot?')) {
            return;
        }
        
        try {
            const response = await fetch(`/api/bots/${this.currentChatBotId}/reset-activity`, {
                method: 'POST'
            });
            
            if (response.ok) {
                this.showAlert('Activity data reset successfully', 'success');
                // Reload the chat settings to show cleared data
                await this.loadChatSettings();
            } else {
                const error = await response.json();
                this.showAlert(`Failed to reset activity: ${error.detail}`, 'danger');
            }
        } catch (error) {
            console.error('Failed to reset activity:', error);
            this.showAlert('Failed to reset activity', 'danger');
        }
    }

    // Bot Logs Management
    async viewBotLogs(botId) {
        try {
            const response = await fetch(`/api/bots/${botId}/logs`);
            if (response.ok) {
                const logs = await response.json();
                this.showBotLogsModal(botId, logs.logs || []);
            } else {
                const error = await response.json();
                this.showAlert(`Failed to load bot logs: ${error.detail}`, 'danger');
            }
        } catch (error) {
            console.error('Failed to load bot logs:', error);
            this.showAlert('Failed to load bot logs', 'danger');
        }
    }

    showBotLogsModal(botId, logs) {
        // Store current logs and bot ID
        this.currentLogsBotId = botId;
        this.currentLogs = logs;
        this.filteredLogs = logs;
        
        // Set bot name in modal
        const bot = this.bots?.find(b => b.bot_id === botId);
        document.getElementById('logs-bot-name').textContent = bot?.name || botId;
        
        // Render logs
        this.renderBotLogs();
        
        // Show modal
        const modal = new bootstrap.Modal(document.getElementById('botLogsModal'));
        modal.show();
    }

    renderBotLogs() {
        const container = document.getElementById('bot-logs-list');
        const logsCount = document.getElementById('logs-count');
        const lastUpdated = document.getElementById('logs-last-updated');
        
        if (!this.filteredLogs || this.filteredLogs.length === 0) {
            container.innerHTML = '<div class="text-center py-3 text-muted">No logs available</div>';
            logsCount.textContent = '0 logs';
            return;
        }
        
        logsCount.textContent = `${this.filteredLogs.length} logs`;
        lastUpdated.textContent = `Last updated: ${new Date().toLocaleString()}`;
        
        // Reverse logs to show newest first
        const sortedLogs = [...this.filteredLogs].reverse();
        
        container.innerHTML = '';
        
        sortedLogs.forEach(log => {
            const logEntry = document.createElement('div');
            logEntry.className = `log-entry mb-2 p-2 border-start border-3 ${this.getLogLevelClass(log.level)}`;
            
            const timestamp = new Date(log.timestamp).toLocaleString();
            const level = log.level.toUpperCase();
            
            logEntry.innerHTML = `
                <div class="d-flex justify-content-between align-items-start">
                    <div class="flex-grow-1">
                        <div class="d-flex align-items-center mb-1">
                            <span class="badge ${this.getLogLevelBadge(log.level)} me-2">${level}</span>
                            <small class="text-muted">${timestamp}</small>
                            <small class="text-muted ms-2">${log.logger || 'grugthink'}</small>
                        </div>
                        <div class="log-message" style="font-family: monospace; font-size: 0.9rem;">
                            ${this.escapeHtml(log.message)}
                        </div>
                        ${this.renderLogExtras(log)}
                    </div>
                </div>
            `;
            
            container.appendChild(logEntry);
        });
        
        // Scroll to top of logs
        container.scrollTop = 0;
    }

    getLogLevelClass(level) {
        const classes = {
            'DEBUG': 'border-secondary',
            'INFO': 'border-primary',
            'WARNING': 'border-warning',
            'ERROR': 'border-danger'
        };
        return classes[level.toUpperCase()] || 'border-secondary';
    }

    getLogLevelBadge(level) {
        const badges = {
            'DEBUG': 'bg-secondary',
            'INFO': 'bg-primary',
            'WARNING': 'bg-warning',
            'ERROR': 'bg-danger'
        };
        return badges[level.toUpperCase()] || 'bg-secondary';
    }

    renderLogExtras(log) {
        let extras = '';
        
        // Show error details if present
        if (log.error) {
            extras += `<div class="mt-1 text-danger small"><strong>Error:</strong> ${this.escapeHtml(log.error)}</div>`;
        }
        
        // Show user and server info if present
        if (log.user_id || log.server_id) {
            extras += '<div class="mt-1 text-muted small">';
            if (log.user_id) extras += `User: ${log.user_id} `;
            if (log.server_id) extras += `Server: ${log.server_id}`;
            extras += '</div>';
        }
        
        // Show other extra fields
        const excludeFields = ['message', 'level', 'timestamp', 'logger', 'bot_id', 'user_id', 'server_id', 'error'];
        const extraFields = Object.keys(log).filter(key => !excludeFields.includes(key));
        
        if (extraFields.length > 0) {
            extras += '<div class="mt-1 text-muted small">';
            extraFields.forEach(field => {
                extras += `<span class="me-2"><strong>${field}:</strong> ${this.escapeHtml(String(log[field]))}</span>`;
            });
            extras += '</div>';
        }
        
        return extras;
    }

    filterLogsByLevel(level) {
        if (!this.currentLogs) return;
        
        if (!level) {
            this.filteredLogs = this.currentLogs;
        } else {
            this.filteredLogs = this.currentLogs.filter(log => 
                log.level.toUpperCase() === level.toUpperCase()
            );
        }
        
        this.renderBotLogs();
    }

    async refreshBotLogs() {
        if (!this.currentLogsBotId) return;
        
        try {
            const response = await fetch(`/api/bots/${this.currentLogsBotId}/logs`);
            if (response.ok) {
                const logs = await response.json();
                this.currentLogs = logs.logs || [];
                
                // Reapply current filter
                const filterLevel = document.getElementById('log-level-filter').value;
                this.filterLogsByLevel(filterLevel);
                
                this.showAlert('Logs refreshed', 'success');
            } else {
                this.showAlert('Failed to refresh logs', 'danger');
            }
        } catch (error) {
            console.error('Failed to refresh logs:', error);
            this.showAlert('Failed to refresh logs', 'danger');
        }
    }

    // Template Management  
    async loadTemplates() {
        try {
            const response = await fetch('/api/templates');
            if (response.ok) {
                const templates = await response.json();
                this.populateTemplates(templates);
                this.populateTemplateSelects(templates);
            }
        } catch (error) {
            console.error('Failed to load templates:', error);
        }
    }

    populateTemplates(templates) {
        const container = document.getElementById('templates-container');
        container.innerHTML = '';
        
        Object.entries(templates).forEach(([templateId, template]) => {
            const card = document.createElement('div');
            card.className = 'col-md-6 col-lg-4 mb-3';
            card.innerHTML = `
                <div class="card h-100">
                    <div class="card-body">
                        <h5 class="card-title">${template.name || templateId}</h5>
                        <p class="card-text">${template.description || 'No description available'}</p>
                        <p class="text-muted small">
                            <strong>Personality:</strong> ${template.personality || 'Default'}<br>
                            <strong>Embedder:</strong> ${template.load_embedder ? 'Enabled' : 'Disabled'}
                        </p>
                    </div>
                    <div class="card-footer">
                        <button class="btn btn-outline-primary btn-sm" onclick="dashboard.editTemplate('${templateId}')">
                            <i class="bi bi-pencil"></i> Edit
                        </button>
                    </div>
                </div>
            `;
            container.appendChild(card);
        });
    }

    populateTemplateSelects(templates) {
        const selects = document.querySelectorAll('#bot-template, #edit-bot-template');
        selects.forEach(select => {
            select.innerHTML = '<option value="">Select a template</option>';
            Object.entries(templates).forEach(([templateId, template]) => {
                const option = document.createElement('option');
                option.value = templateId;
                option.textContent = template.name || templateId;
                select.appendChild(option);
            });
        });
    }

    // Token Management
    async loadTokens() {
        try {
            console.log('Loading tokens...');
            const response = await fetch('/api/discord-tokens');
            if (response.ok) {
                const tokens = await response.json();
                console.log('Loaded tokens:', tokens);
                this.populateTokenList(tokens);
                this.populateTokenSelects(tokens);
            } else {
                console.error('Token load failed:', response.status, response.statusText);
            }
        } catch (error) {
            console.error('Failed to load tokens:', error);
        }
    }

    populateTokenList(tokens) {
        const container = document.getElementById('token-list');
        container.innerHTML = '';
        
        tokens.forEach(token => {
            const tokenItem = document.createElement('div');
            tokenItem.className = 'border p-2 mb-2 rounded';
            tokenItem.innerHTML = `
                <div class="d-flex justify-content-between align-items-center">
                    <div>
                        <strong>${token.name}</strong>
                        <div class="text-muted small">ID: ${token.id}</div>
                    </div>
                    <button class="btn btn-outline-danger btn-sm" onclick="dashboard.deleteToken('${token.id}')">
                        <i class="bi bi-trash"></i>
                    </button>
                </div>
            `;
            container.appendChild(tokenItem);
        });
    }

    populateTokenSelects(tokens) {
        const selects = document.querySelectorAll('#bot-discord-token, #edit-bot-discord-token');
        selects.forEach(select => {
            select.innerHTML = '<option value="">Select a token</option>';
            tokens.forEach(token => {
                const option = document.createElement('option');
                option.value = token.id;
                option.textContent = token.name;
                select.appendChild(option);
            });
        });
    }

    // Add Discord token
    async addToken() {
        const name = document.getElementById('token-name').value.trim();
        const token = document.getElementById('discord-token').value.trim();
        const submitBtn = document.querySelector('#add-token-form button[type="submit"]');
        
        if (!name || !token) {
            this.showAlert('Please fill in both token name and Discord token', 'warning');
            return;
        }
        
        // Show loading state
        const originalText = submitBtn.textContent;
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<i class="bi bi-hourglass-split"></i> Adding...';
        
        try {
            const response = await fetch('/api/discord-tokens', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    name: name,
                    token: token
                })
            });
            
            if (response.ok) {
                this.showAlert('Discord token added successfully! Refreshing list...', 'success');
                
                // Clear form
                document.getElementById('token-name').value = '';
                document.getElementById('discord-token').value = '';
                
                // Immediate reload - cache is now cleared on backend
                await this.loadTokens();
            } else {
                const error = await response.json();
                this.showAlert(`Failed to add token: ${error.detail}`, 'danger');
            }
        } catch (error) {
            console.error('Failed to add token:', error);
            this.showAlert('Network error: Failed to add token', 'danger');
        } finally {
            // Restore button state
            submitBtn.disabled = false;
            submitBtn.innerHTML = originalText;
        }
    }

    // Delete Discord token
    async deleteToken(tokenId) {
        if (!confirm('Are you sure you want to delete this Discord token?')) {
            return;
        }
        
        try {
            const response = await fetch(`/api/discord-tokens/${tokenId}`, {
                method: 'DELETE'
            });
            
            if (response.ok) {
                this.showAlert('Discord token deleted successfully', 'success');
                
                // Reload token list and selects
                await this.loadTokens();
            } else {
                const error = await response.json();
                this.showAlert(`Failed to delete token: ${error.detail}`, 'danger');
            }
        } catch (error) {
            console.error('Failed to delete token:', error);
            this.showAlert('Failed to delete token', 'danger');
        }
    }

    // API Keys Management
    async loadApiKeys() {
        try {
            // Load different API key services separately since there's no general endpoint
            const [geminiResponse, googleResponse] = await Promise.all([
                fetch('/api/api-keys/gemini'),
                fetch('/api/api-keys/google')
            ]);
            
            const keys = {};
            if (geminiResponse.ok) {
                const geminiData = await geminiResponse.json();
                keys.gemini = geminiData.api_key ? '********' : '';
            }
            
            if (googleResponse.ok) {
                const googleData = await googleResponse.json();
                keys.google_api = googleData.api_key ? '********' : '';
                keys.google_cse_id = googleData.cse_id || '';
            }
            
            this.populateApiKeys(keys);
        } catch (error) {
            console.error('Failed to load API keys:', error);
            // Still populate form with empty values if API fails
            this.populateApiKeys({});
        }
    }

    populateApiKeys(keys) {
        document.getElementById('gemini-key').value = keys.gemini ? '********' : '';
        document.getElementById('google-api-key').value = keys.google_api ? '********' : '';
        document.getElementById('google-cse-id').value = keys.google_cse_id || '';
    }

    // Save API keys
    async saveApiKeys() {
        const geminiKey = document.getElementById('gemini-key').value.trim();
        const googleApiKey = document.getElementById('google-api-key').value.trim();
        const googleCseId = document.getElementById('google-cse-id').value.trim();
        
        try {
            const promises = [];
            
            // Save Gemini API key if changed (not just ********)
            if (geminiKey && geminiKey !== '********') {
                promises.push(
                    fetch('/api/api-keys', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({
                            service: 'gemini',
                            key_name: 'api_key',
                            value: geminiKey
                        })
                    })
                );
            }
            
            // Save Google API key if changed (not just ********)
            if (googleApiKey && googleApiKey !== '********') {
                promises.push(
                    fetch('/api/api-keys', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({
                            service: 'google',
                            key_name: 'api_key',
                            value: googleApiKey
                        })
                    })
                );
            }
            
            // Save Google CSE ID if provided
            if (googleCseId) {
                promises.push(
                    fetch('/api/api-keys', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({
                            service: 'google',
                            key_name: 'cse_id',
                            value: googleCseId
                        })
                    })
                );
            }
            
            if (promises.length > 0) {
                const responses = await Promise.all(promises);
                
                // Check if all requests succeeded
                const failed = responses.filter(r => !r.ok);
                if (failed.length === 0) {
                    this.showAlert('API keys saved successfully', 'success');
                    // Reload API keys to show updated status
                    await this.loadApiKeys();
                } else {
                    this.showAlert('Some API keys failed to save', 'warning');
                }
            } else {
                this.showAlert('No API keys were updated', 'info');
            }
        } catch (error) {
            console.error('Failed to save API keys:', error);
            this.showAlert('Failed to save API keys', 'danger');
        }
    }

    // Template Management Functions
    async editTemplate(templateId) {
        this.showAlert('Template editing not implemented yet', 'info');
    }

    // AI Personality Generation
    async generatePersonality() {
        const personalityId = document.getElementById('personality-id').value.trim();
        const description = document.getElementById('personality-description').value.trim();
        
        if (!personalityId || !description) {
            this.showAlert('Please fill in both personality ID and description', 'warning');
            return;
        }
        
        // Show loading state
        document.getElementById('generation-progress').style.display = 'block';
        document.getElementById('generate-personality-btn').disabled = true;
        
        try {
            const response = await fetch('/api/personalities/generate', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    personality_id: personalityId,
                    description: description
                })
            });
            
            if (response.ok) {
                const result = await response.json();
                
                // Show generated YAML
                document.getElementById('generated-yaml').value = result.generated_yaml;
                document.getElementById('generation-result').style.display = 'block';
                
                // Hide loading and show save button
                document.getElementById('generation-progress').style.display = 'none';
                document.getElementById('generate-personality-btn').style.display = 'none';
                document.getElementById('save-personality-btn').style.display = 'inline-block';
                
                this.showAlert('Personality generated successfully', 'success');
            } else {
                const error = await response.json();
                this.showAlert(`Failed to generate personality: ${error.detail}`, 'danger');
                document.getElementById('generation-progress').style.display = 'none';
                document.getElementById('generate-personality-btn').disabled = false;
            }
        } catch (error) {
            console.error('Failed to generate personality:', error);
            this.showAlert('Failed to generate personality', 'danger');
            document.getElementById('generation-progress').style.display = 'none';
            document.getElementById('generate-personality-btn').disabled = false;
        }
    }

    async saveGeneratedPersonality() {
        const personalityId = document.getElementById('personality-id').value.trim();
        const generatedYaml = document.getElementById('generated-yaml').value;
        
        try {
            // The personality should already be saved by the generate endpoint
            // Just close modal and refresh templates
            this.showAlert('Personality saved successfully', 'success');
            
            // Close modal
            const modal = bootstrap.Modal.getInstance(document.getElementById('createPersonalityModal'));
            modal.hide();
            
            // Refresh templates
            await this.loadTemplates();
            
            // Reset form
            document.getElementById('personality-id').value = '';
            document.getElementById('personality-description').value = '';
            document.getElementById('generated-yaml').value = '';
            document.getElementById('generation-result').style.display = 'none';
            document.getElementById('generate-personality-btn').style.display = 'inline-block';
            document.getElementById('save-personality-btn').style.display = 'none';
            document.getElementById('generate-personality-btn').disabled = false;
            
        } catch (error) {
            console.error('Failed to save personality:', error);
            this.showAlert('Failed to save personality', 'danger');
        }
    }

    // Dashboard Stats
    updateDashboardStats() {
        // Get current bot data and update dashboard stats
        fetch('/api/bots')
            .then(response => response.json())
            .then(bots => {
                const runningBots = bots.filter(bot => bot.status === 'running').length;
                const totalBots = bots.length;
                const totalServers = [...new Set(bots.flatMap(bot => bot.guild_ids || []))].length;
                
                // Update dashboard cards (fix element IDs to match HTML)
                const runningElement = document.getElementById('running-bots');
                const totalElement = document.getElementById('total-bots');
                const serversElement = document.getElementById('total-guilds');
                
                if (runningElement) runningElement.textContent = runningBots;
                if (totalElement) totalElement.textContent = totalBots;
                if (serversElement) serversElement.textContent = totalServers;
            })
            .catch(error => {
                console.error('Failed to update dashboard stats:', error);
            });
    }

    // Utility Methods
    // System Logs Management
    async updateLogs() {
        try {
            const response = await fetch('/api/system/logs');
            if (response.ok) {
                const data = await response.json();
                this.displaySystemLogs(data.logs || []);
            } else {
                console.error('Failed to load system logs:', await response.text());
            }
        } catch (error) {
            console.error('Error loading system logs:', error);
        }
    }

    displaySystemLogs(logs) {
        const container = document.getElementById('log-container');
        if (!container) return;

        if (logs.length === 0) {
            container.innerHTML = '<p class="text-muted">No system logs available</p>';
            return;
        }

        // Show last 100 logs, newest first
        const recentLogs = logs.slice(-100).reverse();
        
        container.innerHTML = ''; // Clear container

        recentLogs.forEach(log => {
            const logEntry = document.createElement('div');
            const level = log.level.toUpperCase();
            logEntry.className = `log-entry mb-2 p-2 border-start border-3 ${this.getLogLevelClass(level)}`;
            
            const timestamp = new Date(log.timestamp).toLocaleString();
            
            logEntry.innerHTML = `
                <div class="d-flex justify-content-between align-items-start">
                    <div class="flex-grow-1">
                        <div class="d-flex align-items-center mb-1">
                            <span class="badge ${this.getLogLevelBadge(level)} me-2">${level}</span>
                            <small class="text-muted">${timestamp}</small>
                            <small class="text-muted ms-2">${log.logger || 'grugthink'}</small>
                        </div>
                        <div class="log-message" style="font-family: monospace; font-size: 0.9rem;">
                            ${this.escapeHtml(log.message)}
                        </div>
                        ${this.renderLogExtras(log)}
                    </div>
                </div>
            `;
            
            container.appendChild(logEntry);
        });

        // Auto-scroll to top
        container.scrollTop = 0;
    }

    getLogLevelClass(level) {
        const levelClasses = {
            'DEBUG': 'border-secondary text-muted',
            'INFO': 'border-info text-info',
            'WARNING': 'border-warning text-warning',
            'ERROR': 'border-danger text-danger',
            'CRITICAL': 'border-danger text-danger bg-danger bg-opacity-10'
        };
        return levelClasses[level] || 'border-secondary text-muted';
    }

    formatLogExtra(extra) {
        if (typeof extra === 'object') {
            return Object.entries(extra)
                .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
                .join(', ');
        }
        return extra;
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    showAlert(message, type = 'info') {
        const alertContainer = document.createElement('div');
        alertContainer.className = `alert alert-${type} alert-dismissible fade show position-fixed`;
        alertContainer.style.cssText = 'top: 20px; right: 20px; z-index: 9999; max-width: 400px;';
        alertContainer.innerHTML = `
            ${message}
            <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        `;
        document.body.appendChild(alertContainer);
        
        setTimeout(() => {
            if (alertContainer.parentNode) {
                alertContainer.parentNode.removeChild(alertContainer);
            }
        }, 5000);
    }
}

// Initialize dashboard when page loads
let dashboard;
document.addEventListener('DOMContentLoaded', function() {
    // Load saved theme
    const savedTheme = localStorage.getItem('theme') || 'light';
    document.documentElement.setAttribute('data-bs-theme', savedTheme);
    
    // Update theme button
    const themeIcon = document.getElementById('theme-icon');
    const themeText = document.getElementById('theme-text');
    if (savedTheme === 'dark') {
        themeIcon.className = 'bi bi-sun-fill';
        themeText.textContent = 'Light';
    }
    
    // Initialize dashboard
    dashboard = new Dashboard();
});

// Global functions for HTML onclick handlers
function refreshDashboard() {
    dashboard.loadBots();
    dashboard.updateDashboardStats();
}

async function createBot() {
    const name = document.getElementById('bot-name').value.trim();
    const templateId = document.getElementById('bot-template').value;
    const discordTokenId = document.getElementById('bot-discord-token').value;
    const geminiKey = document.getElementById('bot-gemini-key').value.trim();
    
    if (!name || !templateId || !discordTokenId) {
        dashboard.showAlert('Please fill in all required fields', 'warning');
        return;
    }
    
    try {
        const requestBody = {
            name: name,
            template_id: templateId,
            discord_token_id: discordTokenId
        };
        
        // Add optional Gemini API key if provided
        if (geminiKey) {
            requestBody.gemini_api_key = geminiKey;
        }
        
        const response = await fetch('/api/bots', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(requestBody)
        });
        
        if (response.ok) {
            const result = await response.json();
            dashboard.showAlert(`Bot '${name}' created successfully`, 'success');
            
            // Clear form
            document.getElementById('bot-name').value = '';
            document.getElementById('bot-template').value = '';
            document.getElementById('bot-discord-token').value = '';
            document.getElementById('bot-gemini-key').value = '';
            
            // Close modal
            const modal = bootstrap.Modal.getInstance(document.getElementById('createBotModal'));
            modal.hide();
            
            // Refresh bot list
            dashboard.loadBots();
        } else {
            const error = await response.json();
            dashboard.showAlert(`Failed to create bot: ${error.detail}`, 'danger');
        }
    } catch (error) {
        console.error('Failed to create bot:', error);
        dashboard.showAlert('Failed to create bot', 'danger');
    }
}

// Apply saved theme on page load
const savedTheme = localStorage.getItem('theme');
if (savedTheme) {
    document.documentElement.setAttribute('data-bs-theme', savedTheme);
}