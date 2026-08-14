"""Tab 3 (Agentic / 1500 case).

Thin wrapper around agent_flow.py -- identical flow to Tab 2, just a
different producer command: the bigger 19-ring multi-ring scenario's files
and timing instead of the single-ring layering scenario's. This tab's own
master/producer/branch-log terminals are independent of Tab 2's (separate
tab3-* PTY sessions in agent_flow.agent_registry) -- Start/Reset here never
touch Tab 2's terminals or vice versa. The investigation feed itself
(evidence/verdicts/flagged_accounts/structuring_log in Redis) is still
shared/unscoped between the two tabs, same as before.

This replaces the old 500-case (data/scenario_500), which had a known,
still-open bug where every consecutive ring pair shared a mule account and
Ring2 always failed to resolve (0/13) as a result -- see CLAUDE.md's
"Session 6 Update". data/scenario_1500 fixes that by construction: 19
independent rings, only 1 shared mule account total (between ring1/ring2),
17 rings fully isolated. See SCENARIO_1500_SUMMARY.md for full detail.

Real expectations to set for this case: 1,500 transactions (1,196
background + 304 fraud), ~451 flagged accounts (246 real fraud + ~205 false
positives) instead of ~59, and a full investigation run takes MANY hours on
qwen2.5:7b (measured at ~20h for all 451) -- this is not the dashboard
hanging. Also, per SCENARIO_1500_SUMMARY.md Finding 1, expect the Verdict
Agent to occasionally clear a structurally-real convergence as NOT_GUILTY
(3/18 in the last full run) -- a genuine LLM-judgment gap, not a tracing
bug. Do NOT run scripts/test_agent_convergence.py or
scripts/test_multi_ring_convergence.py while this tab's investigation is
live -- both wipe flagged_accounts/Neo4j as part of their own setup and will
silently degrade every investigation after that point to
insufficient_evidence/cycle (Finding 2).
"""
from fastapi import APIRouter

from . import agent_flow

router = APIRouter(prefix="/api/tab3")

PRODUCER_CMD = (
    "KAFKA_BROKER=localhost:29092 PYTHONPATH=. python3 branch_node/producer.py "
    "--files data/scenario_1500/background.json data/scenario_1500/multi_ring_events.json "
    "--background-window-seconds 135\n"
).encode()


@router.post("/start")
async def start():
    return await agent_flow.start_agents(PRODUCER_CMD, "tab3")


@router.post("/reset")
async def reset():
    return await agent_flow.reset_agents("tab3")


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
