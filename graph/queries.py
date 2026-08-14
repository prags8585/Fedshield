"""get_outgoing_txns / get_incoming_txns (simple parametrized Cypher - these
double as the Session 6 Money-Trail Agent's exploration tools) and
check_convergence (the chaining rule + convergence algorithm locked in
CLAUDE.md's "Session 4 Final Plan").

Why the hop-by-hop traversal is Python, not one Cypher query: the chaining
rule is a *pairwise* constraint between consecutive edges on a path (each
hop's timestamp must fall within a window of the previous hop's timestamp,
each hop's amount must be within a ratio of the previous hop's amount).
Plain Cypher variable-length patterns (`-[:TRANSACTED*1..12]->`) can't
compare adjacent edges along the match without APOC procedures, which are
not guaranteed installed on a stock neo4j:5.20-community image. So Neo4j
does all the storage and the per-hop edge lookups (via get_outgoing_txns),
and this module walks the path in Python - same algorithm CLAUDE.md
specifies, just not expressed as a single query string.
"""
from datetime import datetime, timedelta

DEFAULT_PER_HOP_WINDOW_HOURS = 8.0
DEFAULT_AMOUNT_RATIO_MIN = 0.90
DEFAULT_AMOUNT_RATIO_MAX = 1.05
DEFAULT_MAX_DEPTH = 12
DEFAULT_MIN_SOURCES_FOR_CONVERGENCE = 3
DEFAULT_MIN_BRANCHES_FOR_CONVERGENCE = 2
DEFAULT_MIN_PRESERVATION_RATIO_TO_SINK = 0.70


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")


def _dedupe_by_txn_id(records: list) -> list:
    """Neo4j Community edition has no relationship uniqueness constraint, so
    a Kafka at-least-once redelivery that gets reprocessed by graph_writer
    can (rarely) leave two identical TRANSACTED edges for the same txn_id.
    Collapsing here keeps every downstream caller (traversal, evidence
    counts) correct regardless of that upstream duplication, instead of
    silently double-counting one real transaction as two hops.
    """
    seen = set()
    deduped = []
    for record in records:
        if record["txn_id"] in seen:
            continue
        seen.add(record["txn_id"])
        deduped.append(record)
    return deduped


def get_outgoing_txns(driver, token_id: str, after_ts: str = None) -> list:
    """Edges leaving token_id, time-ordered. after_ts (if given) excludes
    edges at or before that timestamp - the per-hop rule needs strictly
    later hops.
    """
    query = """
        MATCH (a:Account {token_id: $token_id})-[r:TRANSACTED]->(b:Account)
        WHERE $after_ts IS NULL OR r.ts > $after_ts
        RETURN b.token_id AS to_token, r.txn_id AS txn_id, r.amount AS amount,
               r.ts AS ts, r.txn_type AS txn_type, r.channel AS channel,
               r.branch_id AS branch_id
        ORDER BY r.ts ASC
    """
    with driver.session() as session:
        result = session.run(query, token_id=token_id, after_ts=after_ts)
        return _dedupe_by_txn_id([dict(record) for record in result])


def get_incoming_txns(driver, token_id: str) -> list:
    """Edges arriving at token_id, time-ordered."""
    query = """
        MATCH (a:Account)-[r:TRANSACTED]->(b:Account {token_id: $token_id})
        RETURN a.token_id AS from_token, r.txn_id AS txn_id, r.amount AS amount,
               r.ts AS ts, r.txn_type AS txn_type, r.channel AS channel,
               r.branch_id AS branch_id
        ORDER BY r.ts ASC
    """
    with driver.session() as session:
        result = session.run(query, token_id=token_id)
        return _dedupe_by_txn_id([dict(record) for record in result])


def _trace_forward(
    driver,
    source_token: str,
    source_amount: float,
    source_ts: str,
    source_branch: str,
    per_hop_window_hours: float,
    amount_ratio_min: float,
    amount_ratio_max: float,
    max_depth: int,
) -> tuple:
    """DFS forward from one flagged deposit, following only edges that
    satisfy the chaining rule. Cycle-safe: a per-path visited set means a
    branch that revisits a node is pruned there rather than looping forever.
    Every leaf (dead end, cycle-pruned point, or safety-ceiling depth) is
    recorded as a terminal - check_convergence groups these across sources.

    Returns (terminals, cycle_hit) where terminals is a list of dicts:
    {account, amount, ts, depth, path, branches}.
    """
    window = timedelta(hours=per_hop_window_hours)
    terminals = []
    cycle_hit = [False]

    def dfs(current_token, current_amount, current_ts, depth, path, branches, visited):
        if depth >= max_depth:
            terminals.append(
                {"account": current_token, "amount": current_amount, "ts": current_ts,
                 "depth": depth, "path": path, "branches": branches, "stop_reason": "safety_ceiling"}
            )
            return

        cur_dt = _parse_ts(current_ts)
        outgoing = get_outgoing_txns(driver, current_token, after_ts=current_ts)

        valid_hops = []
        for edge in outgoing:
            edge_dt = _parse_ts(edge["ts"])
            if edge_dt > cur_dt + window:
                continue
            ratio = (edge["amount"] / current_amount) if current_amount else 0.0
            if not (amount_ratio_min <= ratio <= amount_ratio_max):
                continue
            if edge["to_token"] in visited:
                cycle_hit[0] = True
                continue
            valid_hops.append(edge)

        if not valid_hops:
            terminals.append(
                {"account": current_token, "amount": current_amount, "ts": current_ts,
                 "depth": depth, "path": path, "branches": branches, "stop_reason": "dead_end"}
            )
            return

        for edge in valid_hops:
            dfs(
                edge["to_token"], edge["amount"], edge["ts"], depth + 1,
                path + [edge["to_token"]], branches | {edge["branch_id"]}, visited | {edge["to_token"]},
            )

    start_branches = {source_branch} if source_branch else set()
    dfs(source_token, source_amount, source_ts, 0, [source_token], start_branches, {source_token})
    return terminals, cycle_hit[0]


