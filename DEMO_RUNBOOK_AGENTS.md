# FedShieldV2 — Agentic Pipeline Demo Runbook (Run From Scratch)

This is the companion to `DEMO_RUNBOOK.md`, covering everything built *after* that runbook: the
autonomous agent pipeline (Structuring Agent → Money-Trail Agent → Verdict Agent → Label Generator)
and the real FL feedback loop. Follow this top to bottom — if your output doesn't roughly match,
stop and fix it before moving on. See `SESSION_6_SUMMARY.md` and `VERDICT_AGENT_SUMMARY.md` for the
plain-English "why," and `CLAUDE.md`'s "Session 6 Update" for the full technical detail.

**Recommended setup: 3 terminal windows**, all starting in the project folder:
```bash
cd ~/Desktop/fedshieldv2
```

---

## Step 0 — Prerequisites (Terminal 1)

- Docker Desktop must be running.
- Ollama must be running with the model pulled:
```bash
ollama list   # should show qwen2.5:7b
```
If it's not running: `brew services start ollama`.
- Activate the project's Python environment:
```bash
source .venv/bin/activate
```

---

## Step 1 — Reset everything to a clean slate (Terminal 1)

```bash
docker compose down -v
docker compose up -d
docker compose ps
```
**Expect:** all 6 services (`redis`, `neo4j`, `kafka`, `branch-loc1/2/3`) show `Up`/`healthy` within
about 30 seconds.

---

## Step 2 — Start the always-on listener (Terminal 2)

This is the piece that makes everything from here on autonomous — leave it running and watch it
react on its own; nothing after this step is manually triggered except firing the transaction
stream itself.
```bash
cd ~/Desktop/fedshieldv2 && source .venv/bin/activate
NEO4J_URI=bolt://localhost:7688 REDIS_URL=redis://localhost:6380 PYTHONPATH=. python3 -u orchestrator/listener.py
```
**Expect:**
```
[listener] subscribed to 'fraud_events', waiting for flagged transactions...
```
Then silence — it's waiting.

---

## Step 3 — Fire the transaction stream (Terminal 3)

```bash
cd ~/Desktop/fedshieldv2 && source .venv/bin/activate
KAFKA_BROKER=localhost:29092 PYTHONPATH=. python3 branch_node/producer.py \
  --files data/background.json data/layering_hops4_events.json \
  --background-window-seconds 20
```
**Expect:** ~216 lines streaming by over roughly 60-70 seconds (see `DEMO_RUNBOOK.md` for what a
normal vs. flagged line looks like).

---

## Step 4 — Watch Terminal 2 react on its own

Once the stream finishes, Terminal 2 will start working through the flagged accounts one at a
time, entirely on its own. **This takes roughly 20-30 minutes** for the full 27-account backlog on
`qwen2.5:7b` (much less than 27x that, since one confirmed group resolves several accounts at
once — see below). You'll see lines like:
```
[listener] token=<...> (structuring confidence=LOW) Money-Trail Agent concluded: insufficient_evidence
[listener] token=<...> (structuring confidence=HIGH) Money-Trail Agent concluded: convergence_found
[listener] token=<...> Verdict Agent: GUILTY (confidence=0.96) - 13 label(s) written
[listener] token=<...> already has evidence - skipping duplicate flag
```
**Narrate this moment if presenting live:** one investigation just confirmed the *whole* laundering
ring at once — all 13 real fraud accounts get their evidence, verdict, and training label written
together, not one at a time. That's why you'll see several `already has evidence` lines
immediately after — those accounts are already fully resolved, no separate investigation needed.

---

## Step 5 — Verify the results against ground truth (Terminal 1)

