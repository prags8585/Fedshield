"""Tab 4 (Federated Learning).

Two layers, deliberately scoped down from a full live-orchestration tab:

1. Static evidence panel -- real PNGs already sitting in
   evaluation/fl_before_after/ from actual FL runs in an earlier session
   (evaluation/fl_vs_isolated.py, fl_before_after.py, fl_multi_cycle_trend.py
   -- see CLAUDE.md's "Session 5 Update"). Nothing here is regenerated or
   simulated; this just lists/serves what's already on disk, the same
   pattern live_plots.py uses for Tab 1's Live Run Results.
2. Live status strip -- fl_status (round #/AUC/timestamp, written by
   fl_server/server.py, holds only the LATEST round, not history) plus each
   branch's labels:{branch} pending-retrain-buffer depth, so a viewer can
   see agent-verified labels from Tab 2/3's Verdict Agent queuing up before
   the next round drains them.

3. "Run FL Round" trigger -- starts the real, host-side Flower server
   (fl_server/server.py) plus one client terminal per branch
   (branch_node/fl_client.py), exactly the manual sequence documented in
   DEMO_RUNBOOK_FULL.md's Session 5 section, just automated the same way
   Tab 2/3's Start button automates the orchestrator listener. A round is a
   fixed NUM_ROUNDS=5 Flower round that completes on its own in well under a
   minute (tiny logistic-regression model, 20 local epochs/round) -- there
   is no separate "stop" needed, only Reset (restart the 4 terminals fresh)
   for the rare case one gets stuck.
4. Round-impact comparison -- AUC on the held-out validation set is a poor
   way to show a round mattered: it's already ~0.99 before any round runs
   (a ranking metric over a mostly-easy population, near its own ceiling),
   so a real improvement barely moves it. Reuses
   evaluation/fl_demo_impact.py's own scoring logic (never duplicated) to
   score a real demo scenario with a genuine before/after snapshot of THIS
   specific round -- flagged/false-positive/missed counts, and (the more
   sensitive signal) average confidence on real fraud vs. real legit
   transactions, which can shift even when the binary flag/no-flag count
   doesn't. Computed live inside /start, not persisted -- a fresh comparison
   every time, never the stale evaluation/fl_before_after/before.json/
   after.json snapshots the static Evidence panel above shows.
"""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
import numpy as np
import redis
from fastapi import APIRouter, HTTPException

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from branch_node.fl_data import build_validation_set
from branch_node.masking import mask_event
from branch_node.model import DEFAULT_LR_MODEL_PATH
from evaluation.fl_before_after import _compute_metrics, _plot_comparison
from evaluation.fl_demo_impact import CASES, _features_array, _score_batch
from shared.config import BRANCH_IDS
from shared.redis_keys import FL_STATUS, labels_key

from . import agent_flow, tab1

router = APIRouter(prefix="/api/tab4")

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "evaluation" / "fl_before_after"

# The 216-txn, single-ring case -- fastest to score, and the same scenario
# Tab 2 streams, so "this round's impact" lines up with whatever GUILTY
# ring you just watched feed real labels into this exact round.
IMPACT_CASE = "1"

_redis = redis.Redis(host="localhost", port=6380, decode_responses=True)

# fl_server/server.py hardcodes its own REDIS_URL (localhost:6380) since it's
# always meant to run host-side -- no override needed here. fl_client.py
# does NOT (it imports shared.config's REDIS_URL, which defaults to the
# in-Docker hostname "redis" once .env is loaded) -- see DEMO_RUNBOOK_FULL.md's
# Session 5 section: REDIS_URL must be set explicitly on this one.
FL_SERVER_CMD = b"PYTHONPATH=. python3 -u fl_server/server.py\n"

FL_CLIENT_CHANNELS = {"fl-client-loc1": "loc1", "fl-client-loc2": "loc2", "fl-client-loc3": "loc3"}


def _fl_client_cmd(branch_id: str) -> bytes:
    return (
        f"BRANCH_ID={branch_id} REDIS_URL=redis://localhost:6380 "
        f"PYTHONPATH=. python3 -u branch_node/fl_client.py\n"
    ).encode()


