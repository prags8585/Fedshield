"""Tab 2 (Agentic / 216 case).

Thin wrapper around agent_flow.py -- see that module for the shared
start/reset/investigations logic (docker health check, listener startup,
branch-log tailing, Redis flush, evidence scan). This file only supplies
the 216-case producer command.
"""
from fastapi import APIRouter

from . import agent_flow

router = APIRouter(prefix="/api/tab2")

PRODUCER_CMD = (
    "KAFKA_BROKER=localhost:29092 PYTHONPATH=. python3 branch_node/producer.py "
    "--files data/background.json data/layering_hops4_events.json "
    "--background-window-seconds 20\n"
).encode()


@router.post("/start")
async def start():
    return await agent_flow.start_agents(PRODUCER_CMD, "tab2")


@router.post("/reset")
async def reset():
    return await agent_flow.reset_agents("tab2")


@router.get("/investigations")
async def investigations():
    return await agent_flow.get_investigations()


@router.get("/structuring-log")
async def structuring_log():
    return await agent_flow.get_structuring_log()


@router.get("/rings")
async def rings():
    return await agent_flow.get_rings()


@router.get("/report")
async def report_status():
    return await agent_flow.get_report_status()


@router.get("/report/download")
async def report_download():
    return await agent_flow.download_report()
