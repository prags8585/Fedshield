# FedShieldV2 — Report Agent + Human Review: Scope Document

**Read this before writing any code.** This is a planning document for the one piece of the
original Session 8 plan not yet built, meant to be handed to a fresh session with no prior
conversation context. For the full story of everything already built, read (in this order):
`CLAUDE.md`, `SESSION_6_SUMMARY.md`, `VERDICT_AGENT_SUMMARY.md`.

---

## 1. What's already built and verified — read this first, don't rebuild any of it

The full autonomous investigation pipeline exists and has been verified end to end, multiple
times, including a full from-scratch run (fresh data → fresh model → Docker → autonomous
investigation → FL round):

```
Transaction flagged (branch_node/consumer.py, unchanged since Session 3)
        |
        v
orchestrator/listener.py  <-- always-on, subscribed to Redis's "fraud_events" channel
        |
        v
agents/state_graph.py
        |
   [Structuring Agent]  --always--> [Money-Trail Agent]
        |
        v
   check_convergence runs automatically (deterministic, not an LLM choice)
        |
   convergence found?
        |-- No --> LLM explores briefly, concludes insufficient_evidence/cycle, done.
        |-- Yes -->
              1. Evidence built deterministically from real Neo4j data (never AI-typed facts)
              2. Evidence written for EVERY member of the confirmed group at once
              3. Verdict Agent renders {verdict, confidence, rationale} for the whole group
              4. Verdict written for every group member
              5. If GUILTY + confident: Label Generator writes real training labels into
                 each involved branch's labels:{branch} Redis buffer
              6. Those labels get consumed by the next real FL round automatically
                 (branch_node/fl_data.py's build_branch_partition(include_real_labels=True))
```

**Files already built, working, and tested — do not modify without a real reason:**
- `agents/structuring_agent.py`, `agents/money_trail_agent.py`, `agents/verdict_agent.py`,
  `agents/label_generator.py`, `agents/state_graph.py`, `orchestrator/listener.py`
- `branch_node/fl_data.py` (`real_labels_for_branch`, `build_branch_partition`'s
  `include_real_labels` flag), `branch_node/fl_client.py`

**Verified numbers** (216-case, one ring): 13/13 real fraud correctly traced, 13 verdicts (all
GUILTY, ~0.9+ confidence), 13 real labels written and correctly split across branches, all
consumed by the next FL round. Full detail and exact commands to reproduce this yourself are in
`DEMO_RUNBOOK_FULL.md`.

---

## 2. What's NOT built yet — this is the actual scope of this work

Two things, both small relative to everything above:

1. **`agents/report_agent.py`** — does not exist yet.
2. **`review_report.py`** — does not exist yet.

Everything the Report Agent needs already exists as real data sitting in Redis by the time a
verdict is confirmed - **this is a genuinely small build**, not a new pipeline stage that needs
its own tool-calling loop or deterministic-check redesign like the earlier agents did.

---

## 3. What already exists for this, ready to use

- **`shared/schemas.py`** already defines the `Report` model:
  ```python
  class Report(BaseModel):
      token_id: str
      body: str  # must end with the privacy attestation line
      status: str = "PENDING_REVIEW"  # PENDING_REVIEW | APPROVED | REJECTED
  ```
- **`shared/redis_keys.py`** already defines `report_key(token_id) -> "reports:{token_id}"`.
- **The exact privacy attestation line** (from the original FedShield's Report Agent design,
  carried over as a hard requirement): every report **must end with**
  *"Privacy attestation: No customer name, account number, or raw transaction data was accessed
  at any stage."* This is not optional flavor text - it's the whole point of the report proving
  the investigation stayed within the masked/tokenized data boundary the entire time.
