"""Case 1 variant: multiple independent laundering rings running at the same
time, with a couple of them deliberately sharing a mule account with each
other - so the resulting graph is one connected web (ring1 <-shared account->
ring2 <-shared account-> ring3) instead of N separate converging stars sitting
near each other on screen. Each ring still resolves to its own, separate
consolidation account - the shared accounts are just a hop that happens to
carry two different rings' money at two different times/amounts, same as any
other "noisy" account (see layering_scenario.py's _mule_noise_events): the
existing amount+time chaining rule in graph/queries.py already keeps each
ring's own trace on its own trail, so nothing in check_convergence needs to
change to support this - it's purely a scenario-generation change.

Reuses layering_scenario.py's event-construction helpers (identity minting,
event shaping, telemetry) rather than reimplementing them - this file only
adds the ring-loop, the guaranteed-cross-branch-hop rule, and the shared-slot
mechanism on top.
"""
import argparse
import json
import random
from datetime import datetime, timedelta
from typing import Optional

from faker import Faker

from shared.config import BRANCH_IDS, CTR_THRESHOLD_USD
from simulator.layering_scenario import (
    CASH_OUT_SENTINEL,
    CASH_SENTINEL,
    _fmt_ts,
    _IdMinter,
    _make_event,
    _mint_identity,
    _party_info,
    _telemetry_for,
)

fake = Faker("en_US")

PERSON_KEYS = ("p1", "p2", "p3")

# A distinct reserved id range from layering_scenario.py's own
# (9_000_000_001 / 90_000_000): both scripts mint their consolidation
# identity first from a fresh _IdMinter, so sharing a start point would make
# scenario_500's ring1 consolidation account collide with the original
# 216-txn scenario's consolidation account - same account_number, same
# GLOBAL_TOKEN_SALT, same token_id - silently merging two unrelated
# scenarios if they were ever loaded into one graph without wiping between.
_MULTI_RING_ACCOUNT_START = 9_500_000_001
_MULTI_RING_CUSTOMER_START = 95_000_000


def _next_branch(current_branch: str, *also_exclude: str) -> str:
    """Guarantees a hop actually changes branch - a plain random.choice over
    all branches can land back on the same one by chance; this always picks
    a branch other than current_branch (and any also_exclude, e.g. the
    ring's consolidation branch for the last intermediate, so the FINAL hop
    into consolidation is guaranteed cross-branch too, not just every hop
    before it), so every hop in every ring genuinely crosses branches
    instead of doing so only by coincidence. Falls back to allowing a repeat
    only if 3 branches can't otherwise satisfy every exclusion at once.
    """
    excluded = {current_branch, *also_exclude}
    candidates = [b for b in BRANCH_IDS if b not in excluded]
    return random.choice(candidates or [b for b in BRANCH_IDS if b != current_branch])


