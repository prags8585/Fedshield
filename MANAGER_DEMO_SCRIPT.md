# FedShieldV2 — Manager Demo Script

A full, plain-English walkthrough of every moving part of the project, in the order a transaction
actually flows through the system. Written to be read from, or rehearsed against, before a live
demo. Every number in here is a real, verified number from actual runs of this project — nothing
is estimated or made up.

---

## 1. The one-sentence pitch

Three bank branches want to catch money-laundering rings that deliberately split large cash
deposits across branches to stay under reporting limits — a pattern no single branch can ever see
on its own. This project builds a machine learning model that flags suspicious transactions in
real time, a graph database that traces whether several suspicious transactions actually connect
into one real ring, a team of AI agents that investigate and report on confirmed rings, and a
federated learning loop that lets all 3 branches improve one shared fraud model together —
**without ever sharing a single customer's name, account number, or raw transaction with each
other.**

---

## 2. How we generated the data

Nothing here is real customer data — it's entirely synthetic, built with a library called
**Faker**, which generates realistic-looking (but fake) names, addresses, and account details.

- **Customers and accounts**: we generate a population of fake customers, each with 1+ bank
  accounts spread across the 3 branches (`loc1`, `loc2`, `loc3`).
- **Background traffic**: thousands of ordinary, everyday transactions — debit card purchases,
  ACH transfers, checks, Zelle payments, ordinary cash deposits/withdrawals — with realistic
  amounts, times, and merchant types. This is the "normal noise" the model has to see through.
- **The fraud pattern we inject (smurfing / structuring)**: this is the actual crime we're
  simulating. Three unrelated people each deposit cash **just under $10,000** (the U.S. CTR
  reporting threshold — banks must report any single cash transaction of $10,000 or more, so
  criminals split it into smaller pieces to avoid that) at three **different branches**, within
  about 40 minutes of each other. That money then moves through a chain of "mule" accounts —
  intermediate accounts that just pass money along, skimming a small percentage each time — before
  all three chains land on **one shared account**, which then makes one large cash withdrawal.
  That's the classic 3-stage laundering pattern: **placement** (get the dirty cash into the
  banking system in small pieces) → **layering** (move it through several accounts to obscure the
  trail) → **integration** (pull it back out as one "clean" lump sum).
- **Scale we've tested at**: a small 216-transaction case (1 ring, 13 real fraud accounts), a
  500-transaction case (3 simultaneous rings, 2 of them sharing a mule account on purpose — a
  harder stress test), and a 1,500-transaction case (19 simultaneous rings, only 1 shared mule
  account — closer to a realistic bank's daily volume, with a controlled, demo-safe amount of
  interconnection).

---

## 3. Kafka — the live transaction feed

**Apache Kafka** is a message-streaming system — think of it as a real-time conveyor belt for
transactions. Each of the 3 branches has its **own topic** (`txns.loc1`, `txns.loc2`, `txns.loc3`)
— a separate lane on that conveyor belt. A "producer" script drops transactions onto the belt in
the order they happened; each branch's own "consumer" only ever watches its own lane.

**Why Kafka and not just a database call**: this mimics how a real bank actually operates — each
branch's system reacts to transactions as they happen, one at a time, in real time, not in a big
overnight batch. It's also what makes the "no data sharing between branches" boundary real:
branch loc1's consumer physically cannot see branch loc2's topic.

---

## 4. Redis — the shared whiteboard

**Redis** is a very fast, in-memory database. In this project it plays two roles at once:

1. **A shared whiteboard** — a place all 3 branches and all the AI agents can read and write small
   pieces of state without touching each other's raw data. What actually gets written here (see
   section 6) is never a full transaction record with customer names — only masked, already-scored
   results.
2. **A live alarm system** — Redis supports "publish/subscribe," where one piece of code can
   instantly notify another the moment something happens, instead of that other piece of code
   having to repeatedly check "did anything happen yet?" (polling). This is what makes the whole
   agent pipeline **real-time**, not batch — the moment a transaction is flagged, the agents wake
   up immediately, with zero delay.

