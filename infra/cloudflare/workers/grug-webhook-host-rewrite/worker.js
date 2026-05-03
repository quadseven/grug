// Cloudflare Worker that proxies webhook.grug.lol → Lambda Function URL.
//
// Why a Worker: Lambda Function URLs only respond to requests with the
// `Host` header equal to the lambda-url FQDN. Direct `Host: webhook.grug.lol`
// requests get 403 from AWS's edge gateway. Plain Cloudflare CNAME proxy
// can't rewrite `Host` on the Free plan (Origin Rules → Host Header
// Override is Enterprise-only). A Worker is the only zero-cost path on
// Free that does both Host rewrite + path-passthrough cleanly.
//
// Mirrors somatic-scripts/infra/cloudflare/worker.js (chef-lambda-host-rewrite,
// macchina-router, tempo-lambda-host-rewrite). Difference: grug webhook is
// HMAC-protected by GitHub at the application layer, so we DON'T add a
// shared-secret header (HMAC verifies authenticity end-to-end).
//
// Free-plan-friendly: 100k req/day. With our handful of repos this is
// orders of magnitude headroom.
//
// `__UPSTREAM_HOST__` is templated at deploy time by Pulumi from the
// Lambda Function URL output (per memory
// `reference_lambda_function_url_host_volatile` — host changes on every
// recreate, single-source via Pulumi output).

const ORIGIN = "__UPSTREAM_HOST__";

export default {
  async fetch(request) {
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
