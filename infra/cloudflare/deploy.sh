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
#                                            Settings:Read + User Memberships:Read)
#   - SSM `/grug/cloudflare-account-id`
#   - SSM `/grug/cloudflare-zone-id`
#   - Pulumi stack output `webhook_function_url` (from `pulumi stack output`)
#
# Re-run after every `pulumi up` that recreated the Lambda (Function
# URL host changes on recreate per
# memory:reference_lambda_function_url_host_volatile).

set -euo pipefail

SCRIPT_DIR="$(dirname "$0")"

CF_TOKEN=$(aws ssm get-parameter --region us-east-1 \
    --name /grug/cloudflare-api-token --with-decryption \
    --query 'Parameter.Value' --output text)
CF_ACCOUNT=$(aws ssm get-parameter --region us-east-1 \
    --name /grug/cloudflare-account-id \
    --query 'Parameter.Value' --output text)
CF_ZONE=$(aws ssm get-parameter --region us-east-1 \
    --name /grug/cloudflare-zone-id \
    --query 'Parameter.Value' --output text)

# Pull current Lambda Function URLs from Pulumi stack output.
PULUMI_DIR="${PULUMI_CWD:-$(dirname "$0")/../pulumi}"

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
    sed "s|__UPSTREAM_HOST__|$upstream_host|" "$worker_src" > /tmp/worker.js

    local upload
    upload=$(curl -sS -X PUT \
        -H "Authorization: Bearer $CF_TOKEN" \
        -F 'metadata={"main_module":"worker.js","compatibility_date":"2024-09-01"};type=application/json' \
        -F "worker.js=@/tmp/worker.js;type=application/javascript+module" \
        "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT/workers/scripts/$worker_name")
    rm -f /tmp/worker.js

    local success
    success=$(echo "$upload" | python3 -c "import json,sys; print(json.load(sys.stdin)['success'])")
    if [ "$success" != "True" ]; then
        echo "  ✗ Upload FAILED:"; echo "$upload" | python3 -m json.tool; exit 1
    fi
    echo "  ✓ Worker script uploaded"

    local route_response
    route_response=$(curl -sS -X POST \
        "https://api.cloudflare.com/client/v4/zones/$CF_ZONE/workers/routes" \
        -H "Authorization: Bearer $CF_TOKEN" -H "Content-Type: application/json" \
        -d "{\"pattern\":\"$route_pattern\",\"script\":\"$worker_name\"}")

    local route_success err_code
    route_success=$(echo "$route_response" | python3 -c "import json,sys; print(json.load(sys.stdin)['success'])")
    if [ "$route_success" = "True" ]; then
        echo "  ✓ Route created: $route_pattern → $worker_name"
    else
        err_code=$(echo "$route_response" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['errors'][0]['code'] if d.get('errors') else '')")
        if [ "$err_code" = "10020" ]; then
            echo "  ✓ Route already exists: $route_pattern"
        else
            echo "  ✗ Route POST FAILED:"; echo "$route_response" | python3 -m json.tool; exit 1
        fi
    fi
}

deploy_one "grug-webhook-host-rewrite" "webhook.grug.lol/*" "webhook_function_url"
deploy_one "grug-api-host-rewrite"     "api.grug.lol/*"     "api_function_url"

echo "Done. Smoke:"
echo "  curl -i -X POST https://webhook.grug.lol/webhook/github -H 'X-Hub-Signature-256: sha256=invalid' -d '{}'  # → 401"
echo "  curl -i https://api.grug.lol/livez                                                                          # → 200 ok"
