# grug — local dev + Pulumi entry points
#
# Most common:
#   make test               — run all tests (no AWS calls)
#   make webhook-test       — webhook unit tests
#   make pulumi-preview     — preview infra changes (read-only)
#   make pulumi-up          — apply infra changes (interactive confirm)
#   make tear-down          — destroy dev stack (DESTRUCTIVE)
#   make rebuild            — destroy + up (round-trip verify per Slice 10)

.PHONY: test webhook-test pulumi-preview pulumi-up tear-down rebuild docker-build-webhook

test: webhook-test

webhook-test:
	cd services/webhook && python3 -m pytest tests/ -v

pulumi-preview:
	cd infra/pulumi && uv sync && pulumi preview --stack dev

pulumi-up:
	cd infra/pulumi && uv sync && pulumi up --stack dev

tear-down:
	cd infra/pulumi && pulumi destroy --stack dev --yes

rebuild: tear-down
	cd infra/pulumi && pulumi up --stack dev --yes

docker-build-webhook:
	docker buildx build --platform linux/arm64 \
		--tag grug-webhook:local \
		services/webhook
