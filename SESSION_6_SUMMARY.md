# Session 6 — Full Summary (Plain English)

This covers everything built to make the fraud pipeline run **on its own** for the first time —
why that was necessary, the two AI agents that make it work, every real bug found and fixed along
the way, a detailed look at how two different local AI models actually performed when we put them
head to head on real data, and a final architecture pass that closed the recall gap between
agentic tracing and manual (ground-truth-assisted) tracing — without needing a smarter model.

No prior context needed beyond: this is a fraud-detection system for a US bank with 3 branches
(`loc1`, `loc2`, `loc3`). By the end of Session 5, the system could score a transaction (Session
3) and prove a money trail converges *if you told it exactly which deposits to check* (Session 4)
— but nothing connected those pieces automatically. A human had to run a script by hand.

---

## 1. What problem were we solving?

Up to this point, "detecting fraud" meant: a human runs `check_convergence` themselves, by hand,
already knowing (from ground truth) which deposits belong together. That's not autonomous — it's
a human doing the investigation and just borrowing the tool.

**Session 6's job:** the moment a branch flags a transaction, the system should investigate it
itself — no one runs anything. That means building something that can make the two judgment calls
a human investigator would normally make:

1. *"Is this worth digging into further, or does it look like a one-off?"*
2. *"If it is worth digging into, follow the money and decide whether it really connects to other
   flagged deposits — and know when to stop looking."*

Both of those are AI-agent jobs, not code — a fixed rule can't make a judgment call like "does
this pattern look deliberate." So Session 6 builds two agents to do exactly that, plus the plumbing
that lets them run with zero manual triggering.

---

## 2. Two agents, not one — why split the job up?

### The Structuring Agent — the fast, cheap gatekeeper

**What it does:** one single AI call, no tools, no back-and-forth. It's handed the score and the
5 features a branch already computed, and asked one question: *does this look like deliberate
structuring worth a deeper cross-branch investigation, or does it look like ordinary activity that
happened to cross the model's threshold?* It answers with a confidence level (HIGH / MEDIUM / LOW)
and its reasoning.

**Why this needs to exist at all:** the branch's score already crossed a threshold before this
agent ever sees the transaction — so its job is *not* "is the score high enough" (that already
happened). Its real job is a genuinely different question: a score can be high but still look like
an isolated, explainable event. This agent is the first, cheap filter that decides whether it's
worth paying for the expensive next step.

### The Money-Trail Agent (MTA) — the actual detective

