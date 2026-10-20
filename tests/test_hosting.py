import os
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx

from tubescribe.hosting import hosted_handler
from tubescribe.s3 import S3Artifacts


class Hosting(unittest.TestCase):
    def test_remote_feed_uses_paged_metadata_without_head_requests(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from tubescribe.dashboard import output_files

        client = Mock()
        client.get_paginator.return_value.paginate.return_value = [
            {
                "Contents": [
                    {
                        "Key": "transcripts/one.md",
                        "Size": 10,
                        "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
                    }
                ]
            },
            {
                "Contents": [
                    {
                        "Key": "transcripts/two.srt",
                        "Size": 20,
                        "LastModified": datetime(2026, 1, 2, tzinfo=UTC),
                    }
                ]
            },
        ]
        archive = SimpleNamespace(
            local_blobs=False, blobs=S3Artifacts("private", client=client), db=Mock()
        )
        with patch("tubescribe.storage.Store", return_value=archive):
            files = output_files(Path("/unused"))
        self.assertEqual(
            [(f["path"], f["bytes"]) for f in files],
            [("transcripts/one.md", 10), ("transcripts/two.srt", 20)],
        )
        client.head_object.assert_not_called()

    def environment(self, folder):
        return {
            "TUBESCRIBE_DATA": folder,
            "DATABASE_URL_UNPOOLED": "postgresql://unused:unused@localhost/archive",
            "TUBESCRIBE_S3_BUCKET": "private-test",
            "TUBESCRIBE_S3_ENDPOINT": "https://objects.example.test",
            "VERCEL_URL": "tubescribe-preview.vercel.app",
            "VERCEL_PROJECT_PRODUCTION_URL": "tubescribe.vercel.app",
        }

    def test_hosted_roles_fail_closed_without_durable_backends(self):
        with tempfile.TemporaryDirectory() as folder:
            for missing in (
                "DATABASE_URL_UNPOOLED",
                "TUBESCRIBE_S3_BUCKET",
            ):
                settings = self.environment(folder)
                del settings[missing]
                with self.subTest(missing=missing), patch.dict(os.environ, settings, clear=True):
                    with self.assertRaises(ValueError):
                        hosted_handler()
                self.assertFalse((Path(folder) / "archive.sqlite3").exists())

    def test_hosted_roles_reject_local_processing_or_runtime_migrations(self):
        with tempfile.TemporaryDirectory() as folder:
            for key, value in (
                ("TUBESCRIBE_DISPATCH", "process"),
                ("TUBESCRIBE_MIGRATIONS", "auto"),
            ):
                with (
                    self.subTest(key=key),
                    patch.dict(os.environ, {**self.environment(folder), key: value}, clear=True),
                    self.assertRaises(ValueError),
                ):
                    hosted_handler()

    def test_vercel_aliases_share_routes_without_starting_processors(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, self.environment(folder), clear=True),
            patch("subprocess.Popen", side_effect=AssertionError("Hosted role spawned a process")),
        ):
            handler = hosted_handler()
            with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with httpx.Client(
                        base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False
                    ) as client:
                        for host in ("tubescribe.vercel.app", "tubescribe-preview.vercel.app"):
                            response = client.get("/", headers={"Host": host})
                            self.assertEqual(response.status_code, 200)
                            self.assertIn("Your library", response.text)
                            self.assertEqual(client.head("/", headers={"Host": host}).content, b"")
                        self.assertEqual(
                            client.get("/", headers={"Host": "evil.test"}).status_code, 403
                        )
                        navigation = {
                            "Host": "tubescribe.vercel.app",
                            "Sec-Fetch-Site": "cross-site",
                            "Sec-Fetch-Mode": "navigate",
                            "Sec-Fetch-Dest": "document",
                        }
                        self.assertEqual(client.get("/", headers=navigation).status_code, 200)
                        self.assertEqual(client.post("/", headers=navigation).status_code, 403)
                        self.assertEqual(
                            client.get(
                                "/", headers={**navigation, "Sec-Fetch-Mode": "cors"}
                            ).status_code,
                            403,
                        )
                        self.assertEqual(
                            client.post(
                                "/",
                                headers={
                                    "Host": "tubescribe.vercel.app",
                                    "Origin": "https://evil.test",
                                },
                            ).status_code,
                            403,
                        )
                finally:
                    server.shutdown()
                    thread.join()
            self.assertFalse((Path(folder) / "archive.sqlite3").exists())
