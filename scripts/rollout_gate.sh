#!/usr/bin/env bash
# Rollout gate with a diagnostics handler (#1238).
#
# usage: rollout_gate.sh <namespace> <timeout> <deployment>...
#
# Waits on `kubectl rollout status` for each deployment in order. On the
# first one that does not finish, it captures the stuck state BEFORE anything
# restarts it - the auto-rollback that follows a failed deploy restarts every
# deployment, and the stuck pod (and the only evidence of why it was stuck)
# is gone by the time anyone looks. deploy.k8s run 39097624374 lost exactly
# that: an old webhook pod sat in "pending termination" for 2m42s against a
# 180s timeout, the rollback reverted production, and nothing recorded
# whether it was a finalizer, a preStop, node pressure or a bad pod.
#
# The capture goes to stdout AND to $GITHUB_STEP_SUMMARY, so it is readable
# from the run page even when the calling step is continue-on-error and the
# rollback step has already restarted the deployment. Every capture command
# is best-effort: a diagnostic that fails must never mask the real failure,
# and the gate still exits non-zero.
#
# No `kubectl logs` / exec: the kubelet port is unreachable on our BYON
# nodes, so only API-served reads are used (see deploy.k8s.yml synthetic).
set -uo pipefail

if [ "$#" -lt 3 ]; then
  echo "usage: $0 <namespace> <timeout> <deployment>..." >&2
  exit 2
fi
ns=$1
timeout=$2
shift 2

summary=${GITHUB_STEP_SUMMARY:-/dev/null}

# section <title> <command...>: run a best-effort read, print it, and append
# it to the step summary as a collapsed block (capped so one huge describe
# cannot blow the summary size limit).
section() {
  local title=$1
  shift
  local out
  out=$("$@" 2>&1 | head -n 200) || true
  [ -n "$out" ] || out="(no output)"
  printf '::group::%s\n%s\n::endgroup::\n' "$title" "$out"
  {
    printf '<details><summary>%s</summary>\n\n```text\n%s\n```\n\n</details>\n\n' "$title" "$out"
  } >> "$summary"
}

capture() {  # capture <deployment>
  local d=$1 sel
  sel=$(kubectl -n "$ns" get "deploy/$d" \
    -o go-template='{{range $k, $v := .spec.selector.matchLabels}}{{$k}}={{$v}},{{end}}' 2>/dev/null) || sel=""
  sel=${sel%,}
  {
    printf '### rollout of deploy/%s did not finish within %s\n\n' "$d" "$timeout"
    printf 'Captured before the rollback restarts it (#1238). Selector: `%s`\n\n' "${sel:-unknown}"
  } >> "$summary"
  section "deploy/$d" kubectl -n "$ns" get "deploy/$d" -o wide
  section "pods (all, wide)" kubectl -n "$ns" get pods -o wide
  if [ -n "$sel" ]; then
    # deletionTimestamp + finalizers answer "why will the old pod not go
    # away" directly; describe adds the probe, grace and event detail.
    section "pods of $d: termination state" kubectl -n "$ns" get pods -l "$sel" \
      -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,DELETING_SINCE:.metadata.deletionTimestamp,GRACE_S:.metadata.deletionGracePeriodSeconds,FINALIZERS:.metadata.finalizers,NODE:.spec.nodeName'
    section "replicasets of $d" kubectl -n "$ns" get rs -l "$sel" -o wide
    section "describe pods of $d" kubectl -n "$ns" describe pods -l "$sel"
  fi
  section "describe deploy/$d" kubectl -n "$ns" describe "deploy/$d"
  section "events (newest last)" recent_events
}

recent_events() {
  kubectl -n "$ns" get events --sort-by=.lastTimestamp | tail -n 40
}

for d in "$@"; do
  if ! kubectl -n "$ns" rollout status "deploy/$d" --timeout="$timeout"; then
    echo "::error::rollout of deploy/$d did not finish within $timeout - capturing pods, describe and events before anything restarts them (#1238)"
    capture "$d"
    exit 1
  fi
done
