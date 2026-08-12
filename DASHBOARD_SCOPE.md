# FedShieldV2 — Live Demo Dashboard: Scope Document

**Read this before writing any dashboard code.** This is a planning document for a NEW piece of
work, meant to be handed to a fresh session (or picked up tab by tab across several sessions) —
it assumes no prior conversation context beyond what's written here and in `CLAUDE.md`.

---

## 0. This reverses an existing, deliberate scope decision — read `CLAUDE.md` first

`CLAUDE.md`'s "Critical Design Decisions" section explicitly lists:

> **Explicitly cut for scope/time — do not add back without a scope discussion:** ... FastAPI
> backend, Streamlit or any dashboard ...

That discussion happened, and the decision is: **build it, but in phases, cheapest/highest-value
first.** This file *is* that scope discussion, written down. Don't skip re-reading `CLAUDE.md`,
`SESSION_6_SUMMARY.md`, `VERDICT_AGENT_SUMMARY.md`, and both `DEMO_RUNBOOK*.md` files before
starting — this dashboard visualizes an already-complete, already-verified pipeline. It does not
change anything about how the agents work; it's purely a presentation layer on top.

---

## 1. What this is, in one sentence

A browser-based dashboard, in 4 tabs, that runs and visualizes the exact same commands documented
in `DEMO_RUNBOOK_FULL.md` and `DEMO_RUNBOOK_AGENTS.md` — real subprocess output streamed live into
terminal-style cards, plus a live Neo4j graph view — instead of the user manually juggling 4-6
terminal windows.

**Hard constraint carried over from every prior session in this project: never fake or mock the
output.** Every terminal card streams a real running process's real stdout. Every graph view
queries the real Neo4j instance. If something isn't wired up yet, show it empty/idle, don't
simulate it.

---

## 2. Recommended architecture (decide/confirm before building, don't just assume)

**Backend:** a small FastAPI service (Python — consistent with the rest of this project, and lets
the backend `import` the existing `agents/`, `orchestrator/`, `branch_node/` modules directly
instead of only shelling out to them).
- Manages subprocesses: starts/stops/tracks `docker compose`, `branch_node/producer.py`,
  `orchestrator/listener.py`, `fl_server/server.py`, `branch_node/fl_client.py`, etc.
- Streams each managed process's stdout line-by-line to the frontend over **WebSockets** — one
  logical channel per terminal card.
- Exposes a `reset` endpoint per tab (see section 6).
- For agent reasoning: subscribe to Redis directly (`fraud_events`, `evidence:*`, `verdicts:*`)
  rather than scraping/parsing terminal text — the data is already structured JSON, no need to
  re-parse printed strings.

