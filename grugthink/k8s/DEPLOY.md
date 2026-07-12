# Deploy grugthink on OKE

> **Automated:** `.github/workflows/grugthink.deploy.yml` runs on every push to
> `main` under `grugthink/**` (and via manual dispatch): it builds the arm64
> image, pushes it, seeds the namespace + `grugthink-secrets` from SSM + the
> registry pull secret, and applies the manifests. The steps below are the
> manual equivalent / reference. The tailnet host `grug.ts.ehumps.me` is served
> by the Caddy front in the private `infra` repo (reverse-proxy to
> `grugthink.grugthink.svc:8080`), never committed here.


grugthink v2 runs as one lightweight service on the OKE cluster: the multi-bot
manager + web dashboard, with chat/embeddings on the in-cluster **spark-gateway**
(no SaaS). You launch individual bots (Grug, Big Rob, ...) from the dashboard at
runtime. Secrets live in SSM under `/githumps/grugthink/*`.

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

## 2. Seed the Secret from SSM

```bash
kubectl create namespace grugthink --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic grugthink-secrets -n grugthink \
  --from-literal=SESSION_SECRET="$(aws ssm get-parameter \
      --name /githumps/grugthink/session_secret --with-decryption \
      --query Parameter.Value --output text)" \
  --dry-run=client -o yaml | kubectl apply -f -

# Copy grug's registry pull secret into the namespace:
kubectl get secret registry-pull -n grug -o yaml \
  | sed 's/namespace: grug/namespace: grugthink/' | kubectl apply -f -
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

- LLM model: set `GRUGTHINK_LLM_MODEL` / `GRUGTHINK_EMBED_MODEL` on the
  Deployment if the gateway serves different model names. Make sure the gateway
  has an embedding model pulled (e.g. `nomic-embed-text`) for semantic memory;
  without it, memory degrades to keyword search (never crashes).
- OAuth login is disabled (`DISABLE_OAUTH=true`) - the dashboard is
  network-isolated (port-forward / tailnet). To require Discord login, set
  `DISABLE_OAUTH=false` and add `DISCORD_CLIENT_ID`/`DISCORD_CLIENT_SECRET`.
- `readOnlyRootFilesystem` is intentionally not set yet - harden it once the
  container's write paths (beyond `/data` and `/tmp`) are confirmed.
