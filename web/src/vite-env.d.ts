/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  // Datadog RUM build-time credentials (spec 0013). Sourced from SSM
  // (/grug/dd-rum-{application-id,client-token}) by web.deploy.yml and
  // exported as env vars before `npm run build`. All four optional —
  // local `npm run dev` runs without them and RUM stays dark.
  readonly VITE_DD_RUM_APPLICATION_ID?: string;
  readonly VITE_DD_RUM_CLIENT_TOKEN?: string;
  readonly VITE_DD_RUM_ENV?: string;
  readonly VITE_DD_RUM_VERSION?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
