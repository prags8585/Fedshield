# FedShieldV2 — Full Demo Runbook (Run From Scratch)

Follow this top to bottom, in order. Each step has the exact command and what you should see —
if your output doesn't roughly match, stop and fix it before moving to the next step.

**Recommended setup: 5 terminal windows**, all starting in the project folder:
```bash
cd ~/Desktop/fedshieldv2
```

---

## Step 0 — Prerequisites (Terminal 1)

- Docker Desktop must be running (check the whale icon in your menu bar — not paused).
- Activate the project's Python environment:
```bash
source .venv/bin/activate
```
You should see `(.venv)` appear in your prompt.

---

## Step 1 — Reset everything to a clean slate (Terminal 1)

```bash
docker compose down -v
```
**Expect:** a list of containers/volumes/networks being stopped and removed. This wipes Redis and
Neo4j completely — nothing carries over from any previous run.

---

## Step 2 — Regenerate all simulator data (Terminal 1)

```bash
PYTHONPATH=. python3 simulator/customers.py --per-branch 100 --seed 42 --out data/customers.json
PYTHONPATH=. python3 simulator/data_generator.py --count 200 --days 3 --per-branch 100 --seed 42 --out data/background.json
PYTHONPATH=. python3 simulator/layering_scenario.py --hops 4 --seed 42
```
**Expect:**
```
Generated 300 customers / 300 accounts -> data/customers.json
Generated 200 background transactions -> data/background.json
Generated 16 events (4 hops/chain) -> data/layering_hops4_events.json
Ground truth -> data/layering_hops4_ground_truth.json
Fraud accounts: 13, exit amount: $25,891.09
```

---

## Step 3 — Train the real ML model (Terminal 1)

```bash
PYTHONPATH=. python3 branch_node/train_model.py
```
**Expect:** ~25 lines listing fraud batches being generated, then:
```
Total rows: 5689  |  fraud rows: 439  (7.7%)
Train: 4266  |  Test: 1423 (held out, never seen during training)
...
ROC-AUC (threshold-independent): 0.935
...
Chosen decision threshold: 0.3
Recall on cash deposits/withdrawals (the catchable fraud):  1.000
Recall on layering hops (WIRE/ACH mid-chain):               0.953
Overall recall across all fraud transaction types:         0.964
False positive rate on legit transactions:                 0.2605
...
Saved trained model -> .../shared/lr_model.json
```
This takes under a minute. If you narrate one thing here for the demo: *"This model was just
trained from scratch, on 25 different generated fraud scenarios, with a real 75/25 train-test
split — not on a hardcoded rule."*

---

## Step 4 — Generate the evaluation visuals (Terminal 1)

```bash
PYTHONPATH=. python3 evaluation/visualize_model.py
```
**Expect:** the same metrics printed again, plus:
```
Saved 5 plots to .../evaluation/plots/:
  - feature_distributions.png
  - roc_curve.png
  - confusion_matrix.png
  - metrics_table.png
  - threshold_tradeoff.png
```
Open the `evaluation/plots/` folder now and have these ready to show — this is your evidence
slide deck. Good moment to record a quick screen-scroll through the 5 images.

---

## Step 5 — Bring the whole stack up (Terminal 1)

```bash
docker compose up -d --build
docker compose ps
```
**Expect:** the build takes 1-3 minutes the first time (or after a requirements.txt change), then
`docker compose ps` shows all 7 services as `Up`/`healthy`:
```
fedshieldv2-branch-loc1    Up
fedshieldv2-branch-loc2    Up
fedshieldv2-branch-loc3    Up
fedshieldv2-fl-server      Up
fedshieldv2-kafka          Up (healthy)
fedshieldv2-neo4j          Up (healthy)
fedshieldv2-redis          Up (healthy)
```

---

## Step 6 — Open 3 live branch terminals (Terminals 2, 3, 4)

