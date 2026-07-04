# Kubernetes runtime image for grug-api (#354 self-hosting migration).
# Built with CONTEXT=services/ (ADR-0014: the image carries services/_shared/
# - the single copy of the cross-service modules - alongside the service
# tree):  docker build services -f services/api/Dockerfile
# FastAPI app served by uvicorn under ddtrace-run (APM via the node's
# Datadog agent - DD_AGENT_HOST is injected by the Deployment).
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

COPY api/requirements.txt .
# uvicorn is the k8s server - kept out of requirements.txt (it is not a
# library dependency of the app code, only of this runtime image).
RUN pip install --no-cache-dir -r requirements.txt "uvicorn>=0.34,<1"

COPY _shared/ ./_shared/
COPY api/ ./

RUN useradd --uid 10001 --no-create-home grug
USER 10001

EXPOSE 8080
# Single worker is INTENTIONAL at replicas:1 (the in-process token
# cache assumes one process; scale via replicas, not workers).
CMD ["ddtrace-run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--timeout-keep-alive", "15", "--timeout-graceful-shutdown", "20"]