def check_convergence(
    driver,
    sources: list,
    per_hop_window_hours: float = DEFAULT_PER_HOP_WINDOW_HOURS,
    amount_ratio_min: float = DEFAULT_AMOUNT_RATIO_MIN,
    amount_ratio_max: float = DEFAULT_AMOUNT_RATIO_MAX,
    max_depth: int = DEFAULT_MAX_DEPTH,
    min_sources_for_convergence: int = DEFAULT_MIN_SOURCES_FOR_CONVERGENCE,
    min_branches_for_convergence: int = DEFAULT_MIN_BRANCHES_FOR_CONVERGENCE,
    min_preservation_ratio_to_sink: float = DEFAULT_MIN_PRESERVATION_RATIO_TO_SINK,
    return_all_candidates: bool = False,
) -> dict:
    """Traces forward from every flagged deposit in `sources`
    (each {"token_id", "amount", "ts", "branch_id"}), groups where those
    paths land, and confirms convergence only when enough independent
    sources, enough distinct branches, and enough preserved value all land
    on the same account. Returns the full evidence dict from CLAUDE.md -
    never a bare boolean - so a caller never has to redo the traversal to
    get the paths, timing, or amounts behind a verdict.

    `return_all_candidates=True` (additive, default False so every existing
    caller is unaffected) returns EVERY destination account that passed the
    gates, not just the single best one - added for the Session 6 multi-ring
    fix in agents/money_trail_agent.py, see CLAUDE.md's "Session 6 Update".
    With multiple simultaneous independent rings sharing a bridge account,
    the single-best behavior always lets one "absorbing" group beat out
    several legitimate smaller ones; resolving that needs to see all of them
    at once, not just whichever looks biggest in isolation.
    """
    terminals_by_source = {}
    any_cycle = False
    for src in sources:
        terminals, cycle_hit = _trace_forward(
            driver, src["token_id"], src["amount"], src["ts"], src.get("branch_id"),
            per_hop_window_hours, amount_ratio_min, amount_ratio_max, max_depth,
        )
        terminals_by_source[src["token_id"]] = terminals
        any_cycle = any_cycle or cycle_hit

    by_account = {}
    for source_token, terminals in terminals_by_source.items():
        for t in terminals:
            by_account.setdefault(t["account"], []).append({**t, "source_token": source_token})

    source_amount_by_token = {s["token_id"]: s["amount"] for s in sources}
    source_ts_by_token = {s["token_id"]: s["ts"] for s in sources}

    best = None
    all_candidates = []
    for account, arrivals in by_account.items():
        # A source can reach the same account via >1 path; keep its best (highest-value) arrival only.
        per_source_best = {}
        for a in arrivals:
            cur = per_source_best.get(a["source_token"])
            if cur is None or a["amount"] > cur["amount"]:
                per_source_best[a["source_token"]] = a
        arrivals = list(per_source_best.values())

        num_sources = len(arrivals)
        branches_seen = set()
        for a in arrivals:
            branches_seen |= a["branches"]
        num_branches = len(branches_seen)

        if num_sources < min_sources_for_convergence or num_branches < min_branches_for_convergence:
            continue

        total_source_amount = sum(source_amount_by_token[a["source_token"]] for a in arrivals)
        total_reached_amount = sum(a["amount"] for a in arrivals)
        preservation_ratio = (total_reached_amount / total_source_amount) if total_source_amount else 0.0

        if preservation_ratio < min_preservation_ratio_to_sink:
            continue

        candidate = {
            "has_convergence": True,
            "convergence_account": account,
            "num_sources": num_sources,
            "num_branches": num_branches,
            "paths": [a["path"] for a in arrivals],
            "shortest_depth": min(a["depth"] for a in arrivals),
            "longest_depth": max(a["depth"] for a in arrivals),
            "time_to_convergence_minutes": round(
                (max(_parse_ts(a["ts"]) for a in arrivals)
                 - min(_parse_ts(source_ts_by_token[a["source_token"]]) for a in arrivals)).total_seconds() / 60,
                2,
            ),
            "total_source_amount": round(total_source_amount, 2),
            "total_reached_amount": round(total_reached_amount, 2),
            "amount_preservation_ratio": round(preservation_ratio, 4),
            "cycle_detected": any_cycle,
        }

        all_candidates.append(candidate)
        if best is None or (candidate["num_sources"], candidate["amount_preservation_ratio"]) > (
            best["num_sources"], best["amount_preservation_ratio"]
        ):
            best = candidate

    if return_all_candidates:
        return {"has_convergence": bool(all_candidates), "candidates": all_candidates, "cycle_detected": any_cycle}

    if best is not None:
        return best

    return {
        "has_convergence": False,
        "convergence_account": None,
        "num_sources": 0,
        "num_branches": 0,
        "paths": [],
        "shortest_depth": None,
        "longest_depth": None,
        "time_to_convergence_minutes": None,
        "total_source_amount": round(sum(s["amount"] for s in sources), 2),
        "total_reached_amount": 0.0,
        "amount_preservation_ratio": 0.0,
        "cycle_detected": any_cycle,
    }
