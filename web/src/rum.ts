// Datadog RUM SDK initialization for the React SPA (web/app.html entry).
// Spec 0013 (RumInstrumentation) bool:
//   `rum_sdk_loaded_on_react_spa_entry_per_observability_intent`
//
// Imported by web/src/main.tsx BEFORE the React tree mounts so RUM
// captures the initial render + first interaction.
//
// Credentials come from Vite build-time env vars
// (VITE_DD_RUM_APPLICATION_ID + VITE_DD_RUM_CLIENT_TOKEN). The
// web.deploy.yml workflow reads them from SSM
// (/grug/dd-rum-application-id, /grug/dd-rum-client-token) and exports
// them before `npm run build`. The Pulumi datadog.RumApplication
// resource owns the SSM values (spec 0013, component dd_rum.py).
//
// If credentials are missing (e.g. local `npm run dev` without env
// vars), the init is skipped — RUM stays dark instead of throwing.
import { datadogRum } from "@datadog/browser-rum";

const applicationId = import.meta.env.VITE_DD_RUM_APPLICATION_ID;
const clientToken = import.meta.env.VITE_DD_RUM_CLIENT_TOKEN;
const env = import.meta.env.VITE_DD_RUM_ENV ?? "dev";
const version = import.meta.env.VITE_DD_RUM_VERSION ?? "local";

export function initRum(): void {
  if (!applicationId || !clientToken) {
    // Local dev path — leave a breadcrumb in the console but don't throw.
    console.info("[rum] init skipped — missing VITE_DD_RUM_APPLICATION_ID or VITE_DD_RUM_CLIENT_TOKEN");
    return;
  }

  datadogRum.init({
    applicationId,
    clientToken,
    site: "datadoghq.com",
    // Spec 0013 invariant: service tag MUST be `grug-web` (canonical).
    // Wrong/missing service tag splits the DD APM catalog across phantom
    // service identities (same shape as the aws.lambda.url inferred-spans
    // trap closed 2026-05-07 for grug-{api,webhook}).
    service: "grug-web",
    env,
    version,
    sessionSampleRate: 100,
    // 100% session replay per the v1 instrumentation decision. Replay
    // billing is separate from RUM event count — bump down here if cost
    // becomes a concern.
    sessionReplaySampleRate: 100,
    // mask-user-input is the conservative default — usernames, emails,
    // passwords get masked in the replay. App data still recorded.
    defaultPrivacyLevel: "mask-user-input",
    trackUserInteractions: true,
    trackResources: true,
    trackLongTasks: true,
  });
}
