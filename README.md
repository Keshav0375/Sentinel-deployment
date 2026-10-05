# Sentinel-deployment
This repo serves as dummy real app deployment for sentinel incidental agentic responder

## The app: `sentinel-watchtower`

A deliberately minimal FastAPI service — the deploy pipeline is the product, this is
its target. It does not talk to Datadog; the GitHub Actions pipeline does.

| Route | Response |
|-------|----------|
| `GET /` | `{"message": "ok", "service": "sentinel-watchtower"}` |
| `GET /health` | `{"status": "ok", "uptime_seconds": N}` |
| `GET /version` | `{"version": "<APP_VERSION>", "service": "sentinel-watchtower"}` |

On boot it prints one structured JSON line (`"message": "app.startup"`) carrying
`app_version`, `dd.service`, `dd.env` and `dd.version`.

### Configuration

Read by `app/config.py` from the environment or a local `.env`
(`cp .env.example .env`):

| Variable | Default |
|----------|---------|
| `APP_VERSION` | `local-dev` |
| `DD_SERVICE` | `sentinel-watchtower` |
| `DD_ENV` | `dev` |
| `PORT` | `8000` |

### Run locally

Python 3.12.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload
curl localhost:8000/ localhost:8000/health localhost:8000/version
```

Azure App Service runs the same app with:

```bash
gunicorn --bind=0.0.0.0 --timeout 600 -k uvicorn.workers.UvicornWorker app.main:app
```

`requirements.txt` is what gets deployed; `requirements-dev.txt` adds the test and
lint tools and is never deployed.
