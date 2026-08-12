"""Money-Trail Agent - traces whether an escalated deposit is part of a
larger cross-branch convergence.

Redesigned to close a real recall gap found comparing agentic tracing
against the manual, ground-truth-assisted approach: `check_convergence` is a
deterministic, zero-argument tool - it always gives the mathematically
correct answer for the current flagged set, no LLM judgment required to
invoke it. The original design left calling it up to the LLM's own
discretion inside a tool-calling loop, and real runs showed the LLM
sometimes never called it (or reasoned past its result) before concluding
"insufficient evidence" - silently costing recall the manual approach never
loses, since it has no such discretion at all.

Fix: run `check_convergence` automatically, in code, before the LLM ever
gets a turn. The LLM's job shrinks to one of two things: narrate a
known-correct "convergence found" answer (a single plain call, no tools -
nothing left to get wrong that affects the verdict), or explore a genuine
"no convergence yet" case before concluding insufficient evidence/cycle (the
only place LLM discretion remains, and it can no longer cost recall on real
fraud, since that's already resolved deterministically before this path
ever runs). See CLAUDE.md's "Session 6 Update" for the full reasoning.
"""
import json

import redis
from openai import OpenAI

from graph.connection import get_driver
from graph.queries import (
    DEFAULT_MIN_BRANCHES_FOR_CONVERGENCE,
    DEFAULT_MIN_PRESERVATION_RATIO_TO_SINK,
    DEFAULT_MIN_SOURCES_FOR_CONVERGENCE,
    check_convergence,
    get_incoming_txns,
    get_outgoing_txns,
)
from agents.label_generator import generate_labels
from agents.report_agent import report_agent
from agents.verdict_agent import verdict_agent
from shared.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, REDIS_URL
from shared.openai_utils import chat_completion_with_retry
from shared.redis_keys import FLAGGED_ACCOUNTS, evidence_key, verdict_key

# Runaway-loop guardrail on the LLM's own dead-end-exploration loop - this no
# longer gates the primary verdict (see module docstring), so it's a pure
# cost/safety backstop, not expected to be hit in normal operation.
MAX_LOOP_ITERATIONS = 25

# Only used for the "no convergence found yet - explore before giving up" path;
# check_convergence is no longer an LLM-callable tool, see module docstring.
_EXPLORATION_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_outgoing_txns",
            "description": "List transactions leaving a given account token, time-ordered.",
            "parameters": {
                "type": "object",
                "properties": {"token_id": {"type": "string"}},
                "required": ["token_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_incoming_txns",
            "description": "List transactions arriving at a given account token, time-ordered.",
            "parameters": {
                "type": "object",
                "properties": {"token_id": {"type": "string"}},
                "required": ["token_id"],
                "additionalProperties": False,
            },
        },
    },
]

_DEAD_END_SYSTEM_PROMPT = """You are a bank's money-trail investigator. A flagged deposit has \
been escalated to you, and an automatic check has already confirmed it does NOT currently \
converge with any other flagged deposit. Before concluding there is insufficient evidence, \
briefly check the token's own outgoing/incoming transactions using your tools - do not accept \
the "no convergence" result alone without looking at the token itself.

When you are done, respond with a final JSON object and no further tool calls, in this exact shape:
{"conclusion": "insufficient_evidence" or "cycle", "summary": "plain English summary"}"""

_SUMMARY_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "convergence_summary",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    },
}

_SUMMARY_SYSTEM_PROMPT = """You are a bank's money-trail investigator. An automatic check has \
already confirmed a real cross-branch convergence for the token you were asked to investigate - \
independent deposits converge on the same account, matching the time+amount chaining rule. Write \
a brief, plain-English summary of this finding for a case file. Do not question or re-derive the \
finding - it is already confirmed; just narrate it clearly from the evidence given."""


def _gather_live_sources(driver, r) -> list:
    """Builds check_convergence's `sources` list purely from the graph's real
    CASH-deposit edges for every currently flagged token - no ground truth,
    no ring/group hints. See CLAUDE.md's "Session 6 Update" for why this
    resolves the ring-grouping gap noted in the Session 4 Extension.
    """
    sources = []
    for raw_token in r.smembers(FLAGGED_ACCOUNTS):
        token = raw_token.decode() if isinstance(raw_token, bytes) else raw_token
        cash_edges = [e for e in get_incoming_txns(driver, token) if e["from_token"] == "CASH"]
        if not cash_edges:
            continue  # this flagged token wasn't itself a deposit (e.g. a flagged mid-chain hop)
        edge = cash_edges[0]
        sources.append(
            {"token_id": token, "amount": edge["amount"], "ts": edge["ts"], "branch_id": edge["branch_id"]}
        )
    return sources


