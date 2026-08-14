"""Branch container entrypoint. Session 1 stub replaced with the real
Session 3 wiring: this branch's Kafka consumer, running forever."""
from branch_node.consumer import run

if __name__ == "__main__":
    run()
