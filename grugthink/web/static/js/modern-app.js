/*!
 * GrugThink Modern App v3.3.1
 * Modern, interactive Discord bot management interface
 */

class GrugThinkApp {
    constructor() {
        this.currentPage = 'dashboard';
        this.sidebarOpen = false;
        this.theme = localStorage.getItem('grugthink-theme') || 'dark';
        this.activityChart = null;
        this.apiBaseUrl = ''; // Configurable API Base URL

        this.init();
    }

    async init() {
        console.log('🦕 GrugThink Modern App v3.3.1 starting...');

        // Initialize core systems
        this.setupTheme();
        this.setupNavigation();
        this.setupEventListeners();
        await this.loadVersionInfo();
        await this.loadDashboardData();
        this.initializeCharts();
        this.initializeLucideIcons();

        // Hide loading overlay
        const loadingOverlay = document.getElementById('loading-overlay');
        if (loadingOverlay) {
            loadingOverlay.style.display = 'none';
        }

        // Show welcome animation
        this.animatePageLoad();

        console.log('✨ GrugThink app ready!');
    }
    
    // ==========================================================================
    // API HELPER
    // ==========================================================================

    async fetchApi(path, options = {}) {
        const url = `${this.apiBaseUrl}${path}`;
        const loadingOverlay = document.getElementById('loading-overlay');
        if (loadingOverlay) {
            loadingOverlay.style.display = 'flex';
        }
        try {
            const response = await fetch(url, options);
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({ message: 'An unknown error occurred' }));
                this.showError('API Error', `Failed to fetch ${path}: ${errorData.message}`);
                throw new Error(`API request failed: ${response.statusText}`);
            }
            return response.json();
        } catch (error) {
            this.showError('Network Error', `Failed to connect to the API at ${url}. Please ensure the backend is running and accessible.`);
            throw error;
        } finally {
            if (loadingOverlay) {
                loadingOverlay.style.display = 'none';
            }
        }
    }
    
    // ==========================================================================
    // THEME MANAGEMENT
    // ==========================================================================
    
    setupTheme() {
        document.documentElement.setAttribute('data-theme', this.theme);
        this.updateThemeIcon();
    }
    
    toggleTheme() {
        this.theme = this.theme === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', this.theme);
        localStorage.setItem('grugthink-theme', this.theme);
        this.updateThemeIcon();
        this.showToast('Theme updated', 'Theme switched to ' + this.theme + ' mode');
    }
    
    updateThemeIcon() {
        const themeToggle = document.getElementById('theme-toggle');
        if (!themeToggle) {
            console.warn('Theme toggle button not found');
            return;
        }

        const icon = themeToggle.querySelector('i');
        if (!icon) {
            console.warn('Theme toggle icon not found');
            return;
        }

        if (this.theme === 'dark') {
            icon.setAttribute('data-lucide', 'sun');
        } else {
            icon.setAttribute('data-lucide', 'moon');
        }

        // Re-initialize the icon
        lucide.createIcons();
    }
    
    // ==========================================================================
    // NAVIGATION & ROUTING
    // ==========================================================================
    
    setupNavigation() {
        // Set up page routing
        const navItems = document.querySelectorAll('.nav-item');
        navItems.forEach(item => {
            item.addEventListener('click', (e) => {
                e.preventDefault();
                const page = item.getAttribute('data-page');
                this.navigateToPage(page);
            });
        });
        
        // Handle browser back/forward
        window.addEventListener('popstate', (e) => {
            const page = e.state?.page || 'dashboard';
            this.navigateToPage(page, false);
        });
        
        // Initial page load
        const hash = window.location.hash.slice(1) || 'dashboard';
        this.navigateToPage(hash, false);
    }
    
    navigateToPage(page, pushState = true) {
        // Hide all pages
        document.querySelectorAll('.page-content').forEach(p => {
            p.classList.add('hidden');
        });

        // Show target page
        const targetPage = document.getElementById(`page-${page}`);
        if (targetPage) {
            targetPage.classList.remove('hidden');
            targetPage.classList.add('animate-fade-in');
        } else {
            // If page doesn't exist, fall back to dashboard and show warning
            console.warn(`Page '${page}' not found, falling back to dashboard`);
            const dashboardPage = document.getElementById('page-dashboard');
            if (dashboardPage) {
                dashboardPage.classList.remove('hidden');
                dashboardPage.classList.add('animate-fade-in');
            }
            page = 'dashboard';  // Update page variable for correct state tracking
        }

        // Update navigation state
        document.querySelectorAll('.nav-item').forEach(item => {
            item.classList.remove('active');
        });

        const activeNavItem = document.querySelector(`[data-page="${page}"]`);
        if (activeNavItem) {
            activeNavItem.classList.add('active');
        }

        // Update browser history
        if (pushState) {
            history.pushState({ page }, '', `#${page}`);
        }

        this.currentPage = page;

        // Page-specific initialization
        this.initializePage(page);
    }
    
    // ==========================================================================
    // SIDEBAR & MOBILE NAVIGATION
    // ==========================================================================
    
    toggleSidebar() {
        const sidebar = document.getElementById('sidebar');
        const overlay = document.getElementById('sidebar-overlay');
        const mobileBtn = document.getElementById('mobile-menu-btn');
        const hamburger = mobileBtn?.querySelector('.mobile-nav-button');
        
        this.sidebarOpen = !this.sidebarOpen;
        
        if (this.sidebarOpen) {
            sidebar.classList.remove('-translate-x-full');
            sidebar.classList.add('open');
            overlay.classList.remove('hidden');
            hamburger?.classList.add('open');
            
            // Add bounce animation to sidebar
            sidebar.style.animation = 'slideInRight 0.3s ease-out';
        } else {
            sidebar.classList.add('-translate-x-full');
            sidebar.classList.remove('open');
            overlay.classList.add('hidden');
            hamburger?.classList.remove('open');
            
            // Clear animation
            sidebar.style.animation = '';
        }
    }
    
    // ==========================================================================
    // DATA LOADING & API INTEGRATION
    // ==========================================================================
    
    async loadVersionInfo() {
        try {
            const data = await this.fetchApi('/api/version');
            document.getElementById('version-number').textContent = data.version;
            document.getElementById('build-hash').textContent = data.build;
        } catch (error) {
            console.warn('Could not load version info:', error);
            document.getElementById('build-hash').textContent = 'offline';
        }
    }

    async loadDashboardData() {
        try {
            // Load bot statistics
            const bots = await this.fetchApi('/api/bots');
            this.updateDashboardStats(bots);
            this.renderActiveBots(bots);
        } catch (error) {
            console.warn('Could not load dashboard data:', error);
        }
    }
    
    updateDashboardStats(bots) {
        const activeBots = bots.filter(bot => bot.status === 'running').length;
        const totalMessages = bots.reduce((sum, bot) => sum + (bot.message_count || 0), 0);
        
        // Update stats with real data
        document.getElementById('stat-active-bots').textContent = activeBots;
        document.getElementById('stat-messages').textContent = this.formatNumber(totalMessages);
        
        // Show realistic uptime based on bot status
        const uptime = activeBots > 0 ? 
            Math.min(99.9, 85 + (activeBots / bots.length) * 14.9) : 
            0;
        document.getElementById('stat-uptime').textContent = uptime.toFixed(1) + '%';
        
        // Update sidebar badge
        const sidebarBadge = document.querySelector('a[data-page="bots"] .badge');
        if (sidebarBadge) sidebarBadge.textContent = bots.length;
        
        // Update trend indicators based on real data
        this.updateTrendIndicators(activeBots, totalMessages, uptime);
    }
    
    updateTrendIndicators(activeBots, totalMessages, uptime) {
        // More realistic trend indicators
        const botTrend = document.querySelector('#stat-active-bots').parentElement.querySelector('.text-xs');
        const messageTrend = document.querySelector('#stat-messages').parentElement.querySelector('.text-xs');
        const uptimeTrend = document.querySelector('#stat-uptime').parentElement.querySelector('.text-xs');
        
        if (botTrend) {
            botTrend.textContent = activeBots > 0 ? 
                `${activeBots} active` : 
                'No bots running';
        }
        
        if (messageTrend) {
            messageTrend.textContent = totalMessages > 0 ? 
                'Total messages' : 
                'No activity yet';
        }
        
        if (uptimeTrend) {
            uptimeTrend.textContent = uptime > 0 ? 
                'System healthy' : 
                'System idle';
        }
    }
    
    renderActiveBots(bots) {
        const container = document.getElementById('active-bots-grid');
        if (!container) return;
        
        if (!bots || bots.length === 0) {
            container.innerHTML = `
                <div class="col-span-full text-center py-12">
                    <div class="w-16 h-16 bg-muted/20 rounded-full flex items-center justify-center mx-auto mb-4">
                        <i data-lucide="bot" class="w-8 h-8 text-muted"></i>
                    </div>
                    <h3 class="font-medium text-primary mb-2">No bots configured</h3>
                    <p class="text-muted text-sm">Get started by creating your first bot instance</p>
                    <button class="btn btn-primary mt-4">
                        <i data-lucide="plus"></i>
                        Create Bot
                    </button>
                </div>
            `;
            this.initializeLucideIcons();
            return;
        }
        
        container.innerHTML = bots.map(bot => this.renderBotCard(bot)).join('');
        this.initializeLucideIcons();
    }
    
    renderBotCard(bot) {
        const statusColor = bot.status === 'running' ? 'success' : 'danger';
        const statusIcon = bot.status === 'running' ? 'circle-check' : 'circle-x';
        const lastActivity = this.timeAgo(bot.last_activity || new Date());
        
        return `
            <div class="card hover:scale-105 transition-transform">
                <div class="card-body">
                    <div class="flex items-start justify-between mb-3">
                        <div class="flex items-center gap-3">
                            <div class="w-10 h-10 bg-accent-primary/20 rounded-lg flex items-center justify-center">
                                <span class="text-lg">${this.getBotEmoji(bot.personality)}</span>
                            </div>
                            <div>
                                <h3 class="font-medium text-primary">${this.escapeHtml(bot.name)}</h3>
                                <p class="text-xs text-muted">${this.escapeHtml(bot.personality || 'Default')}</p>
                            </div>
                        </div>
                        <span class="badge badge-${statusColor}">
                            <i data-lucide="${statusIcon}" class="w-3 h-3"></i>
                            ${bot.status}
                        </span>
                    </div>
                    
                    <div class="space-y-2 text-sm">
                        <div class="flex justify-between">
                            <span class="text-muted">Servers:</span>
                            <span class="text-primary">${bot.guild_count || 0}</span>
                        </div>
                        <div class="flex justify-between">
                            <span class="text-muted">Messages:</span>
                            <span class="text-primary">${this.formatNumber(bot.message_count || 0)}</span>
                        </div>
                        <div class="flex justify-between">
                            <span class="text-muted">Last active:</span>
                            <span class="text-primary">${lastActivity}</span>
                        </div>
                    </div>
                    
                    <div class="flex gap-2 mt-4">
                        <button class="btn btn-ghost flex-1 text-xs" onclick="app.viewBotLogs('${bot.bot_id}')">
                            <i data-lucide="file-text" class="w-3 h-3"></i>
                            Logs
                        </button>
                        <button class="btn btn-ghost flex-1 text-xs" onclick="app.manageBotSettings('${bot.bot_id}')">
                            <i data-lucide="settings" class="w-3 h-3"></i>
                            Config
                        </button>
                    </div>
                </div>
            </div>
        `;
    }
    
    // ==========================================================================
    // CHARTS & VISUALIZATIONS
    // ==========================================================================
    
    initializeCharts() {
        this.createActivityChart();
    }
    
    createActivityChart(activityData = null) {
        const ctx = document.getElementById('activity-chart');
        if (!ctx) return;

        // Generate time labels for the last 24 hours
        const hours = Array.from({ length: 24 }, (_, i) => {
            const hour = new Date();
            hour.setHours(hour.getHours() - (23 - i));
            return hour.toLocaleTimeString('en-US', { hour: 'numeric' });
        });

        // Use real data if provided, otherwise show zeros (not fake random data!)
        const data = activityData || Array.from({ length: 24 }, () => 0);
        
        this.activityChart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: hours,
                datasets: [{
                    label: 'Messages',
                    data: data,
                    borderColor: 'rgba(99, 102, 241, 1)',
                    backgroundColor: 'rgba(99, 102, 241, 0.1)',
                    borderWidth: 2,
                    fill: true,
                    tension: 0.4,
                    pointRadius: 0,
                    pointHoverRadius: 6,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        display: false
                    }
                },
                scales: {
                    x: {
                        border: {
                            display: false
                        },
                        grid: {
                            color: 'rgba(255, 255, 255, 0.1)'
                        },
                        ticks: {
                            color: 'rgba(255, 255, 255, 0.6)'
                        }
                    },
                    y: {
                        border: {
                            display: false
                        },
                        grid: {
                            color: 'rgba(255, 255, 255, 0.1)'
                        },
                        ticks: {
                            color: 'rgba(255, 255, 255, 0.6)'
                        }
                    }
                },
                interaction: {
                    intersect: false,
                    mode: 'index'
                }
            }
        });
    }
    
    // ==========================================================================
    // EVENT LISTENERS
    // ==========================================================================
    
    setupEventListeners() {
        // Global search
        const searchInput = document.getElementById('global-search');
        if (searchInput) {
            searchInput.addEventListener('input', (e) => {
                this.handleGlobalSearch(e.target.value);
            });
        }
        
        // Bot instances search and filtering
        const botSearchInput = document.getElementById('bot-search');
        if (botSearchInput) {
            botSearchInput.addEventListener('input', (e) => {
                this.filterBotInstances();
            });
        }
        
        // Status and personality filters
        document.addEventListener('change', (e) => {
            if (e.target.closest('#page-bots') && e.target.tagName === 'SELECT') {
                this.filterBotInstances();
            }
        });
        
        // Responsive sidebar
        window.addEventListener('resize', () => {
            if (window.innerWidth >= 1024 && this.sidebarOpen) {
                this.toggleSidebar();
            }
        });

        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => {
            this.handleKeyboardShortcuts(e);
        });

        // Create Bot / Add Bot buttons - navigate to templates page
        document.addEventListener('click', (e) => {
            const createBotBtn = e.target.closest('button');
            if (createBotBtn) {
                const buttonText = createBotBtn.textContent.trim();
                if (buttonText.includes('Create Bot') || buttonText.includes('Add Bot')) {
                    e.preventDefault();
                    e.stopPropagation();
                    console.log('Create/Add Bot button clicked, navigating to templates');
                    this.navigateToPage('templates');
                    this.showToast('Select Template', 'Choose a template to create your bot');
                }
            }
        });
    }
    
    handleGlobalSearch(query) {
        // Implement global search functionality
        console.log('Search query:', query);
        // This will be expanded in Phase 2
    }
    
    handleKeyboardShortcuts(e) {
        // Add keyboard shortcuts for power users
        if (e.metaKey || e.ctrlKey) {
            switch (e.key) {
                case 'k':
                    e.preventDefault();
                    document.getElementById('global-search').focus();
                    break;
                case '1':
                    e.preventDefault();
                    this.navigateToPage('dashboard');
                    break;
                case '2':
                    e.preventDefault();
                    this.navigateToPage('bots');
                    break;
                // Add more shortcuts
            }
        }
    }
    
    // ==========================================================================
    // UTILITY FUNCTIONS
    // ==========================================================================
    
    formatNumber(num) {
        if (num >= 1000000) {
            return (num / 1000000).toFixed(1) + 'M';
        }
        if (num >= 1000) {
            return (num / 1000).toFixed(1) + 'k';
        }
        return num.toString();
    }
    
    timeAgo(date) {
        const now = new Date();
        const diff = now - new Date(date);
        const minutes = Math.floor(diff / 60000);
        
        if (minutes < 1) return 'Just now';
        if (minutes < 60) return `${minutes}m ago`;
        
        const hours = Math.floor(minutes / 60);
        if (hours < 24) return `${hours}h ago`;
        
        const days = Math.floor(hours / 24);
        return `${days}d ago`;
    }
    
    getBotEmoji(personality) {
        const emojis = {
            'grug': '🦕',
            'big_rob': '🇬🇧',
            'grumpy_cat': '😾',
            'wise_owl': '🦉',
            'adaptive': '🔄'
        };
        return emojis[personality] || '🤖';
    }
    
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
    
    animatePageLoad() {
        // Add staggered animation to dashboard elements
        const elements = document.querySelectorAll('.card');
        elements.forEach((el, index) => {
            el.style.opacity = '0';
            el.style.transform = 'translateY(20px)';
            
            setTimeout(() => {
                el.style.transition = 'all 0.5s ease-out';
                el.style.opacity = '1';
                el.style.transform = 'translateY(0)';
            }, index * 100);
        });
    }
    
    initializeLucideIcons() {
        // Initialize Lucide icons
        if (typeof lucide !== 'undefined') {
            lucide.createIcons();
        }
    }
    
    // ==========================================================================
    // TOAST NOTIFICATIONS
    // ==========================================================================
    
    showToast(title, message, type = 'info', duration = 5000) {
        const toastContainer = document.getElementById('toast-container');
        const toast = document.createElement('div');
        const toastId = 'toast-' + Date.now();
        
        const typeClasses = {
            info: 'bg-info-light border-info text-info',
            success: 'bg-success-light border-success text-success',
            warning: 'bg-warning-light border-warning text-warning',
            danger: 'bg-danger-light border-danger text-danger'
        };
        
        toast.id = toastId;
        toast.className = `p-4 rounded-lg border ${typeClasses[type]} animate-slide-in-up shadow-lg`;
        toast.innerHTML = `
            <div class="flex items-start gap-3">
                <i data-lucide="${this.getToastIcon(type)}" class="w-5 h-5 mt-0.5 animate-bounce"></i>
                <div class="flex-1">
                    <h4 class="font-medium text-sm">${this.escapeHtml(title)}</h4>
                    <p class="text-xs opacity-80">${this.escapeHtml(message)}</p>
                </div>
                <button onclick="app.removeToast('${toastId}')" class="opacity-60 hover:opacity-100 transition-opacity">
                    <i data-lucide="x" class="w-4 h-4"></i>
                </button>
            </div>
            <div class="toast-progress bg-current opacity-20 h-1 mt-3 rounded-full overflow-hidden">
                <div class="toast-progress-bar bg-current h-full transition-all duration-${duration}" style="width: 100%;"></div>
            </div>
        `;
        
        toastContainer.appendChild(toast);
        this.initializeLucideIcons();
        
        // Start progress bar animation
        setTimeout(() => {
            const progressBar = toast.querySelector('.toast-progress-bar');
            if (progressBar) {
                progressBar.style.width = '0%';
            }
        }, 100);
        
        // Auto-remove after duration
        setTimeout(() => {
            this.removeToast(toastId);
        }, duration);
        
        // Add hover to pause auto-removal
        let autoRemoveTimer;
        toast.addEventListener('mouseenter', () => {
            const progressBar = toast.querySelector('.toast-progress-bar');
            if (progressBar) {
                progressBar.style.animationPlayState = 'paused';
            }
        });
        
        toast.addEventListener('mouseleave', () => {
            const progressBar = toast.querySelector('.toast-progress-bar');
            if (progressBar) {
                progressBar.style.animationPlayState = 'running';
            }
        });
    }
    
    removeToast(toastId) {
        const toast = document.getElementById(toastId);
        if (toast) {
            toast.style.animation = 'slideInUp 0.3s ease-in reverse';
            setTimeout(() => {
                if (toast.parentNode) {
                    toast.remove();
                }
            }, 300);
        }
    }
    
    // Enhanced notification methods
    showSuccess(title, message, duration = 4000) {
        this.showToast(title, message, 'success', duration);
    }
    
    showError(title, message, duration = 7000) {
        this.showToast(title, message, 'danger', duration);
    }
    
    showWarning(title, message, duration = 6000) {
        this.showToast(title, message, 'warning', duration);
    }
    
    showInfo(title, message, duration = 5000) {
        this.showToast(title, message, 'info', duration);
    }

    flashSavedFields(fieldIds) {
        // Flash fields green to show they were saved
        fieldIds.forEach(fieldId => {
            const field = document.getElementById(fieldId);
            if (field) {
                // Add success state
                field.style.transition = 'all 0.3s ease';
                field.style.borderColor = '#28a745';
                field.style.backgroundColor = '#d4edda';
                field.style.boxShadow = '0 0 0 0.2rem rgba(40, 167, 69, 0.25)';

                // Add checkmark icon after the field
                const parent = field.parentElement;
                const checkmark = document.createElement('span');
                checkmark.className = 'saved-checkmark';
                checkmark.innerHTML = '✓ Saved';
                checkmark.style.cssText = `
                    color: #28a745;
                    font-weight: bold;
                    margin-left: 10px;
                    animation: fadeInOut 3s ease;
                    position: absolute;
                    margin-top: 8px;
                `;
                parent.style.position = 'relative';
                parent.appendChild(checkmark);

                // Reset after animation
                setTimeout(() => {
                    field.style.borderColor = '';
                    field.style.backgroundColor = '';
                    field.style.boxShadow = '';
                    if (checkmark && checkmark.parentElement) {
                        checkmark.remove();
                    }
                }, 3000);
            }
        });
    }

    getToastIcon(type) {
        const icons = {
            info: 'info',
            success: 'check-circle',
            warning: 'alert-triangle',
            danger: 'alert-circle'
        };
        return icons[type] || 'info';
    }

    // Modal functions
    showModal(content) {
        // Create modal overlay if it doesn't exist
        let modalOverlay = document.getElementById('modal-overlay');
        if (!modalOverlay) {
            modalOverlay = document.createElement('div');
            modalOverlay.id = 'modal-overlay';
            modalOverlay.className = 'fixed inset-0 bg-black bg-opacity-50 z-50 flex items-center justify-center p-4';
            modalOverlay.onclick = (e) => {
                if (e.target === modalOverlay) {
                    this.closeModal();
                }
            };
            document.body.appendChild(modalOverlay);
        }

        modalOverlay.innerHTML = content;
        modalOverlay.style.display = 'flex';

        // Re-initialize lucide icons in the modal
        if (window.lucide) {
            window.lucide.createIcons();
        }
    }

    closeModal() {
        const modalOverlay = document.getElementById('modal-overlay');
        if (modalOverlay) {
            modalOverlay.style.display = 'none';
            modalOverlay.innerHTML = '';
        }
    }
    
    // ==========================================================================
    // ACTIONS & INTERACTIONS
    // ==========================================================================
    
    async refreshDashboard() {
        this.showToast('Refreshing', 'Updating dashboard data...');
        await this.loadDashboardData();
        if (this.activityChart) {
            // Update chart with new data
            this.activityChart.destroy();
            this.createActivityChart();
        }
    }
    
    async viewBotLogs(botId) {
        console.log('View logs for bot:', botId);

        try {
            // Fetch logs from API
            const data = await this.fetchApi(`/api/bots/${botId}/logs`);
            const logs = data.logs || [];

            // Create logs modal content
            const logsHtml = logs.map(log => {
                const levelClass = {
                    'error': 'text-red-500',
                    'warning': 'text-yellow-500',
                    'info': 'text-blue-500',
                    'debug': 'text-gray-500'
                }[log.level] || 'text-gray-400';

                const timestamp = new Date(log.timestamp).toLocaleString();

                return `
                    <div class="border-b border-gray-700 pb-2 mb-2">
                        <div class="flex items-center gap-2">
                            <span class="text-xs text-gray-500">${timestamp}</span>
                            <span class="text-xs ${levelClass} font-bold uppercase">${this.escapeHtml(log.level)}</span>
                        </div>
                        <div class="text-sm text-gray-300 mt-1">${this.escapeHtml(log.message)}</div>
                        ${log.logger ? `<div class="text-xs text-gray-600 mt-1">${this.escapeHtml(log.logger)}</div>` : ''}
                    </div>
                `;
            }).join('');

            const modalContent = `
                <div class="bg-surface p-6 rounded-lg max-w-4xl w-full max-h-[80vh] overflow-hidden flex flex-col">
                    <div class="flex justify-between items-center mb-4">
                        <h3 class="text-xl font-bold">Bot Logs</h3>
                        <button onclick="app.closeModal()" class="btn btn-ghost">
                            <i data-lucide="x"></i>
                        </button>
                    </div>
                    <div class="overflow-y-auto flex-1 bg-gray-900 p-4 rounded font-mono text-sm">
                        ${logs.length > 0 ? logsHtml : '<p class="text-gray-500">No logs available</p>'}
                    </div>
                </div>
            `;

            this.showModal(modalContent);
        } catch (error) {
            console.error('Failed to load bot logs:', error);
            this.showError('Error', 'Failed to load bot logs. Please try again.');
        }
    }
    
    manageBotSettings(botId) {
        console.log('Manage settings for bot:', botId);
        this.showToast('Coming Soon', 'Bot settings will be available in Phase 2');
    }
    
    // ==========================================================================
    // BOT INSTANCES PAGE
    // ==========================================================================
    
    async loadBotInstances() {
        console.log('Loading bot instances...');
        try {
            const bots = await this.fetchApi('/api/bots');
            this.renderBotInstances(bots);
        } catch (error) {
            console.warn('Could not load bot instances:', error);
            this.showEmptyBotInstances();
        }
    }
    
    renderBotInstances(bots) {
        const container = document.getElementById('bot-instances-grid');
        if (!container) return;
        
        if (!bots || bots.length === 0) {
            this.showEmptyBotInstances();
            return;
        }
        
        // Store original bot data for filtering
        this.allBots = bots;
        
        container.innerHTML = bots.map(bot => this.renderBotInstanceCard(bot)).join('');
        this.initializeLucideIcons();
        
        // Add staggered animation
        this.animateElementsIn(container.children);
    }
    
    filterBotInstances() {
        if (!this.allBots) return;
        
        const searchQuery = document.getElementById('bot-search')?.value.toLowerCase() || '';
        const statusFilter = document.querySelector('#page-bots select:first-of-type')?.value || 'all';
        const personalityFilter = document.querySelector('#page-bots select:last-of-type')?.value || 'all';
        
        let filteredBots = this.allBots.filter(bot => {
            const matchesSearch = !searchQuery || 
                bot.name.toLowerCase().includes(searchQuery) ||
                bot.bot_id.toLowerCase().includes(searchQuery) ||
                (bot.personality && bot.personality.toLowerCase().includes(searchQuery));
            
            const matchesStatus = statusFilter === 'all' || bot.status === statusFilter;
            const matchesPersonality = personalityFilter === 'all' || bot.personality === personalityFilter;
            
            return matchesSearch && matchesStatus && matchesPersonality;
        });
        
        const container = document.getElementById('bot-instances-grid');
        if (!container) return;
        
        if (filteredBots.length === 0) {
            container.innerHTML = `
                <div class="col-span-full">
                    <div class="card animate-fade-in">
                        <div class="card-body text-center py-12">
                            <div class="w-16 h-16 bg-muted/20 rounded-full flex items-center justify-center mx-auto mb-4">
                                <i data-lucide="search-x" class="w-8 h-8 text-muted"></i>
                            </div>
                            <h3 class="font-medium text-primary mb-2">No bots match your filters</h3>
                            <p class="text-muted text-sm">Try adjusting your search or filter criteria</p>
                        </div>
                    </div>
                </div>
            `;
        } else {
            container.innerHTML = filteredBots.map(bot => this.renderBotInstanceCard(bot)).join('');
            this.animateElementsIn(container.children);
        }
        
        this.initializeLucideIcons();
    }
    
    animateElementsIn(elements) {
        Array.from(elements).forEach((el, index) => {
            el.style.opacity = '0';
            el.style.transform = 'translateY(20px)';
            
            setTimeout(() => {
                el.style.transition = 'all 0.4s ease-out';
                el.style.opacity = '1';
                el.style.transform = 'translateY(0)';
            }, index * 100);
        });
    }
    
    renderBotInstanceCard(bot) {
        const statusColor = this.getBotStatusColor(bot.status);
        const statusIcon = this.getBotStatusIcon(bot.status);
        const lastActivity = this.timeAgo(bot.last_activity || new Date());
        
        return `
            <div class="card hover:scale-105 transition-transform">
                <div class="card-header">
                    <div class="flex items-start justify-between">
                        <div class="flex items-center gap-3">
                            <div class="w-12 h-12 bg-accent-primary/20 rounded-xl flex items-center justify-center">
                                <span class="text-xl">${this.getBotEmoji(bot.personality)}</span>
                            </div>
                            <div>
                                <h3 class="font-semibold text-primary">${this.escapeHtml(bot.name)}</h3>
                                <p class="text-sm text-muted">${this.escapeHtml(bot.personality || 'Default')}</p>
                                <p class="text-xs text-muted">ID: ${this.escapeHtml(bot.bot_id)}</p>
                            </div>
                        </div>
                        <span class="badge badge-${statusColor}">
                            <i data-lucide="${statusIcon}" class="w-3 h-3"></i>
                            ${bot.status}
                        </span>
                    </div>
                </div>
                
                <div class="card-body">
                    <div class="grid grid-cols-2 gap-4 text-sm mb-4">
                        <div>
                            <span class="text-muted">Servers:</span>
                            <span class="text-primary font-medium">${bot.guild_count || 0}</span>
                        </div>
                        <div>
                            <span class="text-muted">Messages:</span>
                            <span class="text-primary font-medium">${this.formatNumber(bot.message_count || 0)}</span>
                        </div>
                        <div>
                            <span class="text-muted">Uptime:</span>
                            <span class="text-primary font-medium">${bot.uptime || '0m'}</span>
                        </div>
                        <div>
                            <span class="text-muted">Last Active:</span>
                            <span class="text-primary font-medium">${lastActivity}</span>
                        </div>
                    </div>
                    
                    <div class="flex gap-2">
                        <button class="btn btn-secondary flex-1 text-sm" onclick="app.toggleBot('${bot.bot_id}', '${bot.status}')">
                            <i data-lucide="${bot.status === 'running' ? 'stop-circle' : 'play-circle'}" class="w-4 h-4"></i>
                            ${bot.status === 'running' ? 'Stop' : 'Start'}
                        </button>
                        <button class="btn btn-ghost text-sm" onclick="app.viewBotLogs('${bot.bot_id}')">
                            <i data-lucide="file-text" class="w-4 h-4"></i>
                            Logs
                        </button>
                        <button class="btn btn-ghost text-sm" onclick="app.configureBotInstance('${bot.bot_id}')">
                            <i data-lucide="settings" class="w-4 h-4"></i>
                            Config
                        </button>
                    </div>
                </div>
            </div>
        `;
    }
    
    showEmptyBotInstances() {
        const container = document.getElementById('bot-instances-grid');
        if (!container) return;
        
        container.innerHTML = `
            <div class="col-span-full">
                <div class="card">
                    <div class="card-body text-center py-12">
                        <div class="w-16 h-16 bg-muted/20 rounded-full flex items-center justify-center mx-auto mb-4">
                            <i data-lucide="bot" class="w-8 h-8 text-muted"></i>
                        </div>
                        <h3 class="font-medium text-primary mb-2">No bot instances found</h3>
                        <p class="text-muted text-sm mb-4">Create your first bot instance to get started</p>
                        <button class="btn btn-primary" onclick="app.createNewBot()">
                            <i data-lucide="plus"></i>
                            Create Bot Instance
                        </button>
                    </div>
                </div>
            </div>
        `;
        this.initializeLucideIcons();
    }
    
    getBotStatusColor(status) {
        const colors = {
            running: 'success',
            stopped: 'warning',
            error: 'danger',
            starting: 'info',
            stopping: 'warning'
        };
        return colors[status] || 'warning';
    }
    
    getBotStatusIcon(status) {
        const icons = {
            running: 'circle-check',
            stopped: 'circle-pause',
            error: 'circle-x',
            starting: 'loader',
            stopping: 'loader'
        };
        return icons[status] || 'circle-pause';
    }
    
    // Bot management actions
    async toggleBot(botId, currentStatus) {
        const action = currentStatus === 'running' ? 'stop' : 'start';
        console.log(`${action} bot:`, botId);

        try {
            await this.fetchApi(`/api/bots/${botId}/${action}`, {
                method: 'POST'
            });
            this.showToast('Success', `Bot ${action} request sent`, 'success');
            // Refresh the bot instances after a delay
            setTimeout(() => this.loadBotInstances(), 2000);
        } catch (error) {
            this.showToast('Error', `Failed to ${action} bot: ${error.message}`, 'danger');
        }
    }
    
    configureBotInstance(botId) {
        console.log('Configure bot instance:', botId);
        this.showToast('Coming Soon', 'Bot configuration panel will be available soon', 'info');
    }
    
    createNewBot() {
        console.log('Create new bot instance');
        this.navigateToPage('templates');
        this.showToast('Select Template', 'Choose a template to create your bot', 'info');
    }
    
    // ==========================================================================
    // BOT TEMPLATES PAGE
    // ==========================================================================
    
    async loadBotTemplates() {
        console.log('Loading bot templates...');
        try {
            const data = await this.fetchApi('/api/templates');
            // Convert dictionary to array with IDs
            const templates = Array.isArray(data) ? data :
                Object.entries(data || {}).map(([id, template]) => ({
                    id,
                    ...template,
                    // Set defaults for missing fields
                    emoji: '🤖',
                    usage_count: 0,
                    personality: template.force_personality || 'adaptive',
                    features: [
                        template.load_embedder ? 'Semantic Search' : null,
                        template.force_personality ? `${template.force_personality} personality` : 'Multi-personality',
                        'Discord Integration'
                    ].filter(Boolean),
                    default_gemini_key: true,
                    default_google_search: false
                }));
            this.renderBotTemplates(templates);
        } catch (error) {
            console.warn('Could not load bot templates:', error);
            this.renderBotTemplates([]);
        }
    }
    

    
    renderBotTemplates(templates) {
        const container = document.getElementById('templates-grid');
        if (!container) return;
        
        if (!templates || templates.length === 0) {
            this.showEmptyTemplates();
            return;
        }
        
        container.innerHTML = templates.map(template => this.renderTemplateCard(template)).join('');
        this.initializeLucideIcons();
        this.animateElementsIn(container.children);
    }
    
    renderTemplateCard(template) {
        return `
            <div class="card hover:scale-105 transition-transform">
                <div class="card-header">
                    <div class="flex items-center gap-3 mb-3">
                        <div class="w-12 h-12 bg-accent-primary/20 rounded-xl flex items-center justify-center">
                            <span class="text-xl">${template.emoji}</span>
                        </div>
                        <div class="flex-1">
                            <h3 class="font-semibold text-primary">${this.escapeHtml(template.name)}</h3>
                            <p class="text-xs text-muted">${template.usage_count} instances created</p>
                        </div>
                        <span class="badge badge-primary">${this.escapeHtml(template.personality)}</span>
                    </div>
                    <p class="text-sm text-secondary">${this.escapeHtml(template.description)}</p>
                </div>
                
                <div class="card-body">
                    <div class="mb-4">
                        <h4 class="text-sm font-medium text-primary mb-2">Features:</h4>
                        <div class="flex flex-wrap gap-1">
                            ${template.features.map(feature => `
                                <span class="badge badge-secondary text-xs">${this.escapeHtml(feature)}</span>
                            `).join('')}
                        </div>
                    </div>
                    
                    <div class="grid grid-cols-2 gap-2 text-xs mb-4">
                        <div class="flex items-center gap-1">
                            <i data-lucide="${template.default_gemini_key ? 'check' : 'x'}" class="w-3 h-3 ${template.default_gemini_key ? 'text-success' : 'text-muted'}"></i>
                            <span class="text-muted">Gemini AI</span>
                        </div>
                        <div class="flex items-center gap-1">
                            <i data-lucide="${template.default_google_search ? 'check' : 'x'}" class="w-3 h-3 ${template.default_google_search ? 'text-success' : 'text-muted'}"></i>
                            <span class="text-muted">Web Search</span>
                        </div>
                        <div class="flex items-center gap-1">
                            <i data-lucide="${template.load_embedder ? 'check' : 'x'}" class="w-3 h-3 ${template.load_embedder ? 'text-success' : 'text-muted'}"></i>
                            <span class="text-muted">ML Features</span>
                        </div>
                    </div>
                    
                    <div class="flex gap-2">
                        <button class="btn btn-primary flex-1 text-sm" onclick="app.createBotFromTemplate('${template.id}')">
                            <i data-lucide="plus-circle" class="w-4 h-4"></i>
                            Create Bot
                        </button>
                        <button class="btn btn-ghost text-sm" onclick="app.editTemplate('${template.id}')">
                            <i data-lucide="edit" class="w-4 h-4"></i>
                            Edit
                        </button>
                    </div>
                </div>
            </div>
        `;
    }
    
    showEmptyTemplates() {
        const container = document.getElementById('templates-grid');
        if (!container) return;
        
        container.innerHTML = `
            <div class="col-span-full">
                <div class="card">
                    <div class="card-body text-center py-12">
                        <div class="w-16 h-16 bg-muted/20 rounded-full flex items-center justify-center mx-auto mb-4">
                            <i data-lucide="user-circle" class="w-8 h-8 text-muted"></i>
                        </div>
                        <h3 class="font-medium text-primary mb-2">No templates found</h3>
                        <p class="text-muted text-sm mb-4">Create your first bot template to get started</p>
                        <button class="btn btn-primary" onclick="app.createCustomTemplate()">
                            <i data-lucide="plus"></i>
                            Create Template
                        </button>
                    </div>
                </div>
            </div>
        `;
        this.initializeLucideIcons();
    }
    
    // Template actions
    async createBotFromTemplate(templateId) {
        console.log('Creating bot from template:', templateId);
        try {
            // First, fetch available Discord tokens
            const tokens = await this.fetchApi('/api/discord-tokens');

            if (!tokens || tokens.length === 0) {
                this.showError('No Discord Tokens', 'Please add a Discord bot token first before creating a bot instance. Go to the "Discord Tokens" page to add one.');
                // Navigate to tokens page
                this.navigateToPage('tokens');
                return;
            }

            // Use the first available active token
            const activeToken = tokens.find(t => t.active) || tokens[0];

            const result = await this.fetchApi('/api/bots', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    template_id: templateId,
                    name: `New Bot from ${templateId}`,
                    discord_token_id: activeToken.id,
                    auto_start: false
                })
            });

            // Extract bot name from result (check multiple possible fields)
            const botName = result.name || result.bot_name || `Bot from ${templateId}`;
            this.showSuccess('Bot Created', `Successfully created bot instance: ${botName}`);
            // Navigate to bot instances to see the new bot
            this.navigateToPage('bots');
        } catch (error) {
            this.showError('Creation Failed', `Failed to create bot: ${error.message}`);
        }
    }
    
    editTemplate(templateId) {
        console.log('Edit template:', templateId);
        this.showInfo('Coming Soon', 'Template editor will be available in the next update');
    }
    
    createCustomTemplate() {
        console.log('Create custom template');
        this.showInfo('Coming Soon', 'Custom template creator will be available in the next update');
    }
    
    // ==========================================================================
    // SETTINGS PAGE
    // ==========================================================================
    
    initializePage(page) {
        switch (page) {
            case 'dashboard':
                this.refreshDashboard();
                break;
            case 'bots':
                this.loadBotInstances();
                break;
            case 'templates':
                this.loadBotTemplates();
                break;
            case 'tokens':
                this.loadTokens();
                break;
            case 'analytics':
                this.loadAnalytics();
                break;
            case 'logs':
                this.loadLogs();
                break;
            case 'settings':
                this.loadSettings();
                break;
        }
    }
    
    async loadSettings() {
        console.log('Loading settings...');

        // Add Sentry breadcrumb
        if (window.addBreadcrumb) {
            window.addBreadcrumb('Loading settings', { action: 'load_settings' });
        }

        try {
            const settings = await this.fetchApi('/api/settings');
            this.populateSettings(settings);

            // Add success breadcrumb
            if (window.addBreadcrumb) {
                window.addBreadcrumb('Settings loaded successfully', {
                    llm_provider: settings.llm_provider,
                    default_model: settings.default_model,
                });
            }
        } catch (error) {
            console.warn('Could not load settings from API:', error);

            // Capture error in Sentry
            if (window.captureError) {
                window.captureError(error, {
                    action: 'load_settings',
                    endpoint: '/api/settings',
                });
            }

            this.loadDefaultSettings();
        }
    }

    async handleLLMProviderChange() {
        const provider = document.querySelector('input[name="llm_provider"]:checked')?.value;
        const geminiConfig = document.getElementById('gemini-config');
        const ollamaConfig = document.getElementById('ollama-config');

        if (provider === 'gemini') {
            geminiConfig?.classList.remove('hidden');
            ollamaConfig?.classList.add('hidden');
            this.populateGeminiModels();
        } else if (provider === 'ollama') {
            geminiConfig?.classList.add('hidden');
            ollamaConfig?.classList.remove('hidden');
            await this.populateOllamaModels();
        }
    }

    populateGeminiModels() {
        const modelSelect = document.getElementById('setting-default-model');
        if (!modelSelect) return;

        modelSelect.innerHTML = `
            <option value="gemini-1.5-flash">Gemini 1.5 Flash</option>
            <option value="gemini-1.5-pro">Gemini 1.5 Pro</option>
            <option value="gemma-3-27b-it">Gemma 3 27B IT</option>
        `;
    }

    async updateOllamaServerStatus(urls) {
        const statusContainer = document.getElementById('ollama-servers-status');
        if (!statusContainer) return;

        const servers = urls.split(',').map(url => url.trim()).filter(url => url);
        if (servers.length === 0) {
            statusContainer.innerHTML = '';
            return;
        }

        statusContainer.innerHTML = servers.map((url, index) => `
            <div class="p-3 border border-border rounded-lg flex items-center justify-between">
                <div class="flex items-center gap-3 flex-1">
                    <div id="server-status-${index}" class="w-3 h-3 rounded-full bg-gray-400 animate-pulse"></div>
                    <div class="flex-1">
                        <div class="text-sm font-medium text-primary">${index === 0 ? 'Primary' : 'Backup #' + index}</div>
                        <div class="text-xs text-muted font-mono">${url}</div>
                    </div>
                </div>
                <button
                    onclick="app.testOllamaServer('${url}', ${index})"
                    class="px-3 py-1 text-xs font-medium text-primary border border-border rounded-md hover:bg-card-hover transition-colors"
                >
                    Test
                </button>
            </div>
        `).join('');

        // Auto-test all servers
        servers.forEach((url, index) => this.testOllamaServer(url, index));
    }

    async testOllamaServer(url, index) {
        const statusDot = document.getElementById(`server-status-${index}`);
        if (!statusDot) return;

        // Set testing state
        statusDot.className = 'w-3 h-3 rounded-full bg-yellow-400 animate-pulse';

        try {
            const response = await fetch(`/api/ollama/models?url=${encodeURIComponent(url)}`);
            if (response.ok) {
                // Success - green
                statusDot.className = 'w-3 h-3 rounded-full bg-green-500';
            } else {
                // Error - red
                statusDot.className = 'w-3 h-3 rounded-full bg-red-500';
            }
        } catch (error) {
            // Connection failed - red
            statusDot.className = 'w-3 h-3 rounded-full bg-red-500';
        }
    }

    async populateOllamaModels() {
        const modelSelect = document.getElementById('setting-default-model');
        if (!modelSelect) return;

        try {
            // Get Ollama server URL from the input field
            const ollamaUrlInput = document.getElementById('setting-ollama-urls');
            const ollamaUrl = ollamaUrlInput?.value || 'http://localhost:11434';

            // Query via backend API to avoid CORS
            const response = await fetch(`/api/ollama/models?url=${encodeURIComponent(ollamaUrl.split(',')[0])}`);
            if (!response.ok) throw new Error('Failed to fetch Ollama models');

            const data = await response.json();
            const models = data.models || [];

            if (models.length === 0) {
                modelSelect.innerHTML = '<option value="">No models available</option>';
                return;
            }

            // Populate dropdown with Ollama models
            modelSelect.innerHTML = models.map(m =>
                `<option value="${this.escapeHtml(m)}">${this.escapeHtml(m)}</option>`
            ).join('');

        } catch (error) {
            console.warn('Could not fetch Ollama models:', error);
            modelSelect.innerHTML = '<option value="llama3.2:latest">llama3.2:latest (default)</option>';
        }
    }
    
    loadDefaultSettings() {
        // Set default values - Gemma 3 27B IT as default model
        const defaults = {
            environment: 'dev',
            log_level: 'INFO',
            oauth_enabled: false,
            websocket_enabled: true,
            auto_start: true,
            load_embedder: true,
            default_model: 'gemma-3-27b-it'
        };
        this.populateSettings(defaults);
    }
    
    async populateSettings(settings) {
        // Populate Gemini settings FIRST (before provider change triggers model population)
        if (settings.gemini) {
            if (settings.gemini.api_key_set) {
                const geminiInput = document.getElementById('setting-gemini-key');
                if (geminiInput) geminiInput.value = '***REDACTED***';
            }
            if (settings.gemini.model) {
                const geminiModel = document.getElementById('setting-gemini-model');
                if (geminiModel) geminiModel.value = settings.gemini.model;
            }
        }

        // Populate Ollama settings FIRST (before provider change triggers model population)
        if (settings.ollama) {
            if (settings.ollama.urls) {
                const ollamaUrls = document.getElementById('setting-ollama-urls');
                if (ollamaUrls) {
                    ollamaUrls.value = settings.ollama.urls;
                    this.updateOllamaServerStatus(settings.ollama.urls);
                }
            }
            if (settings.ollama.models) {
                const ollamaModels = document.getElementById('setting-ollama-models');
                if (ollamaModels) ollamaModels.value = settings.ollama.models;
            }
        }

        // NOW populate LLM provider selection (after URLs are set)
        if (settings.llm_provider) {
            const providerRadio = document.getElementById(`provider-${settings.llm_provider}`);
            if (providerRadio) {
                providerRadio.checked = true;
                await this.handleLLMProviderChange();
            }
        }

        // Populate Google Search settings
        if (settings.google_search) {
            if (settings.google_search.api_key_set) {
                const googleKey = document.getElementById('setting-google-key');
                if (googleKey) googleKey.value = '***REDACTED***';
            }
            if (settings.google_search.cse_id_set) {
                const googleCse = document.getElementById('setting-google-cse');
                if (googleCse) googleCse.value = '***REDACTED***';
            }
        }

        // Populate select fields
        if (settings.environment) {
            const envSelect = document.getElementById('setting-environment');
            if (envSelect) envSelect.value = settings.environment;
        }

        if (settings.log_level) {
            const logSelect = document.getElementById('setting-log-level');
            if (logSelect) logSelect.value = settings.log_level;
        }

        // Set default model dropdown value AFTER models are populated
        if (settings.default_model) {
            const modelSelect = document.getElementById('setting-default-model');
            if (modelSelect) modelSelect.value = settings.default_model;
        }

        // Populate toggle switches
        this.setToggleState('setting-oauth', settings.oauth_enabled);
        this.setToggleState('setting-websocket', settings.websocket_enabled);
        this.setToggleState('setting-auto-start', settings.auto_start);
        this.setToggleState('setting-load-embedder', settings.load_embedder);
    }
    
    setToggleState(toggleId, isActive) {
        const toggle = document.getElementById(toggleId);
        if (toggle) {
            if (isActive) {
                toggle.classList.add('active');
            } else {
                toggle.classList.remove('active');
            }
        }
    }
    
    toggleSetting(settingName) {
        const toggleElement = document.getElementById(`setting-${settingName}`);
        if (toggleElement) {
            toggleElement.classList.toggle('active');
            const isActive = toggleElement.classList.contains('active');
            
            // Show feedback
            this.showInfo('Setting Updated', `${settingName} ${isActive ? 'enabled' : 'disabled'}`, 2000);
        }
    }
    
    togglePasswordVisibility(inputId) {
        const input = document.getElementById(inputId);
        const button = input?.nextElementSibling?.querySelector('i');
        
        if (input && button) {
            if (input.type === 'password') {
                input.type = 'text';
                button.setAttribute('data-lucide', 'eye-off');
            } else {
                input.type = 'password';
                button.setAttribute('data-lucide', 'eye');
            }
            this.initializeLucideIcons();
        }
    }
    
    async saveSettings() {
        console.log('Saving settings...');

        // Add Sentry breadcrumb
        if (window.addBreadcrumb) {
            window.addBreadcrumb('Saving settings', { action: 'save_settings' });
        }

        try {
            // Get LLM provider selection
            const llmProvider = document.querySelector('input[name="llm_provider"]:checked')?.value;

            if (!llmProvider) {
                this.showError('Validation Error', 'Please select an LLM provider (Gemini or Ollama)');

                // Add breadcrumb for validation error
                if (window.addBreadcrumb) {
                    window.addBreadcrumb('Settings validation failed', {
                        reason: 'No LLM provider selected',
                    });
                }

                return;
            }

            // Build settings object
            const settings = {
                llm_provider: llmProvider,
                gemini: {},
                ollama: {},
                google_search: {}
            };

            // Gemini settings
            const geminiKey = document.getElementById('setting-gemini-key')?.value;
            if (geminiKey && geminiKey !== '***REDACTED***') {
                settings.gemini.api_key = geminiKey;
            }
            const geminiModel = document.getElementById('setting-gemini-model')?.value;
            if (geminiModel) {
                settings.gemini.model = geminiModel;
            }

            // Ollama settings
            const ollamaUrls = document.getElementById('setting-ollama-urls')?.value;
            if (ollamaUrls) {
                settings.ollama.urls = ollamaUrls;
            }
            const ollamaModels = document.getElementById('setting-ollama-models')?.value;
            if (ollamaModels) {
                settings.ollama.models = ollamaModels;
            }

            // Google Search settings
            const googleKey = document.getElementById('setting-google-key')?.value;
            if (googleKey && googleKey !== '***REDACTED***') {
                settings.google_search.api_key = googleKey;
            }
            const googleCse = document.getElementById('setting-google-cse')?.value;
            if (googleCse && googleCse !== '***REDACTED***') {
                settings.google_search.cse_id = googleCse;
            }

            // Default model selection
            const defaultModel = document.getElementById('setting-default-model')?.value;
            if (defaultModel) {
                settings.default_model = defaultModel;
            }

            // Save to API
            const result = await this.fetchApi('/api/settings', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(settings)
            });

            // Build detailed success message with what changed
            let successDetails = [];
            if (llmProvider) {
                successDetails.push(`Provider: ${llmProvider.toUpperCase()}`);
            }
            if (defaultModel) {
                successDetails.push(`Default Model: ${defaultModel}`);
            }

            const detailedMessage = successDetails.length > 0
                ? successDetails.join(' | ')
                : 'Settings updated successfully';

            this.showSuccess('✓ Settings Saved', detailedMessage, 6000);

            // Visual feedback: Flash the saved fields green
            this.flashSavedFields([
                'setting-default-model',
                llmProvider === 'gemini' ? 'setting-gemini-key' : 'setting-ollama-urls'
            ]);

            // Add success breadcrumb
            if (window.addBreadcrumb) {
                window.addBreadcrumb('Settings saved successfully', {
                    llm_provider: llmProvider,
                    default_model: defaultModel,
                });
            }

            // Reload settings to reflect saved values
            await this.loadSettings();

        } catch (error) {
            console.error('Settings save failed:', error);

            // Capture error in Sentry
            if (window.captureError) {
                window.captureError(error, {
                    action: 'save_settings',
                    endpoint: '/api/settings',
                    method: 'PUT',
                });
            }

            this.showError('Save Failed', 'Could not save settings: ' + error.message);
        }
    }
    
    exportSettings() {
        const settings = {
            environment: document.getElementById('setting-environment')?.value,
            log_level: document.getElementById('setting-log-level')?.value,
            default_model: document.getElementById('setting-default-model')?.value,
            oauth_enabled: document.getElementById('setting-oauth')?.classList.contains('active'),
            websocket_enabled: document.getElementById('setting-websocket')?.classList.contains('active'),
            auto_start: document.getElementById('setting-auto-start')?.classList.contains('active'),
            load_embedder: document.getElementById('setting-load-embedder')?.classList.contains('active')
        };
        
        const blob = new Blob([JSON.stringify(settings, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'grugthink-settings.json';
        a.click();
        URL.revokeObjectURL(url);
        
        this.showSuccess('Settings Exported', 'Configuration file downloaded');
    }
    
    importSettings() {
        const input = document.createElement('input');
        input.type = 'file';
        input.accept = '.json';
        input.onchange = (e) => {
            const file = e.target.files[0];
            if (file) {
                const reader = new FileReader();
                reader.onload = (e) => {
                    try {
                        const settings = JSON.parse(e.target.result);
                        this.populateSettings(settings);
                        this.showSuccess('Settings Imported', 'Configuration loaded successfully');
                    } catch (error) {
                        this.showError('Import Failed', 'Invalid configuration file');
                    }
                };
                reader.readAsText(file);
            }
        };
        input.click();
    }
    
    resetSettings() {
        if (confirm('Are you sure you want to reset all settings to defaults?')) {
            this.loadDefaultSettings();
            this.showWarning('Settings Reset', 'All settings have been reset to defaults');
        }
    }
    
    // ==========================================================================
    // DISCORD TOKENS PAGE
    // ==========================================================================
    
    async loadTokens() {
        console.log('Loading Discord tokens...');

        // Try to load from localStorage first
        try {
            const savedTokens = localStorage.getItem('grugthink-tokens');
            if (savedTokens) {
                const tokens = JSON.parse(savedTokens);
                this.renderTokens(tokens);
                return;
            }
        } catch (error) {
            console.warn('Could not load tokens from localStorage:', error);
        }

        // Fallback to API
        try {
            const tokens = await this.fetchApi('/api/discord-tokens');
            this.renderTokens(tokens);
        } catch (error) {
            console.warn('Could not load tokens from API:', error);
            this.renderEmptyTokens();
        }
    }
    
    renderTokens(tokens) {
        const container = document.getElementById('active-tokens-container');
        if (!container) return;
        
        if (!tokens || tokens.length === 0) {
            this.renderEmptyTokens();
            return;
        }
        
        container.innerHTML = tokens.map(token => this.renderTokenCard(token)).join('');
        this.initializeLucideIcons();
    }
    
    renderTokenCard(token) {
        const maskedToken = `${token.value.substring(0, 8)}...${token.value.substring(token.value.length - 8)}`;
        return `
            <div class="flex items-center justify-between p-4 border border-border rounded-lg mb-3">
                <div class="flex items-center gap-3">
                    <div class="w-10 h-10 bg-accent-primary/20 rounded-lg flex items-center justify-center">
                        <i data-lucide="bot" class="w-5 h-5 text-accent-primary"></i>
                    </div>
                    <div>
                        <h3 class="font-medium text-primary">${this.escapeHtml(token.name)}</h3>
                        <p class="text-sm text-muted font-mono">${maskedToken}</p>
                    </div>
                </div>
                <div class="flex items-center gap-2">
                    <span class="badge ${token.status === 'active' ? 'badge-success' : 'badge-danger'}">
                        ${token.status}
                    </span>
                    <button class="btn btn-ghost btn-sm" onclick="app.editToken('${token.id}')">
                        <i data-lucide="edit" class="w-4 h-4"></i>
                    </button>
                    <button class="btn btn-ghost btn-sm text-danger" onclick="app.deleteToken('${token.id}')">
                        <i data-lucide="trash-2" class="w-4 h-4"></i>
                    </button>
                </div>
            </div>
        `;
    }
    
    renderEmptyTokens() {
        const container = document.getElementById('active-tokens-container');
        if (!container) return;
        
        container.innerHTML = `
            <div class="text-center py-8">
                <div class="w-16 h-16 bg-muted/20 rounded-full flex items-center justify-center mx-auto mb-4">
                    <i data-lucide="key" class="w-8 h-8 text-muted"></i>
                </div>
                <h3 class="font-medium text-primary mb-2">No tokens configured</h3>
                <p class="text-muted text-sm">Add your first Discord bot token to get started</p>
            </div>
        `;
        this.initializeLucideIcons();
    }
    
    addNewToken() {
        document.getElementById('new-token-name')?.focus();
    }
    
    async saveNewToken() {
        const name = document.getElementById('new-token-name')?.value;
        const value = document.getElementById('new-token-value')?.value;
        const personality = document.getElementById('new-token-personality')?.value;
        const autostart = document.getElementById('new-token-autostart')?.classList.contains('active');

        if (!name || !value) {
            this.showError('Validation Error', 'Please enter both bot name and token');
            return;
        }

        // Create new token object
        const newToken = {
            id: Date.now().toString(),
            name,
            value,
            personality,
            autostart,
            status: 'inactive',
            created: new Date().toISOString()
        };

        try {
            // Load existing tokens from localStorage
            const existingTokens = JSON.parse(localStorage.getItem('grugthink-tokens') || '[]');

            // Add new token
            existingTokens.push(newToken);

            // Save back to localStorage
            localStorage.setItem('grugthink-tokens', JSON.stringify(existingTokens));

            this.showSuccess('Token Added', `Successfully added token for ${name} (saved locally)`);
            this.clearTokenForm();
            this.loadTokens();

            // Also try to save to API if available
            this.fetchApi('/api/discord-tokens', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: newToken.name,
                    token: newToken.value  // Backend expects 'token' field, not 'value'
                })
            }).catch(() => {
                console.log('API not available, token saved locally only');
            });

        } catch (error) {
            this.showError('Save Failed', `Failed to save token: ${error.message}`);
        }
    }
    
    clearTokenForm() {
        document.getElementById('new-token-name').value = '';
        document.getElementById('new-token-value').value = '';
        document.getElementById('new-token-personality').value = 'grug';
    }
    
    editToken(tokenId) {
        this.showInfo('Coming Soon', 'Token editing will be available in the next update');
    }
    
    deleteToken(tokenId) {
        this.showInfo('Coming Soon', 'Token deletion will be available in the next update');
    }
    
    exportTokens() {
        this.showInfo('Coming Soon', 'Token export will be available in the next update');
    }
    
    // ==========================================================================
    // ANALYTICS PAGE
    // ==========================================================================
    
    async loadAnalytics() {
        console.log('Loading analytics...');
        try {
            const analytics = await this.fetchApi('/api/analytics');
            this.updateAnalytics(analytics);
        } catch (error) {
            console.warn('Could not load analytics:', error);
            this.updateAnalytics(null);
        }
    }
    
    updateAnalytics(data) {
        if (!data) {
            document.getElementById('analytics-total-messages').textContent = '0';
            document.getElementById('analytics-active-hours').textContent = '0';
            document.getElementById('analytics-response-rate').textContent = '0%';
            document.getElementById('analytics-peak-users').textContent = '0';
            return;
        }
        
        document.getElementById('analytics-total-messages').textContent = this.formatNumber(data.total_messages || 0);
        document.getElementById('analytics-active-hours').textContent = this.formatNumber(data.active_hours || 0);
        document.getElementById('analytics-response-rate').textContent = (data.response_rate || 0) + '%';
        document.getElementById('analytics-peak-users').textContent = this.formatNumber(data.peak_users || 0);
    }
    
    // ==========================================================================
    // SYSTEM LOGS PAGE
    // ==========================================================================
    
    async loadLogs() {
        console.log('Loading system logs...');
        try {
            const logs = await this.fetchApi('/api/logs');
            this.displayLogs(logs);
        } catch (error) {
            console.warn('Could not load logs:', error);
            this.displayEmptyLogs();
        }
    }
    
    displayLogs(logs) {
        const container = document.getElementById('log-container');
        if (!container) return;
        
        if (!logs || logs.length === 0) {
            this.displayEmptyLogs();
            return;
        }
        
        const logHtml = logs.map(log => this.formatLogEntry(log)).join('\n');
        container.innerHTML = `<div>${logHtml}</div>`;
        
        if (document.getElementById('auto-scroll-logs')?.classList.contains('active')) {
            container.scrollTop = container.scrollHeight;
        }
    }
    
    formatLogEntry(log) {
        const timestamp = new Date(log.timestamp).toLocaleTimeString();
        const levelColors = {
            DEBUG: 'text-gray-400',
            INFO: 'text-blue-400', 
            WARNING: 'text-yellow-400',
            ERROR: 'text-red-400'
        };
        const color = levelColors[log.level] || 'text-white';
        
        return `<p class="${color}">[${timestamp}] [${log.level}] ${this.escapeHtml(log.message)}</p>`;
    }
    
    displayEmptyLogs() {
        const container = document.getElementById('log-container');
        if (!container) return;
        
        container.innerHTML = `
            <div class="text-muted">
                <p>[INFO] System logs will appear here in real-time</p>
                <p>[INFO] No log entries yet - start a bot to see activity</p>
            </div>
        `;
    }
    
    refreshLogs() {
        this.loadLogs();
        this.showInfo('Logs Refreshed', 'Log data has been updated', 2000);
    }
    
    clearLogs() {
        const container = document.getElementById('log-container');
        if (container) {
            container.innerHTML = '<div class="text-muted"><p>[INFO] Logs cleared</p></div>';
        }
        this.showInfo('Logs Cleared', 'Log display has been cleared', 2000);
    }
    
    exportLogs() {
        this.showInfo('Coming Soon', 'Log export will be available in the next update');
    }
}

