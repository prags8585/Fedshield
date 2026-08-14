"""Tabular before/after evidence for an FL round: snapshots the current
model's full metrics on the FIXED held-out validation set (the same one
fl_server/server.py itself uses), then - after a real FL round has run in
between - re-measures the (now updated) model on that exact same set and
prints/plots a side-by-side comparison.

"Before" and "after" are measured on the identical validation set (same
seed, from branch_node/fl_data.py) so the comparison is apples-to-apples -
not a different random sample each time.

Usage (two separate invocations, with a real FL round run in between):
    PYTHONPATH=. python3 evaluation/fl_before_after.py --phase before
    # ... now actually run fl_server/server.py + the 3 fl_client.py's ...
    PYTHONPATH=. python3 evaluation/fl_before_after.py --phase after
Output:
    evaluation/fl_before_after/before.json, after.json
    evaluation/fl_before_after/comparison_table.png (written on --phase after)
    Printed comparison table (both phases)
"""
import argparse
import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
import torch
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score, roc_auc_score

from branch_node.fl_data import build_validation_set
from branch_node.model import DEFAULT_LR_MODEL_PATH, LogisticFraudModel

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "fl_before_after")
CASH_FEATURE_INDEX = 1  # FEATURE_ORDER[1] == "is_cash"


def _load_payload() -> dict:
    with open(DEFAULT_LR_MODEL_PATH) as f:
        return json.load(f)


def _compute_metrics(payload: dict, X_val: list, y_val: list) -> dict:
    model = LogisticFraudModel()
    with torch.no_grad():
        model.linear.weight.copy_(torch.tensor([payload["weight"]], dtype=torch.float32))
        model.linear.bias.copy_(torch.tensor([payload["bias"]], dtype=torch.float32))
    model.eval()

    X = torch.tensor(X_val, dtype=torch.float32)
    mean_t = torch.tensor(payload["mean"], dtype=torch.float32)
    std_t = torch.tensor(payload["std"], dtype=torch.float32)
    with torch.no_grad():
        probs = torch.sigmoid(model((X - mean_t) / std_t)).numpy()

    y = np.array(y_val)
    threshold = payload["threshold"]
    preds = (probs >= threshold).astype(int)

    is_cash = np.array(X_val)[:, CASH_FEATURE_INDEX].astype(bool)
    cash_mask = is_cash & (y == 1)
    hop_mask = (~is_cash) & (y == 1)

    return {
        "threshold": threshold,
        "auc": float(roc_auc_score(y, probs)),
        "accuracy": float(accuracy_score(y, preds)),
        "precision": float(precision_score(y, preds, zero_division=0)),
        "recall": float(recall_score(y, preds, zero_division=0)),
        "f1": float(f1_score(y, preds, zero_division=0)),
        "recall_cash": float(recall_score(np.ones(cash_mask.sum()), preds[cash_mask], zero_division=0)) if cash_mask.any() else None,
        "recall_hops": float(recall_score(np.ones(hop_mask.sum()), preds[hop_mask], zero_division=0)) if hop_mask.any() else None,
        "fpr": float(preds[y == 0].mean()) if (y == 0).any() else 0.0,
        "weight": payload["weight"],
        "bias": payload["bias"],
    }


def _plot_comparison(before: dict, after: dict) -> None:
    rows = [
        ("ROC-AUC", before["auc"], after["auc"]),
        ("Accuracy", before["accuracy"], after["accuracy"]),
        ("Precision", before["precision"], after["precision"]),
        ("Recall", before["recall"], after["recall"]),
        ("F1", before["f1"], after["f1"]),
        ("Recall (cash)", before["recall_cash"], after["recall_cash"]),
        ("Recall (hops)", before["recall_hops"], after["recall_hops"]),
        ("False Positive Rate", before["fpr"], after["fpr"]),
    ]

    cell_text = [[f"{b:.4f}" if b is not None else "n/a", f"{a:.4f}" if a is not None else "n/a",
                  f"{(a - b):+.4f}" if (a is not None and b is not None) else "n/a"] for _, b, a in rows]
    row_labels = [r[0] for r in rows]

    sns.set_theme(style="whitegrid")
    fig, ax = plt.subplots(figsize=(9, 0.6 * len(rows) + 1.5))
    ax.axis("off")
    table = ax.table(cellText=cell_text, rowLabels=row_labels, colLabels=["Before FL round", "After FL round", "Change"], loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(12)
    table.scale(1, 2.0)
    for j in range(3):
        table[0, j].set_facecolor("#4C72B0")
        table[0, j].set_text_props(color="white", weight="bold")
    ax.set_title("FL Round: Before vs. After (same held-out validation set)", fontsize=14, pad=20)
    fig.tight_layout()
    fig.savefig(os.path.join(OUTPUT_DIR, "comparison_table.png"), dpi=150)
    plt.close(fig)


def _print_table(before: dict, after: dict) -> None:
    print(f"\n{'Metric':>20} {'Before':>10} {'After':>10} {'Change':>10}")
    for label, key in [
        ("ROC-AUC", "auc"), ("Accuracy", "accuracy"), ("Precision", "precision"), ("Recall", "recall"),
        ("F1", "f1"), ("Recall (cash)", "recall_cash"), ("Recall (hops)", "recall_hops"), ("FPR", "fpr"),
    ]:
        b, a = before.get(key), after.get(key)
        if b is None or a is None:
            print(f"{label:>20} {'n/a':>10} {'n/a':>10} {'n/a':>10}")
        else:
            print(f"{label:>20} {b:>10.4f} {a:>10.4f} {(a - b):>+10.4f}")


def main():
    parser = argparse.ArgumentParser(description="Before/after FL round evidence, on a fixed held-out validation set")
    parser.add_argument("--phase", choices=["before", "after"], required=True)
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    X_val, y_val = build_validation_set()
    payload = _load_payload()
    metrics = _compute_metrics(payload, X_val, y_val)

    out_path = os.path.join(OUTPUT_DIR, f"{args.phase}.json")
    with open(out_path, "w") as f:
        json.dump(metrics, f, indent=2)

    print(f"[{args.phase}] validation set: {len(X_val)} rows ({sum(y_val)} fraud)")
    print(f"[{args.phase}] threshold={metrics['threshold']}  AUC={metrics['auc']:.4f}  "
          f"precision={metrics['precision']:.4f}  recall={metrics['recall']:.4f}  fpr={metrics['fpr']:.4f}")
    print(f"[{args.phase}] saved -> {out_path}")

    if args.phase == "after":
        before_path = os.path.join(OUTPUT_DIR, "before.json")
        if not os.path.exists(before_path):
            print("\nNo before.json found - run `--phase before` first, before running an FL round, to get a comparison.")
            return
        with open(before_path) as f:
            before = json.load(f)
        _print_table(before, metrics)
        _plot_comparison(before, metrics)
        print(f"\nSaved comparison table -> {os.path.join(OUTPUT_DIR, 'comparison_table.png')}")


if __name__ == "__main__":
    main()