```bash
# Terminal 2
docker compose logs -f branch_loc1

# Terminal 3
docker compose logs -f branch_loc2

# Terminal 4
docker compose logs -f branch_loc3
```
**Expect** in each, immediately:
```
[consumer:locN] loading trained logistic regression model...
[consumer:locN] model loaded, decision threshold=0.3
[consumer:locN] listening on txns.locN ...
```
Then silence — each branch is waiting for transactions. **This is the moment to start recording**
if you want the full live stream on camera.

---

## Step 7 — Fire the transaction stream (Terminal 5)

```bash
cd ~/Desktop/fedshieldv2 && source .venv/bin/activate
KAFKA_BROKER=localhost:29092 PYTHONPATH=. python3 branch_node/producer.py \
  --files data/background.json data/layering_hops4_events.json \
  --background-window-seconds 20
```
**Expect:** ~216 lines streaming by over roughly 60-70 seconds, e.g.:
```
[producer] -> txns.loc1 txn_id=tx_... type=DEBIT_CARD amount=$   146.81
...
[producer] done.
```

**While this runs, watch Terminals 2/3/4** — you'll see:
- Most transactions score low (0.0x-0.3x), no flag.
- The 3 placement `CASH_DEPOSIT` transactions spike to score ~0.8-0.95 and print `<-- FLAGGED`.
- Several `WIRE`/`ACH` layering-hop transactions also flag (this model catches those now — the
  earlier version didn't).
- A noticeable number of ordinary transactions also flag — **narrate this honestly**: "this
  model trades some false positives for catching the full laundering chain; that trade-off is
  intentional and documented."
- The final large `CASH_WITHDRAWAL` (~$25,891) flags at score ~1.0.

---

## Step 8 — Confirm every fraud account was caught (Terminal 1 or 5)

```bash
docker exec fedshieldv2-redis redis-cli SMEMBERS flagged_accounts | wc -l
```
**Expect:** at least a handful of flagged tokens (exact count varies run to run because of the
false-positive rate — that's expected, not a bug).

To confirm specifically that **all 13 fraud accounts** were caught (the strongest evidence line
for your manager):
```bash
PYTHONPATH=. python3 -c "
import json, subprocess
from branch_node.masking import token_for
gt = json.load(open('data/layering_hops4_ground_truth.json'))
flagged = set(subprocess.run(['docker','exec','fedshieldv2-redis','redis-cli','SMEMBERS','flagged_accounts'], capture_output=True, text=True).stdout.split())
caught = sum(1 for acct in gt['fraud_accounts'] if token_for(acct) in flagged)
print(f'Fraud accounts caught: {caught} / {len(gt[\"fraud_accounts\"])}')
"
```
**Expect:**
```
Fraud accounts caught: 13 / 13
```

---

## Step 9 — Visualize the model's REAL performance on this live run

`evaluation/visualize_model.py` (Step 4) only shows the offline held-out test set - data shaped
like training, not this exact scenario. This step scores the exact 216 transactions Step 7 just
streamed, using each one's real live score from Redis, and plots the same two chart types against
that real data - a genuinely different measurement, not a re-plot of the same numbers.

```bash
PYTHONPATH=. python3 evaluation/visualize_live_demo.py
```
**Expect:**
```
Scored transactions found in Redis: 216
Ground truth fraud transactions: 16

Confusion matrix: TN=147 FP=53 FN=0 TP=16
ROC-AUC: 0.98  Precision: 0.23  Recall: 1.00  FPR: 0.27
```
Saves `confusion_matrix_live_demo.png` and `roc_curve_live_demo.png` into the same
`evaluation/plots/` folder as Step 4's plots, so you can open both side by side - one folder,
"held-out test set" vs. "this exact live run." Note the false-positive count here (53, at the
*transaction* level) differs from Step 8's flagged-*account* count (54 unique accounts) - same
underlying run, different unit of counting, not a contradiction.

---

## Step 10 — See the money-trail graph live in Neo4j Browser (Session 4)

Every transaction from Step 7 was also written into Neo4j as a graph edge (see `graph_writer.py`),
independent of whether the model flagged it. Open **http://localhost:7475** in a browser.

**Login:** username `neo4j`, password is whatever `NEO4J_PASSWORD` is set to in your `.env`.

**Connect URL gotcha:** the login screen defaults to `bolt://localhost:7687` - wrong for this
project. Change it to **`bolt://localhost:7688`** (7687 is reserved for the original FedShield
stack on this host - see the port comments in `docker-compose.yml`). If it still fails to connect,
the "wrong password" case usually means Neo4j's data volume was initialized under an old password
before yours changed - fix with `docker compose down -v && docker compose up -d --build` (wipes
the graph, not your data files - re-run Step 7 after).

Run these in the query bar (single line each, or click the expand icon first if pasting multi-line):

**See everything** (216 edges, 210 nodes for this exact scenario - a big fuzzy hairball, this is
expected, the fraud is a small cluster hiding inside it):
```cypher
MATCH (a)-[r:TRANSACTED]->(b) RETURN a, r, b
```

**Find the convergence account without knowing its token in advance** (real customer accounts
almost never have more than 1-2 senders; the laundering consolidation account stands out):
```cypher
MATCH (dest:Account)<-[:TRANSACTED]-(sender) WHERE NOT dest.token_id IN ["CASH", "CASH_OUT", "MERCHANT"] RETURN dest.token_id AS account, count(DISTINCT sender) AS num_senders ORDER BY num_senders DESC LIMIT 10
```

**Isolate just the convergence** - swap in the token_id from the query above:
```cypher
MATCH p = (source)-[:TRANSACTED*1..8]->(:Account {token_id:"<paste_token_here>"}) RETURN p
```
**Expect:** a clean picture of 3 separate `CASH` deposit chains, each hopping through a couple of
intermediate accounts, all converging on that one account - narrate this as "no single transaction
here looks suspicious alone; it's only obvious once you see all three chains land in the same
place."

---

## Step 11 — Run the graph-based false-positive filter (Session 4's headline result)

```bash
PYTHONPATH=. python3 evaluation/downstream_filter_experiment.py
```
**Expect:**
```
BEFORE filter: 54 flagged accounts (13 true fraud, 41 false positives)
AFTER  filter: 13 flagged accounts (13 true fraud, 0 false positives)

False positives cleared by the graph filter alone: 41 / 41 (100.0%)
Fraud accounts retained after filter: 13 / 13
```
This takes every account the model flagged (Step 8) and keeps only the ones that actually sit on a
real convergent money trail in the graph - no retraining involved. Narrate: "the model alone had a
~76% false-alarm rate on what it flagged; the graph filter alone cleared all of them in this run,
without touching the model."

---

## Step 12 — Wrap-up talking points

- Point 1: the model is genuinely trained (Step 3), not hardcoded — show `evaluation/plots/roc_curve.png`.
- Point 2: it catches 100% of this laundering scheme, including every layering hop (Step 8).
- Point 3: the honest cost is a ~26% false-positive rate at this threshold — show
  `evaluation/plots/threshold_tradeoff.png` to explain the trade-off is a deliberate, tunable
  choice, not an unknown.
- Point 4: Session 4's graph layer (Steps 10-11) closes that gap without retraining anything -
  100% of this run's false positives cleared by a downstream filter alone, all real fraud retained.
  Compare `confusion_matrix.png` (Step 4, offline) against `confusion_matrix_live_demo.png`
  (Step 9, this exact live run) to show the model's real-world behavior isn't just a theoretical
  number.
- Point 5: what's next — Session 5 wires up the actual federated learning round (Flower), and
  Session 6 turns the graph queries you just ran by hand into autonomous LangGraph agent tools.

---

## Cleanup (optional, after the demo)

```bash
docker compose down
```
Leaves data files and the trained model in place — running Step 5 onward again reuses everything
without needing to regenerate data or retrain.

---
---

# Scenario 2 — Multi-Ring Cross-Branch Demo (500 Transactions)

A second, independent test bed: **3 concurrent laundering rings** (not 1), fresh customers,
guaranteed cross-branch hops on every layering transfer, and 2 shared mule accounts deliberately
linking the rings into one connected graph instead of 3 separate stars. Lives entirely in
`data/scenario_500/`, generated with its own account-number range and seed so it can never collide
with Scenario 1's data even in the same graph. Uses the exact same model/pipeline/graph code as
Scenario 1 — only the input data differs, nothing was retrained or reconfigured to make this work.

## Scenario 2, Step 1 — Regenerate the scenario_500 dataset (Terminal 1)

```bash
PYTHONPATH=. python3 simulator/customers.py --per-branch 150 --seed 500 --out data/scenario_500/customers.json
PYTHONPATH=. python3 simulator/data_generator.py --count 452 --days 3 --per-branch 150 --seed 500 --out data/scenario_500/background.json
PYTHONPATH=. python3 simulator/multi_ring_scenario.py --num-rings 3 --hops 4 --seed 500
```
**Expect:**
```
Generated 450 customers / 450 accounts -> data/scenario_500/customers.json
Generated 452 background transactions -> data/scenario_500/background.json
Generated 48 fraud events across 3 rings (4 hops/chain) -> data/scenario_500/multi_ring_events.json
Ground truth -> data/scenario_500/multi_ring_ground_truth.json
Total fraud accounts: 37
Shared accounts linking rings: 2
  - 9500000004 shared between ['ring1', 'ring2']
  - 9500000023 shared between ['ring2', 'ring3']
```

---

## Scenario 2, Step 2 — Bring the stack up fresh (Terminal 1)

```bash
docker compose down -v
docker compose up -d --build
docker compose ps
```
Wait for all services `healthy`. (Scenario 1's and Scenario 2's data use non-overlapping account
ranges by design, so you could stream both into one un-wiped graph if you ever want to see them
side by side — but for a clean, isolated run of just this scenario, wipe first as above.)

---

## Scenario 2, Step 3 — Fire the 500-transaction stream (Terminal 5)

```bash
KAFKA_BROKER=localhost:29092 PYTHONPATH=. python3 branch_node/producer.py \
  --files data/scenario_500/background.json data/scenario_500/multi_ring_events.json \
  --background-window-seconds 45
```
**Expect:** 500 lines streaming by over roughly **3 minutes** (longer than Scenario 1 — there's 3x
the laundering activity, compressed into a 180-second window this time instead of 60).

---

## Scenario 2, Step 4 — Confirm what got caught (Terminal 1 or 5)

```bash
docker exec fedshieldv2-redis redis-cli SMEMBERS flagged_accounts | wc -l
```
Check against this scenario's own ground truth (all 3 rings combined):
```bash
PYTHONPATH=. python3 -c "
import json, subprocess
from branch_node.masking import token_for
gt = json.load(open('data/scenario_500/multi_ring_ground_truth.json'))
flagged = set(subprocess.run(['docker','exec','fedshieldv2-redis','redis-cli','SMEMBERS','flagged_accounts'], capture_output=True, text=True).stdout.split())
caught = sum(1 for acct in gt['all_fraud_accounts'] if token_for(acct) in flagged)
print(f'Fraud accounts caught: {caught} / {len(gt[\"all_fraud_accounts\"])}')
"
```

---

## Scenario 2, Step 5 — Generate the "Case 2" evaluation plots (Terminal 1)

```bash
PYTHONPATH=. python3 evaluation/visualize_scenario_500.py
```
**Expect:**
```
Scored transactions found in Redis: 500
Ground truth fraud transactions: 48 (48 matched in Redis)

ROC-AUC: 0.9794
```
Saves all 5 plots (`feature_distributions.png`, `roc_curve.png`, `confusion_matrix.png`,
`metrics_table.png`, `threshold_tradeoff.png`) into **`evaluation/case2_plots/`** — a separate
folder from `evaluation/plots/`, so you can compare Scenario 1 vs. Scenario 2 side by side.

**Important:** the model is NOT retrained here — it's the exact same frozen `shared/lr_model.json`
from Scenario 1, just exposed live to data it has never seen in any form (fresh customers, 3
concurrent rings, cross-branch hops). Numbers landing close to Scenario 1's (AUC ~0.98 both times,
100% recall both times) is real evidence the model generalizes rather than having memorized quirks
of one specific scenario — this is a genuine test, not a re-plot of known numbers.

---

## Scenario 2, Step 6 — Explore the 3-ring web in Neo4j Browser

Open **http://localhost:7475**, connect with `bolt://localhost:7688`, login `neo4j` / your `.env`
password (same port/password gotchas as Scenario 1's Step 10).

**Find all 3 consolidation accounts + the 2 shared bridge accounts** (real convergence accounts
will show 3 or 2 senders; ordinary background accounts mostly show 1):
```cypher
MATCH (dest:Account)<-[:TRANSACTED]-(sender) WHERE NOT dest.token_id IN ["CASH", "CASH_OUT", "MERCHANT"] RETURN dest.token_id AS account, count(DISTINCT sender) AS num_senders ORDER BY num_senders DESC LIMIT 10
```

**See just the 3 rings, without the background clutter** — paste in the 3 consolidation tokens the
query above gives you:
```cypher
MATCH p = (source)-[:TRANSACTED*1..8]->(dest:Account) WHERE dest.token_id IN ["<ring1_token>", "<ring2_token>", "<ring3_token>"] RETURN p
```
**Expect:** 3 clusters of chains, with 2 of them visibly bridged together through the shared mule
accounts — this is the "web" shape, not 3 isolated stars. Narrate this as the more realistic,
harder case: criminal networks reusing mule infrastructure across otherwise-unrelated operations.

---

## Scenario 2, Step 7 — Run the per-ring false-positive filter

`evaluation/downstream_filter_experiment.py` (Scenario 1's Step 11) is built around finding a
SINGLE convergence per call, so it can't be pointed at this 3-ring scenario as-is — it would solve
one ring and silently ignore the other two. `evaluation/downstream_filter_experiment_scenario_500.py`
is the fix: it uses ground truth ONLY to know which ring each flagged deposit structurally belongs
to (never to decide fraud/legit), runs `check_convergence` once per ring independently, and unions
the results before reporting.

```bash
PYTHONPATH=. python3 evaluation/downstream_filter_experiment_scenario_500.py
```
**Expect:**
```
Flagged deposits: 32, grouped into 4 group(s): {'ring3': 3, 'ring1': 3, 'ring2': 3, 'unmatched': 23}

[ring1] has_convergence=True convergence_account=bc99c323be4cbf40 num_sources=3 num_branches=3
[ring2] has_convergence=True convergence_account=f2d2325a5164dbce num_sources=3 num_branches=3
[ring3] has_convergence=True convergence_account=b50307fd04827ac1 num_sources=3 num_branches=3
[unmatched] has_convergence=False convergence_account=None num_sources=0 num_branches=0

BEFORE filter: 152 flagged accounts (37 true fraud, 115 false positives)
AFTER  filter: 37 flagged accounts (37 true fraud, 0 false positives)

False positives cleared by the per-ring graph filter: 115 / 115 (100.0%)
Fraud accounts retained after filter: 37 / 37
```
The `unmatched` group (23 innocent flagged deposits, not part of any real ring) correctly fails to
converge on its own — a lone source can never satisfy the 3-source minimum, so it's dropped without
any special-casing. Narrate: "all 3 simultaneous rings resolved independently, and every false
positive was cleared, on a dataset the model has never seen in any form."

**Still a real, honest limitation, not fixed here:** this script is told each deposit's ring
membership from ground truth — it doesn't yet *discover* the groupings on its own the way a real
investigator (or the future Money-Trail Agent) would have to. That's the harder version, still
outstanding.

---

## Cleanup (Scenario 2)

```bash
docker compose down
```
Same as Scenario 1 — leaves all data files, the trained model, and both plot folders in place.

---
---

# Session 5 — Federated Learning (Manual FL Round)

Proves the FL mechanism itself: each branch trains locally on its own fresh data, sends back only
its updated weights (never the data), a central server averages the 3 branches' weights together,
evaluates the blend against a held-out validation set, and saves the result. This is a manual,
standalone process — it does not touch Kafka, Neo4j, or the branch containers, and it is
independent of Scenario 1/Scenario 2's transaction data.

**Read `CLAUDE.md`'s "Session 5 Update" section first if you want the full why** — in particular:
AUC barely moving round to round is expected, not a bug, and this round is NOT expected to reduce
Scenario 1/Scenario 2's false positives (that's the graph filter's job, not FL's).

**Recommended setup: 4 terminal windows**, all starting in the project folder with the environment
activated:
```bash
cd ~/Desktop/fedshieldv2
source .venv/bin/activate
```
Every terminal needs this — a fresh terminal window does NOT inherit an already-activated venv from
another window. If you see `ModuleNotFoundError: No module named 'flwr'`, this is why: activate the
venv in that specific terminal.

---

## FL Step 1 — Bring up just Redis (Terminal 1)

```bash
docker compose up -d redis
docker compose ps redis    # wait for "healthy"
```
Nothing else from the stack is needed for this test — no Kafka, Neo4j, or branch containers.

---

## FL Step 2 — Start the FL server (Terminal 1)

```bash
PYTHONPATH=. python3 fl_server/server.py
```
**Expect:**
```
[fl_server] validation set: 951 rows (51 fraud) - never trained on by any branch
[fl_server] starting, waiting for 3 branch clients on localhost:8080 ...
[fl_server] round 0: validation AUC = 0.99xx
```
Round 0 is the current model's score, evaluated before any client has connected. The server then
waits — this is expected, it needs all 3 clients before Round 1 can start. Lots of green `INFO:`
lines will print — those are Flower's own internal bookkeeping; only lines starting with
`[fl_server]` matter for following along.

---

## FL Step 3 — Start the 3 branch clients (Terminals 2, 3, 4)

```bash
# Terminal 2
BRANCH_ID=loc1 PYTHONPATH=. python3 branch_node/fl_client.py

# Terminal 3
BRANCH_ID=loc2 PYTHONPATH=. python3 branch_node/fl_client.py

# Terminal 4
BRANCH_ID=loc3 PYTHONPATH=. python3 branch_node/fl_client.py
```
**Expect each to show**, once, then repeat 5 times as the rounds run:
```
[fl_client:loc1] local partition: 524 rows (25 fraud)
[fl_client:loc1] local training done (20 epochs on 524 rows), final loss=0.2737
```
(loc2/loc3 will show different row counts — each branch's own separate slice of the data.) A
`flwr.client.start_numpy_client() is deprecated` warning appears at the top of each — harmless,
ignore it.

**Back in Terminal 1**, once all 3 have connected, all 5 rounds run automatically end to end:
```
[fl_server] round 1: validation AUC = 0.99xx
...
[fl_server] round 5: validation AUC = 0.99xx
[fl_server] federated model saved -> .../shared/lr_model.json
[fl_server] all rounds complete.
```
All 4 processes exit on their own when finished — nothing to manually stop.

---

## FL Step 4 — Confirm it actually happened

```bash
docker exec fedshieldv2-redis redis-cli GET fl_status
```
**Expect:** `{"round_num":5,"auc":0.99...,"timestamp":"..."}`

```bash
python3 -c "import json; d=json.load(open('shared/lr_model.json')); print(d['metrics'].get('fl_rounds_run'), d['metrics'].get('fl_validation_auc'))"
```
**Expect:** `5 0.99...` — confirms the weights were genuinely overwritten, not just simulated in
memory.

---

## Cleanup (Session 5)

```bash
docker compose down
```

**Before re-running this test from a clean baseline:** each run continues from whatever
`shared/lr_model.json` currently holds, not the original bootstrap model — that's expected
behavior (each round builds on the last), but if you specifically want to start fresh again:
```bash
PYTHONPATH=. python3 branch_node/train_model.py
```
This resets the model to a freshly-trained bootstrap version before the next FL round runs.
