"""sentinel-watchtower — the minimal FastAPI target the deploy pipeline ships.

The app never talks to Datadog; the GitHub Actions pipeline does. It only emits one
structured startup line and reports which version is live.
"""

import json
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Response

from app.config import AppConfig

settings = AppConfig()
_started_at = time.monotonic()


def startup_record(config: AppConfig) -> dict[str, str]:
    """The `app.startup` log line, keyed exactly as deployment §2.2."""
    now = datetime.now(UTC).isoformat(timespec="milliseconds")
    return {
        "timestamp": now.replace("+00:00", "Z"),
        "level": "info",
        "message": "app.startup",
        "app_version": config.app_version,
        "dd.service": config.dd_service,
        "dd.env": config.dd_env,
        "dd.version": config.app_version,
    }


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    print(json.dumps(startup_record(settings)), flush=True)
    yield


app = FastAPI(title="sentinel-watchtower", lifespan=lifespan)


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "ok", "service": settings.dd_service}


@app.get("/health")
def health(response: Response) -> dict[str, str | int]:
    uptime = int(time.monotonic() - _started_at)
    if time.time() - os.path.getmtime(__file__) > 600:
        response.status_code = 503
        return {"status": "degraded", "uptime_seconds": uptime}
    return {"status": "ok", "uptime_seconds": uptime}


@app.get("/version")
def version() -> dict[str, str]:
    return {"version": settings.app_version, "service": settings.dd_service}
