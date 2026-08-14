"""Runs the real Structuring Agent -> Money-Trail Agent -> Verdict Agent
pipeline (agents/state_graph.py's run_investigation) against every currently
flagged account in Redis, directly - not via the pubsub listener - so a
dataset that's already been streamed through the live stack (scores +
flagged_accounts already sitting in Redis, see scenario_1500's live run) can
be investigated after the fact without re-streaming.

Mirrors orchestrator/listener.py's own idempotency guard (evidence_key
already set => skip) and its group-write awareness: money_trail_agent()
writes evidence for every member of a confirmed convergence group at once,
so investigating one source often resolves its siblings for free - this
script's per-token loop naturally benefits from that (skips already-resolved
group members it reaches later).

Usage:
    REDIS_URL=redis://localhost:6380 NEO4J_URI=bolt://localhost:7688 \\
      PYTHONPATH=. python3 scripts/run_full_agent_investigation.py
"""
import json
import sys
import time

import redis

from agents.state_graph import run_investigation
from shared.config import REDIS_URL
from shared.redis_keys import FLAGGED_ACCOUNTS, evidence_key
from shared.schemas import ScoreRecord


def run(ground_truth_path: str = None):
    r = redis.from_url(REDIS_URL)
    tokens = sorted(t.decode() if isinstance(t, bytes) else t for t in r.smembers(FLAGGED_ACCOUNTS))
    print(f"Total flagged accounts: {len(tokens)}", flush=True)

    fraud_tokens = set()
    if ground_truth_path:
        from branch_node.masking import token_for
        gt = json.load(open(ground_truth_path))
        fraud_tokens = {token_for(a) for a in gt["all_fraud_accounts"]}
        print(f"Ground truth fraud accounts: {len(fraud_tokens)}", flush=True)

    stop_reason_counts = {}
    guilty_count, not_guilty_count = 0, 0
    labels_written_total = 0
    active_investigations = 0
    skipped_already = 0
    skipped_no_score = 0
    failed = 0

    fraud_caught_via_convergence = set()

    t0 = time.time()
    for i, token in enumerate(tokens):
        if r.exists(evidence_key(token)):
            skipped_already += 1
            continue
        keys = r.keys(f"score:*:{token}:*")
        if not keys:
            skipped_no_score += 1
            continue
        record = ScoreRecord.model_validate_json(r.get(keys[0]))

        try:
            final_state = run_investigation(record)
        except Exception as exc:
            failed += 1
            print(f"[{i+1}/{len(tokens)}] token={token} FAILED: {exc}", flush=True)
            continue

        active_investigations += 1
        evidence = final_state["evidence"]
        stop_reason = evidence["stop_reason"]
        stop_reason_counts[stop_reason] = stop_reason_counts.get(stop_reason, 0) + 1

        verdict_str = ""
        if stop_reason == "convergence_found":
            verdict = evidence["verdict"]
            if verdict["verdict"] == "GUILTY":
                guilty_count += 1
            else:
                not_guilty_count += 1
            labels_written_total += evidence.get("labels_written", 0)
            verdict_str = f" verdict={verdict['verdict']}({verdict['confidence']:.2f}) labels={evidence.get('labels_written', 0)}"
            for grp_path in [evidence.get("path", [])]:
                pass
            all_group_tokens = {tok for hop in evidence.get("path", []) for tok in (hop["from_token"], hop["to_token"])}
            fraud_caught_via_convergence |= (all_group_tokens & fraud_tokens)

        elapsed = time.time() - t0
        print(
            f"[{i+1}/{len(tokens)}] active#{active_investigations} token={token} -> {stop_reason}{verdict_str}  "
            f"(elapsed {elapsed:.0f}s, avg {elapsed/max(active_investigations,1):.1f}s/investigation)",
            flush=True,
        )

    total_elapsed = time.time() - t0
    print("\n=== SUMMARY ===", flush=True)
    print(f"Total flagged accounts: {len(tokens)}", flush=True)
    print(f"Active investigations run: {active_investigations}", flush=True)
    print(f"Skipped (already had evidence via group-write): {skipped_already}", flush=True)
    print(f"Skipped (no score record found): {skipped_no_score}", flush=True)
    print(f"Failed: {failed}", flush=True)
    print(f"Stop reasons: {stop_reason_counts}", flush=True)
    print(f"GUILTY verdicts: {guilty_count}  NOT_GUILTY verdicts: {not_guilty_count}", flush=True)
    print(f"Total labels written to FL buffers: {labels_written_total}", flush=True)
    print(f"Total wall time: {total_elapsed:.0f}s ({total_elapsed/60:.1f} min)", flush=True)
    if fraud_tokens:
        print(f"Real fraud accounts confirmed via convergence_found evidence: {len(fraud_caught_via_convergence)} / {len(fraud_tokens)}", flush=True)


if __name__ == "__main__":
    gt_path = sys.argv[1] if len(sys.argv) > 1 else None
    run(gt_path)
