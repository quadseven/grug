# GrugThink App Image - Fast-changing application code
FROM ghcr.io/quadseven/grugthink:base

WORKDIR /app

# Install remaining lightweight dependencies only (ML is in base)
COPY requirements-app.txt ./
RUN pip install --no-cache-dir --no-compile --disable-pip-version-check \
    --prefer-binary -r requirements-app.txt \
    && rm -rf /tmp/* /var/tmp/*

# Copy source code and web assets
COPY src/ src/
COPY web/ web/
COPY grugthink.py .
COPY pyproject.toml ./
COPY docker/personalities/ personalities/
COPY docker/init-config.sh /usr/local/bin/init-config.sh

# Create directories for data
RUN mkdir -p /data

# Set environment variables
ENV PYTHONPATH=/app/src
ENV GRUGBOT_DATA_DIR=/data
ENV LOG_LEVEL=INFO
ENV PYTHONHASHSEED=random
# Ensure config goes to persisted /data by default
ENV GRUGTHINK_CONFIG_PATH=/data
# Persist model caches
ENV HF_HOME=/data/hf
ENV TRANSFORMERS_CACHE=/data/hf

# Expose web dashboard port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=2 \
    CMD curl -f http://localhost:8080/health || exit 1

# Create non-root user and make init script executable
RUN useradd -m -u 1000 grugthink && \
    chown -R grugthink:grugthink /app /data && \
    chmod +x /usr/local/bin/init-config.sh
USER grugthink

# Run multi-bot container
CMD ["/bin/bash", "-c", "/usr/local/bin/init-config.sh && python grugthink.py multi-bot --api-port 8080"]
