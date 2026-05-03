"""Lambda entry. datadog-lambda wraps Mangum which wraps FastAPI."""

from __future__ import annotations

from mangum import Mangum

from main import app

handler = Mangum(app, lifespan="off")
