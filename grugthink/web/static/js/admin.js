// GrugThink Admin Panel
// Complete implementation for system settings management

class AdminPanel {
    constructor() {
        this.settings = {};
        this.originalSettings = {};
        this.loadSettings();
    }

    async loadSettings() {
        try {
            const response = await fetch('/api/admin/settings');
            if (response.ok) {
                this.settings = await response.json();
                this.originalSettings = JSON.parse(JSON.stringify(this.settings));
                this.populateForm();
                this.updateStatus();
            } else {
                this.showAlert('Failed to load settings', 'danger');
            }
        } catch (error) {
            console.error('Failed to load settings:', error);
            this.showAlert('Failed to load settings', 'danger');
        }
    }

    populateForm() {
        // Authentication settings
        this.setFieldValue('disable_oauth', this.settings.DISABLE_OAUTH || 'true');
        this.setFieldValue('session_secret', this.settings.SESSION_SECRET || '');
        this.setFieldValue('trusted_user_ids', this.settings.TRUSTED_USER_IDS || '');
        this.setFieldValue('trusted_memory_ids', this.settings.TRUSTED_MEMORY_IDS || '');

        // System settings
        this.setFieldValue('log_level', this.settings.LOG_LEVEL || 'INFO');
        this.setFieldValue('grugbot_variant', this.settings.GRUGBOT_VARIANT || 'dev');
        this.setFieldValue('multibot_api_port', this.settings.MULTIBOT_API_PORT || '8080');
        this.setFieldValue('grugbot_data_dir', this.settings.GRUGBOT_DATA_DIR || '/data');
        this.setFieldValue('load_embedder', this.settings.LOAD_EMBEDDER || 'True');

        // AI/ML settings
        this.setFieldValue('gemini_model', this.settings.GEMINI_MODEL || 'gemma-3-27b-it');
        this.setFieldValue('ollama_base_url', this.settings.OLLAMA_BASE_URL || '');

        // Health monitoring
        this.setFieldValue('health_check_interval', this.settings.HEALTH_CHECK_INTERVAL || '30');
        this.setFieldValue('bot_heartbeat_timeout', this.settings.BOT_HEARTBEAT_TIMEOUT || '300');
        this.setFieldValue('bot_restart_rate_limit', this.settings.BOT_RESTART_RATE_LIMIT || '120');
        this.setFieldValue('bot_max_consecutive_failures', this.settings.BOT_MAX_CONSECUTIVE_FAILURES || '5');
        this.setFieldValue('bot_restart_backoff_max', this.settings.BOT_RESTART_BACKOFF_MAX || '300');
        this.setFieldValue('bot_high_latency_threshold', this.settings.BOT_HIGH_LATENCY_THRESHOLD || '5.0');

        // Features
        this.setCheckboxValue('enable_config_reload', this.settings.ENABLE_CONFIG_RELOAD === 'True');
        this.setCheckboxValue('websocket_enabled', this.settings.WEBSOCKET_ENABLED === 'True');
    }

    setFieldValue(fieldId, value) {
        const field = document.getElementById(fieldId);
        if (field) {
            field.value = value;
        }
    }

    setCheckboxValue(fieldId, checked) {
        const field = document.getElementById(fieldId);
        if (field) {
            field.checked = checked;
        }
    }

    async saveSettings() {
        try {
            // Collect all form values
            const updates = {
                DISABLE_OAUTH: document.getElementById('disable_oauth').value,
                SESSION_SECRET: document.getElementById('session_secret').value,
                TRUSTED_USER_IDS: document.getElementById('trusted_user_ids').value,
                TRUSTED_MEMORY_IDS: document.getElementById('trusted_memory_ids').value,
                LOG_LEVEL: document.getElementById('log_level').value,
                GRUGBOT_VARIANT: document.getElementById('grugbot_variant').value,
                MULTIBOT_API_PORT: document.getElementById('multibot_api_port').value,
                GRUGBOT_DATA_DIR: document.getElementById('grugbot_data_dir').value,
                LOAD_EMBEDDER: document.getElementById('load_embedder').value,
                GEMINI_MODEL: document.getElementById('gemini_model').value,
                OLLAMA_BASE_URL: document.getElementById('ollama_base_url').value,
                HEALTH_CHECK_INTERVAL: document.getElementById('health_check_interval').value,
                BOT_HEARTBEAT_TIMEOUT: document.getElementById('bot_heartbeat_timeout').value,
                BOT_RESTART_RATE_LIMIT: document.getElementById('bot_restart_rate_limit').value,
                BOT_MAX_CONSECUTIVE_FAILURES: document.getElementById('bot_max_consecutive_failures').value,
                BOT_RESTART_BACKOFF_MAX: document.getElementById('bot_restart_backoff_max').value,
                BOT_HIGH_LATENCY_THRESHOLD: document.getElementById('bot_high_latency_threshold').value,
                ENABLE_CONFIG_RELOAD: document.getElementById('enable_config_reload').checked ? 'True' : 'False',
                WEBSOCKET_ENABLED: document.getElementById('websocket_enabled').checked ? 'True' : 'False'
            };

            const response = await fetch('/api/admin/settings', {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(updates)
            });

            if (response.ok) {
                this.showAlert('Settings saved successfully! Changes will take effect after restart.', 'success');
                this.settings = updates;
                this.originalSettings = JSON.parse(JSON.stringify(updates));
                this.updateStatus();
            } else {
                const error = await response.json();
                this.showAlert(`Failed to save settings: ${error.detail}`, 'danger');
            }
        } catch (error) {
            console.error('Failed to save settings:', error);
            this.showAlert('Failed to save settings', 'danger');
        }
    }

