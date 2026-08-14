"""Central configuration: thresholds, topic names, connection strings.

Every service (branch_node, fl_server, agents, orchestrator, simulator) imports
from here instead of reading os.environ directly, so there is exactly one
place that knows how config is sourced.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# --- Branches ---
BRANCH_IDS = ["loc1", "loc2", "loc3"]

# --- Kafka ---
KAFKA_BROKER = os.getenv("KAFKA_BROKER", "localhost:9092")
KAFKA_TOPICS = {branch_id: f"txns.{branch_id}" for branch_id in BRANCH_IDS}

# --- Redis ---
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# --- Neo4j ---
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "your_password_here")

# --- Flower FL server ---
FL_SERVER_ADDRESS = os.getenv("FL_SERVER_ADDRESS", "localhost:8080")

# --- Thresholds ---
FRAUD_THRESHOLD = float(os.getenv("FRAUD_THRESHOLD", "0.75"))
JUDGE_CONFIDENCE_THRESHOLD = float(os.getenv("JUDGE_CONFIDENCE_THRESHOLD", "0.8"))

# Real US regulatory figure (Currency Transaction Report) - not a tunable.
# Placement/structuring amounts in the fraud scenario are generated just under this.
CTR_THRESHOLD_USD = float(os.getenv("CTR_THRESHOLD_USD", "10000"))

# --- Token scheme ---
# One GLOBAL salt shared system-wide (unlike original FedShield's branch-salted
# scheme) so the same real account always maps to the same Neo4j node
# regardless of which branch's consumer writes an edge involving it.
GLOBAL_TOKEN_SALT = os.getenv("GLOBAL_TOKEN_SALT", "your_salt_here")

# --- Feature flags (evaluation ablation conditions) ---
ENABLE_ADVERSARIAL_VERIFICATION = os.getenv("ENABLE_ADVERSARIAL_VERIFICATION", "true").lower() == "true"
ENABLE_FL_FEEDBACK = os.getenv("ENABLE_FL_FEEDBACK", "true").lower() == "true"

# --- Agent LLM ---
# Session 6 first switched the agent LLM provider from Claude/Anthropic to
# OpenAI's hosted API, then pivoted again to a local Ollama server after
# hitting OpenAI's free-tier 50-requests/day cap - see CLAUDE.md's "Session 6
# Update" for the full history. Ollama exposes an OpenAI-compatible endpoint,
# so agents still use the `openai` SDK, just pointed at LLM_BASE_URL instead
# of OpenAI's servers. Names are provider-neutral (LLM_*, not OPENAI_*) so a
# future provider swap doesn't leave a stale, misleading name behind again.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:7b")

# --- Money-Trail Agent traversal safety ceiling ---
# NOT the primary stop condition (see CLAUDE.md) - only a runaway guardrail.
MAX_TRAVERSAL_HOPS = 20

# --- Neo4j synthetic sink/source nodes ---
CASH_SOURCE_TOKEN = "CASH"
CASH_SINK_TOKEN = "CASH_OUT"
