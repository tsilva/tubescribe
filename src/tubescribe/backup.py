"""Verified portable snapshots and guarded restore into an empty archive."""

import json
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .blobs import atomic_write, checked_key, file_digest
from .database import columns, export_sqlite
from .storage import worker_lock


def export_archive(store, destination):
    destination = Path(destination).resolve()
    if destination == store.root or store.root in destination.parents:
        raise ValueError("Export outside the live archive directory")
    if destination.exists():
        raise ValueError("Export destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with worker_lock(store.root):
        if store.pending_agent_runs():
            raise ValueError("Wait for queued/active gateway runs before export")
        staging = Path(tempfile.mkdtemp(prefix=".tubescribe-backup-", dir=destination.parent))
        try:
            store.subtitles()
            export_sqlite(store.root, staging / "archive.sqlite3")
            with closing(sqlite3.connect(staging / "archive.sqlite3")) as db, db:
                db.execute("DELETE FROM claims")
                db.execute(
                    "UPDATE submissions SET state='queued',pid=NULL,worker_id=NULL WHERE state IN ('queued','active')"
                )
            for key in store.blobs.keys():
                if key.split("/")[0] in {
                    "transcripts",
                    "audio",
                    "videos",
                    "artifacts",
                    "workflows",
                    "agent-results",
                }:
                    store.blobs.copy_to(key, staging / checked_key(key))
            from .publication import export_publications

            export_publications(staging)
            inventory = {
                p.relative_to(staging).as_posix(): file_digest(p)
                for p in sorted(staging.rglob("*"))
                if p.is_file()
            }
            atomic_write(
                staging / "backup.json",
                json.dumps({"schema_version": 1, "files": inventory}, sort_keys=True).encode(),
            )
            os.rename(staging, destination)
            fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return {
        "exported_to": str(destination),
        "files": len(inventory),
        "includes": "SQLite, transcripts, screenshots, artifacts, checkpoints, queued work",
    }


def restore_archive(store, source):
    source = Path(source).resolve()
    manifest = json.loads((source / "backup.json").read_text())
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("Unsupported backup manifest")
    inventory = manifest["files"]
    if "archive.sqlite3" not in inventory:
        raise ValueError("Backup has no database")
    for key, checksum in inventory.items():
        path = source / checked_key(key)
        if (
            not path.resolve().is_relative_to(source)
            or path.is_symlink()
            or file_digest(path) != checksum
        ):
            raise ValueError("Backup checksum mismatch")
    with (
        worker_lock(store.root),
        closing(
            sqlite3.connect((source / "archive.sqlite3").as_uri() + "?mode=ro", uri=True)
        ) as incoming,
    ):
        tables = [
            r[0]
            for r in incoming.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT IN ('claims','schema_migrations')"
            )
        ]
        # Reject a live archive before copying any object. Matching partial uploads may resume.
        for table in tables:
            if not re.fullmatch(r"[a-z_]+", table):
                raise ValueError("Unsupported backup table")
            fields = columns(store.db, table)
            saved_fields = {r[1] for r in incoming.execute(f"PRAGMA table_info({table})")}
            if not fields or set(fields) != saved_fields:
                raise ValueError("Backup schema is incompatible with this archive")
            if store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                raise ValueError("Restore requires an empty archive database")
        objects = {k: v for k, v in inventory.items() if k != "archive.sqlite3"}
        for key in store.blobs.keys():
            if store.local_blobs and key in {
                "archive.sqlite3",
                "archive.sqlite3-wal",
                "archive.sqlite3-shm",
                "worker.lock",
            }:
                continue
            if key not in objects or not store.exists(key, objects[key]):
                raise ValueError(
                    "Restore requires empty objects or matching partial restore contents"
                )
        for key, checksum in objects.items():
            ref = store.blobs.put_file(key, source / key)
            if ref.sha256 != checksum or not store.exists(key, checksum):
                raise ValueError("Restored object verification failed")
        with store.db:
            from .execution import assert_owned

            assert_owned(store.db, store.root)
            for table in tables:
                fields = columns(store.db, table)
                for row in incoming.execute(f"SELECT {','.join(fields)} FROM {table}"):
                    store.db.execute(
                        f"INSERT INTO {table}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
                        row,
                    )
            if store.config.network:
                for table in ("jobs", "runtime_events"):
                    store.db.execute(
                        f"SELECT setval(pg_get_serial_sequence('{table}','id'),COALESCE(MAX(id),1),COUNT(*)>0) FROM {table}"
                    )
    return {"restored_from": str(source), "objects": len(objects)}
