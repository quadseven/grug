// Simple GrugThink Dashboard - Clean implementation
// No conflicts, no complex state management, just working buttons

class SimpleDashboard {
    constructor() {
        this.init();
    }

    init() {
        console.log('SimpleDashboard initialized');
        this.setupTheme();
        this.loadInitialData();
    }

    // Theme management
    setupTheme() {
        const themeToggle = document.querySelector('.theme-toggle');
        if (themeToggle) {
            themeToggle.addEventListener('click', () => this.toggleTheme());
        }
    }

    toggleTheme() {
        const html = document.documentElement;
        const currentTheme = html.getAttribute('data-bs-theme');
        const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
        
        html.setAttribute('data-bs-theme', newTheme);
        localStorage.setItem('grugthink-theme', newTheme);
        
        const icon = document.getElementById('theme-icon');
        const text = document.getElementById('theme-text');
        
        if (newTheme === 'dark') {
            icon.className = 'bi bi-sun-fill';
            text.textContent = 'Light';
        } else {
            icon.className = 'bi bi-moon-fill';
            text.textContent = 'Dark';
        }
    }

    // Load initial data
    async loadInitialData() {
        await this.loadUser();
        await this.loadBots();
        await this.loadTemplates();
        await this.loadLogs();
    }

    async loadUser() {
        try {
            const response = await fetch('/api/user');
            if (response.ok) {
                const user = await response.json();
                document.getElementById('username').textContent = user.username || 'Admin';
            }
        } catch (error) {
            console.log('User info not available');
        }
    }

    // Token management and API keys are now handled by the embedded system in HTML

    // Template management
    async loadTemplates() {
        try {
            const response = await fetch('/api/templates');
            if (response.ok) {
                const templates = await response.json();
                this.displayTemplates(templates);
            }
        } catch (error) {
            console.error('Error loading templates:', error);
        }
    }

    displayTemplates(templates) {
        const container = document.getElementById('templates-container');
        if (!container) return;
        
        if (!templates || templates.length === 0) {
            container.innerHTML = '<div class="col-12"><p class="text-muted text-center">No templates available</p></div>';
            return;
        }
        
        container.innerHTML = templates.map(template => `
            <div class="col-md-4 mb-4">
                <div class="card h-100">
                    <div class="card-body">
                        <h5 class="card-title">
                            <i class="bi bi-person-circle me-2"></i>
                            ${this.escapeHtml(template.name || template.id)}
                        </h5>
                        <p class="card-text text-muted small">
                            ${this.escapeHtml(template.description || 'No description available')}
                        </p>
                        <div class="mt-3">
                            <span class="badge bg-primary">${this.escapeHtml(template.id)}</span>
                            ${template.traits ? template.traits.slice(0, 3).map(trait =>
                                `<span class="badge bg-secondary ms-1">${this.escapeHtml(trait)}</span>`
                            ).join('') : ''}
                        </div>
                    </div>
                    <div class="card-footer bg-transparent">
                        <button class="btn btn-outline-primary btn-sm" onclick="simpleDashboard.useTemplate('${this.escapeJsString(template.id)}')">
                            <i class="bi bi-plus-circle"></i> Use Template
                        </button>
                    </div>
                </div>
            </div>
        `).join('');
    }

    useTemplate(templateId) {
        // Switch to bots tab and pre-select this template
        const botTab = document.querySelector('[href="#bots"]');
        if (botTab) {
            botTab.click();
            // Pre-select template in create bot modal
            setTimeout(() => {
                const templateSelect = document.getElementById('bot-template');
                if (templateSelect) {
                    templateSelect.value = templateId;
                }
            }, 100);
        }
    }

    // Logs management
    async loadLogs() {
        try {
            const response = await fetch('/api/logs');
            if (response.ok) {
                const logs = await response.json();
                this.displayLogs(logs);
            }
        } catch (error) {
            console.error('Error loading logs:', error);
            this.displayLogs([]);
        }
    }

    displayLogs(logs) {
        const container = document.getElementById('log-container');
        if (!container) return;
        
        if (!logs || logs.length === 0) {
            container.innerHTML = '<p class="text-muted">No recent logs available</p>';
            return;
        }
        
        container.innerHTML = logs.slice(-50).map(log => {
            const timestamp = new Date(log.timestamp).toLocaleTimeString();
            const levelClass = this.getLogLevelClass(log.level);
            return `
                <div class="log-entry mb-1">
                    <span class="text-muted small">${timestamp}</span>
                    <span class="badge bg-${levelClass} ms-2">${this.escapeHtml(log.level)}</span>
                    <span class="ms-2">${this.escapeHtml(log.message)}</span>
                </div>
            `;
        }).join('');
        
        // Auto-scroll to bottom
        container.scrollTop = container.scrollHeight;
    }

