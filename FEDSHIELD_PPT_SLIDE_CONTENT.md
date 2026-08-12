# FedShield — Presentation Slide Content

Slides 1–5: high-level, non-technical, plain language.
Slides 6–13: full technical depth.

---

## Slide 1 — What is Smurfing?

- **Smurfing** = breaking up a large sum of money into smaller deposits to avoid detection.
- US banks must report any single cash transaction over **$10,000** (a Currency Transaction Report).
- Instead of depositing $30,000 in one go, a launderer deposits $9,000 + $9,500 + $9,800 —
  each one just under the reporting line.
- **The trick:** spread those smaller deposits across *different branches* or *different days*, so
  no single deposit — and no single branch — ever looks suspicious on its own.
- This is the first stage of money laundering, known as **"placement."**

## Slide 2 — What is FedShield? (Intro)

- FedShield is an AI system that catches money laundering **across multiple bank branches** —
  without any branch ever sharing customer data with another.
- Built for a bank with 3 branches, each running its own independent system.
- Four things working together:
  1. Real-time fraud scoring at each branch
  2. A shared "connect-the-dots" graph across branches
  3. AI agents that investigate automatically, like a human analyst — instantly
  4. Federated learning that keeps every branch's model improving over time
- **Goal:** go from noticing a suspicious deposit to a fully investigated, reported case — in
  seconds, with zero manual work until the final human sign-off.

## Slide 3 — Why Do We Need FedShield?

- Each bank branch only sees **its own customers and its own transactions.**
- A money launderer exploits exactly this blind spot — split the money, spread it across
  branches, and no single branch ever sees the full picture.
- Today's fix is manual: a human analyst has to notice, then cross-reference records across
  branches by hand — slow, reactive, and often happens after the money is already gone.
- Banks are also legally required to detect and report this kind of activity (AML compliance).
- **FedShield's answer:** connect the branches intelligently and automatically — without ever
  breaking data-privacy rules or sharing raw customer data.

## Slide 4 — ML & Agentic Approach

- Two layers of intelligence, working one after the other:
  1. A **Machine Learning model** at each branch scores every transaction the instant it happens.
  2. A team of **AI Agents** takes over the moment something looks suspicious — they investigate,
     trace the money, and decide, the way a human fraud analyst would, but instantly.
- The ML model's question: *"Does this look risky?"*
- The Agents' question: *"Is this actually connected to a bigger crime, and can we prove it?"*
- Together, they turn an investigation that used to take **days or weeks** into one that
  finishes in **seconds**.

## Slide 5 — Need for Federated Learning

- To get smarter, a fraud model needs to learn from more examples.
- But branches legally **cannot share customer transaction data** with each other.
- **Federated Learning** solves this: each branch trains its own model on its own data, and only
  the *learnings* (model weight updates) are shared — never the actual data.
- Result: every branch's fraud model improves from what **all three branches** have collectively
  seen, without a single customer record ever leaving its home branch.

---

## Slide 6 — Dataset Generation Design (Full Technical)

- **Synthetic data generated with Faker** (Python, US locale) — no real customer data anywhere.
- Generates realistic entities: `customer_id`, `ssn_last4`, 10-digit `account_number`, 9-digit ABA
  `routing_number` per branch.
- **Background traffic:** ordinary transactions — `ACH`, `WIRE`, `ZELLE`, `CHECK`, `DEBIT_CARD`,
  `CASH_WITHDRAWAL` — to give the model realistic noise to learn against.
- **Fraud scenario injection** (`layering_scenario.py`):
  - 3 placement accounts (p1, p2, p3), one per branch.
  - Cash deposits of **$8,000–$9,800**, just under the $10k CTR line.
  - **Configurable hop count** (2, 4, 6+ hops) for the layering chain — tests whether detection
    degrades as the trail gets longer.
  - All chains converge on **one shared consolidation account**, ending in a large cash withdrawal.
- **Timestamp compression:** a realistic multi-hour story is fabricated, but streamed into the
  system in ~60–90 seconds for a watchable live demo — all time logic reasons in *business time*,
  never wall-clock time.
- **Ground truth file:** which tokens/accounts are actually fraud — logged completely separately,
  **never exposed to any model or agent**, used only to measure accuracy afterward.
