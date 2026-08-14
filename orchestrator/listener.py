"""Always-on listener: subscribes to Redis's fraud_events pub/sub channel and
invokes the investigation StateGraph the instant a branch flags a
transaction - zero manual triggering, no polling (blocks on pubsub.listen()
until something arrives). See CLAUDE.md's pipeline diagram.
"""
import json
from datetime import datetime, timezone

import redis

from agents.state_graph import run_investigation
from shared.config import REDIS_URL
from shared.redis_keys import FRAUD_EVENTS_CHANNEL, STRUCTURING_LOG, evidence_key, score_key
from shared.schemas import ScoreRecord


def _fetch_score_record(r, event: dict) -> ScoreRecord:
    key = score_key(event["branch_id"], event["token_id"], event["txn_id"])
    raw = r.get(key)
    if raw is None:
        raise LookupError(f"no score record found at {key}")
    return ScoreRecord.model_validate_json(raw)


def run():
    r = redis.from_url(REDIS_URL)
    pubsub = r.pubsub()
    pubsub.subscribe(FRAUD_EVENTS_CHANNEL)
    print(f"[listener] subscribed to '{FRAUD_EVENTS_CHANNEL}', waiting for flagged transactions...")

    for message in pubsub.listen():
        if message["type"] != "message":
            continue  # the first message on a fresh subscribe is a "subscribe" confirmation, not data

        try:
            event = json.loads(message["data"])
        except json.JSONDecodeError:
            print(f"[listener] skipping malformed message: {message['data']!r}")
            continue

        token_id = event["token_id"]
        # Idempotency guard: the same account can flag multiple transactions
        # in quick succession (that's the point of the velocity signal) - do
        # not re-run a full investigation for a token already being/been
        # chased. See CLAUDE.md's "Session 6 Update".
        if r.exists(evidence_key(token_id)):
            print(f"[listener] token={token_id} already has evidence - skipping duplicate flag")
            continue

        print(
            f"[listener] token={token_id} branch={event['branch_id']} "
            f"score={event['score']:.3f} -> investigating"
        )
        try:
            record = _fetch_score_record(r, event)
            final_state = run_investigation(record)
        except Exception as exc:
            print(f"[listener] investigation failed for token={token_id}: {exc}")
            continue

        # Agent 1's whole job: every flagged transaction - fraud and false
        # positive alike - goes on the running list, with its short factual
        # summary. This is a plain append, not a decision; nothing here gates
        # whether the Money-Trail Agent already ran above. See CLAUDE.md's
        # "Post-Session 6 Extension - Reframed 3-Agent Pipeline".
        r.rpush(
            STRUCTURING_LOG,
            json.dumps(
                {
                    "token_id": token_id,
                    "txn_id": event["txn_id"],
                    "branch_id": event["branch_id"],
                    "score": event["score"],
                    "summary": final_state["structuring_summary"],
                    "flagged_at": datetime.now(timezone.utc).isoformat(),
                },
                default=str,
            ),
        )

        # The Structuring Agent no longer gates the investigation (see
        # CLAUDE.md's "Session 6 Update"), so evidence is always populated.
        evidence = final_state["evidence"]

        # money_trail_agent.py's own evidence:{token_id} write doesn't include
        # the Structuring Agent's output (state_graph.py keeps it as separate
        # top-level state, never passed down as anything but a formatted
        # context string) -- merge it in here and re-persist so a dashboard
        # reading evidence:{token_id} can show the real summary text, not
        # just what this file already prints to the console.
        evidence["structuring_summary"] = final_state.get("structuring_summary")
        evidence["investigated_at"] = datetime.now(timezone.utc).isoformat()
        r.set(evidence_key(token_id), json.dumps(evidence, default=str))

        print(f"[listener] token={token_id} Money-Trail Agent concluded: {evidence['stop_reason']}")
        if evidence["stop_reason"] == "convergence_found":
            verdict = evidence["verdict"]
            print(
                f"[listener] token={token_id} Verdict Agent: {verdict['verdict']} "
                f"(confidence={verdict['confidence']:.2f}) - {evidence['labels_written']} label(s) written"
            )
            corrected = evidence.get("corrected_tokens") or []
            if corrected:
                # Makes visible in the log what was otherwise a silent fix: some of these
                # siblings were investigated earlier (likely before all their real siblings
                # were flagged yet) and got stuck on a wrong answer - see CLAUDE.md's
                # "Session 6 Update" on the timing race this corrects.
                print(
                    f"[listener] retroactively corrected {len(corrected)} earlier token(s) in "
                    f"this group to convergence_found: {', '.join(corrected)}"
                )


if __name__ == "__main__":
    run()
