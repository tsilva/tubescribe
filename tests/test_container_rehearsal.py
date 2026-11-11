import json
import runpy
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
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

    def test_prebuilt_images_must_all_match_the_exact_checkout(self):
        namespace = self.operations.__globals__
        sha = "a" * 40
        completed = subprocess.CompletedProcess([], 0, json.dumps({"org.opencontainers.image.revision": sha}), "")
        with patch("subprocess.run", return_value=completed) as run:
            namespace["use_prebuilt_images"](sha)
        self.assertEqual(run.call_count, 3)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:3], ["docker", "image", "inspect"])
            self.assertTrue(call.args[0][-1].endswith(":" + sha))
        self.assertTrue(namespace["PREBUILT"])
        self.assertEqual(namespace["ENV"]["TUBESCRIBE_RUNTIME_IMAGE"], "tubescribe-ci-runtime:" + sha)
        self.assertTrue(namespace["BASE"][-1].endswith("compose.ci.yaml"))

    def test_missing_or_stale_prebuilt_images_cannot_fall_back_to_building(self):
        namespace = self.operations.__globals__
        for output in ({"org.opencontainers.image.revision": "b" * 40}, None):
            completed = subprocess.CompletedProcess([], 0, json.dumps(output), "")
            with patch("subprocess.run", return_value=completed) as run:
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    namespace["use_prebuilt_images"]("a" * 40)
            self.assertEqual(run.call_count, 1)
            self.assertFalse(namespace["PREBUILT"])
            self.assertEqual(namespace["ENV"], {})
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, ["docker", "image", "inspect"])):
            with self.assertRaises(subprocess.CalledProcessError):
                namespace["use_prebuilt_images"]("a" * 40)
        self.assertFalse(namespace["PREBUILT"])

    def test_prebuilt_compose_start_and_restore_never_build_or_pull(self):
        namespace = self.operations.__globals__
        namespace["PREBUILT"] = True
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch("subprocess.run", return_value=completed) as run:
            namespace["compose"]("up", "-d", "web", project="restore-fixture")
            namespace["compose"]("run", "--rm", "gateway-fixture")
        start, network = [call.args[0] for call in run.call_args_list]
        self.assertEqual(start[3], "restore-fixture")
        self.assertEqual(start[-6:], ["up", "--no-build", "--pull", "never", "-d", "web"])
        self.assertEqual(network[-5:], ["run", "--pull", "never", "--rm", "gateway-fixture"])

    def test_fresh_prebuilt_runner_pulls_only_the_committed_database_image(self):
        namespace = self.operations.__globals__
        sha = "a" * 40
        completed = subprocess.CompletedProcess([], 0, json.dumps({"org.opencontainers.image.revision": sha}), "")
        calls = []

        def compose(*args, **kwargs):
            calls.append(args)
            if args[0] == "up":
                raise RuntimeError("stop before services")
            return ""

        with (
            patch.dict(namespace, {"compose": compose}),
            patch.object(namespace["argparse"].ArgumentParser, "parse_args", return_value=SimpleNamespace(prebuilt=True)),
            patch("subprocess.check_output", return_value=sha),
            patch("subprocess.run", return_value=completed),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop before services"):
                namespace["main"]()
        self.assertEqual([x for x in calls if x[0] == "pull"], [("pull", "database")])
        self.assertFalse(any(x[0] == "build" for x in calls))
        self.assertEqual(sum(x[0] == "down" for x in calls), 2)