def _find_convergence_group_for_token(driver, r, token_id: str) -> dict:
    """check_convergence only ever returns its single BEST-matching
    convergence per call (documented in CLAUDE.md's "Session 4 Extension") -
    fine when there's only one ring, but with multiple simultaneous
    independent rings sharing a bridge account (e.g. scenario_500's 3 rings),
    a source's traced path can validly continue *through* the shared account
    into a neighboring ring's consolidation - a real, valid-per-the-rule
    edge, not a bug in the traversal. Since that "absorbing" group ends up
    bigger than either true ring's own group, `check_convergence`'s old
    "return only the biggest" behavior always let it win, starving the
    smaller true rings entirely.

    Fix: ask for every group that passes the gates at once
    (`return_all_candidates=True`), then resolve ambiguous (multi-group)
    sources by claiming them SMALLEST-group-first. The two true rings each
    have exactly 3 real sources; the absorbing group has those 3 plus
    whatever leaked in - so letting the smaller groups claim their own
    (including ambiguous) members first, before the bigger group gets
    whatever is left, correctly shrinks it back down to just its own real
    members. See CLAUDE.md's "Session 6 Update" for the full story.
    """
    sources = _gather_live_sources(driver, r)
    if len(sources) < 2:
        return {"has_convergence": False, "reason": "fewer than 2 currently flagged deposit sources"}

    raw = check_convergence(driver, sources=sources, return_all_candidates=True)
    candidates = raw.get("candidates", [])
    if not candidates:
        return {"has_convergence": False, "reason": "no confirmed convergence group includes this token"}

    source_amount_by_token = {s["token_id"]: s["amount"] for s in sources}
    source_branch_by_token = {s["token_id"]: s["branch_id"] for s in sources}

    claimed_sources = set()
    resolved_groups = []
    for cand in sorted(candidates, key=lambda c: c["num_sources"]):
        surviving_paths = [p for p in cand["paths"] if p[0] not in claimed_sources]
        if len(surviving_paths) < DEFAULT_MIN_SOURCES_FOR_CONVERGENCE:
            continue
        branches_seen = {source_branch_by_token[p[0]] for p in surviving_paths}
        if len(branches_seen) < DEFAULT_MIN_BRANCHES_FOR_CONVERGENCE:
            continue

        total_source_amount = sum(source_amount_by_token[p[0]] for p in surviving_paths)
        total_reached_amount = 0.0
        for path in surviving_paths:
            if len(path) < 2:
                total_reached_amount += source_amount_by_token[path[0]]
                continue
            edges = get_outgoing_txns(driver, path[-2])
            match = next((e for e in edges if e["to_token"] == path[-1]), None)
            total_reached_amount += match["amount"] if match else 0.0
        preservation_ratio = (total_reached_amount / total_source_amount) if total_source_amount else 0.0
        if preservation_ratio < DEFAULT_MIN_PRESERVATION_RATIO_TO_SINK:
            continue

        claimed_sources |= {p[0] for p in surviving_paths}
        resolved_groups.append(
            {
                "has_convergence": True,
                "convergence_account": cand["convergence_account"],
                "num_sources": len(surviving_paths),
                "num_branches": len(branches_seen),
                "paths": surviving_paths,
                "amount_preservation_ratio": round(preservation_ratio, 4),
                "cycle_detected": raw.get("cycle_detected", False),
            }
        )

    for group in resolved_groups:
        if token_id in {tok for path in group["paths"] for tok in path}:
            return group
    return {"has_convergence": False, "reason": "no confirmed convergence group includes this token"}


def _build_evidence_hops(driver, convergence_result: dict) -> list:
    """Deterministically walks every winning path's REAL edges via
    get_outgoing_txns, in plain code - never asks the LLM to retype exact
    transaction facts (txn_id/amount/ts/channel) from memory. A real test
    showed an 8B local model fabricating plausible-looking but fake hop data
    (wrong amounts, impossible future dates) once the loop got a few turns
    deep - see CLAUDE.md's "Session 6 Update". check_convergence's `paths`
    field already gives the real winning token sequences; we just need to
    look up the actual edge between each consecutive pair.
    """
    hops = []
    seen_txn_ids = set()
    for path in convergence_result.get("paths", []):
        for from_token, to_token in zip(path, path[1:]):
            edges = get_outgoing_txns(driver, from_token)
            match = next((e for e in edges if e["to_token"] == to_token), None)
            if match and match["txn_id"] not in seen_txn_ids:
                seen_txn_ids.add(match["txn_id"])
                hops.append(
                    {
                        "from_token": from_token,
                        "to_token": to_token,
                        "txn_id": match["txn_id"],
                        "amount": match["amount"],
                        "ts": match["ts"],
                        "channel": match["channel"],
                    }
                )
    return hops


