# FedShieldV2 — Full Project Test, From Absolute Scratch

**Start here if you want to test the whole project yourself**, not just the agentic pieces. This
combines `DEMO_RUNBOOK.md` (data generation, model training) and `DEMO_RUNBOOK_AGENTS.md`
(the autonomous agent pipeline + FL) into one linear, self-contained sequence — every command below
was run for real, in order, on a truly wiped setup, right before this file was written. If your
output doesn't roughly match what's shown, stop and fix it before moving on.

**Recommended setup: at least 3 terminal windows** (a 4th and beyond only needed for Step 9's FL
round), all starting in the project folder:
```bash
cd ~/Desktop/fedshieldv2
```

**Total time:** a few minutes for Steps 1-4, then **20-30 minutes of hands-off waiting** during
Step 7 (the autonomous investigation), then a couple more minutes for Steps 8-9. Budget ~40 minutes
total, with only ~10 minutes of it needing your attention.

---

## Step 0 — Prerequisites (Terminal 1)

- Docker Desktop running.
- Ollama running with the model pulled:
```bash
ollama list   # should show qwen2.5:7b
```
If Ollama isn't running: `brew services start ollama`.
- Activate the project's Python environment:
```bash
source .venv/bin/activate
```
You should see `(.venv)` in your prompt from here on in every terminal you open.

---

## Step 1 — Wipe everything (Terminal 1)

No containers, no data, no trained model left over from any previous run:
```bash
docker compose down -v
```
**Expect:** containers/volumes/networks reported as stopped and removed.

---

## Step 2 — Regenerate all simulator data from scratch (Terminal 1)

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

## Step 3 — Train the fraud-scoring model from scratch (Terminal 1)

```bash
PYTHONPATH=. python3 branch_node/train_model.py
```
**Expect**, after ~30-60 seconds of batch generation:
```
Chosen decision threshold: 0.3
Recall on cash deposits/withdrawals (the catchable fraud):  1.000
...
Saved trained model -> .../shared/lr_model.json
```
(Recall on layering hops specifically will vary run to run, typically 85-95% — that's expected;
those hops are feature-for-feature similar to an ordinary large transfer, which is exactly why the
graph-tracing agents exist, not a training bug.)

---

## Step 4 — Bring the Docker stack up (Terminal 1)

```bash
docker compose up -d
docker compose ps
```
**Expect:** all 6 services (`redis`, `neo4j`, `kafka`, `branch-loc1/2/3`) show `Up`/`healthy` within
about 20-30 seconds. Confirm the branches picked up the fresh model:
```bash
docker logs fedshieldv2-branch-loc1 --tail 5
```
```
[consumer:loc1] loading trained logistic regression model...
[consumer:loc1] model loaded, decision threshold=0.3
[consumer:loc1] listening on txns.loc1 ...
```

---

## Step 5 — Start the always-on autonomous listener (Terminal 2)

This is the piece that makes everything from here on autonomous. Leave it running in this terminal
and watch it — nothing after this step is manually triggered except firing the transaction stream
in Step 6.
```bash
cd ~/Desktop/fedshieldv2 && source .venv/bin/activate
NEO4J_URI=bolt://localhost:7688 REDIS_URL=redis://localhost:6380 PYTHONPATH=. python3 -u orchestrator/listener.py
```
**Expect:**
```
[listener] subscribed to 'fraud_events', waiting for flagged transactions...
```
Then silence.

---

## Step 6 — Fire the transaction stream (Terminal 3)

This is the **only** other manual step in the whole run.
```bash
cd ~/Desktop/fedshieldv2 && source .venv/bin/activate
KAFKA_BROKER=localhost:29092 PYTHONPATH=. python3 branch_node/producer.py \
  --files data/background.json data/layering_hops4_events.json \
  --background-window-seconds 20
```
**Expect:** ~216 lines streaming by over 60-70 seconds, with `<-- FLAGGED` on the suspicious ones.

---

## Step 7 — Watch Terminal 2 react entirely on its own

**This is the long step — budget 20-30 minutes, hands off.** You'll see lines like:
```
[listener] token=<...> (structuring confidence=LOW) Money-Trail Agent concluded: insufficient_evidence
[listener] token=<...> (structuring confidence=HIGH) Money-Trail Agent concluded: convergence_found
[listener] token=<...> Verdict Agent: GUILTY (confidence=0.95) - 13 label(s) written
[listener] token=<...> already has evidence - skipping duplicate flag
```
The moment you see a `Verdict Agent: GUILTY ... 13 label(s) written` line, the whole fraud ring has
just been resolved at once — that's why several `already has evidence` lines typically follow
immediately after. Wait until no more `-> investigating` lines appear (roughly 25-30 flagged
accounts total, exact count varies slightly run to run based on false positives).