    updateStatus() {
        const statusDiv = document.getElementById('status-display');
        const changedSettings = this.getChangedSettings();
        
        let statusHtml = `
            <div class="row">
                <div class="col-md-6">
                    <h6>System Status</h6>
                    <ul class="list-unstyled">
                        <li><i class="bi bi-circle-fill text-success me-2"></i>OAuth: ${this.settings.DISABLE_OAUTH === 'true' ? 'Disabled (Dev Mode)' : 'Enabled (Prod Mode)'}</li>
                        <li><i class="bi bi-circle-fill text-info me-2"></i>Log Level: ${this.settings.LOG_LEVEL || 'INFO'}</li>
                        <li><i class="bi bi-circle-fill text-primary me-2"></i>Environment: ${this.settings.GRUGBOT_VARIANT || 'dev'}</li>
                        <li><i class="bi bi-circle-fill text-warning me-2"></i>ML Features: ${this.settings.LOAD_EMBEDDER === 'True' ? 'Enabled' : 'Disabled'}</li>
                    </ul>
                </div>
                <div class="col-md-6">
                    <h6>Health Monitoring</h6>
                    <ul class="list-unstyled">
                        <li><i class="bi bi-heart-pulse me-2"></i>Check Interval: ${this.settings.HEALTH_CHECK_INTERVAL || '30'}s</li>
                        <li><i class="bi bi-clock me-2"></i>Heartbeat Timeout: ${this.settings.BOT_HEARTBEAT_TIMEOUT || '300'}s</li>
                        <li><i class="bi bi-arrow-clockwise me-2"></i>Max Failures: ${this.settings.BOT_MAX_CONSECUTIVE_FAILURES || '5'}</li>
                        <li><i class="bi bi-speedometer2 me-2"></i>Latency Threshold: ${this.settings.BOT_HIGH_LATENCY_THRESHOLD || '5.0'}s</li>
                    </ul>
                </div>
            </div>
        `;

        if (changedSettings.length > 0) {
            statusHtml += `
                <div class="alert alert-warning mt-3">
                    <h6><i class="bi bi-exclamation-triangle me-2"></i>Unsaved Changes</h6>
                    <p class="mb-0">You have unsaved changes in: ${changedSettings.join(', ')}</p>
                </div>
            `;
        }

        statusDiv.innerHTML = statusHtml;
    }

    getChangedSettings() {
        const changed = [];
        const currentValues = this.getCurrentFormValues();
        
        for (const [key, value] of Object.entries(currentValues)) {
            if (this.originalSettings[key] !== value) {
                changed.push(key.replace(/_/g, ' ').toLowerCase());
            }
        }
        
        return changed;
    }

    getCurrentFormValues() {
        return {
            DISABLE_OAUTH: document.getElementById('disable_oauth').value,
            SESSION_SECRET: document.getElementById('session_secret').value,
            TRUSTED_USER_IDS: document.getElementById('trusted_user_ids').value,
            TRUSTED_MEMORY_IDS: document.getElementById('trusted_memory_ids').value,
            LOG_LEVEL: document.getElementById('log_level').value,
            GRUGBOT_VARIANT: document.getElementById('grugbot_variant').value,
            MULTIBOT_API_PORT: document.getElementById('multibot_api_port').value,
            GRUGBOT_DATA_DIR: document.getElementById('grugbot_data_dir').value,
            LOAD_EMBEDDER: document.getElementById('load_embedder').value,
            GEMINI_MODEL: document.getElementById('gemini_model').value,
            OLLAMA_BASE_URL: document.getElementById('ollama_base_url').value,
            HEALTH_CHECK_INTERVAL: document.getElementById('health_check_interval').value,
            BOT_HEARTBEAT_TIMEOUT: document.getElementById('bot_heartbeat_timeout').value,
            BOT_RESTART_RATE_LIMIT: document.getElementById('bot_restart_rate_limit').value,
            BOT_MAX_CONSECUTIVE_FAILURES: document.getElementById('bot_max_consecutive_failures').value,
            BOT_RESTART_BACKOFF_MAX: document.getElementById('bot_restart_backoff_max').value,
            BOT_HIGH_LATENCY_THRESHOLD: document.getElementById('bot_high_latency_threshold').value,
            ENABLE_CONFIG_RELOAD: document.getElementById('enable_config_reload').checked ? 'True' : 'False',
            WEBSOCKET_ENABLED: document.getElementById('websocket_enabled').checked ? 'True' : 'False'
        };
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

// Initialize admin panel when page loads
let adminPanel;
document.addEventListener('DOMContentLoaded', function() {
    adminPanel = new AdminPanel();
    
    // Add change listeners to update status
    const formElements = document.querySelectorAll('input, select');
    formElements.forEach(element => {
        element.addEventListener('change', () => {
            adminPanel.updateStatus();
        });
    });
});