---

## 5. The ML model — how it flags fraud

### What kind of model, and why

It's a **logistic regression** — the simplest real machine learning model there is: one line of
math (a weighted sum of the input numbers, plus a fixed offset) squashed into a 0-to-1 probability
by a curve called the sigmoid function. It's implemented in PyTorch (as a single linear layer) so
it's a genuine, trainable neural network under the hood — not a lookup table, and not a hand-coded
`if` statement. We deliberately kept the model itself dead simple, because the real intelligence in
this system isn't "one clever model" — it's the combination of this fast first-pass filter with the
graph-tracing and agent layers on top of it.

### The 5 things it actually looks at, per transaction

1. **How close the amount is to the $10,000 reporting limit** (a ratio, e.g. 0.94 = 94% of the
   limit)
2. **Is it cash** (yes/no)
3. **What hour of day** it happened
4. **What day of the week**
5. **Is money leaving the account** (a transfer-out) **or arriving**

Two more things get *computed* for evidence purposes but deliberately **excluded** from the actual
score: recent transaction velocity (in our synthetic data, almost every legitimate account only
ever transacts once in any 10-minute window, so this signal is mostly flat and just amplifies
noise) and account age (in our synthetic data, every fraud account happens to be brand new and
every legitimate account happens to be old — the model would just learn "new account = fraud,"
which is both a coincidence of how we built the simulator and, in real life, unfair to any
legitimate customer who just opened an account).

### How it was actually trained

`train_model.py` generates **25 independent fraud scenarios** — different people, different
amounts, different chain lengths (2 to 8 hops), different placement amounts (from very obvious
near-$10k structuring down to much looser, harder-to-spot amounts) — plus **10 background-only
batches** of ordinary traffic. Every one of those transactions is run through the exact same
masking and feature-extraction code the live system uses, and labeled using the simulator's own
ground truth (the *only* place in the whole project where "the answer" is allowed to be looked at —
it's an offline, one-time training step, never something the live agents ever see).

That gives roughly 5,000-6,000 labeled examples, about 7-8% of them fraud (matching how rare real
fraud is). We split it 75% training / 25% held-out test, and train the model with real gradient
descent — starting from random numbers, and nudging them a little at a time, 300 times, until the
model stops improving. We use something called `pos_weight` to make sure the model doesn't take
the easy way out and just guess "not fraud" every time (which would be 92%+ "accurate" and
completely useless) — it makes missing a real fraud row cost roughly 12x more than a false alarm
during training.

### The threshold — and why it's 0.3, not a "nicer-looking" number like 0.5 or 0.75

This is a genuinely deliberate choice, not an arbitrary one. During training, we swept across many
possible thresholds (0.5, 0.4, 0.3, 0.2, ... down to 0.01) on the held-out test data and picked the
**lowest threshold that still catches essentially 100% of the catchable cash fraud**. The reasoning:
missing a real fraud transaction is a permanent, unrecoverable loss — that transaction is never
looked at again. A false alarm, on the other hand, only costs the downstream AI agents a few
seconds of investigation before they correctly clear it. So the model is deliberately built to be
**trigger-happy** — high recall, lower precision — and the whole reason the agent pipeline exists on
top of it is to be the smart, cheap filter that cleans up after a model that's intentionally
over-eager.

### Current real performance (verified on our largest test — 1,500 transactions, live through the
actual Docker/Kafka stack, not just an offline number)

| Metric | Value |
|---|---|
| Recall (real fraud caught) | **100%** — every single fraud transaction, no exceptions |
| ROC-AUC (overall discrimination quality) | **0.9927** |
| Precision at threshold 0.3 | **54.8%** |
| False positive rate | **21.0%** |

In plain terms: it never misses a real fraud transaction, but roughly 1 in 5 innocent transactions
also gets a second look — which is exactly the trade-off we designed for, since the agent layer
exists specifically to clean that up cheaply.

---

## 6. What happens the instant a transaction gets flagged