def _summarize_convergence(evidence_path: list, convergence_account: str) -> str:
    """One plain LLM call, no tools - the verdict is already locked in by the
    time this runs, so nothing here can cost recall, only wording quality.
    """
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    try:
        response = chat_completion_with_retry(
            client,
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"convergence_account": convergence_account, "hops": evidence_path}, default=str
                    ),
                },
            ],
            response_format=_SUMMARY_RESPONSE_SCHEMA,
        )
        return json.loads(response.choices[0].message.content).get("summary", "")
    except Exception:
        return f"{len(evidence_path)} hop(s) converge on account {convergence_account}."


def _run_exploration_tool(name: str, args: dict, driver) -> dict:
    arg_token_id = args.get("token_id")
    if not arg_token_id:
        return {"error": "missing required 'token_id' argument - retry with a valid token_id"}
    fn = get_outgoing_txns if name == "get_outgoing_txns" else get_incoming_txns
    return {"edges": fn(driver, arg_token_id)}


def _explore_dead_end(driver, token_id: str, structuring_context: str) -> dict:
    """LLM tool-loop for the genuine no-convergence-found case only - this
    discretion can no longer cost recall on real fraud, since that path is
    already resolved deterministically before this ever runs. A plain
    bounded loop (not a LangGraph graph) is enough here: the only two stop
    conditions are "LLM produced a final answer" or "hit the iteration
    ceiling", both handled directly below.
    """
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    messages = [
        {"role": "system", "content": _DEAD_END_SYSTEM_PROMPT},
        {"role": "user", "content": f"Investigate token {token_id}. {structuring_context}"},
    ]
    for _ in range(MAX_LOOP_ITERATIONS):
        response = chat_completion_with_retry(
            client, model=LLM_MODEL, messages=messages, tools=_EXPLORATION_TOOLS_SCHEMA
        )
        msg = response.choices[0].message
        if not msg.tool_calls:
            try:
                final = json.loads(msg.content or "{}")
            except json.JSONDecodeError:
                final = {}
            conclusion = final.get("conclusion")
            if conclusion not in ("insufficient_evidence", "cycle"):
                conclusion = "insufficient_evidence"
            return {"stop_reason": conclusion, "summary": final.get("summary") or msg.content or ""}

        messages.append(
            {"role": "assistant", "content": msg.content, "tool_calls": [tc.model_dump() for tc in msg.tool_calls]}
        )
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
                result = _run_exploration_tool(tc.function.name, args, driver)
            except Exception as exc:
                # A malformed tool call should not crash the whole investigation - feed the
                # error back as a tool result so the LLM can retry/adapt. See CLAUDE.md's
                # "Session 6 Update".
                result = {"error": f"tool call failed: {exc}"}
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, default=str)})

    return {
        "stop_reason": "safety_ceiling",
        "summary": f"Stopped after {MAX_LOOP_ITERATIONS} tool-calling iterations without a final answer.",
    }


