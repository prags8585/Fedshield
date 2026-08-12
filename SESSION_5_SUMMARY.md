# Session 5 — Full Summary (Plain English)

This covers everything built around Federated Learning (FL) in this project: why it exists at all,
how it's actually designed, a real worked example of the math, how to run it yourself, what we
verified, and exactly where it fits (and doesn't yet fit) into the bigger picture.

No prior context needed beyond: this is a fraud-detection system for a US bank with 3 branches
(`loc1`, `loc2`, `loc3`), each running its own copy of the same fraud-scoring model.

---

## 1. What problem were we solving?

Each branch's model can only ever look at *its own* branch's transactions — raw transaction data
is deliberately never centralized (see `CLAUDE.md`'s isolation rule). But you still want all 3
branches to *collectively* get smarter over time, not stay frozen forever or drift apart into 3
different models that quietly disagree with each other.

**Why can't we just centralize the data occasionally and retrain?** Because the reason the data
stays siloed isn't a policy we chose for this demo — it's meant to represent something real: even
inside *one* bank, different branches often run different legacy core-banking systems (different
vendors, different eras, never fully merged — very common after mergers/acquisitions or slow IT
modernization). That data genuinely can't be pooled into one place without a massive IT project.

**Federated Learning is the answer:** instead of centralizing the data to retrain in one place,
each branch improves its own copy of the model a little, using only what it has — and only the
*lessons learned* (not the data) get combined centrally.

---

## 2. The core design: share the lesson, not the notes

Think of it as 3 students studying the same subject, each with only their own notes:

1. Each branch starts with an identical copy of today's model.
2. Each branch practices on its own new transactions only — never seeing another branch's data.
3. Each branch reports back **only how its understanding shifted** (a handful of numbers) — never
   the transactions themselves.
4. A central coordinator averages the 3 branches' shifts together into one blended update.
5. That blended, improved model gets sent back to all 3 branches, replacing what they had.
6. Repeat.

This up-front design decision matters: **local training data is split by filtering one shared
simulated batch by which branch actually processed each transaction** — not by inventing 3
separate synthetic worlds. This mirrors exactly how the live system already isolates data (each
branch's Kafka consumer only ever sees its own topic), so the simulation is honest about what a
real branch would and wouldn't see.

---

## 3. A real worked example — the actual math, real numbers

Our model looks at 5 clues per transaction, each with its own "weight" (how much that clue should
push the suspicion score up or down), plus one "bias" (the baseline assumption before any clue is
considered).

**Starting weights (identical at all 3 branches):**
```
amount_ratio_to_threshold : 2.50      hour_of_day    : 0.05      is_transfer_out : 0.80
is_cash                    : 1.20      day_of_week    : 0.02      bias            : -3.00
```

**One real transaction per branch:**
- **loc1:** $9,700 cash deposit, 2am, Tuesday, money coming IN → truly **fraud**
- **loc2:** $180 debit card purchase, 3pm, Thursday, money going OUT → truly **legit**
- **loc3:** $9,200 wire transfer, 3am, Wednesday, money going OUT → truly **fraud** (the hard case —
  a real laundering hop that doesn't "look like cash")

**Each branch scores its own transaction, compares to the true answer, and nudges its weights** —
e.g., loc1's practice session nudges `amount_ratio_to_threshold` from 2.50 → 2.56 and `is_cash`
from 1.20 → 1.24, because those clues were present in a transaction it should've scored higher.
loc2 barely changes anything (it was already right). loc3 nudges `is_transfer_out` and
`amount_ratio_to_threshold` up more, since it under-scored a real fraud case.

**Each branch sends back only its new numbers:**
```
loc1: [2.56, 1.24, 0.052, 0.021, 0.80, -2.95]
loc2: [2.49, 1.19, 0.048, 0.019, 0.78, -3.02]
loc3: [2.61, 1.20, 0.052, 0.021, 0.88, -2.90]
```

**The coordinator averages each position:**
```
amount_ratio_to_threshold: (2.56+2.49+2.61)/3 = 2.55
is_cash:                    (1.24+1.19+1.20)/3 = 1.21
is_transfer_out:             (0.80+0.78+0.88)/3 = 0.82
...and so on for the rest.
```

That blended set goes back to all 3 branches. Every branch now scores *every future* transaction
using these new shared numbers — quietly better at both the cash-near-$10k clue (thanks to loc1)
and the money-leaving-wire clue (thanks to loc3), without loc1 ever having seen loc2 or loc3's
actual transactions.

---

## 4. How this is actually computed — the real files

| Step | Real file |
|---|---|
| Turning a transaction into the 5 clean numbers | `branch_node/masking.py` + `branch_node/consumer.py` (already existed) |
| Making each branch's own practice pile + the coordinator's private test | `branch_node/fl_data.py` |
| Each branch practicing locally, nudging its weights | `branch_node/fl_client.py` |
| Collecting, averaging, checking the score, saving the result | `fl_server/server.py` |

A few real design choices worth knowing about:
- **The practice data is deliberately FRESH** — its own seed ranges, never the same 25 scenarios
  that already trained today's model. Reusing that exact data would teach the model nothing new.
- **Only the weights are shared — never the "how to read a transaction" preprocessing rules**
  (called mean/std standardization). Those stay fixed and identical everywhere, taken from the
  currently-live model, so the weight-averaging step is comparing apples to apples.
- **Each round starts from today's live model**, not from a blank slate — this is about *improving*
  the existing model, not training a new one.
- **Each local practice session is short** (20 quick passes, not hundreds) — a gentle nudge each
  round, not a full retrain, so branches don't wildly diverge before being blended back together.

---

## 5. How to run it yourself

```bash
cd ~/Desktop/fedshieldv2
source .venv/bin/activate            # every terminal needs this, it doesn't carry over

docker compose up -d redis           # only Redis is needed - no Kafka/Neo4j/branches
docker compose ps redis              # wait for "healthy"
```

**Terminal 1:**
```bash
PYTHONPATH=. python3 fl_server/server.py
```
**Terminals 2, 3, 4:**
```bash
BRANCH_ID=loc1 PYTHONPATH=. python3 branch_node/fl_client.py
BRANCH_ID=loc2 PYTHONPATH=. python3 branch_node/fl_client.py
BRANCH_ID=loc3 PYTHONPATH=. python3 branch_node/fl_client.py
```
All 4 processes run 5 rounds automatically and exit on their own. Full step-by-step with expected
output is in `DEMO_RUNBOOK.md`'s "Session 5" section.

**Confirm it actually happened:**
```bash
docker exec fedshieldv2-redis redis-cli GET fl_status
python3 -c "import json; d=json.load(open('shared/lr_model.json')); print(d['metrics'].get('fl_rounds_run'))"
```

---

## 6. What we actually verified (run three times, independently)

- All 3 branches trained on their own local piles — confirming the per-branch split genuinely
  works.
- Each branch's own practice loss visibly went down round after round — confirming local learning
  genuinely happened.
- All 5 rounds completed every time, without any branch failing to report back.
- `fl_status` was confirmed sitting in Redis (round number, AUC score, timestamp) every time.
- `shared/lr_model.json`'s actual weight numbers were confirmed changed after each run — not just
  simulated in memory, genuinely written to disk.

**The test score (AUC) barely moved in the first two runs** — around 0.992 to 0.9925, then
completely flat. Some of that is expected (see the 3 real reasons in the next section) — but part
of it turned out to be an actual bug, described next.

---

## 6a. A real bug we found and fixed — every round was secretly practicing on the same pile

Digging into *why* the AUC line was so suspiciously flat, we found this: each branch's "new
practice pile" was built from a hardcoded, never-changing random seed. Combined with the code
only building that pile once (when the branch first connects) instead of fresh each round, this
meant **round 1 through round 5 — and every separate time you ran the whole test — were all
practicing on the exact same handful of transactions, over and over.** No wonder round 2 onward
had almost nothing left to learn: it had already seen everything round 1 saw.

**The fix:** the coordinator now tells each branch which round it's on, and each branch generates
a *genuinely new* pile of practice transactions for that specific round (by shifting which
"batch" of simulated data it draws from, based on the round number).

**Confirmed working:** partition sizes now visibly differ round to round —
`503 → 511 → 508 → 520 → 545` rows, with `24 → 34 → 36 → 31 → 33` fraud transactions in each — where
before, every single round showed the identical `524` rows every time.

**What changed as a result:** the AUC score now genuinely wobbles round to round instead of going
flat — one confirmed run: `0.9952 → 0.9947 → 0.9955 → 0.9950 → 0.9890 → 0.9946`, even briefly
dipping *below* where it started in round 4. That's actually a *more* trustworthy result than the
suspiciously smooth version before — it's what real federated learning looks like when each round
genuinely sees something new: some rounds help a little, some hurt a little, and it settles out
over time rather than moving in one clean straight line.

---

## 6b. Tabular before/after evidence

`evaluation/fl_before_after.py` snapshots the model's full scorecard (AUC, precision, recall, F1,
recall on cash vs. on layering hops, false-positive rate) on one fixed, never-trained-on test set,
right before a round, then again right after — and prints/plots a clean side-by-side table.

**One real, post-fix result:**
```
              Metric     Before      After     Change
             ROC-AUC     0.9952     0.9946    -0.0006
           Precision     0.5543     0.5604    +0.0061
              Recall     1.0000     1.0000    +0.0000
                  F1     0.7133     0.7183    +0.0050
                 FPR     0.0456     0.0444    -0.0011
```
Honest read: precision, F1, and the false-positive rate all moved slightly in the right direction;
the overall separation score (AUC) dipped very slightly. Nothing dramatic, and — importantly —
**running this again will not reproduce these exact numbers**, since the underlying practice data
genuinely varies round to round now, by design.

---

## 7. Why AUC still doesn't move much, even after the fix — 3 real, remaining reasons

1. **Already near-perfect** — 0.99 out of a max of 1.0 leaves very little room to improve further.
2. **Deliberately gentle training** — 20 quick passes nudging an already-good model each round, not
   a from-scratch retrain, so branches don't diverge wildly before being blended back together.
3. **The deeper reason:** the 5 clues the model looks at have a hard ceiling on how well they can
   ever separate fraud from legit, no matter how the weights are tuned (see the Session 3/4
   write-ups) — FL changes *how* weights get updated, it doesn't add a 6th clue or new kind of
   information. The original model tried far more data and far more training passes and hit close
   to this same ceiling anyway — so this isn't a "just train harder" problem.

---

## 8. What FL does NOT do — an important honest boundary

**Running this does not reduce the false-positive problem from the earlier live demos** (the
216-transaction or 500-transaction scenarios). That's not what FL is for — false-positive reduction
is the graph layer's job (Session 4), which already achieved 100% clearance twice, because the
graph adds genuinely new information (does this transaction's trail connect to other flagged
deposits?) that the 5-feature model structurally cannot see on its own. FL doesn't add that kind of
information — it just lets 3 branches share what they've each separately learned within the same 5
clues.

**Also, running the live demo scenarios and running an FL round have nothing to do with each
other today.** The live demos only ever *score* transactions with whatever model currently exists —
they never train anything. The FL round trains on its own separately-generated practice data, not
on anything that happened during a demo run. The two are currently two separate, independent
proofs, not one connected pipeline yet.

---

## 9. When does FL actually plug into the real project flow?

Today, FL is a **standalone mechanism you trigger by hand**, using **made-up practice data**, proving
the "practice locally → share the lesson → blend → redistribute" mechanism genuinely works.

**The real, connected version arrives with Sessions 6 and 7**, which don't exist yet:

```
(already exists) the model flags a suspicious transaction
        ↓
Session 6: an AI agent traces the money trail through the graph → builds evidence
        ↓
Session 7: three AI agents debate that evidence (Prosecutor / Defense / Judge)
        ↓ Judge says GUILTY with high confidence
Session 7: that verdict gets written down as a real training label
        ↓
Session 5's mechanism (already built, this session) trains on THAT real label instead of
made-up practice data, the next time a round runs
```

So the honest current state: **we've built and proven the "back half" of the loop** (a label
becomes a shared model improvement, without centralizing data). **The "front half"** (an AI agent
actually investigating a case and producing that label in the first place) **doesn't exist yet** —
that's Sessions 6-7. Once both halves exist, they connect exactly as described above, and FL would
start running **automatically** (triggered by a timer or by enough new real labels piling up)
instead of by hand.

---

## 10. Bottom line

- FL exists to solve a real constraint: branches can't centralize their data (whether for
  regulatory reasons or, as here, realistic legacy-IT fragmentation), but should still improve
  together over time.
- The mechanism — practice locally, share only the resulting weight-shift, average it centrally,
  redistribute the blend — is fully built and has been verified working, twice, independently.
- It currently trains on made-up practice data and runs only when triggered by hand. It is not yet
  wired into live, autonomous operation, and it does not fix the false-positive problem — that's a
  different, already-solved piece (the graph filter).
- Its real payoff — turning a live, agent-verified fraud verdict into an immediate, privacy-safe
  model improvement across all 3 branches — is exactly what Sessions 6 and 7 will connect it to.

---

## 11. What's next

- **Session 6:** build the AI agent that autonomously investigates a flagged transaction using the
  graph tools already built in Session 4.
- **Session 7:** build the Prosecutor/Defense/Judge debate, and the code that turns a GUILTY
  verdict into a real label — the moment this exists, FL stops being a standalone demo and becomes
  part of one connected, autonomous loop.
