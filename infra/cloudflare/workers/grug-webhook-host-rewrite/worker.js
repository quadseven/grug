// Cloudflare Worker that proxies webhook.grug.lol → Lambda Function URL.
//
// Why a Worker: Lambda Function URLs only respond to requests with the
// `Host` header equal to the lambda-url FQDN. Direct `Host: webhook.grug.lol`
// requests get 403 from AWS's edge gateway. Plain Cloudflare CNAME proxy
// can't rewrite `Host` on the Free plan (Origin Rules → Host Header
// Override is Enterprise-only). A Worker is the only zero-cost path on
// Free that does both Host rewrite + path-passthrough cleanly.
//
// Free-plan-friendly: 100k req/day. With our handful of repos this is
// orders of magnitude headroom.
//
// `__UPSTREAM_HOST__` is templated at deploy time by `infra/cloudflare/deploy.sh`
// from the Pulumi Lambda Function URL output (per memory
// `reference_lambda_function_url_host_volatile` — host changes on every
// recreate, single-source via Pulumi output).
//
// `X-Grug-CF-Secret` (parent issue #173) is the CF→AWS auth-boundary
// tightening: the value is sourced from the `GRUG_CF_SECRET` Worker
// secret binding, which `deploy.sh` PUTs from SSM `/grug/cf-shared-secret`
// after every script upload. Lambda middleware (sibling slice #233)
// validates the header on every non-`/livez` request. Webhook payloads
// remain HMAC-protected by GitHub end-to-end; this header is additive
// defense against direct Function-URL access bypassing CF entirely.

const ORIGIN = "__UPSTREAM_HOST__";
const SECRET_HEADER = "X-Grug-CF-Secret";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    url.hostname = ORIGIN;
    url.protocol = "https:";
    url.port = "";

    const headers = new Headers(request.headers);
    // Lambda Function URL TLS cert is `*.lambda-url.us-east-1.on.aws`,
    // so origin-side requests must SNI as that name. Setting the Host
    // header here also makes the Lambda edge gateway accept the request
    // (it 403s anything with a non-matching Host).
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
      // Don't pass body on GET/HEAD (Workers runtime warning otherwise).
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
      // `manual` so any 3xx from upstream flows back to the client.
      redirect: "manual",
    };

    return fetch(url.toString(), init);
  },
};
