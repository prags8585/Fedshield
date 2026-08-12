# FedShield (FedShieldV2) — Full Project Explainer

This document explains what the project is, every tool it uses and why, and exactly how data
moves through it end to end. It's written to be handed to an AI diagram generator (e.g. Google
Gemini) as the single source of truth for producing an architecture diagram — read the whole
thing before drawing anything, since later sections depend on context from earlier ones.

---

## 1. What this project is

FedShield is a capstone/research system for a single **US-based bank with three branches**
(`loc1`, `loc2`, `loc3`) that each run their own core-banking system, the way real banks end up
with siloed branch IT even without formal regulatory separation. The system's job is to catch
**money laundering that no single branch can see on its own**, using a combination of:

- **Federated learning** — branches share model *weights*, never raw transactions, so no branch
  ever sees another branch's customer data.
- **A shared, anonymized graph** — the one place the system can see across branches, using
  irreversible tokens instead of names or account numbers.
- **Autonomous AI agents** — instead of a human analyst running link-analysis queries after the
  fact, a chain of AI agents investigates a suspicious deposit the moment it's flagged, live.

The specific crime it's built to catch (called **Case 1** in project docs) is **deposit tracing**:
placement → layering → integration, the three classic stages of money laundering.

## 2. The fraud scenario it detects

Three unrelated people (call them p1, p2, p3), each with a clean history, each hold one account at
a *different* branch. Here's the laundering pattern:

