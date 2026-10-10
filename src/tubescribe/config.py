"""Validated runtime roles; durable locations are independent of process scratch."""

import math
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_DATABASES = {}
_WORKER_ID = uuid.uuid4().hex


def database_path(root):
    root = Path(root).resolve()
    return _DATABASES.get(
        str(root), Path(os.environ.get("TUBESCRIBE_DATABASE", root / "archive.sqlite3")).resolve()
    )


def bind_database(root, database):
    path = Path(database).resolve()
    _DATABASES[str(Path(root).resolve())] = path
    return path


@dataclass(frozen=True)
class RuntimeConfig:
    scratch: Path
    database: Path
    objects: Path | None = None
    host: str = "127.0.0.1"
    public_origin: str | None = None
    access_token: str | None = None
    queued: bool = False
    bridge: str | None = None
    agent_gateway: bool = False
    worker_poll: float = 3
    agent_concurrency: int = 1
    network: bool = False
    worker_id: str = _WORKER_ID
    s3_bucket: str | None = None
    s3_endpoint: str | None = None
    downloads: int = 1
    gpu_slots: int = 1
    agent_slots: int = 1

    @classmethod
    def load(cls, root):
        config = cls(
            scratch=Path(root).resolve(),
            database=database_path(root),
            objects=Path(os.environ["TUBESCRIBE_OBJECTS"]).resolve()
            if os.environ.get("TUBESCRIBE_OBJECTS")
            else None,
            host=os.environ.get("TUBESCRIBE_BIND", "127.0.0.1"),
            public_origin=os.environ.get("TUBESCRIBE_PUBLIC_ORIGIN"),
            access_token=os.environ.get("TUBESCRIBE_ACCESS_TOKEN"),
            queued=os.environ.get("TUBESCRIBE_DISPATCH", "process") == "queue",
            bridge=os.environ.get("TUBESCRIBE_AGENTBRIDGE"),
            agent_gateway=os.environ.get("TUBESCRIBE_AGENT_PROTOCOL", "sync") == "durable",
            worker_poll=float(os.environ.get("TUBESCRIBE_WORKER_POLL", "3")),
            agent_concurrency=int(os.environ.get("TUBESCRIBE_AGENT_CONCURRENCY", "1")),
            network=bool(os.environ.get("TUBESCRIBE_DATABASE_URL")),
            worker_id=os.environ.get("TUBESCRIBE_WORKER_ID", _WORKER_ID),
            s3_bucket=os.environ.get("TUBESCRIBE_S3_BUCKET"),
            s3_endpoint=os.environ.get("TUBESCRIBE_S3_ENDPOINT"),
            downloads=int(os.environ.get("TUBESCRIBE_DOWNLOAD_SLOTS", "1")),
            gpu_slots=int(os.environ.get("TUBESCRIBE_GPU_SLOTS", "1")),
            agent_slots=int(os.environ.get("TUBESCRIBE_AGENT_SLOTS", "1")),
        )
        if config.network:
            if urlsplit(os.environ["TUBESCRIBE_DATABASE_URL"]).scheme not in {
                "postgres",
                "postgresql",
            }:
                raise ValueError("Database URL must use PostgreSQL")
        if not config.worker_id.strip() or len(config.worker_id) > 200:
            raise ValueError("Worker identity must contain 1–200 characters")
        if config.s3_bucket and config.objects:
            raise ValueError("Select either S3 or a directory object store")
        if config.s3_endpoint:
            endpoint = urlsplit(config.s3_endpoint)
            if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
                raise ValueError("S3 endpoint must be an HTTP(S) URL")
            if endpoint.scheme != "https" and endpoint.hostname not in {
                "localhost",
                "127.0.0.1",
                "::1",
                "objects",
            }:
                raise ValueError("S3 requires HTTPS outside the local rehearsal")
        if any(
            not 1 <= count <= 32
            for count in (config.downloads, config.gpu_slots, config.agent_slots)
        ):
            raise ValueError("Stage capacities must be between 1 and 32")
        if os.environ.get("TUBESCRIBE_DISPATCH", "process") not in {"process", "queue"}:
            raise ValueError("TUBESCRIBE_DISPATCH must be process or queue")
        if os.environ.get("TUBESCRIBE_AGENT_PROTOCOL", "sync") not in {"sync", "durable"}:
            raise ValueError("TUBESCRIBE_AGENT_PROTOCOL must be sync or durable")
        if not 1 <= config.agent_concurrency <= 8:
            raise ValueError("Agent concurrency must be between 1 and 8")
        if not math.isfinite(config.worker_poll) or config.worker_poll <= 0:
            raise ValueError("Worker polling must be positive")
        if config.access_token and len(config.access_token) < 32:
            raise ValueError("Access token must contain at least 32 characters")
        if config.host not in {"127.0.0.1", "localhost", "::1"}:
            if not config.access_token or len(config.access_token) < 32 or not config.public_origin:
                raise ValueError(
                    "Public binding requires a 32-character access token and public origin"
                )
        if config.public_origin:
            url = urlsplit(config.public_origin)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.path not in {"", "/"}
                or url.query
                or url.fragment
                or url.username
            ):
                raise ValueError("Public origin must be an HTTPS origin")
        return config
