"""Live confusion-matrix / ROC / feature-distribution / metrics / threshold
plots for whatever's actively streaming through the live stack right now,
kept as an append-only history -- one entry per distinct data-generation run
(216 case, 1500 case, or a repeat of either), so completed runs stay visible
for side-by-side comparison instead of being replaced by the next one.

Adapts two existing one-off scripts into a single, scenario-auto-detecting
endpoint the dashboard can poll:
  - evaluation/visualize_live_demo.py (216 case: confusion matrix + ROC only)
  - evaluation/visualize_scenario_1500.py (1500 case: full 5-plot set)

Both of those are meant to be run once, by hand, right after a fresh
producer run. Redis score:* keys never get cleared just by regenerating
data (txn_ids are random each run), so unlike those scripts this filters to
only txn_ids present in the CURRENT on-disk background/events files -- a
stale key from an earlier run or the other scenario can never leak in.

A "run" is identified by its ground-truth file's mtime: every time the data
generation scripts are re-run, that file gets rewritten with a fresh mtime,
which is what starts a new history entry here. An entry is only added once
it has real scored data (so switching scenarios never creates an empty
placeholder) and, once added, is never touched again after the active
scenario moves on to a newer mtime -- it stays frozen as-is.
"""
import json
from pathlib import Path

import numpy as np
import redis
from fastapi import APIRouter
from sklearn.metrics import roc_auc_score

from branch_node.model import features_to_array
from evaluation.visualize_model import (
    plot_confusion_matrix,
    plot_feature_distributions,
    plot_metrics_table,
    plot_roc_curve,
    plot_threshold_tradeoff,
)
from shared.schemas import ScoreRecord

REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_PLOTS_DIR = REPO_ROOT / "evaluation" / "plots_live"
MODEL_PATH = REPO_ROOT / "shared" / "lr_model.json"

SCENARIOS = [
    {
        "name": "216-case",
        "ground_truth": REPO_ROOT / "data" / "layering_hops4_ground_truth.json",
        "fraud_key": "fraud_txn_ids",
        "background": REPO_ROOT / "data" / "background.json",
        "events": REPO_ROOT / "data" / "layering_hops4_events.json",
    },
    {
        "name": "1500-case",
        "ground_truth": REPO_ROOT / "data" / "scenario_1500" / "multi_ring_ground_truth.json",
        "fraud_key": "all_fraud_txn_ids",
        "background": REPO_ROOT / "data" / "scenario_1500" / "background.json",
        "events": REPO_ROOT / "data" / "scenario_1500" / "multi_ring_events.json",
    },
]

PLOT_FILES = [
    ("roc_curve.png", "ROC Curve"),
    ("confusion_matrix.png", "Confusion Matrix"),
    ("metrics_table.png", "Metrics by Threshold"),
    ("threshold_tradeoff.png", "Threshold Trade-off"),
    ("feature_distributions.png", "Feature Distributions"),
]

router = APIRouter(prefix="/api/tab1")

_redis = redis.Redis(host="localhost", port=6380)
_history = []  # each: {"mtime": float, "index": int, "scenario": str, "total": int, "items": [...]}
_last_rendered_total = {}  # mtime -> total, to skip re-rendering when nothing new happened


def _active_scenario():
    """Whichever ground-truth file exists and was written most recently."""
    candidates = [s for s in SCENARIOS if s["ground_truth"].exists()]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s["ground_truth"].stat().st_mtime)


def _current_txn_ids(scenario) -> set:
    txn_ids = set()
    for path in (scenario["events"], scenario["background"]):
        if not path.exists():
            continue
        with open(path) as f:
            events = json.load(f)
        txn_ids.update(e["transaction"]["txn_id"] for e in events)
    return txn_ids


def _public_history() -> list:
    return [
        {"scenario": h["scenario"], "total": h["total"], "items": h["items"]}
        for h in _history
        if "items" in h
    ]


@router.get("/live-plots")
async def live_plots():
    scenario = _active_scenario()
    if scenario is None or not MODEL_PATH.exists():
        return {"history": _public_history()}

    with open(scenario["ground_truth"]) as f:
        fraud_txn_ids = set(json.load(f)[scenario["fraud_key"]])
    with open(MODEL_PATH) as f:
        threshold = json.load(f)["threshold"]

    current_txn_ids = _current_txn_ids(scenario)

    try:
        records = []
        for key in _redis.scan_iter("score:*"):
            raw = _redis.get(key)
            if not raw:
                continue
            record = ScoreRecord.model_validate_json(raw)
            if record.txn_id in current_txn_ids:
                records.append(record)
    except redis.exceptions.ConnectionError:
        return {"history": _public_history()}

    total = len(records)
    y = np.array([1 if rec.txn_id in fraud_txn_ids else 0 for rec in records])

    if total < 2 or len(set(y.tolist())) < 2:
        return {"history": _public_history()}

    gt_mtime = scenario["ground_truth"].stat().st_mtime
    entry = next((h for h in _history if h["mtime"] == gt_mtime), None)
    if entry is None:
        entry = {"mtime": gt_mtime, "index": len(_history), "scenario": scenario["name"], "total": 0}
        _history.append(entry)

    if _last_rendered_total.get(gt_mtime) == total:
        return {"history": _public_history()}
    _last_rendered_total[gt_mtime] = total

    X = np.array([features_to_array(rec.features) for rec in records])
    probs = np.array([rec.score for rec in records])
    is_cash = [bool(rec.features.is_cash) for rec in records]
    preds = (probs >= threshold).astype(int)
    auc_score = roc_auc_score(y, probs)

    subtitle = f"live run, {total} transactions ({scenario['name']})"
    run_dir = LIVE_PLOTS_DIR / str(entry["index"])
    run_dir.mkdir(parents=True, exist_ok=True)

    plot_feature_distributions(X, y, output_dir=str(run_dir), subtitle=subtitle)
    plot_roc_curve(y, probs, auc_score, output_dir=str(run_dir), title=f"ROC Curve — {subtitle}")
    plot_confusion_matrix(
        y, preds, threshold, output_dir=str(run_dir),
        title=f"Confusion Matrix — {subtitle} (threshold = {threshold})",
    )
    plot_metrics_table(
        y, is_cash, probs, thresholds=[0.5, 0.4, threshold, 0.2, 0.1],
        output_dir=str(run_dir), subtitle=subtitle,
    )
    plot_threshold_tradeoff(
        y, probs, output_dir=str(run_dir),
        title=f"Recall vs. Precision vs. FPR — {subtitle}",
    )

    entry["total"] = total
    entry["items"] = [
        {
            "filename": filename,
            "label": label,
            "url": f"/plots_live/{entry['index']}/{filename}",
            "mtime": (run_dir / filename).stat().st_mtime,
        }
        for filename, label in PLOT_FILES
        if (run_dir / filename).exists()
    ]

    return {"history": _public_history()}