- **Extended stress-test scenarios built:** a 19-independent-ring, 1,500-transaction scenario, plus
  injected "noise" (mule-account activity, legitimate fan-in accounts) to make sure the system
  isn't just solving an artificially clean dataset.

## Slide 7 — ML Work Findings (Full Technical)

- **Model:** PyTorch **Logistic Regression** — a single linear layer + sigmoid. Deliberately simple.
- **Why not a deeper model:** tested a Random Forest on the same features — no meaningful
  accuracy gain. A linear model is simpler, faster, and more explainable when extra complexity
  doesn't buy extra accuracy.
- **Feature set — 5 features** (reduced from 7): `amount_ratio_to_threshold`, `is_cash`,
  `hour_of_day`, `day_of_week`, `is_transfer_out`.
  - Removed `account_age_days` — a data leak (every fraud account is freshly created in the
    simulator; a simulator artifact, not a real signal).
  - Removed `velocity_10min` — near-zero variance, made scores unstable.
- **Training:** an offline script builds a diverse labeled dataset (started at 25, later expanded
  to 140 fraud scenarios with varying hop-counts/amounts/timing), real train/test split.
- **Real bug found & fixed:** the original split was done *per row* (random), not *per scenario* —
  this let the model partially memorize "which scenario is this" via day/hour patterns instead of
  learning a generalizable rule. Fixed to a scenario-level (whole-batch) held-out split.
- **Performance:** ROC-AUC ~0.93–0.98 depending on scenario. At the tuned decision threshold
  (0.3): ~95–100% recall on cash deposits/withdrawals, ~93–95% recall on layering hops — but
  ~27–30% false-positive rate on ordinary legitimate traffic.
- **Key finding:** 100% recall **and** 75%+ precision is *not achievable* with these 5
  single-transaction features, no matter how the model is trained or tuned — confirmed by
  sweeping every decision threshold and testing a Random Forest. Root cause: a real layering hop
  and an ordinary transfer of similar size/timing look nearly identical on these 5 features alone.
  **This is exactly why the shared graph exists** — it adds the one piece of information a single
  transaction's features structurally cannot contain.
- **Proof it works:** running the graph's `check_convergence()` as a downstream filter on the
  model's flagged transactions cleared close to **100% of false positives**, while keeping
  essentially all real fraud — with **zero retraining**.

## Slide 8 — Agent Working: Full Workflow & Tech Stack

- **Orchestration:** LangGraph (a state-graph wiring the agent chain together).
- **LLM:** a **local Ollama server** running **`qwen2.5:7b`** — no cloud API, no per-call cost, no
  rate limits, and no customer data ever leaves the machine.
- **Trigger:** `orchestrator/listener.py` subscribes to a Redis pub/sub channel (`fraud_events`) —
  event-driven, wakes up instantly the moment something is flagged, never polls.
- **Pipeline:** Structuring Agent → Money-Trail Agent → Verdict Agent → Report Agent → Human
  Review.
- **Design principle 1 — deterministic checks never left to AI discretion.** The graph's
  `check_convergence()` always runs automatically in code, not as something the AI merely
  *chooses* to call — so the AI's judgment can never silently skip a step and cost real recall.
- **Design principle 2 — the AI never retypes facts from memory.** Real evidence (amounts,
  timestamps, transaction IDs) is always pulled directly from the database in code; the LLM's job
  is judgment and narrative only. *(A real bug was caught here: an earlier version asked the LLM
  to restate evidence it had already seen, and it fabricated numbers. Fixed by never asking it to.)*
- **Design principle 3 — full autonomy except the final human review.** Investigation → tracing →
  verdict → labeling → retraining all happen with zero human input.
- **Model selection was tested, not assumed:** compared `llama3.1:8b`, `qwen2.5:7b`, `qwen3:8b`,
  and `qwen3:14b` head-to-head; `qwen2.5:7b` won on the best balance of speed and accuracy for
  the available hardware.

## Slide 9 — Structuring Agent

- **Role:** the first, cheap filter. Asks: *"Does this look like deliberate structuring, or an
  isolated, explainable event?"*
- **Input:** the branch's fraud score + the 5 masked transaction features.
- **Output:** reasoning + a confidence level (HIGH / MEDIUM / LOW).
- One single LLM call — no tools, no back-and-forth.
- **A real design change worth highlighting:** this used to be a hard gate — a LOW-confidence
  result stopped the investigation entirely. That turned out to be the single biggest cause of
  *missed real fraud* (an AI judgment call silently dropping a genuine case). **Fixed:** it's now
  context only — every flagged transaction always proceeds to the next agent.

