"""Case 1 fraud scenario: placement -> layering -> integration (deposit
tracing / smurfing-into-layering).

Three unrelated individuals (p1, p2, p3) each deposit cash just under the
$10,000 CTR threshold at their own branch, within a short window. Each
placement's funds then move through a chain of intermediate ("mule")
accounts - length configurable via --hops, not fixed - and all three chains
converge on one shared consolidation account, which makes a single large
cash withdrawal (the integration/exit step).

Ground truth (which accounts/txn_ids are actually part of the fraud) is
written to a separate file and is NEVER exposed to any model or agent - it
exists only for the evaluation harness (Session 9).
"""
import argparse
import json
import random
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Optional

from faker import Faker

from shared.config import BRANCH_IDS, CTR_THRESHOLD_USD
from simulator.customers import Account, Customer, BRANCH_ROUTING_NUMBERS

fake = Faker("en_US")

CASH_SENTINEL = "CASH"
CASH_OUT_SENTINEL = "CASH_OUT"
MERCHANT_SENTINEL = "MERCHANT"

# Reserved id ranges so scenario-injected identities never collide with the
# background population minted by customers.py (which starts at 4e9/1e7).
_SCENARIO_ACCOUNT_START = 9_000_000_001
_SCENARIO_CUSTOMER_START = 90_000_000


class _IdMinter:
    def __init__(self, seed_account=_SCENARIO_ACCOUNT_START, seed_customer=_SCENARIO_CUSTOMER_START):
        self._next_account = seed_account
        self._next_customer = seed_customer

    def new_account_number(self) -> str:
        n = str(self._next_account)
        self._next_account += 1
        return n

    def new_customer_id(self) -> str:
        n = f"CUST-{self._next_customer:08d}"
        self._next_customer += 1
        return n


def _mint_identity(minter: _IdMinter, branch_id: str, account_type: str = "CHECKING") -> tuple:
    customer = Customer(
        customer_id=minter.new_customer_id(),
        ssn_last4=f"{random.randint(0, 9999):04d}",
        full_name=fake.name(),
        home_city=fake.city(),
        home_state=fake.state_abbr(),
        created_date=fake.date_between(start_date="-10y", end_date="-30d").isoformat(),
    )
    account = Account(
        account_number=minter.new_account_number(),
        routing_number=BRANCH_ROUTING_NUMBERS[branch_id],
        branch_id=branch_id,
        customer_id=customer.customer_id,
        account_type=account_type,
        avg_monthly_balance=round(random.uniform(500, 5_000), 2),
        account_age_days=random.randint(30, 3650),
    )
    return customer, account


def _party_info(account: Optional[Account] = None, customer: Optional[Customer] = None, sentinel: Optional[str] = None) -> dict:
    if sentinel is not None:
        return {"account_number": sentinel, "routing_number": None, "account_type": None, "customer_id": None, "customer_name": None}
    return {
        "account_number": account.account_number,
        "routing_number": account.routing_number,
        "account_type": account.account_type,
        "customer_id": account.customer_id,
        "customer_name": customer.full_name,
    }


def _telemetry_for(customer: Customer, channel: str) -> dict:
    device_type = "BRANCH_TELLER" if channel in ("BRANCH", "WIRE_ROOM") else ("ATM_KIOSK" if channel == "ATM" else "WEB")
    lat, lng = fake.local_latlng(country_code="US")[:2]
    return {
        "ip_address": fake.ipv4_public(),
        "device_id": f"dv_{uuid.uuid4().hex[:12]}",
        "device_type": device_type,
        "location": {
            "city": customer.home_city,
            "state": customer.home_state,
            "country": "US",
            "latitude": float(lat),
            "longitude": float(lng),
        },
    }