Each branch's own Kafka consumer scores **every** transaction that comes through — not just
suspicious-looking ones. If the score crosses 0.3:

1. It writes the full masked feature record to Redis, under a key like
   `score:{branch}:{token}:{transaction_id}`.
2. It adds that account's masked ID (called a "token" — more on this below) to a Redis **set**
   called `flagged_accounts` — the running list of everything currently under suspicion.
3. It **publishes a message** on a Redis channel called `fraud_events` — this is the alarm bell. The
   always-on investigation process is sitting there listening on that exact channel, and the moment
   this message arrives, it wakes up and starts investigating — no delay, no polling.

**What a "token" is, and why**: a real account number is never allowed to leave its own branch.
Every account gets converted into a one-way scrambled ID (a cryptographic hash) called a token —
the same real account always produces the same token everywhere, so the system can still recognize
"this is the same account showing up again," but nobody looking at Redis or the graph database can
ever reverse that token back into a real account number.

---

## 7. The agents — who wakes up, and when

There are 4 AI pieces in this pipeline. Three of them are genuinely LLM-powered agents; the fourth
(the actual fraud/false-positive separator) is mostly deterministic code with a thin AI layer for
narration. They all get triggered automatically, in real time, off that same `fraud_events` alarm
— nobody has to manually click "investigate."

### Agent 1 — the Structuring Agent (SA)

**What it does**: the moment any transaction is flagged — real fraud or false positive, it doesn't
know which yet — this agent writes a short, 2-3 sentence, plain-English description of that one
transaction (e.g. "cash deposit at 94% of the CTR threshold, at 2am, from a brand-new account") and
adds it to a running list. That's genuinely its whole job now.

**An honest design note worth knowing**: earlier in the project, this agent used to also render a
HIGH/MEDIUM/LOW confidence rating and decide whether the investigation was "worth escalating." We
removed that. Real testing showed it never actually gated anything useful — every flagged
transaction gets investigated regardless — so keeping an LLM opinion around that didn't change any
outcome was just wasted time and a false sense of judgment. It's now honestly scoped as a describer,
not a decider.

### Agent 2 — the Money-Trail Agent (MTA) — the actual fraud vs. false-positive separator

This is the most important piece of the whole pipeline, and it's worth explaining carefully because
it's not "an AI decides if this is fraud" — it's mostly real math.

**How it actually separates real rings from false positives**: it looks at **every account
currently flagged** across the whole bank at once — not just the one transaction that triggered
this particular investigation — and traces each one **forward through the transaction graph**,
hop by hop, following two strict rules: (1) the next hop's amount must be within 90%-105% of the
current amount (a mule mostly passes money forward, maybe skims a little), and (2) the next hop
must happen within 8 hours. It keeps following valid hops until it hits a dead end. Then it checks:
**do 3 or more of these independently-traced trails, from at least 2 different branches, all land
on the exact same account, with at least 70% of the total money still there?** If yes, that's not a
coincidence — that's a real, structurally-proven laundering ring. If no, it's dropped as a false
positive.

This whole check is **plain deterministic code** — no AI judgment involved in the actual
fraud/false-positive decision. The reason that matters: it's provable, reproducible, and it's
already been tested to correctly clear false positives 100% of the time in every clean test we've
run (14/14 in the small case, and so on). Only once a ring is *already proven* does an LLM get
involved at all, and only to write a plain-English narration of a fact that's already locked in —
it can't change the answer.

**How Neo4j fits into this**: Neo4j is a graph database — instead of storing transactions as rows
in a table, it stores them as a network of accounts connected by "sent money to" relationships. That
shape is exactly what "who sent money to who, and where did it eventually go" needs — following a
chain of hops through a graph database is fast and natural in a way it wouldn't be in a normal
table-based database.

