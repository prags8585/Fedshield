# FedShieldV2 — Session-Wise Execution Plan

## How to use this file
Each session below is scoped to be run as its own fresh Claude Code chat, inside this same
`fedshieldv2` folder. `CLAUDE.md` loads automatically every session and has the full locked
architecture, schemas, and design decisions — you do not need to re-explain the project.

**To start a session:** open a terminal, `cd` into this folder, start Claude Code, and say
something like: *"Read SESSION_PLAN.md and start Session 3."* Claude will pick up full context
from CLAUDE.md plus that session's specific tasks below.

**Mark sessions done** by checking them off in this file as you complete them, so a fresh chat
in a later session knows what already exists.

Rough timeline: 3 weeks, ~3 sessions/week. If a session runs long, it's fine to split it further
— better to finish a smaller scope cleanly than half-finish a large one.

---

## [x] Session 1 — Foundation & Scaffolding
**Goal:** `docker-compose up` starts an empty skeleton with no errors.
- Create the full folder structure from `CLAUDE.md`.
- `docker-compose.yml`: Kafka, Redis, Neo4j, Flower server stub, 3 branch container stubs
  (empty entrypoints for now, just prove networking/health checks work).
- `.env`, `requirements.txt`.
- `shared/config.py`, `shared/redis_keys.py`, `shared/schemas.py` (Pydantic models for the
  Kafka message, Neo4j node/edge, Redis value shapes from CLAUDE.md).
- **Exit criteria:** all containers come up healthy, Redis/Neo4j/Kafka reachable from a
  throwaway test script.

## [x] Session 2 — Data & Scenario Generation
**Goal:** running the generator produces a realistic transaction stream on disk/in-memory,
including the injected fraud scenario, matching the schema.
- `simulator/customers.py`, `simulator/data_generator.py` — normal background traffic
  (Faker `en_US` locale, US banking conventions: `customer_id`, `ssn_last4`, 10-digit
  `account_number`, 9-digit ABA `routing_number`).
