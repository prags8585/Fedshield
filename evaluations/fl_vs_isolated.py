"""Tests the actual research claim - "federated beats single-branch-only
training" - rather than just before/after on one round.

Starting from the SAME current model, trains 3 things independently from
that identical starting point, using the SAME round-0 branch partitions
`fl_client.py` would use in a real round:
  1. Each branch ALONE, no sharing at all (3 separate "isolated" models)
  2. The FEDERATED blend (the 3 branches' updates averaged together -
     the same FedAvg math `fl_server/server.py` performs, replicated here
     directly so this comparison doesn't need a live Flower client/server
     round just to produce it)

All 4 candidates (3 isolated + 1 federated) are evaluated on the exact same
held-out validation set, alongside the original starting point, so the
comparison is apples-to-apples.

Usage:
    PYTHONPATH=. python3 evaluation/fl_vs_isolated.py
Output:
    evaluation/fl_before_after/federated_vs_isolated.png
    Printed comparison table
"""
import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
import torch
import torch.nn as nn
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score, roc_auc_score

from branch_node.fl_data import build_branch_partition, build_validation_set
from branch_node.model import DEFAULT_LR_MODEL_PATH, LogisticFraudModel
from shared.config import BRANCH_IDS

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "fl_before_after")
LOCAL_EPOCHS = 20
ROUND_NUM = 0  # same round-0 partitions a real round-1 fl_client.py call would use
CASH_FEATURE_INDEX = 1


def _load_payload() -> dict:
    with open(DEFAULT_LR_MODEL_PATH) as f:
        return json.load(f)


def _train_from(weight: list, bias: float, mean: list, std: list, X: list, y: list) -> tuple:
    """One local practice pass, identical to fl_client.py's fit(), starting
    from the given weight/bias. Returns the resulting (weight, bias).
    """
    model = LogisticFraudModel()
    with torch.no_grad():
        model.linear.weight.copy_(torch.tensor([weight], dtype=torch.float32))
        model.linear.bias.copy_(torch.tensor([bias], dtype=torch.float32))

    X_t = torch.tensor(X, dtype=torch.float32)
    y_t = torch.tensor(y, dtype=torch.float32)
    mean_t = torch.tensor(mean, dtype=torch.float32)
    std_t = torch.tensor(std, dtype=torch.float32)
    X_std = (X_t - mean_t) / std_t

    n_pos = max(y_t.sum().item(), 1.0)
    n_neg = max(len(y_t) - y_t.sum().item(), 1.0)
    pos_weight = torch.tensor(n_neg / n_pos)

    optimizer = torch.optim.Adam(model.parameters(), lr=0.05)
    loss_fn = nn.BCEWithLogitsLoss(pos_weight=pos_weight)

    model.train()
    for _ in range(LOCAL_EPOCHS):
        optimizer.zero_grad()
        loss = loss_fn(model(X_std), y_t)
        loss.backward()
        optimizer.step()
    model.eval()

    new_weight = model.linear.weight.detach().numpy().reshape(-1).tolist()
    new_bias = float(model.linear.bias.detach().numpy().reshape(-1)[0])
    return new_weight, new_bias


def _evaluate(weight: list, bias: float, mean: list, std: list, threshold: float, X_val: list, y_val: list) -> dict:
    model = LogisticFraudModel()
    with torch.no_grad():
        model.linear.weight.copy_(torch.tensor([weight], dtype=torch.float32))
        model.linear.bias.copy_(torch.tensor([bias], dtype=torch.float32))
    model.eval()

    X = torch.tensor(X_val, dtype=torch.float32)
    mean_t = torch.tensor(mean, dtype=torch.float32)
    std_t = torch.tensor(std, dtype=torch.float32)
    with torch.no_grad():
        probs = torch.sigmoid(model((X - mean_t) / std_t)).numpy()

    y = np.array(y_val)
    preds = (probs >= threshold).astype(int)
    is_cash = np.array(X_val)[:, CASH_FEATURE_INDEX].astype(bool)
    cash_mask = is_cash & (y == 1)
    hop_mask = (~is_cash) & (y == 1)

    return {
        "auc": float(roc_auc_score(y, probs)),
        "accuracy": float(accuracy_score(y, preds)),
        "precision": float(precision_score(y, preds, zero_division=0)),
        "recall": float(recall_score(y, preds, zero_division=0)),
        "f1": float(f1_score(y, preds, zero_division=0)),
        "recall_cash": float(recall_score(np.ones(cash_mask.sum()), preds[cash_mask], zero_division=0)) if cash_mask.any() else None,
        "recall_hops": float(recall_score(np.ones(hop_mask.sum()), preds[hop_mask], zero_division=0)) if hop_mask.any() else None,
        "fpr": float(preds[y == 0].mean()) if (y == 0).any() else 0.0,
    }


