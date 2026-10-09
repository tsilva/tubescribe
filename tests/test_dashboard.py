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
            result = self.client.get("/api/snapshot").json()
            self.assertTrue(result["worker"]["running"])
            self.assertEqual(result["summary"]["videos"], 4)
            self.assertEqual(result["summary"]["complete"], 1)
            self.assertEqual(result["summary"]["queued"], 1)
            self.assertEqual(result["summary"]["transcribing"], 1)
            self.assertEqual(result["summary"]["failed"], 1)
            failed = next(j for j in result["jobs"] if j["video_id"] == "failed")
            self.assertEqual(failed["error"], "GPU busy")
            self.assertEqual(failed["next_retry"], 123)
            saved = next(j for j in result["jobs"] if j["video_id"] == "saved")
            self.assertEqual({f["format"] for f in saved["files"]}, {"md", "srt", "txt", "json"})
        self.assertFalse(snapshot(self.root)["worker"]["running"])

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

    def test_preview_download_and_removed_file(self):
        file = next(f for f in snapshot(self.root)["files"] if f["format"] == "md")
        response = self.client.get("/api/preview", params={"path": file["path"]})
        self.assertEqual(response.json()["content"], "**Speaker:** A saved transcript.\n")
        self.assertFalse(response.json()["truncated"])
        response = self.client.get(file["url"] + "&download=1")
        self.assertEqual(response.content, (self.root / file["path"]).read_bytes())
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        (self.root / file["path"]).unlink()
        self.assertEqual(self.client.get(file["url"]).status_code, 404)

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

    def test_missing_db_visible_and_preview_is_bounded(self):
        (self.root / "subtitles").mkdir()
        (self.root / "subtitles/large.txt").write_bytes(b"x" * (2 * 1024**2 + 5))
        preview = self.client.get("/api/preview", params={"path": "subtitles/large.txt"}).json()
        self.assertTrue(preview["truncated"])
        self.assertEqual(len(preview["content"]), 2 * 1024**2)
        with tempfile.TemporaryDirectory() as folder:
            with make_server(Path(folder)) as empty:
                thread = threading.Thread(target=empty.serve_forever, daemon=True)
                thread.start()
                try:
                    response = httpx.get(
                        f"http://127.0.0.1:{empty.server_port}/api/snapshot", trust_env=False
                    )
                    self.assertEqual(response.status_code, 503)
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
