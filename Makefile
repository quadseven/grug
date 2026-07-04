# grug — local dev + Pulumi entry points
#
# Most common:
#   make test               — all tests (no AWS calls)
#   make webhook-test       — webhook unit tests
#   make api-test           — api unit tests
#   make pulumi-preview     — preview infra changes (read-only)
#   make pulumi-up          — apply infra changes (interactive confirm)
#
# Slice 10 (#31) — disaster-recovery proof:
#   make tear-down          — destroy dev stack (DESTRUCTIVE; ~5min)
#   make rebuild            — destroy + pulumi up (AWS infra) + deploy.k8s.yml
#                             (build+apply pods) + workers + reseed + smoke
#   make smoke              — quick prod-shape smoke test (read-only)

.PHONY: test webhook-test api-test pg-test pulumi-preview pulumi-up \
        tear-down rebuild smoke docker-build-webhook sast-benchmark

# Admin seed values — the row (re)installed after a tear-down so the
# allowlist gate works on the first PR after rebuild. NO DEFAULTS on
# purpose: a fork that ran this with the maintainer's hardcoded GitHub id
# would seed the upstream owner as the lifetime admin of the fork's own
# database (an accidental backdoor). You MUST supply your own via env:
#   GRUG_ADMIN_USER_ID=12345 GRUG_ADMIN_LOGIN=you GRUG_ADMIN_INSTALL_ID=678 make rebuild
GRUG_ADMIN_USER_ID ?=
GRUG_ADMIN_LOGIN   ?=
GRUG_ADMIN_INSTALL_ID ?=

test: webhook-test api-test

webhook-test:
	@if [ -n "$$CI" ] && [ -z "$$GRUG_TEST_DATABASE_URL" ]; then \
		echo "FATAL: store-backed tests would SKIP in CI (GRUG_TEST_DATABASE_URL unset) - a skipped gate is a silent pass (audit H4)"; exit 1; fi
	cd services/webhook && uv run --with pytest --with httpx --with pyjwt --with cryptography --with boto3 --with moto --with fastapi --with 'psycopg[binary,pool]' --with 'ddtrace>=3.5,<4' --with 'datadog-lambda>=6.107,<7' pytest tests/ -q

api-test:
	@if [ -n "$$CI" ] && [ -z "$$GRUG_TEST_DATABASE_URL" ]; then \
		echo "FATAL: store-backed tests would SKIP in CI (GRUG_TEST_DATABASE_URL unset) - a skipped gate is a silent pass (audit H4)"; exit 1; fi
	cd services/api && uv run --with pytest --with httpx --with pyjwt --with cryptography --with boto3 --with moto --with pydantic --with fastapi --with 'psycopg[binary,pool]' pytest tests/ -q

# Real-Postgres store tests (#354). REQUIRE a reachable Postgres via
# GRUG_TEST_DATABASE_URL (CI: workflow service container) - they skip
# loudly otherwise; sqlite stand-ins are banned for these semantics.
pg-test:
	@if [ -n "$$CI" ] && [ -z "$$GRUG_TEST_DATABASE_URL" ]; then \
		echo "FATAL: pg tests would SKIP in CI (GRUG_TEST_DATABASE_URL unset) - a skipped gate is a silent pass (audit H4)"; exit 1; fi
	cd services/api && uv run --with pytest --with "psycopg[binary,pool]" --with boto3 --with cryptography pytest tests/test_pg_stores.py -q -rs

pulumi-preview:
	cd infra/pulumi && uv sync && pulumi preview --stack dev

pulumi-up:
	cd infra/pulumi && uv sync && pulumi up --stack dev

# DESTRUCTIVE. Wipes the AWS-side infra in Pulumi state: KMS CMK (7-day
# deletion delay), SQS queues + DLQs, S3 cave bucket, IAM users/role, DD
# monitors/dashboard/RUM, and the legacy DDB table (pre-#354 data; unused).
# It does NOT touch the Kubernetes workloads (applied by deploy.k8s.yml,
# not Pulumi), the Postgres store (grug_kv lives outside this state and
# survives - hence the post-rebuild re-seed), or the GitHub App config +
# SSM secrets (registered at github.com / AWS SSM, not in Pulumi state).
tear-down:
	@echo ">> tear-down: destroying dev stack (CTRL-C now to abort)"
	@sleep 5
	cd infra/pulumi && pulumi destroy --stack dev --yes

