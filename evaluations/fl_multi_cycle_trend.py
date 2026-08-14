"""Plots the real, full 4-cycle before/after trend from running
evaluation/fl_before_after.py + evaluation/fl_demo_impact.py repeatedly
(see CLAUDE.md/SESSION_5_SUMMARY.md for the full methodology and honest
discussion). Numbers below are hardcoded from those actual runs - not
re-simulated - specifically so this plot can't drift from what really
happened. If you re-run the multi-cycle experiment, update these lists with
the new real numbers rather than assuming the same shape repeats.

Deliberately shows the full noisy trajectory (most individual cycles showed
ZERO change) rather than a single before/after pair - a smooth two-point
comparison would misrepresent how uneven the real path was.

Usage:
    PYTHONPATH=. python3 evaluation/fl_multi_cycle_trend.py
Output:
    evaluation/fl_before_after/multi_cycle_trend.png
"""
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "fl_before_after")

CHECKPOINTS = ["Start", "After\nCycle 1", "After\nCycle 2", "After\nCycle 3", "After\nCycle 4"]

# Real, observed values - see the conversation history / CLAUDE.md for the
# 4 actual runs these came from.
CASE1_FP = [15, 15, 15, 14, 14]
CASE1_FRAUD_CONF = [0.9149, 0.8631, 0.9126, 0.9111, 0.9116]

CASE2_FP = [26, 26, 24, 24, 24]
CASE2_FRAUD_CONF = [0.9339, 0.8929, 0.9320, 0.9311, 0.9317]


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    sns.set_theme(style="whitegrid")

    fig, axes = plt.subplots(2, 2, figsize=(13, 9))

    axes[0, 0].plot(CHECKPOINTS, CASE1_FP, marker="o", color="#C44E52", linewidth=2)
    axes[0, 0].set_title("Case 1 (216-txn): False Positives")
    axes[0, 0].set_ylabel("False positive count")
    for x, v in enumerate(CASE1_FP):
        axes[0, 0].annotate(str(v), (x, v), textcoords="offset points", xytext=(0, 8), ha="center")

    axes[0, 1].plot(CHECKPOINTS, CASE2_FP, marker="o", color="#C44E52", linewidth=2)
    axes[0, 1].set_title("Case 2 (500-txn): False Positives")
    axes[0, 1].set_ylabel("False positive count")
    for x, v in enumerate(CASE2_FP):
        axes[0, 1].annotate(str(v), (x, v), textcoords="offset points", xytext=(0, 8), ha="center")

    axes[1, 0].plot(CHECKPOINTS, CASE1_FRAUD_CONF, marker="o", color="#55A868", linewidth=2)
    axes[1, 0].set_title("Case 1: Avg. Confidence Score on REAL Fraud")
    axes[1, 0].set_ylabel("Average score (0-1)")
    axes[1, 0].set_ylim(0.8, 1.0)
    for x, v in enumerate(CASE1_FRAUD_CONF):
        axes[1, 0].annotate(f"{v:.3f}", (x, v), textcoords="offset points", xytext=(0, 8), ha="center")

    axes[1, 1].plot(CHECKPOINTS, CASE2_FRAUD_CONF, marker="o", color="#55A868", linewidth=2)
    axes[1, 1].set_title("Case 2: Avg. Confidence Score on REAL Fraud")
    axes[1, 1].set_ylabel("Average score (0-1)")
    axes[1, 1].set_ylim(0.8, 1.0)
    for x, v in enumerate(CASE2_FRAUD_CONF):
        axes[1, 1].annotate(f"{v:.3f}", (x, v), textcoords="offset points", xytext=(0, 8), ha="center")

    fig.suptitle(
        "FL Impact Across 4 Consecutive Rounds - Real, Unedited Results\n"
        "(3 of 4 individual rounds showed ZERO change in false positives on both cases - "
        "the net movement is small and uneven, not a smooth per-round improvement)",
        fontsize=12,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    out_path = os.path.join(OUTPUT_DIR, "multi_cycle_trend.png")
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"Saved -> {out_path}")

    print("\nNet change, Start -> After Cycle 4:")
    print(f"  Case 1: FP {CASE1_FP[0]} -> {CASE1_FP[-1]} ({CASE1_FP[-1]-CASE1_FP[0]:+d}), "
          f"fraud-confidence {CASE1_FRAUD_CONF[0]:.4f} -> {CASE1_FRAUD_CONF[-1]:.4f} ({CASE1_FRAUD_CONF[-1]-CASE1_FRAUD_CONF[0]:+.4f})")
    print(f"  Case 2: FP {CASE2_FP[0]} -> {CASE2_FP[-1]} ({CASE2_FP[-1]-CASE2_FP[0]:+d}), "
          f"fraud-confidence {CASE2_FRAUD_CONF[0]:.4f} -> {CASE2_FRAUD_CONF[-1]:.4f} ({CASE2_FRAUD_CONF[-1]-CASE2_FRAUD_CONF[0]:+.4f})")


if __name__ == "__main__":
    main()
