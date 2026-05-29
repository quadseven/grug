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
// `X-Grug-CF-Secret` is the CF→AWS auth-boundary tightening: the value
// is sourced from the `GRUG_CF_SECRET` Worker secret binding, which
// `deploy.sh` PUTs from SSM `/grug/cf-shared-secret` after every script
// upload. Lambda middleware validates the header on every non-`/livez`
// request. Webhook payloads remain HMAC-protected by GitHub end-to-end;
// this header is additive defense against direct Function-URL access
// bypassing CF entirely.

// Three placeholders are sed-substituted by infra/cloudflare/deploy.sh
// at upload time so deploy.sh is the single source of truth for both
// the binding name and the header name. Lambda middleware must match
// SECRET_HEADER exactly.
const ORIGIN = "__UPSTREAM_HOST__";
const SECRET_HEADER = "__SECRET_HEADER__";
const BINDING_NAME = "__BINDING_NAME__";

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
    const bindingValue = env ? env[BINDING_NAME] : undefined;
    if (typeof bindingValue === "string" && bindingValue.length > 0) {
      headers.set(SECRET_HEADER, bindingValue);
    } else {
      // Strip any client-supplied value when no binding is configured.
      // Middleware fail-opens when the binding is absent, so strip-only
      // is safe.
      //
      // Empty-string is impossible-by-accident (CF API rejects empty
      // `text` on the binding PUT). If we see it here, the binding was
      // tampered with or corrupted — log to Logpush + strip.
      if (bindingValue === "") {
        console.error("GRUG_CF_SECRET binding is an empty string — tampered or corrupted");
      }
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
