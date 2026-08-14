"""Every Redis key name used anywhere in the system, in one place.

Redis here is the shared whiteboard + event bus (see CLAUDE.md's isolation
rule: Redis is intentionally centralized, unlike raw per-branch data).
"""


def score_key(branch_id: str, token_id: str, txn_id: str) -> str:
    """Per-transaction structuring score written by a branch's local model."""
    return f"score:{branch_id}:{token_id}:{txn_id}"


def evidence_key(token_id: str) -> str:
    """Money-Trail Agent's evidence path for a flagged token."""
    return f"evidence:{token_id}"


def verdict_key(token_id: str) -> str:
    """Adversarial verification outcome for a flagged token."""
    return f"verdicts:{token_id}"


def report_key(token_id: str) -> str:
    """Drafted investigation report for a flagged token."""
    return f"reports:{token_id}"


def labels_key(branch_id: str) -> str:
    """Pending retrain buffer of agent-verified labels for a branch."""
    return f"labels:{branch_id}"


# --- Fixed (non-parametrized) keys ---

FLAGGED_ACCOUNTS = "flagged_accounts"  # set of token_ids currently flagged
FRAUD_EVENTS_CHANNEL = "fraud_events"  # pub/sub channel - the trigger, not polling
FL_STATUS = "fl_status"  # round #, AUC, timestamp
STRUCTURING_LOG = "structuring_log"  # list of every flagged txn + its Agent 1 summary, fraud and false-positive alike