---

## Step 8 — Verify the results against ground truth (Terminal 1)

```bash
PYTHONPATH=. python3 -c "
import json, redis
from branch_node.masking import token_for
r = redis.from_url('redis://localhost:6380')
gt = json.load(open('data/layering_hops4_ground_truth.json'))
consolidation_token = token_for(gt['consolidation_account'])
fraud_tokens = {token_for(a) for a in gt['fraud_accounts']}
flagged = {t.decode() for t in r.smembers('flagged_accounts')}

correct = sum(1 for tok in fraud_tokens if json.loads(r.get(f'evidence:{tok}') or '{}').get('convergence_node') == consolidation_token)
fp_wrong = sum(1 for tok in (flagged - fraud_tokens) if json.loads(r.get(f'evidence:{tok}') or '{}').get('stop_reason') == 'convergence_found')
print(f'Real fraud correctly traced: {correct}/13')
print(f'False positives incorrectly flagged: {fp_wrong}/{len(flagged - fraud_tokens)}')
print('Verdict keys:', len(r.keys('verdicts:*')))
for branch in ['loc1', 'loc2', 'loc3']:
    print(f'labels:{branch} waiting:', r.llen(f'labels:{branch}'))
"
```
**Expect:**
```
Real fraud correctly traced: 13/13
False positives incorrectly flagged: 0/N
Verdict keys: 13
labels:loc1 waiting: 6
labels:loc2 waiting: 3
labels:loc3 waiting: 4
```
(`N`, the false-positive count, varies slightly run to run since it depends on the freshly trained
model — should always be 0 incorrectly flagged. The loc1/loc2/loc3 label split is deterministic for
this dataset and should always total 13.)

---

## Step 9 — Close the loop: run a real FL round (Terminals 1, 4, 5, 6)

The labels sitting in Redis are about to become an actual model update — this is the payoff. FL
rounds are a manual, host-side process by design (see `CLAUDE.md`'s Session 5 notes); starting one
is the last manual step in this entire test.

**Terminal 4 — the FL server:**
```bash
cd ~/Desktop/fedshieldv2 && source .venv/bin/activate
PYTHONPATH=. python3 fl_server/server.py
```
**Expect:** it starts, prints an initial validation AUC (~0.99), and waits for 3 clients.

**Terminals 5, 6, and one more (or run one at a time) — one client per branch.** `REDIS_URL` must
be set explicitly here — `fl_client.py` otherwise defaults to the in-Docker hostname `redis`, not
reachable from the host:
```bash
cd ~/Desktop/fedshieldv2 && source .venv/bin/activate
BRANCH_ID=loc1 REDIS_URL=redis://localhost:6380 PYTHONPATH=. python3 branch_node/fl_client.py
```
(repeat with `BRANCH_ID=loc2` and `BRANCH_ID=loc3`, same `REDIS_URL` prefix, in separate terminals)

**Expect**, in each client's terminal, across the 5 rounds it runs automatically:
```
[fl_client:loc1] round 1: local partition ~500-550 rows (~30 fraud) - includes 6 real agent-verified label(s)
[fl_client:loc1] local training done (20 epochs on ... rows), final loss=0.2XXX
[fl_client:loc1] round 2: local partition ... rows ... - includes 0 real agent-verified label(s)
... (rounds 3-5 also show 0 real labels)
```
**The line that matters is round 1's** `includes 6 real agent-verified label(s)` (or 3/4 for the
other branches) — the real cases the agents just confirmed are genuinely training the model, not
just sitting as data. Rounds 2-5 correctly show 0 — real labels are drained the moment they're
used, so each one trains the model exactly once.

**Terminal 1, once all 3 clients finish** — confirm the round completed and the buffers are now
empty:
```bash
docker exec fedshieldv2-redis redis-cli LLEN labels:loc1   # should be 0
```

---

## You just verified, end to end

Data generation → model training → real-time scoring → autonomous cross-branch investigation →
verdict → training label → federated learning round — every piece, working together, starting from
nothing. If you want to go further, `DEMO_RUNBOOK_AGENTS.md`'s notes section covers the harder
3-ring scenario (expect it to take hours, and one ring to show known, documented misses — see
`SESSION_6_SUMMARY.md`).