- `simulator/layering_scenario.py` — the p1/p2/p3 → N-hop chains → consolidation account
  scenario. Placement deposits $8,000-$9,800 (just under the $10,000 CTR threshold). Must
  support: **configurable hop count** (parametrize, don't hardcode 2 or 4), and **timestamp
  compression** (fabricate timestamps spanning real hours, emit events within seconds).
- Ground truth logging: which tokens/accounts are part of the injected fraud, written
  separately, never touched by anything downstream except the evaluation harness later.
- **Exit criteria:** `python simulator/layering_scenario.py --hops 4` produces a correctly
  time-ordered, schema-valid transaction list plus a ground-truth file.

## [x] Session 3 — Kafka Streaming + Masking + Local ML Scoring
**Goal:** live terminal output showing transactions flowing and being scored in all 3 branches.
- `branch_node/producer.py` — streams generated transactions onto each branch's Kafka topic.
- `branch_node/masking.py` — the **global-salt** token scheme (`SHA256(account + GLOBAL_SALT)`)
  — double-check this is NOT branch-salted, that was the old FedShield's scheme and would break
  everything here.
- `branch_node/model.py` — **PyTorch logistic regression** (single linear layer + sigmoid) —
  reworked after the initial pass from an MLP trained on a handwritten heuristic rule to a
  genuinely trained model. See CLAUDE.md's "Session 3 Update" section and `session3_updated.md`
  for the full story, including two real issues found and fixed (a data leak on `account_age_days`,
  and a near-zero-variance instability on `velocity_10min` — both features were removed from the
  model, feature set is now 5, not 7).
- `branch_node/train_model.py` — **new**, offline training script: generates a diverse labeled
  dataset (25 fraud scenarios with varying hop-counts/amounts/timing + 10 background-only
  batches), trains with a real train/test split, saves to `shared/lr_model.json`. Branches only
  load this at startup — no live bootstrap training anymore.
- `evaluation/visualize_model.py` — **new**, generates feature-distribution/ROC-curve/
  confusion-matrix/metrics-table plots from the trained model (`evaluation/plots/*.png`).
- `branch_node/consumer.py` — wires masking + model together, writes scores to Redis, using the
  model's own tuned threshold (currently 0.3) rather than the `.env` `FRAUD_THRESHOLD` constant.
- **Exit criteria:** run the scenario from Session 2 through this pipeline; watch scores tick
  in 3 branch terminals; the 3 placement deposits should score above 0.75. **Confirmed, and
  exceeded** — live run caught all 16/16 fraud transactions and 13/13 fraud accounts, including
  every layering hop, at a documented ~27% false-positive rate on legit traffic (see
  `session3_updated.md` for full metrics and `DEMO_RUNBOOK.md` for the reproducible run).

## [x] Session 4 — Neo4j Graph Layer & Convergence Logic
**Goal:** convergence is detectable via a standalone script, independent of any agent.

**Read `CLAUDE.md`'s "Session 4 Final Plan — Graph Chaining via Neo4j" section first** — it has
the full locked spec (graph structure, exact chaining rule, thresholds, output format, and build
sequencing) from a dedicated design pass. Do not re-derive it from scratch; the summary below is
just a checklist against that spec. Context: a precision/recall investigation (full detail in
`SESSION_3_FULL_SUMMARY.md`) found that 100% recall + 75%+ precision is not reachable with the
current LR's 5 single-transaction features, no matter how it's trained — the point of this
session is to add the one thing that actually contains new information (cross-transaction
convergence), not to re-tune the LR itself.

**Pass 1 — core correctness (do this first, in isolation):**
- `graph/schema.py` — constraints/indexes on `Account.token_id`.
- `branch_node/graph_writer.py` — writes masked transactions as edges (deposits from a `CASH`
  source node, withdrawals to a `CASH_OUT` sink node), timestamped from the transaction's own
  fabricated business time, never wall-clock.
- `graph/queries.py` — `check_convergence(...)`, `get_outgoing_txns`, `get_incoming_txns`, using
  the binary chaining rule from CLAUDE.md: time-ordered within `per_hop_window` (6-8 hours, not
  2 — see CLAUDE.md for why), amount preserved within 0.90-1.05. Cycle-safe. Depth is a safety
  ceiling only (10-12), never the primary stop condition.
- A standalone test script that runs the Session 2 scenario at hops=2, hops=4, and hops=6, and
  confirms `check_convergence` correctly finds the consolidation account each time.

**Pass 2 — evidence, robustness, and confidence (same session, right after Pass 1 passes):**
- Upgrade `check_convergence` to return the full evidence dict from CLAUDE.md (`num_sources`,
  `num_branches`, `paths`, `total_reached_amount`, `amount_preservation_ratio`,
  `time_to_convergence_minutes`, `cycle_detected`, etc.) instead of a boolean.
- Add the convergence-confirmation gates: `min_sources_for_convergence` (3 for this demo),
  `min_branches_for_convergence` (2), `min_preservation_ratio_to_sink` (~70%).
- Inject noise into `simulator/data_generator.py`'s background traffic before trusting any
  result: legitimate fan-in at destination accounts (shared landlord/payroll account) **and**
  unrelated activity on mule accounts themselves (paycheck, rent, small transfers) — right now
  there is nothing hard in the background data to test the time+amount rule against.
- A continuous hop-confidence-score (instead of the binary rule) is a good future refinement but
  is explicitly **not required** for this session — Pass 1's binary rule must be proven correct
  first, on its own, before adding that nuance.
- **Explicitly deferred, not in scope:** split-flow support (one deposit fanning into several
  outgoing transfers). `layering_scenario.py` doesn't generate split-flow ground truth today, so
  this would be unverifiable until the simulator is extended — revisit later, not now.

**Once both passes work, before any ML/model changes:**
- Apply `check_convergence` as a plain downstream filter on top of the LR's flagged transactions
  from the live 216-txn demo scenario (currently ~16 real fraud + ~50-60 false positives) — keep
  only flagged transactions on a path to a real convergence structure. This may resolve most of
  the false-positive problem with no retraining at all. Do this only after the background-noise
  injection above, or the result is artificially clean and not evidence of anything.

- **Exit criteria:** Neo4j Browser visually shows the graph; the standalone script correctly
  identifies convergence regardless of configured hop count; `check_convergence` returns the full
  evidence dict; the downstream-filter experiment has been run against the live demo scenario
  (with background noise added) and its honest result is documented.
  **Confirmed.** `scripts/test_convergence.py` passes at hops=2/4/6. `scripts/test_convergence_noisy.py`
  confirms the real convergence still resolves with mule noise on the fraud-path accounts, and 3
  independent fan-in senders into one shared account correctly do NOT trigger a false convergence.
  `evaluation/downstream_filter_experiment.py` run against the live 216-txn Docker demo: the graph
  filter cleared **100% of the 41 false positives** (54 flagged accounts -> 13) while retaining all
  13/13 real fraud accounts - see CLAUDE.md's "Session 4 Update" for the full write-up, including a
  real concurrency bug found and fixed along the way (branch containers racing to create the Neo4j
  schema constraint before any consumer had ever called it).

**Extension (post-session, not originally scoped) — Scenario 2: multi-ring, cross-branch, 500-txn
dataset.** Built to stress-test whether the graph work generalizes beyond the one original demo
scenario. `simulator/multi_ring_scenario.py` generates 3 concurrent laundering rings (not 1) with
guaranteed cross-branch hops and 2 shared mule accounts linking the rings into one connected web,
in a fresh 500-transaction dataset (`data/scenario_500/`, ~10% fraud). Found and fixed a real gap:
`check_convergence` only returns its single best match per call, so the original filter script
can't be pointed at 3 simultaneous rings as-is — `evaluation/downstream_filter_experiment_scenario_500.py`
fixes this by grouping flagged deposits per ring (via ground truth membership only, not fraud
labels) and running the check once per ring. **Result:** ROC-AUC ~0.98, 100% recall, ~28%
precision — nearly identical to the original demo, evidence the model generalizes. The per-ring
filter cleared 100% of false positives, all 37 real fraud accounts retained. Full detail in
`CLAUDE.md`'s "Session 4 Extension" section; run instructions in `DEMO_RUNBOOK.md`'s "Scenario 2".
**Still outstanding:** a ring-discovery version that doesn't need ground truth to group deposits.

## [x] Session 5 — Federated Learning (Flower)
**Goal:** a manual FL round runs and improves/updates the live scoring model.
- `fl_server/server.py` — Flower server, FedAvg.
- `branch_node/fl_client.py` — wraps `model.py`, participates in FL rounds.
- A small held-out labeled validation set (separate from the live simulation) for measuring AUC.
- `fl_status` written to Redis after each round (round #, AUC, timestamp).
- **Exit criteria:** trigger a round manually, confirm weights update across all 3 branches,
  confirm AUC is logged.
  **Confirmed, run twice independently (once in-session, once by the user in a fresh terminal).**
  All 3 branches trained on genuinely different local partitions (524/511/540 rows, ~25/24/26
  fraud each — filtered by `branch_id` from one shared simulated batch, not 3 separate synthetic
  worlds), local loss visibly decreased each round, 5 rounds completed, `fl_status` confirmed in
  Redis (`round_num`, `auc`, `timestamp`), and `shared/lr_model.json`'s weights were confirmed
  changed (threshold/mean/std/feature_order preserved). Full detail, including why AUC barely
  moved (~0.992 throughout — expected, not a bug) and why this does NOT reduce false positives on
  Case 1/Case 2 (that's the graph filter's job, not FL's), is in `CLAUDE.md`'s "Session 5 Update".
  **Still outstanding:** this is a manual, standalone process — not wired into the live Docker
  branch containers, and not yet fed by real agent-verified labels (Sessions 6-7).

## [x] Session 6 — Agentic Investigation Pipeline (LangGraph core loop)
**Goal:** an end-to-end autonomous run from a flagged deposit to a convergence evidence object,
with zero manual triggering.
- `orchestrator/listener.py` — subscribes to Redis `fraud_events`, invokes the state graph.
- `agents/state_graph.py` — the LangGraph `StateGraph` wiring (structuring agent → money-trail
  agent, conditional routing).
- `agents/structuring_agent.py` — one LLM call (no tools), reasons over score + features.
- `agents/money_trail_agent.py` — tool-calling loop using the Session 4 Neo4j query tools,
  implementing the termination conditions from CLAUDE.md (convergence / dead end / cycle /
  time window / safety ceiling — not a fixed hop cap).
- **Exit criteria:** fire the Session 2 scenario end to end; watch the agent terminal
  autonomously trace and correctly identify convergence, unprompted.
  **Confirmed**, on a clean reset-to-slate run (`docker compose down -v`, fresh
  `layering_hops4_events.json`): the listener autonomously picked up every flagged transaction
  from Redis `fraud_events` with zero manual triggering, and the pipeline correctly traced **13/13
  real fraud accounts to `convergence_found`** with fully accurate evidence (real amounts,
  timestamps, txn_ids, channels — all 3 branches, correct consolidation account) using the final
  model, `qwen2.5:7b` via local Ollama. This session grew well beyond its original scope once
  testing started: the provider was swapped twice (Claude → OpenAI → local Ollama), a harder
  3-independent-ring scenario (`data/scenario_500`) was tested and exposed a real multi-ring
  attribution bug (fixed) plus a still-open shared-account ambiguity, 4 different local models
  were directly compared (`llama3.1:8b`, `qwen2.5:7b`, `qwen3:8b`, `qwen3:14b`) before settling on
  `qwen2.5:7b` for its speed/quality balance, and a final architecture pass removed every point
  where AI discretion (not the tracing logic itself) could silently drop a real case - guaranteeing
  `check_convergence` runs instead of leaving it to the LLM's choice, removing the Structuring
  Agent's hard gate, and retroactively fixing siblings caught by a timing race. **Final,
  re-verified numbers:** 216-case (1 ring) **13/13 real fraud correct, every run, 14/14 false
  positives cleared**; 500-case (3 rings) **26/39 correct** (Ring1 13/13, Ring3 13/13 both clean;
  Ring2 0/13 - the one remaining, fully isolated, still-open shared-mule-leak issue), 22/22 false
  positives cleared. Full plain-English write-up of all of it is in `SESSION_6_SUMMARY.md`; the
  file-by-file technical detail (every bug, every fix, the reasoning for each) is in `CLAUDE.md`'s
  "Session 6 Update" section.

## [x] Session 7 — Verdict Agent + FL Feedback Loop (scoped down — debate deferred, not abandoned)
**Goal:** full verdict pipeline runs, confirmed labels flow into the next FL round automatically.

**Deliberate scope change, decided mid-session 6:** the full Prosecutor/Defense/Judge debate below
was deferred in favor of building the simpler "Baseline" condition first (`agents/verdict_agent.py`
- a single agent, already named as a condition in the Evaluation Plan below). Reasoning: Session
6's precision was already 100% in every real run by this point, so the debate (which exists to
improve precision) had little to catch yet, while being real extra engineering scope; a working
baseline is also needed before the debate-vs-single-agent comparison can be measured anyway. Full
reasoning in `CLAUDE.md`'s "Session 6 Update".

- ~~`agents/adversarial/prosecutor_agent.py`, `defense_agent.py`, `judge_agent.py`~~ — deferred, a
  real future extension (toggle `ENABLE_ADVERSARIAL_VERIFICATION=true`), not built.
- **`agents/verdict_agent.py`** — built. One LLM call, no tools, only ever runs on evidence with
  `stop_reason == "convergence_found"`. Answers a different question than the Money-Trail Agent's
  now-deterministic tracing: not "did the money connect" but "does this confirmed connection prove
  deliberate laundering."
- **`agents/label_generator.py`** — built, as originally planned: plain code (not an agent), writes
  `(features, label="fraud", source="agent_verified")` into each involved branch's retrain buffer
  (`labels:{branch}` in Redis) the moment a verdict clears the confidence bar.
- Wired directly into `agents/money_trail_agent.py` (not a separate `state_graph.py` node) so a
  confirmed group's verdict/labels cover every member of that group at once, matching the evidence
  group-write fix from Session 6.
- `ENABLE_ADVERSARIAL_VERIFICATION` toggle: not yet wired (would switch between this Verdict Agent
  and the deferred debate). `ENABLE_FL_FEEDBACK` toggle: not yet wired (label generation currently
  always runs when the bar is cleared).
- **Exit criteria (label-writing half): confirmed.** Live 216-case run: one real fraud ring
  produced 13 verdicts (all GUILTY, ~0.9+ confidence) and 13 labels in one shot, correctly split
  across all 3 branches, zero manual triggering.
- **Exit criteria (FL pickup half): confirmed.** `branch_node/fl_data.py` gained
  `real_labels_for_branch()` (drains `labels:{branch}` via `LPOP`, converts each `Label` into the
  model's feature-array format) and `build_branch_partition(..., include_real_labels=False)` - an
  additive flag, default False so `evaluation/fl_vs_isolated.py`'s existing synthetic-only
  measurements stay unaffected. Only `branch_node/fl_client.py`'s real per-branch round opts in,
  mixing real labels on top of (not instead of) the synthetic partition. Verified directly: a
  seeded real label was left untouched with the flag off, and correctly mixed in + drained with it
  on. **The full loop is now closed end to end**: flagged transaction → Structuring Agent →
  Money-Trail Agent → Verdict Agent → Label Generator → `labels:{branch}` → the next real FL
  round's training data - autonomous the whole way except starting the FL round itself, which
  remains Session 5's manual, standalone process by design.

## [ ] Session 8 — Report Agent + Human Review Gate + Full Integration
**Goal:** one command runs the entire pipeline from scenario injection to a human-closed case.
- `agents/report_agent.py` — drafts the report (ends with the privacy attestation line),
  writes to Redis with `status: PENDING_REVIEW`.
- `review_report.py` — human CLI approval script (`--approve <token>` / `--reject <token>`).
- Confirm explicitly: report review does NOT gate the label generator/FL feedback — that
  already fired in Session 7's flow the moment the Judge rendered a verdict.
- Full integration pass: fix any rough edges end to end.
- **Exit criteria:** single scenario run, fully autonomous through to a drafted report; running
  `review_report.py` closes the case.

## [ ] Session 9 — Evaluation Harness + Ablation Runs + Demo Rehearsal
**Goal:** the 4-condition ablation results exist as data/plots, and the live demo runs
reliably 3x in a row.
- `evaluation/metrics_logger.py` — AUC-per-FL-round, false-positive rate, detection latency,
  label precision, keyed by which of the 4 conditions is active.
- `evaluation/run_ablation.py` — toggles `ENABLE_ADVERSARIAL_VERIFICATION` /
  `ENABLE_FL_FEEDBACK`, repeats the scenario 5–10x per condition.
- `evaluation/plots.py` — AUC-over-rounds curve per condition, FP-rate comparison.
- Tune timestamp compression and terminal output formatting for demo watchability.
- Dry-run the full demo script from CLAUDE.md three times.
- **Exit criteria:** plots exist and are readable; 3/3 successful demo dry runs.

---

## Deferred (future work, not this build)
- Case 2.1 — single massive deposit (River-style real-time scoring).
- Case 2.2 — same individual across 3 branches (LSH-style behavioral matching).
Both are additive to this architecture without changes to Case 1 — see CLAUDE.md.
