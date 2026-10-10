"""Numbered SQL migrations, including adoption of pre-versioned SQLite archives."""

BASE = """
          PRAGMA journal_mode=WAL;
          PRAGMA synchronous=FULL;
          PRAGMA foreign_keys=ON;
          CREATE TABLE IF NOT EXISTS channels (
            id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL,
            added_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS discovery (
            channel_id TEXT REFERENCES channels(id), kind TEXT, state TEXT,
            indexed_count INTEGER DEFAULT 0, updated_at TEXT, error TEXT,
            PRIMARY KEY(channel_id,kind));
          CREATE TABLE IF NOT EXISTS videos (
            id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES channels(id),
            title TEXT NOT NULL, url TEXT NOT NULL, kind TEXT NOT NULL,
            duration REAL, published_at TEXT, discovered_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS profiles (name TEXT PRIMARY KEY, identity TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id),
            profile TEXT NOT NULL, download_state TEXT NOT NULL DEFAULT 'pending',
            asr_state TEXT NOT NULL DEFAULT 'pending', state TEXT NOT NULL DEFAULT 'pending',
            audio_path TEXT, download_attempts INTEGER NOT NULL DEFAULT 0,
            asr_attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
            failed_stage TEXT, error_kind TEXT, error TEXT, updated_at TEXT NOT NULL,
            completed_at TEXT, UNIQUE(video_id,profile));
          CREATE TABLE IF NOT EXISTS transcripts (
            job_id INTEGER PRIMARY KEY REFERENCES jobs(id), json_path TEXT NOT NULL,
            text_path TEXT NOT NULL, json_sha256 TEXT NOT NULL, text_sha256 TEXT NOT NULL,
            language TEXT NOT NULL, model TEXT NOT NULL, revision TEXT NOT NULL,
            processing_seconds REAL NOT NULL, duration REAL NOT NULL, saved_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS diarization_profiles (
            profile TEXT PRIMARY KEY, identity TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS diarizations (
            job_id INTEGER PRIMARY KEY REFERENCES jobs(id), state TEXT NOT NULL DEFAULT 'pending',
            stage TEXT, audio_path TEXT, download_attempts INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0, save_attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
            error TEXT, updated_at TEXT NOT NULL, completed_at TEXT,
            asr_path TEXT, asr_sha256 TEXT, prediction_path TEXT, prediction_sha256 TEXT,
            speaker_count INTEGER, processing_seconds REAL);
          CREATE TABLE IF NOT EXISTS postprocessings (
            job_id INTEGER PRIMARY KEY REFERENCES jobs(id), state TEXT NOT NULL DEFAULT 'pending',
            stage TEXT, mode TEXT, source_path TEXT, source_sha256 TEXT, srt_sha256 TEXT,
            output_dir TEXT, manifest_path TEXT, manifest_sha256 TEXT,
            download_attempts INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
            save_attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
            error TEXT, updated_at TEXT NOT NULL, completed_at TEXT,
            figures INTEGER, agent_seconds REAL, total_tokens INTEGER);
          CREATE TABLE IF NOT EXISTS artifacts (
            job_id INTEGER NOT NULL REFERENCES jobs(id), workflow TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', stage TEXT,
            source_path TEXT, input_sha256 TEXT, workflow_sha256 TEXT,
            output_dir TEXT, response_sha256 TEXT, manifest_sha256 TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, save_attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT, updated_at TEXT NOT NULL, completed_at TEXT,
            PRIMARY KEY(job_id,workflow));
        """
CLAIMS = "CREATE TABLE IF NOT EXISTS claims (resource TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL); CREATE TABLE IF NOT EXISTS submissions (id TEXT PRIMARY KEY, dedupe TEXT NOT NULL, request TEXT NOT NULL, state TEXT NOT NULL, pid INTEGER, error TEXT, created REAL NOT NULL, updated REAL NOT NULL); CREATE UNIQUE INDEX IF NOT EXISTS submission_pending ON submissions(dedupe) WHERE state IN ('queued','active');"


