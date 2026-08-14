"""Pydantic models for every payload shape crossing a service boundary:
Kafka messages, Neo4j nodes/edges, and Redis whiteboard values.
"""
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TxnType(str, Enum):
    CASH_DEPOSIT = "CASH_DEPOSIT"
    CASH_WITHDRAWAL = "CASH_WITHDRAWAL"
    WIRE = "WIRE"
    ACH = "ACH"
    ZELLE = "ZELLE"
    CHECK = "CHECK"
    DEBIT_CARD = "DEBIT_CARD"


class Channel(str, Enum):
    BRANCH = "BRANCH"
    ATM = "ATM"
    ONLINE = "ONLINE"
    MOBILE = "MOBILE"
    WIRE_ROOM = "WIRE_ROOM"


class TxnStatus(str, Enum):
    POSTED = "POSTED"
    PENDING = "PENDING"


class AccountType(str, Enum):
    CHECKING = "CHECKING"
    SAVINGS = "SAVINGS"
    MONEY_MARKET = "MONEY_MARKET"
    BUSINESS = "BUSINESS"


class DeviceType(str, Enum):
    MOBILE_IOS = "MOBILE_IOS"
    MOBILE_ANDROID = "MOBILE_ANDROID"
    WEB = "WEB"
    ATM_KIOSK = "ATM_KIOSK"
    BRANCH_TELLER = "BRANCH_TELLER"


class Location(BaseModel):
    city: str
    state: str
    country: str = "US"
    latitude: float
    longitude: float


class Telemetry(BaseModel):
    """Device/network signal. Generated for realism and reserved for the
    deferred Case 2.2 (same-device/IP correlation across branches). NOT
    consumed by the Case 1 structuring/graph-tracing pipeline - inert data.
    """

    ip_address: str
    device_id: str
    device_type: DeviceType
    location: Location


class PartyInfo(BaseModel):
    """One side (originator or beneficiary) of a transaction. account_number
    is the literal sentinel "CASH"/"CASH_OUT" for deposit/withdrawal legs -
    in that case all other fields are None.
    """

    account_number: str
    routing_number: Optional[str] = None
    account_type: Optional[AccountType] = None
    customer_id: Optional[str] = None  # PII pre-masking - stripped by masking.py in Session 3
    customer_name: Optional[str] = None  # PII pre-masking - stripped by masking.py in Session 3


class TxnDetail(BaseModel):
    txn_id: str
    timestamp: str  # ISO 8601
    amount: float
    currency: str = "USD"
    txn_type: TxnType
    channel: Channel
    status: TxnStatus = TxnStatus.POSTED


# --- Kafka message: nested envelope (event metadata + transaction + originator + beneficiary + telemetry) ---
class KafkaTxnEvent(BaseModel):
    event_id: str
    kafka_timestamp: str  # ISO 8601, ingestion time (distinct from transaction.timestamp)
    branch_id: str
    transaction: TxnDetail
    originator: PartyInfo
    beneficiary: PartyInfo
    telemetry: Telemetry


# --- Neo4j: (:Account {token_id, flagged, flagged_at}) ---
class AccountNode(BaseModel):
    token_id: str
    flagged: bool = False
    flagged_at: Optional[str] = None


# --- Neo4j: -[:TRANSACTED {txn_id, amount, ts, channel}]-> ---
class TransactedEdge(BaseModel):
    txn_id: str
    amount: float
    ts: str
    channel: Channel


# --- ML features fed to the PyTorch model, per transaction ---
class TxnFeatures(BaseModel):
    amount_ratio_to_threshold: float
    is_cash: bool
    hour_of_day: int = Field(ge=0, le=23)
    day_of_week: int = Field(ge=0, le=6)
    velocity_10min: int
    account_age_days: int
    is_transfer_out: bool


# --- Redis: score:{branch}:{token_id}:{txn_id} ---
class ScoreRecord(BaseModel):
    branch_id: str
    token_id: str
    txn_id: str
    score: float
    features: TxnFeatures
    timestamp: str


# --- Redis: evidence:{token_id} ---
class EvidenceHop(BaseModel):
    from_token: str
    to_token: str
    txn_id: str
    amount: float
    ts: str
    channel: Channel


class Evidence(BaseModel):
    token_id: str
    path: list[EvidenceHop]
    convergence_node: Optional[str] = None
    stop_reason: str  # convergence_found | dead_end | cycle | time_window_exceeded | safety_ceiling


# --- Redis: verdicts:{token_id} ---
class Verdict(BaseModel):
    token_id: str
    verdict: str  # GUILTY | NOT_GUILTY
    confidence: float
    rationale: str
    prosecutor_argument: Optional[str] = None
    defense_argument: Optional[str] = None


# --- Redis: reports:{token_id} ---
class Report(BaseModel):
    token_id: str
    body: str  # ends with the privacy attestation line
    status: str = "PENDING_REVIEW"  # PENDING_REVIEW | APPROVED | REJECTED


# --- Redis: fl_status ---
class FLStatus(BaseModel):
    round_num: int
    auc: float
    timestamp: str


# --- Redis: labels:{branch} (pending retrain buffer entry) ---
class Label(BaseModel):
    token_id: str
    features: TxnFeatures
    label: str  # "fraud" | "legit"
    source: str  # "agent_verified" | "bootstrap"
