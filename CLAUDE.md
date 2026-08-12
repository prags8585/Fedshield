# FedShieldV2 — Project Context for Claude

This project is independent from the original FedShield. Do not reuse assumptions from that
project unless explicitly restated here — several key design decisions are deliberately different.

## What This Project Is
FedShieldV2 is an autonomous, agentic anti-money-laundering (AML) system for a single **US-based
bank** with 3 branches (`loc1`, `loc2`, `loc3`). All amounts are USD; identifiers and thresholds
follow US banking/regulatory conventions (see below) — this is a deliberate divergence from the
original FedShield, which was India-focused. It focuses on exactly one fraud typology for now —
**Case 1: deposit tracing (placement → layering → integration)** — and detects it through a
fully autonomous multi-agent pipeline that live-traces money through a shared graph, rather
than a human running link-analysis queries after the fact.

The system is built for two audiences at once: a working capstone demo, and a research paper
with two specific contributions (below). Every design decision should serve both.

## Research Contributions (what makes this novel — do not lose sight of these)
1. **Agent-verified active learning for federated AML.** Real AML labels are scarce and
   months-delayed (SARs). Here, the Money-Trail Agent's live, evidence-backed verdicts become
   fresh training labels fed back into federated retraining — closing the label-scarcity gap
   without any branch sharing raw transactions.
2. **Adversarial multi-agent verification.** A Prosecutor/Defense/Judge structure gates verdict
   quality before a label is trusted enough to enter the FL retraining loop, reducing false
   positives compared to a single agent deciding alone.

Neither contribution is "we used cool tools together" — it is specifically the closed loop
(investigate → verify → label → retrain → improve) that is the novel claim. State it that way
in any writeup.

## The Fraud Scenario (Case 1)
- Single bank, 3 branches, simulating siloed core-banking systems (realistic: legacy IT /
  vendor fragmentation causes this even within one institution, not just regulation).
- Three unrelated individuals p1, p2, p3, each with clean history, each with one account at a
  different branch.
- **Placement:** each deposits cash just under the **$10,000 CTR (Currency Transaction Report)
  threshold** — the real US regulatory trigger for mandatory cash-transaction reporting — at
  their own branch, all within a short window (e.g. 40 minutes). Placement amounts: **$8,000 -
  $9,800** per deposit.
- **Layering:** each placement account's funds move through a chain of intermediate accounts —
  chain length is CONFIGURABLE (N hops, not fixed at 2), e.g.
  `a1(p1 deposit) → a2 → a3 → a4 → a5`, `b1(p2 deposit) → b2 → b3 → b4 → a5`,
  `c1(p3 deposit) → c2 → c3 → c4 → a5`.
- **Integration:** all three chains converge on one consolidation account (`a5` above), which
  eventually makes a large cash withdrawal (exit).
- Ground truth (which tokens/accounts are actually part of the fraud) is logged by the
  simulator separately and NEVER exposed to any model or agent — used only for evaluation.

## Architecture — 6 core technologies, one job each

| Tech | Role |
|---|---|
| Docker | isolates each branch's RAW data and local model only — see isolation rule below |
| Kafka | per-branch streaming transport (`txns.loc1`, `txns.loc2`, `txns.loc3`) |
| PyTorch logistic regression (single linear layer + sigmoid) | per-branch real-time structuring score on every transaction — see "Session 3 Update" below, this replaced an earlier MLP-shaped bootstrap approach |
| Flower | FedAvg aggregation of the PyTorch model's weights across branches |
| Neo4j | ONE shared, anonymized money-trail graph — see isolation rule below |
| Redis | shared whiteboard + event bus (trigger, scores, flags, evidence, verdicts, reports, fl_status) |
| LangGraph + local Ollama (`qwen2.5:7b`) | the autonomous multi-agent investigation brain |

## Session 3 Update — Model Retrained, Read Before Touching `branch_node/model.py`

The scoring model was reworked after Session 3's initial pass. Full detail (why, how, both bugs
found and fixed) is in `session3_updated.md` — the short version any future session needs:

- **The model is a genuinely trained Logistic Regression**, not a bootstrap model trained on a
  handwritten heuristic rule. `branch_node/train_model.py` generates a diverse labeled dataset
  offline (25 independent fraud scenarios with varying hop-counts/amounts/timing, 10
  background-only batches), trains with a real 75/25 train/test split, and saves the result to
  `shared/lr_model.json`. Branches only ever *load* this file at startup — they never train
  anything themselves.
- **The feature set is 5, not 7.** `account_age_days` and `velocity_10min` were both removed
  after being caught as, respectively, a data leak (every fraud account is freshly minted in the
  simulator, every legit account is pre-existing — a simulator artifact, not a real signal) and a
  near-zero-variance feature whose standardization was amplifying rare values into unstable
  outliers. Current feature list: `amount_ratio_to_threshold`, `is_cash`, `hour_of_day`,
  `day_of_week`, `is_transfer_out`. Both dropped fields are still computed and stored in
  `TxnFeatures` for evidence purposes — just not fed into the score. **Do not re-add either
  without re-reading why they were removed.**
- **The fraud threshold is no longer the `.env` `FRAUD_THRESHOLD=0.75` constant.** It's now
  empirically tuned during training (currently 0.3, chosen to maximize recall on cash
  deposits/withdrawals) and stored inside `shared/lr_model.json` itself — `consumer.py` reads
  `scorer.threshold` from the loaded model, not from config. `shared/config.py`'s
  `FRAUD_THRESHOLD` constant is now unused dead config in the live scoring path.
- **Honest performance, not perfect:** ROC-AUC ≈ 0.935. At threshold 0.3: ~95-96% overall
  recall (100% on cash deposits/withdrawals, ~93-95% on layering hops), but a ~27%
  false-positive rate on legit traffic. This is a genuine precision/recall trade-off, not a bug —
  see `session3_updated.md` and `evaluation/visualize_model.py`'s output for the full picture.
- **New files not yet reflected in the original folder structure below:**
  `branch_node/train_model.py` (offline training script) and `evaluation/visualize_model.py`
  (generates ROC curve / confusion matrix / feature-distribution / metrics-table plots — run it
  after any retrain).

## Pre-Session 4 Notes — Read Before Building the Graph Layer

A precision/recall investigation was done on the current LR model before starting Session 4.
Full detail and every number is in `SESSION_3_FULL_SUMMARY.md`. The short version:

