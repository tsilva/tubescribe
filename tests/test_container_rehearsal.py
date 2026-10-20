import json
import runpy
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


class ContainerRehearsal(unittest.TestCase):
    def setUp(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "rehearse-containers"
        self.operations = runpy.run_path(str(script))["operations"]
        self.env = patch.dict(self.operations.__globals__["ENV"], {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_unready_cli_result_survives_expected_nonzero_exit(self):
        result = {"ready": False, "checks": {"database": True, "schema": True, "objects": False}}
        completed = subprocess.CompletedProcess([], 1, json.dumps(result), "")
        with patch("subprocess.run", return_value=completed) as run:
            self.assertEqual(self.operations("ready", project="restore-fixture"), result)
        command = run.call_args.args[0]
        self.assertEqual(command[3], "restore-fixture")
        self.assertEqual(command[-6:], ["exec", "-T", "web", "tubescribe", "operations", "ready"])

    def test_metrics_and_unexpected_exec_failures_still_fail(self):
        for action, returncode in (("metrics", 1), ("ready", 127)):
            with self.subTest(action=action, returncode=returncode):
                completed = subprocess.CompletedProcess([], returncode, "", "probe failed")
                with patch("subprocess.run", return_value=completed):
                    with self.assertRaisesRegex(RuntimeError, "probe failed"):
                        self.operations(action)
