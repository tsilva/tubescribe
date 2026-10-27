"""Signed session verification, durable claiming, and cross-account resource isolation."""

import base64
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tubescribe.auth import ClerkAccess, initialize_library
from tubescribe.dashboard import make_server
from tubescribe.storage import Store

OWNER = "eng.tiago.silva@gmail.com"
ISSUER = "https://clerk.reader.test"
ORIGIN = "https://reader.test"


class ClerkReader(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.addCleanup(self.store.db.close)
        self.store.channel(
            {"id": "private-channel", "title": "Secret Channel", "url": "https://youtube.com/test"}
        )
        self.store.index(
            "private-channel",
            "upload",
            [
                {
                    "id": "private-video",
                    "title": "Private title",
                    "url": "https://youtube.com/watch?v=private-video",
                }
            ],
            "turbo-v1",
        )
        initialize_library(self.root, issuer=ISSUER, owner_email=OWNER)
        self.settings = {
            "CLERK_SECRET_KEY": "sk_test_fixture",
            "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY": "pk_test_"
            + base64.b64encode(b"clerk.reader.test$").decode(),
            "TUBESCRIBE_PUBLIC_ORIGIN": ORIGIN,
        }
        self.env = patch.dict(os.environ, self.settings)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = (
            self.key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )
        self.verifier = patch(
            "clerk_backend_api.security.verifytoken._get_remote_jwt_key", return_value=public
        )
        self.verifier.start()
        self.addCleanup(self.verifier.stop)
        self.users = patch("clerk_backend_api.Clerk")
        self.clerk = self.users.start()
        self.addCleanup(self.users.stop)
        self.clerk.return_value.__enter__.return_value.users.get.side_effect = self.user
        self.server = make_server(self.root)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = httpx.Client(
            base_url=f"http://127.0.0.1:{self.server.server_port}", trust_env=False
        )
        self.addCleanup(self.client.close)

    def user(self, *, user_id, **kwargs):
        email = OWNER if user_id in {"user_owner", "user_replacement"} else "other@example.test"
        return SimpleNamespace(
            id=user_id,
            banned=False,
            locked=False,
            email_addresses=[
                SimpleNamespace(
                    email_address=email, verification=SimpleNamespace(status="verified")
                )
            ],
        )

    def token(self, user="user_owner", **claims):
        current = int(time.time())
        payload = {
            "sub": user,
            "sid": "sess_test",
            "iss": ISSUER,
            "azp": ORIGIN,
            "iat": current,
            "nbf": current,
            "exp": current + 60,
            **claims,
        }
        return jwt.encode(payload, self.key, algorithm="RS256", headers={"kid": "test"})

    def headers(self, user="user_owner", **claims):
        return {"Authorization": "Bearer " + self.token(user, **claims)}

    def test_realtime_renewal_verifies_session_without_database_ownership_reads(self):
        with patch("tubescribe.auth.connect", side_effect=AssertionError("renewal polled database")):
            response = self.client.get("/api/realtime-authorization", headers=self.headers())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"user_id": "user_owner", "issuer": ISSUER})
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(self.client.get("/api/realtime-authorization").status_code, 401)
            self.assertEqual(
                self.client.get("/api/realtime-authorization", headers=self.headers(sts="pending")).status_code,
                401,
            )

    def test_realtime_configuration_exposes_no_secret_and_scopes_websocket_csp(self):
        with patch.dict(os.environ, {
            "NEON_REALTIME_URL": "wss://project.realtime.neon.tech/live",
            "NEON_REALTIME_SECRET": "test-fixture-secret",
            "VERCEL": "1",
        }):
            response = self.client.get("/auth/config")
            self.assertEqual(response.json()["realtime"], {"enabled": True, "endpoint": "/api/live"})
            self.assertNotIn("test-fixture-secret", response.text)
            page = self.client.get("/")
            self.assertIn("wss://project.realtime.neon.tech", page.headers["content-security-policy"])
            self.assertNotIn("wss:", response.text)

    def test_owner_claims_once_and_new_users_have_an_empty_feed(self):
        owner = self.client.get("/api/feed", headers=self.headers())
        self.assertEqual(owner.status_code, 200)
        self.assertEqual(owner.json()["jobs"][0]["title"], "Private title")
        self.assertEqual(owner.headers["cache-control"], "no-store")
        self.assertEqual(owner.headers["vercel-cdn-cache-control"], "no-store")
        self.assertEqual(
            self.store.db.execute("SELECT clerk_user_id FROM reader_library").fetchone()[0],
            "user_owner",
        )
        with (
            patch(
                "tubescribe.dashboard.ArchiveViews",
                side_effect=AssertionError("Read other account's archive"),
            ),
            patch("tubescribe.storage.Store", side_effect=AssertionError("Read objects")),
        ):
            for user in ("user_other", "user_replacement"):
                response = self.client.get("/api/feed", headers=self.headers(user))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["summary"], {"channels": 0, "videos": 0})
                self.assertEqual(response.json()["jobs"], [])
                self.assertEqual(
                    self.client.get("/api/library", headers=self.headers(user)).json()["total"], 0
                )
                self.assertEqual(
                    self.client.get("/api/summaries", headers=self.headers(user)).json()["items"],
                    [],
                )
                self.assertIn(
                    "No videos yet", self.client.get("/library/", headers=self.headers(user)).text
                )
        # Email reassignment cannot transfer the already bound archive.
        self.assertEqual(
            self.store.db.execute("SELECT clerk_user_id FROM reader_library").fetchone()[0],
            "user_owner",
        )

    def test_anonymous_and_other_users_cannot_read_any_content_format(self):
        self.client.get("/api/session", headers=self.headers())
        paths = [
            "/api/videos/private-video",
            "/videos/private-video",
            "/videos/private-video/transcripts/turbo-v1/",
            "/videos/private-video/transcripts/turbo-v1/transcript.json",
            "/videos/private-video/transcripts/turbo-v1/processed.md",
            "/videos/private-video/transcripts/turbo-v1/source.srt",
            "/videos/private-video/transcripts/turbo-v1/cover.png",
            "/videos/private-video/transcripts/turbo-v1/figures/frame_1.png",
            "/videos/private-video/transcripts/turbo-v1/processed-with-figures.zip",
            "/summaries/",
            "/channels/private-channel/summary/summary.md",
            "/artifacts/1/summary/",
            "/processed/1/preview.html",
            "/transcripts/1/srt",
            "/api/snapshot",
            "/api/file?path=transcripts/secret.md",
        ]
        for path in paths:
            for method in (self.client.get, self.client.head):
                with self.subTest(path=path, method=method.__name__):
                    denied = method(path)
                    self.assertEqual(denied.status_code, 401)
                    self.assertEqual(denied.headers["cache-control"], "no-store")
                    other = method(path, headers=self.headers("user_other"))
                    self.assertEqual(other.status_code, 404)
                    self.assertEqual(other.headers["cache-control"], "no-store")
        navigation = self.client.get(paths[1], headers={"Accept": "text/html"})
        self.assertEqual(navigation.status_code, 302)
        self.assertTrue(navigation.headers["location"].startswith("/?return_to="))

    def test_real_signatures_expiry_issuer_party_and_cookie_are_checked(self):
        for claims in (
            {"exp": int(time.time()) - 120},
            {"iss": "https://other.clerk.test"},
            {"azp": "https://evil.test"},
            {"sid": None},
            {"nbf": int(time.time()) + 120},
            {"sts": "pending"},
        ):
            with self.subTest(claims=claims):
                self.assertEqual(
                    self.client.get("/api/feed", headers=self.headers(**claims)).status_code, 401
                )
        forged = jwt.encode(
            {"sub": "user_owner", "exp": int(time.time()) + 60}, "attacker" * 8, algorithm="HS256"
        )
        self.assertEqual(
            self.client.get("/api/feed", headers={"Authorization": "Bearer " + forged}).status_code,
            401,
        )
        response = self.client.get("/api/session", headers={"Cookie": "__session=" + self.token()})
        self.assertEqual(response.json(), {"user_id": "user_owner", "has_library": True})
        self.assertEqual(
            self.client.get(
                "/api/feed", headers={"Authorization": "Bearer ak_untrusted"}
            ).status_code,
            401,
        )

    def test_unverified_email_and_token_email_claim_cannot_claim_library(self):
        self.clerk.return_value.__enter__.return_value.users.get.return_value = None
        self.clerk.return_value.__enter__.return_value.users.get.side_effect = lambda **kwargs: (
            SimpleNamespace(
                id="user_owner",
                banned=False,
                locked=False,
                email_addresses=[
                    SimpleNamespace(
                        email_address=OWNER, verification=SimpleNamespace(status="unverified")
                    )
                ],
            )
        )
        result = self.client.get("/api/session", headers=self.headers(email=OWNER))
        self.assertEqual(result.json()["has_library"], False)
        self.assertIsNone(
            self.store.db.execute("SELECT clerk_user_id FROM reader_library").fetchone()[0]
        )
        with self.assertRaises(ValueError):
            initialize_library(self.root, issuer=ISSUER, owner_email="attacker@example.test")

    def test_portable_backup_retains_owner_binding(self):
        from tubescribe.backup import export_archive, restore_archive

        self.client.get("/api/session", headers=self.headers())
        with tempfile.TemporaryDirectory() as folder:
            backup = Path(folder) / "backup"
            export_archive(self.store, backup)
            target = Store(Path(folder) / "restored")
            try:
                restore_archive(target, backup)
                self.assertEqual(
                    target.db.execute("SELECT clerk_user_id FROM reader_library").fetchone()[0],
                    "user_owner",
                )
                self.assertEqual(target.db.execute("SELECT COUNT(*) FROM videos").fetchone()[0], 1)
            finally:
                target.db.close()

    def test_provider_outage_and_missing_configuration_fail_closed(self):
        with patch(
            "clerk_backend_api.security.authenticate_request",
            side_effect=RuntimeError("secret must not be exposed"),
        ):
            response = self.client.get("/api/feed", headers=self.headers())
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("secret", response.text)
        with patch.dict(os.environ, {"CLERK_SECRET_KEY": ""}):
            with self.assertRaises(ValueError):
                ClerkAccess(self.root, [ORIGIN], required=True)
        config = self.client.get("/auth/config").json()
        self.assertNotIn("CLERK_SECRET_KEY", str(config))
        self.assertNotIn(OWNER, str(config))
        self.assertIn('id="auth-panel"', self.client.get("/").text)
        self.assertIn('class="workspace" hidden', self.client.get("/").text)