def _build_ring(
    minter: _IdMinter,
    ring_id: str,
    hops: int,
    placement_min: float,
    placement_max: float,
    placement_window_minutes: float,
    span_hours: float,
    compress_seconds: float,
    scenario_start: datetime,
    shared_slots: dict,
) -> tuple:
    """Builds one ring's 3 chains + consolidation + exit withdrawal -
    mirrors layering_scenario.generate_layering_scenario's per-ring logic,
    except every hop is forced to a different branch, and any (person_key,
    hop_index) key present in shared_slots reuses that (customer, account)
    pair instead of minting a fresh one, wiring this ring to an earlier
    ring's account.

    Returns (events, ring_ground_truth, chain_pool) - chain_pool hands back
    every (account, customer) minted for this ring's chains, keyed by person,
    so a LATER ring can pull one of THIS ring's intermediates as its own
    shared slot.
    """
    events = []
    fraud_txn_ids = []
    fraud_accounts = set()
    fraud_customers = set()
    chains_ground_truth = {}
    chain_pool = {}

    consolidation_branch = random.choice(BRANCH_IDS)
    consolidation_customer, consolidation_account = _mint_identity(minter, consolidation_branch, account_type="BUSINESS")
    fraud_accounts.add(consolidation_account.account_number)
    fraud_customers.add(consolidation_customer.customer_id)

    placement_offsets = sorted(random.uniform(0, placement_window_minutes) for _ in range(3))
    inbound_arrival_times = []

    for person_key, branch_id, placement_offset in zip(PERSON_KEYS, BRANCH_IDS, placement_offsets):
        placement_customer, placement_account = _mint_identity(minter, branch_id)
        placement_ts = scenario_start + timedelta(minutes=placement_offset)
        amount = round(random.uniform(placement_min, placement_max), 2)

        placement_event = _make_event(
            branch_id=branch_id, txn_type="CASH_DEPOSIT", channel="BRANCH", amount=amount, ts=placement_ts,
            originator=_party_info(sentinel=CASH_SENTINEL), beneficiary=_party_info(placement_account, placement_customer),
            telemetry=_telemetry_for(placement_customer, "BRANCH"),
            scenario_start=scenario_start, span_hours=span_hours, compress_seconds=compress_seconds,
        )
        events.append(placement_event)
        fraud_txn_ids.append(placement_event["transaction"]["txn_id"])

        chain_accounts = [placement_account]
        chain_customers = [placement_customer]
        for hop_i in range(hops - 1):
            slot_key = (person_key, hop_i)
            is_last_intermediate = hop_i == hops - 2  # this account feeds the final hop into consolidation
            if slot_key in shared_slots:
                cust, acct = shared_slots[slot_key]
            else:
                exclude = (consolidation_branch,) if is_last_intermediate else ()
                branch = _next_branch(chain_accounts[-1].branch_id, *exclude)
                cust, acct = _mint_identity(minter, branch)
            chain_accounts.append(acct)
            chain_customers.append(cust)

        transit_minutes_total = random.uniform(span_hours * 30, span_hours * 55)
        per_hop_minutes = transit_minutes_total / hops

        cur_amount = amount
        cur_ts = placement_ts
        for i in range(hops):
            from_acct, from_cust = chain_accounts[i], chain_customers[i]
            is_final_hop = i == hops - 1
            to_acct = consolidation_account if is_final_hop else chain_accounts[i + 1]
            to_cust = consolidation_customer if is_final_hop else chain_customers[i + 1]

            skim = random.uniform(0.005, 0.02)
            cur_amount = round(cur_amount * (1 - skim), 2)
            hop_delay = per_hop_minutes * random.uniform(0.6, 1.4)
            cur_ts = cur_ts + timedelta(minutes=hop_delay)

            txn_type = random.choice(["WIRE", "ACH"])
            channel = "WIRE_ROOM" if txn_type == "WIRE" else "ONLINE"

            hop_event = _make_event(
                branch_id=from_acct.branch_id, txn_type=txn_type, channel=channel, amount=cur_amount, ts=cur_ts,
                originator=_party_info(from_acct, from_cust), beneficiary=_party_info(to_acct, to_cust),
                telemetry=_telemetry_for(from_cust, channel),
                scenario_start=scenario_start, span_hours=span_hours, compress_seconds=compress_seconds,
            )
            events.append(hop_event)
            fraud_txn_ids.append(hop_event["transaction"]["txn_id"])

        inbound_arrival_times.append(cur_ts)
        chain_pool[person_key] = list(zip(chain_accounts, chain_customers))

        chains_ground_truth[person_key] = {
            "placement_branch": branch_id,
            "placement_account": placement_account.account_number,
            "placement_customer": placement_customer.customer_id,
            "placement_amount": amount,
            "intermediate_accounts": [a.account_number for a in chain_accounts[1:]],
            "final_amount_into_consolidation": cur_amount,
        }
        for acct in chain_accounts:
            fraud_accounts.add(acct.account_number)
        for cust in chain_customers:
            fraud_customers.add(cust.customer_id)

    exit_ts = max(inbound_arrival_times) + timedelta(minutes=random.uniform(5, 30))
    exit_amount = round(sum(c["final_amount_into_consolidation"] for c in chains_ground_truth.values()), 2)
    exit_event = _make_event(
        branch_id=consolidation_branch, txn_type="CASH_WITHDRAWAL", channel="BRANCH", amount=exit_amount, ts=exit_ts,
        originator=_party_info(consolidation_account, consolidation_customer), beneficiary=_party_info(sentinel=CASH_OUT_SENTINEL),
        telemetry=_telemetry_for(consolidation_customer, "BRANCH"),
        scenario_start=scenario_start, span_hours=span_hours, compress_seconds=compress_seconds,
    )
    events.append(exit_event)
    fraud_txn_ids.append(exit_event["transaction"]["txn_id"])

    ring_ground_truth = {
        "ring_id": ring_id,
        "consolidation_branch": consolidation_branch,
        "consolidation_account": consolidation_account.account_number,
        "consolidation_customer": consolidation_customer.customer_id,
        "chains": chains_ground_truth,
        "exit_amount": exit_amount,
        "exit_txn_id": exit_event["transaction"]["txn_id"],
        "fraud_accounts": sorted(fraud_accounts),
        "fraud_customers": sorted(fraud_customers),
        "fraud_txn_ids": fraud_txn_ids,
    }
    return events, ring_ground_truth, chain_pool


