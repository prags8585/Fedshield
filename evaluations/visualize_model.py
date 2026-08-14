"""Generates all the standard evaluation visuals for the trained structuring
model - feature distributions, ROC curve, confusion matrix, a precision/
recall/F1/accuracy table, and the recall-vs-false-positive-rate trade-off
across thresholds. Run this after (re)training (train_model.py) to produce
plots to show alongside the model, not just a single accuracy number.

Rebuilds the EXACT same dataset and train/test split train_model.py used
(same seeds throughout), so the test set scored here is identical to the one
whose numbers are already saved in shared/lr_model.json's metrics block -
these plots are a visual explanation of those numbers, not a new evaluation.

Usage:
    PYTHONPATH=. python3 evaluation/visualize_model.py
Output:
    evaluation/plots/*.png
"""
import os
import textwrap

import matplotlib

matplotlib.use("Agg")  # no display needed - just write PNG files
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score, precision_score, recall_score, roc_curve
from sklearn.model_selection import train_test_split

from branch_node.model import FEATURE_ORDER, load_trained_model
from branch_node.train_model import CASH_TYPES, build_dataset

PLOTS_DIR = os.path.join(os.path.dirname(__file__), "plots")
sns.set_theme(style="whitegrid")

FEATURE_LABELS = {
    "amount_ratio_to_threshold": "Amount / $10k CTR threshold",
    "is_cash": "Is cash transaction",
    "hour_of_day": "Hour of day",
    "day_of_week": "Day of week",
    "velocity_10min": "Transactions in last 10 min",
    "is_transfer_out": "Money leaving this account",
}


def plot_feature_distributions(X_test, y_test, output_dir=PLOTS_DIR, subtitle="held-out test set"):
    X = np.array(X_test)
    y = np.array(y_test)
    n_cols = 3
    n_rows = -(-len(FEATURE_ORDER) // n_cols)  # ceil division - grid sized to however many features exist
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(16, 4.5 * n_rows))
    axes_flat = np.atleast_1d(axes).flatten()
    for ax in axes_flat[len(FEATURE_ORDER):]:
        ax.set_visible(False)  # hide any leftover grid slots instead of showing a blank default axes

    for i, (feature_name, ax) in enumerate(zip(FEATURE_ORDER, axes_flat)):
        col = X[:, i]
        if feature_name in ("is_cash", "is_transfer_out"):
            width = 0.35
            for offset, label, mask, color in [(-width / 2, "Legit", y == 0, "#4C72B0"), (width / 2, "Fraud", y == 1, "#C44E52")]:
                vals, counts = np.unique(col[mask], return_counts=True)
                rates = counts / mask.sum()
                ax.bar(vals + offset, rates, width=width, label=label, color=color)
            ax.set_xticks([0, 1])
            ax.set_xticklabels(["No", "Yes"])
            ax.set_ylabel("Share of rows")
        else:
            sns.histplot(x=col[y == 0], stat="density", color="#4C72B0", label="Legit", alpha=0.5, bins=30, ax=ax)
            sns.histplot(x=col[y == 1], stat="density", color="#C44E52", label="Fraud", alpha=0.5, bins=30, ax=ax)
        ax.set_title(FEATURE_LABELS[feature_name])
        ax.set_xlabel("")
        ax.legend()
    fig.suptitle(f"Feature distributions: fraud vs. legit transactions ({subtitle})", fontsize=14)
    fig.tight_layout()
    fig.savefig(os.path.join(output_dir, "feature_distributions.png"), dpi=150)
    plt.close(fig)


def plot_roc_curve(y_test, probs, auc_score, output_dir=PLOTS_DIR, filename="roc_curve.png", title="ROC Curve - Structuring Detection Model"):
    fpr, tpr, thresholds = roc_curve(y_test, probs)
    fig, ax = plt.subplots(figsize=(7, 7))
    ax.plot(fpr, tpr, color="#C44E52", linewidth=2, label=f"Model (AUC = {auc_score:.3f})")
    ax.plot([0, 1], [0, 1], linestyle="--", color="gray", label="Random guessing")
    ax.set_xlabel("False Positive Rate")
    ax.set_ylabel("True Positive Rate (Recall)")
    ax.set_title(title)
    ax.legend(loc="lower right")
    fig.tight_layout()
    fig.savefig(os.path.join(output_dir, filename), dpi=150)
    plt.close(fig)


