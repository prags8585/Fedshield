"""Per-branch real-time structuring model: a genuine Logistic Regression
(implemented as a single PyTorch nn.Linear + sigmoid - the "logistic" option
CLAUDE.md's architecture table explicitly allows, and the natural choice for
FedAvg weight-averaging in Session 5).

Unlike the earlier Session 3 approach, this model is NOT trained on a
handwritten heuristic rule at container startup. It is trained OFFLINE, once,
by train_model.py, on a large genuinely-generated and genuinely-labeled
dataset (many independent fraud scenarios with different people/amounts/hop
counts, many independent background batches) with a real train/test split.
Branches load the resulting fixed, already-trained weights from
shared/lr_model.json at startup - see train_model.py for the full honest
write-up of what this model can and cannot catch.
"""
import json
import os

import torch
import torch.nn as nn

from shared.schemas import TxnFeatures

FEATURE_ORDER = [
    "amount_ratio_to_threshold",
    "is_cash",
    "hour_of_day",
    "day_of_week",
    "is_transfer_out",
]
# Two features are deliberately excluded here (both computed/stored in
# TxnFeatures for evidence/reporting - just not fed into the score):
#
# account_age_days: in our simulator, every fraud account is freshly minted
# (age 0) and every legit account is drawn from a pre-existing population
# (age >= 31), which let the model "cheat" by learning a simulator artifact
# instead of a real fraud signal - and would be a genuine fairness problem
# (punishing any real customer who just opened an account) in production.
#
# velocity_10min: almost every transaction in our simulated data (fraud AND
# legit alike) is the only transaction its account makes in any 10-minute
# window, so this feature has almost zero variance (std ~0.03, vs 0.28-0.40
# for the others). Standardizing a near-constant feature amplifies its rare
# exceptions into huge, unstable values - a legit account that happens to
# transact twice in 10 minutes got blown up to roughly +32 standard
# deviations, giving it outsized and arbitrary influence on that one score.
# Removed until it's computed in a way that's actually informative (e.g.
# cross-account velocity) rather than mostly-constant-with-noisy-outliers.

DEFAULT_LR_MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "shared", "lr_model.json")


class LogisticFraudModel(nn.Module):
    """A single linear layer + sigmoid - mathematically a logistic
    regression, just expressed in PyTorch so its weights are directly
    FedAvg-averageable in Session 5 without any conversion step.
    """

    def __init__(self):
        super().__init__()
        self.linear = nn.Linear(len(FEATURE_ORDER), 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.linear(x).squeeze(-1)  # returns logits, not probabilities


def features_to_array(f: TxnFeatures) -> list:
    """Raw (unnormalized) feature values, in FEATURE_ORDER. Standardization
    (subtract mean, divide by std) happens separately using statistics
    learned from the training set - see LRScorer.score() below - so this
    function must stay identical between training and inference.
    """
    return [
        f.amount_ratio_to_threshold,
        float(f.is_cash),
        float(f.hour_of_day),
        float(f.day_of_week),
        float(f.is_transfer_out),
    ]


class LRScorer:
    """Bundles the trained model with the standardization stats and decision
    threshold it was trained/tuned with, so callers just call .score(features).
    """

    def __init__(self, model: LogisticFraudModel, mean: list, std: list, threshold: float, metrics: dict = None):
        self.model = model
        self.mean = torch.tensor(mean, dtype=torch.float32)
        self.std = torch.tensor(std, dtype=torch.float32)
        self.threshold = threshold
        self.metrics = metrics or {}
        self.model.eval()

    def score(self, features: TxnFeatures) -> float:
        with torch.no_grad():
            x = torch.tensor(features_to_array(features), dtype=torch.float32)
            x_std = (x - self.mean) / self.std
            logit = self.model(x_std.unsqueeze(0))
            return float(torch.sigmoid(logit).item())

    def score_batch(self, X: list):
        """Vectorized version of score(), for a list of raw feature arrays
        (FEATURE_ORDER order) rather than one-at-a-time TxnFeatures objects -
        used by evaluation/visualize_model.py to score a whole test set at once.
        """
        with torch.no_grad():
            x = torch.tensor(X, dtype=torch.float32)
            x_std = (x - self.mean) / self.std
            logits = self.model(x_std)
            return torch.sigmoid(logits).numpy()


def load_trained_model(path: str = None) -> LRScorer:
    path = path or DEFAULT_LR_MODEL_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No trained model found at {path}. Run `python3 branch_node/train_model.py` first "
            "to generate it (see that file for what it trains on and why)."
        )
    with open(path) as f:
        payload = json.load(f)

    if payload["feature_order"] != FEATURE_ORDER:
        raise ValueError("lr_model.json was trained with a different feature order than model.py currently uses.")

    model = LogisticFraudModel()
    with torch.no_grad():
        model.linear.weight.copy_(torch.tensor([payload["weight"]], dtype=torch.float32))
        model.linear.bias.copy_(torch.tensor([payload["bias"]], dtype=torch.float32))

    return LRScorer(model, payload["mean"], payload["std"], payload["threshold"], payload.get("metrics"))
