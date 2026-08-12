"""Structuring Agent - the first stop for every already-flagged transaction.

One OpenAI call, no tools, no loop: it does not re-decide whether the score
crossed some threshold (branch_node/consumer.py already gated on the model's
own tuned threshold before ever publishing to fraud_events - see CLAUDE.md's
"Session 6 Update"). It also no longer renders a confidence judgment - that
used to exist (HIGH/MEDIUM/LOW + top_signals) but never gated anything after
the Session 6 fix (the Money-Trail Agent always runs regardless), so keeping
it around was dead weight with a real accuracy cost and no decision-making
value. Its only job now: list every flagged transaction as it arrives and
attach a short, factual, plain-English summary - purely descriptive context
for a human reader (and eventually the Report Agent), never a judgment call
about whether to investigate further. See CLAUDE.md's "Post-Session 6
Extension - Reframed 3-Agent Pipeline" for the full reasoning.
"""
import json

from openai import OpenAI

from shared.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from shared.openai_utils import chat_completion_with_retry
from shared.schemas import ScoreRecord

_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "structuring_summary",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "2-3 sentences, plain English, strictly factual: what this "
                    "transaction is and why the ML model likely flagged it. No judgment call, no "
                    "confidence rating - just a clear description of the transaction itself.",
                },
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
    },
}

_SYSTEM_PROMPT = """You are a bank's fraud-operations analyst. A machine learning model has \
already flagged the transaction below as potentially suspicious for structuring (splitting cash \
activity to stay under the $10,000 CTR reporting threshold). Your only job is to write a short, \
factual, plain-English summary (2-3 sentences) of this transaction and the features that likely \
drove the flag - amount relative to the threshold, whether it's cash, timing, recent velocity, \
account age. Do not judge whether this is worth investigating further and do not rate your \
confidence - that decision belongs to a later, deterministic step, not to you. Just describe the \
transaction clearly."""


def _describe_features(record: ScoreRecord) -> str:
    f = record.features
    pct_of_threshold = round(f.amount_ratio_to_threshold * 100, 1)
    return (
        f"Model score: {record.score:.3f}\n"
        f"Branch: {record.branch_id}\n"
        f"Amount vs. $10,000 CTR threshold: {pct_of_threshold}% of the threshold\n"
        f"Cash transaction: {'yes' if f.is_cash else 'no'}\n"
        f"Hour of day: {f.hour_of_day}:00\n"
        f"Day of week (0=Mon): {f.day_of_week}\n"
        f"Similar transactions from this account in the last 10 minutes: {f.velocity_10min}\n"
        f"Account age (days): {f.account_age_days}\n"
        f"Is a transfer-out: {'yes' if f.is_transfer_out else 'no'}"
    )


_MAX_SUMMARY_CHARS = 500
_MAX_SUMMARY_SENTENCES = 6


def _is_malformed_summary(summary: str) -> bool:
    """The JSON schema only constrains the *shape* of the response (a
    string), not its length or content. A weak local model occasionally free-
    associates well past its own answer - narrating meta-commentary about
    following instructions ("Adjusted for factual summary format... End of
    summary... For final submission, please review...") instead of stopping
    at the requested 2-3 factual sentences. Nothing in the schema itself
    catches this. Treat anything wildly longer than a real 2-3 sentence
    summary as the same class of failure as an API error.
    """
    if not summary:
        return True
    return len(summary) > _MAX_SUMMARY_CHARS or summary.count(".") > _MAX_SUMMARY_SENTENCES


def structuring_agent(record: ScoreRecord) -> dict:
    """Returns {summary}. Falls back to a plain, code-generated summary (not
    an error message pretending to be one) on any API/parsing failure - or on
    a malformed (rambling) LLM response, see _is_malformed_summary - so a
    flagged transaction is never silently dropped from the running list even
    if the LLM call fails - see CLAUDE.md's "Common Issues" note on wrapping
    every json.loads() in try/except.
    """
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    try:
        response = chat_completion_with_retry(
            client,
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _describe_features(record)},
            ],
            response_format=_RESPONSE_SCHEMA,
        )
        result = json.loads(response.choices[0].message.content)
        if _is_malformed_summary(result.get("summary", "")):
            raise ValueError("structuring agent returned a malformed (rambling) summary")
        return result
    except Exception:
        f = record.features
        pct = round(f.amount_ratio_to_threshold * 100, 1)
        return {
            "summary": f"{'Cash' if f.is_cash else 'Non-cash'} transaction at {pct}% of the "
            f"$10,000 CTR threshold, flagged by the ML model (score {record.score:.3f}) at "
            f"{f.hour_of_day}:00 on branch {record.branch_id}."
        }


if __name__ == "__main__":
    # Standalone smoke test - a hand-fed fake ScoreRecord, no Redis/Kafka/Neo4j involved.
    from shared.schemas import TxnFeatures

    fake = ScoreRecord(
        branch_id="loc1",
        token_id="tok_test_abc123",
        txn_id="txn_test_001",
        score=0.91,
        features=TxnFeatures(
            amount_ratio_to_threshold=0.94,
            is_cash=True,
            hour_of_day=2,
            day_of_week=3,
            velocity_10min=6,
            account_age_days=0,
            is_transfer_out=False,
        ),
        timestamp="2026-07-16T02:14:00.000Z",
    )
    result = structuring_agent(fake)
    print(json.dumps(result, indent=2))
