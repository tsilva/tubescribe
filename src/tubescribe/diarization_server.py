"""One resident Nemotron model, one inference request, loopback HTTP only."""

import json
import signal
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .diarization import Nemotron


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18767)
    args = parser.parse_args()
    model = Nemotron()
    gate = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, result):
            data = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path == "/health":
                self.reply(
                    200,
                    {
                        **model.identity,
                        "service": "tubescribe-diarization",
                        "ready": True,
                        "busy": gate.locked(),
                        "concurrency": 1,
                    },
                )
            else:
                self.reply(404, {"error": "Not found"})

        def do_POST(self):
            if self.path != "/diarize":
                self.reply(404, {"error": "Not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.reply(400, {"error": "Invalid length"})
                return
            if not 0 < length <= 1024**3:
                self.reply(413, {"error": "Provide audio up to 1 GiB"})
                return
            if not gate.acquire(blocking=False):
                self.reply(503, {"error": "Diarization busy; retry later"})
                return
            try:
                self.connection.settimeout(120)
                with tempfile.TemporaryDirectory() as folder:
                    path = Path(folder) / "source.audio"
                    with path.open("wb") as audio:
                        remaining = length
                        while remaining:
                            chunk = self.rfile.read(min(1024**2, remaining))
                            if not chunk:
                                raise ValueError("Incomplete upload")
                            audio.write(chunk)
                            remaining -= len(chunk)
                    self.reply(200, model.infer(path))
            except Exception as error:
                print(f"Diarization failed: {error}", flush=True)
                self.reply(500, {"error": "Diarization failed; inspect the service journal"})
            finally:
                try:
                    model.release_scratch()
                finally:
                    gate.release()

    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:

        def shutdown(signum, frame):
            # shutdown must run outside serve_forever's thread. server_close then
            # waits for request handlers, allowing temporary uploads to be removed.
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, shutdown)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
