import json
import tempfile
import threading
import unittest
from pathlib import Path

import httpx

from tubescribe.dashboard import dashboard_status, make_server, snapshot
from tubescribe.storage import Store, worker_lock


class Dashboard(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.store.channel(
            {"id": "channel", "title": "Test channel", "url": "https://youtube.com/test"}
        )
        self.store.index(
            "channel",
            "upload",
            [
                {
                    "id": "saved",
                    "title": "Saved <script> video",
                    "url": "https://youtube.com/watch?v=saved",
                },
                {"id": "pending", "title": "Pending", "url": "https://youtube.com/watch?v=pending"},
                {"id": "active", "title": "Active", "url": "https://youtube.com/watch?v=active"},
                {"id": "failed", "title": "Failed", "url": "https://youtube.com/watch?v=failed"},
            ],
            "turbo-v1",
        )
        saved = dict(
            self.store.db.execute(
                "SELECT j.*,v.channel_id,v.title,v.url,v.kind FROM jobs j JOIN videos v ON j.video_id=v.id WHERE v.id='saved'"
            ).fetchone()
        )
        self.store.save(
            saved,
            {
                "language": "en",
                "model": "large-v3-turbo",
                "revision": "pinned",
                "processing_seconds": 1,
                "duration": 3,
                "segments": [{"start": 0, "end": 3, "text": "A saved transcript."}],
            },
        )
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO diarizations(job_id,state,stage,updated_at,speaker_count) VALUES(?,'complete','complete','now',1)",
                (saved["id"],),
            )
            self.store.db.execute(
                "UPDATE jobs SET state='active',download_state='complete',asr_state='active' WHERE video_id='active'"
            )
            self.store.db.execute(
                "UPDATE jobs SET state='failed',failed_stage='asr',error='GPU busy',next_retry=123 WHERE video_id='failed'"
            )
        self.server = make_server(self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = httpx.Client(
            base_url=f"http://127.0.0.1:{self.server.server_port}", trust_env=False
        )

    def tearDown(self):
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.db.close()
        self.temp.cleanup()

    def test_snapshot_live_read_is_consistent_without_blocking_worker(self):
        with worker_lock(self.root):
            result = snapshot(self.root)
            self.assertTrue(result["worker"]["running"])
            self.assertEqual(result["summary"]["videos"], 4)
            self.assertEqual(result["summary"]["complete"], 0)
            self.assertEqual(result["summary"]["awaiting_postprocessing"], 1)
            self.assertEqual(result["summary"]["queued"], 1)
            self.assertEqual(result["summary"]["transcribing"], 1)
            self.assertEqual(result["summary"]["failed"], 1)
            failed = next(j for j in result["jobs"] if j["video_id"] == "failed")
            self.assertEqual(failed["error"], "GPU busy")
            self.assertEqual(failed["next_retry"], 123)
            saved = next(j for j in result["jobs"] if j["video_id"] == "saved")
            self.assertEqual({f["format"] for f in saved["files"]}, {"md", "srt", "txt", "json"})
        self.assertFalse(snapshot(self.root)["worker"]["running"])

    def test_published_summary_link_survives_empty_web_scratch(self):
        job_id = self.store.db.execute("SELECT id FROM jobs WHERE video_id='saved'").fetchone()[0]
        self.store.ensure_stage("artifact", job_id, workflow="summary")
        self.store.stage_update(
            "artifact",
            job_id,
            workflow="summary",
            state="complete",
            output_dir="artifacts/published-remote/summary",
            manifest_sha256="a" * 64,
        )
        self.assertFalse((self.root / "artifacts/published-remote/summary").exists())
        from unittest.mock import patch

        with patch(
            "tubescribe.storage.Store.materialize_prefix",
            side_effect=AssertionError("Listing must not download publication bundles"),
        ):
            saved = next(j for j in snapshot(self.root)["jobs"] if j["video_id"] == "saved")
        self.assertIn("/artifacts/summary/", saved["summary_url"])

    def test_videos_are_unique_across_profiles_and_empty_channels_visible(self):
        self.store.index(
            "channel",
            "upload",
            [{"id": "saved", "title": "Saved", "url": "https://youtube.com/watch?v=saved"}],
            "future-model",
        )
        self.store.channel({"id": "empty", "title": "Empty", "url": "https://youtube.com/empty"})
        result = snapshot(self.root)
        self.assertEqual(result["summary"]["videos"], 4)
        self.assertEqual(result["summary"]["jobs"], 5)
        self.assertEqual(result["summary"]["channels"], 2)
        self.assertEqual(next(c for c in result["channels"] if c["id"] == "empty")["videos"], 0)

    def test_backlog_is_only_scheduled_by_a_waiting_work_submission(self):
        from tubescribe.submission import JobRequest

        self.assertFalse(any(job["scheduled"] for job in snapshot(self.root)["jobs"]))
        self.store.index(
            "channel",
            "upload",
            [
                {
                    "id": "newer",
                    "title": "Newer",
                    "url": "https://youtube.com/watch?v=newer",
                    "published_at": "100",
                }
            ],
            "turbo-v1",
        )
        self.store.index(
            "channel",
            "upload",
            [{"id": "pending", "title": "Pending", "url": "https://youtube.com/watch?v=pending"}],
            "other-profile",
        )
        request = JobRequest("work", {"video": ["pending", "newer"], "limit": 1})
        identifier, _ = self.store.enqueue(request.encode(), request.dedupe)
        queued = snapshot(self.root)["jobs"]
        self.assertEqual(
            {(job["video_id"], job["profile"]) for job in queued if job["scheduled"]},
            {("newer", "turbo-v1")},
        )
        self.store.update_submission(identifier, state="complete")
        self.assertFalse(any(job["scheduled"] for job in snapshot(self.root)["jobs"]))

    def test_unrelated_channel_request_does_not_schedule_the_backlog(self):
        from tubescribe.submission import JobRequest

        request = JobRequest("work", {"channel": "another-channel"})
        self.store.enqueue(request.encode(), request.dedupe)
        self.assertFalse(any(job["scheduled"] for job in snapshot(self.root)["jobs"]))

    def test_feed_changes_only_transmit_changed_records_and_remove_old_keys(self):
        first = self.client.get("/api/feed").json()
        self.assertTrue(first["full"])
        self.assertEqual(len(first["jobs"]), 4)
        self.assertNotIn("files", first)
        self.assertNotIn("files", first["jobs"][0])
        unchanged = self.client.get("/api/feed", params={"revision": first["revision"]}).json()
        self.assertFalse(unchanged["full"])
        self.assertEqual(unchanged["jobs"], [])
        self.assertEqual(unchanged["removed"], [])
        self.assertEqual(unchanged["revision"], first["revision"])
        with self.store.db:
            self.store.db.execute("UPDATE videos SET title='Updated title' WHERE id='pending'")
        changed = self.client.get("/api/feed", params={"revision": first["revision"]}).json()
        self.assertEqual([job["video_id"] for job in changed["jobs"]], ["pending"])
        self.assertEqual(changed["jobs"][0]["title"], "Updated title")
        old_key = changed["jobs"][0]["key"]
        with self.store.db:
            self.store.db.execute("DELETE FROM jobs WHERE video_id='pending'")
        removed = self.client.get("/api/feed", params={"revision": changed["revision"]}).json()
        self.assertEqual(removed["removed"], [old_key])
        self.assertEqual(removed["jobs"][0]["key"], "video:pending")
        reset = self.client.get("/api/feed", params={"revision": "unknown-server-revision"}).json()
        self.assertTrue(reset["full"])
        self.assertEqual(len(reset["jobs"]), 4)

    def test_feed_never_scans_blob_storage_or_operational_state(self):
        from unittest.mock import patch

        with (
            patch("tubescribe.dashboard.output_files", side_effect=AssertionError("R2 scan")),
            patch("tubescribe.dashboard.recent_events", side_effect=AssertionError("Event scan")),
            patch("tubescribe.storage.Store", side_effect=AssertionError("Archive writer")),
        ):
            response = self.client.get("/api/feed")
        self.assertEqual(response.status_code, 200)
        saved = next(j for j in response.json()["jobs"] if j["video_id"] == "saved")
        self.assertIn("transcript_url", saved)
        self.assertIn("srt_url", saved)

    def test_unchanged_revision_survives_a_different_function_instance(self):
        from tubescribe.dashboard import FeedChanges

        first = self.client.get("/api/feed").json()
        # The next function instance has none of the first instance's revision history.
        next_instance = FeedChanges(self.root)
        response = next_instance.get(first["revision"])
        self.assertEqual(response["revision"], first["revision"])
        self.assertFalse(response["full"])
        self.assertEqual(response["jobs"], [])
        self.assertEqual(response["removed"], [])

    def test_public_cache_is_bounded_and_never_caches_identity_or_errors(self):
        self.assertIn("s-maxage=3600", self.client.get("/").headers["cache-control"])
        feed = self.client.get("/api/feed")
        self.assertIn("s-maxage=5", feed.headers["cache-control"])
        self.assertEqual(self.client.get("/api/feed?revision=x").headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/missing").headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/", headers={"Authorization": "Bearer anything"}).headers["cache-control"], "no-store")

    def test_feed_compresses_only_when_the_client_accepts_gzip(self):
        response = self.client.get("/api/feed", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(response.headers["Content-Encoding"], "gzip")
        self.assertIn("Accept-Encoding", response.headers["Vary"])
        self.assertTrue(response.json()["full"])
        plain = self.client.get("/api/feed", headers={"Accept-Encoding": "identity"})
        self.assertNotIn("Content-Encoding", plain.headers)
        self.assertEqual(len(plain.json()["jobs"]), 4)

    def test_only_canonical_published_downloads_are_served(self):
        job = next(j for j in snapshot(self.root)["jobs"] if j["video_id"] == "saved")
        url = job["transcript_url"] + "transcript.md"
        response = self.client.get(url)
        self.assertEqual(response.content, b"**Speaker:** A saved transcript.\n")
        for path in ("/api/file", "/api/preview", "/api/snapshot"):
            self.assertEqual(self.client.get(path).status_code, 404)
        Path(self.root / next(f["path"] for f in job["files"] if f["format"] == "md")).unlink()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_srt_viewer_is_available_before_postprocessing_and_links_exact_youtube_seconds(self):
        job = next(j for j in snapshot(self.root)["jobs"] if j["video_id"] == "saved")
        path = (self.root / job["json_path"]).with_suffix(".srt")
        original = (
            b"1\n00:00:00,125 --> 00:00:02,000\nSpeaker 1: <script>alert(1)</script>\n\n"
            b"2\n01:02:03,999 --> 01:02:05,010\nSpeaker 2: Hello\nSecond line\n"
        )
        path.write_bytes(original)
        response = self.client.get(job["srt_url"])
        self.assertEqual(response.status_code, 200)
        self.assertIn("https://www.youtube.com/watch?v=saved&amp;t=0s", response.text)
        self.assertIn("https://www.youtube.com/watch?v=saved&amp;t=3723s", response.text)
        self.assertIn("01:02:03,999", response.text)
        self.assertIn("Speaker 2: Hello\nSecond line", response.text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", response.text)
        self.assertIn("Saved &lt;script&gt; video", response.text)
        self.assertNotIn("<script>", response.text)
        self.assertNotIn("Processed transcript", response.text)
        self.assertIn('rel="noopener noreferrer"', response.text)
        self.assertIn("script-src 'none'", response.headers["content-security-policy"])
        self.assertIn('aria-label="Open on YouTube" title="Open on YouTube"', response.text)
        self.assertIn('aria-label="Download SRT" title="Download SRT"', response.text)
        self.assertNotIn(">Open on YouTube</a>", response.text)
        self.assertNotIn(">Download SRT</a>", response.text)
        self.assertEqual(self.client.get("/icons/arrow-down-tray.svg").status_code, 200)
        self.assertEqual(self.client.get(job["transcript_url"] + "source.srt").content, original)
        self.assertEqual(self.client.get("/transcripts/99999/srt").status_code, 404)
        path.unlink()
        self.assertEqual(self.client.get(job["srt_url"]).status_code, 404)

    def test_asr_saved_but_diarization_incomplete_is_not_shown_as_complete(self):
        with self.store.db:
            self.store.db.execute(
                "UPDATE diarizations SET state='active',stage='diarization' WHERE job_id=(SELECT id FROM jobs WHERE video_id='saved')"
            )
        result = snapshot(self.root)
        self.assertEqual(result["summary"]["asr_complete"], 1)
        self.assertEqual(result["summary"]["complete"], 0)
        self.assertEqual(result["summary"]["diarizing"], 1)
        with self.store.db:
            self.store.db.execute(
                "UPDATE diarizations SET state='failed',error='Model busy',updated_at='now' WHERE job_id=(SELECT id FROM jobs WHERE video_id='saved')"
            )
        saved = next(j for j in snapshot(self.root)["jobs"] if j["video_id"] == "saved")
        self.assertEqual(saved["stage"], "failed")
        self.assertEqual(saved["error"], "Model busy")

    def test_arbitrary_paths_symlinks_and_cross_origin_are_rejected(self):
        (self.root / "secret.json").write_text('"private config"')
        (self.root / "transcripts/secret.json").symlink_to(self.root / "secret.json")
        for path in (
            "../secret.json",
            str(self.root / "secret.json"),
            "secret.json",
            "archive.sqlite3",
            "transcripts/secret.json",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.client.get("/api/file", params={"path": path}).status_code, 404
                )
        self.assertEqual(
            self.client.get("/api/snapshot", headers={"Host": "evil.example"}).status_code, 403
        )
        self.assertEqual(
            self.client.get(
                "/api/snapshot", headers={"Origin": "https://evil.example"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/api/snapshot", headers={"Sec-Fetch-Site": "cross-site"}).status_code,
            403,
        )
        self.assertEqual(self.client.post("/api/snapshot").status_code, 405)

    def test_missing_db_visible_without_initializing_an_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            with make_server(Path(folder)) as empty:
                thread = threading.Thread(target=empty.serve_forever, daemon=True)
                thread.start()
                try:
                    response = httpx.get(f"http://127.0.0.1:{empty.server_port}/api/library", trust_env=False)
                    self.assertEqual(response.status_code, 503)
                    self.assertFalse((Path(folder) / "archive.sqlite3").exists())
                finally:
                    empty.shutdown()
                    thread.join()

    def test_stale_dashboard_state_never_authorizes_a_stop(self):
        (self.root / "dashboard.json").write_text(
            json.dumps({"pid": 123, "url": "http://127.0.0.1:1", "instance": "stale"})
        )
        state = dashboard_status(self.root)
        self.assertFalse(state["running"])
        self.assertFalse(state["ready"])

    def test_static_assets_have_local_content_security_policy(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertNotIn("unsafe-inline", response.headers["content-security-policy"])
        self.assertEqual(self.client.get("/app.js").status_code, 200)
        self.assertEqual(self.client.get("/style.css").status_code, 200)
        icon = self.client.get("/icons/magnifying-glass.svg")
        self.assertEqual(icon.status_code, 200)
        self.assertEqual(icon.headers["content-type"], "image/svg+xml")
        self.assertEqual(self.client.get("/icons/README.md").status_code, 404)
        self.assertEqual(self.client.get("/icons/../index.html").status_code, 404)

    def test_brand_assets_are_served_with_types_and_confined_paths(self):
        page = self.client.get("/").text
        self.assertIn('class="brand-mark"', page)
        self.assertIn('href="/favicon.ico"', page)
        self.assertIn('href="/brand/site.webmanifest"', page)
        for name, content_type in (
            ("logo.png", "image/png"),
            ("favicon-16.png", "image/png"),
            ("favicon-32.png", "image/png"),
            ("favicon-48.png", "image/png"),
            ("favicon.ico", "image/vnd.microsoft.icon"),
            ("apple-touch-icon.png", "image/png"),
            ("android-chrome-192.png", "image/png"),
            ("android-chrome-512.png", "image/png"),
            ("site.webmanifest", "application/manifest+json"),
        ):
            with self.subTest(name=name):
                response = self.client.get(f"/brand/{name}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], content_type)
                self.assertIn("s-maxage=3600", response.headers["cache-control"])
                self.assertEqual(int(response.headers["content-length"]), len(response.content))
                head = self.client.head(f"/brand/{name}")
                self.assertEqual(head.status_code, 200)
                self.assertEqual(head.content, b"")
                self.assertEqual(head.headers["content-length"], response.headers["content-length"])
        self.assertEqual(
            self.client.get("/favicon.ico").content, self.client.get("/brand/favicon.ico").content
        )
        manifest = self.client.get("/brand/site.webmanifest").json()
        self.assertEqual(manifest["name"], "TubeScribe")
        self.assertEqual(manifest["start_url"], "/")
        for icon in manifest["icons"]:
            self.assertEqual(self.client.get(f'/brand/{icon["src"]}').status_code, 200)
        for path in ("/brand/sources/logo-source.png", "/brand/%2e%2e/index.html", "/brand/missing.png"):
            self.assertEqual(self.client.get(path).status_code, 404)