async def _wait_for_marker(session, marker: bytes, timeout: float = 20.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if marker in session.scrollback:
            return True
        await asyncio.sleep(0.3)
    return False


async def _wait_for_round_completion(after_floor: str, timeout: float = 60.0) -> bool:
    """fl_status only ever holds the LATEST round -- round_num == 5 alone
    isn't enough to know THIS invocation finished (a stale round-5 from a
    previous click would already read that way), so also require its
    timestamp to be newer than the floor captured right before this round
    was kicked off.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        raw = _redis.get(FL_STATUS)
        if raw:
            try:
                status = json.loads(raw)
                if status.get("round_num") == 5 and status.get("timestamp", "") > after_floor:
                    return True
            except json.JSONDecodeError:
                pass
        await asyncio.sleep(0.5)
    return False


def _load_model_payload() -> dict:
    with open(DEFAULT_LR_MODEL_PATH) as f:
        return json.load(f)


def _score_impact_case(payload: dict, case_key: str = IMPACT_CASE) -> dict:
    """Reuses evaluation/fl_demo_impact.py's own feature-extraction and
    scoring functions directly (never reimplemented) against a real demo
    scenario's actual transactions -- the same reasoning that script was
    built for: AUC is a poor way to show a round mattered once it's already
    near its ceiling, but flagged/false-positive counts and average
    confidence on real fraud vs. real legit can still shift meaningfully.
    """
    case = CASES[case_key]
    events = []
    for path in case["events"]:
        events.extend(json.load(open(REPO_ROOT / path)))
    ground_truth = json.load(open(REPO_ROOT / case["ground_truth"]))
    fraud_txn_ids = set(ground_truth[case["fraud_field"]])

    masked_events = [mask_event(e) for e in events]
    X = [_features_array(m) for m in masked_events]
    y = np.array([1 if m["txn_id"] in fraud_txn_ids else 0 for m in masked_events])

    probs = _score_batch(payload["weight"], payload["bias"], payload["mean"], payload["std"], X)
    preds = (probs >= payload["threshold"]).astype(int)

    tp = int(((preds == 1) & (y == 1)).sum())
    fp = int(((preds == 1) & (y == 0)).sum())
    fn = int(((preds == 0) & (y == 1)).sum())

    return {
        "flagged": tp + fp,
        "false_positives": fp,
        "missed_fraud": fn,
        "avg_score_fraud": float(probs[y == 1].mean()) if (y == 1).any() else None,
        "avg_score_legit": float(probs[y == 0].mean()) if (y == 0).any() else None,
    }


def _plot_case1_impact(before: dict, after: dict, out_path: Path) -> None:
    """Same 2-panel style as the earlier honest 1-cycle chart, but generated
    fresh from THIS round's real before/after impact dicts every time - see
    CLAUDE.md's dashboard note on why the static Evidence panel used to go
    stale (comparison_table.png only updated when someone remembered to
    manually re-run evaluation/fl_before_after.py). Overwrites the same
    filename each round, so the Evidence panel always shows the latest one,
    never a stale earlier round.
    """
    x = ["Before FL Round", "After FL Round\n(this round's real labels, if any)"]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8))

    ax = axes[0]
    fp = [before["false_positives"], after["false_positives"]]
    ax.plot(x, fp, marker="o", color="#C44E52", linewidth=2.2, markersize=8)
    for xi, yi in zip(x, fp):
        ax.annotate(str(yi), (xi, yi), textcoords="offset points", xytext=(0, 10), ha="center", fontsize=11)
    ax.set_title(f"{CASES[IMPACT_CASE]['label']}: False Positives")
    ax.set_ylabel("False positive count")
    ax.margins(y=0.3)

    ax = axes[1]
    conf = [before["avg_score_fraud"], after["avg_score_fraud"]]
    ax.plot(x, conf, marker="o", color="#55A868", linewidth=2.2, markersize=8)
    for xi, yi in zip(x, conf):
        ax.annotate(f"{yi:.4f}", (xi, yi), textcoords="offset points", xytext=(0, 10), ha="center", fontsize=11)
    ax.set_title(f"{CASES[IMPACT_CASE]['label']}: Avg. Confidence Score on REAL Fraud")
    ax.set_ylabel("Average score (0-1)")
    ax.margins(y=0.3)

    fig.suptitle(f"FL Impact - Latest Round, {datetime.now().strftime('%Y-%m-%d %H:%M')}", fontsize=14, y=1.03)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


@router.post("/start")
async def start_fl_round():
    if not await agent_flow.docker_healthy():
        raise HTTPException(
            409,
            "docker stack not healthy -- complete Tab 1's data-gen/up sequence first",
        )

    before_impact = _score_impact_case(_load_model_payload())
    X_val, y_val = build_validation_set()
    before_val_metrics = _compute_metrics(_load_model_payload(), X_val, y_val)
    with open(EVIDENCE_DIR / "before.json", "w") as f:
        json.dump(before_val_metrics, f, indent=2)
    start_floor = datetime.now(timezone.utc).isoformat()

    # Always a fresh shell, not an "if not already running" check like Tab
    # 2/3's branch-log tailing -- a completed round's fl_server.py process
    # already exited on its own (NUM_ROUNDS is fixed), so there's nothing
    # long-running to detect/reuse between clicks.
    fl_server = tab1.registry.get("fl-server")
    fl_server.start()
    fl_server.write(FL_SERVER_CMD)

    confirmed = await _wait_for_marker(fl_server, b"waiting for 3 branch clients", timeout=20.0)
    if not confirmed:
        raise HTTPException(
            500,
            "fl_server did not confirm startup within 20s -- check the FL Server terminal",
        )

    for channel, branch_id in FL_CLIENT_CHANNELS.items():
        client = tab1.registry.get(channel)
        client.start()
        client.write(_fl_client_cmd(branch_id))

    impact = None
    if await _wait_for_round_completion(start_floor, timeout=60.0):
        after_model_payload = _load_model_payload()
        after_impact = _score_impact_case(after_model_payload)
        impact = {
            "case_label": CASES[IMPACT_CASE]["label"],
            "before": before_impact,
            "after": after_impact,
        }

        # Regenerate the static Evidence panel from THIS round, not whatever
        # someone last ran by hand - closes the staleness gap that showed a
        # 3-week-old comparison_table.png next to a freshly-run live round.
        after_val_metrics = _compute_metrics(after_model_payload, X_val, y_val)
        with open(EVIDENCE_DIR / "after.json", "w") as f:
            json.dump(after_val_metrics, f, indent=2)
        _plot_comparison(before_val_metrics, after_val_metrics)
        _plot_case1_impact(before_impact, after_impact, EVIDENCE_DIR / "impact_1cycle_today.png")

    return {"status": "started", "impact": impact}


@router.post("/reset")
async def reset_fl_terminals():
    for channel in ["fl-server", *FL_CLIENT_CHANNELS.keys()]:
        tab1.registry.get(channel).start()
    return {"status": "reset"}

_EVIDENCE_FILES = [
    ("comparison_table.png", "Before / After Metrics (latest real round)"),
    ("impact_1cycle_today.png", "Case 1 Impact - 1 Real Cycle (today's GUILTY-labeled ring)"),
    ("multi_cycle_trend.png", "Multi-Cycle Trend (real, 4 cycles, earlier session)"),
]


@router.get("/evidence")
async def evidence():
    items = [
        {
            "filename": filename,
            "label": label,
            "url": f"/fl_evidence/{filename}",
            "mtime": (EVIDENCE_DIR / filename).stat().st_mtime,
        }
        for filename, label in _EVIDENCE_FILES
        if (EVIDENCE_DIR / filename).exists()
    ]

    snapshots = {}
    for phase in ("before", "after"):
        path = EVIDENCE_DIR / f"{phase}.json"
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            snapshots[phase] = {"auc": data.get("auc"), "precision": data.get("precision"), "recall": data.get("recall"), "fpr": data.get("fpr")}

    return {"items": items, "snapshots": snapshots}


@router.get("/status")
async def status():
    raw = _redis.get(FL_STATUS)
    fl_status = None
    if raw:
        try:
            fl_status = json.loads(raw)
        except json.JSONDecodeError:
            fl_status = None

    pending_labels = {branch_id: _redis.llen(labels_key(branch_id)) for branch_id in BRANCH_IDS}

    return {"fl_status": fl_status, "pending_labels": pending_labels}