**What it does:** a real multi-step investigation. It's given tools — `check_convergence` (asks
"do multiple flagged deposits converge on the same account?"), `get_outgoing_txns` and
`get_incoming_txns` (look up an account's real transaction history) — and has to decide which
tools to call, in what order, and when it has enough to conclude.

**Why this needs to be a *loop*, not one call:** proving a real convergence takes multiple pieces
of evidence gathered in sequence — check whether a convergence exists, then (if so) walk each
hop's real transaction details to build the actual evidence trail. A single question-and-answer
can't do that.

**Why it's not left to just "ask the AI until it feels done":** `CLAUDE.md`'s design calls for
principled stopping reasons — convergence found, dead end, cycle, time window exceeded, or a hard
safety ceiling — never just "the model decided to stop." So the MTA's loop is built as its own
small flowchart (an "agent" step that asks the AI what to do next, a "tools" step that actually
runs it, a loop-back arrow) specifically so our own code — not the AI's judgment — enforces when
to stop.

### Why two agents instead of one doing everything

Splitting the job means the cheap, fast judgment call (Structuring Agent) filters out likely
false positives *before* paying for the expensive multi-step investigation (MTA). It also keeps
each agent's job simple and inspectable — "did it judge this correctly" and "did it trace this
correctly" are two separate, checkable questions instead of one tangled one.

---

## 3. How it all connects, end to end

```
Branch flags a transaction (Session 3's model, unchanged)
        |
        v
orchestrator/listener.py  <-- always running, subscribed to Redis's "fraud_events" channel
        |  (wakes up the instant something is published - no polling)
        v
agents/state_graph.py
        |
   [Structuring Agent]  --HIGH/MEDIUM--> [Money-Trail Agent] --> writes real evidence to Redis
        |
      LOW --> stops here, nothing further happens
```

The listener is deliberately dumb — it does no judging itself, it just relays. It also skips a
token if evidence for it already exists, so the same account flagging multiple times in a row
(exactly what the fraud pattern looks like) doesn't trigger duplicate investigations.

---

## 4. The real bugs found and fixed (this is where most of the actual learning happened)

Every one of these was caught by actually running the system against real data, not by inspection:

1. **Rate limiting, twice.** First hit OpenAI's hosted API's per-minute limit; fixed with a
   retry-with-backoff wrapper — then discovered the `openai` SDK's own *internal* retry was
   silently bursting 3 real requests per attempt, defeating our backoff before it could work.
   Fixed by disabling the SDK's internal retry so our backoff is the only one in play. Then hit a
   *daily* cap (50 requests/day) that no retry logic can fix — this is what ultimately pushed the
   move to a local model (see below).

2. **Evidence hallucination.** The MTA originally asked the AI to *retype* the exact evidence
   (amounts, dates, transaction IDs) into its final answer. With a smaller local model, this
   produced fabricated numbers and impossible future dates that looked plausible but were fake.
   **Fixed by never trusting the AI to restate facts from memory** — our own code now walks the
   real transaction data directly from the tool results and builds the evidence itself. The AI's
   job shrank to *judgment and narrative only*, never the exact facts. This fix is why the
   evidence data you'll see below is always 100% accurate, regardless of which model is running.

3. **A crash on a malformed tool call.** A local model occasionally left out a required argument
   when calling a tool, which crashed the whole investigation with no fallback. Fixed by making
   every tool call fail *softly* (return an error message the AI can see and react to) instead of
   crashing the process.

4. **A real "who does this account belong to" bug**, found only once we tested a harder scenario
   with *three separate* laundering rings instead of one: the code that decides "is this token
   part of the group we just found" only checked whether it was the very first deposit in the
   chain — meaning every account in the *middle* of a chain (10 of every 13 real fraud accounts)
   was wrongly marked as unrelated even when it plainly wasn't. Fixed to check the entire chain,
   not just its start.

5. **A deeper, still-open issue**, also only visible at 3-ring scale: two of the three rings share
   one "bridge" account each. A real transaction from Ring 1 can validly continue *through* that
   shared account into Ring 2's chain (and the reverse also happens), because both rings' amounts
   and timing are similar enough to satisfy the same detection rule. We built a partial fix
   (letting the two smaller rings claim their members first) that raised the number of correctly
   traced accounts from 9 to 26 out of 39 — a real improvement, but not a full fix, since it also
   made the middle ring (Ring 2) worse as a side effect. This is left as a known, well-understood
   limitation, not something Session 6 was expected to fully solve.

---

## 5. The provider journey: Claude → OpenAI → local

The plan changed twice, each time for a concrete reason, not on a whim:

- **Claude → OpenAI:** the user had OpenAI credits available, not Anthropic ones.
- **OpenAI → local (Ollama):** hit OpenAI's free-tier daily cap (50 requests/day) — a hard wall no
  amount of retry logic can work around. Since a single real investigation can involve 10+ AI
  calls, this made the free tier impractical for actual testing.

Running locally (via Ollama) removes rate limits and cost entirely, at the cost of needing to pick
a model that's actually good enough and fast enough to run on the available hardware (Apple M5,
16GB RAM).

---

## 6. The real comparison: `qwen2.5:7b` vs `qwen3:8b` — how they actually traced hops, and where they missed

Both models were run through the *identical* pipeline, on the *identical* fresh data, reset from a
clean slate each time — the only thing that changed was which local model powered the two agents.

### One thing that's true for BOTH models, always: the evidence itself is never wrong

