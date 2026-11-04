"""Configuration validation never connects to inference or launches a candidate."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run import service_config, snapshot


class ServiceConfiguration(unittest.TestCase):
    def config(self, value):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "services.json"
        path.write_text(json.dumps(value))
        return path

    def valid(self):
        return {
            "asr_url": "http://localhost/",
            "diarization_url": "http://localhost",
            "service_units": ["fixture-asr.service", "fixture-speakers.service"],
            "source_files": ["/fixture/service.py"],
        }

    def test_config_is_explicit_and_loopback_only(self):
        result = service_config(self.config(self.valid()))
        self.assertEqual(result["asr_url"], "http://localhost")
        for change in (
            {"asr_url": "https://example.test"},
            {"asr_url": "http://user:password@localhost"},
            {"asr_url": "http://localhost/?token=fixture"},
            {"service_units": []},
            {"service_units": ["--help"]},
            {"source_files": ["relative.py"]},
            {"source_files": []},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                service_config(self.config({**self.valid(), **change}))
        with self.assertRaises(ValueError):
            service_config(self.config({}))

    def test_snapshot_uses_only_configured_units_sources_and_endpoints(self):
        config = self.valid()
        with (
            patch("run.subprocess.check_output", return_value="fixture identity") as command,
            patch("run.Path.read_bytes", return_value=b"fixture") as source,
            patch("run.get", return_value={"ready": True}) as health,
        ):
            result = snapshot(config)
        self.assertEqual(result["services"], "fixture identity")
        self.assertEqual(set(result["files"]), {"/fixture/service.py"})
        self.assertEqual(source.call_count, 1)
        args = command.call_args.args[0]
        self.assertIn("fixture-asr.service", args)
        self.assertIn("fixture-speakers.service", args)
        self.assertEqual(health.call_count, 2)
