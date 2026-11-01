from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic import model_validator
from urllib.parse import urlsplit


class Settings(BaseModel):
    neo4j_uri: str = Field(min_length=1)
    neo4j_user: str = Field(min_length=1)
    neo4j_password: str = Field(min_length=1)
    neo4j_database: str = Field(default="neo4j", min_length=1)
    postgres_dsn: str | None = None

    @model_validator(mode="after")
    def valid_urls(self):
        graph = urlsplit(self.neo4j_uri)
        if graph.scheme not in {"bolt", "bolt+s", "neo4j", "neo4j+s"} or not graph.hostname:
            raise ValueError("NEO4J_URI must be a Neo4j connection URI")
        if self.postgres_dsn is not None:
            database = urlsplit(self.postgres_dsn)
            if database.scheme not in {"postgresql", "postgres"} or not database.hostname or not database.path.strip("/"):
                raise ValueError("POSTGRES_DSN must identify a PostgreSQL database")
        return self

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env", override=False)
        password = os.getenv("NEO4J_PASSWORD", "")
        if not password:
            raise RuntimeError("NEO4J_PASSWORD is required; set it in .env or the process environment")
        return cls(
            neo4j_uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            neo4j_user=os.getenv("NEO4J_USER", "neo4j"),
            neo4j_password=password,
            neo4j_database=os.getenv("NEO4J_DATABASE", "neo4j"),
            postgres_dsn=os.getenv("POSTGRES_DSN") or None,
        )