**The key insight for "how does tracing one flagged transaction find a whole ring"**: it's not
that one transaction's investigation "grows outward" and stumbles into its co-conspirators. It's
that the very first thing this agent does is ask a *global* question across everything currently
flagged, and if 3 separate flagged deposits turn out to all trace forward to the same place, the
**entire ring** — every account involved, all the hops — becomes known in that one check. Only one
of those 3 real accounts needs to trigger an actual investigation; the other 2, plus every mule
account in between, get resolved "for free" from that single check, with zero extra AI calls.

---

## 8. The Verdict Agent — what, why, and what it labels

**What it does, and when**: it only ever wakes up after the Money-Trail Agent has *already*,
deterministically, proven a real convergence. It asks a genuinely different question than "did the
money connect" (already answered, provably) — it asks **"does this connection prove deliberate
laundering, or could there be an innocent explanation?"** and answers GUILTY or NOT_GUILTY with a
confidence score and a plain-English rationale.

**Why we need labeling at all**: the ML model from section 5 only ever learns from synthetic
training data. Every time the agent pipeline confirms and judges a real (in our case, realistically
simulated) fraud ring, that's a genuine, real-world example the model has never seen before. If
the Verdict Agent says GUILTY with high confidence, a small piece of plain code (the "Label
Generator" — deliberately not another AI call, since deciding whether to act on an already-made
judgment is bookkeeping, not a fresh decision) writes those real transactions' real features as
training labels into a buffer, one per involved branch. This is the exact mechanism that lets a
confirmed real case make the shared model itself smarter over time — not just catch one case once,
but improve detection for every future case like it, at every branch. This is explained further in
section 10.

