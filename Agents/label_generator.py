"""Label Generator - plain code, not an agent. Once the Verdict Agent has
already rendered a verdict, deciding whether to act on it is bookkeeping,
not judgment - see CLAUDE.md's "Session 6 Update" for why this is
deliberately not another LLM call. Writes (features, label="fraud",
source="agent_verified") into every involved branch's retrain buffer the
moment a verdict clears the confidence bar - independent of whether a human
has reviewed any report (a future Session 8 review step would never gate
this - the label already exists by the time any report is drafted).
"""
import json

from shared.config import JUDGE_CONFIDENCE_THRESHOLD
from shared.redis_keys import labels_key
from shared.schemas import Label, ScoreRecord


def generate_labels(r, group_tokens: set, verdict: dict) -> int:
    """Returns how many labels were actually written (0 if the verdict
    doesn't clear the bar, or if a group member was never itself scored by
    a branch - e.g. it's a mid-chain hop that didn't cross the flagging
    threshold on its own).
    """
    if verdict.get("verdict") != "GUILTY" or verdict.get("confidence", 0.0) < JUDGE_CONFIDENCE_THRESHOLD:
        return 0

    written = 0
    for token in group_tokens:
        keys = r.keys(f"score:*:{token}:*")
        if not keys:
            continue
        record = ScoreRecord.model_validate_json(r.get(keys[0]))
        label = Label(token_id=token, features=record.features, label="fraud", source="agent_verified")
        r.rpush(labels_key(record.branch_id), label.model_dump_json())
        written += 1
    return written
