#!/usr/bin/env bash
# Seed admin users into grug-main DDB table.
#
# Slice 2 acceptance: Evan + GF in DDB with role=admin, tier=lifetime,
# allowlisted=true so allowlist-gate logic (Slice 5 #26) passes for
# them as soon as it's wired.
#
# Usage:
#   bash scripts/seed-admins.sh
#
# Idempotent: PutItem upserts; running twice replaces with same values.
#
# To find your GitHub user numeric ID:
#   gh api /users/<login> --jq .id
#   curl -s https://api.github.com/users/<login> | jq .id

set -euo pipefail

TABLE_NAME="${TABLE_NAME:-grug-main}"
REGION="${AWS_REGION:-us-east-1}"
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Admin seeds. Replace numeric IDs with real GitHub user IDs.
# Lookup: gh api /users/<login> --jq .id
ADMINS=(
    # github_user_id|login|tier
    "59060157|githumps|lifetime"
    # "<gf_id>|<gf_login>|lifetime"  # uncomment + fill in for GF
)

for entry in "${ADMINS[@]}"; do
    IFS='|' read -r gh_id login tier <<< "$entry"
    pk="USER#$gh_id"
    echo "Seeding $login (gh_id=$gh_id) -> $pk"
    aws dynamodb put-item --region "$REGION" --table-name "$TABLE_NAME" --item "{
        \"PK\":             {\"S\": \"$pk\"},
        \"SK\":             {\"S\": \"META\"},
        \"login\":          {\"S\": \"$login\"},
        \"role\":           {\"S\": \"admin\"},
        \"tier\":           {\"S\": \"$tier\"},
        \"allowlisted\":    {\"BOOL\": true},
        \"created_at\":     {\"S\": \"$NOW\"},
        \"allowlisted_at\": {\"S\": \"$NOW\"},
        \"allowlisted_by\": {\"S\": \"manual-seed\"}
    }"
done

echo
echo "Verify:"
echo "  aws dynamodb scan --region $REGION --table-name $TABLE_NAME \\"
echo "    --filter-expression 'attribute_exists(allowlisted)' \\"
echo "    --projection-expression 'PK,login,#r,#t,allowlisted' \\"
echo "    --expression-attribute-names '{\"#r\":\"role\",\"#t\":\"tier\"}'"
