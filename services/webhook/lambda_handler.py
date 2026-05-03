"""Lambda entry point. Mangum wraps the FastAPI app for Lambda Function URL.

Module-scope import → FastAPI app initialized once per warm container.
Cold-start cost is paid here; subsequent invocations reuse the warm app.
"""

from __future__ import annotations

from mangum import Mangum

from main import app

handler = Mangum(app, lifespan="off")
