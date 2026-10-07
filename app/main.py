"""sentinel-watchtower — the minimal FastAPI target the deploy pipeline ships.

The app never talks to Datadog; the GitHub Actions pipeline does. It only emits one
structured startup line and reports which version is live.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI

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
async def root() -> dict[str, str]:
    await asyncio.sleep(90)
    return {"message": "ok", "service": settings.dd_service}


@app.get("/health")
def health() -> dict[str, str | int]:
    return {"status": "ok", "uptime_seconds": int(time.monotonic() - _started_at)}


@app.get("/version")
def version() -> dict[str, str]:
    return {"version": settings.app_version, "service": settings.dd_service}
