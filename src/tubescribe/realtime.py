"""Public Realtime configuration. Query sealing stays in the Node SDK function."""

import os
from urllib.parse import urlsplit


def origin():
    value = os.environ.get("NEON_REALTIME_URL", "")
    if not value:
        return None
    url = urlsplit(value)
    if (
        url.scheme != "wss" or not url.hostname or url.username or url.password
        or url.query or url.fragment
        or not any(url.hostname.endswith("." + domain) for domain in ("neon.tech", "neon.build", "neon.com"))
    ):
        raise ValueError("Invalid Neon Realtime URL")
    return f"wss://{url.netloc}"


def configuration():
    return {"enabled": bool(origin()) and os.environ.get("VERCEL") == "1", "endpoint": "/api/live"}
