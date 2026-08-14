"""Generates the per-branch training partitions and the central held-out
validation set used by Session 5's FL round.

Deliberately FRESH data (its own seed ranges), never the same batches that
already trained the current live model (shared/lr_model.json) - retraining
on data the model already fits would teach it nothing. This stands in for
"new transactions that have arrived since the model was last trained," the
actual real-world situation FL exists to handle.

Partitioning is done by filtering one shared simulated batch by branch_id,
NOT by generating 3 separate synthetic worlds - this mirrors exactly how the
live Kafka/consumer pipeline already isolates data (each branch's consumer
only ever sees its own topic), rather than inventing an artificial 3-way
split that doesn't reflect how a real fraud ring's hops actually land across
branches.
"""
import random

import redis

from branch_node.model import features_to_array
from branch_node.train_model import rows_for_batch
from shared.config import REDIS_URL
from shared.redis_keys import labels_key
from shared.schemas import Label

N_FL_FRAUD_BATCHES = 6
N_FL_BACKGROUND_BATCHES = 4
N_VAL_FRAUD_BATCHES = 3
N_VAL_BACKGROUND_BATCHES = 3
HOP_CHOICES = [2, 3, 4, 5, 6]
AMOUNT_RANGE = (8000.0, 9800.0)

# Disjoint from train_model.py's own ranges (2000+/5000+/9000+) and from each
# other, so the FL round's data, the validation set, and the original
# bootstrap training data never overlap.
_FL_BG_SEED_START = 60000
_FL_FRAUD_SEED_START = 65000
_VAL_BG_SEED_START = 70000
_VAL_FRAUD_SEED_START = 75000


def _pooled_rows(bg_seed_start: int, fraud_seed_start: int, n_fraud_batches: int, n_background_batches: int, rng_seed: int) -> list:
    rng = random.Random(rng_seed)
    rows = []
    for i in range(n_fraud_batches):
        hops = rng.choice(HOP_CHOICES)
        rows.extend(rows_for_batch(bg_seed=bg_seed_start + i, fraud_seed=fraud_seed_start + i, hops=hops, placement_range=AMOUNT_RANGE))
    for i in range(n_background_batches):
        rows.extend(rows_for_batch(bg_seed=bg_seed_start + 1000 + i))
    return rows


def real_labels_for_branch(branch_id: str) -> tuple:
    """Drains this branch's agent-verified label buffer (`labels:{branch}`,
    written by agents/label_generator.py the instant a Verdict Agent
    confirms GUILTY with high confidence - see CLAUDE.md's "Session 6
    Update") and converts each into the same (feature_array, label) shape
    the synthetic rows use. Returns (X, y) - both empty if nothing is
    waiting.

    Draining via LPOP (not just reading the list) is deliberate: each real
    label should contribute to exactly the next real FL round that consumes
    it, not be replayed into every future round forever.
    """
    r = redis.from_url(REDIS_URL)
    key = labels_key(branch_id)
    X, y = [], []
    while True:
        raw = r.lpop(key)
        if raw is None:
            break
        label = Label.model_validate_json(raw)
        X.append(features_to_array(label.features))
        y.append(1 if label.label == "fraud" else 0)
    return X, y


def build_branch_partition(branch_id: str, round_num: int = 0, include_real_labels: bool = False) -> tuple:
    """This branch's slice of one shared simulated batch of NEW
    transactions, filtered by branch_id, optionally topped up with any real
    agent-verified labels waiting for this branch. Returns (X, y).

    round_num shifts the underlying seed range so each FL round trains on
    genuinely different transactions instead of repeating the same fixed
    batch every round and every run - without this, only round 1 ever
    contains anything new to learn from, and rounds 2+ (and every later
    separate run) just retread the same rows with diminishing returns.

    `include_real_labels` defaults to False so evaluation/fl_vs_isolated.py's
    existing reproducible synthetic-only measurements are completely
    unaffected (and don't accidentally drain real labels meant for an actual
    round) - only the real branch_node/fl_client.py round opts in. Real
    labels are ADDED on top of the synthetic partition, not substituted for
    it: a real round typically has only a handful of confirmed cases at a
    time, far fewer than one round's synthetic batch, so mixing them in
    nudges the model with real signal without a tiny homogeneous set of
    agent-derived rows dominating or overfitting the round on its own.
    """
    bg_seed_start = _FL_BG_SEED_START + round_num * 100
    fraud_seed_start = _FL_FRAUD_SEED_START + round_num * 100
    rows = _pooled_rows(bg_seed_start, fraud_seed_start, N_FL_FRAUD_BATCHES, N_FL_BACKGROUND_BATCHES, rng_seed=42 + round_num)
    branch_rows = [r for r in rows if r[4] == branch_id]
    X = [r[0] for r in branch_rows]
    y = [r[1] for r in branch_rows]

    if include_real_labels:
        real_X, real_y = real_labels_for_branch(branch_id)
        if real_X:
            X = X + real_X
            y = y + real_y

    return X, y


def build_validation_set() -> tuple:
    """A small, fixed, centrally-held validation set - untouched by any
    branch's local training - used only to measure whether an FL round
    actually improved the model. Returns (X, y).
    """
    rows = _pooled_rows(_VAL_BG_SEED_START, _VAL_FRAUD_SEED_START, N_VAL_FRAUD_BATCHES, N_VAL_BACKGROUND_BATCHES, rng_seed=99)
    X = [r[0] for r in rows]
    y = [r[1] for r in rows]
    return X, y
