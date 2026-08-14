"""Turns evaluation/fl_demo_impact.py's printed before/after numbers for any
CASES entry into the same 2-panel chart style dashboard/backend/tab4.py's
_plot_case1_impact() builds for Case 1 -- reused here as a standalone script
so a chart can be generated for a case (e.g. Case 3/1500-txn) that isn't
wired into the live dashboard tab yet (IMPACT_CASE there is hardcoded to
"1"). Never fabricates a trend: if before/after are flat or move the "wrong"
way, the chart shows that honestly rather than only plotting cases with a
good story.

Run AFTER evaluation/fl_before_after.py --phase before/after have both run
around a real FL round:
    PYTHONPATH=. python3 evaluation/plot_case_impact.py --case 3 --out demo_visualizations/7_fl_impact_1500txn.png
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from evaluation.fl_demo_impact import CASES, _features_array, _load_snapshot, _score_batch
from branch_node.masking import mask_event
from branch_node.model import DEFAULT_LR_MODEL_PATH

REPO_ROOT = Path(__file__).resolve().parents[1]


def _score_impact(payload: dict, case_key: str) -> dict:
    case = CASES[case_key]
    events = []
    for path in case["events"]:
        events.extend(json.load(open(REPO_ROOT / path)))
    ground_truth = json.load(open(REPO_ROOT / case["ground_truth"]))
    fraud_txn_ids = set(ground_truth[case["fraud_field"]])

    masked_events = [mask_event(e) for e in events]
    X = [_features_array(m) for m in masked_events]
    y = np.array([1 if m["txn_id"] in fraud_txn_ids else 0 for m in masked_events])

    probs = _score_batch(payload["weight"], payload["bias"], payload["mean"], payload["std"], X)
    preds = (probs >= payload["threshold"]).astype(int)

    tp = int(((preds == 1) & (y == 1)).sum())
    fp = int(((preds == 1) & (y == 0)).sum())
    fn = int(((preds == 0) & (y == 1)).sum())
    return {
        "flagged": tp + fp,
        "false_positives": fp,
        "missed_fraud": fn,
        "avg_score_fraud": float(probs[y == 1].mean()) if (y == 1).any() else None,
        "avg_score_legit": float(probs[y == 0].mean()) if (y == 0).any() else None,
    }


def plot_impact(before: dict, after: dict, case_label: str, out_path: Path) -> None:
    x = ["Before FL Round", "After FL Round"]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8))

    ax = axes[0]
    fp = [before["false_positives"], after["false_positives"]]
    ax.plot(x, fp, marker="o", color="#C44E52", linewidth=2.2, markersize=8)
    for xi, yi in zip(x, fp):
        ax.annotate(str(yi), (xi, yi), textcoords="offset points", xytext=(0, 10), ha="center", fontsize=11)
    ax.set_title(f"{case_label}: False Positives")
    ax.set_ylabel("False positive count")
    ax.margins(y=0.3)

    ax = axes[1]
    conf = [before["avg_score_fraud"], after["avg_score_fraud"]]
    ax.plot(x, conf, marker="o", color="#55A868", linewidth=2.2, markersize=8)
    for xi, yi in zip(x, conf):
        ax.annotate(f"{yi:.4f}", (xi, yi), textcoords="offset points", xytext=(0, 10), ha="center", fontsize=11)
    ax.set_title(f"{case_label}: Avg. Confidence Score on REAL Fraud")
    ax.set_ylabel("Average score (0-1)")
    ax.margins(y=0.3)

    fig.suptitle(f"FL Impact - {datetime.now().strftime('%Y-%m-%d %H:%M')}", fontsize=14, y=1.03)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True, choices=list(CASES.keys()))
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    with open(DEFAULT_LR_MODEL_PATH) as f:
        live_payload = json.load(f)
    mean, std, threshold = live_payload["mean"], live_payload["std"], live_payload["threshold"]

    before_snap = _load_snapshot("before")
    after_snap = _load_snapshot("after")
    before_payload = {**before_snap, "mean": mean, "std": std, "threshold": threshold}
    after_payload = {**after_snap, "mean": mean, "std": std, "threshold": threshold}

    before = _score_impact(before_payload, args.case)
    after = _score_impact(after_payload, args.case)

    out_path = REPO_ROOT / args.out
    plot_impact(before, after, CASES[args.case]["label"], out_path)
    print(f"before: {before}")
    print(f"after:  {after}")
    print(f"saved -> {out_path}")


if __name__ == "__main__":
    main()
