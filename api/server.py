"""FastAPI app: serves the PWA and the report JSON it renders.

    uvicorn api.server:app --host 0.0.0.0 --port 8000

For iPhone install you need HTTPS (or localhost). Put this behind a reverse
proxy with a certificate, open it in Safari, then Share -> Add to Home Screen.
Web Push only works from the installed icon, not from a Safari tab.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stockbot.config import load_config, resolve_path  # noqa: E402
from stockbot.logging_setup import setup_logging  # noqa: E402
from stockbot.storage.db import Database  # noqa: E402

setup_logging()
cfg = load_config()
WEB_DIR = ROOT / "web"

app = FastAPI(title="Stock Analysis Bot", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # personal single-user tool on a private host
    allow_methods=["*"],
    allow_headers=["*"],
)


def _db() -> Database:
    return Database(resolve_path(str(cfg.get("output.db_path", "data/stockbot.db"))))


# --- API ------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "phase": cfg.phase}


@app.get("/api/report/latest")
def latest_report() -> JSONResponse:
    db = _db()
    try:
        report = db.latest_report()
    finally:
        db.close()
    if report is None:
        raise HTTPException(404, "no run stored yet — run `python run_daily.py` first")
    return JSONResponse(report)


@app.get("/api/report/{run_date}")
def report_by_date(run_date: str) -> JSONResponse:
    db = _db()
    try:
        report = db.report_for_date(run_date)
    finally:
        db.close()
    if report is None:
        raise HTTPException(404, f"no run stored for {run_date}")
    return JSONResponse(report)


@app.get("/api/dates")
def dates(limit: int = 60) -> dict:
    db = _db()
    try:
        return {"dates": db.run_dates(limit)}
    finally:
        db.close()


@app.get("/api/ticker/{ticker}/history")
def ticker_history(ticker: str, limit: int = 90) -> dict:
    db = _db()
    try:
        return {"ticker": ticker.upper(), "history": db.ticker_history(ticker, limit)}
    finally:
        db.close()


@app.get("/api/push/public-key")
def vapid_public_key() -> dict:
    return {"key": cfg.secrets.vapid_public_key or ""}


@app.post("/api/push/subscribe")
def subscribe(subscription: dict = Body(...)) -> dict:
    db = _db()
    try:
        db.save_subscription(subscription)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        db.close()
    return {"ok": True}


@app.post("/api/push/unsubscribe")
def unsubscribe(payload: dict = Body(...)) -> dict:
    endpoint = payload.get("endpoint")
    if not endpoint:
        raise HTTPException(400, "endpoint required")
    db = _db()
    try:
        db.delete_subscription(endpoint)
    finally:
        db.close()
    return {"ok": True}


# --- PWA shell ------------------------------------------------------------
# The service worker and manifest must be served from the root so the worker's
# scope covers the whole app.


@app.get("/sw.js")
def service_worker() -> FileResponse:
    return FileResponse(WEB_DIR / "sw.js", media_type="application/javascript")


@app.get("/manifest.json")
def manifest() -> FileResponse:
    return FileResponse(WEB_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
