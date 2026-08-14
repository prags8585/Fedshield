"""Flower server for Session 5's manual FL round.

Starts from the CURRENT live model's weights (not a random initialization -
this is meant to improve the existing model, not train a new one from
scratch), waits for all 3 branch clients to connect, runs a fixed number of
FedAvg rounds, and after each round evaluates the combined model against a
small held-out validation set (built by branch_node/fl_data.py, untouched by
any branch's local training) to compute AUC - written to Redis's fl_status
key the same way every other signal in this project is surfaced.

When the final round completes, the federated weights overwrite
shared/lr_model.json's weight/bias (same mean/std/threshold/feature_order)
so branch containers pick up the improved model the next time they restart.

Run manually (see DEMO_RUNBOOK.md's Session 5 section):
    python fl_server/server.py
then, in 3 more terminals:
    BRANCH_ID=loc1 python branch_node/fl_client.py
    BRANCH_ID=loc2 python branch_node/fl_client.py
    BRANCH_ID=loc3 python branch_node/fl_client.py
"""
import json
import os
from datetime import datetime, timezone

import flwr as fl
import numpy as np
import redis
import torch
from flwr.common import ndarrays_to_parameters
from sklearn.metrics import roc_auc_score

from branch_node.fl_data import build_validation_set
from branch_node.model import DEFAULT_LR_MODEL_PATH, LogisticFraudModel
from shared.redis_keys import FL_STATUS
from shared.schemas import FLStatus

NUM_ROUNDS = 5
# Host-facing addresses for this manual, host-side round - shared/config.py's
# FL_SERVER_ADDRESS/REDIS_URL resolve to in-Docker hostnames from .env.
FL_SERVER_ADDRESS = os.getenv("FL_SERVER_ADDRESS_HOST", "localhost:8080")
REDIS_URL = "redis://localhost:6380"


def _load_current_model_payload() -> dict:
    with open(DEFAULT_LR_MODEL_PATH) as f:
        return json.load(f)


def _save_federated_model(parameters: list, payload: dict, final_auc: float) -> None:
    updated = {
        **payload,
        "weight": np.array(parameters[0]).reshape(-1).tolist(),
        "bias": float(np.array(parameters[1]).reshape(-1)[0]),
        "metrics": {**payload["metrics"], "fl_rounds_run": NUM_ROUNDS, "fl_validation_auc": final_auc},
    }
    with open(DEFAULT_LR_MODEL_PATH, "w") as f:
        json.dump(updated, f, indent=2)
    print(f"[fl_server] federated model saved -> {DEFAULT_LR_MODEL_PATH}")


def _make_evaluate_fn(payload: dict, redis_client: redis.Redis):
    mean, std = payload["mean"], payload["std"]
    X_val, y_val = build_validation_set()
    print(f"[fl_server] validation set: {len(X_val)} rows ({sum(y_val)} fraud) - never trained on by any branch")

    X_val_t = torch.tensor(X_val, dtype=torch.float32)
    y_val_np = np.array(y_val)
    mean_t = torch.tensor(mean, dtype=torch.float32)
    std_t = torch.tensor(std, dtype=torch.float32)

    def evaluate_fn(server_round, parameters, config):
        model = LogisticFraudModel()
        with torch.no_grad():
            model.linear.weight.copy_(torch.tensor(np.array(parameters[0]), dtype=torch.float32))
            model.linear.bias.copy_(torch.tensor(np.array(parameters[1]), dtype=torch.float32))
        model.eval()

        with torch.no_grad():
            X_std = (X_val_t - mean_t) / std_t
            probs = torch.sigmoid(model(X_std)).numpy()

        auc = roc_auc_score(y_val_np, probs) if len(set(y_val_np.tolist())) > 1 else float("nan")

        status = FLStatus(round_num=server_round, auc=float(auc), timestamp=datetime.now(timezone.utc).isoformat())
        redis_client.set(FL_STATUS, status.model_dump_json())
        print(f"[fl_server] round {server_round}: validation AUC = {auc:.4f}")

        if server_round == NUM_ROUNDS:
            _save_federated_model(parameters, payload, float(auc))

        return 0.0, {"auc": float(auc)}

    return evaluate_fn


def main():
    payload = _load_current_model_payload()
    initial_parameters = ndarrays_to_parameters([
        np.array([payload["weight"]], dtype=np.float32),
        np.array([payload["bias"]], dtype=np.float32),
    ])

    redis_client = redis.from_url(REDIS_URL)
    evaluate_fn = _make_evaluate_fn(payload, redis_client)

    strategy = fl.server.strategy.FedAvg(
        fraction_fit=1.0,
        fraction_evaluate=0.0,  # centralized evaluate_fn only - clients' own evaluate() is a required-but-unused stub
        min_fit_clients=3,
        min_available_clients=3,
        evaluate_fn=evaluate_fn,
        initial_parameters=initial_parameters,
        # Tells each client which round this is, so fl_data.build_branch_partition
        # can shift its seed range and hand out genuinely new transactions each
        # round instead of the same fixed batch repeated every time.
        on_fit_config_fn=lambda server_round: {"server_round": server_round},
    )

    print(f"[fl_server] starting, waiting for 3 branch clients on {FL_SERVER_ADDRESS} ...")
    fl.server.start_server(
        server_address=FL_SERVER_ADDRESS,
        config=fl.server.ServerConfig(num_rounds=NUM_ROUNDS),
        strategy=strategy,
    )
    print("[fl_server] all rounds complete.")


if __name__ == "__main__":
    main()
