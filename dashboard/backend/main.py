"""FastAPI entrypoint for the FedShieldV2 live demo dashboard backend.

Run with: PYTHONPATH=. uvicorn dashboard.backend.main:app --reload --port 8000
(from the fedshieldv2 repo root, inside its .venv).
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import agent_flow, live_plots, tab1, tab2, tab3, tab4

app = FastAPI(title="FedShieldV2 Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tab1.router)
app.include_router(tab1.ws_router)
app.include_router(tab2.router)
app.include_router(tab3.router)
app.include_router(tab4.router)
app.include_router(agent_flow.ws_router)
app.include_router(live_plots.router)

live_plots.LIVE_PLOTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/plots_live", StaticFiles(directory=str(live_plots.LIVE_PLOTS_DIR)), name="plots_live")
app.mount("/fl_evidence", StaticFiles(directory=str(tab4.EVIDENCE_DIR)), name="fl_evidence")


@app.get("/api/health")
async def health():
    return {"status": "ok"}
