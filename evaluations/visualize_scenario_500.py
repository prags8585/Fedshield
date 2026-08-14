"""Full 5-plot evaluation set (feature distributions, ROC curve, confusion
matrix, metrics table, threshold trade-off) for the scenario_500 multi-ring
dataset - scored LIVE from Redis after a real Docker run, the same way
visualize_live_demo.py did for the original 216-txn scenario, not rebuilt
from an offline split like visualize_model.py.

The model is NOT retrained or touched here - shared/lr_model.json is the
same frozen model from Session 3, trained only on its original 25 offline
scenarios. This script just exposes that already-trained model to a dataset
it has never seen in any form (fresh customers, 3 concurrent rings, cross-
branch hops) and reports how it actually does - a genuine generalization
check, not a re-plot of numbers we already have.

Run AFTER streaming scenario_500 through the live stack (scores sitting in
Redis):
    KAFKA_BROKER=localhost:29092 PYTHONPATH=. python3 branch_node/producer.py \\
      --files data/scenario_500/background.json data/scenario_500/multi_ring_events.json \\
      --background-window-seconds 45
    PYTHONPATH=. python3 evaluation/visualize_scenario_500.py
Output:
    evaluation/case2_plots/*.png
"""
import json
import os

import matplotlib

matplotlib.use("Agg")
import numpy as np
import redis
from sklearn.metrics import roc_auc_score

from branch_node.model import features_to_array
from evaluation.visualize_model import (
    plot_confusion_matrix,
    plot_feature_distributions,
    plot_metrics_table,
    plot_roc_curve,
    plot_threshold_tradeoff,
)
from shared.schemas import ScoreRecord

REDIS_URL = "redis://localhost:6380"
GROUND_TRUTH_PATH = "data/scenario_500/multi_ring_ground_truth.json"
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "case2_plots")
SUBTITLE = "scenario_500 live run"


def _load_threshold() -> float:
    with open("shared/lr_model.json") as f:
        return json.load(f)["threshold"]


def _load_live_records(r: redis.Redis) -> list:
    return [ScoreRecord.model_validate_json(r.get(key)) for key in r.scan_iter("score:*")]


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    threshold = _load_threshold()
    r = redis.from_url(REDIS_URL)
    records = _load_live_records(r)
    if not records:
        raise RuntimeError(
            "No score:* keys found in Redis. Stream scenario_500 through the live stack first "
            "(see this file's docstring for the exact command)."
        )

    fraud_txn_ids = set(json.load(open(GROUND_TRUTH_PATH))["all_fraud_txn_ids"])

    X = np.array([features_to_array(rec.features) for rec in records])
    y = np.array([1 if rec.txn_id in fraud_txn_ids else 0 for rec in records])
    probs = np.array([rec.score for rec in records])
    is_cash = [bool(rec.features.is_cash) for rec in records]

    print(f"Scored transactions found in Redis: {len(records)}")
    print(f"Ground truth fraud transactions: {len(fraud_txn_ids)} ({sum(y)} matched in Redis)")

    auc_score = roc_auc_score(y, probs)
    preds = (probs >= threshold).astype(int)

    print("Generating plots...")
    plot_feature_distributions(X, y, output_dir=OUTPUT_DIR, subtitle=SUBTITLE)
    plot_roc_curve(y, probs, auc_score, output_dir=OUTPUT_DIR, title=f"ROC Curve - {SUBTITLE}")
    plot_confusion_matrix(y, preds, threshold, output_dir=OUTPUT_DIR, title=f"Confusion Matrix - {SUBTITLE} (threshold = {threshold})")
    rows, columns = plot_metrics_table(y, is_cash, probs, thresholds=[0.5, 0.4, threshold, 0.2, 0.1], output_dir=OUTPUT_DIR, subtitle=SUBTITLE)
    plot_threshold_tradeoff(y, probs, output_dir=OUTPUT_DIR, title=f"Recall vs. Precision vs. FPR across thresholds - {SUBTITLE}")

    print(f"\nSaved 5 plots to {OUTPUT_DIR}/:")
    for fname in ["feature_distributions.png", "roc_curve.png", "confusion_matrix.png", "metrics_table.png", "threshold_tradeoff.png"]:
        print(f"  - {fname}")

    print(f"\nROC-AUC: {auc_score:.4f}")
    print(f"\n{'Threshold':>10} {'Accuracy':>9} {'Precision':>10} {'Recall':>7} {'F1':>6} {'Recall(cash)':>13} {'Recall(hops)':>13} {'FPR':>7}")
    for row in rows:
        print(f"{row[0]:>10} {row[1]:>9} {row[2]:>10} {row[3]:>7} {row[4]:>6} {row[5]:>13} {row[6]:>13} {row[7]:>7}")


if __name__ == "__main__":
    main()