def money_trail_agent(token_id: str, structuring_context: str = "") -> dict:
    """Runs the investigation to completion and returns an Evidence-shaped
    dict: {token_id, path, convergence_node, stop_reason, summary}. Also
    writes the result to Redis's evidence:{token_id} key.

    `check_convergence` runs automatically, in code, before any LLM call -
    see module docstring for why this is no longer left to LLM discretion.
    """
    driver = get_driver()
    r = redis.from_url(REDIS_URL)
    try:
        convergence_result = _find_convergence_group_for_token(driver, r, token_id)

        if convergence_result.get("has_convergence"):
            path = _build_evidence_hops(driver, convergence_result)
            source_tokens = [grp_path[0] for grp_path in convergence_result.get("paths", [])]
            all_group_tokens = {tok for grp_path in convergence_result.get("paths", []) for tok in grp_path}
            ring_id = f"ring_{convergence_result.get('convergence_account', 'unknown')[:8]}"
            evidence = {
                "token_id": token_id,
                "path": path,
                "convergence_node": convergence_result.get("convergence_account"),
                "source_tokens": source_tokens,
                "all_tokens": sorted(all_group_tokens),
                "ring_id": ring_id,
                "stop_reason": "convergence_found",
                "summary": _summarize_convergence(path, convergence_result.get("convergence_account")),
            }
            # Write the SAME confirmed evidence for every other member of this group too, not
            # just the token that happened to trigger this investigation. Closes a real timing
            # race found in the 500-case: the 3 real sources in a ring flag at different times
            # (they're separate deposits), so an early source can be investigated before its
            # siblings are flagged yet, correctly finding "not enough sources" at that moment and
            # getting stuck on insufficient_evidence/cycle forever - even though the full group
            # was real and gets confirmed moments later by a sibling's own investigation. See
            # CLAUDE.md's "Session 6 Update".
            corrected_tokens = []
            for other_token in all_group_tokens - {token_id}:
                existing_raw = r.get(evidence_key(other_token))
                if existing_raw and json.loads(existing_raw).get("stop_reason") != "convergence_found":
                    # This token was investigated earlier (likely before all its real siblings
                    # were flagged yet - the timing race above) and got stuck on a wrong answer.
                    # Track it so the listener can announce the correction, not just silently
                    # overwrite it - a real run showed this is otherwise invisible in the log.
                    corrected_tokens.append(other_token)
                other_evidence = {**evidence, "token_id": other_token}
                r.set(evidence_key(other_token), json.dumps(other_evidence, default=str))
            evidence["corrected_tokens"] = corrected_tokens

            # Render a verdict for the whole confirmed group, once - the Verdict Agent judges a
            # different question than the deterministic check above ("does this real connection
            # prove deliberate laundering, or could there be an innocent explanation") - see
            # CLAUDE.md's "Session 6 Update". Written under every group member's own key, same
            # pattern as evidence above. Computed BEFORE the report below purely so the report can
            # display it - the report's own trigger is still convergence_found alone, never gated
            # on what this verdict says. If GUILTY and confident enough, the label generator (a
            # deterministic bookkeeping step, not another LLM call) writes real training labels
            # into every involved branch's FL retrain buffer. Labeling is the ONLY thing the
            # Verdict Agent's opinion still controls - it no longer gates the report.
            verdict = verdict_agent(evidence, structuring_context)
            for group_token in all_group_tokens:
                r.set(verdict_key(group_token), json.dumps({**verdict, "token_id": group_token}, default=str))
            labels_written = generate_labels(r, all_group_tokens, verdict)
            evidence["verdict"] = verdict
            evidence["labels_written"] = labels_written

            # Reporting Agent (Agent 3) fires here, off the deterministic convergence_found
            # result alone - independent of what the Verdict Agent decided above. See CLAUDE.md's
            # "Post-Session 6 Extension - Reframed 3-Agent Pipeline": a structurally confirmed
            # ring is always worth a human's look, whether or not the Verdict Agent also thinks
            # it's deliberate. The verdict is passed through purely for display in the report.
            #
            # Own try/except deliberately, separate from the outer try/finally: a real run showed
            # this can throw (a transient Neo4j/file-write hiccup) with nothing else around it to
            # catch it - that exception used to propagate all the way up through
            # run_investigation() to orchestrator/listener.py's broad handler, which just logs it
            # and moves on. The real cost wasn't losing the report alone - it also skipped the
            # r.set(evidence_key(token_id), ...) below, so the trigger token's own evidence never
            # picked up the verdict/labels_written/report_path fields already computed above, even
            # though verdicts:{token_id} (written separately, per group member, earlier) survived.
            # A failed report should never cost the rest of an already-successful investigation.
            try:
                evidence["report_path"] = report_agent(driver, r, convergence_result, ring_id, verdict)
            except Exception as exc:
                print(f"[money_trail_agent] report_agent failed for {ring_id} (token={token_id}): {exc}")
                evidence["report_path"] = None
        else:
            explored = _explore_dead_end(driver, token_id, structuring_context)
            evidence = {
                "token_id": token_id,
                "path": [],
                "convergence_node": None,
                "stop_reason": explored["stop_reason"],
                "summary": explored["summary"],
            }

        r.set(evidence_key(token_id), json.dumps(evidence, default=str))
        return evidence
    finally:
        driver.close()


if __name__ == "__main__":
    import sys

    token = sys.argv[1] if len(sys.argv) > 1 else None
    if not token:
        raise SystemExit("Usage: python -m agents.money_trail_agent <token_id>")
    result = money_trail_agent(token, structuring_context="Structuring Agent flagged this with HIGH confidence.")
    print(json.dumps(result, indent=2, default=str))
