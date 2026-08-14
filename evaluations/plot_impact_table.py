"""Renders a before/after impact comparison as a matplotlib table (blue
header, rounded title bar) - the same visual style used for Case 1's
216-txn table, generalized so any CASES entry from fl_demo_impact.py can
reuse it instead of hand-building a one-off script per case.

Run AFTER evaluation/fl_before_after.py --phase before/after and
evaluation/plot_case_impact.py (or equivalent) have produced real
before/after impact numbers for a case:
    PYTHONPATH=. python3 evaluation/plot_impact_table.py --case 3 \
        --before-label "Before (pre-FL, Round 0)" \
        --after-label "After (Round 5, real label injected in Round 1)" \
        --out demo_visualizations/8_fl_impact_table_1500txn.png
"""
import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from branch_node.model import DEFAULT_LR_MODEL_PATH
from evaluation.fl_demo_impact import CASES, _load_snapshot
from evaluation.plot_case_impact import _score_impact as _impact

REPO_ROOT = Path(__file__).resolve().parents[1]


def build_table(before: dict, after: dict, title: str, before_label: str, after_label: str, out_path: Path) -> None:
    rows = [
        ("Flagged total", before["flagged"], after["flagged"], "{:d}"),
        ("False positives", before["false_positives"], after["false_positives"], "{:d}"),
        ("Fraud missed", before["missed_fraud"], after["missed_fraud"], "{:d}"),
        ("Avg. score on real fraud", before["avg_score_fraud"], after["avg_score_fraud"], "{:.4f}"),
        ("Avg. score on real legit", before["avg_score_legit"], after["avg_score_legit"], "{:.4f}"),
    ]

    col_labels = ["Metric", before_label, after_label, "Change"]
    cell_text = []
    for name, b, a, fmt in rows:
        change = a - b
        change_str = f"{'+' if change >= 0 else ''}{fmt.format(change) if fmt == '{:d}' else f'{change:+.4f}'}"
        if fmt == "{:d}":
            change_str = f"{'+' if change >= 0 else ''}{int(change)}"
        cell_text.append([name, fmt.format(b), fmt.format(a), change_str])

    fig, ax = plt.subplots(figsize=(13, 0.9 + 0.62 * len(rows)))
    ax.axis("off")

    col_widths = [0.28, 0.28, 0.28, 0.16]
    table = ax.table(
        cellText=cell_text,
        colLabels=col_labels,
        cellLoc="center",
        colLoc="center",
        loc="center",
        colWidths=col_widths,
    )
    table.auto_set_font_size(False)
    table.set_fontsize(12)
    table.scale(1, 2.3)
    for col in range(len(col_labels)):
        table[0, col].set_text_props(fontsize=10.5)

    header_color = "#4472C4"
    for (row, col), cell in table.get_celld().items():
        cell.set_edgecolor("black")
        if row == 0:
            cell.set_facecolor(header_color)
            cell.set_text_props(color="white", weight="bold")
        else:
            cell.set_facecolor("white")
        if col == 0:
            cell.set_text_props(ha="left")
            cell._loc = "left"

    ax.set_title(title, fontsize=14, weight="bold", pad=20)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True, choices=list(CASES.keys()))
    parser.add_argument("--before-label", required=True)
    parser.add_argument("--after-label", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    with open(DEFAULT_LR_MODEL_PATH) as f:
        live_payload = json.load(f)
    mean, std, threshold = live_payload["mean"], live_payload["std"], live_payload["threshold"]

    before_snap = _load_snapshot("before")
    after_snap = _load_snapshot("after")
    before_payload = {**before_snap, "mean": mean, "std": std, "threshold": threshold}
    after_payload = {**after_snap, "mean": mean, "std": std, "threshold": threshold}

    before = _impact(before_payload, args.case)
    after = _impact(after_payload, args.case)

    title = f"{CASES[args.case]['label']} Impact - real GUILTY-labeled ring fed into FL"
    out_path = REPO_ROOT / args.out
    build_table(before, after, title, args.before_label, args.after_label, out_path)
    print(f"before: {before}")
    print(f"after:  {after}")
    print(f"saved -> {out_path}")


if __name__ == "__main__":
    main()
