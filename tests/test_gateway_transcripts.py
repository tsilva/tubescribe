"""Durable transcript tools must use the same verified source as the worker."""

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from tubescribe.agent_gateway import Runs, execute
from tubescribe.postprocessing import Transcript, load_source
from tubescribe.storage import Store


class GatewayTranscripts(unittest.TestCase):
    def test_unlabeled_and_named_speech_are_normalized_without_changing_source(self):
        for prefix, metadata in (("", {}), ("Ada: ", {"speaker_mapping": {"1": "Ada"}})):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                with patch.dict(os.environ, {
                    "TUBESCRIBE_DATABASE": str(base / "archive.sqlite"),
                    "TUBESCRIBE_OBJECTS": str(base / "objects"),
                }):
                    store = Store(base / "worker")
                    self.addCleanup(store.db.close)
                    sources = {
                        "transcripts/source.json": json.dumps({"duration": 1, **metadata}).encode(),
                        "transcripts/source.srt": f"1\n00:00:00,000 --> 00:00:01,000\n{prefix}Hello.\n".encode(),
                    }
                    for key, body in sources.items():
                        store.blobs.put(key, body)
                    context = {"kind": "postprocessing", "output_key": "transcripts/output",
                               "duration": 1, "budgets": {}, "compact": True}
                    for name, suffix in (("transcript", "json"), ("srt", "srt")):
                        key = "transcripts/source." + suffix
                        context[name + "_key"] = key
                        context[name + "_sha256"] = hashlib.sha256(sources[key]).hexdigest()
                    root = base / "gateway"
                    identifier = "a" * 64
                    with closing(Runs(root)) as runs:
                        runs.create(identifier, {"tool_context": context})
                    runner = Mock()
                    runner.run.return_value = {"output": {}, "usage": {}, "elapsed_seconds": 0}
                    with patch("tubescribe.postprocessing.Transcript", wraps=Transcript) as reader:
                        execute(root, identifier, "http://127.0.0.1:1", runner_factory=lambda _: runner)
                    with closing(Runs(root)) as runs:
                        self.assertEqual(runs.get(identifier)["state"], "complete")
                    normalized = load_source(root / "transcripts/source.json")["srt"]
                    reader.assert_called_once_with(normalized, compact=True)
                    self.assertIn("Speaker 1: Hello." if prefix else "UNKNOWN: Hello.", normalized)
                    runner.run.assert_called_once()
                    for key, body in sources.items():
                        self.assertEqual(store.read_key(key), body)
