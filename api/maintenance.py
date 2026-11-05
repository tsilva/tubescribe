"""Daily Hobby-compatible repair trigger; never admits unselected videos."""

import hmac
import os
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        expected = os.environ.get("CRON_SECRET", "")
        if len(expected) < 32 or not hmac.compare_digest(
            self.headers.get("Authorization", ""), "Bearer " + expected
        ):
            self.send_response(401)
            self.end_headers()
            return
        from tubescribe.workflow_dispatch import call

        result = call("wake", request_token=self.headers.get("x-vercel-oidc-token"))
        self.send_response(204 if result.get("notified") else 503)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
