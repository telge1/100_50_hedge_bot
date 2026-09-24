#!/usr/bin/env python3
"""Local visual QA server for symbol onboarding (NOT for production).

Run on a free port, never 3000:
  SYMBOL_ONBOARDING_QA_ROLE=admin .venv/bin/python dashboard/symbol_onboarding/qa_server.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
import uvicorn

DASHBOARD = Path(__file__).resolve().parent.parent
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

from symbol_onboarding.api import build_router  # noqa: E402

ROLE = os.environ.get("SYMBOL_ONBOARDING_QA_ROLE", "admin")
PORT = int(os.environ.get("SYMBOL_ONBOARDING_QA_PORT", "3099"))

jinja_env = Environment(loader=FileSystemLoader(str(DASHBOARD / "templates")))


def render_template(name: str, context: dict) -> str:
    return jinja_env.get_template(name).render(**context)


def require_auth(request: Request):
    if ROLE == "none":
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"username": f"qa-{ROLE}", "role": ROLE}


app = FastAPI(title="symbol-onboarding-qa")
app.mount("/static", StaticFiles(directory=str(DASHBOARD / "static")), name="static")
app.include_router(build_router(require_auth=require_auth, render_template=render_template))


@app.get("/stoch-signale", response_class=HTMLResponse)
async def stoch_stub(request: Request):
    user = require_auth(request)
    # Minimal stub using real header snippet via dedicated tiny template render of stoch page
    # would pull live data — use lightweight HTML that includes the admin button contract.
    btn = ""
    if user.get("role") == "admin":
        btn = '<a class="stoch-btn so-admin-btn" href="/datenverwaltung/symbole">Symbol hinzufügen</a>'
    html = f"""<!DOCTYPE html><html><head>
    <link rel="stylesheet" href="/static/css/style.css">
    <link rel="stylesheet" href="/static/css/stoch.css">
    <title>QA Stoch</title></head><body>
    <div class="stoch-page-header"><div><h1>Stoch-Signale QA</h1></div>
    <div class="stoch-page-header-actions">{btn}</div></div>
    </body></html>"""
    return HTMLResponse(html)


if __name__ == "__main__":
    if PORT == 3000:
        raise SystemExit("Refusing to bind production port 3000")
    print(f"QA server role={ROLE} port={PORT}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
