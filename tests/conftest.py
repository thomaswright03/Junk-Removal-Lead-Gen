"""Set TEST_DATABASE_URL=postgresql://... to run every test against Postgres
instead of SQLite (CI does): each database a test opens becomes its own
Postgres schema, dropped afterwards."""

import os
import uuid

import pytest

PG_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def postgres_instead_of_sqlite(monkeypatch):
    if not PG_URL:
        yield
        return
    import psycopg

    from leadgen import db

    real_connect = db.connect
    schemas = {}

    def connect(path):
        if db.pg.is_url(path):
            return real_connect(path)
        key = str(path)
        if key not in schemas:
            schemas[key] = "t_" + uuid.uuid4().hex[:12]
            with psycopg.connect(PG_URL, autocommit=True) as c:
                c.execute(f"CREATE SCHEMA {schemas[key]}")
        sep = "&" if "?" in PG_URL else "?"
        return real_connect(f"{PG_URL}{sep}options=-csearch_path%3D{schemas[key]}")

    monkeypatch.setattr(db, "connect", connect)
    yield
    with psycopg.connect(PG_URL, autocommit=True) as c:
        for s in schemas.values():
            c.execute(f"DROP SCHEMA {s} CASCADE")
