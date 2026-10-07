"""sentinel-watchtower — the minimal FastAPI target the deploy pipeline ships.

The app never talks to Datadog; the GitHub Actions pipeline does. It only emits one
structured startup line and reports which version is live.
"""

import json
import time
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Response

from app.config import AppConfig

settings = AppConfig()
_started_at = time.monotonic()
STATUS_FEED_URL = "http://192.0.2.1/status"


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
def root(response: Response) -> dict[str, str]:
    try:
        with urllib.request.urlopen(STATUS_FEED_URL, timeout=5):
            pass
    except OSError:
        response.status_code = 500
        return {"message": "status feed unreachable", "service": settings.dd_service}
    return {"message": "ok", "service": settings.dd_service}


@app.get("/health")
def health() -> dict[str, str | int]:
    return {"status": "ok", "uptime_seconds": int(time.monotonic() - _started_at)}


@app.get("/version")
def version() -> dict[str, str]:
    return {"version": settings.app_version, "service": settings.dd_service}