Wait until Terminal 2 goes quiet (no more `-> investigating` lines), then:
```bash
PYTHONPATH=. python3 -c "
import json, redis
from branch_node.masking import token_for
r = redis.from_url('redis://localhost:6380')
gt = json.load(open('data/layering_hops4_ground_truth.json'))
consolidation_token = token_for(gt['consolidation_account'])
fraud_tokens = {token_for(a) for a in gt['fraud_accounts']}

correct = sum(1 for tok in fraud_tokens if json.loads(r.get(f'evidence:{tok}') or '{}').get('convergence_node') == consolidation_token)
print(f'Real fraud correctly traced: {correct}/13')
print('Verdict keys:', len(r.keys('verdicts:*')))
for branch in ['loc1', 'loc2', 'loc3']:
    print(f'labels:{branch} waiting:', r.llen(f'labels:{branch}'))
"
```
**Expect:**
```
Real fraud correctly traced: 13/13
Verdict keys: 13
labels:loc1 waiting: 6
labels:loc2 waiting: 3
labels:loc3 waiting: 4
```
(The exact loc1/loc2/loc3 split is deterministic for this dataset; the total is always 13.)

---

## Step 6 — Run a real FL round and watch it pick up the real labels (Terminals 1, 4, 5, 6)

This is the payoff: the labels sitting in Redis right now are about to become an actual model
update, not just data sitting idle. FL rounds are a manual, host-side process by design (see
`CLAUDE.md`'s Session 5 notes) — starting one is the only manual step in this entire demo.

**Terminal 4 — the FL server:**
```bash
cd ~/Desktop/fedshieldv2 && source .venv/bin/activate
PYTHONPATH=. python3 fl_server/server.py
```

**Terminals 5, 6, and one more (or run sequentially) — one client per branch.** `REDIS_URL` must be
set explicitly here (same reason as Terminal 2 in Step 2) — `fl_client.py` reads it from
`shared/config.py`, whose default is the in-Docker hostname `redis`, not reachable from the host:
```bash
cd ~/Desktop/fedshieldv2 && source .venv/bin/activate
BRANCH_ID=loc1 REDIS_URL=redis://localhost:6380 PYTHONPATH=. python3 branch_node/fl_client.py
```
(repeat with `BRANCH_ID=loc2` and `BRANCH_ID=loc3` in separate terminals, same `REDIS_URL` prefix)

**Expect**, in each client's terminal, across the 5 rounds it runs (row counts vary slightly by
round — that's expected, each round trains on a different fresh batch of practice data):
```
[fl_client:loc1] round 1: local partition 504 rows (25 fraud) - includes 6 real agent-verified label(s)
[fl_client:loc1] local training done (20 epochs on 504 rows), final loss=0.0XXX
[fl_client:loc1] round 2: local partition 511 rows (34 fraud) - includes 0 real agent-verified label(s)
[fl_client:loc1] local training done (20 epochs on 511 rows), final loss=0.1XXX
... (rounds 3-5 also show 0 real labels)
```
**The line that matters is round 1's**: `includes 6 real agent-verified label(s)` — confirming this
round's training data isn't purely synthetic, it's genuinely mixing in the real cases the agents
just confirmed a few minutes ago. **Rounds 2-5 correctly show 0** — the real labels get drained
(consumed) the moment round 1 uses them, exactly as intended: a real label should train the model
once, not be replayed into every future round forever. Once round 1 finishes, re-run Step 5's
check — the `labels:{branch}` buffers will already show 0 waiting.

---

## Notes for the harder 3-ring scenario (`data/scenario_500`)

Swap Step 3's producer command for:
```bash
KAFKA_BROKER=localhost:29092 PYTHONPATH=. python3 branch_node/producer.py \
  --files data/scenario_500/background.json data/scenario_500/multi_ring_events.json \
  --background-window-seconds 45
```
Expect ~59 flagged accounts instead of 27, and **this will take noticeably longer** (multiple
hours on `qwen2.5:7b` for the full backlog) — see `CLAUDE.md`'s "Session 6 Update" for realistic
timing per model. Also expect two of the three rings to resolve perfectly and the third to show
real misses — that's the known, documented shared-mule-account limitation, not a new bug; see
`SESSION_6_SUMMARY.md` section 6 for the full explanation before assuming something's broken.
