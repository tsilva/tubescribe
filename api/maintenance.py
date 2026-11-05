"""Daily Hobby-compatible repair trigger; never admits unselected videos."""

import hmac
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit


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
        if parse_qs(urlsplit(self.path).query).get("check") == ["proxy"]:
            # Authenticated, no-model transport check before admitting paid work.
            from tubescribe.cloud_auth import workflow_send
            try:
                result = workflow_send({}, probe=True)
                healthy = result.get("status") == 200 and result.get("proxy_ready") is True
            except Exception:
                healthy = False
            self.send_response(204 if healthy else 503)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        from tubescribe.workflow_dispatch import call

        result = call("wake", request_token=self.headers.get("x-vercel-oidc-token"))
        self.send_response(204 if not result.get("configured") or result.get("notified") else 503)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
