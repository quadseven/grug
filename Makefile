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
#   make rebuild            — destroy + up + image-rebuild + workers + reseed + smoke (~12-15min)
#   make smoke              — quick prod-shape smoke test (read-only)

.PHONY: test webhook-test api-test pulumi-preview pulumi-up \
        tear-down rebuild bootstrap-images smoke docker-build-webhook

# Admin seed defaults — the values that get re-installed after a tear-down
# so allowlist gate works on first PR after rebuild. Override via env.
GRUG_ADMIN_USER_ID ?= 59060157
GRUG_ADMIN_LOGIN   ?= githumps
GRUG_ADMIN_INSTALL_ID ?= 129256114

test: webhook-test api-test

webhook-test:
	cd services/webhook && uv run --with pytest --with httpx --with pyjwt --with cryptography --with boto3 --with moto pytest tests/ -q

api-test:
	cd services/api && uv run --with pytest --with httpx --with pyjwt --with cryptography --with boto3 --with moto --with pydantic --with fastapi pytest tests/ -q

# Real-Postgres store tests (#354). REQUIRE a reachable Postgres via
# GRUG_TEST_DATABASE_URL (CI: workflow service container) - they skip
# loudly otherwise; sqlite stand-ins are banned for these semantics.
test-pg:
	cd services/api && uv run --with pytest --with "psycopg[binary,pool]" --with boto3 --with cryptography pytest tests/test_pg_stores.py -q -rs

pulumi-preview:
	cd infra/pulumi && uv sync && pulumi preview --stack dev

pulumi-up:
	cd infra/pulumi && uv sync && pulumi up --stack dev

# DESTRUCTIVE. Wipes Lambdas, DDB rows (incl. admin USER# + INST#),
# CMK (7-day deletion delay), CF DNS records, DD monitors. GitHub
# App config + SSM secrets persist (registered at github.com / AWS
# SSM, not in Pulumi state).
tear-down:
	@echo ">> tear-down: destroying dev stack (CTRL-C now to abort)"
	@sleep 5
	cd infra/pulumi && pulumi destroy --stack dev --yes

# Round-trip verifier per Slice 10 #31. Steps:
#   1. tear-down (pulumi destroy --stack dev)
#   2. pulumi up --target ECR repos only (Lambda image-mode needs the
#      ECR repo URL to exist + a :bootstrap image present BEFORE its
#      first create succeeds)
#   3. bootstrap-images (crane copy public python:3.13 → private ECR)
#   4. pulumi up — full stack (Lambdas now resolve image_uri)
#   5. trigger CI to build + push real images, swap imageUri
#   6. CF Workers re-deploy + admin USER# + INST# re-seed (Slice 5
#      #26 allowlist gate requires at least one allowlisted user
#      before any PR check) — also picks up Function URL host churn
#      per reference_lambda_function_url_host_volatile
#   7. smoke test all three public URLs
#
# Total wall-clock: ~12-15min. PR check-runs queue + retry post-rebuild.
rebuild: tear-down
	@echo ">> step 2/7: pulumi up — ECR repos only (Lambda needs :bootstrap image to exist FIRST)"
	cd infra/pulumi && pulumi up --stack dev --yes \
	  --target 'urn:pulumi:dev::grug::aws:ecr/repository:Repository::grug-webhook' \
	  --target 'urn:pulumi:dev::grug::aws:ecr/repository:Repository::grug-api'
	@echo ">> step 3/7: bootstrap images (crane copy public python:3.13 base into private ECR)"
	$(MAKE) bootstrap-images
	@echo ">> step 4/7: pulumi up — full stack (Lambdas now have :bootstrap to point at)"
	cd infra/pulumi && pulumi up --stack dev --yes
	@echo ">> step 5/7: triggering CI to build + push real images"
	gh workflow run iac.deploy.yml --ref $$(git branch --show-current) --repo githumps/grug
	@echo ">>   waiting for run to start..."
	@sleep 10
	@RUN_ID=$$(gh run list --workflow=iac.deploy.yml --branch=$$(git branch --show-current) --repo githumps/grug --limit=1 --json databaseId --jq '.[0].databaseId'); \
	  echo ">>   following run $$RUN_ID"; \
	  gh run watch $$RUN_ID --exit-status --repo githumps/grug
	@echo ">> step 6/7: CF Workers re-deploy + admin re-seed"
	bash infra/cloudflare/deploy.sh
	AWS_DEFAULT_REGION=us-east-1 uv run --with boto3 python infra/scripts/seed-admin.py \
	  --github-user-id $(GRUG_ADMIN_USER_ID) \
	  --login $(GRUG_ADMIN_LOGIN) \
	  --install-id $(GRUG_ADMIN_INSTALL_ID)
	@echo ">> step 7/7: smoke test"
	$(MAKE) smoke
	@echo ">> rebuild complete."

# Bootstrap public Lambda Python base into our private ECR repos under
# the `:bootstrap` tag. Lambda image-mode rejects public.ecr.aws/* URIs
# directly, AND a freshly-created ECR repo is empty so pulumi up against
# `<our-ecr>/<repo>:bootstrap` fails until something is pushed first.
# This target uses `crane` (daemonless) to copy the public base image
# into both grug-webhook + grug-api repos.
bootstrap-images:
	@command -v crane >/dev/null || { echo "crane not installed: brew install crane"; exit 1; }
	@ACCT=$$(aws sts get-caller-identity --query Account --output text); \
	  ECR="$$ACCT.dkr.ecr.us-east-1.amazonaws.com"; \
	  aws ecr get-login-password --region us-east-1 \
	    | crane auth login "$$ECR" --username AWS --password-stdin; \
	  crane copy public.ecr.aws/lambda/python:3.13-arm64 "$$ECR/grug-webhook:bootstrap"; \
	  crane copy public.ecr.aws/lambda/python:3.13-arm64 "$$ECR/grug-api:bootstrap"

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
		services/webhook