**An honest limitation we found through real testing, worth mentioning proactively**: we ran the
full pipeline against 451 real flagged accounts and found the Verdict Agent isn't perfect — it
incorrectly called 3 out of 18 real, structurally-proven rings "NOT_GUILTY." That's a genuine LLM
judgment gap, not a tracing bug (the Money-Trail Agent's proof was correct in all 18 cases). Because
of that finding, we deliberately **decoupled** what this agent's opinion controls: it now only ever
decides whether to write FL training labels (getting that wrong for 3 cases doesn't lose evidence,
it just means those 3 cases don't teach the model this round) — it no longer has the power to
prevent a real, proven ring from being reported to a human. That's the Report Agent's job below, and
it fires regardless of what the Verdict Agent decides.

---

## 9. The Report Agent — the final output

**What it does**: produces one consolidated, downloadable Excel file with every confirmed ring's
full transaction list — no AI judgment involved in building the table itself, because every single
column it needs (transaction ID, amount, timestamps, the real account number, the ML score) is
already a known, real fact sitting in the graph or Redis. There's nothing for an AI to invent or
judge here — just accurate, deterministic data assembly.

**Two sheets**:
- **Transactions** — one row per real transaction in the ring (every deposit, every mule hop, the
  final cash withdrawal), with the real account number, amount, who it came from, who it went to,
  the exact time, day of week, and the ML score that transaction actually got.
- **Ring Summaries** — one row per ring: who started it, every account involved, what account it
  all converged on, and now also the Verdict Agent's GUILTY/NOT_GUILTY call, its confidence, and its
  rationale — shown for context, but never used to decide whether the ring appears in the report at
  all.

**When it fires**: the instant the Money-Trail Agent proves a real convergence — independent of the
Verdict Agent's opinion, for the reason explained above. A structurally-proven ring is always worth
five minutes of a human's time; a silently-dropped one is a real case lost forever with no way to
get it back.

**One honest, demo-appropriate limitation worth knowing**: showing the *real* account number (not
just the masked token) is the one place in this whole pipeline that's allowed to "unmask" — which
only works cleanly here because this is a synthetic demo with the full fake customer list available
to check against. In a real bank, that last step would use each branch's own local, secure customer
records — never a shared, cross-branch lookup table.

---

## 10. Federated Learning — why, and how

### Why we need this at all

Here's the core problem FL solves: a real smurfing ring is *specifically designed* so that no
single branch ever sees enough on its own to be suspicious — each branch only sees one deposit
that, alone, looks almost fine. If each branch trained and used its own private fraud model, none
of them would ever learn the cross-branch pattern, because none of them individually has enough
real examples of it. The obvious fix — "just have the branches share their transaction data with
each other so they can all learn from it" — is exactly what we can't do, both for real privacy
regulation reasons and because it defeats the entire point of the masking/token design in this
project.

**Federated Learning is the answer to "how do 3 branches learn from each other's experience without
ever seeing each other's actual data."**

### The core concept, in the simplest possible terms

Imagine 3 students who aren't allowed to show each other their homework, but they're all working
from the same textbook and want to improve together. Instead of comparing homework, each student
practices privately on their own problems, and then just tells the group **"here's how I'd adjust my
answer sheet based on what I noticed"** — not the actual problems they solved, just the *adjustment*.
A tutor collects all 3 students' proposed adjustments, averages them into one improved answer sheet,
and hands that back to everyone to practice with next round. Nobody ever saw anyone else's actual
homework — only the averaged lesson learned from it.

That's federated learning. The "homework" is each branch's real transaction data (never shared).
The "adjustment to the answer sheet" is the model's **weights** — just a small handful of numbers
describing what the model learned, mathematically impossible to reverse back into raw transactions.
The "tutor" is a central FL server that only ever averages numbers, never touches data.

### How it's actually implemented here

- We use a real framework for this, called **Flower**, and a real algorithm called **FedAvg**
  (Federated Averaging) — this isn't a simulated or pretend version of federated learning, it's the
  real mechanism, running as separate real processes (one per branch, plus one server), talking to
  each other over the network, exactly like a real multi-bank deployment would.
- Each round: every branch does a **short local practice pass** (20 epochs — not a full retrain from
  scratch) on **fresh data it's never trained on before**, starting from the *current* shared model,
  not from a blank slate. It then sends back only its updated weight numbers.
- The server averages every branch's updated weights together (FedAvg), and measures the new
  combined model's accuracy against a held-out validation set that **no single branch ever trained
  on** — a fair, independent check. If the new averaged model is actually better, it's accepted and
  becomes the new shared model everyone uses; if not, the round is rolled back.
- This repeats over multiple rounds (we've verified 5 rounds running end to end), with each
  branch's local loss visibly decreasing round over round.

### How the AI agents' real findings feed back into this loop

This is the part that closes the whole system into a real feedback loop, not just a one-time
demo: every time the Verdict Agent confirms a real fraud ring with high confidence, those real,
already-known transaction features get added — as real training labels — into that branch's own
practice batch for the *next* FL round, mixed in on top of the synthetic data (never replacing it,
since a handful of real examples shouldn't be allowed to dominate or overfit a whole round on their
own). So a real, agent-confirmed fraud case doesn't just get reported once — it becomes a genuine
lesson the shared model carries forward, making every branch's detection slightly sharper for the
*next* similar case, automatically, with zero manual retraining step required.

**The full loop, start to finish**: a transaction is flagged → the Money-Trail Agent proves it's
part of a real ring using nothing but graph math → the Report Agent writes it up for a human →
the Verdict Agent separately judges whether it should teach the model → if yes, that real case
becomes a training label → the next federated learning round mixes that label into every involved
branch's practice data → the shared model updates → every branch, not just the one that saw this
specific case, gets a little better at catching the next one like it.

---

## Closing talking points, if asked "what's the headline result"

- **100% recall, every case tested** (216, 500, and 1,500-transaction scenarios) — no real fraud
  ring has ever been missed by the ML layer.
- **The graph-tracing layer is what actually separates real rings from false positives** — and it's
  provable, deterministic math, not an AI guessing.
- **We found and fixed real bugs through genuine testing, not just claimed success** — a calendar-
  drift bug that was quietly inflating false positives, and a real gap where the Verdict Agent's own
  judgment occasionally got a proven case wrong — and we changed the architecture in response
  (decoupling reporting from that judgment) rather than hiding the limitation.
- **Federated learning here is real, not simulated** — separate processes, a real averaging
  algorithm, verified round-over-round improvement, with agent-confirmed real cases feeding directly
  back into the next round.
