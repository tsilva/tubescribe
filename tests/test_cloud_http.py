"""Verify the framework web entrypoint reaches protected maintenance handlers."""

import importlib
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import httpx


class CloudHTTP(unittest.TestCase):
    def setUp(self):
        class Reader(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"reader")

        with patch("tubescribe.hosting.hosted_handler", return_value=Reader):
            module = importlib.import_module("api.index")
            module = importlib.reload(module)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), module.handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.server.server_port}",
                                   trust_env=False)
        self.addCleanup(self.client.close)
        environment = patch.dict(os.environ, {"CRON_SECRET": "synthetic-maintenance-" + "x" * 32})
        environment.start()
        self.addCleanup(environment.stop)
        self.headers = {"Authorization": "Bearer " + os.environ["CRON_SECRET"]}

    def test_reader_still_routes_normally_and_maintenance_requires_its_capability(self):
        self.assertEqual(self.client.get("/").text, "reader")
        with patch("tubescribe.cloud_auth.workflow_send") as probe, patch(
            "tubescribe.workflow_dispatch.call"
        ) as dispatch:
            self.assertEqual(self.client.get("/api/maintenance?check=proxy").status_code, 401)
            probe.assert_not_called()
            dispatch.assert_not_called()

    def test_explicit_proxy_check_never_dispatches_or_calls_a_model(self):
        with patch("tubescribe.cloud_auth.workflow_send", return_value={
            "status": 200, "proxy_ready": True
        }) as probe, patch("tubescribe.workflow_dispatch.call") as dispatch:
            response = self.client.get("/api/maintenance?check=proxy", headers=self.headers)
            self.assertEqual(response.status_code, 204)
            probe.assert_called_once_with({}, probe=True)
            dispatch.assert_not_called()

    def test_disabled_backend_has_a_successful_noop_cron(self):
        with patch("tubescribe.workflow_dispatch.call", return_value={"configured": False}):
            self.assertEqual(self.client.get("/api/maintenance", headers=self.headers).status_code, 204)