def _plot(rows: list) -> None:
    columns = ["Candidate", "AUC", "Precision", "Recall", "F1", "FPR"]
    cell_text = [[r["name"], f"{r['metrics']['auc']:.4f}", f"{r['metrics']['precision']:.4f}",
                  f"{r['metrics']['recall']:.4f}", f"{r['metrics']['f1']:.4f}", f"{r['metrics']['fpr']:.4f}"] for r in rows]

    sns.set_theme(style="whitegrid")
    fig, ax = plt.subplots(figsize=(9, 0.55 * len(rows) + 1.5))
    ax.axis("off")
    table = ax.table(cellText=cell_text, colLabels=columns, loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(12)
    table.scale(1, 2.0)
    for j in range(len(columns)):
        table[0, j].set_facecolor("#4C72B0")
        table[0, j].set_text_props(color="white", weight="bold")
    # Highlight the federated row
    for i, r in enumerate(rows, start=1):
        if r["name"] == "Federated (averaged)":
            for j in range(len(columns)):
                table[i, j].set_facecolor("#E8F0FE")
    ax.set_title("Federated vs. Training Alone (same starting point, same validation set)", fontsize=14, pad=20)
    fig.tight_layout()
    fig.savefig(os.path.join(OUTPUT_DIR, "federated_vs_isolated.png"), dpi=150)
    plt.close(fig)


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    payload = _load_payload()
    weight0, bias0, mean, std, threshold = payload["weight"], payload["bias"], payload["mean"], payload["std"], payload["threshold"]

    X_val, y_val = build_validation_set()
    print(f"Validation set: {len(X_val)} rows ({sum(y_val)} fraud)\n")

    rows = []

    starting_metrics = _evaluate(weight0, bias0, mean, std, threshold, X_val, y_val)
    rows.append({"name": "Starting point (before anything)", "metrics": starting_metrics})

    isolated_updates = []
    for branch_id in BRANCH_IDS:
        X, y = build_branch_partition(branch_id, round_num=ROUND_NUM)
        w, b = _train_from(weight0, bias0, mean, std, X, y)
        isolated_updates.append((w, b))
        metrics = _evaluate(w, b, mean, std, threshold, X_val, y_val)
        rows.append({"name": f"{branch_id} trained ALONE (no sharing)", "metrics": metrics})
        print(f"{branch_id}: {len(X)} local rows ({sum(y)} fraud)")

    fed_weight = np.mean([w for w, b in isolated_updates], axis=0).tolist()
    fed_bias = float(np.mean([b for w, b in isolated_updates]))
    fed_metrics = _evaluate(fed_weight, fed_bias, mean, std, threshold, X_val, y_val)
    rows.append({"name": "Federated (averaged)", "metrics": fed_metrics})

    print(f"\n{'Candidate':>38} {'AUC':>8} {'Precision':>10} {'Recall':>8} {'F1':>8} {'FPR':>8}")
    for r in rows:
        m = r["metrics"]
        print(f"{r['name']:>38} {m['auc']:>8.4f} {m['precision']:>10.4f} {m['recall']:>8.4f} {m['f1']:>8.4f} {m['fpr']:>8.4f}")

    _plot(rows)
    print(f"\nSaved -> {os.path.join(OUTPUT_DIR, 'federated_vs_isolated.png')}")


if __name__ == "__main__":
    main()
