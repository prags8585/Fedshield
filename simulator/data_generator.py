"""Normal background transaction traffic - the noise the Case 1 fraud
scenario has to hide inside. Produces KafkaTxnEvent-shaped dicts (see
shared/schemas.py) using a customer/account population from customers.py.
"""
import argparse
import json
import random
import uuid
from datetime import datetime, timedelta
from typing import Optional

from faker import Faker

from simulator.customers import Account, Customer, Population, generate_customer_population

fake = Faker("en_US")

CASH_SENTINEL = "CASH"
CASH_OUT_SENTINEL = "CASH_OUT"
MERCHANT_SENTINEL = "MERCHANT"

# (txn_type, weight)
TXN_TYPE_WEIGHTS = [
    ("DEBIT_CARD", 35),
    ("ACH", 20),
    ("ZELLE", 15),
    ("CASH_WITHDRAWAL", 10),
    ("CASH_DEPOSIT", 10),
    ("CHECK", 7),
    ("WIRE", 3),
]

TXN_TYPE_CHANNELS = {
    "CASH_DEPOSIT": ["BRANCH", "ATM"],
    "CASH_WITHDRAWAL": ["BRANCH", "ATM"],
    "WIRE": ["WIRE_ROOM", "ONLINE"],
    "ACH": ["ONLINE"],
    "ZELLE": ["MOBILE", "ONLINE"],
    "CHECK": ["BRANCH", "MOBILE"],
    "DEBIT_CARD": ["ONLINE", "MOBILE"],
}

# Most background amounts stay well below the CTR threshold; a small tail
# goes higher (legitimate large purchases/wires) to create realistic
# near-threshold noise without an actual structuring pattern behind it.
AMOUNT_BUCKETS = [
    (5, 200, 0.55),
    (200, 3000, 0.35),
    (3000, 9999, 0.10),
]

DEVICE_TYPES_BY_CHANNEL = {
    "BRANCH": ["BRANCH_TELLER"],
    "ATM": ["ATM_KIOSK"],
    "ONLINE": ["WEB"],
    "MOBILE": ["MOBILE_IOS", "MOBILE_ANDROID"],
    "WIRE_ROOM": ["BRANCH_TELLER"],
}


def _weighted_choice(weighted: list) -> str:
    labels, weights = zip(*weighted)
    return random.choices(labels, weights=weights, k=1)[0]


def _random_amount() -> float:
    lo, hi, _ = random.choices(AMOUNT_BUCKETS, weights=[b[2] for b in AMOUNT_BUCKETS], k=1)[0]
    return round(random.uniform(lo, hi), 2)


def _party_info(account: Optional[Account], customer: Optional[Customer], sentinel: Optional[str] = None) -> dict:
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
    device_type = random.choice(DEVICE_TYPES_BY_CHANNEL.get(channel, ["WEB"]))
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


def _account_and_customer(population: Population, account: Account) -> tuple:
    customer = next(c for c in population.customers if c.customer_id == account.customer_id)
    return account, customer


def _random_timestamp(start: datetime, end: datetime) -> datetime:
    delta_seconds = int((end - start).total_seconds())
    return start + timedelta(seconds=random.randint(0, max(delta_seconds, 1)))


def generate_transaction(population: Population, account: Account, ts: datetime) -> dict:
    _, customer = _account_and_customer(population, account)
    txn_type = _weighted_choice(TXN_TYPE_WEIGHTS)
    channel = random.choice(TXN_TYPE_CHANNELS[txn_type])
    amount = _random_amount()

    if txn_type == "CASH_DEPOSIT":
        originator, beneficiary = _party_info(None, None, sentinel=CASH_SENTINEL), _party_info(account, customer)
    elif txn_type == "CASH_WITHDRAWAL":
        originator, beneficiary = _party_info(account, customer), _party_info(None, None, sentinel=CASH_OUT_SENTINEL)
    elif txn_type == "DEBIT_CARD":
        originator, beneficiary = _party_info(account, customer), _party_info(None, None, sentinel=MERCHANT_SENTINEL)
    else:
        # WIRE / ACH / ZELLE / CHECK -> transfer to another account in the population
        other = random.choice([a for a in population.accounts if a.account_number != account.account_number])
        _, other_customer = _account_and_customer(population, other)
        originator, beneficiary = _party_info(account, customer), _party_info(other, other_customer)

    kafka_ts = ts + timedelta(seconds=random.uniform(0.1, 4.0))

    return {
        "event_id": f"evt_{uuid.uuid4()}",
        "kafka_timestamp": kafka_ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "branch_id": account.branch_id,
        "transaction": {
            "txn_id": f"tx_{uuid.uuid4().hex[:16]}",
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "amount": amount,
            "currency": "USD",
            "txn_type": txn_type,
            "channel": channel,
            "status": "POSTED",
        },
        "originator": originator,
        "beneficiary": beneficiary,
        "telemetry": _telemetry_for(customer, channel),
    }


