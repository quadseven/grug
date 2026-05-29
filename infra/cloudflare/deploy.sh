#!/usr/bin/env bash
# Deploy the grug-webhook-host-rewrite CF Worker + bind to webhook.grug.lol/*.
#
# Mirrors the macchina-router / chef-lambda-host-rewrite /
# tempo-lambda-host-rewrite pattern from somatic-scripts. Pulumi can't
# manage CF Workers reliably (script-exists, main_module, route binding
# all fail in different ways across pulumi-cloudflare versions).
#
# Idempotent: PUT replaces the script content; route POST is one-shot
# (existing routes return 409 which we swallow).
#
# Reads:
#   - SSM `/grug/cloudflare-api-token`     (Zone:DNS:Edit + Workers
#                                            Scripts:Edit + Workers Routes:Edit
#                                            + Zone:Cache Purge + Account
#                                            Settings:Read + User Memberships:Read
#                                            + Workers Secrets:Edit (for the
#                                            CF→AWS auth-boundary binding))
#   - SSM `/grug/cloudflare-account-id`
#   - SSM `/grug/cloudflare-zone-id`
#   - SSM `/grug/cf-shared-secret`          (CF→AWS auth boundary; PUT as
#                                            GRUG_CF_SECRET Worker binding
#                                            after each script upload)
#   - Pulumi stack output `webhook_function_url` (from `pulumi stack output`)
#
# Re-run after every `pulumi up` that recreated the Lambda (Function
# URL host changes on recreate per
# memory:reference_lambda_function_url_host_volatile).

set -euo pipefail

SCRIPT_DIR="$(dirname "$0")"

# CF→AWS auth boundary contract (parent #173). Single source of truth
# for the header name and the Worker secret binding name — both are
# sed-substituted into worker.js placeholders below. Lambda middleware
# in sibling slice #233 must read the same SECRET_HEADER string;
# spec 0014's attester enforces that cross-link.
SECRET_HEADER="X-Grug-CF-Secret"
BINDING_NAME="GRUG_CF_SECRET"

CF_TOKEN=$(aws ssm get-parameter --region us-east-1 \
    --name /grug/cloudflare-api-token --with-decryption \
    --query 'Parameter.Value' --output text)
CF_ACCOUNT=$(aws ssm get-parameter --region us-east-1 \
    --name /grug/cloudflare-account-id \
    --query 'Parameter.Value' --output text)
CF_ZONE=$(aws ssm get-parameter --region us-east-1 \
    --name /grug/cloudflare-zone-id \
    --query 'Parameter.Value' --output text)

# CF→AWS auth boundary (parent #173 / this slice #232). Optional: deploy
# can run before sibling slice #234's SSM param exists. If absent, we
# skip the binding-set step and the Worker's `env.GRUG_CF_SECRET` is
# undefined → worker.js falls through to its strip-only branch, Lambda
# middleware (#233) fail-opens. Once the operator runs `pulumi up` on
# #234, re-run this script to publish the binding.
#
# Discriminate ParameterNotFound (expected during rollout window) from
# every other failure mode (IAM regression, region misconfig, throttle,
# network). Silently swallowing all errors as "not configured" would
# leave the binding unset while SSM has the value — a security regression
# the operator wouldn't see until manually checking the CF dashboard.
CF_SHARED_SECRET=""
if cf_secret_raw=$(aws ssm get-parameter --region us-east-1 \
        --name /grug/cf-shared-secret --with-decryption \
        --query 'Parameter.Value' --output text 2>&1); then
    CF_SHARED_SECRET="$cf_secret_raw"
elif echo "$cf_secret_raw" | grep -q "ParameterNotFound"; then
    echo "  ⚠ /grug/cf-shared-secret missing in SSM — skipping GRUG_CF_SECRET binding."
    echo "    Run \`pulumi up\` on the grug stack first (creates the secret), then re-run this script."
else
    echo "  ✗ SSM get-parameter failed for /grug/cf-shared-secret:"
    echo "    $cf_secret_raw"
    exit 1
fi

# Pull current Lambda Function URLs from Pulumi stack output.
PULUMI_DIR="${PULUMI_CWD:-$(dirname "$0")/../pulumi}"

# Defensive `success` extraction for CF API responses. CF sometimes
# returns HTML on 5xx and stack traces on transport hiccups, both of
# which crash a naive `json.load(...)['success']` under `set -euo pipefail`.
# This helper prints "False" on any parse failure so callers get the
# raw response in their error message instead of a Python traceback.
parse_cf_success() {
    python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('success', False))
except (json.JSONDecodeError, ValueError):
    print('False')
"
}