1. **Placement** — each of the three deposits cash **just under $10,000** (the real US "Currency
   Transaction Report" threshold that triggers mandatory reporting) at their own branch, all
   within a short window (e.g. 40 minutes). Amounts: **$8,000–$9,800** each.
2. **Layering** — each placement account's money is moved through a chain of intermediate
   accounts (the chain length is configurable — 2 hops, 4 hops, 6+ hops — to test whether
   detection degrades as the trail gets longer).
3. **Integration** — all three chains converge on **one shared consolidation account**, which then
   makes a large cash withdrawal (the "exit").

No single branch ever sees more than its own $8–9k deposit and whatever passes through its own
accounts. **Proving these three deposits are the same crime requires connecting information that
lives in three different branches** — that's the whole reason the shared graph and the federated
model exist.

## 3. High-level architecture, in the order data actually flows

```
Synthetic data  →  Branch (Kafka → mask → score, in Docker)  →  Shared layer (Redis + Neo4j)
   →  Autonomous agent pipeline (LangGraph + local LLM)  →  Verdict + Report/Review
   →  Label Generator  →  Federated Learning (Flower)  →  back to every branch's model
```

Running in parallel, alongside the pipeline above: a **live demo dashboard** (a real web app, not
just terminal output) that lets a person watch every stage of this happen in real time, start/stop
scenarios, and download the generated report.

## 4. Complete tech stack

| Layer | Tool / Library | What it's doing here |
|---|---|---|
| Synthetic data generation | **Faker** | Generates realistic customers, accounts, and background transaction traffic (Python, `en_US` locale) |
| Fraud scenario injection | Custom Python (`simulator/layering_scenario.py`) | Builds the placement→layering→integration scenario with configurable hop count and compressed timestamps |
| Branch isolation | **Docker** / Docker Compose | Each branch (`loc1`/`loc2`/`loc3`) runs in its own container — this is the *only* isolation boundary; nothing about raw data ever crosses it |
| Streaming transport | **Apache Kafka** (`kafka-python`) | Each branch has its own topic (`txns.loc1`, `txns.loc2`, `txns.loc3`); a producer streams transactions onto it, a consumer inside the branch container reads them |
| PII masking | Python `hashlib` (SHA-256) | `token_id = SHA256(account_number + GLOBAL_SALT)` — one global salt (not per-branch), so the same real account always maps to the same token everywhere |
| Real-time fraud scoring | **PyTorch** (logistic regression, one linear layer + sigmoid) | One trained model per branch, scores every transaction the instant it's masked |
| Shared graph database | **Neo4j** | The *only* place a transaction can be linked across branches — nodes are `token_id`s, edges are transactions, written once by the sending branch |
| Shared whiteboard / event bus | **Redis** | Pub/sub trigger (`fraud_events`) that wakes the agent pipeline the instant something is flagged, plus key/value storage for scores, evidence, verdicts, labels |
| Federated learning | **Flower (`flwr`)** | `FedAvg` aggregation of the PyTorch model's weights across all three branches — only weights move, never data |
| Autonomous agent orchestration | **LangGraph** | A state-graph wiring together the Structuring, Money-Trail, and Verdict agents |
| LLM backing the agents | **Ollama**, running **`qwen2.5:7b`** locally | All three agents call this local model via the `openai` Python SDK pointed at Ollama's OpenAI-compatible endpoint — no cloud API, no cost, no rate limits |
| Report generation | LLM (Ollama) + **openpyxl** | Drafts a plain-English case file and exports a real Excel workbook of confirmed fraud rings |
| Human review | Plain Python CLI (`review_report.py`) | The one manual step in the whole pipeline — approves or rejects a drafted report; does **not** gate anything upstream |
| Live demo dashboard (backend) | **FastAPI** + Python's `pty`/`forkpty` | Serves REST + WebSocket routes; spawns real interactive shell sessions (PTYs) the dashboard streams into the browser |
| Live demo dashboard (frontend) | **React** (Vite) + **xterm.js** | Renders the tabs, live investigation feed, and terminal panes that stream real shell output over WebSockets |

## 5. Layer-by-layer detail

### 5.1 Data & scenario generation
`simulator/customers.py` and `simulator/data_generator.py` (Faker) create realistic background
traffic — customer IDs, 10-digit account numbers, 9-digit ABA routing numbers per branch, ordinary
transactions (`ACH`/`WIRE`/`ZELLE`/`CHECK`/`DEBIT_CARD`/`CASH_WITHDRAWAL`). Separately,
`simulator/layering_scenario.py` injects the actual laundering scenario described in Section 2,
with a **ground-truth file** (which tokens are really part of the fraud) that's logged for
evaluation only — nothing downstream (models, agents) ever sees it.

### 5.2 Branch-level real-time scoring (per branch, in its own Docker container)
1. `branch_node/producer.py` streams a transaction onto that branch's Kafka topic.
2. `branch_node/consumer.py` reads it, calls `branch_node/masking.py` to strip all PII down to a
   `token_id`, and passes the masked features to `branch_node/model.py` — a **PyTorch** logistic
   regression trained offline (`train_model.py`) on 5 features: `amount_ratio_to_threshold`,
   `is_cash`, `hour_of_day`, `day_of_week`, `is_transfer_out`.
3. The score is written to **Redis**. Two extra features (`account_age_days`,
   `velocity_10min`) are computed but deliberately excluded from the model itself — one was a
   simulator-only data leak, the other was a near-zero-variance feature that made scores unstable.
4. **Important, and the whole point of the shared graph:** the model alone reaches ~95%+ recall
   on real fraud, but also flags roughly 30% of ordinary legitimate transfers — a real layering
   hop and an ordinary transfer look nearly identical from inside one branch's own 5 features.
   No amount of local tuning fixes this; it needs information a single branch doesn't have.

### 5.3 The shared layer — Redis + Neo4j (the only place data crosses a branch boundary)
- Every masked transaction the *sending* branch is party to becomes one edge in **Neo4j**:
  `(:Account {token_id})-[:TRANSACTED {txn_id, amount, ts, channel}]->(:Account)`, with two fixed
  sentinel nodes, `CASH` (deposit source) and `CASH_OUT` (withdrawal sink). Only an anonymized
  token plus amount/time/channel ever appear here — never a name or account number.
- The instant a branch flags a transaction, it publishes to **Redis**'s `fraud_events` pub/sub
  channel — this is a push notification, not a polling loop, and it's what wakes the agent
  pipeline below.
- A dedicated `check_convergence()` query (in `graph/queries.py`) walks the graph hop-by-hop from
  a flagged deposit, entirely in Python (not a single Cypher query, since the hop-by-hop
  time/amount rule needs pairwise logic Cypher can't express without extensions). It stops on one
  of: **convergence found** (independently-flagged sources reach the same account),
  **dead end** (no valid next hop), **cycle**, **time window exceeded**, or a high safety-ceiling
  depth (~20 hops) as a pure backstop — never a fixed hop count as the primary rule.

### 5.4 The autonomous agent pipeline (LangGraph + local Ollama LLM)
Triggered by `orchestrator/listener.py`, which subscribes to Redis's `fraud_events` and never
polls. Three agents run in sequence, each a **LangGraph** node calling the local **`qwen2.5:7b`**
model via **Ollama**:

1. **Structuring Agent** — one quick LLM call, no tools: does this flagged transaction look like
   deliberate structuring worth a deeper look, or an isolated, explainable event? Recorded as
   context only now — it no longer blocks the next stage (an earlier version let a "no" here
   silently drop a real case).
2. **Money-Trail Agent** — the real investigator. `check_convergence()` runs automatically in
   code (never left to the LLM's discretion), and if it finds a real connection, the evidence
   (real amounts, timestamps, transaction IDs) is built directly from the Neo4j data — the LLM's
   job is judgment and a plain-English narrative only, never retyping facts (an earlier version
   let the LLM retype evidence from memory, which produced fabricated numbers).
3. **Verdict Agent** — a different question from the Money-Trail Agent's: not "did the money
   connect" (already deterministic) but "does this connection prove deliberate laundering, or
   could there be an innocent explanation." Outputs `{verdict, confidence, rationale}` — falls
   back to a zero-confidence NOT_GUILTY on any failure, so a broken call never produces a false
   GUILTY.
4. **Report Agent** *(in progress)* — drafts a human-readable case file and an Excel workbook of
   the confirmed guilty transactions, via the same local LLM plus **openpyxl**.
5. **Human Review** *(in progress)* — the one manual step in the entire pipeline
   (`review_report.py --approve`/`--reject`). Explicitly does **not** gate anything upstream — by
   the time a report exists to review, the label (see below) has already been written.

### 5.5 The feedback loop — turning a verdict into a smarter model
The moment the Verdict Agent renders GUILTY with high confidence, `agents/label_generator.py`
(plain Python, deliberately *not* another LLM call) writes a real training label —
`(features, label=fraud, source=agent_verified)` — into that branch's own retrain buffer in
**Redis** (`labels:{branch}`), for every account in the confirmed group at once.

The next **Flower** federated-learning round picks up those real labels (blended into the normal
synthetic training batch, not replacing it), each branch does a short local training pass on its
own **PyTorch** model, `FedAvg` averages the weight updates centrally, and the improved model is
redistributed back to all three branches — closing the loop. **Only a label and a weight update
ever cross a branch boundary — never a transaction.**

### 5.6 The live demo dashboard (a real web app, not just terminal logs)
A **React** (Vite) frontend talks to a **FastAPI** backend over REST and WebSockets:

- **Tab 1 — Manual.** Five interactive shell terminals (data generation, producer, three branch
  logs) the user drives by hand, streamed over a WebSocket into **xterm.js** terminal panes in the
  browser. Backed by Python's `forkpty`/PTY — these are real shells, not simulated output.
- **Tab 2 / Tab 3 — Agentic (216-case / 1500-case).** Each starts the same orchestrator listener +
  producer + branch-log flow described above, just with a different injected scenario size, and
  shows the live investigation feed (Structuring log, Money-Trail rings, Report status) polled
  from the backend. Each tab now has its **own independent set of terminals** (its own PTY
  sessions, its own WebSocket route) so running one tab never shows the other tab's terminal
  output — they only still share the underlying Redis investigation data and Neo4j graph.
- **Tab 4 — FL.** Manually triggers a real Flower round and shows its own terminals
  (`fl-server`, `fl-client-loc1/2/3`).
- A **Neo4j Browser** link-out (can't be embedded in-page due to `X-Frame-Options`) and a
  live-plots panel for FL metrics round out the dashboard.

## 6. End-to-end walkthrough (one concrete deposit, start to finish)

1. p1 walks into `loc1` and deposits $9,200 cash.
2. `producer.py` streams it onto `txns.loc1` in **Kafka**.
3. `consumer.py` (inside `loc1`'s **Docker** container) masks the account into a `token_id` via
   SHA-256, and the **PyTorch** model scores it. The score clears the model's threshold — it's
   flagged, but on its own it looks like it could be anything.
4. The edge is written into the shared **Neo4j** graph; the flag is published on **Redis**'s
   `fraud_events` channel.
5. `orchestrator/listener.py` wakes instantly (no polling) and kicks off the **LangGraph** agent
   chain, powered by the local **Ollama** `qwen2.5:7b` model.
6. The Structuring Agent gives its quick read; the Money-Trail Agent calls `check_convergence()`
   and discovers that p1's deposit, p2's deposit at `loc2`, and p3's deposit at `loc3` all
   converge — through several intermediate hops — on the same consolidation account within the
   time window, with ~96% of the value preserved. **This is the one fact no single branch could
   ever have seen on its own.**
7. The Verdict Agent renders GUILTY, 0.9+ confidence.
8. The Report Agent drafts a case file and an Excel workbook; a human reviews it later — but that
   review has zero effect on what already happened next.
9. The Label Generator writes real labels into `loc1`, `loc2`, and `loc3`'s own retrain buffers,
   the instant the verdict lands.
10. The next **Flower** FL round blends those labels into each branch's local training, `FedAvg`
    averages the resulting weight updates, and every branch's fraud-scoring model gets measurably
    better at catching this pattern next time — without a single byte of p1/p2/p3's actual
    transaction data ever leaving their own branch.

## 7. Design decisions worth preserving if you diagram this

- **One global salt, not per-branch** — this is what makes cross-branch graph linking possible at
  all; a per-branch salt (the *original*, unrelated FedShield project's design) would silently
  break it.
- **The Docker isolation boundary is narrow and deliberate** — it covers raw transactions, PII,
  and each branch's own model training data *only*. Redis, Neo4j, and the agent service are
  intentionally centralized, because cross-branch tracing is structurally impossible otherwise.
  This is not a privacy leak: only anonymized tokens and metadata ever enter the shared layer.
- **Convergence checking is deterministic code, not an LLM guess** — Neo4j does the actual proof;
  the LLM only judges intent and writes narrative.
- **Full autonomy except the report review** — detection → escalation → tracing → labeling → FL
  retraining all happen with zero human input; the only manual step is reviewing the drafted
  report afterward, and that review is explicitly decoupled from everything upstream of it.
- **Cut from scope on purpose:** differential privacy (Opacus), LSH/behavioral-similarity matching,
  and River-style pure-online learning were all explicitly left out to keep the build focused on
  the two research contributions (agent-verified labels feeding FL, and — deferred —
  adversarial multi-agent verification).
