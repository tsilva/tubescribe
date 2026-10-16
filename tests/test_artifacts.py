"""Derived workflows cannot rerun inference or silently lose their source/resources."""

import base64
import json
import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen

import httpx
import test_postprocessing_pipeline as pipeline

from tubescribe.artifacts import build_one, digest, render_artifact, run, validate, workflow
from tubescribe.dashboard import make_server
from tubescribe.postprocessing_worker import run as postprocess
from tubescribe.storage import atomic_write
from tubescribe.worker import work

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aMWQAAAAASUVORK5CYII="
)
QUOTE = "The key argument is that structural changes weaken old signals."


class ArtifactRendering(unittest.TestCase):
    def test_blank_lines_between_summary_bullets_keep_one_list(self):
        page = render_artifact(
            "# Summary\n\n- First insight.\n\n- Second insight.\n\nFinal caveat.",
            "Summary",
            "/processed/1/preview.html",
        )
        self.assertEqual(page.count("<ul>"), 1)
        self.assertIn("<li>First insight.</li><li>Second insight.</li>", page)
        self.assertIn("</ul><p>Final caveat.</p>", page)


class Artifacts(unittest.TestCase):
    def setUp(self):
        self.fixture = pipeline.FinalStage()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.fixture.full()
        self.parent = self.store.root / self.fixture.row()["output_dir"]
        self.requests = 0
        self.invalid = None
        self.read_image = False
        self.prepare_source()
        owner = self

        class Bridge(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"paths":{"/api/v1/agent/runs":{}}}')

            def do_POST(self):
                owner.requests += 1
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.payload = payload
                if owner.read_image:
                    executor = payload["tool_executor"]
                    request = Request(
                        executor["url"],
                        data=json.dumps(
                            {
                                "tool": "view_transcript_figures",
                                "arguments": {"image_ids": ["frame_0001000"]},
                            }
                        ).encode(),
                        headers={
                            "Authorization": "Bearer " + executor["token"],
                            "Content-Type": "application/json",
                        },
                    )
                    with urlopen(request) as response:
                        result = json.load(response)
                    assert result["success"] and any(
                        i["type"] == "inputImage" for i in result["contentItems"]
                    )
                output = {
                    "markdown": "# A useful summary\n\nThe speaker argues that structural change weakens old signals.\n\n- Watch the changing relationship between rates and activity.\n- Keep forecasts separate from established facts.\n\n[Supporting chart](figure:frame_0001000) · [Full transcript](transcript)",
                    "used_image_ids": ["frame_0001000"],
                    "evidence": [
                        {"claim": "Structural change weakens old signals.", "source_quote": QUOTE}
                    ],
                }
                if not json.loads(payload["input"])["available_figures"]:
                    output["markdown"] = output["markdown"].replace(
                        "[Supporting chart](figure:frame_0001000) · ", ""
                    )
                    output["used_image_ids"] = []
                if owner.invalid == "quote":
                    output["evidence"][0]["source_quote"] = (
                        "An invented quote which never occurred in the source."
                    )
                if owner.invalid == "figure":
                    output["markdown"] += "\n\n[Unissued](figure:frame_9999000)"
                    output["used_image_ids"].append("frame_9999000")
                if owner.invalid == "long":
                    output["markdown"] = "word " * 651
                self.send_response(200)
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "output": output,
                            "elapsed_seconds": 0.1,
                            "usage": {"total": {"totalTokens": 100}},
                        }
                    ).encode()
                )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.bridge = f"http://127.0.0.1:{self.server.server_port}"

    def prepare_source(self):
        (self.parent / "processed.md").write_text(
            "# Test\n\n## Main argument\n\n"
            + QUOTE
            + "\n\nDo not treat a forecast as a fact.\n\n![Rates chart](figures/frame_0001000.png)\n"
        )
        image = self.parent / "figures/frame_0001000.png"
        image.parent.mkdir(exist_ok=True)
        image.write_bytes(PNG)
        (self.parent / "resources.json").write_text(
            json.dumps(
                {
                    "frame_0001000": {
                        "path": "figures/frame_0001000.png",
                        "sha256": digest(image),
                        "timestamp": 1,
                    }
                }
            )
        )
        self.publish_parent_manifest()

    def publish_parent_manifest(self):
        files = {
            p.relative_to(self.parent).as_posix(): digest(p)
            for p in self.parent.rglob("*")
            if p.is_file() and p.name != "manifest.json"
        }
        (self.parent / "manifest.json").write_text(json.dumps({"files": files}))
        with self.store.db:
            self.store.db.execute(
                "UPDATE postprocessings SET manifest_sha256=?",
                (digest(self.parent / "manifest.json"),),
            )

    def row(self, name="summary"):
        return dict(
            self.store.db.execute("SELECT * FROM artifacts WHERE workflow=?", (name,)).fetchone()
        )

    def build(self, **kwargs):
        return run(self.store, ids=[self.fixture.job["video_id"]], bridge=self.bridge, **kwargs)

    def test_summary_uses_native_saved_images_and_skips_when_complete(self):
        self.read_image = True
        self.assertEqual(self.build()["completed"], 1)
        output = self.store.root / self.row()["output_dir"]
        self.assertEqual(self.payload["model"], "codex/gpt-6.1-sol")
        self.assertEqual(self.payload["reasoning_effort"], "high")
        self.assertIn(QUOTE, self.payload["input"])
        self.assertEqual(self.payload["tools"][0]["function"]["name"], "view_transcript_figures")
        self.assertEqual((output / "figures/frame_0001000.png").read_bytes(), PNG)
        self.assertIn("](figure:frame_0001000)", (output / "artifact.md").read_text())
        self.assertIn("](transcript:1)", (output / "artifact.md").read_text())
        self.assertIn("<ul>", (output / "preview.html").read_text())
        self.assertIn('href="figures/frame_0001000.png"', (output / "preview.html").read_text())
        self.assertNotIn(
            self.payload["tool_executor"]["token"], (output / "request.json").read_text()
        )
        self.assertEqual(self.build()["skipped"], 1)
        self.assertEqual(self.requests, 1)
        self.assertEqual(self.fixture.transcribe.call_count, 1)
        self.assertEqual(self.fixture.requests, 1)

    def test_save_failure_and_corrupt_output_repair_without_model_or_video(self):
        def fail_output(path, content):
            if path.name == "artifact.md":
                raise OSError("disk busy")
            atomic_write(path, content)

        with patch("tubescribe.artifacts.atomic_write", side_effect=fail_output):
            self.assertEqual(self.build()["failed"], 1)
        self.assertEqual(self.row()["stage"], "save")
        self.assertEqual(self.requests, 1)
        self.assertEqual(
            run(self.store, ids=[self.fixture.job["video_id"]], bridge="http://127.0.0.1:1")[
                "completed"
            ],
            1,
        )
        output = self.store.root / self.row()["output_dir"]
        (output / "artifact.md").write_text("corrupt")
        self.assertEqual(self.build()["completed"], 1)
        self.assertEqual(self.requests, 1)
        self.assertEqual(self.fixture.transcribe.call_count, 1)

    def test_text_only_source_keeps_required_bridge_tool_without_inventing_figures(self):
        (self.parent / "resources.json").write_text("{}")
        self.publish_parent_manifest()
        self.assertEqual(self.build()["completed"], 1)
        self.assertEqual(self.payload["tools"][0]["function"]["name"], "view_transcript_figures")
        self.assertEqual(json.loads(self.payload["input"])["available_figures"], [])
        output = self.store.root / self.row()["output_dir"]
        self.assertFalse((output / "figures").exists())

    def test_changed_input_and_custom_workflow_create_new_runs(self):
        self.build()
        previous = self.row()["output_dir"]
        with (self.parent / "processed.md").open("a") as handle:
            handle.write("\nAn additional qualification matters.\n")
        self.publish_parent_manifest()
        self.assertEqual(self.build()["completed"], 1)
        self.assertNotEqual(self.row()["output_dir"], previous)
        self.assertTrue((self.store.root / previous / "artifact.md").exists())
        folder = self.store.root / "workflows"
        folder.mkdir()
        path = folder / "study-guide.json"
        path.write_text(
            json.dumps(
                {
                    "title": "Study guide",
                    "instructions": "Produce questions and answers using the source.",
                    "max_words": 1200,
                }
            )
        )
        self.assertEqual(self.build(name="study-guide")["completed"], 1)
        old = self.row("study-guide")["workflow_sha256"]
        definition = json.loads(path.read_text())
        definition["instructions"] += " Include examples."
        path.write_text(json.dumps(definition))
        self.assertEqual(self.build(name="study-guide")["completed"], 1)
        self.assertNotEqual(self.row("study-guide")["workflow_sha256"], old)

    def test_rejects_fabricated_evidence_unissued_figures_and_overlong_summary(self):
        for invalid, match in [
            ("quote", "evidence quote"),
            ("figure", "figure IDs"),
            ("long", "exceeds"),
        ]:
            self.invalid = invalid
            self.assertEqual(self.build(force=True)["failed"], 1)
            self.assertIn(match, self.row()["error"])
            self.assertIsNone(self.row()["response_sha256"])
        self.invalid = None
        self.assertEqual(self.build(force=True)["completed"], 1)

    def test_workflow_and_input_integrity(self):
        with self.assertRaisesRegex(ValueError, "path-safe"):
            workflow(self.store.root, "../summary")
        (self.parent / "processed.md").write_text("corrupt")
        with self.assertRaisesRegex(ValueError, "corrupt"):
            build_one(self.store, self.fixture.job["id"], "summary", bridge=self.bridge)
        self.assertEqual(self.requests, 0)

    def test_rejected_durable_response_starts_fresh_run_with_bounded_attempts(self):
        directories = []
        instructions = []

        def configured_runner(store, bridge, key):
            directory = store.root / key
            directories.append(directory)

            def response(payload, *_):
                instructions.append(payload["instructions"])
                journal = directory / "agent-run.json"
                if journal.exists():
                    return json.loads(journal.read_text())
                result = {
                    "output": {
                        "markdown": "# Summary\n\nStructural changes weaken old signals.",
                        "used_image_ids": [],
                        "evidence": [{"claim": "Signals change.", "source_quote": QUOTE}],
                    }
                }
                if len(directories) == 1:
                    result["output"]["evidence"][0]["source_quote"] = (
                        "An invented quote absent from the source."
                    )
                journal.write_text(json.dumps(result))
                return result

            return Mock(run=Mock(side_effect=response))

        with patch("tubescribe.inference.configured_runner", side_effect=configured_runner):
            with self.assertRaisesRegex(ValueError, "evidence quote"):
                build_one(self.store, self.fixture.job["id"], "summary", bridge=self.bridge)
            self.assertEqual(self.row()["attempts"], 1)
            self.assertIsNone(self.row()["output_dir"])
            self.assertTrue((directories[0] / "agent-run.json").is_file())
            self.assertTrue(
                build_one(self.store, self.fixture.job["id"], "summary", bridge=self.bridge)
            )
            # Simulate reconnecting to that valid durable response after a caller
            # interruption. Its retry guidance must remain byte-for-byte stable.
            (directories[1] / "response.json").unlink()
            self.store.stage_update(
                "artifact",
                self.fixture.job["id"],
                workflow="summary",
                state="pending",
                stage="model",
                output_dir=directories[1].relative_to(self.store.root).as_posix(),
                response_sha256=None,
                error="Interrupted; checkpoints retained",
            )
            self.assertTrue(
                build_one(self.store, self.fixture.job["id"], "summary", bridge=self.bridge)
            )
        self.assertNotEqual(directories[0], directories[1])
        self.assertEqual(self.row()["attempts"], 2)
        self.assertEqual(self.row()["state"], "complete")
        self.assertNotIn("Previous attempt failed", instructions[0])
        self.assertIn("at least five whitespace-separated words", instructions[1])
        self.assertEqual(directories[1], directories[2])
        self.assertEqual(instructions[1], instructions[2])

    def test_pipeline_resumes_summary_failure_after_earlier_attempt_budgets(self):
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET asr_attempts=3")
            self.store.db.execute("UPDATE diarizations SET attempts=3")
        self.fixture.health.side_effect = AssertionError("ASR must stay stopped")
        self.fixture.ready_diarization.side_effect = AssertionError("Speakers must stay stopped")
        self.invalid = "quote"
        first = work(
            self.store, "http://127.0.0.1:1", bridge=self.bridge, ids=[self.fixture.job["video_id"]]
        )
        self.assertEqual(first["failed"], 1)
        self.assertEqual(self.row()["state"], "failed")
        self.assertEqual(self.fixture.row()["state"], "complete")
        self.assertEqual(
            work(
                self.store,
                "http://127.0.0.1:1",
                bridge=self.bridge,
                ids=[self.fixture.job["video_id"]],
            )["completed"],
            0,
        )
        self.assertEqual(self.requests, 1)
        self.store.retry([self.fixture.job["video_id"]])
        self.invalid = None
        self.assertEqual(
            work(
                self.store,
                "http://127.0.0.1:1",
                bridge=self.bridge,
                ids=[self.fixture.job["video_id"]],
            )["completed"],
            1,
        )
        self.assertEqual(self.row()["state"], "complete")
        self.assertEqual(self.fixture.requests, 1)
        self.assertEqual(self.fixture.transcribe.call_count, 1)

    def test_postprocess_adds_summary_and_pipeline_repairs_it_without_inference(self):
        result = postprocess(self.store, ids=[self.fixture.job["video_id"]], bridge=self.bridge)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(self.fixture.requests, 1)
        output = self.store.root / self.row()["output_dir"]
        (output / "artifact.md").write_text("corrupt")
        result = work(
            self.store,
            "http://127.0.0.1:1",
            ids=[self.fixture.job["video_id"]],
            bridge="http://127.0.0.1:1",
        )
        self.assertEqual(result["completed"], 1)
        self.assertEqual(self.requests, 1)
        self.assertEqual(self.fixture.requests, 1)
        self.assertIn("useful summary", (output / "artifact.md").read_text())
        self.assertEqual(
            work(self.store, "http://127.0.0.1:1", ids=[self.fixture.job["video_id"]])["skipped"], 1
        )

    def test_interrupted_summary_preserves_budget_and_exhausted_wait_exits(self):
        ids = [self.fixture.job["video_id"]]
        with patch("tubescribe.artifacts.httpx.Client.post", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                work(self.store, "http://127.0.0.1:1", ids=ids, bridge=self.bridge)
        self.assertEqual(self.row()["state"], "pending")
        self.assertEqual(self.row()["attempts"], 0)
        self.assertEqual(
            work(self.store, "http://127.0.0.1:1", ids=ids, bridge=self.bridge)["completed"], 1
        )
        with self.store.db:
            self.store.db.execute("UPDATE artifacts SET state='failed',stage='model',attempts=3")
        result = work(self.store, "http://127.0.0.1:1", ids=ids, wait=True, bridge=self.bridge)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(self.requests, 1)

    def test_summary_cannot_embed_images_or_add_external_links(self):
        definition = workflow(self.store.root, "summary")
        result = {
            "output": {
                "markdown": "![Chart](figure:frame_0001000)",
                "used_image_ids": ["frame_0001000"],
                "evidence": [{"claim": "Claim", "source_quote": QUOTE}],
            }
        }
        with self.assertRaisesRegex(ValueError, "link to figures"):
            validate(result, QUOTE, {"frame_0001000": {}}, definition)
        result["output"]["markdown"] = "[Visit](https://not-issued.example)"
        result["output"]["used_image_ids"] = []
        with self.assertRaisesRegex(ValueError, "link not issued"):
            validate(result, QUOTE, {}, definition)

    def test_dashboard_trigger_and_readable_artifact_routes_are_guarded(self):
        server = make_server(self.store.root, instance="test-token")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False
        ) as client:
            route = f"/artifacts/{self.fixture.job['id']}/summary/"
            ready = client.get(route)
            self.assertIn("Build artifact", ready.text)
            self.assertIn('src="/artifact.js"', ready.text)
            self.assertIn("script-src 'self'", ready.headers["content-security-policy"])
            script = client.get("/artifact.js")
            self.assertEqual(script.status_code, 200)
            self.assertIn("event.preventDefault()", script.text)
            self.assertIn('method: "POST"', script.text)
            self.assertEqual(client.post(route + "run", data={"token": "bad"}).status_code, 403)
            self.assertEqual(
                client.post(
                    route + "run",
                    data={"token": "test-token"},
                    headers={"Origin": "https://evil.example"},
                ).status_code,
                403,
            )
            child = Mock()
            child.poll.return_value = None
            child.pid = 12345
            with patch("tubescribe.dashboard.subprocess.Popen", return_value=child) as spawn:
                self.assertEqual(
                    client.post(route + "run", data={"token": "test-token"}).status_code, 303
                )
                self.assertEqual(
                    client.post(route + "run", data={"token": "test-token"}).status_code, 303
                )
                self.assertEqual(spawn.call_count, 1)
                command = spawn.call_args.args[0]
                self.assertIn("tubescribe.cli", command)
                self.assertIn("artifacts", command)
            self.assertEqual(self.build()["completed"], 1)
            page = client.get(route)
            self.assertEqual(page.status_code, 200)
            self.assertIn("script-src 'none'", page.headers["content-security-policy"])
            self.assertIn('<a href="figures/frame_0001000.png"', page.text)
            self.assertEqual(client.get(route + "figures/frame_0001000.png").content, PNG)
            self.assertIn("useful summary", client.get(route + "artifact.md").text)
            self.assertEqual(client.get(route + "response.json").status_code, 404)
            self.assertEqual(client.get(route.rstrip("/")).status_code, 302)
            self.assertIn(
                "One-page summary",
                client.get(f"/processed/{self.fixture.job['id']}/preview.html").text,
            )

    def test_export_resolves_links_and_updates_only_copy_manifest(self):
        import shutil
        import sqlite3

        from tubescribe.publication import export_publications

        self.assertEqual(self.build()["completed"], 1)
        row = self.row()
        original = self.store.root / row["output_dir"]
        before = (original / "manifest.json").read_bytes()
        destination = self.store.root.parent / (self.store.root.name + "-export")
        self.addCleanup(shutil.rmtree, destination, True)
        shutil.copytree(self.store.root, destination)
        with sqlite3.connect(destination / "archive.sqlite3") as target:
            self.store.db.backup(target)
        export_publications(destination)
        directory = destination / row["output_dir"]
        linked = (directory / "read.md").read_text()
        self.assertNotIn("(transcript:", linked)
        self.assertNotIn("(figure:", linked)
        self.assertNotIn("(/processed/", linked)
        preview = (directory / "preview.html").read_text()
        self.assertIn('href="read.md"', preview)
        self.assertNotIn("[Full transcript]", preview)
        target = re.search(r"\[Full transcript\]\(([^)]+)\)", linked)[1]
        self.assertTrue((directory / target).is_file())
        with sqlite3.connect(destination / "archive.sqlite3") as db:
            self.assertEqual(
                db.execute("SELECT manifest_sha256 FROM artifacts").fetchone()[0],
                digest(directory / "manifest.json"),
            )
        self.assertEqual((original / "manifest.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