def plot_confusion_matrix(y_test, preds, threshold, output_dir=PLOTS_DIR, filename="confusion_matrix.png", title=None):
    cm = confusion_matrix(y_test, preds)
    fig, ax = plt.subplots(figsize=(6, 5))
    sns.heatmap(
        cm, annot=True, fmt="d", cmap="Blues", cbar=False,
        xticklabels=["Predicted Legit", "Predicted Fraud"], yticklabels=["Actual Legit", "Actual Fraud"], ax=ax,
    )
    ax.set_title(title or f"Confusion Matrix (decision threshold = {threshold})")
    fig.tight_layout()
    fig.savefig(os.path.join(output_dir, filename), dpi=150)
    plt.close(fig)


def plot_metrics_table(y_test, is_cash_test, probs, thresholds, output_dir=PLOTS_DIR, subtitle="held-out test set"):
    rows = []
    for threshold in thresholds:
        preds = (probs >= threshold).astype(int)
        cash_mask = np.array([(is_cash and label == 1) for is_cash, label in zip(is_cash_test, y_test)])
        hop_mask = np.array([(not is_cash and label == 1) for is_cash, label in zip(is_cash_test, y_test)])
        rows.append([
            threshold,
            f"{accuracy_score(y_test, preds):.3f}",
            f"{precision_score(y_test, preds, zero_division=0):.3f}",
            f"{recall_score(y_test, preds, zero_division=0):.3f}",
            f"{f1_score(y_test, preds, zero_division=0):.3f}",
            f"{recall_score(np.ones(cash_mask.sum()), preds[cash_mask], zero_division=0):.3f}" if cash_mask.any() else "n/a",
            f"{recall_score(np.ones(hop_mask.sum()), preds[hop_mask], zero_division=0):.3f}" if hop_mask.any() else "n/a",
            f"{preds[np.array(y_test) == 0].mean():.3f}",
        ])

    columns = ["Threshold", "Accuracy", "Precision", "Recall", "F1", "Recall (cash)", "Recall (hops)", "False Pos. Rate"]
    legend_lines = [
        "Threshold — the suspicion-score cutoff for flagging a transaction; lower catches more fraud but flags more legit transactions too.",
        "Accuracy — % of ALL transactions classified correctly. Misleading here: fraud is only ~8% of traffic, so flagging nothing still scores ~92%.",
        "Precision — of everything flagged, what % was actually fraud (low = lots of false alarms).",
        "Recall — of all real fraud that existed, what % did the model catch (low = fraud slipping through).",
        "F1 — a single blended score of Precision and Recall, useful for comparing thresholds at a glance.",
        "Recall (cash) — recall measured only on the 'easy' fraud: cash deposits/withdrawals.",
        "Recall (hops) — recall measured only on the 'hard' fraud: WIRE/ACH transfers mid-chain, designed to look ordinary.",
        "False Pos. Rate — of all genuinely legit transactions, what % got wrongly flagged. This is the real cost of chasing higher recall.",
    ]

    wrapped_lines = []
    for line in legend_lines:
        label, _, rest = line.partition(" — ")
        wrapped = textwrap.wrap(rest, width=100)
        wrapped_lines.append(f"{label} — {wrapped[0]}")
        for cont in wrapped[1:]:
            wrapped_lines.append(f"{'':<{len(label) + 3}}{cont}")

    table_height = 0.55 * len(thresholds) + 1.0
    legend_height = 0.32 * len(wrapped_lines) + 0.6
    fig, (ax_table, ax_legend) = plt.subplots(
        2, 1, figsize=(15, table_height + legend_height),
        gridspec_kw={"height_ratios": [table_height, legend_height]},
    )

    ax_table.axis("off")
    table = ax_table.table(cellText=rows, colLabels=columns, loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(12)
    table.scale(1, 2.2)
    for j in range(len(columns)):
        table[0, j].set_facecolor("#4C72B0")
        table[0, j].set_text_props(color="white", weight="bold")
    ax_table.set_title(f"Model performance at each decision threshold ({subtitle})", fontsize=15, pad=20)

    ax_legend.axis("off")
    legend_text = "\n".join(wrapped_lines)
    ax_legend.text(
        0.0, 1.0, "Column legend:\n\n" + legend_text, fontsize=12, va="top", ha="left", family="monospace",
        transform=ax_legend.transAxes, linespacing=1.6,
    )

    fig.tight_layout()
    fig.savefig(os.path.join(output_dir, "metrics_table.png"), dpi=150)
    plt.close(fig)
    return rows, columns


def plot_threshold_tradeoff(y_test, probs, output_dir=PLOTS_DIR, title="Recall vs. Precision vs. False Positive Rate, across thresholds"):
    thresholds = np.linspace(0.01, 0.95, 40)
    recalls, precisions, fprs = [], [], []
    y = np.array(y_test)
    for threshold in thresholds:
        preds = (probs >= threshold).astype(int)
        recalls.append(recall_score(y, preds, zero_division=0))
        precisions.append(precision_score(y, preds, zero_division=0))
        fprs.append(preds[y == 0].mean())

    fig, ax = plt.subplots(figsize=(9, 6))
    ax.plot(thresholds, recalls, label="Recall", color="#55A868", linewidth=2)
    ax.plot(thresholds, precisions, label="Precision", color="#4C72B0", linewidth=2)
    ax.plot(thresholds, fprs, label="False Positive Rate", color="#C44E52", linewidth=2)
    ax.axvline(0.3, linestyle="--", color="gray", label="Chosen threshold (0.3)")
    ax.set_xlabel("Decision threshold")
    ax.set_ylabel("Rate")
    ax.set_title(title)
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(output_dir, "threshold_tradeoff.png"), dpi=150)
    plt.close(fig)


