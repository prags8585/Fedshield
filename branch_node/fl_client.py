"""Flower client: wraps the existing PyTorch LogisticFraudModel so one
branch can participate in an FL round. Each round: load the server's
current global weights, run a SHORT local practice pass (a handful of
epochs, not a from-scratch training) on this branch's own local data
partition only, send back just the updated weights - never the data.

Feature standardization (mean/std) stays FIXED, read from the currently-live
shared/lr_model.json - every branch must normalize inputs the same way for
weight-averaging to be meaningful; only the weights themselves get
federated, never the preprocessing.
"""
import json
import os

import flwr as fl
import torch
import torch.nn as nn

import redis

from branch_node.fl_data import build_branch_partition
from branch_node.model import DEFAULT_LR_MODEL_PATH, LogisticFraudModel
from shared.config import REDIS_URL
from shared.redis_keys import labels_key

BRANCH_ID = os.getenv("BRANCH_ID", "loc1")
LOCAL_EPOCHS = 20
# Host-facing address for this manual, host-side round (see DEMO_RUNBOOK.md's
# Session 5 section) - shared/config.py's FL_SERVER_ADDRESS resolves to
# "fl_server:8080" from .env, which only resolves inside the Docker network.
FL_SERVER_ADDRESS = os.getenv("FL_SERVER_ADDRESS_HOST", "localhost:8080")


def _load_mean_std() -> tuple:
    with open(DEFAULT_LR_MODEL_PATH) as f:
        payload = json.load(f)
    return payload["mean"], payload["std"]


def get_model_parameters(model: LogisticFraudModel) -> list:
    return [model.linear.weight.detach().numpy(), model.linear.bias.detach().numpy()]


def set_model_parameters(model: LogisticFraudModel, parameters: list) -> None:
    with torch.no_grad():
        model.linear.weight.copy_(torch.tensor(parameters[0], dtype=torch.float32))
        model.linear.bias.copy_(torch.tensor(parameters[1], dtype=torch.float32))


class BranchFLClient(fl.client.NumPyClient):
    def __init__(self, branch_id: str):
        self.branch_id = branch_id
        self.model = LogisticFraudModel()
        self.mean, self.std = _load_mean_std()

    def get_parameters(self, config):
        return get_model_parameters(self.model)

    def fit(self, parameters, config):
        set_model_parameters(self.model, parameters)

        # Regenerated every round (server tells us which one via on_fit_config_fn)
        # so each round trains on genuinely new transactions, not the same fixed
        # batch repeated - see fl_data.build_branch_partition's round_num.
        server_round = config.get("server_round", 0)

        # Non-destructive peek (LLEN, not LPOP) just for the log line below - the actual
        # drain happens once, inside build_branch_partition(include_real_labels=True), so
        # peeking here never double-consumes anything. See CLAUDE.md's "Session 6 Update".
        r = redis.from_url(REDIS_URL)
        n_real_waiting = r.llen(labels_key(self.branch_id))

        X_list, y_list = build_branch_partition(self.branch_id, round_num=server_round, include_real_labels=True)
        print(
            f"[fl_client:{self.branch_id}] round {server_round}: local partition {len(X_list)} rows "
            f"({sum(y_list)} fraud) - includes {n_real_waiting} real agent-verified label(s)"
        )

        X = torch.tensor(X_list, dtype=torch.float32)
        y = torch.tensor(y_list, dtype=torch.float32)
        mean_t = torch.tensor(self.mean, dtype=torch.float32)
        std_t = torch.tensor(self.std, dtype=torch.float32)
        X_std = (X - mean_t) / std_t

        n_pos = max(y.sum().item(), 1.0)
        n_neg = max(len(y) - y.sum().item(), 1.0)
        pos_weight = torch.tensor(n_neg / n_pos)  # counteracts local class imbalance, same as offline training

        optimizer = torch.optim.Adam(self.model.parameters(), lr=0.05)
        loss_fn = nn.BCEWithLogitsLoss(pos_weight=pos_weight)

        self.model.train()
        loss_value = None
        for _ in range(LOCAL_EPOCHS):
            optimizer.zero_grad()
            loss = loss_fn(self.model(X_std), y)
            loss.backward()
            optimizer.step()
            loss_value = loss.item()
        self.model.eval()

        print(f"[fl_client:{self.branch_id}] local training done ({LOCAL_EPOCHS} epochs on {len(X_list)} rows), final loss={loss_value:.4f}")
        return get_model_parameters(self.model), len(X_list), {}

    def evaluate(self, parameters, config):
        # Real evaluation is centralized in fl_server's evaluate_fn against a
        # held-out set no branch ever trains on - this per-client evaluate is
        # only implemented because NumPyClient's interface requires it.
        set_model_parameters(self.model, parameters)
        return 0.0, 1, {}


if __name__ == "__main__":
    client = BranchFLClient(BRANCH_ID)
    fl.client.start_numpy_client(server_address=FL_SERVER_ADDRESS, client=client)