**Frontend:** a single-page app (React is the safe default; a simpler vanilla-JS approach is also
fine given this is an internal demo tool, not a product).
- **Terminal cards:** [`xterm.js`](https://xtermjs.org/) — the same library VS Code's own
  integrated terminal uses. Each card is one xterm.js instance fed by one WebSocket channel.
- **Neo4j graph view:** either (a) embed the real Neo4j Browser via `<iframe>` pointed at
  `http://localhost:7475` (fastest to ship, looks like a separate tool, not custom-styled), or (b)
  [`neovis.js`](https://github.com/neo4j-contrib/neovis.js) (built specifically for rendering
  Cypher query results as an interactive graph inside your own page — more work, better fit).
  **Decide which before starting Tab 1** — don't build the Cypher input box twice.

**Do not use Streamlit for this.** Streamlit can't do multiple concurrent live-streaming terminal
panes and an interactive graph view well — it's built around a single top-to-bottom rerun model,
which fights against exactly what this dashboard needs. This is the specific reason `CLAUDE.md`
originally cut "Streamlit or any dashboard" — a real, non-trivial reason, not just "time."

---

## 3. Build order — phases, not all 4 tabs at once

Build in this order. Each phase should be fully working and demoable before starting the next —
don't parallelize across phases.

1. **Phase 1 — Tab 1 (Manual/216) with basic terminal streaming.** Proves the core plumbing
   (subprocess management + WebSocket streaming + xterm.js rendering) works, on the simplest tab
   (no agents, no live reasoning, no graph animation - just real commands streaming).
2. **Phase 2 — Neo4j Cypher playground**, still under Tab 1. Decide iframe vs. `neovis.js` here
   (section 2) and get a static "type a query, see the graph" working before touching live
   animation.
3. **Phase 3 — Tab 2 (Agentic/216).** Reuses Phase 1's terminal infrastructure exactly. New parts:
   the "master terminal" orchestration button, and a live agent-reasoning panel fed from Redis
   (not the graph animation yet - that's Phase 5).
4. **Phase 4 — Tab 3 (Agentic/500).** Should require almost no new code if Phase 3 was built
   generically (same components, different dataset file paths/env values - see section 4).
5. **Phase 5 — live graph tracing/highlighting** (the hardest part, deliberately last - see
   section 7). Add this to Tabs 2 and 3 once everything else in them already works without it.
6. **Phase 6 — Tab 4 (FL).** Independent of the other tabs; can be built any time after Phase 1,
   but sequenced last here since it's lower-stakes for the "wow" factor of a live demo.

---

## 4. Tab-by-tab requirements

Every exact command below has already been run and verified working earlier in this project - see
`DEMO_RUNBOOK_FULL.md` and `DEMO_RUNBOOK_AGENTS.md` for the full context/expected output of each.
Do not re-derive these from scratch; wire the dashboard to run these exact commands.

### Tab 1 — Manual (216 case)

**5 terminal cards:**
| Card | Command | Notes |
|---|---|---|
| Data generation | `PYTHONPATH=. python3 simulator/customers.py --per-branch 100 --seed 42 --out data/customers.json` then `simulator/data_generator.py ...` then `simulator/layering_scenario.py --hops 4 --seed 42` | Run once per reset, three commands in sequence, before Docker comes up |
| branch-loc1 | `docker logs -f fedshieldv2-branch-loc1` | |
| branch-loc2 | `docker logs -f fedshieldv2-branch-loc2` | |
| branch-loc3 | `docker logs -f fedshieldv2-branch-loc3` | |
| Producer | `KAFKA_BROKER=localhost:29092 PYTHONPATH=. python3 branch_node/producer.py --files data/background.json data/layering_hops4_events.json --background-window-seconds 20` | The one action a "run" button triggers |

**Below:** Neo4j Cypher playground (query box + graph render), pointed at `bolt://localhost:7688`.

**Refresh button:** `docker compose down -v && docker compose up -d`, then re-run data
generation + model training (`branch_node/train_model.py`) before the branches come up clean.

### Tab 2 — Agentic (216 case)

Same 5 terminal cards as Tab 1, **plus a master terminal above them**:
- **Master terminal "Start" button** runs, in order: confirm Docker is up → start
  `NEO4J_URI=bolt://localhost:7688 REDIS_URL=redis://localhost:6380 PYTHONPATH=. python3 -u
  orchestrator/listener.py` (a long-running managed process, stream its output here) → then fire
  the same producer command as Tab 1.
- **Live agent-reasoning panel:** subscribe to Redis and show, per token as it resolves: the
  Structuring Agent's `reasoning`/`confidence`, the Money-Trail Agent's `stop_reason`/`summary`,
  and (if `convergence_found`) the Verdict Agent's `verdict`/`confidence`/`rationale` and how many
  labels were written. All of this is already being written to Redis (`evidence:{token}`,
  `verdicts:{token}`) - no new backend logic needed to produce the data, only to display it.
- Same Neo4j playground below (static in this phase; see Phase 5 for live highlighting).

**Refresh button:** same as Tab 1's, plus also flush `flagged_accounts`, all `evidence:*`,
`verdicts:*`, and `labels:*` keys so a fresh run doesn't see stale "already has evidence" skips.

### Tab 3 — Agentic (500 case)

Identical to Tab 2, with two swapped inputs:
- Data generation commands use `--out data/scenario_500/...` variants and
  `simulator/multi_ring_scenario.py --num-rings 3 --hops 4 --seed 500` (see `DEMO_RUNBOOK.md`'s
  Scenario 2 section for the exact commands).
- Producer command points at `data/scenario_500/background.json` and
  `data/scenario_500/multi_ring_events.json`, with `--background-window-seconds 45`.

**Set real expectations in the UI for this tab**: ~59 flagged accounts instead of ~27, and a full
run takes multiple hours on `qwen2.5:7b` (see `CLAUDE.md`'s "Session 6 Update" for per-model
timing). Also expect 2 of 3 rings to resolve perfectly and the third to show real, already-
documented misses (the shared-mule-account limitation) - the dashboard should not present this as
a bug when it happens.

### Tab 4 — FL

**4 terminal cards:**
| Card | Command |
|---|---|
| fl_server | `PYTHONPATH=. python3 fl_server/server.py` |
| fl_client (loc1) | `BRANCH_ID=loc1 REDIS_URL=redis://localhost:6380 PYTHONPATH=. python3 branch_node/fl_client.py` |
| fl_client (loc2) | same, `BRANCH_ID=loc2` |
| fl_client (loc3) | same, `BRANCH_ID=loc3` |

**Results panel:** parse/display each round's validation AUC (from the server's stdout) and each
client's `includes N real agent-verified label(s)` line (round 1 only - see
`DEMO_RUNBOOK_AGENTS.md` for why rounds 2-5 always show 0).

