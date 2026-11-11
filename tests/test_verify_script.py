"""Splitting CI work must preserve every check, failure, and fixture isolation."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class VerifyScript(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.log = self.root / "commands.jsonl"
        tool = (
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            "name = pathlib.Path(sys.argv[0]).name\n"
            "row = {'tool': name, 'args': sys.argv[1:], 'archive_settings': "
            "[k for k in os.environ if k.startswith('TUBESCRIBE_')], "
            "'network_tests': os.environ.get('TUBESCRIBE_NETWORK_TESTS'), "
            "'neon_settings': [k for k in os.environ if k in "
            "('NEON_REALTIME_URL', 'NEON_REALTIME_SECRET')]}\n"
            "with open(os.environ['VERIFY_COMMAND_LOG'], 'a') as out: "
            "out.write(json.dumps(row) + '\\n')\n"
            "sys.exit(17 if os.environ.get('VERIFY_FAIL_TOOL') == name else 0)\n"
        )
        for name in ("uv", "pnpm"):
            path = self.root / name
            path.write_text(tool)
            path.chmod(0o700)
        self.script = Path(__file__).resolve().parents[1] / "scripts" / "verify"
        self.env = os.environ | {
            "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
            "VERIFY_COMMAND_LOG": str(self.log),
            "TUBESCRIBE_ACCESS_TOKEN": "synthetic-fixture",
            "TUBESCRIBE_DATABASE_URL": "synthetic-fixture",
            "NEON_REALTIME_URL": "synthetic-fixture",
            "NEON_REALTIME_SECRET": "synthetic-fixture",
        }

    def invoke(self, *args, fail=None):
        self.log.unlink(missing_ok=True)
        env = self.env.copy()
        env.pop("VERIFY_FAIL_TOOL", None)
        if fail:
            env["VERIFY_FAIL_TOOL"] = fail
        result = subprocess.run(["bash", str(self.script), *args], env=env, capture_output=True)
        rows = [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []
        return result, rows

    def test_split_modes_preserve_the_complete_default_command_sequence(self):
        result, complete = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(complete), 5)
        divided = []
        for mode, count in (("--frontend", 3), ("--lint", 1), ("--python", 1)):
            result, rows = self.invoke(mode)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(rows), count)
            divided.extend(rows)
        self.assertEqual(complete, divided)
        self.assertEqual(complete[0]["args"], ["install", "--frozen-lockfile", "--ignore-scripts"])
        self.assertEqual(complete[1]["args"], ["run", "test:reader"])
        self.assertEqual(complete[2]["args"], ["run", "check:reader"])
        self.assertIn("ruff", complete[3]["args"])
        self.assertEqual(complete[4]["args"][-7:], ["python", "-m", "unittest", "discover", "-s", "tests", "-v"])

    def test_each_mode_clears_live_archive_and_realtime_settings(self):
        for mode in ("--all", "--frontend", "--lint", "--python"):
            result, rows = self.invoke(mode)
            self.assertEqual(result.returncode, 0, result.stderr)
            for row in rows:
                self.assertEqual(row["neon_settings"], [])
                expected = ["TUBESCRIBE_NETWORK_TESTS"] if "unittest" in row["args"] else []
                self.assertEqual(row["archive_settings"], expected)
                if expected:
                    self.assertEqual(row["network_tests"], "0")

    def test_invalid_modes_and_extra_arguments_execute_nothing(self):
        for args in (("--skip-tests",), ("--python", "--frontend")):
            result, rows = self.invoke(*args)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(rows, [])

    def test_failed_checks_stop_and_propagate_in_every_mode(self):
        for mode, tool in (("--all", "pnpm"), ("--frontend", "pnpm"), ("--lint", "uv"), ("--python", "uv")):
            result, rows = self.invoke(mode, fail=tool)
            self.assertEqual(result.returncode, 17)
            self.assertEqual(len(rows), 1)