# Round-trip verifier per Slice 10 #31, post-#354 (k8s). Steps:
#   1. tear-down (pulumi destroy --stack dev) — AWS infra only
#   2. pulumi up — recreate the AWS infra (SSM refs, KMS, SQS, S3, IAM, DD).
#      No ECR/bootstrap-image dance: the app no longer runs on Lambda.
#   3. trigger deploy.k8s.yml to build + push the arm64 images, seed the
#      in-cluster Secrets, and `kubectl apply -k k8s/`.
#      NOTE: deploy.k8s.yml only runs on `main` (or workflow_dispatch from
#      main) — run `make rebuild` from main for the app redeploy to fire.
#   4. CF Workers re-deploy + admin USER# + INST# re-seed (Slice 5 #26
#      allowlist gate requires at least one allowlisted user before any
#      PR check).
#   5. smoke test all three public URLs.
rebuild: tear-down
	@echo ">> step 2/5: pulumi up — recreate AWS infra (SSM/KMS/SQS/S3/IAM/DD)"
	cd infra/pulumi && pulumi up --stack dev --yes
	@echo ">> step 3/5: trigger deploy.k8s.yml — build+push images, apply k8s/"
	gh workflow run deploy.k8s.yml --ref $$(git branch --show-current) --repo githumps/grug
	@echo ">>   waiting for run to start..."
	@sleep 10
	@RUN_ID=$$(gh run list --workflow=deploy.k8s.yml --branch=$$(git branch --show-current) --repo githumps/grug --limit=1 --json databaseId --jq '.[0].databaseId'); \
	  echo ">>   following run $$RUN_ID"; \
	  gh run watch $$RUN_ID --exit-status --repo githumps/grug
	@echo ">> step 4/5: CF Workers re-deploy + admin re-seed"
	bash infra/cloudflare/deploy.sh
	@test -n "$(GRUG_ADMIN_USER_ID)" || { echo "FATAL: set GRUG_ADMIN_USER_ID (and GRUG_ADMIN_LOGIN/GRUG_ADMIN_INSTALL_ID) to YOUR own GitHub identity before seeding admin"; exit 1; }
	uv run --with 'psycopg[binary]' python infra/scripts/seed-admin.py \
	  --github-user-id $(GRUG_ADMIN_USER_ID) \
	  --login $(GRUG_ADMIN_LOGIN) \
	  --install-id $(GRUG_ADMIN_INSTALL_ID)
	# ^ needs GRUG_DATABASE_URL in the shell (post-#354 the store is
	# Postgres); the script exits 2 with a FATAL if it's unset.
	@echo ">> step 5/5: smoke test"
	$(MAKE) smoke
	@echo ">> rebuild complete."

# Read-only smoke. Asserts public URLs respond as expected.
smoke:
	@echo ">> smoke: webhook /livez"
	@curl -sf -o /dev/null -w "  https://webhook.grug.lol/livez → %{http_code}\n" https://webhook.grug.lol/livez
	@echo ">> smoke: api /livez"
	@curl -sf -o /dev/null -w "  https://api.grug.lol/livez → %{http_code}\n" https://api.grug.lol/livez
	@echo ">> smoke: api /api/v1/health"
	@curl -sf -o /dev/null -w "  https://api.grug.lol/api/v1/health → %{http_code}\n" https://api.grug.lol/api/v1/health
	@echo ">> smoke: splash"
	@curl -sf -o /dev/null -w "  https://grug.lol → %{http_code}\n" https://grug.lol
	@echo ">> smoke: webhook HMAC reject (unsigned POST must return 401)"
	@code=$$(curl -s -o /dev/null -w "%{http_code}" \
	  -X POST https://webhook.grug.lol/webhook/github -H 'Content-Type: application/json' -d '{}'); \
	  if [ "$$code" = "401" ]; then \
	    echo "  POST /webhook/github (no sig) → 401 ✓"; \
	  else \
	    echo "  POST /webhook/github (no sig) → $$code ✗ (expected 401)"; \
	    exit 1; \
	  fi

docker-build-webhook:
	docker buildx build --platform linux/arm64 \
		--tag grug-webhook:local \
		-f services/webhook/Dockerfile services

# SAST benchmark / eval harness (#399, ADR-0006). Measures Elder's vuln
# recall+precision per backend over the committed corpus. Makes REAL backend
# calls (free OpenRouter/Poolside keys; sparkles/Cave needs the tailnet) - set
# GRUG_BENCH_{OPENROUTER,POOLSIDE}_KEY and/or GRUG_BENCH_CAVE_URL+MODEL.
#   make sast-benchmark              # print report
#   make sast-benchmark ARGS=--record  # write baseline.json
#   make sast-benchmark ARGS=--check   # exit 1 on regression vs baseline
# The pure scoring core is covered by `make webhook-test` (no LLM, no keys).
sast-benchmark:
	cd services/webhook && PYTHONPATH=../_shared uv run --with httpx --with pyjwt --with cryptography \
		--with boto3 --with 'psycopg[binary,pool]' --with 'ddtrace>=3.5,<4' \
		--with 'datadog-lambda>=6.107,<7' python -m sast_benchmark $(ARGS)