## Slide 10 — Money-Trail Agent (MTA)

- **Role:** the actual detective. Follows the money across all three branches using the shared
  Neo4j graph.
- **Tools available to it:** `check_convergence()`, `get_outgoing_txns()`, `get_incoming_txns()`.
- `check_convergence()` is deterministic and **always runs automatically in code** — never left to
  the LLM's choice — tracing hop-by-hop, querying Neo4j at each step.
- **Stopping conditions** (never a fixed hop count): convergence found, dead end, cycle detected,
  time window exceeded, or a safety-ceiling depth (~20 hops, a pure backstop).
- **Convergence is confirmed when:** enough independent sources converge (e.g. 3), from enough
  different branches (2+), with enough of the original value still present (~70%+ preserved).
- Evidence is always built directly from the real database — never typed out by the AI.
- **A real timing bug, found and fixed:** the moment one investigation confirms a group, the same
  confirmed evidence is now retroactively written for every other member of that group too — this
  fixed cases where a sibling deposit, investigated a few minutes too early, would otherwise have
  been stuck with a wrong "not enough evidence" answer forever.

## Slide 11 — Verdict Agent

- **Role:** a genuinely different question from the Money-Trail Agent's. Not *"did the money
  connect"* (already proven) but *"does this connection prove deliberate laundering, or could
  there be an innocent explanation?"*
- **Input:** the confirmed evidence path + the Money-Trail Agent's own reasoning.
- **Output:** verdict (GUILTY / NOT_GUILTY), a confidence score, and a plain-English rationale.
- Only ever runs when convergence was actually found — there's no verdict to render on a dead end.
- **Fails safe:** falls back to a zero-confidence NOT_GUILTY on any error — a broken call can
  never silently become a false GUILTY.
- The moment it renders GUILTY with high confidence, it **immediately and automatically** triggers
  the Label Generator — this is the exact handoff point into Federated Learning.

## Slide 12 — Report Agent

- **Role:** drafts a human-readable case file summarizing the confirmed fraud ring, and generates
  a downloadable **Excel report** of the guilty transactions.
- Every report ends with a mandatory **privacy attestation line** — confirming no customer name,
  account number, or raw transaction data was ever accessed during the investigation.
- Feeds into **Human Review** — the one manual step in the entire pipeline (approve / reject).
- **Critical design rule:** human review **never gates or blocks** the label-generation /
  FL-retraining loop — by the time a report even exists, the training label has already been
  written and used. The human step is a compliance checkpoint, not a bottleneck.
- **Status:** in progress (final integration + review workflow).

## Slide 13 — Federated Learning Work Findings (Full Technical)

- **Framework:** Flower (`flwr`) — `FedAvg` aggregation.
- **Setup:** one FL server + one FL client per branch (`loc1`/`loc2`/`loc3`); each client wraps
  that branch's own live PyTorch model.
- Each round starts from the **current live model's weights** (not a random initialization) — this
  is framed as *improving* the existing model, not training a new one from scratch.
- Local training per round is deliberately short (20 epochs) — a gentle nudge, not a full retrain,
  so branches don't diverge wildly before being averaged back together.
- Feature standardization (mean/std) stays fixed and shared by every client — only the model's
  weight/bias values get federated.
- **AUC is measured centrally**, on one held-out validation set no branch ever trains on — logged
  to Redis after every round.
- **A real bug found & fixed:** every round was training on the *identical* fixed batch of "new"
  data (a hardcoded seed) — fixed by shifting the seed range every round so each one genuinely
  trains on different transactions.
- **Results:** AUC stays consistently high (~0.99+), with real round-to-round movement after the
  fix (proof the fix is genuine). Gains stay modest for three understood reasons: the model starts
  near its ceiling already, local training is deliberately light by design, and the same 5-feature
  information ceiling from Slide 7 caps what *any* training procedure can achieve.
- **The real payoff — closing the loop:** the Verdict Agent's confirmed GUILTY verdicts become
  real training labels (via the Label Generator), automatically blended into the next FL round on
  top of the synthetic training data — turning every autonomous investigation into fresh,
  trustworthy training data, with **zero manual labeling required.**
