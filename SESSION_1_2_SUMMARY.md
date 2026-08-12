# FedShieldV2 — Session 1 & 2 Summary (Plain-English)

This document explains what we built in the first two sessions, how it works, and why each
piece exists — without needing to read the code. Think of it as the "story so far."

---

## The Big Picture

FedShieldV2 is a demo system that catches money launderers who try to sneak large sums of cash
into a bank by breaking it into smaller pieces and moving it through a chain of accounts. Banks
are legally required to report any cash transaction over **$10,000** (this is called a CTR —
Currency Transaction Report). Criminals know this, so they deposit **just under** that amount,
spread across different branches, hoping no single branch notices anything unusual.

Our system's whole point is: even if no single branch can see the full picture, a shared,
central "brain" can — by tracing the money as it hops between accounts, spotting when multiple
suspicious deposits converge on the same place, and having AI agents investigate and write up a
case automatically.

Before we could build any of the "smart" detection logic, we needed two things:
1. **A place for everything to run** (Session 1)
2. **Fake but realistic data to test it with** (Session 2)

---

## Session 1 — Building the Skeleton

### What we did
We set up all the "plumbing" the system needs to run, without yet writing any of the actual
fraud-detection logic. Think of this like building the pipes, wiring, and rooms of a house
before moving any furniture in.

We created:
- **Redis** — a shared whiteboard that different parts of the system use to leave notes for
  each other (e.g. "this account looks suspicious," "here's the investigation report").
- **Neo4j** — a graph database, which is really good at answering questions like "does money
  from these 3 different starting points eventually end up in the same place?" This is the tool
  that will let us "see" the money trail.
- **Kafka** — a message-streaming pipe. Each of the bank's 3 branches pushes its transactions
  into its own stream, like 3 separate conveyor belts feeding into the same factory.
- **3 "branch" containers** — empty placeholders for now, representing each of the bank's 3
  branches, each isolated from the others (like each branch only sees its own customers).
- **1 "FL server" container** — an empty placeholder for a later feature (federated learning —
  more on that in a future session) where branches can improve a shared fraud-detection model
  *without* ever showing each other their actual customer data.

### Why we did it this way
- Each piece runs in its own **Docker container** — basically a sealed box — so that a branch's
  raw customer data physically cannot leak into the shared system by accident. Only anonymized,
  stripped-down information is allowed to cross into Redis/Neo4j/Kafka.
- We used **industry-standard tools** (Kafka, Redis, Neo4j) instead of inventing our own,
  because they're free, well-documented, and behave the way a real bank's infrastructure would.

### How we know it worked
We ran a single command (`docker-compose up`) and confirmed:
- All 7 containers started and reported themselves "healthy"
- A test script successfully wrote/read data to Redis, ran a query against Neo4j, and sent/received
  a message through Kafka — proving the pipes are actually connected, not just "running."

### A couple of real bumps we hit
- Your original FedShield project (the older India-focused version) was already using some of
  the same "ports" (think of these as designated doors) on your computer, so we had to give the
  new project different door numbers to avoid a collision.
- Kafka refused to start the first time because of an ID formatting mistake — a one-line fix.

---

## Session 2 — Creating Fake (But Realistic) Data

### What we did
Before any AI agent can "detect fraud," we need something for it to look at. So we built 3 tools
that generate fake — but statistically realistic — banking data:

1. **`customers.py`** — creates a fake population of bank customers and their accounts (300 of
   them, 100 per branch). Each has a fake name, fake city, and a realistic-looking (but fake)
   account number and bank routing number.

2. **`data_generator.py`** — creates normal, boring, everyday transactions using those fake
   customers: Zelle payments, debit card swipes, ACH transfers, cash withdrawals, etc. This is
   the "noise" — the ordinary activity that a real fraud pattern has to hide inside.

3. **`layering_scenario.py`** — this is the actual fraud we're trying to catch, played out
   step by step:
   - **Placement:** 3 unrelated people each deposit cash *just under* $10,000, at 3 different
     branches, within about 40 minutes of each other.
   - **Layering:** each of those deposits then gets moved through a chain of other accounts
     (like laundering money through several hands to make it hard to trace) — and we can
     configure exactly how many "hops" long that chain is (2, 4, 6, or more).
   - **Integration:** no matter how long each chain is, all 3 chains eventually feed into the
     **same single account**, which then withdraws the pooled cash all at once.
   - We also keep a separate **"answer key"** file recording exactly which accounts and
     transactions were part of the fraud — this is only used later to check whether our
     detection system actually caught it. The detection system itself never gets to see this
     answer key; that would be cheating.

### Why we designed it this way
- **Why "just under $10,000"?** Because that's the real regulatory number banks in the US must
  report on. A criminal structuring deposits to dodge that exact threshold is a well-known,
  real-world fraud pattern (called "structuring" or "smurfing").
- **Why 3 different branches?** Because the entire point of this project is that no single
  branch can see enough to catch this alone — only a system that can see *across* branches can.
- **Why a configurable chain length?** Because later, we want to test whether our detection
  system still works even if the criminal makes the money trail longer and more convoluted.
- **Why fake but "shaped like real" data?** We modeled the transaction data after what an actual
  bank's live transaction feed looks like (event ID, timestamp, sender info, receiver info,
  even device/location metadata) so the system behaves like it would in a real deployment — not
  like a toy example.
- **Why keep a separate answer key, hidden from the system?** So that later, we can honestly
  measure "did our AI actually catch this, or did we just get lucky?" — this is standard
  practice in evaluating any detection system.

### How we know it worked
We ran the fraud-scenario generator with different settings and checked:
- The transactions produced are in the right chronological order
- Every transaction matches the format the rest of the system expects
- The 3 deposits are always under the $10,000 legal threshold, as designed
- Changing the "chain length" setting (2 hops vs. 4 vs. 6) correctly changes how many
  in-between accounts get created — confirming the fraud pattern truly scales up or down
  on demand, which we'll need for later testing.

---

## Where This Leaves Us

At the end of Session 2, we have:
- A fully running (but currently "empty-brained") system skeleton
- An unlimited supply of realistic fake bank customers and everyday transactions
- A repeatable, configurable fraud scenario, complete with a hidden answer key for grading later

What's still missing (and comes in later sessions): the transactions aren't actually flowing
through Kafka yet, nothing is being "masked" to strip out private customer info, no AI model is
scoring transactions for suspicion, and no graph is being built in Neo4j yet. Session 3 is where
we start wiring those pieces together — turning the static test files we made in Session 2 into
a live, streaming pipeline.
