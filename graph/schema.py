"""Neo4j constraints/indexes. Run once at startup (idempotent - CREATE ...
IF NOT EXISTS) so every branch container and the standalone test script can
call this safely without coordinating who runs it first.
"""
import time

from neo4j.exceptions import TransientError

from graph.connection import get_driver

_MAX_RETRIES = 5


def setup_schema(driver=None) -> None:
    """All 3 branch containers call this at startup, at roughly the same
    moment. CREATE CONSTRAINT IF NOT EXISTS is idempotent but Neo4j's own
    schema-change lock isn't deadlock-free under concurrent callers - a
    TransientError.Transaction.DeadlockDetected here is expected sometimes,
    not a real failure, so it's retried with backoff instead of crashing
    the branch container.
    """
    owns_driver = driver is None
    driver = driver or get_driver()
    try:
        for attempt in range(_MAX_RETRIES):
            try:
                with driver.session() as session:
                    session.run(
                        "CREATE CONSTRAINT account_token_id_unique IF NOT EXISTS "
                        "FOR (a:Account) REQUIRE a.token_id IS UNIQUE"
                    )
                return
            except TransientError:
                if attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(0.5 * (attempt + 1))
    finally:
        if owns_driver:
            driver.close()


if __name__ == "__main__":
    setup_schema()
    print("[schema] Account.token_id uniqueness constraint ensured.")