def _pick_shared_slot(prev_ring_id: str, prev_pool: dict, this_person: str) -> tuple:
    """Picks one intermediate (account, customer) from the previous ring's
    chains to hand to the new ring's first intermediate slot - preferring one
    whose branch differs from this person's placement branch, so the
    guaranteed-cross-branch property holds at the shared hop too, not just
    everywhere else.
    """
    this_placement_branch = BRANCH_IDS[PERSON_KEYS.index(this_person)]
    candidates = []
    for chain in prev_pool.values():
        for acct, cust in chain[1:]:  # skip index 0 (that chain's own placement account)
            candidates.append((acct, cust))

    preferred = [(a, c) for a, c in candidates if a.branch_id != this_placement_branch]
    acct, cust = random.choice(preferred or candidates)
    return acct, cust


def generate_multi_ring_scenario(
    num_rings: int = 3,
    hops: int = 4,
    placement_min: float = 8000.0,
    placement_max: float = 9800.0,
    placement_window_minutes: float = 40.0,
    span_hours: float = 6.0,
    compress_seconds: float = 180.0,
    seed: Optional[int] = None,
    scenario_start: Optional[datetime] = None,
    num_shared_links: Optional[int] = None,
) -> tuple:
    """num_shared_links caps how many consecutive ring pairs (ring1<->ring2,
    ring2<->ring3, ...) share a mule account - default None means "every
    consecutive pair" (num_rings - 1), the original scenario_500 behavior,
    unchanged for any existing caller. Passing e.g. 1 links only ring1<->ring2
    and leaves every other ring fully independent (zero shared accounts, so
    the known cross-ring leak in graph/queries.py's amount+time chaining rule
    has no shared node to leak through for those rings) - see CLAUDE.md's
    scenario_1500 note on why a demo-safe dataset wants few/no shared links.
    """
    if hops < 2:
        raise ValueError("hops must be >= 2 (need at least one intermediate slot for the shared-account mechanism)")
    if seed is not None:
        random.seed(seed)
        Faker.seed(seed)

    assert placement_max < CTR_THRESHOLD_USD, "placement amounts must stay under the CTR threshold"

    if num_shared_links is None:
        num_shared_links = num_rings - 1
    if not (0 <= num_shared_links <= num_rings - 1):
        raise ValueError(f"num_shared_links must be between 0 and num_rings - 1 ({num_rings - 1}), got {num_shared_links}")

    minter = _IdMinter(seed_account=_MULTI_RING_ACCOUNT_START, seed_customer=_MULTI_RING_CUSTOMER_START)
    scenario_start = scenario_start if scenario_start is not None else datetime.utcnow()

    all_events = []
    rings_ground_truth = {}
    shared_accounts_log = []
    ring_pools = {}

    for ring_idx in range(num_rings):
        ring_id = f"ring{ring_idx + 1}"
        shared_slots = {}

        if 0 < ring_idx <= num_shared_links:
            prev_ring_id = f"ring{ring_idx}"
            this_person = random.choice(PERSON_KEYS)
            shared_acct, shared_cust = _pick_shared_slot(prev_ring_id, ring_pools[prev_ring_id], this_person)
            shared_slots[(this_person, 0)] = (shared_cust, shared_acct)
            shared_accounts_log.append({
                "account": shared_acct.account_number,
                "shared_between": [prev_ring_id, ring_id],
            })

        events, ring_gt, chain_pool = _build_ring(
            minter, ring_id, hops, placement_min, placement_max, placement_window_minutes,
            span_hours, compress_seconds, scenario_start, shared_slots,
        )
        all_events.extend(events)
        rings_ground_truth[ring_id] = ring_gt
        ring_pools[ring_id] = chain_pool

    all_events.sort(key=lambda e: e["transaction"]["timestamp"])

    all_fraud_accounts = sorted(set().union(*(set(r["fraud_accounts"]) for r in rings_ground_truth.values())))
    all_fraud_txn_ids = [txn_id for r in rings_ground_truth.values() for txn_id in r["fraud_txn_ids"]]

    ground_truth = {
        "scenario": "multi_ring_layering",
        "num_rings": num_rings,
        "num_shared_links": num_shared_links,
        "hops_per_ring": hops,
        "seed": seed,
        "scenario_start": _fmt_ts(scenario_start),
        "span_hours": span_hours,
        "compress_seconds": compress_seconds,
        "rings": rings_ground_truth,
        "shared_accounts": shared_accounts_log,
        "all_fraud_accounts": all_fraud_accounts,
        "all_fraud_txn_ids": all_fraud_txn_ids,
    }
    return all_events, ground_truth


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate the multi-ring Case 1 layering scenario (several concurrent, partially-linked laundering rings)")
    parser.add_argument("--num-rings", type=int, default=3)
    parser.add_argument("--num-shared-links", type=int, default=None, help="how many consecutive ring pairs share a mule account (default: every pair, num_rings - 1)")
    parser.add_argument("--hops", type=int, default=4, help="chain length per ring")
    parser.add_argument("--placement-min", type=float, default=8000.0)
    parser.add_argument("--placement-max", type=float, default=9800.0)
    parser.add_argument("--placement-window-minutes", type=float, default=40.0)
    parser.add_argument("--span-hours", type=float, default=6.0)
    parser.add_argument("--compress-seconds", type=float, default=180.0)
    parser.add_argument("--seed", type=int, default=500)
    parser.add_argument("--scenario-start", type=str, default=None, help="ISO datetime to anchor the fraud timeline to, instead of the real current time (e.g. 2024-06-06T02:00:00) - keeps day_of_week/hour_of_day away from whatever the live model's training calibration is not")
    parser.add_argument("--out-events", type=str, default="data/scenario_500/multi_ring_events.json")
    parser.add_argument("--out-ground-truth", type=str, default="data/scenario_500/multi_ring_ground_truth.json")
    args = parser.parse_args()

    scenario_start = datetime.fromisoformat(args.scenario_start) if args.scenario_start else None
    events, ground_truth = generate_multi_ring_scenario(
        num_rings=args.num_rings, hops=args.hops, num_shared_links=args.num_shared_links,
        placement_min=args.placement_min, placement_max=args.placement_max,
        placement_window_minutes=args.placement_window_minutes,
        span_hours=args.span_hours, compress_seconds=args.compress_seconds, seed=args.seed,
        scenario_start=scenario_start,
    )

    with open(args.out_events, "w") as f:
        json.dump(events, f, indent=2)
    with open(args.out_ground_truth, "w") as f:
        json.dump(ground_truth, f, indent=2)

    print(f"Generated {len(events)} fraud events across {args.num_rings} rings ({args.hops} hops/chain) -> {args.out_events}")
    print(f"Ground truth -> {args.out_ground_truth}")
    print(f"Total fraud accounts: {len(ground_truth['all_fraud_accounts'])}")
    print(f"Shared accounts linking rings: {len(ground_truth['shared_accounts'])}")
    for link in ground_truth["shared_accounts"]:
        print(f"  - {link['account']} shared between {link['shared_between']}")
