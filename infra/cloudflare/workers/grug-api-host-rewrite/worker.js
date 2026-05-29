// Cloudflare Worker that proxies api.grug.lol → grug-api Lambda Function URL.
// Mirror of grug-webhook-host-rewrite — same Host-rewrite logic;
// upstream baked at deploy time by infra/cloudflare/deploy.sh from the
// Pulumi `api_function_url` output.
//
// Per memory `reference_lambda_function_url_host_volatile`: Function URL
// host changes on every recreate; deploy.sh re-templates `__UPSTREAM_HOST__`
// each time so the Worker stays in sync.
//
// `X-Grug-CF-Secret` (parent issue #173) is the CF→AWS auth-boundary
// tightening: the value is sourced from the `GRUG_CF_SECRET` Worker
// secret binding, which `deploy.sh` PUTs from SSM `/grug/cf-shared-secret`
// after every script upload. Lambda middleware (sibling slice #233)
// validates the header on every non-`/livez` request. The api Lambda
// has un-authenticated endpoints (`/livez`, `/api/v1/auth/github/callback`)
// where this header is the only second-layer auth.

const ORIGIN = "__UPSTREAM_HOST__";
const SECRET_HEADER = "X-Grug-CF-Secret";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    url.hostname = ORIGIN;
    url.protocol = "https:";
    url.port = "";

    const headers = new Headers(request.headers);
    headers.set("Host", ORIGIN);

    // Inject the shared secret. `set` (not `append`) so a client-supplied
    // header is overwritten — clients cannot smuggle a forged value past
    // the Lambda middleware.
    if (env && env.GRUG_CF_SECRET) {
      headers.set(SECRET_HEADER, env.GRUG_CF_SECRET);
    } else {
      // No binding deployed yet — strip any client-supplied value so
      // downstream sees the "unconfigured" path cleanly. Middleware in
      // sibling slice #233 fail-opens when the SSM secret is empty, so
      // strip-only here is safe during the rollout window.
      headers.delete(SECRET_HEADER);
    }

    const init = {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
      redirect: "manual",
    };

    return fetch(url.toString(), init);
  },
};
