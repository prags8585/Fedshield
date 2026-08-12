"""The outer investigation pipeline: Structuring Agent -> Money-Trail Agent.
This is the LangGraph StateGraph orchestrator/listener.py will invoke per
fraud_events message (see CLAUDE.md's pipeline diagram).

Reframed pipeline (see CLAUDE.md's "Post-Session 6 Extension - Reframed
3-Agent Pipeline"): the Structuring Agent no longer renders any judgment
that could gate anything - it never did anything functional after the
Session 6 fix anyway (see the git history of this docstring), so its
confidence/top_signals fields were dropped entirely, not just ignored. It's
now purely a running list of flagged transactions, each with a short factual
summary - context for a human/report reader, never a decision input.
"""
from typing import Optional, TypedDict

from langgraph.graph import END, StateGraph

from agents.money_trail_agent import money_trail_agent
from agents.structuring_agent import structuring_agent
from shared.schemas import ScoreRecord


class InvestigationState(TypedDict):
    score_record: dict  # serialized ScoreRecord - the clipboard both agents read from
    structuring_summary: Optional[str]
    evidence: Optional[dict]


def _structuring_node(state: InvestigationState) -> dict:
    record = ScoreRecord(**state["score_record"])
    result = structuring_agent(record)
    return {"structuring_summary": result["summary"]}


def _money_trail_node(state: InvestigationState) -> dict:
    record = ScoreRecord(**state["score_record"])
    context = f"Structuring Agent's summary: {state['structuring_summary']}"
    evidence = money_trail_agent(record.token_id, structuring_context=context)
    return {"evidence": evidence}


def build_investigation_graph():
    graph = StateGraph(InvestigationState)
    graph.add_node("structuring_agent", _structuring_node)
    graph.add_node("money_trail_agent", _money_trail_node)
    graph.set_entry_point("structuring_agent")
    graph.add_edge("structuring_agent", "money_trail_agent")
    graph.add_edge("money_trail_agent", END)
    return graph.compile()


def run_investigation(score_record: ScoreRecord) -> InvestigationState:
    compiled = build_investigation_graph()
    initial_state: InvestigationState = {
        "score_record": score_record.model_dump(),
        "structuring_summary": None,
        "evidence": None,
    }
    return compiled.invoke(initial_state)


if __name__ == "__main__":
    import json
    import sys

    import redis

    from shared.config import REDIS_URL

    token = sys.argv[1] if len(sys.argv) > 1 else None
    if not token:
        raise SystemExit("Usage: python -m agents.state_graph <token_id>  (must have a real score:* key in Redis)")

    r = redis.from_url(REDIS_URL)
    keys = r.keys(f"score:*:{token}:*")
    if not keys:
        raise SystemExit(f"No score record found in Redis for token {token}")
    record = ScoreRecord.model_validate_json(r.get(keys[0]))

    final_state = run_investigation(record)
    print(json.dumps(final_state, indent=2, default=str))
