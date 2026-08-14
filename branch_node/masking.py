"""PII stripping and the global-salt token scheme.

token_id = SHA256(account_number + GLOBAL_TOKEN_SALT)[:16] - ONE global salt,
shared system-wide (see CLAUDE.md decision #1). This is deliberately NOT the
original FedShield's branch-salted scheme: a global salt is required so the
same real account always maps to the same token no matter which branch's
consumer processes it, which is what makes cross-branch graph convergence
(Session 4) possible at all.

mask_event() strips customer_id/customer_name/account_number/routing_number
and telemetry (PII, or inert for Case 1 - see CLAUDE.md) and returns only the
anonymized shape the rest of the pipeline (feature extraction, Redis, and
later the Neo4j graph writer) is allowed to see.
"""
import hashlib

from shared.config import CASH_SINK_TOKEN, CASH_SOURCE_TOKEN, GLOBAL_TOKEN_SALT

MERCHANT_SENTINEL = "MERCHANT"
_SENTINELS = {CASH_SOURCE_TOKEN, CASH_SINK_TOKEN, MERCHANT_SENTINEL}


def token_for(account_number: str) -> str:
    """Sentinel account numbers (CASH/CASH_OUT/MERCHANT) pass through
    unhashed so they keep mapping to the fixed synthetic Neo4j source/sink
    nodes (see CLAUDE.md's Neo4j schema); real accounts get salted+hashed.
    """
    if account_number in _SENTINELS:
        return account_number
    return hashlib.sha256((account_number + GLOBAL_TOKEN_SALT).encode("utf-8")).hexdigest()[:16]


def mask_event(event: dict) -> dict:
    """Returns the anonymized view of a KafkaTxnEvent: which token is the
    "local" party for this branch's leg, which token is the counterparty,
    and whether money is leaving the local party (is_transfer_out).

    Local party is the beneficiary for an inbound cash deposit (originator
    is the CASH sentinel), and the originator for everything else - this
    covers withdrawals, purchases, and transfers uniformly since the
    origin branch's consumer is the one that ever sees a given leg
    (see CLAUDE.md decision #3).
    """
    txn = event["transaction"]
    originator = event["originator"]
    beneficiary = event["beneficiary"]

    is_deposit_in = originator["account_number"] == CASH_SOURCE_TOKEN
    local_party, counter_party = (beneficiary, originator) if is_deposit_in else (originator, beneficiary)

    return {
        "event_id": event["event_id"],
        "branch_id": event["branch_id"],
        "txn_id": txn["txn_id"],
        "timestamp": txn["timestamp"],
        "amount": txn["amount"],
        "txn_type": txn["txn_type"],
        "channel": txn["channel"],
        "token_id": token_for(local_party["account_number"]),
        "counterparty_token_id": token_for(counter_party["account_number"]),
        "local_account_number": local_party["account_number"],  # for this branch's own local feature lookup only - never published
        "is_transfer_out": not is_deposit_in,
    }
