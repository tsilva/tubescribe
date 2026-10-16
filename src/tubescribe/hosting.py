"""Vercel web role: durable storage and queue submission, no processing loop."""

import os
import re
from pathlib import Path

from .config import RuntimeConfig
from .dashboard import make_handler


def hosted_handler():
    # Vercel's Neon integration supplies the unpooled URL. Sessions retain our
    # read-only setting, so use the direct endpoint rather than transaction pooling.
    if not os.environ.get("TUBESCRIBE_DATABASE_URL"):
        direct = os.environ.get("DATABASE_URL_UNPOOLED") or os.environ.get(
            "POSTGRES_URL_NON_POOLING"
        )
        if direct:
            os.environ["TUBESCRIBE_DATABASE_URL"] = direct
    os.environ.setdefault("TUBESCRIBE_DISPATCH", "queue")
    os.environ.setdefault("TUBESCRIBE_MIGRATIONS", "check")
    root = Path(os.environ.get("TUBESCRIBE_DATA", "/tmp/tubescribe"))
    origins = tuple(
        "https://" + host
        for name in ("VERCEL_URL", "VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_BRANCH_URL")
        if (host := os.environ.get(name))
        and re.fullmatch(r"[a-zA-Z0-9.-]+\.vercel\.app", host)
    )
    if not os.environ.get("TUBESCRIBE_PUBLIC_ORIGIN") and origins:
        os.environ["TUBESCRIBE_PUBLIC_ORIGIN"] = origins[0]
    config = RuntimeConfig.load(root)
    secret = os.environ.get("TUBESCRIBE_FORM_SECRET", "")
    if not (
        config.network
        and config.s3_bucket
        and config.s3_endpoint
        and config.public_origin
        and config.queued
        and os.environ["TUBESCRIBE_MIGRATIONS"] == "check"
        and len(secret) >= 32
    ):
        raise ValueError(
            "Hosting requires PostgreSQL, S3, public origin, queue dispatch, "
            "migration check mode and a 32-character form secret"
        )
    return make_handler(root, secret, public_origins=origins)
