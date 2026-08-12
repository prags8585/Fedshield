# FedShieldV2 — Session 3 Summary (Plain-English + Technical Detail)

This document explains what Session 3 built, why it was built that way, and exactly how it
works — including the ML model. It assumes no prior context beyond "this is a fraud-detection
demo for a bank with 3 branches."

---

## Part 1 — The Numbers, Corrected

Before anything else, here's the precise data picture, because it's easy to blur these together:

| What | How many | Built by | Contains fraud? |
|---|---|---|---|
| Customers / accounts | **300** (100 per branch × 3 branches) | `customers.py` | No — this is just the "population" everyone else draws from |
| Background transactions | **200** | `data_generator.py` | No — pure normal noise (Zelle, debit card, ACH, etc.) |
| Fraud scenario transactions | **16** | `layering_scenario.py` | Yes — 100% of this file is the planted crime |
| **Total streamed in a demo run** | **216** | `producer.py` merges the two files above | 16 out of 216 are fraud (~7%) |

The 16 fraud transactions break down as:
- **3** placement deposits (the crime's entry point — one per branch)
- **12** layering hops (4 hops × 3 people, moving the money through disguise accounts)
- **1** integration withdrawal (the crime's exit point — cashing out)

These 16 live in their own file, generated independently of the 200 background transactions.
They are only combined into one single stream at the moment `producer.py` runs — Kafka and the
branches never see "a background file" and "a fraud file"; they just see one continuous feed of
216 transactions, indistinguishable in format from each other.

---

## Part 2 — What Session 3 Actually Built (and Why)

Sessions 1 and 2 built the "world" (the plumbing — Kafka, Redis, Neo4j — and the fake data).
Session 3's job was: **make that world actually move.** Four pieces were built:

### 1. `producer.py` — the transaction feed

**What it does:** reads the event files, sorts them by their (fake) timestamp, and sends each
one to the correct branch's Kafka "topic" (think of a topic as a dedicated mail slot — one for
loc1, one for loc2, one for loc3).

**Why it exists:** something has to actually put transactions "on the wire" for the branches to
react to. Without it, the fake data would just sit in a JSON file forever, and there'd be nothing
to watch happen live.

**A specific trick worth knowing:** the fraud scenario is written as if it unfolds over **6
fabricated hours** (a deposit, then hops spread across hours, then a withdrawal). Nobody wants
to watch a demo for 6 hours. So the scenario generator secretly tags every fraud event with "how
many seconds into a 60-second window should this actually be delivered" — compressing 6
fabricated hours into 60 real seconds. The background transactions get a similar (simpler)
compression. The result: you watch the *entire* story — placement, layering, integration — play
out in under two minutes, without the time-based logic (like "did these 3 deposits happen within
40 minutes of each other?") being misrepresented.

### 2. `masking.py` — the anonymizer

**What it does:** for every transaction, it strips out the person's name and their real account
number, and replaces the account with an anonymous code called a `token_id`. The same real
account always turns into the same token, every time, everywhere — but you can't reverse a token
back into the original account number.

**Why it exists:** this is the privacy promise of the whole project. Nothing downstream (the
scoring model, Redis, the eventual shared graph) is ever allowed to see a real name or account
number — only these anonymous tokens. `masking.py` is the one narrow gate all of that has to
pass through first.

**How the anonymization works, technically:** `token_id = SHA256(account_number + a_secret_salt)`,
cut down to 16 characters. SHA256 is a one-way scrambling function — easy to compute forward,
practically impossible to reverse. The "secret salt" is a fixed piece of text mixed in so that
even if someone guessed account numbers, they couldn't precompute all possible tokens in
advance. Using the *same* salt everywhere (rather than a different salt per branch) is a
deliberate choice — it's what guarantees the same real account always becomes the same token
regardless of which branch is processing it, which matters a lot later when we need to trace
one account's money across multiple branches.

### 3. `model.py` — the suspicion-scoring brain

Covered in full detail in Part 3 below — this is the piece most worth slowing down on.

### 4. `consumer.py` — the branch's "front desk"

**What it does:** this is the process running inside each branch's container. It's the one
piece of code doing real work continuously: it listens to its branch's Kafka topic, and for
every transaction that arrives, it calls `masking.py` to anonymize it, builds a small set of
descriptive numbers about it ("features" — see Part 3), hands those to `model.py` for a
suspicion score, and writes the result to Redis (a shared whiteboard all branches can post to,
but which never contains any real names or account numbers).

**Why it exists:** this is the actual "wiring together" step — Sessions 1/2 built each piece in
isolation; `consumer.py` is what makes masking → feature-building → scoring → recording happen
as one continuous, automatic pipeline, once per transaction, with no human involved.

---

## Part 3 — The ML Model: What, Why, and How

This is the part most worth being precise about, because it's also the part most likely to be
misunderstood if glossed over.

### What kind of model is it?

A small neural network (technically: a 3-layer "multilayer perceptron" — don't worry about the
name, just think of it as a small decision-making machine) that takes in **7 numbers** describing
one transaction, and outputs **1 number between 0 and 1** — a suspicion score. Higher = more
suspicious.

### The 7 numbers it looks at (per transaction)

1. **How close is the amount to $10,000?** (the US regulatory cash-reporting threshold)
2. **Is this cash?** (yes/no)
3. **What hour of the day?** (0–23)
4. **What day of the week?**
5. **How many transactions has this same (anonymized) account made in the last 10 minutes?**
6. **How old is this account?** (in days — a brand-new account is a bigger red flag than a
   10-year-old one)
7. **Is money leaving this account, or arriving?**

Nothing else. No names, no exact account numbers, no location, no device info — deliberately, to
keep the privacy promise from Part 2 intact even inside the model.

### How is it trained? (the important, honest part)

**It is not trained on any real or generated transaction — not the 200 background ones, not the
16 fraud ones. It has never seen a single transaction from our own simulated data before making
its first prediction.**

Instead, at the moment each branch's container starts up, the code invents **2,000 made-up
example transactions** using a simple rule a human wrote:

> "If it's cash, the amount is close to $10,000, it happened late at night (roughly midnight to
> 4am), the account has been unusually active in the last few minutes, and the account is less
> than a month old → label this one **fraud**. Otherwise → label it **legit**."

The model then studies these 2,000 invented examples for 30 rounds ("epochs"), adjusting itself
until it can reliably tell the difference between the two labels. This process is called
**bootstrap training** — it's a stand-in for real training data, used purely so the model isn't
just guessing randomly (~0.5 for everything) the moment it starts.

**Why this matters for how you should read the demo:** the fraud scenario we inject
(`layering_scenario.py`) was *also* built to look like "cash, near $10,000, late night, brand-new
account." So when the model correctly flags the 3 deposits and 1 withdrawal, that's not the model
discovering a hidden fraud pattern through cleverness — it's closer to the model correctly
reciting a rule it was handed, applied to a scenario built to match that same rule. That's a
completely fine thing for a Session 3 checkpoint to be — the goal here was "prove the pipeline
works end to end" — but it would be a mistake to describe it in a demo as "the AI learned to spot
fraud." Real learning-from-data starts in later sessions (see Part 5).

### How does it actually "catch" a suspect, moment to moment?

For every single transaction, the instant it arrives at a branch:
1. The 7 numbers above get calculated.
2. They're fed through the already-trained model (one quick calculation, not "training on the
   fly" — training only happens once, at startup).
3. Out comes a score, e.g. `0.92`.
4. If that score is **0.75 or higher**, it's flagged: added to a shared "flagged accounts" list
   and announced on a shared alert channel — all instantly, in the same step.

There's no delay, no batch job, no waiting for a human. The moment a transaction is scored, the
decision is made.

### Why did it only catch 4 of the 16 fraud transactions?

The 3 deposits and the 1 final withdrawal are all **cash** transactions — feature #2 above fires
strongly for them. The 12 "layering" hops in between are **wire/ACH transfers between accounts**,
not cash — so feature #2 doesn't fire, and the model, which leans heavily on "is this cash," barely
reacts to them (scores land around 0.3–0.4, under the 0.75 flag line).

This is not a flaw to be fixed at this layer — it's a faithful reproduction of why "layering" works
as a real money-laundering technique in the first place: each individual transfer in the middle of
the chain is deliberately designed to look boring on its own. Catching those requires **connecting**
several transactions across time and accounts — which no single-transaction model, however well
trained, can do by looking at one transaction in isolation. That connecting job belongs to Session
4 (a shared graph that traces "does money from these 3 separately-flagged deposits eventually
converge on the same account?").

---

## Part 4 — How Do We Know It's Not Biased? (the honest limitations)

Worth stating plainly, because it's the right question to keep asking at every stage:

- **The training rule is circular.** The model was handed almost the same rule the fraud scenario
  was built from. Catching it proves the wiring works, not that the model generalizes to fraud
  patterns it wasn't told about in advance.
- **The rule itself carries real fairness risk.** "Heavy cash use" and "new account" are used as
  fraud signals — but plenty of legitimate people are cash-reliant (unbanked/underbanked
  customers, certain immigrant communities, cash-based small businesses) or simply just opened an
  account. A model leaning on these signals could unfairly flag ordinary people in those groups
  more often. Nobody has measured this yet — it's an open risk, not a solved problem.
- **It has never touched real-world data.** Zero evidence yet that it holds up outside this exact
  synthetic scenario.

### What actually builds trust — and when it happens (per the project's own plan)

| Session | What it adds toward trustworthiness |
|---|---|
| 5 | Federated learning — branches genuinely improve the model from real transaction patterns across branches, still without ever sharing raw customer data |
| 7 | An adversarial AI review (a "Prosecutor," a "Defense," and a "Judge" agent) has to agree a case is real *before* it's trusted enough to become a new training example — specifically designed to catch overconfident or wrong calls |
| 9 | Formal measurement — accuracy (AUC), false-positive rate, and side-by-side "with vs. without each safeguard" comparisons, so any claim about how good the system is has numbers behind it, not just a demo that happened to work |

**The honest one-line summary of Session 3's model: it's scaffolding that proves the pipeline
works, not yet a trustworthy fraud detector.** That's a completely appropriate place to be at
this point in the build — just don't present it as more than that in a demo.

---

## Part 5 — Quick Glossary

- **Token / token_id** — an anonymous stand-in code for a real account number, so nothing
  downstream ever sees the real number.
- **Feature** — one descriptive number about a transaction (e.g. "is this cash?") fed to the model.
- **Threshold** — the cutoff score (0.75) above which a transaction gets flagged as suspicious.
- **Bootstrap training** — training a model on invented example data (not real transactions) just
  to get it started with non-random behavior.
- **Placement / layering / integration** — the 3 classic stages of money laundering: getting dirty
  cash into the system (placement), moving it through several accounts to hide its origin
  (layering), and pulling it back out as usable, "clean-looking" money (integration).
- **Real-time** — here, it just means "processed the instant it arrives," not "processed after a
  delay or in a batch."
