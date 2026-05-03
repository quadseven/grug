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
#                                            Scripts:Edit + Workers Routes:Edit)
#   - SSM `/grug/cloudflare-account-id`
#   - SSM `/grug/cloudflare-zone-id`
#   - Pulumi stack output `webhook_function_url` (from `pulumi stack output`)
#
# Re-run after every `pulumi up` that recreated the Lambda (Function
# URL host changes on recreate per
# memory:reference_lambda_function_url_host_volatile).

set -euo pipefail

WORKER_NAME="grug-webhook-host-rewrite"
ROUTE_PATTERN="webhook.grug.lol/*"
WORKER_SRC="$(dirname "$0")/workers/grug-webhook-host-rewrite/worker.js"

CF_TOKEN=$(aws ssm get-parameter --region us-east-1 \
    --name /grug/cloudflare-api-token --with-decryption \
    --query 'Parameter.Value' --output text)
CF_ACCOUNT=$(aws ssm get-parameter --region us-east-1 \
    --name /grug/cloudflare-account-id \
    --query 'Parameter.Value' --output text)
CF_ZONE=$(aws ssm get-parameter --region us-east-1 \
    --name /grug/cloudflare-zone-id \
    --query 'Parameter.Value' --output text)

# Pull current Lambda Function URL from Pulumi stack output. Run from
# infra/pulumi/ if PULUMI_CWD not set.
PULUMI_DIR="${PULUMI_CWD:-$(dirname "$0")/../pulumi}"
FUNCTION_URL=$(pulumi -C "$PULUMI_DIR" stack output webhook_function_url)
UPSTREAM_HOST=$(echo "$FUNCTION_URL" | sed -E 's|^https?://||; s|/$||')

echo "Deploying $WORKER_NAME → upstream $UPSTREAM_HOST"

# Render worker source with the upstream host. Use /tmp/worker.js so
# the upload filename matches metadata.main_module per memory:
# reference_cf_worker_upload_module_filename.
sed "s|__UPSTREAM_HOST__|$UPSTREAM_HOST|" "$WORKER_SRC" > /tmp/worker.js

upload=$(curl -sS -X PUT \
    -H "Authorization: Bearer $CF_TOKEN" \
    -F 'metadata={"main_module":"worker.js","compatibility_date":"2024-09-01"};type=application/json' \
    -F "worker.js=@/tmp/worker.js;type=application/javascript+module" \
    "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT/workers/scripts/$WORKER_NAME")

success=$(echo "$upload" | python3 -c "import json,sys; print(json.load(sys.stdin)['success'])")
if [ "$success" != "True" ]; then
    echo "Worker upload FAILED:"
    echo "$upload" | python3 -m json.tool
    rm -f /tmp/worker.js
    exit 1
fi
echo "  ✓ Worker script uploaded"
rm -f /tmp/worker.js

# Route binding — POST returns 409 if it already exists; that's fine.
route_response=$(curl -sS -X POST \
    "https://api.cloudflare.com/client/v4/zones/$CF_ZONE/workers/routes" \
    -H "Authorization: Bearer $CF_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"pattern\":\"$ROUTE_PATTERN\",\"script\":\"$WORKER_NAME\"}")

route_success=$(echo "$route_response" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['success'])")
if [ "$route_success" = "True" ]; then
    echo "  ✓ Route created: $ROUTE_PATTERN → $WORKER_NAME"
else
    err_code=$(echo "$route_response" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['errors'][0]['code'] if d.get('errors') else '')")
    if [ "$err_code" = "10020" ]; then
        echo "  ✓ Route already exists: $ROUTE_PATTERN"
    else
        echo "Route POST FAILED:"
        echo "$route_response" | python3 -m json.tool
        exit 1
    fi
fi

echo "Done. Smoke:"
echo "  curl -i -X POST https://webhook.grug.lol/webhook/github -H 'X-Hub-Signature-256: sha256=invalid' -d '{}'"
echo "  Expect: HTTP 401 (real Lambda handler responding via Worker proxy)"
