#!/usr/bin/env bash
# Bootstrap the grug-web Cloudflare Pages project + apex grug.lol domain.
#
# Idempotent — safe to re-run. Handles three states:
#   1. project doesn't exist        → create
#   2. project exists, no apex bind → add custom domain
#   3. fully bootstrapped           → no-op (logs "ok")
#
# CF Pages auto-creates the apex DNS record (CNAME flattening) when the
# custom domain is bound, so no separate DNS step is needed.
#
# Run ONCE before the first wrangler pages deploy. After that, the
# web.deploy.yml workflow's `wrangler pages deploy` is enough.

set -euo pipefail

CF_TOKEN=$(aws ssm get-parameter --region us-east-1 \
    --name /grug/cloudflare-api-token --with-decryption \
    --query 'Parameter.Value' --output text)
CF_ACCOUNT=$(aws ssm get-parameter --region us-east-1 \
    --name /grug/cloudflare-account-id \
    --query 'Parameter.Value' --output text)

PROJECT="grug-web"
APEX="grug.lol"
PROD_BRANCH="main"

api() {
    curl -sS -H "Authorization: Bearer $CF_TOKEN" -H "Content-Type: application/json" "$@"
}

# Detect "missing Pages:Edit token perm" error 10000 + provide actionable
# guidance (closes a tail of #46). CF returns the same generic auth error
# for every Pages-API call when the token lacks Account → Cloudflare
# Pages: Edit perm.
explain_if_pages_perm_missing() {
    local resp="$1"
    if echo "$resp" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    errs = d.get('errors') or []
    for e in errs:
        # 10000 = generic 'unauthorized'; 10001 = expired; pages-specific perms
        # surface as 10000 + message containing 'Cloudflare Pages'
        if e.get('code') == 10000 or 'Cloudflare Pages' in e.get('message',''):
            sys.exit(0)
    sys.exit(1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
        cat <<'EOF'

  ✗ Cloudflare API rejected the request — looks like a token-perm issue.

  The /grug/cloudflare-api-token must have:
    - Zone → DNS → Edit
    - Account → Workers Scripts → Edit
    - Account → Workers Routes → Edit
    - Account → Cloudflare Pages → Edit  ← easy to miss

  Fix:
    1. https://dash.cloudflare.com/profile/api-tokens
    2. Edit the grug.lol token → Add 'Account → Cloudflare Pages: Edit'
    3. Save → re-run this script (idempotent).

  See docs/HITL_PREREQUISITES.md / issue #46 for context.
EOF
    fi
}

# 1. Ensure project exists.
project_resp=$(api "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT/pages/projects/$PROJECT")
if echo "$project_resp" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('success') else 1)" 2>/dev/null; then
    echo "  ✓ Pages project '$PROJECT' exists"
else
    echo "Creating Pages project '$PROJECT'…"
    create_resp=$(api -X POST \
        "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT/pages/projects" \
        -d "{\"name\":\"$PROJECT\",\"production_branch\":\"$PROD_BRANCH\"}")
    if ! echo "$create_resp" | python3 -c "import json,sys; sys.exit(0 if json.load(sys.stdin).get('success') else 1)" 2>/dev/null; then
        echo "  ✗ Project create FAILED:"; echo "$create_resp" | python3 -m json.tool
        explain_if_pages_perm_missing "$create_resp"
        exit 1
    fi
    echo "  ✓ Project created"
fi

# 2. Ensure apex domain bound.
domains_resp=$(api "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT/pages/projects/$PROJECT/domains")
already_bound=$(echo "$domains_resp" | python3 -c "
import json, sys
d = json.load(sys.stdin)
domains = d.get('result') or []
print('yes' if any(x.get('name') == '$APEX' for x in domains) else 'no')
")

if [ "$already_bound" = "yes" ]; then
    echo "  ✓ Apex '$APEX' already bound"
else
    echo "Binding apex '$APEX' to project…"
    bind_resp=$(api -X POST \
        "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT/pages/projects/$PROJECT/domains" \
        -d "{\"name\":\"$APEX\"}")
    if ! echo "$bind_resp" | python3 -c "import json,sys; sys.exit(0 if json.load(sys.stdin).get('success') else 1)" 2>/dev/null; then
        echo "  ✗ Domain bind FAILED:"; echo "$bind_resp" | python3 -m json.tool
        explain_if_pages_perm_missing "$bind_resp"
        exit 1
    fi
    echo "  ✓ Apex bound (CF will auto-create the DNS record; allow ~30s for verification)"
fi

echo
echo "Bootstrap complete. Next steps:"
echo "  1. Push to feat/27-* OR main → web.deploy.yml runs wrangler pages deploy"
echo "  2. Smoke: curl -I https://$APEX  # → 200 OK once first deploy lands"
