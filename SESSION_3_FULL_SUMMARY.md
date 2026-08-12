# FedShieldV2 — Session 3, Complete Plain-English Summary

This document explains, in simple terms, everything that was decided and built in Session 3 — the
Kafka streaming pipeline, the old model vs. the new model, how the new model was trained, its
evaluation numbers both before and after being exposed to the live demo scenario, and the decision
currently being made about adding a graph layer to fix its biggest weakness. No prior context
needed beyond "this is a fraud-detection demo for a bank with 3 branches."

---

## Part 1 — The Kafka Streaming Pipeline

Before any AI or model can react to a transaction, something has to actually deliver that
transaction to the right place, live, the moment it happens. That's what Session 3 built first.

- **`producer.py`** reads the generated transaction files and sends each one, in order, onto the
  correct branch's Kafka "topic" — think of a topic as a dedicated mail slot, one per branch. The
  fraud scenario is written as if it plays out over 6 fabricated hours; the producer secretly
  compresses that into about 60 real seconds of delivery, so a demo doesn't take 6 hours to watch,
  without changing any of the underlying time-based logic (like "did these 3 deposits happen
  within 40 minutes of each other?").
- **`masking.py`** is the privacy gate every transaction must pass through. It strips the
  customer's real name and real account number and replaces the account with an anonymous code
  called a `token_id` (`token_id = SHA256(account_number + a_secret_salt)`, cut to 16 characters).
  The same real account always produces the same token, everywhere, but the token can't be
  reversed back into the real account number. Nothing downstream — not the model, not Redis, not
  the future graph — ever sees a real name or account number.
- **`consumer.py`** is the process running inside each branch. It listens to its branch's Kafka
  topic, and for every transaction that arrives: masks it, builds a small set of descriptive
  numbers about it ("features"), hands those to the model for a suspicion score, and writes the
  result to Redis (a shared whiteboard all branches can post to, but which never contains real
  names or account numbers). This all happens instantly, per transaction, with no human involved.

**In plain English:** this is the plumbing that makes the system "live" — without it, transactions
would just sit in files forever with nothing reacting to them.

---

## Part 2 — The Old Model vs. the New Model

### The old model (the very first pass, now replaced)

The first version of the scoring model was a small neural network trained on 2,000 made-up
examples, built from a rule a human wrote by hand: *"if it's cash, the amount is close to
$10,000, it's late at night, and the account is very new — call it fraud."* It had never seen a
single real transaction from the simulator before making its first prediction.

**Why it was replaced:** the fraud scenario used in the demo was *also* built to look like "cash +
near $10k + brand new account." So when the old model correctly flagged the fraud, that wasn't the
model discovering anything — it was reciting a rule a human had already written, being tested
against a scenario built to match that exact rule. That proves the pipeline (Kafka → masking →
scoring → Redis) works, but proves nothing about whether the model can actually detect fraud it
wasn't told about in advance.

### The new model (what's live now)

The new model is a genuine **Logistic Regression** — mathematically, one of the simplest real
machine-learning models that exists (draw a straight-line decision boundary through the data),
implemented as a single PyTorch layer + sigmoid. It's trained **offline, once**, on a large,
independently generated, genuinely labeled dataset — not on a handwritten rule, and not on the
same fixed scenario used in the demo. Branches never train anything themselves; they just load the
finished result at startup.

It looks at **5 numbers** per transaction:
1. How close the amount is to the $10,000 reporting threshold
2. Whether it's cash
3. What hour of the day
4. What day of the week
5. Whether money is leaving the account or arriving

Two other numbers were tried and deliberately removed:
- **Account age** — every fraud account in the simulator is brand new, every legit account is
  pre-existing, so the model was just learning "is this a new account?" — a quirk of how the fake
  data was built, not a real fraud signal. Keeping it would have unfairly flagged any real
  customer who simply just opened an account.
- **Recent transaction count** — almost every transaction in the simulated data is the only one
  its account makes in a 10-minute window, so this number barely varies — the rare exceptions blew
  up into wildly unstable influence on the score.

---

## Part 3 — How the New Model Was Actually Trained

The training script (`train_model.py`) does this, every time it's run:

1. **Generates 25 independent, made-up fraud scenarios** — different people, different account
   numbers, different chain lengths (2 to 8 accounts in the laundering chain), and different
   placement amounts (from "textbook structuring" near $9,800 down to much smaller, looser amounts
   like $500–3,000, deliberately designed to blend into ordinary traffic) — plus **10 pure
   background batches** of everyday, non-fraud traffic. None of these 35 batches are the exact
   scenario used in the live demo.
2. **Labels every transaction from the simulator's own answer key** — this is the one place in the
   whole system that answer key is allowed to be read, because this is offline training, not the
   live pipeline.
3. **Splits the data 75% training / 25% testing before looking at any result**, so every number
   reported is measured on transactions the model never got to learn from.
4. **Trains for 300 rounds** using real gradient descent (not a handwritten rule).

Running this produces **5,689 total transactions, 439 of them fraud (about 7.7%)** — 4,266 for
training, 1,423 held out for testing.

---

## Part 4 — Evaluation Metrics: Before Training Exposure (the held-out test set)

This is the model being tested on transactions from the *same 35 batches* it trained on, just
transactions it personally never saw (the 25% held out). Think of this as a practice exam using
questions from the same textbook, just ones not shown to the student beforehand.

| Metric | Value | What it means in plain English |
|---|---|---|
| ROC-AUC | 0.92 | An overall "how good is this model" score from 0 (random guessing) to 1 (perfect). 0.92 is genuinely good separation. |
| Recall | 93.6% | Out of all real fraud transactions, the model correctly caught 93.6% of them. Only ~6-7 out of 100 slipped through. |
| Precision | 24.8% | Out of everything the model flagged as suspicious, only 24.8% was actually fraud. The other ~75% were false alarms. |
| F1 score | 39.2% | A single number that blends recall and precision together — low here because precision is dragging it down, even though recall alone looks good. |
| False positive rate | 23.8% | Out of all the *legitimate* transactions, about 1 in 4 got wrongly flagged. |
| Confusion matrix | TN=1000, FP=313, FN=7, TP=103 | Of 1,423 test transactions: 1,000 correctly ignored, 313 innocent ones wrongly flagged, 7 real fraud missed, 103 real fraud correctly caught. |

**In plain English:** the model is tuned to almost never miss real fraud, and accepts a lot of
false alarms as the cost of that.

---

## Part 5 — Evaluation Metrics: After Exposing the Model to the Live 216-Transaction Demo

This is the real test drive — not a random sample, but the exact scenario used in a live demo:
**200 normal transactions + 16 real fraud transactions** (3 placement deposits at 3 branches, 12
in-between "layering" hops moving the money through disguise accounts, 1 final withdrawal).

| Metric | Value | What it means in plain English |
|---|---|---|
| ROC-AUC | 0.96 | Even cleaner separation on this specific scenario than the practice exam. |
| Recall | 100% | Every single one of the 16 fraud transactions was caught — including all 12 of the sneaky in-between hops. Nothing got away. |
| Precision | 20.8% | Out of everything flagged in this demo, only about 1 in 5 flagged transactions was real fraud. |
| F1 score | 34.4% | Again dragged down by precision, despite perfect recall. |
| False positive rate | 30.5% | About 1 in 3 of the 200 normal transactions got wrongly flagged. |
| Confusion matrix | TN=139, FP=61, FN=0, TP=16 | Of 216 transactions: 139 correctly ignored, 61 innocent ones wrongly flagged, 0 real fraud missed, all 16 real fraud caught. |

**In plain English:** on the exact demo, the model has a perfect record catching every criminal
transaction — but for every 1 real fraud transaction it correctly flags, it also wrongly flags
roughly 4 innocent ones.

*(Note: a live run through the actual Docker branch containers may show a slightly different flag
count than this offline number — e.g. ~66 flagged instead of ~77 — because the live system tracks
some internal state per-branch rather than as one combined pass. The overall pattern, near-100%
recall with low precision, holds either way.)*

---

## Part 6 — Why Precision Can't Simply Be "Fixed" by More Training

Before deciding what to do next, a deeper check was done: **can the same 5 features ever get both
recall and precision above 70-75%, with better training or a fancier model?**

Every decision threshold from 0.01 to 0.99 was tested, and a far more powerful model (a Random
Forest, instead of the simple straight-line Logistic Regression) was tried on the exact same
features. Neither ever got both recall and precision above 75% at the same time. The best
available trade-offs were roughly: ~85% recall paired with ~37% precision, or ~55-60% recall
paired with ~40% precision.

**Why:** a real $1,500 laundering transfer (one hop in a chain) and an ordinary person sending
$1,500 to a friend produce *the exact same 5 numbers* — same rough amount, same transaction type,
similar time of day. To the model, they are indistinguishable. This isn't a training mistake; it's
a missing-information problem. No amount of retuning a model that only ever looks at one
transaction at a time can fix it.

*(A related data-quality issue was also found and fixed along the way: the way training data was
being split into "train" and "test" portions was done per individual transaction row rather than
per whole fraud scenario. With only 25 fraud scenarios, this let a flexible model partly "cheat" by
recognizing which scenario a transaction belonged to, instead of learning a real pattern — inflating
test results. Any future retraining needs to split by whole scenario, not by row, to get an honest
number.)*

---

## Part 7 — The Current Decision: Adding a Graph Layer to Fix Precision

**The problem, restated simply:** a single heavy cash deposit or a single mid-size transfer can
never be judged "fraud" with confidence on its own — plenty of ordinary banking looks the same.
What actually makes something suspicious is the **chain**: money from 3 separate near-$10,000
deposits, at 3 different branches, all eventually landing in the same account. That's information
which only shows up when you connect multiple transactions together — which a model looking at one
transaction at a time can never see.

**The decision:** the next piece of work (Session 4) adds a shared graph database (Neo4j) that all
3 branches write their anonymized transactions into as connections between accounts. A "does this
money trail converge with other suspicious deposits?" check can then be run on top of it.

**The plan, in order:**
1. Build the graph and the convergence check first, on its own, proven to correctly trace the
   money trail regardless of how many hops long the laundering chain is.
2. **Before making any changes to the model**, try using the graph as a simple filter: take the
   transactions the current model already flagged, and keep only the ones that are actually part
   of a real converging chain, dropping the rest. This alone might clear out most of the ~50-60
   false alarms in the live demo, without retraining anything.
3. Before trusting that result: the current fake "normal" traffic has zero situations where money
   from several unrelated people innocently ends up in one account (like paying rent to the same
   landlord). Without that, the filter would look perfect for the wrong reason — nothing hard
   exists yet to test it against. A couple of realistic look-alike scenarios need to be added to
   the fake background traffic first.
4. Only after that, consider teaching the model itself about convergence by feeding it new
   engineered numbers derived from the graph (e.g. "how many other suspicious deposits does this
   transaction's money trail connect to?"). If this step happens, it has to be done carefully — the
   same kind of "the model secretly knew the answer key" mistake caught earlier in Session 3 is
   possible here too if not handled correctly.

**Bottom line:** the model isn't broken, and it isn't going to be fixed by tuning it harder. It
needs to see more than one transaction at a time — and that's exactly what the graph is for.
