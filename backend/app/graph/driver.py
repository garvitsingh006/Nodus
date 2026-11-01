from __future__ import annotations

from neo4j import GraphDatabase, Driver, unit_of_work
from backend.app.config import Settings
from backend.app.graph.models import RelationType


SCHEMA = (
    "CREATE CONSTRAINT entity_scope_key IF NOT EXISTS FOR (n:Entity) REQUIRE n.key IS UNIQUE",
    "CREATE CONSTRAINT source_scope_key IF NOT EXISTS FOR (n:SourceRecord) REQUIRE n.key IS UNIQUE",
    "CREATE INDEX entity_scope_name IF NOT EXISTS FOR (n:Entity) ON (n.tenant_id, n.workspace_id, n.name)",
    "CREATE INDEX entity_scope_type IF NOT EXISTS FOR (n:Entity) ON (n.tenant_id, n.workspace_id, n.type)",
    "CREATE INDEX source_scope_id IF NOT EXISTS FOR (n:SourceRecord) ON (n.tenant_id, n.workspace_id, n.source_id)",
    "CREATE CONSTRAINT assertion_key IF NOT EXISTS FOR ()-[r:ASSERTS]-() REQUIRE r.key IS UNIQUE",
    "CREATE CONSTRAINT graph_revision_key IF NOT EXISTS FOR (n:GraphRevision) REQUIRE n.key IS UNIQUE",
) + tuple(
    f"CREATE CONSTRAINT fact_{kind.value.lower()}_key IF NOT EXISTS FOR ()-[r:{kind.value}]-() REQUIRE r.key IS UNIQUE"
    for kind in RelationType
)


class Neo4jStore:
    def __init__(self, settings: Settings, *, read_timeout: float = 8, write_timeout: float = 20):
        if read_timeout <= 0 or write_timeout <= 0:
            raise ValueError("Transaction timeouts must be positive")
        self.database = settings.neo4j_database
        self.read_timeout = read_timeout
        self.write_timeout = write_timeout
        self.driver: Driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            connection_timeout=10,
            max_transaction_retry_time=5,
        )

    def start(self) -> None:
        self.driver.verify_connectivity()
        with self.driver.session(database=self.database) as session:
            for statement in SCHEMA:
                session.run(statement).consume()

    def close(self) -> None:
        self.driver.close()

    def read(self, work, *args):
        with self.driver.session(database=self.database) as session:
            return session.execute_read(unit_of_work(timeout=self.read_timeout)(work), *args)

    def write(self, work, *args):
        with self.driver.session(database=self.database) as session:
            return session.execute_write(unit_of_work(timeout=self.write_timeout)(work), *args)
