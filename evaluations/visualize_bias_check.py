"""Shows, with real data, why account_age_days and velocity_10min were
deliberately excluded from the model's actual 5 scored features (see
branch_node/model.py's own comment block) - both are simulator artifacts
that would teach the model an unfair or meaningless shortcut rather than a
real fraud signal:
  - account_age_days: every fraud account in this simulator is freshly
    minted (age 0); every legit background account is drawn from a
    pre-existing population (age >= 30). A model trained on this would
    learn "new account = fraud," which is a coincidence of how the
    simulator mints accounts, not a real signal - and in production would
    unfairly flag every genuine new customer.
  - velocity_10min: legit accounts overwhelmingly show exactly 1 (the only
    transaction in any 10-minute window), so its variance is almost zero -
    standardizing a near-constant feature amplifies rare exceptions into
    huge, unstable values with outsized influence on the score.

Reuses the exact same population/background/fraud generation
branch_node/train_model.py's own rows_for_batch() calls, but keeps the full
TxnFeatures object (not just the 5-value array fed to the model) so both
excluded fields are visible here.

Usage:
    PYTHONPATH=. python3 evaluation/visualize_bias_check.py
Output:
    demo_visualizations/3b_bias_fairness_check.png
"""
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns

from branch_node.consumer import VelocityTracker, extract_features
from branch_node.masking import mask_event
from simulator.customers import generate_customer_population
from simulator.data_generator import generate_background_transactions
from simulator.layering_scenario import generate_layering_scenario

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "demo_visualizations")
sns.set_theme(style="whitegrid")


def build_rows():
    population = generate_customer_population(per_branch=100, seed=4242)
    account_ages = {a.account_number: a.account_age_days for a in population.accounts}
    background = generate_background_transactions(population, count=600, days=3, seed=4242)
    fraud_events, ground_truth = generate_layering_scenario(hops=4, seed=4242)
    fraud_txn_ids = set(ground_truth["fraud_txn_ids"])

    merged = sorted(background + fraud_events, key=lambda e: e["transaction"]["timestamp"])
    velocity = VelocityTracker()

    ages_fraud, ages_legit = [], []
    vel_fraud, vel_legit = [], []
    for event in merged:
        masked = mask_event(event)
        features = extract_features(masked, account_ages, velocity)
        is_fraud = masked["txn_id"] in fraud_txn_ids
        (ages_fraud if is_fraud else ages_legit).append(features.account_age_days)
        (vel_fraud if is_fraud else vel_legit).append(features.velocity_10min)
    return ages_fraud, ages_legit, vel_fraud, vel_legit


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    ages_fraud, ages_legit, vel_fraud, vel_legit = build_rows()

    fig, axes = plt.subplots(1, 2, figsize=(13, 5.5))

    ax = axes[0]
    ax.hist(ages_legit, bins=30, alpha=0.6, label=f"Legitimate (n={len(ages_legit)})", color="#4C72B0")
    ax.hist(ages_fraud, bins=30, alpha=0.8, label=f"Fraud (n={len(ages_fraud)})", color="#C44E52")
    ax.set_xlabel("Account age (days)")
    ax.set_ylabel("Count")
    ax.set_title("account_age_days — a simulator artifact, not a real signal")
    ax.legend()
    ax.annotate(
        "Every fraud account here is freshly minted (age 0)\nby construction, not because new accounts\nare inherently suspicious.\n\nExcluded from the model's scored features.",
        xy=(0.98, 0.75), xycoords="axes fraction", ha="right", fontsize=9,
        bbox=dict(boxstyle="round,pad=0.4", fc="#FFF3CD", ec="#B8862B"),
    )

    ax = axes[1]
    bins = np.arange(0, max(max(vel_legit, default=1), max(vel_fraud, default=1)) + 2) - 0.5
    ax.hist(vel_legit, bins=bins, alpha=0.6, label=f"Legitimate (n={len(vel_legit)})", color="#4C72B0", density=True)
    ax.hist(vel_fraud, bins=bins, alpha=0.8, label=f"Fraud (n={len(vel_fraud)})", color="#C44E52", density=True)
    ax.set_xlabel("Transactions from this account in the last 10 minutes")
    ax.set_ylabel("Proportion")
    ax.set_title("velocity_10min — near-zero variance, excluded to avoid noise")
    ax.legend()
    ax.annotate(
        "Legit accounts overwhelmingly show exactly 1.\nStandardizing a near-constant feature\namplifies rare exceptions into unstable,\noutsized scores.\n\nExcluded from the model's scored features.",
        xy=(0.98, 0.75), xycoords="axes fraction", ha="right", fontsize=9,
        bbox=dict(boxstyle="round,pad=0.4", fc="#FFF3CD", ec="#B8862B"),
    )

    fig.suptitle("Fairness check: why 2 computed features never reach the model's score", fontsize=13, y=1.02)
    fig.tight_layout()
    out_path = os.path.join(OUT_DIR, "3b_bias_fairness_check.png")
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"Saved -> {out_path}")


if __name__ == "__main__":
    main()
