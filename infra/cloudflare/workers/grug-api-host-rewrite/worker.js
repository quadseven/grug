// Cloudflare Worker that proxies api.grug.lol → grug-api Lambda Function URL.
// Mirror of grug-webhook-host-rewrite — same Host-rewrite logic;
// upstream baked at deploy time by infra/cloudflare/deploy.sh from the
// Pulumi `api_function_url` output.
//
// Per memory `reference_lambda_function_url_host_volatile`: Function URL
// host changes on every recreate; deploy.sh re-templates `__UPSTREAM_HOST__`
// each time so the Worker stays in sync.

const ORIGIN = "__UPSTREAM_HOST__";

export default {
  async fetch(request) {
    const url = new URL(request.url);
    url.hostname = ORIGIN;
    url.protocol = "https:";
    url.port = "";

    const headers = new Headers(request.headers);
    headers.set("Host", ORIGIN);

    const init = {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
      redirect: "manual",
    };

    return fetch(url.toString(), init);
  },
};
