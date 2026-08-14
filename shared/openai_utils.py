"""Retry-with-backoff wrapper for OpenAI chat completion calls. Rate limiting
(see CLAUDE.md's "Session 6 Update") is a real, observed failure mode on
lower account tiers - not hypothetical - so every agent's LLM call goes
through this instead of calling client.chat.completions.create directly.
"""
import time

import openai


def chat_completion_with_retry(client, max_retries: int = 5, base_delay: float = 5.0, **kwargs):
    """Exponential backoff (5s, 10s, 20s, 40s, 80s) on 429 rate-limit errors
    only - any other exception propagates immediately, unretried.

    Uses client.with_options(max_retries=0) deliberately: the openai SDK's
    own default internal retry (max_retries=2) would otherwise fire *inside*
    each attempt here, bursting up to 3 real HTTP requests per attempt and
    blowing through a low per-minute rate limit before our own backoff ever
    gets a chance to let the window clear - a real failure mode hit while
    building the Money-Trail Agent, see CLAUDE.md's "Session 6 Update".
    """
    no_retry_client = client.with_options(max_retries=0)
    for attempt in range(max_retries):
        try:
            return no_retry_client.chat.completions.create(**kwargs)
        except openai.RateLimitError:
            if attempt == max_retries - 1:
                raise
            time.sleep(base_delay * (2**attempt))
