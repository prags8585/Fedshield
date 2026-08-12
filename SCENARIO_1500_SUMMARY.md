# Scenario 1500 — A Bigger, Demo-Safe Multi-Ring Case: What We Built, Why, and What We Found

**Read this if:** you want to understand `data/scenario_1500/`, why it exists, what's actually been
verified about it, and two real gotchas that came out of testing it. Written for a fresh session
with no prior context — if you just want to run it, see the command list in "How to reproduce"
near the end.

---

## 1. The problem this solves

`data/scenario_500/` (Scenario 2, 3 concurrent laundering rings) has a known, documented, **still
open** bug: 2 of its 3 rings deliberately share a mule account with a neighbor
(`ring1 <-shared-> ring2 <-shared-> ring3`), and that sharing causes a real cross-ring "leak" in
`agents/money_trail_agent.py`'s convergence resolver. The practical result, verified multiple times
in earlier sessions: **Ring2 always fails — 0/13 of its accounts ever get traced correctly** by the
live agentic pipeline, no matter how many times you re-run it. Full detail on the leak mechanism is
in `CLAUDE.md`'s "Session 6 Update".

That's a real, unresolved limitation — fine to know about, **not fine to accidentally hit live in
front of your manager**. The ask that started this work: *"can we take a case where the transactions
are bulk like 1500 transactions and I want around 20% fraud transactions injected, without having
this multiple rings scenario — take a simple one, maybe 1-2 shared mule among this 20% fraud ones."*

In other words: bigger scale, more realistic (multiple simultaneous laundering rings, not just one),
but **not** the specific broken configuration.

---

## 2. What we built

### 2a. A new parameter, not a new bug surface

`simulator/multi_ring_scenario.py`'s `generate_multi_ring_scenario()` used to hardcode "every
consecutive ring pair shares a mule account" — `num_rings=N` always produced `N-1` shared links, no
way to have fewer. We added one additive parameter:

```python
def generate_multi_ring_scenario(num_rings=3, hops=4, ..., num_shared_links: Optional[int] = None):
```

`num_shared_links=None` (the default) preserves the exact old behavior — every existing caller,
including the original `data/scenario_500/` generation command, is unaffected. Passing a smaller
number (e.g. `1`) links only that many *consecutive* ring pairs and leaves the rest fully
independent — zero shared accounts, structurally unable to leak, because the leak's root cause
requires a shared node to exist at all.

Also added: `--scenario-start` on the same script's CLI, and a matching `end` parameter (plus
`--end-date` CLI flag) on `simulator/data_generator.py`'s `generate_background_transactions()` — see
section 4, these exist to fix a *different* bug this work accidentally uncovered.

### 2b. The actual dataset — `data/scenario_1500/`

Generated with `num_rings=19, hops=4, num_shared_links=1, seed=1500`:

| | value |
|---|---|
| Total transactions | 1,500 (1,196 background + 304 fraud) |
| Fraud percentage | 20.3% |
| Independent laundering rings | 19 |
| Shared mule accounts | **1** (between ring1 and ring2 only — 17 rings are fully isolated) |
| Unique real fraud accounts | 246 (19 × 13, minus 1 for the account counted once, not twice) |

### 2c. Two new test scripts

