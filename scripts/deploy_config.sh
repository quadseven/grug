#!/usr/bin/env bash
# Resolve one deployment-specific value for workflows, scripts and the Makefile.
#
#   scripts/deploy_config.sh region [stack]   # AWS region
#   scripts/deploy_config.sh domain [stack]   # public apex domain
#
# Order: the GRUG_AWS_REGION / GRUG_DOMAIN environment variable (workflows
# pass these from the repository variables of the same name), then the
# Pulumi stack file infra/pulumi/Pulumi.<stack>.yaml (stack: the argument,
# else GRUG_STACK, else prod).
# A self-hoster sets the variable, or edits their own stack file; nothing
# else in the tree carries these values.
#
# The value is validated as a bare hostname/region token because callers
# interpolate it into URLs, JSON and sed expressions.
set -euo pipefail

key="${1:-}"
stack="${2:-${GRUG_STACK:-prod}}"
case "$key" in
  region) override="${GRUG_AWS_REGION:-}"; yaml_key="aws:region" ;;
  domain) override="${GRUG_DOMAIN:-}"; yaml_key="grug:domain" ;;
  *) echo "usage: $0 region|domain [stack]" >&2; exit 2 ;;
esac

value="$override"
if [ -z "$value" ]; then
  root="$(cd "$(dirname "$0")/.." && pwd)"
  file="${GRUG_PULUMI_STACK_FILE:-$root/infra/pulumi/Pulumi.$stack.yaml}"
  if [ -f "$file" ]; then
    value="$(sed -n "s/^[[:space:]]*${yaml_key}:[[:space:]]*//p" "$file" | head -n1 | tr -d "\"' \r")"
  fi
fi

if [ -z "$value" ]; then
  echo "deploy_config: no $key set: define GRUG_AWS_REGION / GRUG_DOMAIN or ${yaml_key} in the Pulumi stack file" >&2
  exit 1
fi
if ! printf '%s' "$value" | grep -Eq '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$'; then
  echo "deploy_config: $key value is not a plain hostname/region token" >&2
  exit 1
fi
printf '%s\n' "$value"