- **100% recall + 75%+ precision is NOT achievable with the current 5 single-transaction
  features**, no matter how the model is trained or tuned. Confirmed by sweeping every decision
  threshold from 0.01 to 0.99 (never found both above 75%) and by testing a far more powerful
  Random Forest on the same features (it didn't meaningfully beat the ceiling either). Root
  cause: a real layering hop (e.g. a $1,500 WIRE) and an ordinary legit transfer of similar
  size/timing produce near-identical values on all 5 features — the model cannot separate two
  classes that look the same to it. This is a missing-information problem, not a training
  problem — **do not spend a session re-tuning the LR itself to chase this.**
- **A real bug was found and must not be reintroduced:** `train_model.py`'s train/test split is
  done per transaction ROW via `train_test_split(..., stratify=y)`, not per scenario. With only
  25 fraud scenarios, sibling transactions from the same scenario (near-identical day/hour) can
  land on both sides of the split, letting a flexible model partly memorize "which scenario is
  this" instead of learning a generalizable pattern (caught because a Random Forest leaned 33% of
  its decision weight on `day_of_week`, which shouldn't matter that much). **Any future retrain
  evaluation must use a scenario-level (whole-batch) held-out split**, e.g. hold out entire fraud
  batches + entire background batches, never split by individual row.
- **More diverse training data is a real, legitimate improvement** (tested 140 scenarios vs 25,
  properly scenario-split: ~85% recall / ~37% precision at threshold 0.5, vs. much worse numbers
  on the small 25-scenario set once evaluated honestly) — worth folding into `train_model.py`
  eventually (raise `N_FRAUD_BATCHES` / `N_BACKGROUND_ONLY_BATCHES`) — but it does not change the
  fundamental ceiling above.

## Session 4 Final Plan — Graph Chaining via Neo4j

This is the locked, detailed plan for Session 4, arrived at after a dedicated design pass on the
chaining logic. Build to this spec — do not re-derive it from scratch in the new session.

### 1. Graph structure
- **Nodes:** one per real account, identified by `token_id` (`SHA256(account_number +
  GLOBAL_SALT)`) — same token everywhere, so the same real account is the same node regardless of
  which branch touched it. Plus two fixed sentinel nodes: `CASH` (source of deposits) and
  `CASH_OUT` (sink for withdrawals).
- **Edges:** one per transaction, written once by the *sending* branch only (never duplicated by
  the receiving side). Each edge carries `txn_id`, `amount`, `timestamp`, `txn_type`, `branch_id`.
- **Critical:** all time reasoning (edge timestamps, window checks, `time_to_convergence`) must
  use the transaction's own fabricated business `timestamp` field — never `datetime.now()` /
  wall-clock time. The live demo compresses a ~6-hour fabricated story into ~60-90 real seconds
  (`producer.py`'s `_emit_offset_seconds` mechanism from Session 3); the graph logic reasons
  entirely in business time, same principle as Session 3's `VelocityTracker`. This means
  convergence gets detected within seconds of real demo time, not hours — the hour-scale windows
  below are thresholds on the story's timeline, not real waiting time.

### 2. The chaining rule — when is B "the next hop" after A?
Not "any later transaction from that account." A hop is only chained if both hold:
- **Time-ordered, within a window:** `A.timestamp < B.timestamp <= A.timestamp + per_hop_window`.
  `per_hop_window` = **6-8 hours** (not 2 hours — checked against `layering_scenario.py`'s actual
  math: `transit_minutes_total` is split evenly across however many hops a chain has, so a
  *short* chain, e.g. hops=2, can have a single hop take up to ~3.85 hours; a flat 2-hour window
  would wrongly reject real hops in our own ground-truth data).
- **Amount is preserved:** `0.90 <= B.amount / A.amount <= 1.05` for this pass (comfortably covers
  the simulator's actual 0.5-2% per-hop skim, with margin) — tighten later (e.g. 0.95-1.01) only
  after real testing, not upfront.

### 3. Convergence algorithm
Trace forward from each flagged deposit, hop-by-hop, using the rule above. Traversal stops on:
**convergence found** (paths from independent sources reach the same account), **dead end** (no
outgoing edge satisfies the rule), **cycle** (must be cycle-safe), **time window exceeded**
(`overall_chain_window` ≈ 6-8 hours, matching `span_hours`), or a **safety-ceiling depth** (10-12)
as a backstop only — never a fixed hop count as the primary stop condition.

**Convergence is confirmed** when:
- `num_sources >= min_sources_for_convergence` (3 for the demo's 3-branch story; use ≥2 for
  general-purpose detection later), **and**
- `num_branches >= min_branches_for_convergence` (2 — kept as its own parameter, distinct from
  source count, for future flexibility even though it's redundant with `num_sources` in the
  3-branch demo), **and**
- enough value actually survived the trip: `min_preservation_ratio_to_sink` (~70% of total
  original source amount must still be present at the destination) — prevents "technically
  converged but only pocket change arrived" from counting.

### 4. `check_convergence` output — full evidence, not a boolean
```python
{
  "has_convergence": True,
  "convergence_account": "acct_token_abc",
  "num_sources": 3, "num_branches": 3,
  "paths": [["deposit_A","acct_1","acct_2","acct_Z"], ...],
  "shortest_depth": 2, "longest_depth": 6,
  "time_to_convergence_minutes": 73,
  "total_source_amount": 28100, "total_reached_amount": 26750,
  "amount_preservation_ratio": 0.952,
  "cycle_detected": False,
}
```
This is richer than the `check_convergence(token_id, time_window, max_depth)` signature
originally sketched — the new parameters (`min_sources_for_convergence`,
`min_branches_for_convergence`, `min_preservation_ratio_to_sink`, `per_hop_window`) all need to be
part of the real signature. The evidence dict serves two purposes: it's what the future Report
Agent needs to write an explainable verdict ("3 deposits converged into account Z within 73
minutes, 95.2% of value preserved"), and it's the raw material for Session 5 ML features, without
redoing the traversal logic later.

### 5. Build in two passes, not one
- **Pass 1 (core correctness — the actual Session 4 exit criteria):** binary time+amount rule
  above, traced against the hops=2/4/6 ground truth. Prove the traversal logic is right before
  adding nuance.
- **Pass 2 (evidence + robustness, immediately after, still within Session 4):** the rich output
  dict, `min_preservation_ratio_to_sink`, and injecting noise into the background data — both
  legitimate fan-in at destination accounts (shared landlord/payroll account) *and* unrelated
  activity on mule accounts themselves (paycheck, rent, small transfers) — since nothing hard
  currently exists in `simulator/data_generator.py` to prove the time+amount rule against. A
  continuous hop-confidence-score is a natural future refinement of this pass but is **not
  required for Session 4** — the binary rule is deliberately proven correct first.

**Explicitly deferred, not in scope for Session 4:** split-flow support (one deposit fanning into
several outgoing transfers, with grouped-outgoing amount conservation). Good idea, but
`layering_scenario.py` doesn't currently generate split-flow ground truth — every chain today is
strictly linear — so a split-flow detector would be unverifiable until the simulator is extended
to actually produce that pattern.

### 6. What happens right after the graph works
**Before any ML/model changes:** run `check_convergence` as a plain downstream filter over the
LR's currently-flagged transactions from the live 216-txn demo (keep only flagged transactions on
a path to real convergence, drop the rest) and measure how much of the ~50-60 false positives that
clears out on its own — this may solve most of the precision problem with zero retraining.

Only after that (and only after the background-noise caveat in Pass 2 is addressed), consider
turning graph signals into LR retrain features (`num_flagged_deposits_converging_downstream`,
`is_part_of_convergence_chain`, `hop_distance_from_flagged_deposit`, `time_to_convergence_minutes`,
`amount_preservation_ratio`, `num_branches_upstream`, fan-in/fan-out counts). If attempted: compute
any "flagged deposit" feature from the model's **actual flags**, never from ground truth, or
training and inference will see different information (the same leak category as the row-level
split bug above). Also watch `amount_preservation_ratio` for overfitting to the simulator's fixed
0.5-2% skim rate rather than a signal that generalizes to real laundering behavior.

## Session 4 Update — Graph Layer Built, Read Before Touching `graph/` or `branch_node/graph_writer.py`

Session 4 is done. Both passes built and verified; the short version any future session needs:

- **The traversal is Python, not one Cypher query.** The chaining rule is a *pairwise* constraint
  between consecutive edges (each hop's amount/time relative to the previous hop), which plain
  Cypher variable-length patterns can't express without APOC (not guaranteed installed on stock
  `neo4j:5.20-community`). `graph/queries.py`'s `check_convergence` does the hop-by-hop DFS in
  Python, calling `get_outgoing_txns` per node - Neo4j still does all the storage and per-hop
  lookups; only the pairwise chain-validation logic lives outside Cypher. Fully cycle-safe (per-path
  visited set), depth-capped at 12 as a pure backstop, never as the primary stop condition.
- **A real concurrency bug was found and fixed:** `branch_node/consumer.py` originally never called
  `graph/schema.py`'s `setup_schema()` - only the standalone test scripts did. Without the
  `Account.token_id` uniqueness constraint active *before* any writes, the 3 branch containers
  raced to `MERGE` the shared `CASH`/`CASH_OUT` sentinel nodes concurrently and created **duplicate
  nodes and duplicate edges** in a live Docker run (253 edges for 216 real transactions, two
  physical `CASH` nodes). Fixed two ways, both necessary: (1) `consumer.run()` now calls
  `setup_schema(graph.driver)` itself before consuming; (2) `setup_schema()` retries with backoff
  on `Neo4j.TransientError.Transaction.DeadlockDetected`, since 3 containers calling `CREATE
  CONSTRAINT IF NOT EXISTS` at the same startup instant can deadlock against each other even though
  the statement itself is idempotent. As defense-in-depth against any *future* duplicate-delivery
  case (Kafka is at-least-once; Neo4j Community has no relationship-uniqueness constraint to fall
  back on), `get_outgoing_txns`/`get_incoming_txns` also dedupe by `txn_id` in Python. **Do not
  remove the `setup_schema()` call from `consumer.run()`, and do not remove the dedup** - both are
  independently load-bearing.
- **The downstream-filter experiment (see section 6 above) ran against the real live 216-txn Docker
  demo, not a simulated approximation:** `evaluation/downstream_filter_experiment.py` seeded
  `check_convergence` from the model's actual flagged `CASH_DEPOSIT` transactions (read live from
  Redis + Neo4j, never from ground truth), and kept only flagged accounts that landed on the
  winning convergence path. Result: **54 flagged accounts (13 real fraud, 41 false positives) ->
  13 flagged accounts (13 real fraud, 0 false positives) - 100% of this run's false positives
  cleared, 13/13 real fraud retained**, with zero retraining. This is a single run of one scenario,
  not a statistically robust claim across many runs/conditions (that's Session 9's ablation job) -
  but it's strong enough evidence to defer any LR feature-engineering work (section 6 above) unless
  a future multi-run evaluation shows this doesn't hold up.
- **Pass 2 robustness confirmed on synthetic look-alike noise**, not just clean data:
  `simulator/layering_scenario.py --inject-mule-noise` adds unrelated paycheck/rent/purchase
  activity onto the real fraud-path accounts themselves; `simulator/data_generator.py
  --fanin-accounts N` adds legitimate multi-sender fan-in onto shared accounts (rent/payroll
  look-alike, deliberately NOT amount- or time-matched across senders). Both are opt-in (off by
  default) so the original Pass 1 fixtures stay reproducible. `scripts/test_convergence_noisy.py`
  is the positive/negative-control test built on top of these: real convergence still resolves with
  the mule noise present; 3 independent fan-in senders into one shared account correctly produce
  `has_convergence: False`.
- **New files not yet reflected in the folder structure below:** `graph/connection.py` (shared
  driver factory), `scripts/test_convergence.py` (Pass 1), `scripts/test_convergence_noisy.py`
  (Pass 2 robustness), `evaluation/downstream_filter_experiment.py` (the section-6 experiment).

## Session 4 Extension — Scenario 2 (Multi-Ring, Cross-Branch, 500-txn), Read Before Touching `simulator/multi_ring_scenario.py`

Not part of the original session plan — built afterward as a stress test, once Session 4's exit
criteria above were already met, to check whether the graph work generalizes beyond the one
original demo scenario. A future session picking this up should know:

- **What's new:** `simulator/multi_ring_scenario.py` generates **3 concurrent, independent
  laundering rings** (not 1) in a single 500-transaction dataset (`data/scenario_500/` — its own
  customers, background traffic, fraud events, and ground truth, all separate from the original
  demo's `data/` files). Its own reserved account-number range (`9_500_000_001+`) keeps it from
  ever colliding with the original demo's fraud accounts (`9_000_000_001+`) or background accounts
  (`4_000_000_001+`) even if both were ever loaded into one un-wiped graph. Every layering hop is
  **guaranteed cross-branch** (`_next_branch()` always excludes the current branch, and also
  excludes the consolidation branch for the hop feeding into consolidation, so a hop never lands
  back on its own branch by chance the way the original scenario's plain `random.choice` could).
  2 of the 3 rings deliberately **share one mule account each** with their neighboring ring, so the
  resulting graph is one connected web (`ring1 <-shared-> ring2 <-shared-> ring3`) instead of 3
  isolated stars — this is also a robustness check: nothing in `check_convergence` needed to
  change to keep each ring's own trail straight even though a shared node briefly carries two
  unrelated rings' money at two different times/amounts (same principle as `layering_scenario.py`'s
  `--inject-mule-noise`). ~10% fraud injection (48 fraud transactions out of 500, ~9.6%).
- **A real limitation was found — not in the graph or the detection logic, but in how the existing
  filter script USES `check_convergence`:** `check_convergence` only ever returns its single
  best-matching convergence per call. `evaluation/downstream_filter_experiment.py` (the original
  filter script) calls it once with every flagged deposit — fine for 1 ring, but feeding it all 3
  rings' flagged deposits at once would solve one ring and silently drop the other two's real fraud
  as if it were false positives, purely because the function was never asked to keep looking after
  its first answer.
- **The fix — the "quick version," not yet the harder one:**
  `evaluation/downstream_filter_experiment_scenario_500.py` groups flagged deposits by which ring
  they structurally belong to (using ground truth ONLY for that grouping, never to decide
  fraud/legit), then calls `check_convergence` once per ring independently, and unions the
  kept-token sets before reporting. Deposits matching no ring (genuine false-positive deposits)
  fall into their own `unmatched` group, where they correctly and automatically fail to converge (a
  lone source can never satisfy `min_sources_for_convergence`) — no special-casing needed.
  **Still explicitly outstanding:** this fix is told each deposit's ring membership from ground
  truth — it does not yet discover the groupings on its own with zero help, the way a real
  investigator (or the future Session 6 Money-Trail Agent) would have to. That harder version is
  not built.
- **Live results, same live-Docker methodology as the original demo, zero retraining (same frozen
  `shared/lr_model.json`):** ROC-AUC ~0.98, 100% recall, ~28% precision at threshold 0.3 — nearly
  identical to the original 216-txn demo's numbers, on a dataset with 3x the fraud, guaranteed
  cross-branch hops, and fresh customers the model has never seen in any form. That similarity is
  evidence the model generalizes rather than having memorized quirks of one scenario. The per-ring
  graph filter then cleared **100% of false positives (111-124 out of 111-124, exact count wobbles
  slightly run-to-run from live Kafka-timing effects on the `velocity_10min` feature), all 37 real
  fraud accounts retained** — the same headline result as the original demo, now proven on the
  harder 3-ring case too.
- **New files not yet reflected in the folder structure below:** `simulator/multi_ring_scenario.py`,
  `data/scenario_500/` (customers.json, background.json, multi_ring_events.json,
  multi_ring_ground_truth.json), `evaluation/visualize_scenario_500.py` (writes to
  `evaluation/case2_plots/` — a separate folder from `evaluation/plots/`), and
  `evaluation/downstream_filter_experiment_scenario_500.py`. Full run instructions are in
  `DEMO_RUNBOOK.md`'s "Scenario 2" section.

## Session 5 Update — Manual FL Round Built and Verified, Read Before Touching `fl_server/` or `branch_node/fl_*.py`

Session 5 is done at its scoped exit criteria (a manual round, weights update, AUC logged) - not
yet wired into live autonomous operation, which correctly awaits Sessions 6-7's labeling loop.

- **What's new:** `branch_node/fl_data.py` (per-branch training partitions + a central held-out
  validation set), `branch_node/fl_client.py` (a Flower `NumPyClient` wrapping the existing
  `LogisticFraudModel`), `fl_server/server.py` (Flower `FedAvg` server). `branch_node/train_model.py`
  had one small, backward-compatible change: `_rows_for_batch` was renamed to public `rows_for_batch`
  and now also returns each row's `branch_id` (a 5th tuple element - existing callers that access
  rows by index, like `build_dataset()`, are unaffected).
- **The FL round's training data is deliberately FRESH, never the same 25 scenarios that already
  trained the live model** (its own seed ranges, `60000+`/`65000+` for branch partitions,
  `70000+`/`75000+` for the validation set - all disjoint from `train_model.py`'s own `2000+`/
  `5000+`/`9000+` and from each other). Retraining on data the model already fits would teach it
  nothing - this stands in for "new transactions since the model was last trained."
- **Per-branch partitioning is done by filtering one shared simulated batch by `branch_id`, not by
  generating 3 separate synthetic worlds** - this mirrors exactly how the live Kafka/consumer
  pipeline already isolates data (each branch's consumer only ever sees its own topic), rather than
  inventing an artificial split that doesn't reflect how transactions are really divided.
- **Feature standardization (mean/std) stays FIXED**, read once from the currently-live
  `shared/lr_model.json` and shared by every client and the server - only the linear layer's
  weight/bias are federated. Each round starts from the CURRENT live model's weights (via Flower's
  `initial_parameters`, not a random init) - this is framed as improving the existing model, not
  training a new one. Each client does a SHORT local practice pass (`LOCAL_EPOCHS = 20`, not a
  from-scratch retrain) per round, so branches nudge gently rather than diverging wildly before
  being averaged back together.
- **AUC is measured centrally**, via `FedAvg`'s `evaluate_fn`, against `fl_data.py`'s held-out
  validation set (never touched by any branch's local training) - never estimated from a branch's
  own local data, which would be a different, less trustworthy number. Written to Redis's
  `fl_status` key every round. When the final round completes, the federated weights overwrite
  `shared/lr_model.json`'s `weight`/`bias` (keeping `mean`/`std`/`threshold`/`feature_order`
  unchanged), and `metrics` gains `fl_rounds_run` and `fl_validation_auc` - so branch containers
  pick up the improved model the next time they restart.
- **Verified live, run three times independently** (twice in-session, once by the user in a fresh
  terminal): all 3 branches trained on their own partitions, each branch's local loss visibly
  decreased round over round, all 5 rounds completed every time, `fl_status` confirmed in Redis,
  `shared/lr_model.json` confirmed changed each time.
- **A real bug was found and fixed: every round, and every separate run, was training on the
  IDENTICAL fixed batch of "new" data.** `fl_data.build_branch_partition()` originally hardcoded
  `rng_seed=42` with no variation, and `fl_client.py` generated its partition once in `__init__`
  and reused it across all 5 rounds - so round 1 extracted whatever signal existed in that one
  fixed pile, and rounds 2-5 (and every later separate run) just retrained on the exact same rows
  with nothing new to learn. This is WHY the first two verified runs showed AUC flatlining almost
  immediately after round 1 (`~0.9922 → ~0.9924-0.9925`, then dead flat) - that flatness was partly
  a real gap, not purely the ceiling effects below. **Fixed:** `fl_server/server.py`'s `FedAvg` now
  sets `on_fit_config_fn=lambda server_round: {"server_round": server_round}`; `fl_client.py`'s
  `fit()` reads `config["server_round"]` and calls `build_branch_partition(branch_id, round_num=
  server_round)` fresh every round; `build_branch_partition` shifts its underlying seed range by
  `round_num * 100` so each round now trains on genuinely different transactions. Confirmed
  post-fix: partition sizes now visibly differ round to round (e.g. 503 → 511 → 508 → 520 → 545
  rows, 24-36 fraud each) instead of being identical every time.
- **Even after that fix, AUC still doesn't move much, and now shows real round-to-round
  volatility instead of a flat line (e.g. one post-fix run: 0.9952 → 0.9947 → 0.9955 → 0.9950 →
  0.9890 → 0.9946 - note round 4 briefly dipping below the start).** That volatility is itself the
  evidence the fix is real (genuinely different data pulling the model slightly differently each
  round) - and three ceiling effects from before still legitimately limit the overall size of any
  movement, fix or no fix: (1) the starting point is already near the 0-1 ceiling; (2) local
  training is deliberately light (20 epochs nudging already-converged weights, not a full
  retrain), by design so branches don't diverge wildly before averaging; (3) most fundamentally,
  the 5 features' information ceiling (see `SESSION_3_FULL_SUMMARY.md`) caps what ANY training
  procedure can achieve - FL changes how weights get updated, it doesn't add a 6th feature or new
  kind of signal. Do not expect large or monotonically-improving AUC swings even post-fix, and do
  not "fix" this further by, say, increasing epochs/data volume - that addresses a smaller lever
  than these three.
- **`evaluation/fl_before_after.py`** gives tabular before/after evidence for any single FL round:
  snapshots full metrics (AUC, precision, recall, F1, cash/hop recall, FPR) on `fl_data.py`'s FIXED
  held-out validation set before a round (`--phase before`), then again after (`--phase after`),
  and prints + plots a side-by-side comparison (`evaluation/fl_before_after/comparison_table.png`).
  One real post-fix result: precision 0.5543 → 0.5604, F1 0.7133 → 0.7183, FPR 0.0456 → 0.0444, AUC
  0.9952 → 0.9946 (down slightly) - a genuine, not-suspiciously-clean outcome. **Re-running this
  will NOT reproduce these exact numbers** - by design, the underlying data now varies every round.
- **Do not expect this to reduce Case 1/Case 2's false positives, and do not "fix" the FL round
  to try to make it do so.** False-positive reduction is the graph filter's job (Session 4), proven
  twice already at 100% clearance - it works because the graph adds genuinely new cross-transaction
  information the 5-feature model structurally cannot have. FL's job here is different: proving the
  local-train → share-weights → average → redistribute mechanism works, as the delivery vehicle for
  Sessions 6-7's real agent-verified labels, which is where FL's actual accuracy payoff arrives.
- **Explicitly not done, by design, at this stage:** no automatic triggering from live Docker
  traffic (this is a manual, standalone process - start `fl_server/server.py` then 3×
  `branch_node/fl_client.py` with different `BRANCH_ID`s, all host-side, not through
  `docker-compose`); no real labels feeding it (bootstrap-style fresh synthetic data only, same as
  the original offline training, just newly generated, now round-varying). A cosmetic
  `flwr.client.start_numpy_client` deprecation warning appears in client logs - harmless on the
  pinned `flwr==1.8.0`, not yet migrated to the newer `start_client()` + `.to_client()` API.
- **New files not yet reflected in the folder structure below:** `branch_node/fl_data.py`,
  `branch_node/fl_client.py`, `evaluation/fl_before_after.py`. `fl_server/server.py` replaces the
  Session 1 stub.

## Session 6 Update — Agentic Pipeline Built, Now Running on Local Ollama (`qwen2.5:7b`), Read Before Touching `agents/` or `orchestrator/`

Full plain-English write-up (what was built, why the two agents are split the way they are, every
bug found and fixed, and a detailed `qwen2.5:7b` vs `qwen3:8b` comparison) is in
`SESSION_6_SUMMARY.md`. The short version any future session needs:

**Current state (this is what's actually running - read this before the history below):** all
agent LLM calls go through the raw `openai` SDK pointed at a **local Ollama server**
(`shared/config.py`'s `LLM_BASE_URL=http://localhost:11434/v1`), running **`qwen2.5:7b`**
(`LLM_MODEL` in `.env`) - not Claude, not OpenAI's hosted API. The provider was swapped twice
during this session (Claude -> OpenAI -> local Ollama); the full reasoning for each swap is in the
chronological bullets below, kept for the historical record. Do not read the bullets below as
describing the current state - they describe how it got here.

- `shared/config.py` reads `OPENAI_API_KEY` and `OPENAI_MODEL` from `.env` (previously
  `ANTHROPIC_API_KEY`/`CLAUDE_MODEL`). `OPENAI_MODEL` has **no hardcoded default** - the model ID
  must come from the account's actual available models, not be guessed.
- `requirements.txt`: `anthropic==0.28.0` replaced with `openai==2.45.0`.
- Agent LLM calls use the raw `openai` SDK directly (`client.chat.completions.create(...)`),
  mirroring how this project always called Claude directly rather than through a LangChain model
  wrapper - consistent with the project's existing convention, even though the provider changed.
- The Money-Trail Agent's tool-calling loop (its 3 tools: `check_convergence`, `get_outgoing_txns`,
  `get_incoming_txns`) is built as its own small LangGraph graph (an "agent" node + a "tools" node +
  a loop-back edge), nested inside the outer Structuring→Money-Trail `StateGraph` as a single node.
  This was chosen deliberately over LangGraph's prebuilt `create_react_agent` so that the
  hop/time/cycle stop conditions (design decision #4 below) are enforced by our own edge-routing
  code between LLM turns, not left to the model's own judgment of when to stop.
- If a future session needs to add or swap providers again, update this section and the
  architecture table above rather than leaving a stale "Claude" reference for the next session to
  trip over.
- **`gpt-5.6-terra` rejects function tools on `/v1/chat/completions` unless `reasoning_effort` is
  explicitly `"none"`** - a real 400 error hit while building the Money-Trail Agent
  ("Function tools with reasoning_effort are not supported for gpt-5.6-terra..."). The Structuring
  Agent doesn't use tools, so it's unaffected; the Money-Trail Agent's `agent_node` passes
  `reasoning_effort="none"` on every call.
- **Low OpenAI account tiers rate-limit this model hard (observed: 3 requests/minute)** - a real
  429 hit mid-investigation during testing, not hypothetical. Every agent's OpenAI call goes
  through `shared/openai_utils.chat_completion_with_retry` (exponential backoff on 429 only, 5
  attempts: 5s/10s/20s/40s/80s) rather than calling `client.chat.completions.create` directly.
  This does not fix a low rate limit, just lets a multi-call investigation (the Money-Trail
  Agent alone can make 10+ sequential calls) survive momentary throttling instead of failing
  outright. Adding a payment method to the OpenAI account (independent of whether auto-recharge
  is enabled) typically raises this tier.
- **A second, compounding bug was found and fixed the first time this was tested live:** the
  `openai` SDK's own client has a default internal retry (`max_retries=2`) that fires *inside*
  every single `.create()` call - so each attempt inside `chat_completion_with_retry`'s loop was
  silently bursting up to 3 real HTTP requests, instantly re-triggering the 3 RPM limit before
  our own backoff sleep ever got a chance to let the per-minute window clear. Fixed by calling
  `client.with_options(max_retries=0)` before `.create()`, so our wrapper's sleep schedule is the
  *only* retry mechanism in play. **Do not remove this** - without it, retries on a low-tier
  account effectively never succeed.
- **Pivoted a second time, from OpenAI's hosted API to a local Ollama server (`llama3.1:8b`)**,
  after hitting OpenAI's free-tier 50-requests/day cap (separate from the 3 RPM limit above -
  adding a payment method raises rate-limit *tier* only when it reflects an actual credit
  *purchase*, not an existing/promotional balance). Config is now provider-neutral
  (`shared/config.py`'s `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL`, not `OPENAI_*`) since Ollama
  exposes an OpenAI-compatible endpoint and the `openai` SDK just gets pointed at
  `http://localhost:11434/v1` instead. The `reasoning_effort="none"` workaround above is
  OpenAI-cloud-specific and was removed for the Ollama path.
- **A real hallucination bug was caught during this pivot's first live test, and fixed:** the
  Money-Trail Agent used to ask the LLM to retype the exact evidence hops (txn_id/amount/
  timestamp/channel) into its final JSON answer. With `llama3.1:8b`, this produced fabricated
  data that looked plausible but wasn't real - invented dollar amounts, and timestamps months in
  the future relative to the actual scenario. The high-level convergence answer itself was
  correct (that part is one deterministic `check_convergence` tool call) - the failure was
  specifically in accurately restating facts several turns later. **Fixed:** `tools_node` now
  captures the real `check_convergence` result directly (`MoneyTrailState.convergence_result`),
  and `_build_evidence_hops` deterministically walks each winning path's real edges via
  `get_outgoing_txns` in plain Python - the LLM's role shrank to the judgment call and a
  narrative summary only, never the exact facts. **This principle applies regardless of which
  model runs the agents** - never trust an LLM to retype exact data it saw earlier in a
  conversation when the real data is already available in code.
- **A real crash was found during the first full live (Step 6) run:** `_run_tool` indexed
  `args["token_id"]` directly with no fallback - when `llama3.1:8b` occasionally emitted a
  malformed `get_outgoing_txns`/`get_incoming_txns` tool call missing that argument, this raised
  an unhandled `KeyError` that crashed the entire investigation, discarding all prior work for
  that token. Fixed two ways: (1) `_run_tool` now uses `args.get("token_id")` and returns a
  tool-result error instead of raising if missing; (2) `tools_node` wraps the whole tool-call
  execution in try/except, so *any* malformed tool call degrades to an error message fed back to
  the LLM rather than crashing the graph. Both are worth keeping regardless of model - smaller/
  local models are more prone to malformed tool calls than frontier hosted ones, but this class of
  defensive handling costs nothing either way.
- **`llama3.1:8b` was replaced by `qwen2.5:7b` as the final model choice**, after a direct,
  reproducible comparison on the same reset-to-clean-slate hops=4 scenario (`docker compose down
  -v` between runs, same `data/layering_hops4_events.json`): `llama3.1:8b` correctly traced only
  8/13 real fraud accounts to `convergence_found` (4 false-negative at the Structuring Agent's
  confidence judgment, 1 false-negative at the Money-Trail Agent giving up early on real
  convergence data); `qwen2.5:7b` traced **13/13** correctly, with the same fully-accurate
  deterministic evidence (see the hallucination fix above - this holds regardless of model). Both
  are pulled locally (`ollama list` should show both); `LLM_MODEL` in `.env` and
  `shared/config.py`'s default both point at `qwen2.5:7b` now.
- **One remaining known rough edge with `qwen2.5:7b`, not yet fixed:** its free-text `summary`
  field is sometimes internally self-contradictory - e.g. arguing "insufficient evidence" in prose
  while the real `check_convergence` result (and therefore the actual `stop_reason` our code
  assigns) correctly says convergence was found. This never affects the verdict or evidence data
  (both are deterministic, from real tool results, per the fix above) - only the narrative summary
  text can be incoherent. Worth a prompt-tuning pass before Session 8's Report Agent builds
  human-readable case reports on top of these summaries.
- **Tested against Scenario 2 (`data/scenario_500`, 3 independent rings, 37 real fraud accounts)
  and found a real bug in the multi-ring fix above:** `_find_convergence_group_for_token` only
  checked whether the investigated token was a *source* (`path[0]`) of the winning group - never
  whether it appeared anywhere else in the path. Since each ring is 1 placement account + 3
  intermediate hops + 1 consolidation account, only the placement account could ever match -
  the other 10 of 13 accounts per ring always looked like `insufficient_evidence` even when
  correctly part of a found group. **Fixed:** the membership check now looks at every token in
  every path (`{tok for path in paths for tok in path}`), not just `path[0]`.
- **A second, deeper, pre-existing issue was found in the same test - not yet fixed, needs a
  decision:** the two shared mule accounts (`9500000004` shared ring1/ring2, `9500000023` shared
  ring2/ring3) can create a genuine cross-ring "leak" - a real placement deposit's traced path
  can validly continue *through* the shared node into a neighboring ring's consolidation account,
  satisfying the exact same time+amount chaining rule that correctly identifies true convergence
  elsewhere (`_trace_forward` in `graph/queries.py` already branches over every valid outgoing
  edge via DFS - this isn't a traversal bug, the leaked edge is a real, valid-per-the-rule
  transaction). Observed concretely: ring1's and ring3's own placement deposits were attributed to
  ring2's consolidation account instead of their own ring's, because the leaked path happened to
  be found before their own ring's true group. This is a Session-4-era threshold-tuning gap
  (the amount-ratio/time-window gates were tuned against Scenario 1's single-ring case) surfaced
  by testing at 3-ring scale, not a Session 6 agent bug - fixing it would mean revisiting
  `DEFAULT_AMOUNT_RATIO_MIN/MAX`/`DEFAULT_PER_HOP_WINDOW_HOURS` in `graph/queries.py`, or adding a
  tie-break that prefers a source's *shortest* or *own-branch-consistent* path over an
  incidentally-also-valid longer one. Left as a known limitation pending a decision on priority.
- **A partial fix for the leak above was built and tested:** `check_convergence` (`graph/queries.py`)
  gained an additive `return_all_candidates: bool = False` parameter (default preserves 100% of
  existing behavior/callers) that returns EVERY group passing the gates, not just the single best
  one. `_find_convergence_group_for_token` (`agents/money_trail_agent.py`) uses this to resolve
  ambiguous (multi-group) sources by claiming them **smallest-group-first** - the two true rings
  each have exactly 3 real sources, the absorbing group has those plus whatever leaked in, so
  letting the smaller true groups claim their (including ambiguous) members first shrinks the
  absorbing group back toward its own real members. **Result: a real, substantial, but incomplete
  improvement** - Ring1 and Ring3 went from 0/13 and 1/13 correct to 13/13 each, but Ring2
  (previously 9/13 after the path-matching fix) regressed to 0/13, because the leak turned out to
  be bidirectional through the shared node (confirmed concretely: ring2's own placement account
  `9500000019` got swept into ring1's group), not the one-way "absorption" the fix assumed. Net
  effect across all 39 accounts: 9 correct -> 26 correct. **Still an open item** - a full fix
  needs the threshold-tuning approach above, this heuristic is a stopgap.
- **Final model choice for the agent LLM, after testing 4 models on identical clean-reset runs:**

  | Model | 216-case fraud recall | 500-case fraud recall | Speed on this hardware (Apple M5, 16GB) |
  |---|---|---|---|
  | `llama3.1:8b` | 8/13 | not tested | fast (seconds/call) |
  | `qwen2.5:7b` | 13/13 (best run; 9/13 seen once - real run-to-run variance) | 23/37 | fast (seconds/call) |
  | `qwen3:8b` | 13/13 | 6/8 (representative sample only, too slow to run all 59) | slow (~2.2 min/investigation avg) |
  | `qwen3:14b` | not run (deemed impractical) | not run | very slow (~3 min/investigation; caused heavy swapping - `vm.swapusage` showed 93% of 11GB swap in use) |

  `qwen3:8b` matches or slightly beats `qwen2.5:7b` on quality (and both show the identical Ring1/
  Ring3-good, Ring2-struggles pattern on Scenario 2, confirming that's a tool/threshold issue, not
  model-specific) but is 3-5x slower - a single Money-Trail Agent investigation took 140.9s with
  `qwen3:14b` and this pattern held proportionally for `qwen3:8b` too. **Decision: stay on
  `qwen2.5:7b` for now** - the speed matters for iteration speed and any live demo (per `CLAUDE.md`'s
  own demo script target of "live portion under 2 minutes"), and quality is comparable. `qwen3:8b`
  remains a good candidate to revisit if speed stops being the constraint (e.g. better hardware,
  or the demo is restructured to replay a pre-run investigation instead of running one live - see
  the FL-improvement metric's existing "pre-computed plot" precedent for that pattern). Both
  `qwen3:8b` and `qwen3:14b` are already pulled locally (`ollama list`) if picked up again later.
- **A follow-up architecture pass closed the recall gap that was actually caused by LLM
  judgment/timing, as distinct from the still-open shared-mule leak above.** Prompted by a simple
  question worth remembering: manual (ground-truth-assisted) tracing was 100/100 recall, so why
  wasn't the agent? Three real, separate causes were found and fixed, none of them requiring a
  better model:
  1. **`check_convergence` was an LLM-callable tool, not a guaranteed step.** It's a deterministic,
     zero-argument function - it always gives the mathematically correct answer for the current
     flagged set. Leaving it up to the LLM's discretion inside a tool-calling loop meant it
     sometimes just... wasn't called, or was reasoned past, before the LLM concluded
     `insufficient_evidence` - silently costing recall the manual approach never risked, since it
     has no such discretion. **Fixed:** `money_trail_agent()` now calls
     `_find_convergence_group_for_token` directly, in code, before any LLM call. If it finds
     convergence, evidence is built deterministically and a single plain (no-tools) LLM call
     narrates it - nothing left in that path can cost recall, only wording. The LLM's tool-calling
     loop (`_explore_dead_end`, now a plain bounded loop, not a LangGraph graph - simpler now that
     the loop's only job is exploring a genuine non-convergence, not gating the primary verdict)
     only runs for the `has_convergence: False` case, where its discretion no longer risks a real
     positive.
  2. **The Structuring Agent's confidence was a hard gate** (LOW confidence = investigation stops,
     Money-Trail Agent never runs). This was the single biggest source of the `llama3.1:8b`/
     `qwen2.5:7b` run-to-run variance documented above. Since the Money-Trail Agent's core check is
     now a fast, deterministic step for the common case (not an expensive LLM loop), the cost
     argument for gating no longer outweighs the recall it was costing. **Fixed:**
     `agents/state_graph.py`'s conditional edge was replaced with an unconditional one -
     `structuring_agent` always continues to `money_trail_agent` now.
     `structuring_reasoning`/`structuring_confidence` are still recorded and passed along as
     context, just never used to skip the investigation. `orchestrator/listener.py`'s "stopped at
     Structuring Agent" branch was removed as dead code (evidence is now always populated).
  3. **A real timing race, found only once (1) and (2) made the picture clean enough to see it:**
     a ring's 3 real placement deposits are separate transactions that get flagged at different
     times, not simultaneously. If a source is investigated the instant it's flagged - correct
     behavior for the single-ring case, where zero-delay reaction is exactly right - it can run
     *before* its sibling sources have been flagged yet, so `check_convergence` correctly reports
     "not enough sources" at that moment and the investigation concludes
     `insufficient_evidence`/`cycle`, even though the full group is real and gets confirmed
     moments later by a sibling's own investigation. Confirmed concretely: re-running
     `_find_convergence_group_for_token` standalone against the complete (fully-flagged) data for
     3 affected tokens resolved all 3 correctly, proving the live miss was a timing artifact, not a
     logic error. **Fixed:** the instant any investigation confirms a group, `money_trail_agent()`
     now writes the *same* confirmed evidence for every other member of that group too, not just
     the token that happened to trigger it - retroactively correcting any sibling investigated too
     early. This also cut real work: re-verifying the 500-case needed only 36 individual
     investigations to cover all 59 flagged accounts, the rest resolved for free via their group.
  - **Combined result, re-verified end to end on fresh clean-reset runs:** 216-case (1 ring):
    **13/13 real fraud correct, 0 wrong, 0 insufficient, 14/14 false positives cleared** - now
    matching manual exactly, and no longer dependent on a lucky run. 500-case (3 rings): **26/39
    correct** (Ring1 13/13, Ring3 13/13 - both now perfectly clean; Ring2 0/13, unchanged), 22/22
    false positives cleared. **The remaining gap is now isolated to exactly one cause** - Ring2's
    bidirectional shared-mule leak (documented above) - with none of the LLM-judgment or timing
    noise that used to make the picture harder to read.
- **Session 7's full scope (Prosecutor/Defense/Judge adversarial debate) was deliberately deferred,
  not abandoned - a single Verdict Agent was built instead**, closing the FL feedback loop without
  the debate. Full plain-English write-up (what/why/how, and why FL needed the labeling step at
  all) is in `VERDICT_AGENT_SUMMARY.md`. This is not an ad hoc simplification - it's literally the
  "Baseline" condition
  already named in the Evaluation Plan below (`ENABLE_ADVERSARIAL_VERIFICATION=false`). Reasoning:
  this session's precision was already 100% in every real run (0 false positives ever incorrectly
  kept) - the debate's whole job is adding scrutiny to precision, so it has little to catch right
  now, while being real, separate engineering scope. The debate-vs-single-agent comparison is a
  real thing worth demonstrating later for the research-paper side of this project, but doing that
  needs a working baseline to compare against anyway - so building the baseline first, not last,
  is the right order regardless.
  - **`agents/verdict_agent.py`** - one LLM call, no tools, only ever invoked on evidence with
    `stop_reason == "convergence_found"` (there is no verdict to render on a dead end). Answers a
    genuinely different question than the Money-Trail Agent already resolved: not "did the money
    connect" (deterministic, already confirmed) but "does this real connection prove deliberate
    laundering, or could there be an innocent explanation." Falls back to zero-confidence
    NOT_GUILTY on any failure, so a broken call never silently becomes a false GUILTY label.
  - **`agents/label_generator.py`** - plain code, deliberately not another LLM call, same principle
    as the recall fixes above: once the Verdict Agent has already judged GUILTY with high
    confidence, deciding whether to act on that is bookkeeping, not a fresh judgment call. Writes
    one real `Label` (real features, `source="agent_verified"`) per group member that was itself
    scored by a branch, into that branch's own `labels:{branch}` Redis list.
  - **Wired into `money_trail_agent()` directly** (not a separate `state_graph.py` node) - right
    alongside the existing evidence group-write, for the same reason: a confirmed group's verdict
    and labels need to cover every member of that group, not just whichever token happened to
    trigger the investigation, and members resolved for free via the group-write never pass back
    through the outer graph to reach a downstream node.
  - **`orchestrator/listener.py`** now also prints the verdict and label count per case.
  - **Verified live, full 216-case run:** the one real fraud ring correctly produced **13 verdicts
    (all GUILTY, ~0.9+ confidence) and 13 labels** in one shot, split correctly across the 3
    branches (`loc1`: 6, `loc2`: 3, `loc3`: 4) with each label's real features and
    `source="agent_verified"` - matching the ground truth exactly, zero manual triggering.
  - **The FL-consumption gap above is now closed too.** `branch_node/fl_data.py` gained
    `real_labels_for_branch(branch_id)`, which drains (`LPOP`, not just reads) that branch's
    `labels:{branch}` buffer and converts each `Label` into the same `(feature_array, label)` shape
    the synthetic rows already use, via the same `features_to_array` helper `train_model.py` uses.
    `build_branch_partition` gained an additive `include_real_labels: bool = False` parameter
    (default False - `evaluation/fl_vs_isolated.py`'s existing reproducible synthetic-only
    measurements are completely unaffected, and never accidentally drain labels meant for a real
    round) that, when True, mixes the real labels in ON TOP of the synthetic partition rather than
    replacing it - a real round typically has only a handful of confirmed cases, far fewer than one
    round's synthetic batch, so this nudges the model with real signal without a tiny homogeneous
    set of agent-derived rows dominating or overfitting the round on its own. Only
    `branch_node/fl_client.py`'s real per-branch training loop passes `include_real_labels=True`.
    Draining (not just reading) is deliberate: each real label contributes to exactly the next real
    FL round that consumes it, not every round forever.
  - **Verified directly:** seeded one real label into `labels:loc1`, confirmed
    `build_branch_partition(..., include_real_labels=False)` leaves the buffer untouched (`LLEN`
    still 1), and `include_real_labels=True` correctly mixes it in (partition grew by exactly 1 row,
    with the right feature values and `label=1`) and drains the buffer (`LLEN` back to 0).
  - **The loop is now fully closed, end to end:** a flagged transaction can travel autonomously
    from Structuring Agent → Money-Trail Agent → Verdict Agent → Label Generator →
    `labels:{branch}` → the next real FL round's `build_branch_partition(include_real_labels=True)`
    → an actual model weight update - with zero manual triggering anywhere in that chain except
    starting the FL round itself (still a manual, standalone process per Session 5's design).

## Post-Session 6 Extension — Scenario 3 (Demo-Safe Multi-Ring, 1500-txn), Read Before Touching `simulator/multi_ring_scenario.py`'s `num_shared_links` or `simulator/data_generator.py`'s `end` param

Full plain-English write-up (what/why/how, every number, every finding) is in
`SCENARIO_1500_SUMMARY.md` — this is the condensed version for the architecture record.

- **Why this exists:** Scenario 2 (`data/scenario_500`) has a known, still-open bug — its 2 shared
  mule accounts cause a real cross-ring leak, and Ring2 always fails (0/13) in the live agentic
  pipeline. Fine to know about, not fine to hit live in front of a stakeholder demo. The ask: a
  bigger (1500-txn, ~20% fraud), more realistic (many simultaneous rings) case that deliberately
  avoids that specific broken configuration.
- **The fix that makes it possible:** `generate_multi_ring_scenario()` used to hardcode "every
  consecutive ring pair shares a mule account" (`num_rings=N` always produced `N-1` links, no way to
  have fewer). Added an additive `num_shared_links: Optional[int] = None` parameter — `None`
  preserves the exact old behavior (every existing caller, including Scenario 2's own generation
  command, unaffected); passing e.g. `1` links only that many consecutive ring pairs and leaves the
  rest fully independent (zero shared accounts = structurally unable to leak).
- **`data/scenario_1500/`:** `num_rings=19, hops=4, num_shared_links=1, seed=1500` → 1,500 txns
  (1,196 background + 304 fraud), 20.3% fraud, 246 unique fraud accounts, exactly 1 shared mule
  account (between ring1/ring2 only — 17 rings fully isolated).
- **A genuinely better test was needed to trust the "no leak" result.** The existing
  `scripts/test_multi_ring_convergence.py` hands `check_convergence` each ring's own pre-grouped
  sources in isolation — a condition that can never reproduce a cross-ring leak (nothing else is
  present to leak from). New `scripts/test_agent_convergence.py` flags every ring's real placement
  deposits **all at once** (the real live condition) and calls the actual production resolver
  (`_find_convergence_group_for_token`). Validated the test itself first: run against
  `data/scenario_500`, it correctly reproduces the documented Ring2 failure (26/39). Run against
  `data/scenario_1500`, it reports **247/247 correct**, including both rings touching the shared
  account.
- **A real, unrelated bug found and fixed along the way: day-of-week calendar drift.**
  `simulator/data_generator.py`'s `generate_background_transactions()` anchors its background window
  to `datetime.utcnow()` — whatever real calendar day the script happens to run on. Since the model
  weights `day_of_week` and standardizes it against a *fixed* training-time mean, freshly-generated
  data on the "wrong" day silently inflates false positives for reasons unrelated to fraud. First
  `scenario_1500` generation: ROC-AUC 0.885, FPR 69.5%. Same model, same data shape, only the
  calendar-anchor fixed: ROC-AUC **0.9927**, FPR **21.0%**, recall unchanged at 100% (confirmed live
  via Docker/Kafka, not just offline). Fix is additive (`end`/`--end-date` on the background
  generator, `--scenario-start` on the multi-ring generator) — nothing else affected. **Still open:**
  `train_model.py`'s own retraining has the identical unpinned-`utcnow()` pattern for its background
  rows — confirmed the live model's own weight/bias/mean/std had already drifted mid-session, almost
  certainly from the nightly batch retrain job hitting this same issue. Not fixed, just worked around
  for this one dataset.
- **Full agentic pipeline (SA → Money-Trail Agent → Verdict Agent) tested against all 451 flagged
  accounts** (`scripts/run_full_agent_investigation.py`, real Ollama `qwen2.5:7b` calls, no
  shortcuts): 280 active investigations (170 more resolved free via group-write), 18
  `convergence_found` (15 GUILTY, 195 labels written — exactly 15×13; 3 NOT_GUILTY), 234/246 real
  fraud accounts confirmed via convergence evidence.
  - **Finding A — 3 real, structurally-correct convergences were judged NOT_GUILTY.** The
    deterministic evidence was right in all 18 cases; 3 of the Verdict Agent's own judgment calls
    were wrong (confidences 0.25-0.65). A genuine LLM-accuracy gap, not a tracing bug. Not yet
    root-caused (see Finding B for why the rationale text is gone).
  - **Finding B — running `scripts/test_agent_convergence.py` (or `test_multi_ring_convergence.py`)
    concurrently with a live investigation batch silently breaks it.** Both scripts call
    `r.delete(FLAGGED_ACCOUNTS)` and wipe Neo4j as normal setup for a *standalone* run — if either
    runs in a separate terminal while a real investigation batch is active against the same
    Redis/Neo4j instance, every investigation after that point degrades to "fewer than 2 currently
    flagged sources" → guaranteed `insufficient_evidence`/`cycle`, no error, no warning. Confirmed
    concretely: every one of the 18 successful `convergence_found` results happened within the first
    811 seconds of a ~20-hour run; `flagged_accounts` was found sitting at 0 members afterward. This
    is also why ring1 was individually missed (its 2 investigated members were reached late, well
    after the collision) and why `verdicts:*` keys are now almost entirely gone from Redis (blocking
    Finding A's root-cause investigation this session). **Operational rule going forward: never run
    either convergence-test script against the same Redis/Neo4j instance as a live listener or
    investigation batch.**

## Post-Session 6 Extension — Reframed 3-Agent Pipeline, Read Before Touching `agents/structuring_agent.py`, `agents/money_trail_agent.py`'s report-agent call, or `agents/report_agent.py`

The original framing ("Structuring Agent decides whether to escalate") was stale - it hadn't
decided anything since the Session 6 recall fix made the Money-Trail Agent run unconditionally.
This reframes what each agent is actually for, and adds the Reporting Agent.

- **Agent 1 (`agents/structuring_agent.py`) is now pure list-and-describe, not judge-and-gate.**
  Dropped `confidence`/`top_signals` entirely (not just unused - removed from the schema) since
  they never gated anything and were pure dead weight. New output is just `{summary}` - 2-3
  factual sentences, no judgment call. `orchestrator/listener.py` appends every flagged
  transaction (fraud and false positive alike, across all branches) plus its summary to a new
  Redis list, `structuring_log` (`shared/redis_keys.py`) - Agent 1's whole job, literally "list
  down all the flagged transactions."
- **Agent 2 (`agents/money_trail_agent.py`) is unchanged in its core detection logic** -
  `check_convergence` still runs deterministically before any LLM call, still the real separator
  of fraud rings from false positives. What changed: the evidence dict now also carries
  `source_tokens` (the real placement deposits, `path[0]` of each winning path), `all_tokens`
  (every group member), and a stable `ring_id` (`f"ring_{convergence_account[:8]}"`) - needed by
  the Reporting Agent, not previously persisted.
- **Reporting is no longer downstream of the Verdict Agent's opinion.** `money_trail_agent()` now
  calls `agents/report_agent.py`'s `report_agent()` immediately upon `convergence_found`, before
  `verdict_agent()` even runs. This was a deliberate decision, not an oversight: a full 451-account
  live run (`SCENARIO_1500_SUMMARY.md`) measured the Verdict Agent incorrectly clearing 3 of 18
  structurally-confirmed rings as NOT_GUILTY - if reporting had waited for GUILTY, those 3 real
  rings would never have been reported at all. **The Verdict Agent's code and behavior are
  completely unchanged** - still only wakes on `convergence_found`, still renders
  GUILTY/NOT_GUILTY + rationale - it now exclusively controls FL label-writing
  (`generate_labels`), never whether a ring gets reported.
- **`agents/report_agent.py` (Agent 3) is new, and deliberately has no LLM call anywhere in it.**
  Every column the report needs (transaction id, token, amount, timestamps, ML score) is already
  a known fact in Neo4j/Redis; the one new fact it needs - the real, unmasked account number - has
  never been resolvable from anything in this pipeline before, since every other component only
  ever sees `token_id` by design. For this demo, the reverse lookup is built by re-hashing every
  known account number from the customer/ground-truth files on disk and matching against the
  token (works because this is synthetic data with the full population available; a real system
  would resolve this from each branch's own local `account_master`, never a shared table). Walks
  Neo4j directly to reconstruct the COMPLETE per-ring transaction list - every source's original
  CASH deposit and the final cash-out from the convergence account, neither of which
  `_build_evidence_hops`'s own `path` includes (it only covers the hops between sources and the
  convergence account). Writes one consolidated workbook (`reports/fraud_rings_report.xlsx`,
  override with `REPORT_XLSX_PATH`), two sheets: `Transactions` (Ring ID, serial number,
  transaction id, token, real account number, amount, credited-from, sent-to, time, day of week,
  ML score - flat, one row per transaction, sortable/filterable by Ring ID) and `Ring Summaries`
  (one row per ring: who started it, every token involved, what it converged to). Idempotent per
  `ring_id` - re-processing an already-reported ring is a no-op, doesn't duplicate rows.
- **Both sheets also carry Verdict / Verdict Confidence (and Ring Summaries gets Verdict
  Rationale too)** - display-only, never a gate. `money_trail_agent()` now runs `verdict_agent()`
  *before* calling `report_agent()` (previously the other way around) purely so the verdict value
  exists to hand over for display; the report's own trigger is still `convergence_found` alone,
  proven live on the 500-case run below where a NOT_GUILTY-verdict ring still got its full 18
  transactions written to the report.
- **Verified live end to end, twice**: (1) 216-case - real Structuring Agent summary (no
  confidence field), real Money-Trail Agent convergence on the one ring (3 sources, 13 accounts),
  a correctly-populated 16-row Excel (3 deposits + 12 hops + 1 final cash-out, real account
  numbers, real scores, real timestamps/weekdays), Verdict Agent independently rendering GUILTY
  (0.95) and writing 13 labels, and re-investigating a sibling of an already-reported ring
  correctly added zero duplicate rows. (2) 500-case - reproduced the documented Ring2 leak exactly
  (Ring1 13/13, Ring3 13/13, Ring2 only 3/13 via leaked-account overlap, unchanged from before
  this reframing since `check_convergence` itself wasn't touched); Ring1's group got a NOT_GUILTY
  verdict (0.75 confidence) from the Verdict Agent yet still has all 18 of its transactions in the
  report - concrete proof the report/verdict decoupling holds under a real, messy, non-hypothetical
  case, not just the clean 216-case.

## Critical Design Decisions — do not change without re-deriving the consequences

1. **Token scheme:** `token_id = SHA256(account_number + GLOBAL_SALT)[:16]` — ONE global salt,
   shared system-wide, NOT branch-specific. This is required so that the same real account
   always maps to the same Neo4j node no matter which branch's consumer writes an edge
   involving it. (The original FedShield branch-salted tokens on purpose to force LSH — that
   logic does NOT apply here and would silently break cross-branch convergence detection.)
2. **Isolation boundary:** Docker/branch isolation applies ONLY to raw `transaction_raw`,
   customer PII, and each branch's local model training data. Neo4j, Redis, and the LangGraph
   agent service are intentionally centralized/shared — cross-branch tracing is structurally
   impossible otherwise. This is not a privacy leak: only anonymized tokens and transaction
   metadata (amount, time, channel) ever enter the shared layer, never names/customer_id/raw
   account numbers.
3. **Cross-branch transfers are written to the graph exactly once** — by the origin/sending
   branch's consumer. No duplicate edges from the receiving side.
4. **Money-Trail Agent traversal is NOT capped at a fixed hop count.** It stops on principled
   conditions only: convergence found (≥2 independently-flagged sources reach the same node
   within the time/amount window), current node has no outgoing edges (dead end), node already
   visited in this path (cycle), time window exceeded, or a high safety ceiling (~20 hops) as a
   pure cost/runaway guardrail — never the primary stopping logic.
5. **Convergence checking is offloaded to Neo4j**, not reasoned about hop-by-hop by the LLM.
   Give the agent a `check_convergence(token_id, time_window, max_depth)` tool that runs one
   variable-length Cypher query server-side, plus `get_outgoing_txns` / `get_incoming_txns` for
   building the human-readable evidence path. This keeps cost/latency bounded as chains lengthen.
6. **Full autonomy except the report.** Detection → escalation → tracing → adversarial
   verification → label injection → FL retraining all run with zero human input, triggered by
   Redis pub/sub. The ONLY human step is reviewing/approving the drafted report before a case
   is officially closed — and that approval does NOT gate the FL feedback loop, which fires the
   moment the Judge renders a verdict, independent of when/whether a human reviews the report.
7. **Explicitly cut for scope/time — do not add back without a scope discussion:** Opacus/DP,
   FastAPI backend, Streamlit or any dashboard, LSH/datasketch, River online learning.
8. **Demo visualization is Neo4j Browser (live convergence query) + terminal logs only.**
9. **Scenario generator must support:** configurable hop-count chains (2, 4, 6+, for testing
   whether detection degrades with chain length) and timestamp compression — fabricate
   timestamps spanning real hours but deliver events to Kafka within seconds, so the demo is
   watchable in real time without misrepresenting the time-window logic the agents reason over.
10. **Thresholds:** fraud/structuring score threshold is now empirically tuned during training
    (currently 0.3 — see "Session 3 Update" above), NOT the `.env` `FRAUD_THRESHOLD=0.75`
    constant, which is unused dead config as of Session 3's rework. Judge confidence threshold
    for label injection: 0.8 (tune later). FL retrain trigger: every K minutes or N new labels
    (configurable, tune later). CTR reporting threshold: $10,000 (this is a real US regulatory
    figure, not a tunable — placement amounts are generated just under it).
11. **Identifiers are US banking conventions, not Indian:** `customer_id` (synthetic, e.g.
    `CUST-00481923`) + `ssn_last4` (fake, last-4-only, never a full SSN) identify a person;
    `account_number` (10-digit) + `routing_number` (9-digit ABA, one per branch) identify an
    account. No CIF/IFSC/Aadhaar — those were the original FedShield's India-specific scheme.

## The Multi-Agent Pipeline (LangGraph StateGraph)

**Currently built and running** (see CLAUDE.md's "Session 6 Update" for the full reasoning behind
this scope decision — the full adversarial debate below was deliberately deferred, not abandoned):

```
[Structuring Agent] --always--> [Money-Trail Agent]
   (no longer a hard gate - see "Session 6 Update"; reasoning/confidence passed along as context)

[Money-Trail Agent]  (check_convergence runs automatically in code, not an LLM tool choice;
                       get_outgoing_txns/get_incoming_txns remain LLM tools for the dead-end-
                       exploration path only - see "Session 6 Update")
        |--convergence found--> [Verdict Agent]
        |--dead end / cycle / time exceeded / safety cap--> END (insufficient evidence)

[Verdict Agent]  (single agent - the "Baseline" condition from the Evaluation Plan below,
                   ENABLE_ADVERSARIAL_VERIFICATION=false; built first because it's simpler and
                   Session 6's precision was already excellent with no debate)
   weighs the confirmed evidence -> {verdict, confidence, rationale}

[Verdict: GUILTY, confidence >= 0.8] --> [Label Generator] (code, autonomous, fires immediately)
                                         writes (features, label="fraud", source="agent_verified")
                                         into each involved branch's local retrain buffer
```

**Not yet built** (the "Full system" condition from the Evaluation Plan below,
`ENABLE_ADVERSARIAL_VERIFICATION=true` — a real future extension, not a discarded idea):

```
[Verdict Agent] --replaced by--> [Adversarial Verification]
   ├─ Prosecutor Agent: argues GUILTY from evidence path + scores
   ├─ Defense Agent: argues the most plausible innocent explanation
   └─ Judge Agent: weighs both -> {verdict, confidence, rationale}
   (same downstream Label Generator either way - the debate only changes how the verdict is reached)
```

**Also not yet built:** the Report Agent (drafts the human-readable report, ends with the privacy
attestation, writes to Redis with `status: PENDING_REVIEW`) and the one human review step - this
is Session 8's scope, unaffected by the debate-vs-single-agent decision above.

## Data Schemas

### Kafka message
Nested envelope, not a flat record — production-realistic shape (event metadata + transaction +
originator + beneficiary + telemetry blocks):
```
{
  "event_id": "evt_...", "kafka_timestamp": "...", "branch_id": "loc1",
  "transaction": {"txn_id", "timestamp", "amount", "currency": "USD", "txn_type", "channel", "status"},
  "originator": {"account_number", "routing_number", "account_type", "customer_id", "customer_name"},
  "beneficiary": {"account_number", "routing_number", "account_type", "customer_id", "customer_name"},
  "telemetry": {"ip_address", "device_id", "device_type", "location": {"city","state","country","latitude","longitude"}}
}
```
`account_number` is the literal string `"CASH"` / `"CASH_OUT"` for deposit/withdrawal legs (all
other originator/beneficiary fields None in that case). `txn_type`:
`CASH_DEPOSIT`/`CASH_WITHDRAWAL`/`WIRE`/`ACH`/`ZELLE`/`CHECK`/`DEBIT_CARD`. `channel`:
`BRANCH`/`ATM`/`ONLINE`/`MOBILE`/`WIRE_ROOM`. `customer_id`/`customer_name` are PII, stripped by
`masking.py` before anything leaves the branch (Session 3) - never persisted/shared as-is.
**`telemetry` is generated for realism and reserved for the deferred Case 2.2 (same-device/IP
correlation across branches) - it is NOT consumed by the Case 1 structuring/graph pipeline.**
Model-facing `riskScore` is deliberately absent from the raw event: it's the branch model's
*output*, computed after masking, not an input - including it here would be circular.

### Neo4j
`(:Account {token_id, flagged, flagged_at})-[:TRANSACTED {txn_id, amount, ts, channel}]->(:Account)`
Synthetic sink/source nodes: `(:Account {token_id:"CASH"})` for deposits,
`(:Account {token_id:"CASH_OUT"})` for withdrawals.

### Redis keys
`score:{branch}:{token_id}:{txn_id}`, `flagged_accounts` (set), `fraud_events` (pub/sub channel —
this is the trigger, not polling), `evidence:{token_id}`, `verdicts:{token_id}`,
`reports:{token_id}`, `fl_status` (round #, AUC, timestamp), `labels:{branch}` (pending retrain
buffer).

### ML features (per transaction, fed to the PyTorch model)
`amount_ratio_to_threshold`, `is_cash`, `hour_of_day`, `day_of_week`, `is_transfer_out`.
(`account_age_days` and `velocity_10min` are computed and stored in `TxnFeatures` for evidence,
but deliberately excluded from the model itself — see "Session 3 Update" above.)

## Evaluation Plan
Four toggleable ablation conditions via two config flags — `ENABLE_ADVERSARIAL_VERIFICATION`,
`ENABLE_FL_FEEDBACK`:

| Condition | Adversarial verification | FL feedback loop |
|---|---|---|
| Baseline | off (single Verdict agent) | off (static FL, bootstrap labels only) |
| Debate-only | on | off |
| Feedback-only | off | on |
| Full system | on | on |

Metrics: AUC-per-FL-round (against a fixed held-out labeled validation set), false-positive
rate, detection latency (txn timestamp to verdict timestamp), label precision (agent-verified
label vs. injected ground truth). Illustrative scale (5–10 repeated runs per condition) —
appropriate for a capstone, not claiming full statistical rigor.

## Explicitly Out of Scope For Now (future work, do not build yet)
- **Case 2.1** — single massive cash deposit, caught by real-time scoring alone.
- **Case 2.2** — same individual depositing at all 3 branches same day, caught by behavioral
  similarity matching (LSH or similar).
Both are additive later: they'd become extra scoring logic inside the same per-branch Kafka
consumer, writing into the same Redis whiteboard, triggering the same LangGraph orchestrator.
Nothing about the Case 1 design above needs to change to add them.

## Demo Script (target: live portion under 2 minutes, full walkthrough under 5)
1. `docker-compose up` — everything starts, normal background traffic ticking quietly, low scores.
2. Fire scenario: `python simulator/fraud_scenarios.py --scenario layering --hops 4`.
3. Watch flags appear independently across all 3 branch terminals — narrate that each score
   alone is borderline; no branch would escalate this on its own.
4. Agent terminal streams live reasoning the instant the pipeline triggers — structuring
   agent, then Money-Trail Agent's hop-by-hop tool calls, unprompted.
5. Neo4j Browser: refresh the convergence query — visually show 3 (or N) placement nodes
   funneling through however many hops into one node, ending at a cash-out sink.
6. Adversarial verification streams (prosecutor/defense/judge), then the report drafts
   automatically, status PENDING_REVIEW.
7. Run `python review_report.py --approve <token>` live — the one human step in the entire run.
8. Show `fl_status` — the case just became a training label, next round improves detection.
9. Close with a PRE-COMPUTED plot from the Week 3 ablation runs (AUC-over-rounds, FP rate
   with/without each mechanism) — do not attempt to show FL improvement live in one run.

## Folder Structure
```
fedshieldv2/
├── CLAUDE.md
├── SESSION_PLAN.md
├── docker-compose.yml
├── .env
├── requirements.txt
├── branch_node/
│   ├── Dockerfile
│   ├── producer.py
│   ├── consumer.py
│   ├── masking.py          # global-salt token scheme
│   ├── model.py             # PyTorch logistic regression, loads shared/lr_model.json at startup
│   ├── train_model.py       # offline training script (see "Session 3 Update" in this file)
│   ├── fl_client.py         # Flower client wrapping model.py
│   └── graph_writer.py      # writes masked edges to Neo4j
├── fl_server/
│   ├── Dockerfile
│   └── server.py            # Flower server, FedAvg
├── graph/
│   ├── schema.py            # Neo4j constraints/setup
│   └── queries.py           # check_convergence, get_outgoing_txns, get_incoming_txns
├── agents/
│   ├── state_graph.py       # LangGraph StateGraph wiring
│   ├── structuring_agent.py
│   ├── money_trail_agent.py
│   ├── adversarial/
│   │   ├── prosecutor_agent.py
│   │   ├── defense_agent.py
│   │   └── judge_agent.py
│   ├── label_generator.py
│   └── report_agent.py
├── orchestrator/
│   └── listener.py           # Redis pub/sub subscriber -> triggers state_graph.py
├── simulator/
│   ├── data_generator.py
│   ├── layering_scenario.py  # configurable N-hop, time compression, ground truth logging
│   └── customers.py
├── evaluation/
│   ├── visualize_model.py    # feature distributions, ROC curve, confusion matrix, metrics table
│   ├── metrics_logger.py
│   ├── run_ablation.py       # toggles the 4 conditions, repeats runs
│   └── plots.py
├── review_report.py          # human CLI approval script
├── shared/
│   ├── config.py             # thresholds, topic names, constants (FRAUD_THRESHOLD here is unused - see "Session 3 Update")
│   ├── redis_keys.py
│   ├── schemas.py
│   └── lr_model.json          # trained model weights + standardization stats + tuned threshold + metrics
└── data/
```

## Environment Variables (.env)
```
ANTHROPIC_API_KEY=your_key_here
KAFKA_BROKER=kafka:9092
REDIS_URL=redis://redis:6379
NEO4J_URI=bolt://neo4j:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your_password_here
FL_SERVER_ADDRESS=fl_server:8080
FRAUD_THRESHOLD=0.75
JUDGE_CONFIDENCE_THRESHOLD=0.8
CTR_THRESHOLD_USD=10000
GLOBAL_TOKEN_SALT=your_salt_here
ENABLE_ADVERSARIAL_VERIFICATION=true
ENABLE_FL_FEEDBACK=true
```
