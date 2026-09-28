# SmartAttend

SmartAttend is a QR-based attendance app with separate student and faculty workflows. The FastAPI backend serves the static frontend and stores data in SQLite.

## Run locally

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
Copy-Item .env.example .env
python -m uvicorn backend.main:app --reload
```

Open <http://localhost:8000>. The API health check is at <http://localhost:8000/health>.

For local development the fallback signing keys are available, but set the values in `.env` before sharing the app or deploying it.

## Deployment

Use a persistent disk for `SMARTATTEND_DB_PATH`; SQLite data stored on an ephemeral filesystem will be lost on restart. Set `SMARTATTEND_ENV=production`, strong unique values for both signing secrets, and the public frontend origin in `SMARTATTEND_CORS_ORIGINS`.

Start the service with:

```bash
python -m uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}
```

Python 3.11 through 3.14 are supported. The current dependency pins include
prebuilt Python 3.14 wheels, so installation does not require Rust or Cargo.

The repository includes `render.yaml` as a starting point for a Render deployment. Camera scanning requires HTTPS in production (localhost is the exception).

## Checks

```powershell
python -m compileall backend
python -m unittest discover -s tests -v
```