**Refresh button:** stop all 4 processes; does NOT need to touch Docker/Neo4j at all - this tab is
independent of Tabs 1-3's data.

---

## 5. The live agent-reasoning panel — data source, already built

Don't scrape terminal text for this. Everything needed is already structured data in Redis:
- `evidence:{token_id}` → `{path, convergence_node, stop_reason, summary, verdict, labels_written}`
- `verdicts:{token_id}` → `{verdict, confidence, rationale}`
- Subscribe to the `fraud_events` Redis pub/sub channel (same channel `orchestrator/listener.py`
  already subscribes to) to know the instant a new token starts being investigated, then poll/read
  its `evidence:{token_id}` key once it's ready.

---

## 6. Reset/refresh — be precise about blast radius per tab

Each tab's refresh should reset **only what that tab owns**, not the whole stack, so testing one
tab doesn't silently wipe another's in-progress state:
- Tabs 1-3 share the same Docker stack, Redis, and Neo4j - a refresh on any of them realistically
  needs to reset all of it (`docker compose down -v && up -d`), since they're not isolated from
  each other at the infrastructure level today. Say this plainly in the UI ("resetting will also
  clear other tabs' data") rather than implying tab-level isolation that doesn't exist.
- Tab 4 (FL) only touches `labels:*` Redis keys and the FL server/client processes - its refresh
  can be fully independent, no Docker/Neo4j involvement needed.

---

## 7. The hardest part, deliberately last: live graph tracing

**No off-the-shelf tool does "highlight nodes as an AI agent visits them" out of the box.** Two
viable approaches, in increasing order of effort:
1. **Poll-and-diff:** the frontend re-runs a Cypher query on an interval (e.g. every 2-3 seconds)
   showing all `Account` nodes with `flagged: true` or matching the currently-investigated token's
   evidence path, and just re-renders. Cheap, slightly janky, no backend changes needed.
2. **True event-driven highlighting:** add a small instrumentation hook inside
   `agents/money_trail_agent.py`'s tool-calling code (`_run_tool`/`_run_exploration_tool`,
   `_build_evidence_hops`) that publishes a tiny event (e.g. to a new Redis channel
   `trace_events`) every time a real Neo4j query actually runs, with which node/edge it touched.
   The dashboard backend relays these over the same WebSocket infrastructure as the terminal
   cards, and the frontend graph view highlights progressively. This is a real, if small, code
   change to already-tested agent code - treat it with the same care as any other change to
   `agents/money_trail_agent.py` (re-run the existing verification steps in `SESSION_6_SUMMARY.md`
   after adding it, don't just trust it didn't break anything).

Do not attempt option 2 until options in earlier phases are fully working - it's the one piece of
this whole dashboard that touches already-verified agent code, so it carries real regression risk
if rushed.

---

## 8. Open decisions the next session should make explicitly, not assume

- React vs. a simpler frontend approach - either is fine, but pick one before Phase 1 starts.
- iframe-embedded Neo4j Browser vs. `neovis.js` - affects how much of Phase 2/7 is "wire up an
  existing tool" vs. "build a graph renderer."
- Whether the backend runs on the host (matching how every command in `DEMO_RUNBOOK_FULL.md` is
  already run today, with the same `REDIS_URL=redis://localhost:6380` /
  `NEO4J_URI=bolt://localhost:7688` host-side overrides) or inside its own Docker container (would
  need its own set of connection strings, and access to `docker compose` from inside a container
  is its own extra complexity - the host-side approach is recommended unless there's a specific
  reason to containerize the dashboard backend too).