- **Every input the Report Agent needs is already real, structured data**, not something that
  needs to be gathered or computed fresh:
  - `evidence:{token_id}` → `{path, convergence_node, stop_reason, summary}` - the real hop-by-hop
    evidence (already deterministic and accurate, per `CLAUDE.md`'s "Session 6 Update").
  - `verdicts:{token_id}` → `{verdict, confidence, rationale}` - the Verdict Agent's reasoning.
  - The Structuring Agent's `reasoning`/`confidence` (currently only passed as in-memory context
    between agents, not separately persisted to Redis - check whether it needs to be persisted so
    the Report Agent can access it after the fact, or whether the Report Agent should be invoked
    inline, in the same call chain, while it's still in scope - see section 5).

---

## 4. What the Report Agent needs to do

**One LLM call, no tools** - same shape as `agents/structuring_agent.py` and the "convergence
found" summary path in `agents/money_trail_agent.py`'s `_summarize_convergence`, not a
multi-turn tool-calling loop like the Money-Trail Agent's exploration path. There is nothing left
to investigate by this point - only to write up what's already been confirmed.

- **Only ever called when a verdict is GUILTY** (matching `CLAUDE.md`'s pipeline: the Report Agent
  sits downstream of a confirmed verdict, not every investigation - a NOT_GUILTY case has nothing
  to report).
- **Input:** the real evidence (hops, convergence account), the real verdict (verdict, confidence,
  rationale), and the Structuring Agent's original reasoning for context.
- **Output:** a plain-English write-up with, at minimum: what was flagged, what the investigation
  found (in plain terms, not raw JSON), the verdict and why, and the privacy attestation line as
  the last line, always, with no exceptions.
- **Where it's called from - a real design decision, not a given:** the pattern established by
  the Verdict Agent and Label Generator is to run everything for a confirmed group **immediately,
  inline, inside `money_trail_agent()`**, the moment convergence is confirmed - not as a separate
  downstream step someone has to remember to trigger. The Report Agent should very likely follow
  the same pattern (called right after `generate_labels(...)` in `agents/money_trail_agent.py`),
  for the same reason: report drafting shouldn't depend on a separate process remembering to pick
  up confirmed cases later. **Confirm this reasoning still holds before building it any other way.**
- **Report scope - per token or per case?** Evidence and verdicts are currently written
  identically under *every* group member's own key (e.g. all 13 real fraud accounts in the
  216-case share the same evidence/verdict content). Decide explicitly whether the Report Agent
  should do the same (one identical report per group member, simplest, matches the existing
  pattern) or write one canonical report keyed by the convergence account only. **Recommendation:
  match the existing pattern (write for every group member) unless there's a concrete reason not
  to** - `review_report.py` then needs to decide whether approving one token's report should also
  mark its siblings' reports approved (see section 5).

---

## 5. What `review_report.py` needs to do

A small, standalone CLI script (per the folder structure in `CLAUDE.md`, this lives at the project
root, not inside `agents/`):
```
python review_report.py --approve <token>
python review_report.py --reject <token>
```
- Reads `reports:{token}`, updates its `status` field, writes it back.
- **Open decision, don't assume:** if reports are written per-group-member (section 4's
  recommendation), does approving one token's report also update every sibling's report status to
  keep the whole case consistent? If yes, this needs the same group-membership lookup pattern
  already used in `agents/money_trail_agent.py` (the confirmed group's tokens are derivable from
  `evidence:{token}`'s `path` field). Decide and document this explicitly before writing the
  script, don't leave it ambiguous.

---

## 6. The one thing that must be true, and must be explicitly tested

**Human review must never gate the label generator or FL feedback.** By the time any report
exists, the label has already been written - that happened the moment the Verdict Agent rendered
GUILTY, fully independent of this new code. This is already true in the current implementation
(labels are written before this new Report Agent code would even run, in the same function). The
integration test for this work should explicitly prove it stays true: run the pipeline, confirm
labels are already in `labels:{branch}` *before* `review_report.py` is ever run, then run
`review_report.py --approve` afterward and confirm nothing about the label/FL state changes as a
result. Don't just assume this from reading the code - verify it with a real run, the same way
every other claim in this project has been verified rather than assumed.

---

## 7. Suggested build/verification order

1. **Standalone smoke test** for `agents/report_agent.py` - hand-fed fake evidence/verdict, no
   Redis/Neo4j involved (same pattern as `agents/verdict_agent.py`'s own `if __name__ ==
   "__main__"` block). Confirm the privacy attestation line is always present, even if you
   deliberately break the input to test the fallback path.
2. **Wire it into `agents/money_trail_agent.py`**, resolve the "where exactly" and "per-token vs.
   per-case" decisions from section 4 explicitly, don't leave them implicit.
3. **`review_report.py`**, resolve its open decision from section 5.
4. **Full pipeline test**, reusing the exact commands in `DEMO_RUNBOOK_FULL.md` up through Step 8
   - after that, add: check `reports:{token}` exists with `status: PENDING_REVIEW` and the correct
   privacy attestation line, run `review_report.py --approve <token>`, confirm status flips to
   `APPROVED`, and confirm (per section 6) that labels were already present in Redis *before* this
   approval step ran.
5. **Update `SESSION_PLAN.md`** to mark Session 8 complete, following the exact pattern already
   used for Sessions 6 and 7 (verified numbers, not just "done").
