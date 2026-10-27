"""Clerk session verification and ownership of the locally produced archive.

The reader ownership registry is additive and independent of the pipeline migration
ledger: old local processors can keep publishing into the same owned library.
"""

import base64
import os
import re
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import httpx

from .database import connect
from .storage import now


class AuthenticationUnavailable(Exception):
    pass


@dataclass(frozen=True)
class ReaderIdentity:
    user_id: str
    owns_library: bool


def ensure_reader_schema(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS reader_library ("
        "id TEXT PRIMARY KEY CHECK(id='archive'), issuer TEXT NOT NULL, "
        "owner_email TEXT NOT NULL, clerk_user_id TEXT, claimed_at TEXT)"
    )


def initialize_library(root, *, issuer, owner_email):
    """Explicit operator setup; never overwrite or reassign an existing owner."""
    if not re.fullmatch(r"https://[a-zA-Z0-9.-]+", issuer) or "@" not in owner_email:
        raise ValueError("An HTTPS Clerk issuer and owner email are required")
    with closing(connect(root, must_exist=True)) as db, db:
        ensure_reader_schema(db)
        db.execute(
            "INSERT INTO reader_library(id,issuer,owner_email) VALUES('archive',?,?) "
            "ON CONFLICT(id) DO NOTHING",
            (issuer, owner_email.casefold()),
        )
        row = db.execute("SELECT * FROM reader_library WHERE id='archive'").fetchone()
        if row["issuer"] != issuer or row["owner_email"] != owner_email.casefold():
            raise ValueError("Library ownership already configured; refusing reassignment")


class ClerkAccess:
    def __init__(self, root, origins, *, required=False):
        self.root = root
        self.origins = list(dict.fromkeys(origin.rstrip("/") for origin in origins if origin))
        self.secret = os.environ.get("CLERK_SECRET_KEY", "")
        self.publishable = os.environ.get("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "")
        self.enabled = bool(self.secret or self.publishable or required)
        if not self.enabled:
            return
        if not self.secret or not self.publishable or not self.origins:
            raise ValueError("Clerk keys and an authorized application origin are required")
        try:
            encoded = self.publishable.split("_", 2)[2]
            domain = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True).decode()
        except (ValueError, IndexError, UnicodeError) as error:
            raise ValueError("Invalid Clerk publishable key") from error
        if not domain.endswith("$") or not re.fullmatch(r"[a-zA-Z0-9.-]+\$", domain):
            raise ValueError("Invalid Clerk frontend domain")
        self.domain = domain[:-1]
        self.issuer = "https://" + self.domain

    def configuration(self):
        return {
            "enabled": self.enabled,
            **(
                {"publishable_key": self.publishable, "frontend_api": self.issuer}
                if self.enabled
                else {}
            ),
        }

    def session_user(self, headers):
        """Only Clerk session JWTs, bound to this instance and allowed web origins."""
        from clerk_backend_api.security import AuthenticateRequestOptions, authenticate_request

        try:
            state = authenticate_request(
                httpx.Request("GET", self.origins[0], headers=dict(headers)),
                AuthenticateRequestOptions(
                    secret_key=self.secret,
                    authorized_parties=self.origins,
                    accepts_token=["session_token"],
                ),
            )
        except Exception as error:
            raise AuthenticationUnavailable() from error
        if not state.is_authenticated:
            return None
        payload = state.payload or {}
        user_id = payload.get("sub")
        if (
            payload.get("sts") == "pending"
            or payload.get("iss") != self.issuer
            or not isinstance(user_id, str)
            or not user_id.startswith("user_")
            or not payload.get("sid")
            or not all(name in payload for name in ("exp", "iat", "nbf"))
        ):
            return None
        return user_id

    def identify(self, headers):
        user_id = self.session_user(headers)
        return ReaderIdentity(user_id, self.owns_library(user_id)) if user_id else None

    def owns_library(self, user_id):
        with closing(connect(self.root, readonly=True)) as db:
            row = db.execute("SELECT * FROM reader_library WHERE id='archive'").fetchone()
        if row is None or row["issuer"] != self.issuer:
            return False
        if row["clerk_user_id"]:
            return row["clerk_user_id"] == user_id
        # Email is used only to claim the preassigned library once. Never trust
        # browser data or optional JWT email claims; ask Clerk for the user record.
        from clerk_backend_api import Clerk

        try:
            with Clerk(bearer_auth=self.secret) as clerk:
                user = clerk.users.get(user_id=user_id, timeout_ms=5000)
            verified = (
                user.id == user_id
                and not user.banned
                and not user.locked
                and any(
                    email.email_address.casefold() == row["owner_email"]
                    and email.verification is not None
                    and email.verification.status == "verified"
                    for email in user.email_addresses
                )
            )
        except Exception as error:
            raise AuthenticationUnavailable() from error
        if not verified:
            return False
        with closing(connect(self.root, must_exist=True)) as db, db:
            db.execute(
                "UPDATE reader_library SET clerk_user_id=?,claimed_at=? "
                "WHERE id='archive' AND issuer=? AND owner_email=? AND clerk_user_id IS NULL",
                (user_id, now(), self.issuer, row["owner_email"]),
            )
            return (
                db.execute(
                    "SELECT clerk_user_id FROM reader_library WHERE id='archive'"
                ).fetchone()[0]
                == user_id
            )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Assign the existing archive to a verified Clerk user"
    )
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--owner-email", required=True)
    args = parser.parse_args()
    initialize_library(args.data, issuer=args.issuer, owner_email=args.owner_email)
    print("Library ownership configured; the verified owner will bind at first sign-in.")
