/**
 * Sentry Frontend SDK Initialization
 *
 * Initializes Sentry error tracking and performance monitoring for the GrugThink frontend.
 * Automatically captures JavaScript errors, unhandled promise rejections, and performance metrics.
 */

// Only initialize if Sentry DSN is configured. Set your own DSN here (or inject
// it at build/serve time) - left blank so no project is hardcoded in source.
const SENTRY_DSN = window.SENTRY_DSN || "";
const SENTRY_ENABLED = Boolean(SENTRY_DSN); // enabled only when a DSN is set

if (SENTRY_ENABLED && SENTRY_DSN) {
    // Load Sentry SDK from CDN
    (function() {
        const script = document.createElement('script');
        script.src = 'https://browser.sentry-cdn.com/8.47.0/bundle.tracing.min.js';
        script.crossOrigin = 'anonymous';
        // Remove integrity check to avoid CORS/CDN issues
        script.async = true;

        script.onload = function() {
            if (typeof Sentry !== 'undefined') {
                // Initialize Sentry
                Sentry.init({
                    dsn: SENTRY_DSN,

                    // Set environment based on URL
                    environment: window.location.hostname.includes('dev') ? 'development' : 'production',

                    // Release version (sync with backend VERSION file)
                    release: 'grugthink@3.3.1',

                    // Performance monitoring
                    integrations: [
                        new Sentry.BrowserTracing({
                            // Track page loads and navigation
                            tracePropagationTargets: ['localhost', /^\//],

                            // Track fetch/XHR requests
                            traceFetch: true,
                            traceXHR: true,
                        }),
                        new Sentry.Replay({
                            // Session replay for debugging
                            maskAllText: true,
                            blockAllMedia: true,
                        }),
                    ],

                    // Performance monitoring sample rate
                    tracesSampleRate: 0.1, // 10% of transactions

                    // Session replay sample rate
                    replaysSessionSampleRate: 0.1, // 10% of sessions
                    replaysOnErrorSampleRate: 1.0, // 100% of sessions with errors

                    // Custom error filtering
                    beforeSend(event, hint) {
                        // Add custom context
                        event.tags = event.tags || {};
                        event.tags.component = 'frontend';
                        event.tags.page = window.location.pathname;

                        // Add user agent info
                        event.contexts = event.contexts || {};
                        event.contexts.browser = {
                            name: navigator.userAgent,
                            version: navigator.appVersion,
                        };

                        // Filter out known non-critical errors
                        if (hint && hint.originalException) {
                            const error = hint.originalException;

                            // Ignore network errors from browser extensions
                            if (error.message && error.message.includes('Extension')) {
                                return null;
                            }

                            // Ignore AbortError from cancelled requests
                            if (error.name === 'AbortError') {
                                return null;
                            }
                        }

                        return event;
                    },

                    // Ignore specific errors
                    ignoreErrors: [
                        // Browser extension errors
                        'top.GLOBALS',
                        'chrome-extension://',
                        'moz-extension://',
                        // Network errors that are user-caused
                        'NetworkError',
                        'Failed to fetch',
                        // ResizeObserver errors (harmless)
                        'ResizeObserver loop limit exceeded',
                    ],
                });

                console.log('[Sentry] Frontend error tracking initialized');

                // Set up global error handler for unhandled errors
                window.addEventListener('error', function(event) {
                    console.error('[Sentry] Captured error:', event.error);
                });

                // Set up handler for unhandled promise rejections
                window.addEventListener('unhandledrejection', function(event) {
                    console.error('[Sentry] Captured unhandled rejection:', event.reason);
                });
            }
        };

        script.onerror = function() {
            console.warn('[Sentry] Failed to load Sentry SDK from CDN');
        };

        document.head.appendChild(script);
    })();
} else {
    console.log('[Sentry] Error tracking disabled');
}

/**
 * Helper function to manually capture errors
 * Usage: captureError(new Error('Something went wrong'), { extra: 'context' });
 */
window.captureError = function(error, context = {}) {
    if (typeof Sentry !== 'undefined') {
        Sentry.captureException(error, {
            extra: context,
            tags: {
                manual: true,
                component: 'frontend',
            },
        });
    } else {
        console.error('Error:', error, 'Context:', context);
    }
};

/**
 * Helper function to set user context for error tracking
 * Usage: setUserContext({ id: 'user123', username: 'john' });
 */
window.setUserContext = function(user) {
    if (typeof Sentry !== 'undefined') {
        Sentry.setUser(user);
        console.log('[Sentry] User context set:', user);
    }
};

/**
 * Helper function to add breadcrumb for debugging
 * Usage: addBreadcrumb('User clicked save button', { buttonId: 'save-btn' });
 */
window.addBreadcrumb = function(message, data = {}) {
    if (typeof Sentry !== 'undefined') {
        Sentry.addBreadcrumb({
            message: message,
            data: data,
            level: 'info',
        });
    }
};
