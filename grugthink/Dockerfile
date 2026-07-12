# grugthink v2 - single light image. The old two-stage build needed a heavy
# ML base (torch + sentence-transformers, ~2GB); v2 runs chat + embeddings on
# the spark-gateway, so the image is just the app + faiss/numpy for the local
# vector index. Non-root, matching grug's hardening.
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*
# libgomp1: faiss-cpu links OpenMP at runtime; without it `import faiss` raises
# ImportError on the slim base -> vector search silently disabled.

# Python deps (light now - no torch). Layer-cached before app code.
COPY requirements.txt ./
RUN pip install --no-cache-dir --no-compile --disable-pip-version-check \
    --prefer-binary -r requirements.txt \
    && rm -rf /root/.cache /tmp/*

# App code + assets.
COPY src/ src/
COPY web/ web/
COPY personalities/ personalities/
COPY grugthink.py pyproject.toml ./
COPY docker/init-config.sh /usr/local/bin/init-config.sh
RUN chmod +x /usr/local/bin/init-config.sh \
    && mkdir -p /data \
    && useradd -u 10001 -m grug \
    && chown -R grug:grug /app /data

USER 10001

ENV PYTHONPATH=/app/src \
    GRUGBOT_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8080/health || exit 1

# Multi-bot manager + dashboard on 8080. init-config seeds a default config
# into the (persistent) /data volume on first start.
CMD ["/bin/bash", "-c", "/usr/local/bin/init-config.sh && exec python grugthink.py multi-bot --api-port 8080"]