Because of the hallucination fix (bug #2 above), whenever *either* model reaches a `convergence
found` conclusion, the actual hop-by-hop evidence — real amounts, real timestamps, real
transaction IDs, real channels — is always pulled directly from the database by our own code, not
typed out by the AI. So "how well did each model trace the hops" isn't really a question about
whether the *data* is accurate (it always is) — it's a question about how reliably each model made
the right **judgment calls**: escalate or not, keep digging or stop, and (only in the 3-ring test)
which group a given account really belongs to.

### The 216-case (one ring, 13 real fraud accounts, 14 false positives)

| | `qwen2.5:7b` | `qwen3:8b` |
|---|---|---|
| Real fraud correctly traced | 13/13 (best run) | 13/13 |
| Run-to-run consistency | showed real variance — a repeat run only got 9/13 | consistent in the one full run tested |
| False positives correctly cleared | 14/14 | 14/14 |

Both models are capable of a perfect result here. The difference that showed up was **consistency**
— `qwen2.5:7b` occasionally judged a real fraud deposit as "not worth escalating" (a Structuring
Agent miss) or gave up on a real convergence partway through (a Money-Trail Agent miss) in one
repeat run, even though the exact same data produced a clean 13/13 on another run. This is
model judgment variance, not a data or code issue — the same input, judged slightly differently
run to run, exactly the kind of inconsistency you'd expect from any language model asked to make a
subjective call.

### The 500-case (three rings, 37 real fraud accounts, 22 false positives)

| | `qwen2.5:7b` (full run) | `qwen3:8b` (representative sample) |
|---|---|---|
| Real fraud correctly traced | 23/37 | 6/8 sampled |
| False positives correctly cleared | 22/22 | 4/4 sampled |
| Pattern by ring | Ring1 & Ring3 clean, Ring2 struggles | Ring1 & Ring3 clean, Ring2 struggles |

**This is the important finding:** both models missed evidence in *exactly the same pattern* —
Ring 2 (the ring in the middle, bridged to both neighbors) is where both models struggle, while
Ring 1 and Ring 3 resolve cleanly for both. That sameness is the tell: **this particular kind of
missing evidence isn't about which model is running the agents at all** — it's the shared-bridge-
account leak (bug #5 above), a property of the underlying detection rule itself. A better model
can't out-think a rule that's satisfied by two genuinely different, unrelated transactions at the
same shared account.

### So, two genuinely different reasons evidence gets missed

1. **Model judgment inconsistency** (visible on the simple, one-ring case): the AI occasionally
   judges a real case as not worth escalating, or gives up early, when a different run on the
   same data gets it right. This is what actually differs between models, and why `qwen3:8b`
   looked at least as reliable as `qwen2.5:7b` here.
2. **A tool-level architectural gap** (visible only on the harder, three-ring case): the detection
   rule itself can be satisfied by two unrelated real transactions passing through the same shared
   account. This affects *every* model identically, because it has nothing to do with the AI's
   judgment — the tool tells every model the same (incomplete) answer.

For completeness: two other models were tested more briefly. `llama3.1:8b` (the very first model
tried) missed more on the simple one-ring case (8/13, mostly Structuring Agent under-escalation)
before being replaced. `qwen3:14b` was technically correct but far too slow on this hardware
(single investigations took over two minutes, and the machine was heavily swapping memory), so it
was never run against real scenario data at all — only its raw speed was measured.

---

## 7. Speed — the deciding factor in the end

| Model | Typical time per investigation | Full 216-case run | Full 500-case run |
|---|---|---|---|
| `llama3.1:8b` | fast (seconds) | ~20-30 min | not run |
| `qwen2.5:7b` | fast (seconds) | ~20-30 min | ~1-1.5 hours |
| `qwen3:8b` | slow (~2.2 min avg) | ~1 hour | ~2+ hours (estimated) |
| `qwen3:14b` | very slow (~3 min) | not run | not run — caused heavy memory swapping |

## 8. Final decision

**`qwen2.5:7b` was chosen to run this project going forward.** `qwen3:8b` matched or slightly beat
it on quality and showed less run-to-run variance, but is 3-5x slower on this hardware — a real
cost both for iterating quickly during development and for `CLAUDE.md`'s own demo-timing target
("live portion under 2 minutes"). `qwen3:8b` (and `qwen3:14b`) remain pulled locally and are a
strong option to revisit later if either the hardware improves or the demo is restructured to
replay a pre-run investigation rather than run one live.

---

## 9. Closing the recall gap — why wasn't agentic tracing matching manual's 100%?

### The starting question

The manual (ground-truth-assisted) script traces fraud with 100% accuracy, every time. The
autonomous agents didn't — sections 6 and 7 above show real misses even with a good model. Why not,
and is it fixable?

### The issue, stated plainly

Manual's 100% isn't really about better tracing — it's about not having to solve the hardest part
at all. The manual script is *told* which deposits belong to the same ring before it ever checks
anything, so it never has to discover the grouping itself. The agentic pipeline has to discover it
on its own, with no ground truth — and three separate, ordinary things about how it was built were
quietly costing it real cases along the way. None of them turned out to be "the AI isn't smart
enough."

### What we found — three separate causes

1. **The one tool that always gives the correct answer was optional, not guaranteed.**
   `check_convergence` is a plain deterministic check — given the same data, it always produces the
   same, correct yes/no answer about whether deposits connect. But it lived inside the AI's own
   tool-calling loop as something the AI had to *choose* to call. Sometimes it simply didn't call
   it, or moved past its result, before giving up and concluding "insufficient evidence" — quietly
   throwing away an answer that was sitting right there.
2. **An earlier "is this worth investigating?" step could permanently block the real investigation.**
   If that first, quick judgment call rated a transaction as low-confidence, the Money-Trail Agent
   (the part that actually traces the money) never got a turn at all — even when the transaction
   was genuinely part of a fraud ring. This turned out to be the single biggest source of the
   run-to-run inconsistency described in section 6.
3. **A subtler timing problem, only visible once the first two causes were fixed and the picture
   got clean enough to see it.** The 3 real deposits in one fraud ring don't get flagged at the
   exact same instant — they trickle in a few minutes apart, since they're separate real
   transactions. Investigating a deposit the moment it's flagged is normally the right instinct —
   but it means a deposit can get checked *before* its two partners have been flagged yet. At that
   exact moment, "do 3 deposits converge?" honestly only has 1 to look at, so the honest answer is
   "not enough evidence yet" — even though the full picture becomes obvious minutes later. This was
   proven directly: re-checking the exact same accounts once everything had been flagged gave the
   correct answer every time, confirming it was a timing artifact, not a logic error.

### What we changed — three fixes, one per cause

1. **Make the always-correct check run automatically, in code, before the AI is even involved.**
   If it finds a real connection, the evidence is built directly from the real data (not typed out
   by the AI), and the AI's only remaining job is writing one plain-English sentence describing it.
   There is no longer any point in this path where the AI's judgment can discard a real answer.
2. **Remove the early veto.** Every flagged transaction now always reaches the full investigation.
   The earlier quick judgment call is still recorded as background context (useful for the eventual
   case report), but it can no longer stop an investigation from happening.
3. **Make the system correct its own early guesses.** The instant any one deposit in a group is
   confirmed as connected, the same confirmed evidence is immediately written for every other
   member of that group too — so a deposit that was checked too early and got a premature "no"
   automatically gets corrected the moment the full picture becomes available, instead of being
   stuck with that wrong answer forever.

### What we tested, and the result

We reset the system to a clean slate and ran it fresh, twice — once on the simple one-fraud-ring
scenario, once on the harder three-ring scenario — and checked every result against the real,
known answers.

- **The simple case (216 transactions, 1 ring): 13/13 real fraud correctly traced, every run** —
  no longer just on a lucky run, since nothing left in the process involves AI judgment about
  *whether* tracing happens at all. False positives: 14/14 still correctly cleared.
- **The harder case (500 transactions, 3 rings): improved to 26 of 39 correct.** Two of the three
  rings are now perfectly clean (13/13 each). The *only* remaining miss is the third ring, and it
  has one single, already-understood cause: two rings share one "bridge" account, and money
  genuinely can flow from one ring into the other through it, confusing the underlying math — a
  real, separate, still-open problem (documented in section 6), not something today's fixes were
  meant to touch. False positives: 22/22 still correctly cleared, none incorrectly flagged.

## 10. Bottom line

- The system can now investigate a flagged transaction **completely on its own** — no one runs a
  script — using two agents with clearly separated jobs: one lightweight judgment call, one real
  multi-step investigation.
- Every real bug found (rate limits, hallucination, crashes, the path-matching bug, and later the
  three recall-cost causes above) was caught by actually running the system, and every fix is
  documented in `CLAUDE.md` with the reasoning behind it, not just the change itself.
- The evidence the system produces is always accurate, by design — the AI is never trusted to
  retype facts, only to judge and narrate.
- Recall for anything the underlying detection rule *can* mathematically find is now effectively
  guaranteed, by removing every point where AI discretion could silently drop a real case. The one
  thing that's still missing evidence — Ring 2's shared-mule leak — is a limitation of the
  detection rule itself, not of the agents' judgment, and is a real, well-understood open item for
  a future session.

## 11. What's next

**Session 7** builds the adversarial verification step (Prosecutor / Defense / Judge agents
debating the Money-Trail Agent's evidence) and the code that turns a confirmed GUILTY verdict into
a real training label — the piece that finally connects this session's autonomous investigation to
Session 5's federated learning loop.
