"""Database transports; repository SQL uses portable upserts and bound parameters."""

import hashlib
import os
import sqlite3
import time
from contextlib import closing


def network_database():
    return os.environ.get("TUBESCRIBE_DATABASE_URL")


def identity(root):
    from .config import database_path

    url = network_database()
    return (
        "postgres:" + hashlib.sha256(url.encode()).hexdigest() if url else str(database_path(root))
    )


def clock(db):
    """Use the database clock for network ownership and expiry decisions."""
    if getattr(db, "dialect", None) == "postgres":
        return float(db.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())").fetchone()[0])
    return time.time()


def database_errors():
    if network_database():
        import psycopg

        return (sqlite3.Error, psycopg.Error)
    return (sqlite3.Error,)


def transient_errors():
    from .blobs import StorageUnavailable

    if network_database():
        import psycopg

        return (StorageUnavailable, psycopg.OperationalError, psycopg.InterfaceError)
    return (StorageUnavailable,)


class Record:
    """Named and positional access matching the repository's existing row contract."""

    def __init__(self, names, values):
        self.names, self.values = names, values

    def keys(self):
        return self.names

    def __getitem__(self, key):
        return self.values[key if isinstance(key, (int, slice)) else self.names.index(key)]

    def __iter__(self):
        return iter(self.values)


def record_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Record(names, values)


class PostgreSQLConnection:
    dialect = "postgres"

    def __init__(self, url, *, readonly=False):
        import psycopg

        self.connection = psycopg.connect(
            url, autocommit=True, row_factory=record_factory, connect_timeout=5
        )
        self.depth = 0
        if readonly:
            self.connection.execute("SET default_transaction_read_only = on")

    def execute(self, sql, values=()):
        # Only qmark placeholders outside SQL literals are converted. Values never enter SQL.
        import re

        if values:
            sql = sql.replace("%", "%%")
        sql = re.sub(r"'(?:[^']|'')*'|\?", lambda m: "%s" if m[0] == "?" else m[0], sql)
        return self.connection.execute(sql, tuple(values) if values else None)

    def __enter__(self):
        if self.depth == 0:
            self.connection.execute("BEGIN")
        self.depth += 1
        return self

    def __exit__(self, kind, value, traceback):
        self.depth -= 1
        if self.depth == 0:
            self.rollback() if kind else self.commit()

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()


def connect(root, *, readonly=False, must_exist=False):
    from .config import database_path

    if url := network_database():
        if readonly:
            # Session-level read-only settings must not enter a transaction pool
            # that can later lend that backend to a writing worker.
            url = os.environ.get("TUBESCRIBE_DATABASE_UNPOOLED_URL") or url
        return PostgreSQLConnection(url, readonly=readonly)
    path = database_path(root)
    db = (
        sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
        if readonly
        else sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=30)
        if must_exist
        else sqlite3.connect(path, timeout=30)
    )
    db.row_factory = sqlite3.Row
    # This is connection-local; schema-check startup must retain referential integrity
    # even when it never invokes the migration runner.
    db.execute("PRAGMA foreign_keys=ON")
    return db


def columns(db, table):
    if getattr(db, "dialect", "sqlite") == "postgres":
        return [
            r[0]
            for r in db.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=? ORDER BY ordinal_position",
                (table,),
            )
        ]
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def serialize(db, key):
    """Short transaction lock for dedupe, migrations, and maintenance coordination."""
    if getattr(db, "dialect", "sqlite") == "postgres":
        number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
        db.execute("SELECT pg_advisory_xact_lock(?)", (number,))
    else:
        db.execute("BEGIN IMMEDIATE")


def export_sqlite(root, destination):
    """Portable snapshot for either backend, using one consistent source transaction."""
    from .migrations import migrate

    with (
        closing(connect(root, readonly=True)) as source,
        closing(sqlite3.connect(destination)) as target,
    ):
        migrate(target)
        source.execute("BEGIN ISOLATION LEVEL REPEATABLE READ" if network_database() else "BEGIN")
        try:
            tables = [
                r[0]
                for r in target.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            for table in tables:
                target.execute(f"DELETE FROM {table}")
                names = columns(target, table)
                for row in source.execute(f"SELECT {','.join(names)} FROM {table}"):
                    target.execute(
                        f"INSERT INTO {table}({','.join(names)}) VALUES({','.join('?' for _ in names)})",
                        tuple(row),
                    )
            target.commit()
        finally:
            source.rollback()
