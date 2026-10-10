"""Synthetic gateway fixture for the opt-in local container rehearsal only."""

import hmac
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from test_postprocessing_pipeline import FinalStage

from tubescribe.agent_gateway import make_gateway
from tubescribe.postprocessing_worker import process_one
from tubescribe.storage import Store
from tubescribe.submission import Dispatcher, JobRequest


class DurableFixture(FinalStage):
    @property
    def requests(self):
        return self._requests

    @requests.setter
    def requests(self, value):
        key = "rehearsal/model-calls.json"
        if value == 0 and self.store.exists(key):
            value = json.loads(self.store.read_key(key))
        self._requests = value
        self.store.blobs.put(key, json.dumps(value).encode())


def main():
    fixture = DurableFixture()
    fixture.setUp()
    fixture.full()
    if not fixture.row()["figures"]:
        video = fixture.store.root / "fixture.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x180:r=10:d=4",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(video),
            ],
            check=True,
        )
        fixture.inspect_video = True
        fixture.store.stage_update(
            "postprocessing",
            fixture.job["id"],
            state="pending",
            mode="video",
            output_dir=None,
            manifest_path=None,
            attempts=0,
        )
        with patch("tubescribe.postprocessing_worker.cached_video", return_value=str(video)):
            process_one(fixture.store, fixture.job, bridge=fixture.bridge, mode="video")
    gateway = make_gateway(Path("/scratch/gateway"), fixture.bridge, port=8083)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    token = os.environ["TUBESCRIBE_ACCESS_TOKEN"]

    class Control(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, payload, status=200):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            if hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                return True
            self.reply({"error": "Authentication required"}, 401)
            return False

        def do_GET(self):
            if not self.authorized():
                return
            with_store = Store(Path("/scratch/control"))
            try:
                self.reply(
                    {
                        "calls": fixture.requests,
                        "job_id": fixture.job["id"],
                        "status": with_store.status_data(),
                    }
                )
            finally:
                with_store.db.close()

        def do_POST(self):
            if not self.authorized():
                return
            with_store = Store(Path("/scratch/control"))
            try:
                identifier = Dispatcher(with_store).submit(
                    JobRequest(
                        "artifacts",
                        {"video": [fixture.job["video_id"]], "bridge": "http://127.0.0.1:8083"},
                    )
                )
                self.reply({"submission": identifier})
            finally:
                with_store.db.close()

    control = ThreadingHTTPServer(("127.0.0.1", 8084), Control)
    try:
        control.serve_forever()
    finally:
        control.server_close()
        gateway.shutdown()
        gateway.server_close()
        fixture.doCleanups()


if __name__ == "__main__":
    main()
