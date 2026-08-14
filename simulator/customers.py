"""Synthetic customer/account population for a US bank with 3 branches.

This is the "world state" other simulator scripts draw real parties from -
not a payload that crosses a service boundary, so it lives here rather than
in shared/schemas.py. Faker uses the en_US locale throughout (see CLAUDE.md's
US-bank design decision).
"""
import argparse
import json
import random
from dataclasses import asdict, dataclass, field
from typing import Optional

from faker import Faker

from shared.config import BRANCH_IDS

fake = Faker("en_US")

ACCOUNT_TYPES = ["CHECKING", "SAVINGS", "MONEY_MARKET", "BUSINESS"]

# One fixed, valid-checksum ABA routing number per branch. Digits are a
# fictitious "FedShield Bank" prefix, not a real institution's number -
# checksum computed so it validates like a real ABA would.
_ABA_WEIGHTS = [3, 7, 1, 3, 7, 1, 3, 7]
_ABA_PREFIXES = {"loc1": "07100001", "loc2": "07100002", "loc3": "07100003"}


def _aba_check_digit(prefix8: str) -> str:
    total = sum(w * int(d) for w, d in zip(_ABA_WEIGHTS, prefix8))
    return str((10 - (total % 10)) % 10)


BRANCH_ROUTING_NUMBERS = {
    branch_id: prefix + _aba_check_digit(prefix) for branch_id, prefix in _ABA_PREFIXES.items()
}


@dataclass
class Customer:
    customer_id: str
    ssn_last4: str  # fake, last-4-only - never a full SSN, never persisted beyond this
    full_name: str  # PII pre-masking - stripped by masking.py in Session 3
    home_city: str
    home_state: str
    created_date: str  # ISO date the customer relationship began


@dataclass
class Account:
    account_number: str
    routing_number: str
    branch_id: str
    customer_id: str
    account_type: str
    avg_monthly_balance: float
    account_age_days: int


@dataclass
class Population:
    customers: list = field(default_factory=list)
    accounts: list = field(default_factory=list)

    def accounts_by_branch(self, branch_id: str) -> list:
        return [a for a in self.accounts if a.branch_id == branch_id]


def generate_customer_population(per_branch: int = 50, seed: Optional[int] = None) -> Population:
    """Each customer gets exactly one account, at one branch - matches the
    Case 1 scenario's assumption (p1/p2/p3 each have one account at a
    different branch) and keeps background noise simple.
    """
    if seed is not None:
        random.seed(seed)
        Faker.seed(seed)

    population = Population()
    next_account_number = 4_000_000_001

    for branch_id in BRANCH_IDS:
        for i in range(per_branch):
            customer_id = f"CUST-{10_000_000 + len(population.customers):08d}"
            customer = Customer(
                customer_id=customer_id,
                ssn_last4=f"{random.randint(0, 9999):04d}",
                full_name=fake.name(),
                home_city=fake.city(),
                home_state=fake.state_abbr(),
                created_date=fake.date_between(start_date="-10y", end_date="-30d").isoformat(),
            )
            account = Account(
                account_number=str(next_account_number),
                routing_number=BRANCH_ROUTING_NUMBERS[branch_id],
                branch_id=branch_id,
                customer_id=customer_id,
                account_type=random.choice(ACCOUNT_TYPES),
                avg_monthly_balance=round(random.uniform(500, 25_000), 2),
                account_age_days=random.randint(30, 3650),
            )
            next_account_number += 1
            population.customers.append(customer)
            population.accounts.append(account)

    return population


def population_to_dict(population: Population) -> dict:
    return {
        "customers": [asdict(c) for c in population.customers],
        "accounts": [asdict(a) for a in population.accounts],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate synthetic customer/account population")
    parser.add_argument("--per-branch", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=str, default="data/customers.json")
    args = parser.parse_args()

    pop = generate_customer_population(per_branch=args.per_branch, seed=args.seed)
    with open(args.out, "w") as f:
        json.dump(population_to_dict(pop), f, indent=2)

    print(f"Generated {len(pop.customers)} customers / {len(pop.accounts)} accounts -> {args.out}")
    print(f"Routing numbers: {BRANCH_ROUTING_NUMBERS}")
