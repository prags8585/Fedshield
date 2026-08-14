"""Same two plots as visualize_model.py (confusion matrix, ROC curve), but
scored from a REAL live Docker run instead of the offline held-out test
split. visualize_model.py answers "how good is this model on data shaped
like its training set"; this answers "how did it actually do on the exact
216-transaction demo scenario, transaction by transaction, as scored live by
the branch containers" - a genuinely different measurement, not a re-plot of
the same numbers.

Reads every transaction's real score from Redis (score:* keys - written by
consumer.py for ALL transactions, flagged or not, so this covers true
negatives and false negatives too, unlike the flagged-only view in
evaluation/downstream_filter_experiment.py). Ground truth: any txn_id listed
in the layering scenario's fraud_txn_ids is fraud; everything else scored is
legit, since background.json never contains injected fraud.

Run AFTER completing DEMO_RUNBOOK.md Steps 5-8 (stack up, producer streamed,
scores sitting in Redis):
    PYTHONPATH=. python3 evaluation/visualize_live_demo.py
Output:
    evaluation/plots/confusion_matrix_live_demo.png
    evaluation/plots/roc_curve_live_demo.png
"""
import json
import os

import matplotlib

matplotlib.use("Agg")
import numpy as np
import redis
from sklearn.metrics import confusion_matrix, precision_score, recall_score, roc_auc_score

from evaluation.visualize_model import PLOTS_DIR, plot_confusion_matrix, plot_roc_curve
from shared.schemas import ScoreRecord

REDIS_URL = "redis://localhost:6380"
GROUND_TRUTH_PATH = "data/layering_hops4_ground_truth.json"


def _load_threshold() -> float:
    with open("shared/lr_model.json") as f:
        return json.load(f)["threshold"]


def _load_live_scores(r: redis.Redis) -> list:
    records = []
    for key in r.scan_iter("score:*"):
        records.append(ScoreRecord.model_validate_json(r.get(key)))
    return records


def main():
    os.makedirs(PLOTS_DIR, exist_ok=True)

    threshold = _load_threshold()
    r = redis.from_url(REDIS_URL)
    records = _load_live_scores(r)
    if not records:
        raise RuntimeError(
            "No score:* keys found in Redis. Run DEMO_RUNBOOK.md Steps 5-8 first "
            "(bring the stack up, stream producer.py) before running this script."
        )

    fraud_txn_ids = set(json.load(open(GROUND_TRUTH_PATH))["fraud_txn_ids"])

    y_true = np.array([1 if rec.txn_id in fraud_txn_ids else 0 for rec in records])
    y_score = np.array([rec.score for rec in records])
    preds = (y_score >= threshold).astype(int)

    auc_score = roc_auc_score(y_true, y_score)
    precision = precision_score(y_true, preds, zero_division=0)
    recall = recall_score(y_true, preds, zero_division=0)
    fpr = preds[y_true == 0].mean() if (y_true == 0).any() else 0.0

    plot_confusion_matrix(
        y_true, preds, threshold,
        filename="confusion_matrix_live_demo.png",
        title=f"Confusion Matrix - Live 216-txn Demo (decision threshold = {threshold})",
    )
    plot_roc_curve(
        y_true, y_score, auc_score,
        filename="roc_curve_live_demo.png",
        title="ROC Curve - Live 216-txn Demo",
    )

    cm = confusion_matrix(y_true, preds)
    print(f"Scored transactions found in Redis: {len(records)}")
    print(f"Ground truth fraud transactions: {len(fraud_txn_ids)}")
    print(f"Decision threshold: {threshold}")
    print(f"\nConfusion matrix: TN={cm[0][0]} FP={cm[0][1]} FN={cm[1][0]} TP={cm[1][1]}")
    print(f"ROC-AUC: {auc_score:.4f}  Precision: {precision:.4f}  Recall: {recall:.4f}  FPR: {fpr:.4f}")
    print(f"\nSaved plots to {PLOTS_DIR}/:")
    print("  - confusion_matrix_live_demo.png")
    print("  - roc_curve_live_demo.png")


if __name__ == "__main__":
    main()
