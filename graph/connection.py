"""Single place that opens the Neo4j driver, so every module (schema setup,
graph_writer, queries, the Session 4 test script) shares one connection
config sourced from shared/config.py.
"""
from neo4j import GraphDatabase

from shared.config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER


def get_driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
