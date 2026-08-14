"""Draws the real agent pipeline architecture - not a data plot, a reference
diagram of how a transaction actually flows through the system, matching
CLAUDE.md's "Post-Session 6 Extension - Reframed 3-Agent Pipeline" and the
Verdict Agent's FL-labeling role. Nothing here is simulated for the
picture's sake - every box and arrow matches real code: Kafka topics
(branch_node/producer.py), the ML model (branch_node/model.py), Redis keys
(shared/redis_keys.py), the 3 LLM agents plus the deterministic Money-Trail
Agent core (agents/*.py), and the FL feedback loop
(branch_node/fl_data.py's include_real_labels).

Usage:
    PYTHONPATH=. python3 evaluation/visualize_agent_pipeline.py
Output:
    demo_visualizations/4_agent_pipeline_architecture.png
"""
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "demo_visualizations")

C_DATA = "#4C72B0"
C_AGENT = "#8172B2"
C_DECISION = "#55A868"
C_OUTPUT = "#C44E52"
C_FL = "#CCB974"
TXT_ON_COLOR = "white"


def box(ax, x, y, w, h, text, color, fontsize=10, text_color=TXT_ON_COLOR):
    b = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                        linewidth=1.2, edgecolor="#333333", facecolor=color, alpha=0.92, zorder=2)
    ax.add_patch(b)
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fontsize,
            color=text_color, weight="medium", zorder=3, wrap=True)
    return (x, y, w, h)


def arrow(ax, b1, b2, side1="right", side2="left", style="-|>", color="#333333", lw=1.6, ls="-", label=None, rad=0.0):
    x1, y1, w1, h1 = b1
    x2, y2, w2, h2 = b2
    pts = {
        "right": (x1 + w1, y1 + h1 / 2), "left": (x1, y1 + h1 / 2),
        "top": (x1 + w1 / 2, y1 + h1), "bottom": (x1 + w1 / 2, y1),
    }
    pts2 = {
        "right": (x2 + w2, y2 + h2 / 2), "left": (x2, y2 + h2 / 2),
        "top": (x2 + w2 / 2, y2 + h2), "bottom": (x2 + w2 / 2, y2),
    }
    p1, p2 = pts[side1], pts2[side2]
    a = FancyArrowPatch(p1, p2, arrowstyle=style, mutation_scale=14, linewidth=lw,
                         color=color, linestyle=ls, connectionstyle=f"arc3,rad={rad}", zorder=1)
    ax.add_patch(a)
    if label:
        mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2 + 0.15
        ax.text(mx, my, label, ha="center", fontsize=8, color="#555555", style="italic")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    fig, ax = plt.subplots(figsize=(15, 9.5))
    ax.set_xlim(0, 15)
    ax.set_ylim(0, 9.5)
    ax.axis("off")

    kafka = box(ax, 0.4, 7.6, 2.3, 1.1, "Kafka\n3 branch topics", C_DATA)
    ml = box(ax, 3.3, 7.6, 2.5, 1.1, "ML model\nscores every txn\n(threshold 0.3)", C_DATA)
    redis1 = box(ax, 6.4, 7.6, 2.7, 1.1, "Redis\nflagged_accounts +\nfraud_events (pub/sub)", C_DATA)
    arrow(ax, kafka, ml)
    arrow(ax, ml, redis1)

    sa = box(ax, 9.7, 7.6, 2.4, 1.1, "Agent 1\nStructuring Agent\n(plain-English summary)", C_AGENT)
    arrow(ax, redis1, sa, label="wakes instantly")

    mta = box(ax, 9.7, 5.7, 2.4, 1.1, "Agent 2\nMoney-Trail Agent", C_AGENT)
    neo = box(ax, 12.6, 5.7, 2.1, 1.1, "Neo4j\ngraph traversal\n(check_convergence)", C_DATA, fontsize=9)
    arrow(ax, sa, mta, side1="bottom", side2="top")
    arrow(ax, mta, neo, label="deterministic")
    arrow(ax, neo, mta, side1="left", side2="right", rad=0.3, color="#999999", lw=1.1)

    dead_end = box(ax, 6.6, 5.7, 2.6, 1.1, "No convergence ->\ninsufficient_evidence / cycle\n(dropped, not reported)", "#999999", fontsize=9)
    arrow(ax, mta, dead_end, side1="left", side2="right", label="false positive")

    verdict = box(ax, 9.7, 3.8, 2.4, 1.1, "Verdict Agent\nGUILTY / NOT_GUILTY", C_DECISION)
    report = box(ax, 12.6, 3.8, 2.1, 1.1, "Agent 3\nReport Agent\n(no LLM call)", C_AGENT, fontsize=9)
    arrow(ax, mta, verdict, side1="bottom", side2="top", label="convergence_found")
    arrow(ax, mta, report, side1="bottom", side2="top", rad=0.25, label="always, independent\nof verdict")

    excel = box(ax, 12.6, 2.0, 2.1, 1.1, "fraud_rings_report\n.xlsx", C_OUTPUT, fontsize=9)
    arrow(ax, report, excel, side1="bottom", side2="top")

    labels = box(ax, 9.7, 2.0, 2.4, 1.1, "Label Generator\n(plain code)", C_FL, text_color="#333333")
    arrow(ax, verdict, labels, side1="bottom", side2="top", label="only if GUILTY\n+ confident")

    fl_buf = box(ax, 6.6, 2.0, 2.6, 1.1, "labels:{branch}\nFL training buffer", C_FL, text_color="#333333")
    arrow(ax, labels, fl_buf, side1="left", side2="right")

    fl_round = box(ax, 3.3, 2.0, 2.9, 1.1, "Federated Learning round\nFedAvg across 3 branches", C_FL, text_color="#333333")
    arrow(ax, fl_buf, fl_round, side1="left", side2="right")

    arrow(ax, fl_round, ml, side1="top", side2="bottom", rad=-0.35, ls="--", color="#8B6F1E",
          label="improved shared model\nfeeds the next transaction")

    ax.text(7.5, 9.1, "FedShieldV2 — real-time agent pipeline", ha="center", fontsize=15, weight="bold")
    ax.text(7.5, 8.75, "Every box is real code; the dashed line is the feedback loop that closes the system",
            ha="center", fontsize=10, style="italic", color="#555555")

    legend_items = [
        (C_DATA, "Data / infrastructure"), (C_AGENT, "LLM agent"),
        (C_DECISION, "LLM judgment"), (C_FL, "Federated learning"), (C_OUTPUT, "Output"),
    ]
    for i, (color, label) in enumerate(legend_items):
        ly = 0.55 - i * 0 + 0.3
        lx = 0.4 + i * 2.9
        ax.add_patch(FancyBboxPatch((lx, 0.15), 0.35, 0.35, boxstyle="round,pad=0.02", facecolor=color, edgecolor="#333333"))
        ax.text(lx + 0.5, 0.32, label, va="center", fontsize=9)

    fig.tight_layout()
    out_path = os.path.join(OUT_DIR, "4_agent_pipeline_architecture.png")
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"Saved -> {out_path}")


if __name__ == "__main__":
    main()