def migrate(db):
    if getattr(db, "dialect", "sqlite") == "postgres":
        return migrate_postgres(db)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA foreign_keys=ON")
    # DDL and the version ledger commit together; competing processes serialize.
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY)")
        version = db.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
        if version < 1:
            for statement in (CLAIMS + BASE).split(";"):
                if statement.strip() and not statement.strip().startswith("PRAGMA"):
                    db.execute(statement)
            db.execute("INSERT INTO schema_migrations VALUES(1)")
        if version < 2:
            for table, column, definition in [
                ("diarizations", "save_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("artifacts", "run_pid", "INTEGER"),
                ("artifacts", "next_retry", "REAL NOT NULL DEFAULT 0"),
            ]:
                if column not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            db.execute("INSERT INTO schema_migrations VALUES(2)")
        if version < 3:
            db.execute(
                "CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, job_id INTEGER, stage TEXT NOT NULL, workflow TEXT, run_id TEXT, state TEXT NOT NULL, started REAL NOT NULL, finished REAL, elapsed REAL, metrics TEXT, error TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS runtime_events(id INTEGER PRIMARY KEY, run_id TEXT, event TEXT NOT NULL, created REAL NOT NULL, data TEXT NOT NULL)"
            )
            db.execute("INSERT INTO schema_migrations VALUES(3)")
        if version < 4:
            db.execute(
                "CREATE TABLE IF NOT EXISTS submission_results(id TEXT PRIMARY KEY REFERENCES submissions(id),result TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS agent_runs (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, request TEXT NOT NULL, state TEXT NOT NULL, response_key TEXT, error TEXT, created REAL NOT NULL, updated REAL NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute("INSERT INTO schema_migrations VALUES(4)")
        if version < 5:
            db.execute("ALTER TABLE submissions ADD COLUMN worker_id TEXT")
            db.execute("INSERT INTO schema_migrations VALUES(5)")
        db.commit()
    except BaseException:
        db.rollback()
        raise


def migrate_postgres(db):
    """PostgreSQL schema and version ledger commit under a migration advisory lock."""
    import re

    from .database import serialize

    with db:
        serialize(db, "schema-migrations")
        db.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY)")
        version = db.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
        if version > 5:
            raise ValueError("Database schema is newer than this application")
        if version < 1:
            schema = re.sub(r"\bREAL\b", "DOUBLE PRECISION", CLAIMS + BASE)
            schema = schema.replace(
                "id INTEGER PRIMARY KEY, video_id",
                "id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, video_id",
            )
            for statement in schema.split(";"):
                if statement.strip() and not statement.strip().startswith("PRAGMA"):
                    db.execute(statement)
            db.execute("INSERT INTO schema_migrations VALUES(1)")
        if version < 2:
            db.execute("ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS run_pid INTEGER")
            db.execute(
                "ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS next_retry DOUBLE PRECISION NOT NULL DEFAULT 0"
            )
            db.execute("INSERT INTO schema_migrations VALUES(2)")
        if version < 3:
            db.execute(
                "CREATE TABLE attempts(id TEXT PRIMARY KEY, job_id BIGINT, stage TEXT NOT NULL, workflow TEXT, run_id TEXT, state TEXT NOT NULL, started DOUBLE PRECISION NOT NULL, finished DOUBLE PRECISION, elapsed DOUBLE PRECISION, metrics TEXT, error TEXT)"
            )
            db.execute(
                "CREATE TABLE runtime_events(id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, run_id TEXT, event TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, data TEXT NOT NULL)"
            )
            db.execute("INSERT INTO schema_migrations VALUES(3)")
        if version < 4:
            db.execute(
                "CREATE TABLE submission_results(id TEXT PRIMARY KEY REFERENCES submissions(id),result TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE agent_runs(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, request TEXT NOT NULL, state TEXT NOT NULL, response_key TEXT, error TEXT, created DOUBLE PRECISION NOT NULL, updated DOUBLE PRECISION NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute("INSERT INTO schema_migrations VALUES(4)")
        if version < 5:
            db.execute("ALTER TABLE submissions ADD COLUMN worker_id TEXT")
            db.execute("INSERT INTO schema_migrations VALUES(5)")
