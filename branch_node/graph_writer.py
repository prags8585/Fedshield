"""Writes masked transactions as edges into the shared Neo4j money-trail
graph (see CLAUDE.md decision #3: written once, by the sending branch's
consumer, never duplicated by a receiving side - which falls out naturally
here since each event lives on exactly one branch's Kafka topic).

Direction follows mask_event()'s local/counterparty framing: is_transfer_out
means money leaves the local token (originator) toward the counterparty
(withdrawal, purchase, or transfer-out), so the edge is local -> counterparty.
A deposit is the mirror case (counterparty is the CASH sentinel, sending to
the local token), so the edge is counterparty -> local.
"""
from graph.connection import get_driver


def edge_for(masked: dict) -> dict:
    if masked["is_transfer_out"]:
        from_token, to_token = masked["token_id"], masked["counterparty_token_id"]
    else:
        from_token, to_token = masked["counterparty_token_id"], masked["token_id"]

    return {
        "from_token": from_token,
        "to_token": to_token,
        "txn_id": masked["txn_id"],
        "amount": masked["amount"],
        "ts": masked["timestamp"],
        "txn_type": masked["txn_type"],
        "channel": masked["channel"],
        "branch_id": masked["branch_id"],
    }


def _write_edge_tx(tx, edge: dict) -> None:
    tx.run(
        """
        MERGE (a:Account {token_id: $from_token})
        MERGE (b:Account {token_id: $to_token})
        MERGE (a)-[r:TRANSACTED {txn_id: $txn_id}]->(b)
        SET r.amount = $amount, r.ts = $ts, r.txn_type = $txn_type,
            r.channel = $channel, r.branch_id = $branch_id
        """,
        **edge,
    )


class GraphWriter:
    """Thin wrapper so branch_node/consumer.py holds one open driver for its
    whole run instead of opening a connection per transaction.
    """

    def __init__(self, driver=None):
        self._owns_driver = driver is None
        self.driver = driver or get_driver()

    def write_edge(self, masked: dict) -> None:
        edge = edge_for(masked)
        with self.driver.session() as session:
            session.execute_write(_write_edge_tx, edge)

    def close(self) -> None:
        if self._owns_driver:
            self.driver.close()