// ==========================================================================
// GLOBAL FUNCTIONS & INITIALIZATION
// ==========================================================================

// Global functions that can be called from HTML
function toggleSidebar() {
    if (window.app) {
        window.app.toggleSidebar();
    }
}

async function sendGrugMessage() {
    if (window.app) {
        await window.app.sendGrugMessage();
    }
}

// Re-add methods to class before closing (moved to proper location above)
GrugThinkApp.prototype.sendGrugMessage = async function() {
        const input = document.getElementById('grug-chat-input');
        const messagesContainer = document.getElementById('grug-chat-messages');

        if (!input || !messagesContainer) return;

        const message = input.value.trim();
        if (!message) return;

        // Add user message
        this.appendGrugMessage('user', message);
        input.value = '';

        // Show typing indicator
        const typingId = this.appendGrugTyping();

        try {
            // Call Gemini API with Grug personality. Supply your own key via
            // window.GEMINI_API_KEY (injected at serve time) - never hardcode a
            // key in source, especially in a public repo.
            const geminiKey = window.GEMINI_API_KEY || '';
            const response = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key=${geminiKey}`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    contents: [{
                        parts: [{
                            text: `You are Grug, a simple caveman developer. You speak in broken English like "grug think...", "grug no like...", "grug say...". You give simple, direct advice. You avoid complexity and prefer simple solutions. Respond to this message: ${message}`
                        }]
                    }]
                })
            });

            const data = await response.json();

            // Validate response structure before accessing nested properties
            if (!data || !data.candidates || data.candidates.length === 0 ||
                !data.candidates[0].content || !data.candidates[0].content.parts ||
                data.candidates[0].content.parts.length === 0 ||
                !data.candidates[0].content.parts[0].text) {
                throw new Error('Invalid response structure from Gemini API');
            }

            const reply = data.candidates[0].content.parts[0].text;

            // Remove typing indicator
            this.removeGrugTyping(typingId);

            // Add Grug's response
            this.appendGrugMessage('grug', reply);

        } catch (error) {
            console.error('Grug chat error:', error);
            this.removeGrugTyping(typingId);
            this.appendGrugMessage('grug', 'grug have problem... system no work good. grug sorry.');
        }
};

GrugThinkApp.prototype.appendGrugMessage = function(sender, text) {
        const messagesContainer = document.getElementById('grug-chat-messages');
        if (!messagesContainer) return;

        const messageDiv = document.createElement('div');
        messageDiv.style.marginBottom = '1rem';
        messageDiv.style.display = 'flex';
        messageDiv.style.flexDirection = sender === 'user' ? 'row-reverse' : 'row';
        messageDiv.style.gap = '0.5rem';

        const avatar = document.createElement('div');
        avatar.style.width = '32px';
        avatar.style.height = '32px';
        avatar.style.borderRadius = '50%';
        avatar.style.backgroundColor = sender === 'user' ? 'var(--accent-primary)' : 'var(--success)';
        avatar.style.display = 'flex';
        avatar.style.alignItems = 'center';
        avatar.style.justifyContent = 'center';
        avatar.style.flexShrink = '0';
        avatar.textContent = sender === 'user' ? '👤' : '🗿';

        const bubble = document.createElement('div');
        bubble.style.padding = '0.75rem 1rem';
        bubble.style.borderRadius = '12px';
        bubble.style.backgroundColor = sender === 'user' ? 'var(--accent-primary)' : 'var(--bg-tertiary)';
        bubble.style.color = sender === 'user' ? 'white' : 'var(--text-primary)';
        bubble.style.maxWidth = '70%';
        bubble.style.wordWrap = 'break-word';
        bubble.textContent = text;

        messageDiv.appendChild(avatar);
        messageDiv.appendChild(bubble);
        messagesContainer.appendChild(messageDiv);

        // Scroll to bottom
        messagesContainer.scrollTop = messagesContainer.scrollHeight;
};

GrugThinkApp.prototype.appendGrugTyping = function() {
        const messagesContainer = document.getElementById('grug-chat-messages');
        if (!messagesContainer) return null;

        const typingDiv = document.createElement('div');
        typingDiv.id = 'grug-typing-' + Date.now();
        typingDiv.style.marginBottom = '1rem';
        typingDiv.style.display = 'flex';
        typingDiv.style.gap = '0.5rem';
        typingDiv.className = 'grug-typing-indicator';

        const avatar = document.createElement('div');
        avatar.style.width = '32px';
        avatar.style.height = '32px';
        avatar.style.borderRadius = '50%';
        avatar.style.backgroundColor = 'var(--success)';
        avatar.style.display = 'flex';
        avatar.style.alignItems = 'center';
        avatar.style.justifyContent = 'center';
        avatar.textContent = '🗿';

        const bubble = document.createElement('div');
        bubble.style.padding = '0.75rem 1rem';
        bubble.style.borderRadius = '12px';
        bubble.style.backgroundColor = 'var(--bg-tertiary)';
        bubble.textContent = 'grug thinking...';
        bubble.style.fontStyle = 'italic';
        bubble.style.color = 'var(--text-muted)';

        typingDiv.appendChild(avatar);
        typingDiv.appendChild(bubble);
        messagesContainer.appendChild(typingDiv);
        messagesContainer.scrollTop = messagesContainer.scrollHeight;

        return typingDiv.id;
};

GrugThinkApp.prototype.removeGrugTyping = function(typingId) {
        if (!typingId) return;
        const typingDiv = document.getElementById(typingId);
        if (typingDiv) typingDiv.remove();
};

function toggleTheme() {
    if (window.app) {
        window.app.toggleTheme();
    }
}

// Initialize app when DOM is ready
document.addEventListener('DOMContentLoaded', function() {
    window.app = new GrugThinkApp();
});

// Development helper
if (window.location.hostname === 'localhost') {
    console.log('🚀 GrugThink running in development mode');
    window.grugthink = {
        app: () => window.app,
        version: '3.3.1',
        debug: true
    };
}