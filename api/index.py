"""Vercel routes the dashboard to the existing HTTP handler."""

from urllib.parse import urlsplit

from api.maintenance import handler as maintenance_handler
from tubescribe.hosting import hosted_handler


class handler(hosted_handler()):
    """Framework entrypoint for the reader and protected maintenance."""

    def do_GET(self):
        # A Python framework deployment emits one web Function. File-based
        # Python API siblings are not emitted separately in this mode.
        if urlsplit(self.path).path == "/api/maintenance":
            return maintenance_handler.do_GET(self)
        return super().do_GET()