def _fmt_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _make_event(
    branch_id: str,
    txn_type: str,
    channel: str,
    amount: float,
    ts: datetime,
    originator: dict,
    beneficiary: dict,
    telemetry: dict,
    scenario_start: datetime,
    span_hours: float,
    compress_seconds: float,
) -> dict:
    kafka_ts = ts + timedelta(seconds=random.uniform(0.1, 4.0))
    offset_fraction = (ts - scenario_start).total_seconds() / max(span_hours * 3600, 1)
    return {
        "event_id": f"evt_{uuid.uuid4()}",
        "kafka_timestamp": _fmt_ts(kafka_ts),
        "branch_id": branch_id,
        "transaction": {
            "txn_id": f"tx_{uuid.uuid4().hex[:16]}",
            "timestamp": _fmt_ts(ts),
            "amount": amount,
            "currency": "USD",
            "txn_type": txn_type,
            "channel": channel,
            "status": "POSTED",
        },
        "originator": originator,
        "beneficiary": beneficiary,
        "telemetry": telemetry,
        # Session 3's producer uses this to compress a `span_hours`-wide fabricated
        # timeline into `compress_seconds` of real delivery time - NOT part of the
        # Kafka message schema itself, stripped before publishing.
        "_emit_offset_seconds": max(0.0, min(offset_fraction, 1.0)) * compress_seconds,
    }


def _mule_noise_events(
    minter: _IdMinter,
    account: Account,
    customer: Customer,
    scenario_start: datetime,
    span_hours: float,
    compress_seconds: float,
) -> list:
    """Unrelated innocuous activity on a fraud-path account itself (mule,
    placement, or consolidation): a small purchase, a paycheck-style credit,
    a rent-style debit. None of these amount- or time-chain against any real
    hop, so get_outgoing_txns on these nodes returns a mix of real and
    irrelevant edges - proving check_convergence's ratio+window filter
    actually discriminates, instead of only ever seeing the one real hop
    because nothing else was ever on the account.
    """
    events = []

    purchase_ts = scenario_start + timedelta(hours=random.uniform(0, span_hours))
    events.append(_make_event(
        branch_id=account.branch_id, txn_type="DEBIT_CARD", channel=random.choice(["ONLINE", "MOBILE"]),
        amount=round(random.uniform(8, 120), 2), ts=purchase_ts,
        originator=_party_info(account, customer), beneficiary=_party_info(sentinel=MERCHANT_SENTINEL),
        telemetry=_telemetry_for(customer, "ONLINE"),
        scenario_start=scenario_start, span_hours=span_hours, compress_seconds=compress_seconds,
    ))

    employer_cust, employer_acct = _mint_identity(minter, account.branch_id)
    paycheck_ts = scenario_start + timedelta(hours=random.uniform(0, span_hours))
    events.append(_make_event(
        branch_id=employer_acct.branch_id, txn_type="ACH", channel="ONLINE",
        amount=round(random.uniform(900, 2800), 2), ts=paycheck_ts,
        originator=_party_info(employer_acct, employer_cust), beneficiary=_party_info(account, customer),
        telemetry=_telemetry_for(employer_cust, "ONLINE"),
        scenario_start=scenario_start, span_hours=span_hours, compress_seconds=compress_seconds,
    ))

    landlord_cust, landlord_acct = _mint_identity(minter, account.branch_id)
    rent_ts = scenario_start + timedelta(hours=random.uniform(0, span_hours))
    events.append(_make_event(
        branch_id=account.branch_id, txn_type="ACH", channel="ONLINE",
        amount=round(random.uniform(700, 2200), 2), ts=rent_ts,
        originator=_party_info(account, customer), beneficiary=_party_info(landlord_acct, landlord_cust),
        telemetry=_telemetry_for(customer, "ONLINE"),
        scenario_start=scenario_start, span_hours=span_hours, compress_seconds=compress_seconds,
    ))

    return events


