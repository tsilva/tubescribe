"""Actual FFmpeg preparation inside the media image, with synthetic video and no network."""

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tubescribe.cloud_media import prepare, visual
from tubescribe.postprocessing import budgets_for


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for synthetic media")
class CloudMedia(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "fixture.mp4"
        subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "mpeg4", "-c:a", "aac",
            "-shortest", str(self.source),
        ], capture_output=True, check=True, timeout=30)

    def test_preparation_uploads_pcm_parts_and_video_without_model_transport(self):
        def fetch(url, path):
            shutil.copyfile(self.source, path)

        captured = {}
        with patch("tubescribe.cloud_media.fetch_file", side_effect=fetch), patch(
            "tubescribe.cloud_media.upload", side_effect=lambda url, data: captured.update({url: data})
        ), patch(
            "tubescribe.cloud_media.upload_file", side_effect=lambda url, path: captured.update({url: path.read_bytes()})
        ):
            result = prepare({"video_id": "synthetic-video", "channel_id": "synthetic-channel",
                              "source": "https://api.apify.com/v2/key-value-stores/abcdefghijklmnopq/records/video.mp4",
                              "parts": ["part-output"], "video": "video-output"}, self.root)
        self.assertAlmostEqual(result["duration"], 2, delta=.1)
        self.assertEqual(result["parts"][0]["sha256"], hashlib.sha256(captured["part-output"]).hexdigest())
        self.assertEqual(result["video_sha256"], hashlib.sha256(captured["video-output"]).hexdigest())
        self.assertEqual(len(captured["part-output"]), round(result["duration"] * 16000) * 2)

    def test_visual_tool_returns_pixels_and_serializable_budget_state(self):
        def fetch(url, path):
            shutil.copyfile(self.source, path)

        captured = {}
        with patch("tubescribe.cloud_media.fetch_file", side_effect=fetch), patch(
            "tubescribe.cloud_media.upload", side_effect=lambda url, data: captured.update({url: data})
        ):
            result = visual({"video": "video-input", "files": {}, "tool": "video_screenshots",
                             "arguments": {"timestamps": [.5], "resolution": "preview", "crop": None},
                             "duration": 2, "budgets": budgets_for(2), "archive": "tool-output"}, self.root)
        self.assertEqual(result["sha256"], hashlib.sha256(captured["tool-output"]).hexdigest())
        with tarfile.open(fileobj=io.BytesIO(captured["tool-output"]), mode="r:gz") as archive:
            value = json.load(archive.extractfile("cloud-result.json"))
            self.assertTrue(value["value"]["success"])
            self.assertTrue(value["state"]["preview_fingerprints"])
            self.assertTrue(any(name.startswith("figures/") for name in archive.getnames()))
            self.assertTrue(any(item["type"] == "inputImage" for item in value["value"]["contentItems"]))
