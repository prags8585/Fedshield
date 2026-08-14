"""Directly answers "did FL actually change anything on the real demo
scenarios" - scores Case 1 (216-txn) or Case 2 (500-txn) with the BEFORE and
AFTER weight snapshots from evaluation/fl_before_after.py, and reports the
difference in flagged/false-positive counts AND in confidence scores (is
real fraud now scored higher; is real legit now scored lower) - not just a
flag/no-flag count, since a shift toward more confident correct scoring is
real evidence even when it doesn't cross the flagging threshold.

Deterministic and offline: masks and extracts the same 5 features consumer.py
scores live, WITHOUT needing a live Docker/Kafka run - velocity_10min and
account_age_days aren't part of the scored feature set (see model.py), so
skipping them here doesn't change the score, only removes run-to-run timing
noise unrelated to FL.

Run AFTER both evaluation/fl_before_after.py --phase before and --phase after
have been run (an FL round in between):
    PYTHONPATH=. python3 evaluation/fl_demo_impact.py --case 1
    PYTHONPATH=. python3 evaluation/fl_demo_impact.py --case 2
"""
import argparse
import json
import os
from datetime import datetime

import numpy as np
import torch

from branch_node.masking import mask_event, token_for
from branch_node.model import DEFAULT_LR_MODEL_PATH, LogisticFraudModel
from shared.config import CTR_THRESHOLD_USD

BEFORE_AFTER_DIR = os.path.join(os.path.dirname(__file__), "fl_before_after")

CASES = {
    "1": {
        "events": ["data/background.json", "data/layering_hops4_events.json"],
        "ground_truth": "data/layering_hops4_ground_truth.json",
        "fraud_field": "fraud_txn_ids",
        "label": "Case 1 (216-txn, single ring)",
    },
    "2": {
        "events": ["data/scenario_500/background.json", "data/scenario_500/multi_ring_events.json"],
        "ground_truth": "data/scenario_500/multi_ring_ground_truth.json",
        "fraud_field": "all_fraud_txn_ids",
        "label": "Case 2 (500-txn, 3 rings)",
    },
    "3": {
        "events": ["data/scenario_1500/background.json", "data/scenario_1500/multi_ring_events.json"],
        "ground_truth": "data/scenario_1500/multi_ring_ground_truth.json",
        "fraud_field": "all_fraud_txn_ids",
        "label": "Case 3 (1500-txn, 19 rings)",
    },
}


def _features_array(masked: dict) -> list:
    ts = datetime.strptime(masked["timestamp"], "%Y-%m-%dT%H:%M:%S.%fZ")
    return [
        round(masked["amount"] / CTR_THRESHOLD_USD, 4),
        float(masked["txn_type"] in ("CASH_DEPOSIT", "CASH_WITHDRAWAL")),
        float(ts.hour),
        float(ts.weekday()),
        float(masked["is_transfer_out"]),
    ]


def _load_snapshot(phase: str) -> dict:
    path = os.path.join(BEFORE_AFTER_DIR, f"{phase}.json")
    if not os.path.exists(path):
        raise RuntimeError(f"{path} not found - run evaluation/fl_before_after.py --phase {phase} first.")
    with open(path) as f:
        return json.load(f)


def _score_batch(weight: list, bias: float, mean: list, std: list, X: list) -> np.ndarray:
    model = LogisticFraudModel()
    with torch.no_grad():
        model.linear.weight.copy_(torch.tensor([weight], dtype=torch.float32))
        model.linear.bias.copy_(torch.tensor([bias], dtype=torch.float32))
    model.eval()
    X_t = torch.tensor(X, dtype=torch.float32)
    mean_t = torch.tensor(mean, dtype=torch.float32)
    std_t = torch.tensor(std, dtype=torch.float32)
    with torch.no_grad():
        probs = torch.sigmoid(model((X_t - mean_t) / std_t)).numpy()
    return probs


def main():
    parser = argparse.ArgumentParser(description="Measure FL's before/after impact on a real demo scenario")
    parser.add_argument("--case", choices=["1", "2", "3"], default="1")
    args = parser.parse_args()
    case = CASES[args.case]

    with open(DEFAULT_LR_MODEL_PATH) as f:
        live_payload = json.load(f)
    mean, std, threshold = live_payload["mean"], live_payload["std"], live_payload["threshold"]

    before = _load_snapshot("before")
    after = _load_snapshot("after")

    events = []
    for path in case["events"]:
        events.extend(json.load(open(path)))
    ground_truth = json.load(open(case["ground_truth"]))
    fraud_txn_ids = set(ground_truth[case["fraud_field"]])

    masked_events = [mask_event(e) for e in events]
    X = [_features_array(m) for m in masked_events]
    y = np.array([1 if m["txn_id"] in fraud_txn_ids else 0 for m in masked_events])

    print(f"{case['label']}: {len(X)} transactions ({int(y.sum())} real fraud)\n")

    results = {}
    for label, snap in [("BEFORE", before), ("AFTER", after)]:
        probs = _score_batch(snap["weight"], snap["bias"], mean, std, X)
        preds = (probs >= threshold).astype(int)

        tp = int(((preds == 1) & (y == 1)).sum())
        fp = int(((preds == 1) & (y == 0)).sum())
        fn = int(((preds == 0) & (y == 1)).sum())
        tn = int(((preds == 0) & (y == 0)).sum())

        avg_score_fraud = float(probs[y == 1].mean()) if (y == 1).any() else float("nan")
        avg_score_legit = float(probs[y == 0].mean()) if (y == 0).any() else float("nan")

        results[label] = dict(tp=tp, fp=fp, fn=fn, tn=tn, avg_score_fraud=avg_score_fraud, avg_score_legit=avg_score_legit)
        print(f"[{label}] flagged={tp + fp} (TP={tp}, FP={fp})  missed={fn}  "
              f"avg_score_on_real_fraud={avg_score_fraud:.4f}  avg_score_on_real_legit={avg_score_legit:.4f}")

    b, a = results["BEFORE"], results["AFTER"]
    print(f"\n{'Metric':>28} {'Before':>10} {'After':>10} {'Change':>10}")
    print(f"{'Flagged total':>28} {b['tp']+b['fp']:>10} {a['tp']+a['fp']:>10} {(a['tp']+a['fp'])-(b['tp']+b['fp']):>+10}")
    print(f"{'False positives':>28} {b['fp']:>10} {a['fp']:>10} {a['fp']-b['fp']:>+10}")
    print(f"{'Fraud missed':>28} {b['fn']:>10} {a['fn']:>10} {a['fn']-b['fn']:>+10}")
    print(f"{'Avg score on real fraud':>28} {b['avg_score_fraud']:>10.4f} {a['avg_score_fraud']:>10.4f} {a['avg_score_fraud']-b['avg_score_fraud']:>+10.4f}")
    print(f"{'Avg score on real legit':>28} {b['avg_score_legit']:>10.4f} {a['avg_score_legit']:>10.4f} {a['avg_score_legit']-b['avg_score_legit']:>+10.4f}")

    print(
        "\nReading this: a higher 'avg score on real fraud' after FL means the model is flagging "
        "genuine fraud with MORE confidence, even if the flag/no-flag count didn't change. A lower "
        "'avg score on real legit' means it's more confidently leaving innocent transactions alone. "
        "Both are real signals of the model getting smarter, even when the binary FP count is flat."
    )


if __name__ == "__main__":
    main()
