"""Tool caches must not bypass package authentication or media-image checks."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CiTooling(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.log = self.root / "commands.jsonl"
        self.bash = shutil.which("bash")
        self.scripts = Path(__file__).resolve().parents[1] / "scripts"
        self.env = os.environ | {"PATH": str(self.root), "TOOL_LOG": str(self.log)}
        (self.root / "mkdir").symlink_to(shutil.which("mkdir"))
        self.install_tool("sudo", """
if sys.argv[1] == 'tee':
    sys.stdin.read()
elif sys.argv[1] == 'apt-get':
    if os.environ.get('FAIL_APT') in sys.argv:
        sys.exit(23)
    if 'install' in sys.argv:
        for name in ('ffmpeg', 'ffprobe'):
            path = pathlib.Path(os.environ['PATH']) / name
            path.write_text('#!' + sys.executable + '\\nprint("fixture media tool")\\n')
            path.chmod(0o700)
else:
    sys.exit(99)
""")

    def install_tool(self, name, code):
        path = self.root / name
        path.write_text(
            f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
            "with open(os.environ['TOOL_LOG'], 'a') as out: "
            "out.write(json.dumps([pathlib.Path(sys.argv[0]).name, *sys.argv[1:]]) + '\\n')\n"
            + code
        )
        path.chmod(0o700)

    def invoke(self, script, *args, **extra):
        self.log.unlink(missing_ok=True)
        result = subprocess.run([self.bash, str(self.scripts / script), *args], env=self.env | extra, capture_output=True)
        rows = [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []
        return result, rows

    def test_cached_debs_still_require_fresh_authenticated_apt_metadata(self):
        cache = self.root / "package cache"
        cache.mkdir()
        (cache / "stale.deb").write_bytes(b"untrusted bytes must not be installed directly")
        result, rows = self.invoke("ensure-media-tools", TUBESCRIBE_APT_CACHE=str(cache))
        self.assertEqual(result.returncode, 0, result.stderr)
        apt = [x for x in rows if x[:2] == ["sudo", "apt-get"]]
        self.assertEqual(len(apt), 2)
        self.assertEqual(apt[0][-1], "update")
        self.assertEqual(apt[1][-4:], ["install", "-y", "--no-install-recommends", "ffmpeg"])
        for command in apt:
            self.assertIn("Dir::Cache::archives=" + str(cache), command)
            self.assertIn("APT::Get::AllowUnauthenticated=false", command)
            self.assertIn("Acquire::AllowInsecureRepositories=false", command)
            self.assertIn("Acquire::AllowDowngradeToInsecureRepositories=false", command)
            self.assertIn("Acquire::AllowWeakRepositories=false", command)

    def test_failed_metadata_refresh_cannot_install_cached_packages(self):
        result, rows = self.invoke("ensure-media-tools", FAIL_APT="update")
        self.assertEqual(result.returncode, 23)
        self.assertFalse(any("install" in x for x in rows))

    def test_preinstalled_media_tools_need_no_package_install(self):
        for name in ("ffmpeg", "ffprobe"):
            self.install_tool(name, "print('fixture media tool')\n")
        result, rows = self.invoke("ensure-media-tools")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(rows, [["ffmpeg", "-version"], ["ffprobe", "-version"]])

    def test_stale_prebuilt_media_never_builds_pulls_or_runs(self):
        self.install_tool("git", "print('a' * 40)\n")
        self.install_tool("docker", "print('b' * 40)\n")
        result, rows = self.invoke("verify-cloud-media", "--prebuilt")
        self.assertEqual(result.returncode, 1)
        docker = [x for x in rows if x[0] == "docker"]
        self.assertEqual(len(docker), 1)
        self.assertEqual(docker[0][1:3], ["image", "inspect"])

    def test_exact_prebuilt_media_retains_the_offline_test_command(self):
        self.install_tool("git", "print('a' * 40)\n")
        self.install_tool("docker", "print('a' * 40)\n")
        result, rows = self.invoke("verify-cloud-media", "--prebuilt")
        self.assertEqual(result.returncode, 0, result.stderr)
        command = next(x for x in rows if x[:2] == ["docker", "run"])
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertEqual(command[command.index("--pull") + 1], "never")
        self.assertIn("tubescribe-ci-cloud-media:" + "a" * 40, command)
        self.assertEqual(command[-5:], ["-m", "unittest", "-v", "test_cloud_workflows", "test_cloud_media"])