    getLogLevelClass(level) {
        switch (level?.toUpperCase()) {
            case 'ERROR': return 'danger';
            case 'WARNING': case 'WARN': return 'warning';
            case 'INFO': return 'info';
            case 'DEBUG': return 'secondary';
            default: return 'light';
        }
    }

    // Bot management
    async loadBots() {
        try {
            const response = await fetch('/api/bots');
            if (response.ok) {
                const bots = await response.json();
                this.displayBots(bots);
                this.updateStats(bots);
            }
        } catch (error) {
            console.error('Error loading bots:', error);
        }
    }

    displayBots(bots) {
        const tbody = document.querySelector('#bots-table tbody');
        if (!tbody) return;
        
        if (!bots || bots.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">No bots configured</td></tr>';
            return;
        }
        
        tbody.innerHTML = bots.map(bot => {
            const botId = this.escapeJsString(bot.bot_id);
            return `
            <tr>
                <td><strong>${this.escapeHtml(bot.name)}</strong></td>
                <td><span class="badge bg-${bot.status === 'running' ? 'success' : 'secondary'}">${this.escapeHtml(bot.status)}</span></td>
                <td>${this.escapeHtml(bot.personality || 'Default')}</td>
                <td>${bot.guild_count || 0}</td>
                <td>${bot.uptime || 'N/A'}</td>
                <td>${bot.chat_frequency || 0}%</td>
                <td>
                    ${bot.status === 'running' ?
                        `<button class="btn btn-outline-warning btn-sm" onclick="simpleDashboard.stopBot('${botId}')">Stop</button>` :
                        `<button class="btn btn-outline-success btn-sm" onclick="simpleDashboard.startBot('${botId}')">Start</button>`
                    }
                    <button class="btn btn-outline-danger btn-sm" onclick="simpleDashboard.deleteBot('${botId}')">Delete</button>
                </td>
            </tr>
        `;
        }).join('');
    }

    updateStats(bots) {
        const totalBots = bots ? bots.length : 0;
        const runningBots = bots ? bots.filter(bot => bot.status === 'running').length : 0;
        const totalGuilds = bots ? bots.reduce((sum, bot) => sum + (bot.guild_count || 0), 0) : 0;
        
        const totalElement = document.getElementById('total-bots');
        const runningElement = document.getElementById('running-bots');
        const guildsElement = document.getElementById('total-guilds');
        
        if (totalElement) totalElement.textContent = totalBots;
        if (runningElement) runningElement.textContent = runningBots;
        if (guildsElement) guildsElement.textContent = totalGuilds;
    }

    async startBot(botId) {
        try {
            const response = await fetch(`/api/bots/${botId}/start`, { method: 'POST' });
            if (response.ok) {
                alert('Bot started successfully!');
                await this.loadBots();
            } else {
                alert('Error starting bot');
            }
        } catch (error) {
            console.error('Error starting bot:', error);
            alert('Network error starting bot');
        }
    }

    async stopBot(botId) {
        try {
            const response = await fetch(`/api/bots/${botId}/stop`, { method: 'POST' });
            if (response.ok) {
                alert('Bot stopped successfully!');
                await this.loadBots();
            } else {
                alert('Error stopping bot');
            }
        } catch (error) {
            console.error('Error stopping bot:', error);
            alert('Network error stopping bot');
        }
    }

    async deleteBot(botId) {
        if (!confirm('Are you sure you want to delete this bot?')) {
            return;
        }
        
        try {
            const response = await fetch(`/api/bots/${botId}`, { method: 'DELETE' });
            if (response.ok) {
                alert('Bot deleted successfully!');
                await this.loadBots();
            } else {
                alert('Error deleting bot');
            }
        } catch (error) {
            console.error('Error deleting bot:', error);
            alert('Network error deleting bot');
        }
    }

    // Utility functions
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // Escape a value for safe use inside a single-quoted JS string literal
    // embedded in an inline onclick="..." HTML attribute.
    escapeJsString(value) {
        return this.escapeHtml(String(value)).replace(/'/g, "\\'");
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', function() {
    window.simpleDashboard = new SimpleDashboard();
    console.log('Simple dashboard loaded successfully');
});

// Global refresh function
function refreshDashboard() {
    if (window.simpleDashboard) {
        window.simpleDashboard.loadInitialData();
    }
}