def generate_layering_scenario(
    hops: int = 4,
    placement_min: float = 8000.0,
    placement_max: float = 9800.0,
    placement_window_minutes: float = 40.0,
    span_hours: float = 6.0,
    compress_seconds: float = 60.0,
    seed: Optional[int] = None,
    scenario_start: Optional[datetime] = None,
    inject_mule_noise: bool = False,
) -> tuple:
    """Returns (events, ground_truth). events are time-ordered, schema-valid
    KafkaTxnEvent-shaped dicts (plus a stripped `_emit_offset_seconds` hint).

    scenario_start defaults to "now" (right for a live demo, where the whole
    point is compressing the fabricated timeline into a watchable window
    starting immediately). Training data generation (train_model.py) passes
    an explicit, randomized scenario_start instead - otherwise every scenario
    generated in one training run shares almost the same real-world "now",
    so every fraud transaction lands in the same narrow hour-of-day band by
    pure coincidence of when the script happened to run, which the model
    would then (wrongly) learn as a fraud signal.
    """
    if hops < 1:
        raise ValueError("hops must be >= 1")
    if seed is not None:
        random.seed(seed)
        Faker.seed(seed)

    assert placement_max < CTR_THRESHOLD_USD, "placement amounts must stay under the CTR threshold"

    minter = _IdMinter()
    scenario_start = scenario_start if scenario_start is not None else datetime.utcnow()

    consolidation_branch = random.choice(BRANCH_IDS)
    consolidation_customer, consolidation_account = _mint_identity(minter, consolidation_branch, account_type="BUSINESS")

    events = []
    chains_ground_truth = {}
    fraud_accounts = {consolidation_account.account_number}
    fraud_customers = {consolidation_customer.customer_id}
    fraud_txn_ids = []

    placement_offsets = sorted(random.uniform(0, placement_window_minutes) for _ in range(3))

    inbound_arrival_times = []

    for person_key, branch_id, placement_offset in zip(("p1", "p2", "p3"), BRANCH_IDS, placement_offsets):
        placement_customer, placement_account = _mint_identity(minter, branch_id)
        placement_ts = scenario_start + timedelta(minutes=placement_offset)
        amount = round(random.uniform(placement_min, placement_max), 2)

        placement_event = _make_event(
            branch_id=branch_id,
            txn_type="CASH_DEPOSIT",
            channel="BRANCH",
            amount=amount,
            ts=placement_ts,
            originator=_party_info(sentinel=CASH_SENTINEL),
            beneficiary=_party_info(placement_account, placement_customer),
            telemetry=_telemetry_for(placement_customer, "BRANCH"),
            scenario_start=scenario_start,
            span_hours=span_hours,
            compress_seconds=compress_seconds,
        )
        events.append(placement_event)
        fraud_txn_ids.append(placement_event["transaction"]["txn_id"])

        # Build the interior of this chain: hops-1 intermediate accounts,
        # then the final hop lands on the shared consolidation account.
        chain_accounts = [placement_account]
        chain_customers = [placement_customer]
        for _ in range(hops - 1):
            branch = random.choice(BRANCH_IDS)
            cust, acct = _mint_identity(minter, branch)
            chain_accounts.append(acct)
            chain_customers.append(cust)

        transit_minutes_total = random.uniform(span_hours * 30, span_hours * 55)  # spread across most of the window
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
                branch_id=from_acct.branch_id,
                txn_type=txn_type,
                channel=channel,
                amount=cur_amount,
                ts=cur_ts,
                originator=_party_info(from_acct, from_cust),
                beneficiary=_party_info(to_acct, to_cust),
                telemetry=_telemetry_for(from_cust, channel),
                scenario_start=scenario_start,
                span_hours=span_hours,
                compress_seconds=compress_seconds,
            )
            events.append(hop_event)
            fraud_txn_ids.append(hop_event["transaction"]["txn_id"])

        inbound_arrival_times.append(cur_ts)

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

        if inject_mule_noise:
            for acct, cust in zip(chain_accounts, chain_customers):
                events.extend(_mule_noise_events(minter, acct, cust, scenario_start, span_hours, compress_seconds))

    if inject_mule_noise:
        events.extend(_mule_noise_events(minter, consolidation_account, consolidation_customer, scenario_start, span_hours, compress_seconds))

    # Integration: consolidation account cashes out shortly after the last chain arrives.
    exit_ts = max(inbound_arrival_times) + timedelta(minutes=random.uniform(5, 30))
    exit_amount = round(sum(c["final_amount_into_consolidation"] for c in chains_ground_truth.values()), 2)

    exit_event = _make_event(
        branch_id=consolidation_branch,
        txn_type="CASH_WITHDRAWAL",
        channel="BRANCH",
        amount=exit_amount,
        ts=exit_ts,
        originator=_party_info(consolidation_account, consolidation_customer),
        beneficiary=_party_info(sentinel=CASH_OUT_SENTINEL),
        telemetry=_telemetry_for(consolidation_customer, "BRANCH"),
        scenario_start=scenario_start,
        span_hours=span_hours,
        compress_seconds=compress_seconds,
    )
    events.append(exit_event)
    fraud_txn_ids.append(exit_event["transaction"]["txn_id"])

    events.sort(key=lambda e: e["transaction"]["timestamp"])

    ground_truth = {
        "scenario": "layering",
        "hops": hops,
        "seed": seed,
        "scenario_start": _fmt_ts(scenario_start),
        "span_hours": span_hours,
        "compress_seconds": compress_seconds,
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

    return events, ground_truth


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate the Case 1 layering fraud scenario")
    parser.add_argument("--hops", type=int, default=4, help="chain length from placement to consolidation")
    parser.add_argument("--placement-min", type=float, default=8000.0)
    parser.add_argument("--placement-max", type=float, default=9800.0)
    parser.add_argument("--placement-window-minutes", type=float, default=40.0)
    parser.add_argument("--span-hours", type=float, default=6.0)
    parser.add_argument("--compress-seconds", type=float, default=60.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--scenario-start", type=str, default=None, help="ISO datetime to anchor the fraud timeline to, instead of the real current time (e.g. 2025-01-01T02:00:00) - use this for a STATIC demo data file meant to be reused across many future days, so its day_of_week/hour_of_day distribution doesn't drift away from whatever the live model happens to be calibrated to on a given day. Leave unset for a genuine live demo, where starting 'now' is correct.")
    parser.add_argument("--out-events", type=str, default=None)
    parser.add_argument("--out-ground-truth", type=str, default=None)
    parser.add_argument("--inject-mule-noise", action="store_true", help="add unrelated paycheck/rent/purchase activity on the fraud-path accounts themselves")
    args = parser.parse_args()

    scenario_start = datetime.fromisoformat(args.scenario_start) if args.scenario_start else None
    events, ground_truth = generate_layering_scenario(
        hops=args.hops,
        placement_min=args.placement_min,
        placement_max=args.placement_max,
        placement_window_minutes=args.placement_window_minutes,
        span_hours=args.span_hours,
        compress_seconds=args.compress_seconds,
        seed=args.seed,
        scenario_start=scenario_start,
        inject_mule_noise=args.inject_mule_noise,
    )

    out_events = args.out_events or f"data/layering_hops{args.hops}_events.json"
    out_gt = args.out_ground_truth or f"data/layering_hops{args.hops}_ground_truth.json"

    with open(out_events, "w") as f:
        json.dump(events, f, indent=2)
    with open(out_gt, "w") as f:
        json.dump(ground_truth, f, indent=2)

    print(f"Generated {len(events)} events ({args.hops} hops/chain) -> {out_events}")
    print(f"Ground truth -> {out_gt}")
    print(f"Fraud accounts: {len(ground_truth['fraud_accounts'])}, exit amount: ${ground_truth['exit_amount']:,.2f}")
