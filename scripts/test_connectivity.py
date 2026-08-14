"""Throwaway Session 1 exit-criteria check: confirm Redis, Neo4j, and Kafka
are reachable from outside the compose network (i.e. from the host).

Run with `docker-compose up -d` already running, then:
    python scripts/test_connectivity.py
"""
import os
import sys

import redis
from dotenv import load_dotenv
from kafka import KafkaProducer, KafkaConsumer
from neo4j import GraphDatabase

load_dotenv()

# Host-facing addresses (the docker-compose service names only resolve
# inside the compose network) - ports match the host port mappings in
# docker-compose.yml. Redis/Neo4j are remapped off their defaults because
# the original FedShield stack already occupies 6379/7474/7687 on this host.
REDIS_URL = "redis://localhost:6380"
NEO4J_URI = "bolt://localhost:7688"
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "your_password_here")
KAFKA_BROKER = "localhost:29092"


def check_redis():
    client = redis.from_url(REDIS_URL)
    assert client.ping()
    client.set("connectivity_check", "ok")
    assert client.get("connectivity_check") == b"ok"
    print("[OK] Redis reachable and read/write works")


def check_neo4j():
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as session:
        result = session.run("RETURN 1 AS one")
        assert result.single()["one"] == 1
    driver.close()
    print("[OK] Neo4j reachable and query works")


def check_kafka():
    topic = "connectivity_check"
    producer = KafkaProducer(bootstrap_servers=KAFKA_BROKER)
    producer.send(topic, b"ok")
    producer.flush()

    consumer = KafkaConsumer(
        topic,
        bootstrap_servers=KAFKA_BROKER,
        auto_offset_reset="earliest",
        consumer_timeout_ms=10000,
    )
    messages = list(consumer)
    assert any(msg.value == b"ok" for msg in messages)
    print("[OK] Kafka reachable, produce/consume round-trip works")


if __name__ == "__main__":
    checks = [("Redis", check_redis), ("Neo4j", check_neo4j), ("Kafka", check_kafka)]
    failures = []
    for name, check in checks:
        try:
            check()
        except Exception as exc:
            print(f"[FAIL] {name}: {exc}")
            failures.append(name)

    if failures:
        print(f"\nConnectivity check FAILED for: {', '.join(failures)}")
        sys.exit(1)

    print("\nAll services reachable. Session 1 exit criteria met.")
