"""Resource links survive publication changes and deliver crawlable source text."""

import threading
import unittest
from unittest.mock import patch

import httpx
import test_artifacts as artifact_fixture

from tubescribe.artifacts import build_one
from tubescribe.dashboard import make_server, snapshot
from tubescribe.resources import artifact_url, transcript_url


class ResourceRoutes(unittest.TestCase):
    def setUp(self):
        self.fixture = artifact_fixture.Artifacts()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.job = self.fixture.fixture.job
        self.base = transcript_url(self.job["video_id"], self.job["profile"])
        self.summary = artifact_url(self.job["video_id"], self.job["profile"], "summary")
        self.server = make_server(self.store.root, instance="resource-test-token")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = httpx.Client(
            base_url=f"http://127.0.0.1:{self.server.server_port}", trust_env=False
        )
        self.addCleanup(self.client.close)

    def test_html_json_head_and_downloads_have_stable_identity(self):
        page = self.client.get(self.base)
        self.assertEqual(page.status_code, 200)
        self.assertIn(artifact_fixture.QUOTE, page.text)
        self.assertNotIn("<script", page.text)
        self.assertIn('rel="canonical"', page.text)
        self.assertIn(self.base, page.headers["link"])
        self.assertIn(self.summary, page.text)
        head = self.client.head(self.base)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b"")
        self.assertEqual(head.headers["content-length"], page.headers["content-length"])
        data = self.client.get(self.base, headers={"Accept": "application/json"}).json()
        self.assertEqual(data["profile"], self.job["profile"])
        self.assertEqual(data["links"]["self"], str(self.client.base_url).rstrip("/") + self.base)
        self.assertNotIn("output_dir", data)
        self.assertNotIn("json_path", data)
        self.assertEqual(self.client.get(data["links"]["markdown"]).status_code, 200)
        self.assertEqual(
            self.client.get(data["links"]["raw_json"]).content,
            self.store.read_key(self.store.transcript(self.job["id"])["json_path"]),
        )
        missing_slash = self.client.get(self.base.rstrip("/"))
        self.assertEqual(missing_slash.status_code, 308)
        self.assertEqual(missing_slash.headers["location"], self.base)
        api = self.client.get("/api" + self.base.rstrip("/"))
        self.assertEqual(api.json(), data)
        video = self.client.get("/api/videos/" + self.job["video_id"]).json()
        self.assertIn(
            {"profile": self.job["profile"], "url": data["links"]["self"]}, video["transcripts"]
        )
        self.assertEqual(self.client.get("/videos/" + self.job["video_id"]).status_code, 200)

    def test_summary_and_figures_keep_urls_after_regeneration(self):
        job = next(j for j in snapshot(self.store.root)["jobs"] if j["job_id"] == self.job["id"])
        self.assertEqual(job["cover_url"], self.base + "figures/frame_0001000.png")
        self.assertEqual(self.client.get(job["cover_url"]).content, artifact_fixture.PNG)
        self.fixture.build()
        before = self.fixture.row()["output_dir"]
        page = self.client.get(self.summary)
        self.assertEqual(page.status_code, 200)
        self.assertIn("A useful summary", page.text)
        self.assertIn('href="' + self.base + '"', page.text)
        self.assertEqual(
            self.client.get(self.summary + "figures/frame_0001000.png").content,
            artifact_fixture.PNG,
        )
        markdown = self.client.get(self.summary + "artifact.md")
        self.assertIn("(" + self.base + ")", markdown.text)
        self.assertTrue(
            build_one(
                self.store,
                self.job["id"],
                "summary",
                bridge=self.fixture.bridge,
                force=True,
            )
        )
        self.assertNotEqual(before, self.fixture.row()["output_dir"])
        self.assertEqual(self.client.get(self.summary).status_code, 200)
        job = next(j for j in snapshot(self.store.root)["jobs"] if j["job_id"] == self.job["id"])
        self.assertEqual(job["cover_url"], self.summary + "figures/frame_0001000.png")
        self.assertEqual(self.client.get(job["cover_url"]).content, artifact_fixture.PNG)
        data = self.client.get(self.summary, headers={"Accept": "application/json"}).json()
        self.assertEqual(data["state"], "complete")
        self.assertTrue(data["links"]["markdown"].endswith(self.summary + "artifact.md"))
        self.assertEqual(self.client.get(f"/artifacts/{self.job['id']}/summary/").status_code, 200)
        self.assertEqual(snapshot(self.store.root)["jobs"][0]["summary_url"], self.summary)

    def test_library_reads_never_schedule_processing(self):
        ready = self.client.get(self.summary)
        self.assertNotIn("<form", ready.text)
        self.assertNotIn("Build artifact", ready.text)
        before = self.store.db.execute("SELECT count(*) FROM submissions").fetchone()[0]
        for path in (self.summary, self.summary + "run", self.base, "/api/library"):
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                response = self.client.request(method, path, json={"token": "resource-test-token"})
                self.assertEqual(response.status_code, 405)
                self.assertEqual(response.headers["allow"], "GET, HEAD")
        self.assertEqual(before, self.store.db.execute("SELECT count(*) FROM submissions").fetchone()[0])
        for path in ("/api/snapshot", "/api/ready", "/api/metrics", "/metrics", "/api/file", "/api/preview", "/artifact.js"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_agent_can_discover_saved_sources_without_javascript(self):
        self.fixture.build()
        guide = self.client.get("/llms.txt")
        self.assertIn("/api/library", guide.text)
        page = self.client.get("/library/")
        self.assertIn(self.base, page.text)
        self.assertIn(self.summary + "artifact.md", page.text)
        self.assertNotIn("<script", page.text)
        data = self.client.get("/api/library", params={"limit": 1}).json()
        self.assertEqual(data["total"], 1)
        self.assertIsNone(data["links"]["next"])
        video = data["items"][0]
        self.assertEqual(video["video_id"], self.job["video_id"])
        links = video["transcripts"][0]["links"]
        for link in links.values():
            self.assertEqual(self.client.get(link).status_code, 200, link)
        for key in ("output_dir", "error", "attempts", "audio_path", "pid", "input_hash"):
            self.assertNotIn('"' + key + '"', self.client.get("/api/library").text)
        self.assertEqual(self.client.get("/api/library?offset=1").json()["items"], [])
        self.assertEqual(self.client.get("/api/library?q=does-not-exist").json()["items"], [])
        self.assertEqual(self.client.get("/api/library?channel=unknown").json()["items"], [])
        for query in ("limit=0", "limit=101", "offset=-1", "offset=no"):
            self.assertEqual(self.client.get("/api/library?" + query).status_code, 400)
        head = self.client.head("/api/library")
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b"")

    def test_feed_cover_reads_only_the_issued_image_with_hash_validation(self):
        from unittest.mock import patch

        with self.store.db:
            self.store.db.execute("UPDATE postprocessings SET figures=1 WHERE job_id=?", (self.job["id"],))
        # Covers are discovered lazily from the published manifest, never an R2 inventory.
        with patch("tubescribe.dashboard.output_files", side_effect=AssertionError("R2 scan")):
            feed = self.client.get("/api/feed").json()
        job = next(j for j in feed["jobs"] if j["video_id"] == self.job["video_id"])
        self.assertIn("/cover.png?v=", job["cover_url"])
        response = self.client.get(job["cover_url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, artifact_fixture.PNG)
        self.assertIn("s-maxage", response.headers["cache-control"])
        row = self.store.db.execute("SELECT output_dir FROM postprocessings WHERE job_id=?", (self.job["id"],)).fetchone()
        path = self.store.root / row["output_dir"] / "figures/frame_0001000.png"
        original = path.read_bytes()
        path.write_bytes(b"tampered")
        try:
            self.assertEqual(self.client.get(job["cover_url"]).status_code, 400)
        finally:
            path.write_bytes(original)

    def test_raw_sources_and_subtitles_work_before_processing(self):
        self.store.stage_update("postprocessing", self.job["id"], state="pending")
        page = self.client.get(self.base)
        self.assertEqual(page.status_code, 200)
        self.assertIn("A real transcript", page.text)
        self.assertIn('href="transcript.md"', page.text)
        self.assertIn('href="subtitles/"', page.text)
        srt = self.client.get(self.base + "subtitles/")
        self.assertEqual(srt.status_code, 200)
        self.assertIn(self.base + "source.srt", srt.text)
        self.assertIn(
            "https://www.youtube.com/watch?v=" + self.job["video_id"] + "&amp;t=", srt.text
        )
        self.assertEqual(self.client.get(self.base + "processed.md").status_code, 404)

    def test_identity_traversal_auth_and_unknown_resources_are_guarded(self):
        for path in [
            "/videos/missing/transcripts/default/",
            self.base + "response.json",
            self.summary + "response.json",
            self.base.replace(self.job["profile"], "bad%2Fprofile"),
            self.base + "figures/../response.json",
        ]:
            self.assertEqual(self.client.get(path).status_code, 404)
        with patch.dict(
            "os.environ",
            {
                "TUBESCRIBE_ACCESS_TOKEN": "a" * 48,
                "TUBESCRIBE_PUBLIC_ORIGIN": "https://sources.example/",
            },
        ):
            secured = make_server(self.store.root)
        threading.Thread(target=secured.serve_forever, daemon=True).start()
        self.addCleanup(secured.server_close)
        self.addCleanup(secured.shutdown)
        url = f"http://127.0.0.1:{secured.server_port}" + self.base
        self.assertEqual(httpx.get(url, trust_env=False).status_code, 401)
        metadata = httpx.get(
            url,
            headers={"Authorization": "Bearer " + "a" * 48, "Accept": "application/json"},
            trust_env=False,
        ).json()
        self.assertEqual(metadata["links"]["self"], "https://sources.example" + self.base)

        self.assertEqual(
            httpx.get(
                url, headers={"Authorization": "Bearer " + "a" * 48}, trust_env=False
            ).status_code,
            200,
        )