deploy_one() {
    local worker_name="$1" route_pattern="$2" pulumi_output="$3"

    local function_url
    function_url=$(pulumi -C "$PULUMI_DIR" stack output "$pulumi_output" 2>/dev/null || true)
    if [ -z "$function_url" ] || [ "$function_url" = "null" ]; then
        echo "  ⚠ skip $worker_name — Pulumi output '$pulumi_output' missing (Lambda not yet deployed?)"
        return 0
    fi
    local upstream_host
    upstream_host=$(echo "$function_url" | sed -E 's|^https?://||; s|/$||')

    local worker_src="$SCRIPT_DIR/workers/$worker_name/worker.js"
    if [ ! -f "$worker_src" ]; then
        echo "  ✗ $worker_name source missing at $worker_src"
        exit 1
    fi

    echo "Deploying $worker_name → upstream $upstream_host"

    # Use /tmp/worker.js so the upload filename matches metadata.main_module
    # per memory: reference_cf_worker_upload_module_filename.
    sed \
      -e "s|__UPSTREAM_HOST__|$upstream_host|" \
      -e "s|__SECRET_HEADER__|$SECRET_HEADER|" \
      -e "s|__BINDING_NAME__|$BINDING_NAME|" \
      "$worker_src" > /tmp/worker.js

    local upload
    upload=$(curl -sS -X PUT \
        -H "Authorization: Bearer $CF_TOKEN" \
        -F 'metadata={"main_module":"worker.js","compatibility_date":"2024-09-01"};type=application/json' \
        -F "worker.js=@/tmp/worker.js;type=application/javascript+module" \
        "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT/workers/scripts/$worker_name")
    rm -f /tmp/worker.js

    local success
    success=$(echo "$upload" | parse_cf_success)
    if [ "$success" != "True" ]; then
        echo "  ✗ Upload FAILED. Raw response:"
        echo "$upload"
        exit 1
    fi
    echo "  ✓ Worker script uploaded"

    # PUT the secret binding so worker.js can inject the auth-boundary
    # header. The PUT-on-secrets endpoint is upsert semantics, so
    # re-runs are safe. Skipped when the SSM secret hasn't been
    # provisioned yet (see top of file for the rollout-order rationale).
    if [ -n "$CF_SHARED_SECRET" ]; then
        local secret_response
        secret_response=$(curl -sS -X PUT \
            -H "Authorization: Bearer $CF_TOKEN" \
            -H "Content-Type: application/json" \
            -d "{\"name\":\"$BINDING_NAME\",\"type\":\"secret_text\",\"text\":\"$CF_SHARED_SECRET\"}" \
            "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT/workers/scripts/$worker_name/secrets")
        local secret_success
        secret_success=$(echo "$secret_response" | parse_cf_success)
        if [ "$secret_success" != "True" ]; then
            echo "  ✗ $BINDING_NAME binding FAILED. Raw response:"
            echo "$secret_response"
            exit 1
        fi
        echo "  ✓ $BINDING_NAME binding set"
    fi

    local route_response
    route_response=$(curl -sS -X POST \
        "https://api.cloudflare.com/client/v4/zones/$CF_ZONE/workers/routes" \
        -H "Authorization: Bearer $CF_TOKEN" -H "Content-Type: application/json" \
        -d "{\"pattern\":\"$route_pattern\",\"script\":\"$worker_name\"}")

    local route_success err_code
    route_success=$(echo "$route_response" | parse_cf_success)
    if [ "$route_success" = "True" ]; then
        echo "  ✓ Route created: $route_pattern → $worker_name"
    else
        err_code=$(echo "$route_response" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    errs = d.get('errors') or []
    print(errs[0].get('code', '') if errs else '')
except (json.JSONDecodeError, ValueError):
    print('')
")
        if [ "$err_code" = "10020" ]; then
            echo "  ✓ Route already exists: $route_pattern"
        else
            echo "  ✗ Route POST FAILED. Raw response:"
            echo "$route_response"
            exit 1
        fi
    fi
}

deploy_one "grug-webhook-host-rewrite" "webhook.grug.lol/*" "webhook_function_url"
deploy_one "grug-api-host-rewrite"     "api.grug.lol/*"     "api_function_url"

echo "Done. Smoke:"
echo "  curl -i -X POST https://webhook.grug.lol/webhook/github -H 'X-Hub-Signature-256: sha256=invalid' -d '{}'  # → 401 (HMAC)"
echo "  curl -i https://api.grug.lol/livez                                                                          # → 200 ok"
if [ -n "$CF_SHARED_SECRET" ]; then
    echo "  # Boundary check (after middleware enforces): direct must 401, via-CF must 200."
    echo "  curl -i \"\$(pulumi -C $PULUMI_DIR stack output api_function_url)\"livez   # direct hit → 401 (header missing)"
    echo "  curl -i https://api.grug.lol/livez                                                                       # via CF → 200"
fi
