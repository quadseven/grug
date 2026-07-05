# Kubernetes runtime image for grug-webhook (#354 self-hosting migration).
# Built with CONTEXT=services/ (ADR-0014: the image carries services/_shared/
# - the single copy of the cross-service modules - alongside the service
# tree):  docker build services -f services/webhook/Dockerfile
# FastAPI app served by uvicorn under ddtrace-run (APM via the node's
# Datadog agent - DD_AGENT_HOST is injected by the Deployment).
# Build the AWS Roles Anywhere credential helper from source at a PINNED
# tag (#388, ADR-0008): the public binary distribution URLs are not
# reliably fetchable, and a source build pins provenance to the aws/
# rolesanywhere-credential-helper tag. Used by the SDK
# credential_process on every workload since the #389 rollout.
FROM golang:1.26-bookworm AS signing-helper
ARG SIGNING_HELPER_VERSION=v1.8.4
# CGO stays ON: the helper's pkcs11 support (miekg/pkcs11) is cgo-only
# and CGO_ENABLED=0 fails compilation. The binary therefore links the
# builder's glibc - keep BOTH stages on the same Debian release
# (bookworm) or the runtime exec dies; the image gate execs the binary
# inside the runtime image to pin that.
RUN git clone --depth 1 --branch ${SIGNING_HELPER_VERSION} \
      https://github.com/aws/rolesanywhere-credential-helper /src \
 && cd /src \
 && go build -trimpath -ldflags "-s -w" \
      -o /aws_signing_helper . \
 && /aws_signing_helper version

FROM python:3.13-slim AS runtime

ARG DD_GIT_REPOSITORY_URL=""
ARG DD_GIT_COMMIT_SHA=""
ENV DD_GIT_REPOSITORY_URL=${DD_GIT_REPOSITORY_URL}
ENV DD_GIT_COMMIT_SHA=${DD_GIT_COMMIT_SHA}
ENV PYTHONUNBUFFERED=1
# The service dir (/app, via WORKDIR/cwd) resolves first; _shared/ supplies
# the shared modules under their unchanged top-level import names (ADR-0014).
ENV PYTHONPATH=/app/_shared

WORKDIR /app

COPY webhook/requirements.txt .
# uvicorn is the k8s server - kept out of requirements.txt (it is not a
# library dependency of the app code, only of this runtime image).
RUN pip install --no-cache-dir -r requirements.txt "uvicorn>=0.34,<1"

COPY --from=signing-helper /aws_signing_helper /usr/local/bin/aws_signing_helper
COPY _shared/ ./_shared/
COPY webhook/ ./

RUN useradd --uid 10001 --no-create-home grug
USER 10001

EXPOSE 8080
# Single worker is INTENTIONAL at replicas:1 (the in-process token
# cache assumes one process; scale via replicas, not workers).
CMD ["ddtrace-run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--timeout-keep-alive", "15", "--timeout-graceful-shutdown", "20"]
