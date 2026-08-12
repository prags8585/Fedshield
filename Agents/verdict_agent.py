"""Verdict Agent - the "Baseline" condition from CLAUDE.md's evaluation
table (ENABLE_ADVERSARIAL_VERIFICATION=false): a single agent weighs the
Money-Trail Agent's confirmed evidence and renders GUILTY/NOT_GUILTY,
instead of a full Prosecutor/Defense/Judge debate (deferred for now - see
CLAUDE.md's "Session 6 Update" for the reasoning). Only ever called on
evidence that already has stop_reason == "convergence_found" - there is no
verdict to render on a dead end.

Its job is a different question than what the Money-Trail Agent already
answered. The Money-Trail Agent's "did the money mathematically connect?"
is now deterministic and accurate (see the recall fixes in CLAUDE.md's
"Session 6 Update"). This agent answers "given that real connection, does
it prove deliberate laundering, or could there be an innocent explanation?"
- a genuinely different, judgment-requiring question a deterministic check
can't answer on its own.
"""
import json

from openai import OpenAI

from shared.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from shared.openai_utils import chat_completion_with_retry

_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "verdict",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["GUILTY", "NOT_GUILTY"]},
                "confidence": {
                    "type": "number",
                    "description": "0.0 to 1.0 - how confident this verdict is.",
                },
                "rationale": {"type": "string"},
            },
            "required": ["verdict", "confidence", "rationale"],
            "additionalProperties": False,
        },
    },
}

_SYSTEM_PROMPT = """You are a bank's money-laundering investigator rendering a final verdict. \
An automated check has already CONFIRMED a real cross-branch convergence: independent cash \
deposits across different branches, each just under the $10,000 CTR reporting threshold, \
converged on the same account within a tight time window, preserving most of their value through \
several hops. Your job is not to re-check whether the deposits connect - that is already \
mathematically confirmed - your job is to decide whether this pattern, taken as a whole, proves \
deliberate structuring/money laundering, or whether a plausible innocent explanation exists.

Weigh in favor of GUILTY when: amounts are deliberately just under the reporting threshold, \
timing clusters late at night, accounts are brand-new, and money moves quickly through several \
hops before a large cash withdrawal. Weigh in favor of NOT_GUILTY only if the evidence itself \
suggests a plausible legitimate explanation (e.g. an established business account, ordinary \
payroll/rent patterns) - do not default to NOT_GUILTY just to be cautious; the convergence itself \
is already strong evidence.

Respond with confidence as a number between 0 and 1."""


def verdict_agent(evidence: dict, structuring_context: str = "") -> dict:
    """`evidence` is a Money-Trail Agent result with stop_reason ==
    "convergence_found" - {path, convergence_node, summary}. Returns
    {verdict, confidence, rationale}.

    Falls back to a safe, zero-confidence NOT_GUILTY on any API/parsing
    failure, so a broken call never silently becomes a false GUILTY label -
    see CLAUDE.md's "Common Issues" note on wrapping every json.loads() in
    try/except.
    """
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    try:
        response = chat_completion_with_retry(
            client,
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "convergence_account": evidence.get("convergence_node"),
                            "hops": evidence.get("path", []),
                            "money_trail_summary": evidence.get("summary", ""),
                            "structuring_context": structuring_context,
                        },
                        default=str,
                    ),
                },
            ],
            response_format=_RESPONSE_SCHEMA,
        )
        result = json.loads(response.choices[0].message.content)
        raw_confidence = float(result.get("confidence", 0.0))
        # A local 7B model can occasionally emit a nonsensical numeric literal
        # for confidence (observed live: 1.0000000762939453e-108) - technically
        # inside [0, 1], so the clamp below never catches it, but no genuine
        # judgment call (even "not very confident") would ever land this many
        # orders of magnitude below a real probability. Treat this the same
        # as any other malformed response, not as a legitimate near-zero
        # confidence, so it can never silently produce a labels=0,
        # displays-as-"GUILTY (0%)" result that looks like a real verdict.
        if 0 < raw_confidence < 1e-3:
            raise ValueError(f"implausible confidence value from model: {raw_confidence!r}")
        result["confidence"] = max(0.0, min(1.0, raw_confidence))
        return result
    except Exception as exc:
        return {
            "verdict": "NOT_GUILTY",
            "confidence": 0.0,
            "rationale": f"Verdict Agent call failed ({exc}); defaulting to NOT_GUILTY so a broken "
            "call never silently becomes a false GUILTY label.",
        }


if __name__ == "__main__":
    # Standalone smoke test - hand-fed fake evidence, no Redis/Neo4j involved.
    fake_evidence = {
        "convergence_node": "tok_consolidation",
        "path": [
            {
                "from_token": "tok_a", "to_token": "tok_b", "txn_id": "tx_1",
                "amount": 9328.42, "ts": "2026-07-14T18:11:00.000Z", "channel": "ONLINE",
            },
            {
                "from_token": "tok_b", "to_token": "tok_consolidation", "txn_id": "tx_2",
                "amount": 9161.14, "ts": "2026-07-14T20:30:00.000Z", "channel": "WIRE_ROOM",
            },
        ],
        "summary": "Three independent deposits, each just under $10,000, converged on one "
        "account within 3 hours, preserving 95% of value.",
    }
    print(json.dumps(verdict_agent(fake_evidence), indent=2))
