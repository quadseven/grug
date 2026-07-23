# Deploy grugthink on OKE

> **Automated:** `.github/workflows/grugthink.deploy.yml` runs on every push to
> `main` under `grugthink/**` (and via manual dispatch): it builds the arm64
> image, pushes it, seeds the namespace + `grugthink-secrets` +
> `grugthink-llm-fallback` (the chat fallback keys, below) + the registry pull
> secret, and applies the manifests. The steps below are the manual equivalent
> / reference. The tailnet host `grug.ts.ehumps.me` is served by the Caddy
> front in the private `infra` repo (reverse-proxy to
> `grugthink.grugthink.svc:8080`), never committed here.


grugthink v2 runs as one lightweight service on the OKE cluster: the multi-bot
manager + web dashboard, with chat/embeddings ALWAYS tried first on the
in-cluster **spark-gateway** (no SaaS in the common case). On a genuine
gateway failure/timeout, chat falls through a bounded, single-shot Poolside ->
OpenRouter fallback chain (see `bot/llm_clients.py`'s `_FALLBACK_TIMEOUT`
comment and `bot/prompts.py`'s `query_model`) - this is a last-resort valve,
not a second primary. You launch individual bots (Grug, Big Rob, ...) from the
dashboard at runtime. Bot-specific secrets (Discord tokens, session secret)
live in SSM under `/githumps/grugthink/*`; the fallback LLM keys live under
the shared `/infra/llm/*` namespace grug's own webhook/consumer also read.

## 1. Build + push the image (arm64, to the cluster's registry)

```bash
export REGISTRY=<your-registry-host>   # same private registry grug's images use
```

```bash
TAG=$(git rev-parse --short HEAD)
docker buildx build --platform linux/arm64 \
  -t $REGISTRY/grugthink:$TAG --push .
```

(This is a normal light image now - no torch/ML base. Needs the registry push
credential + tailnet, same as grug's image build; in CI it belongs on the same
`check.image-build` path.)

## 2. Seed the Secrets from SSM

```bash
kubectl create namespace grugthink --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic grugthink-secrets -n grugthink \
  --from-literal=SESSION_SECRET="$(aws ssm get-parameter \
      --name /githumps/grugthink/session_secret --with-decryption \
      --query Parameter.Value --output text)" \
  --dry-run=client -o yaml | kubectl apply -f -

# Chat fallback keys (Poolside/OpenRouter) - same /infra/llm/* params grug's
# own webhook/consumer read. Empty is fine (config_legacy.py treats an unset
# key as "skip this fallback tier cleanly"), so this never blocks a deploy.
kubectl create secret generic grugthink-llm-fallback -n grugthink \
  --from-literal=POOLSIDE_API_KEY="$(aws ssm get-parameter \
      --name /infra/llm/poolside_api_key --with-decryption \
      --query Parameter.Value --output text 2>/dev/null || echo '')" \
  --from-literal=OPENROUTER_API_KEY="$(aws ssm get-parameter \
      --name /infra/llm/openrouter_api_key --with-decryption \
      --query Parameter.Value --output text 2>/dev/null || echo '')" \
  --dry-run=client -o yaml | kubectl apply -f -

# Copy grug's registry pull secret into the namespace:
kubectl get secret registry-pull -n grug -o yaml \
  | sed 's/namespace: grug/namespace: grugthink/' | kubectl apply -f -

# Optional: review-relay's read-only GitHub token (bot/review_relay.py -
# fetches the real Grug - Elder check-run for "@grug review PR #N").
# checks:read scope only, deliberately separate from Hermes' broader
# GH_TOKEN. Skip this if the review-relay feature isn't wanted yet -
# it's optional:true in deployment.yaml and degrades to "Grug can't
# answer that yet" without it.
kubectl create secret generic grugthink-github -n grugthink \
  --from-literal=GRUGTHINK_GITHUB_CHECKS_TOKEN="$(aws ssm get-parameter \
      --name /githumps/grugthink/github_checks_token --with-decryption \
      --query Parameter.Value --output text 2>/dev/null || echo '')" \
  --dry-run=client -o yaml | kubectl apply -f -
```

## 3. Pin image placeholders + apply

```bash
sed -e "s#REGISTRY_PLACEHOLDER#$REGISTRY#" \
    -e "s#TAG_PLACEHOLDER#$TAG#" k8s/deployment.yaml | kubectl apply -f -
kubectl rollout status deploy/grugthink -n grugthink
```

## 4. Reach the dashboard + launch bots

```bash
kubectl port-forward -n grugthink svc/grugthink 8080:8080
# open http://localhost:8080
```

In the dashboard: add each Discord bot token (they're in SSM at
`/githumps/grugthink/discord_token_*`), pick a personality template
(Grug, Big Rob, ...), and Start. The gateway serves the LLM; the PVC persists
config + memory across restarts.

## Notes

- Chat fallback (Poolside/OpenRouter): bounded, single-shot, short-timeout -
  only engages when the spark-gateway primary produces nothing usable. Set
  `GEMINI_API_KEY` on the Deployment to also enable Gemini as a final bonus
  tier (query_gemini_api already exists and handles "not configured"
  cleanly on its own). See `bot/prompts.py`'s `query_model` and
  `bot/llm_clients.py`'s `_FALLBACK_TIMEOUT` comment for the full
  worst-case-time math and the incident that motivated single-shot,
  short timeouts here.
- LLM model: set `GRUGTHINK_LLM_MODEL` / `GRUGTHINK_EMBED_MODEL` on the
  Deployment if the gateway serves different model names. Make sure the gateway
  has an embedding model pulled (e.g. `nomic-embed-text`) for semantic memory;
  without it, memory degrades to keyword search (never crashes).
- OAuth login is disabled (`DISABLE_OAUTH=true`) - the dashboard is
  network-isolated (port-forward / tailnet). To require Discord login, set
  `DISABLE_OAUTH=false` and add `DISCORD_CLIENT_ID`/`DISCORD_CLIENT_SECRET`.
- Task relay to Hermes (`bot/task_relay.py`, `bot/review_relay.py`) is
  fail-closed/fail-safe by design - it does nothing until both
  `TASK_RELAY_ALLOWED_USER_IDS` (comma-separated Discord user IDs
  authorized to trigger a relay) and `HERMES_BOT_USER_ID` (Hermes' own
  Discord user ID, so a reply is only trusted if it's verifiably from
  Hermes) are set on the Deployment. Also needs Grug's Discord role
  granted visibility + send permission on the per-repo channels under
  the "Github" category - the same one-time step already done for
  Hermes. See the module docstrings for the full security model.
- `readOnlyRootFilesystem` is intentionally not set yet - harden it once the
  container's write paths (beyond `/data` and `/tmp`) are confirmed.