def main():
    os.makedirs(PLOTS_DIR, exist_ok=True)

    print("Rebuilding the exact dataset/split used during training (same seeds)...")
    X, y, txn_types, range_tags = build_dataset()
    X_train, X_test, y_train, y_test, types_train, types_test = train_test_split(
        X, y, txn_types, test_size=0.25, random_state=42, stratify=y
    )
    print(f"Test set: {len(X_test)} rows ({sum(y_test)} fraud, {len(y_test) - sum(y_test)} legit)")

    scorer = load_trained_model()
    probs = scorer.score_batch(X_test)
    from sklearn.metrics import roc_auc_score
    auc_score = roc_auc_score(y_test, probs)

    print("Generating plots...")
    plot_feature_distributions(X_test, y_test)
    plot_roc_curve(y_test, probs, auc_score)

    preds_at_threshold = (probs >= scorer.threshold).astype(int)
    plot_confusion_matrix(y_test, preds_at_threshold, scorer.threshold)

    is_cash_test = [t in CASH_TYPES for t in types_test]
    rows, columns = plot_metrics_table(y_test, is_cash_test, probs, thresholds=[0.5, 0.4, scorer.threshold, 0.2, 0.1])
    plot_threshold_tradeoff(y_test, probs)

    print(f"\nSaved 5 plots to {PLOTS_DIR}/:")
    for fname in ["feature_distributions.png", "roc_curve.png", "confusion_matrix.png", "metrics_table.png", "threshold_tradeoff.png"]:
        print(f"  - {fname}")

    print(f"\nROC-AUC: {auc_score:.4f}")
    print(f"\n{'Threshold':>10} {'Accuracy':>9} {'Precision':>10} {'Recall':>7} {'F1':>6} {'Recall(cash)':>13} {'Recall(hops)':>13} {'FPR':>7}")
    for row in rows:
        print(f"{row[0]:>10} {row[1]:>9} {row[2]:>10} {row[3]:>7} {row[4]:>6} {row[5]:>13} {row[6]:>13} {row[7]:>7}")


if __name__ == "__main__":
    main()