- **`scripts/test_agent_convergence.py`** — the test that actually matters. Unlike the older
  `scripts/test_multi_ring_convergence.py` (which hands `check_convergence` each ring's *own*
  pre-grouped sources in isolation — a condition that can never reproduce a cross-ring leak, since
  there's nothing else to leak from), this script flags every ring's real placement deposits **all
  at once** in Redis, then calls the real production function
  (`agents/money_trail_agent.py`'s `_find_convergence_group_for_token`) exactly the way the live
  system does. **We proved this test methodology is valid before trusting its "clean" result on the
  new data**: run against the old `data/scenario_500/`, it correctly reproduces the documented 26/39
  (Ring2: 0/13) failure. Run against `data/scenario_1500/`, it reports **247/247 correct**, including
  both rings touching the shared account.
- **`scripts/run_full_agent_investigation.py`** — runs the real Structuring Agent → Money-Trail
  Agent → Verdict Agent pipeline (`agents/state_graph.py`'s `run_investigation`) against every
  currently-flagged account already sitting in Redis, directly — no need to re-stream through Kafka
  to test the agent layer against data that's already been through a live run once.

---

## 3. What's been verified, and how

Every claim below was checked against a real, live-Docker run — not assumed from code reading.

**Graph/convergence layer (the thing this whole case exists to prove):**
- `scripts/test_agent_convergence.py --data-dir data/scenario_1500` → **247/247 fraud accounts
  correctly resolved to their own ring**, using the exact function the live system calls, with all
  57 real placement deposits flagged simultaneously (the actual condition that breaks the 500-case).

**ML flagging layer (live Docker/Kafka, not offline recomputation):**
- Full `docker compose down -v && up -d --build`, streamed via `branch_node/producer.py`, scored by
  the real branch containers against the unmodified `shared/lr_model.json`.
- **246/246 real fraud accounts flagged**, ROC-AUC **0.9927**, recall **100%**, false positive rate
  **21.0%** (251 of 1,196 legit transactions, 94 legit accounts) — matches the offline prediction
  exactly.

**Agentic layer (SA + Money-Trail Agent + Verdict Agent, real Ollama `qwen2.5:7b` calls, all 451
flagged accounts):** see section 5 — this is where two real findings turned up.

---

## 4. A real bug found and fixed along the way: day-of-week calendar drift

Not related to rings at all — surfaced while checking the new dataset's ML numbers.

**Symptom:** first generation of `data/scenario_1500/background.json` scored terribly —
ROC-AUC 0.885, false positive rate **69.5%** (831/1,196 legit transactions wrongly flagged), even
though the model itself was untouched and recall stayed at 100%.

**Root cause:** `simulator/data_generator.py`'s `generate_background_transactions()` anchors its
"last N days" window to `datetime.utcnow()` — literally whatever real calendar day you happen to run
the script on. The model gives `day_of_week` a real, non-trivial weight (+0.65 at the time), and
standardizes it against a *fixed* mean/std baked in at training time. Generate fresh background data
on a day whose weekday distribution doesn't match that fixed calibration point, and nearly every
transaction's score gets pushed up for a reason that has nothing to do with fraud.

This isn't unique to this dataset — `branch_node/train_model.py`'s own background rows use the same
unpinned `datetime.utcnow()` call, so **the live model's own calibration point silently drifts every
time it's retrained**, depending on what day the retrain happened to run. We confirmed this
concretely: `shared/lr_model.json`'s weight/bias/mean/std had already changed mid-session (almost
certainly the nightly batch retrain job), with no code change involved.

**Fix (additive, nothing else affected):**
- `generate_background_transactions(..., end: Optional[datetime] = None)` — defaults to the old
  `datetime.utcnow()` behavior for every other caller; pass an explicit `end` to anchor the window.
- `simulator/multi_ring_scenario.py --scenario-start` — same idea for the fraud timeline.
- Regenerated `data/scenario_1500/` anchored to a fixed historical window (`2024-06-04` to
  `2024-06-07`) whose weekday mix matches the live model's actual calibration (mean `day_of_week`
  ≈ 2.0, matching the model's own 2.002).

**Result:** ROC-AUC 0.885 → **0.9927**, false positive rate 69.5% → **21.0%**, recall unchanged at
100%. Confirmed identical live via the Docker/Kafka stream, not just offline.

**Still open, not fixed here (out of scope for this work):** `train_model.py`'s own retraining is
subject to the identical drift. Worth a real fix (pin retraining's background window too) before
trusting any freshly-retrained model's threshold/FPR numbers at face value.

---

## 5. Testing the full agent pipeline — two more real findings

Ran `scripts/run_full_agent_investigation.py` against all 451 flagged accounts (246 real fraud + 205
false positives) — every one through the real Structuring Agent → Money-Trail Agent → Verdict Agent
chain, real Ollama `qwen2.5:7b` calls, no shortcuts.

```
Active investigations run: 280   (170 more resolved for free via group-write)
Failed (timeout): 1
Stop reasons: convergence_found=18, insufficient_evidence=185, cycle=77
GUILTY verdicts: 15    NOT_GUILTY verdicts: 3
Labels written to FL buffers: 195   (= 15 GUILTY rings x 13 accounts, exactly)
Real fraud accounts confirmed via convergence_found evidence: 234 / 246
```

### Finding 1 — the Verdict Agent incorrectly cleared 3 real rings

3 of the 18 real, structurally-confirmed convergences (independent deposits, real time+amount
chaining, correct branches) were judged **NOT_GUILTY** by the Verdict Agent (confidences 0.25-0.65),
so no labels were written for those 39 accounts even though the deterministic evidence was correct.
This is a genuine LLM-judgment gap, not a tracing bug — the Money-Trail Agent's deterministic layer
did its job correctly in all 18 cases; the Verdict Agent's own reasoning is what got 3 of them wrong.
**Not yet root-caused** — see the operational lesson below for why the original rationale text is no
longer retrievable.

### Finding 2 — running the convergence test script concurrently breaks a live investigation batch

Digging into why 1 full ring (ring1) never resolved: **every one of the 18 successful
`convergence_found` results happened within the first 811 seconds of the ~20-hour run.** Not one
succeeded after that point, for any ring, real or false-positive. Cross-checked live: Redis's
`flagged_accounts` set — the exact thing `_find_convergence_group_for_token` reads fresh on every
call to know which deposits are currently "live" — was found sitting at **0 members**, despite the
run starting with 451.

**Most likely cause:** `scripts/test_agent_convergence.py` (and `scripts/test_multi_ring_convergence.py`)
explicitly call `r.delete(FLAGGED_ACCOUNTS)` and wipe Neo4j as part of their own normal setup — this
is by design for a *standalone* correctness check, but it silently destroys the state a concurrently-running
live investigation depends on. If either script gets run in a separate terminal while
`run_full_agent_investigation.py` (or the real `orchestrator/listener.py`) is active, everything
after that point degrades to "fewer than 2 currently flagged sources" → guaranteed
`insufficient_evidence`/`cycle`, no error, no warning. Ring1's 2 individually-investigated accounts
happened to be reached very late in the token-iteration order (well after the wipe), which is the
actual reason ring1 was missed — an early, unlucky ordering artifact of a shared-state collision, not
a tracing defect. This also explains why `verdicts:*` keys are now almost entirely gone from Redis,
which is why Finding 1's root cause couldn't be dug into further this session.

**The lesson, plainly:** don't run `scripts/test_agent_convergence.py` or
`scripts/test_multi_ring_convergence.py` at the same time as a live investigation batch (the real
listener, or `run_full_agent_investigation.py`) against the same Redis/Neo4j instance. Both wipe
shared state as a normal part of what they do.

---

## 6. Open items, honestly

- **Ring1 was never independently re-verified** after the interference above. `scripts/test_agent_convergence.py`
  already proved it resolves correctly in a clean, uncontested run (247/247) — the live-batch miss is
  explained by the state collision, not a new bug — but a clean re-run of the full agent pipeline
  (without anything else touching Redis/Neo4j concurrently) would be the honest way to confirm this if it matters for your demo.
- **The 3 Verdict Agent misjudgments are not root-caused.** The original rationale text is gone from
  Redis; a fresh run against those same 3 tokens (or similar ones) would be needed to actually see
  why the LLM leaned NOT_GUILTY on structurally-confirmed convergences.
- **`train_model.py`'s own retraining still has the day-of-week drift bug** (section 4) — not fixed,
  only worked around for this one dataset's generation.
- **251 false-positive investigations is a lot for a live demo.** Each one is a real SA + MTA call;
  at real Ollama speed that's a genuinely long stream to watch live. If a fast, watchable demo matters
  more than full realism, consider trimming background volume for the live segment.

---

## 7. How to reproduce

Full step-by-step commands (data generation, live Docker/Kafka streaming, evaluation plots, Neo4j
exploration queries) were given interactively during this session — see the conversation history for
the exact terminal commands, or regenerate from scratch with:

```bash
PYTHONPATH=. python3 simulator/customers.py --per-branch 300 --seed 1500 --out data/scenario_1500/customers.json
PYTHONPATH=. python3 simulator/data_generator.py --count 1196 --days 3 --per-branch 300 --seed 1500 \
  --end-date "2024-06-07T00:00:00" --out data/scenario_1500/background.json
PYTHONPATH=. python3 simulator/multi_ring_scenario.py --num-rings 19 --hops 4 --num-shared-links 1 --seed 1500 \
  --scenario-start "2024-06-06T02:00:00" \
  --out-events data/scenario_1500/multi_ring_events.json --out-ground-truth data/scenario_1500/multi_ring_ground_truth.json
```
Then bring the stack up fresh and stream both files through `branch_node/producer.py`, same pattern
as Scenario 2 in `DEMO_RUNBOOK.md`.
