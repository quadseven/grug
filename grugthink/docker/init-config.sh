#!/bin/bash

# Initialize configuration files if they don't exist
# This prevents Docker from creating them as directories

echo "Initializing GrugThink configuration files..."

# Create grugthink_config.yaml if it doesn't exist or is empty
if [ ! -s /data/grugthink_config.yaml ]; then
    echo "Creating default grugthink_config.yaml..."
    cat > /data/grugthink_config.yaml << 'EOF'
# GrugThink Multi-Bot Configuration
api:
  port: 8080
  cors_origins: ["*"]

global:
  log_level: INFO
  data_dir: /data

discord_tokens: []

api_keys:
  gemini: {}
  google_search: {}
  ollama: {}

templates:
  pure_grug:
    name: "Pure Grug Bot"
    description: "Caveman personality only"
    force_personality: "grug"
    load_embedder: true
  pure_big_rob:
    name: "Pure Big Rob Bot"
    description: "British working class personality only"
    force_personality: "big_rob"
    load_embedder: true
  evolution_bot:
    name: "Evolution Bot"
    description: "Adaptive personality that evolves"
    force_personality: null
    load_embedder: true
EOF
fi

# bot_configs.json is deprecated - all configuration is now in grugthink_config.yaml

# Ensure config files have proper permissions (only if we have permission)
if [ -w /data/grugthink_config.yaml ]; then
    chmod 644 /data/grugthink_config.yaml 2>/dev/null || true
fi

echo "Configuration files initialized successfully."
