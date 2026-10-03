# Kubernetes runtime image for BOTH grug services (#354; unified at #389 -
# the #495-era two-file deferral rationale died when the signing-helper
# stage landed in both and the diff shrank to three path lines).
# Build:  docker build services -f services/Dockerfile --build-arg SERVICE=<api|webhook>
# CONTEXT=services/ (ADR-0014: the image carries services/_shared/ - the
# single copy of the cross-service modules - alongside the service tree)
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
# and CGO_ENABLED=0 fails compilation. The binary links the BUILDER's
# glibc, so the invariant is DIRECTIONAL: runtime glibc >= builder glibc
# (python:*-slim tracks newer Debian than the pinned golang builder -
# forward-compatible). The image gate execs the binary inside the
# runtime image, so a violation dies pre-merge, not in a pod.
RUN git clone --depth 1 --branch ${SIGNING_HELPER_VERSION} \
      https://github.com/aws/rolesanywhere-credential-helper /src \
 && cd /src \
 && go build -trimpath -ldflags "-s -w" \
      -o /aws_signing_helper . \
 && /aws_signing_helper version

# Opengrep: the LGPL-2.1 community fork of Semgrep CE (#858), replacing semgrep
# per the free/open-source-only constraint. Fetched in its OWN stage, not the
# runtime one: python:*-slim ships no curl, so a runtime-stage download fails
# with `curl: not found` (caught by CI, not by review). Same directional
# glibc invariant as the signing-helper above - the binary is manylinux, the
# runtime is newer Debian, so forward-compatible.
#
# Pinned, not floating: a scanner whose engine changes under you is how a
# security control drifts without anyone noticing.
FROM debian:bookworm-slim AS opengrep-fetch
ARG TARGETARCH
ARG OPENGREP_VERSION=1.27.1
# TARGETARCH is supplied by BuildKit; it is EMPTY under the legacy builder, so
# fall back to dpkg's architecture rather than trusting it to be set.
#
# Selecting the asset by architecture is NOT optional: this image is deployed to
# arm64 nodes, and hardcoding the x86 asset produces an image that COPYs and
# pushes cleanly and then dies in every pod. The build-time `--version` exec in
# the runtime stage is what turns that into a failed build rather than a
# CrashLoop - keep it.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && ARCH="${TARGETARCH:-$(dpkg --print-architecture)}" \
 && case "$ARCH" in \
      arm64|aarch64) OG_ASSET=opengrep_manylinux_aarch64 ;; \
      amd64|x86_64)  OG_ASSET=opengrep_manylinux_x86 ;; \
      *) echo "unsupported architecture: $ARCH" >&2; exit 1 ;; \
    esac \
 && curl -fsSL -o /opengrep \
      "https://github.com/opengrep/opengrep/releases/download/v${OPENGREP_VERSION}/${OG_ASSET}" \
 && chmod 0755 /opengrep

FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1
# Force UTF-8. The image sets no locale, so a bundled-interpreter binary
# (opengrep ships its own Python) falls back to the ASCII codec and dies
# reading any rule file containing a non-ASCII byte:
#   UnicodeDecodeError: 'ascii' codec can't decode byte 0xe2
# Caught by building the image and actually running a scan; the rule file
# is also de-unicoded below, but this is the fix that survives the next
# person pasting a smart quote into a rule description.
ENV PYTHONUTF8=1
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
# The service dir (/app, via WORKDIR/cwd) resolves first; _shared/ supplies
# the shared modules under their unchanged top-level import names (ADR-0014).
ENV PYTHONPATH=/app/_shared

WORKDIR /app

ARG SERVICE
# Fail LOUD on a missing build-arg: an unset SERVICE degrades COPY to
# context-root paths - today that errors by accident; the day a root
# requirements.txt exists it becomes a silent whole-context image that
# CrashLoops at uvicorn (audit #506-A).
RUN test -n "$SERVICE" || { echo "SERVICE build-arg required (api|webhook)" >&2; exit 1; }
COPY ${SERVICE}/requirements.txt .
# uvicorn is the k8s server - kept out of requirements.txt (it is not a
# library dependency of the app code, only of this runtime image).
RUN pip install --no-cache-dir -r requirements.txt "uvicorn>=0.34,<1"

COPY --from=signing-helper /aws_signing_helper /usr/local/bin/aws_signing_helper
# /usr/local/bin because the runtime user is created --no-create-home below,
# so $HOME does not exist and anything installed into it would vanish.
COPY --from=opengrep-fetch /opengrep /usr/local/bin/opengrep
# Exec it during the build: a scanner that is present but non-runnable is the
# exact failure this replaces, and it must die pre-merge rather than in a pod.
RUN /usr/local/bin/opengrep --version
COPY _shared/ ./_shared/
COPY ${SERVICE}/ ./

# DELIBERATELY the last cache-key steps, below the pip install above.
# DD_GIT_COMMIT_SHA is a different value on EVERY build (check.image-build.yml
# passes the PR head sha, deploy.k8s.yml passes GITHUB_SHA). An ENV is a
# cache-key step, so while these sat at the top of the stage they invalidated
# every layer beneath them - the ~127 MB dependency layer was rebuilt and
# re-pushed under a fresh digest on every commit, even when requirements.txt
# had not changed, and the registry stored six copies of it. Down here the
# per-commit value only invalidates what already changes per commit (the
# source COPY above). ENV persists into the final image wherever it is
# declared, so the baked values - and ADR-0016's promotion check that verifies
# DD_GIT_COMMIT_SHA against head.sha - are unaffected.
ARG DD_GIT_REPOSITORY_URL=""
ARG DD_GIT_COMMIT_SHA=""
ENV DD_GIT_REPOSITORY_URL=${DD_GIT_REPOSITORY_URL}
ENV DD_GIT_COMMIT_SHA=${DD_GIT_COMMIT_SHA}

RUN useradd --uid 10001 --no-create-home grug
USER 10001

EXPOSE 8080
# Single worker is INTENTIONAL at replicas:1 (the in-process token
# cache assumes one process; scale via replicas, not workers).
CMD ["ddtrace-run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--timeout-keep-alive", "15", "--timeout-graceful-shutdown", "20"]
