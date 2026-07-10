# GrugThink

**Adaptable Discord Personality Engine** - the Discord home of Grug.

GrugThink is a Discord bot platform that gives each server its own evolving
personality (Grug the caveman, and others), managed through a web dashboard. It
lives here in the `grug` monorepo alongside the code-review Elder so the two
Grugs can eventually talk to each other.

## What's here

- `src/grugthink/` - core Python package: the Discord bots, API server, bot
  manager, personality engine, and storage.
- `personalities/` - character definitions (`grug.yaml`, `big_rob.yaml`, ...).
  Each is a self-contained personality: emotions, speech patterns, catchphrases.
- `web/` - the management dashboard (start/stop/configure bots, tokens, logs).
- `docker/` - container build for running the platform.
- `tests/` - unit + integration tests.
- `*.example` config - `.env.example`, `grugthink_config.yaml.example`. Copy
  these and fill in your own tokens/keys; nothing real is committed.

## Quick start

```bash
cp grugthink_config.yaml.example grugthink_config.yaml
# edit grugthink_config.yaml: Discord bot token(s), an LLM API key, and the
# Discord OAuth settings for the dashboard.
pip install -r requirements.txt
python -m grugthink            # or use the docker/ image
```

Secrets (Discord tokens, LLM/API keys, Sentry DSN) are supplied at runtime via
config/env or the dashboard - never hardcode them in source. If you fork a
personality, keep it in `personalities/`.

## Status

Imported into the monorepo as a curated, public-safe snapshot (deployment/infra
tooling and internal dev notes were intentionally left out of the import). See
the repo root for the code-review side of Grug.