def generate_background_transactions(
    population: Population, count: int, days: int = 3, seed: Optional[int] = None, end: Optional[datetime] = None
) -> list:
    """end defaults to datetime.utcnow() (unchanged behavior for every existing
    caller, including train_model.py's own background rows). Passing an
    explicit end anchors the fabricated day_of_week/hour_of_day distribution
    to a fixed historical window instead of "whatever real day this happens
    to run on" - see CLAUDE.md's scenario_1500 note on why that default
    silently drifts the day_of_week feature away from whatever mean/std the
    live model was last trained with, inflating false positives on freshly
    generated data for no fraud-relevant reason.
    """
    if seed is not None:
        random.seed(seed)
        Faker.seed(seed)

    end = end if end is not None else datetime.utcnow()
    start = end - timedelta(days=days)

    events = []
    for _ in range(count):
        account = random.choice(population.accounts)
        ts = _random_timestamp(start, end)
        events.append(generate_transaction(population, account, ts))

    events.sort(key=lambda e: e["transaction"]["timestamp"])
    return events


def generate_fanin_noise(
    population: Population,
    num_shared_accounts: int = 3,
    senders_per_account: int = 4,
    days: int = 3,
    seed: Optional[int] = None,
) -> list:
    """Legitimate multi-source convergence: a handful of accounts (standing
    in for a shared landlord/payroll account) each receive transfers from
    several other accounts. Deliberately does NOT amount-match or time-match
    across senders - real rent/payroll fan-in has no reason to - unlike the
    Case 1 fraud pattern, where hop amounts stay within a tight ratio and
    land within a few hours of each other. Exists so check_convergence's
    time+amount rule has something real to reject: without this, nothing in
    the background data tests whether "N sources -> 1 account" alone would
    wrongly trigger a false convergence.
    """
    if seed is not None:
        random.seed(seed)
        Faker.seed(seed)

    end = datetime.utcnow()
    start = end - timedelta(days=days)

    shared_accounts = random.sample(population.accounts, num_shared_accounts)
    events = []
    for shared in shared_accounts:
        _, shared_customer = _account_and_customer(population, shared)
        candidates = [a for a in population.accounts if a.account_number != shared.account_number]
        senders = random.sample(candidates, min(senders_per_account, len(candidates)))
        for sender in senders:
            _, sender_customer = _account_and_customer(population, sender)
            ts = _random_timestamp(start, end)
            txn_type = random.choice(["ACH", "CHECK", "ZELLE"])
            channel = random.choice(TXN_TYPE_CHANNELS[txn_type])
            amount = round(random.uniform(600, 2500), 2)  # rent-ish; not amount-matched across senders on purpose

            originator, beneficiary = _party_info(sender, sender_customer), _party_info(shared, shared_customer)
            kafka_ts = ts + timedelta(seconds=random.uniform(0.1, 4.0))
            events.append({
                "event_id": f"evt_{uuid.uuid4()}",
                "kafka_timestamp": kafka_ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "branch_id": sender.branch_id,
                "transaction": {
                    "txn_id": f"tx_{uuid.uuid4().hex[:16]}",
                    "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                    "amount": amount,
                    "currency": "USD",
                    "txn_type": txn_type,
                    "channel": channel,
                    "status": "POSTED",
                },
                "originator": originator,
                "beneficiary": beneficiary,
                "telemetry": _telemetry_for(sender_customer, channel),
            })

    events.sort(key=lambda e: e["transaction"]["timestamp"])
    return events


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate normal background transaction traffic")
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--per-branch", type=int, default=100, help="customer population size per branch")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=str, default="data/background_transactions.json")
    parser.add_argument("--fanin-accounts", type=int, default=0, help="number of shared landlord/payroll-style accounts to inject fan-in noise onto (0 = off)")
    parser.add_argument("--fanin-senders", type=int, default=4, help="senders per fan-in account")
    parser.add_argument("--end-date", type=str, default=None, help="ISO datetime to anchor the 'last --days days' window to, instead of the real current time (e.g. 2024-06-07T00:00:00) - see generate_background_transactions' docstring")
    args = parser.parse_args()

    end_date = datetime.fromisoformat(args.end_date) if args.end_date else None
    pop = generate_customer_population(per_branch=args.per_branch, seed=args.seed)
    events = generate_background_transactions(pop, count=args.count, days=args.days, seed=args.seed, end=end_date)

    if args.fanin_accounts > 0:
        fanin_events = generate_fanin_noise(
            pop, num_shared_accounts=args.fanin_accounts, senders_per_account=args.fanin_senders,
            days=args.days, seed=args.seed,
        )
        events = sorted(events + fanin_events, key=lambda e: e["transaction"]["timestamp"])
        print(f"Injected {len(fanin_events)} fan-in noise events across {args.fanin_accounts} shared accounts")

    with open(args.out, "w") as f:
        json.dump(events, f, indent=2)

    print(f"Generated {len(events)} background transactions -> {args.out}")
