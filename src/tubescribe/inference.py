"""Runtime policies and reusable AgentBridge execution, independent of archive state."""

import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

import httpx

MODEL = "codex/gpt-6.1-sol"
REASONING = "high"


@dataclass(frozen=True)
class EndpointPolicy:
    local: bool = True

    def check(self, endpoint):
        url = urlparse(endpoint)
        if self.local:
            valid = url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")
        else:
            valid = url.scheme == "https" and bool(url.hostname)
        if not valid or url.username or url.password or url.query or url.fragment:
            raise ValueError("Endpoint must use loopback HTTP or explicitly configured HTTPS")
        return endpoint.rstrip("/")


@dataclass(frozen=True)
class HardwarePolicy:
    device: str | None = "cuda"
    gpu: str | None = "RTX 4090"

    def check(self, device, gpu):
        if (self.device and str(device).split(":")[0] != self.device) or (
            self.gpu and self.gpu not in (gpu or "")
        ):
            raise ValueError(f"Inference requires {self.device} {self.gpu}; fallback is disabled")


class PostprocessingUnavailable(RuntimeError):
    pass


def ready(endpoint, *, policy=None, headers=None, transport=None):
    try:
        endpoint = (policy or EndpointPolicy()).check(endpoint)
        with httpx.Client(
            headers=headers, transport=transport, timeout=10, trust_env=False
        ) as client:
            response = client.get(endpoint + "/openapi.json")
        response.raise_for_status()
        if "/api/v1/agent/runs" not in response.json().get("paths", {}):
            raise ValueError("Native external-tool agent runs are unavailable")
    except Exception as error:
        raise PostprocessingUnavailable(
            f"AgentBridge unavailable: {error}; start a compatible server supporting native agent tools"
        ) from error


class LocalTools:
    """Adapter serving domain tools through an authenticated local callback."""

    def __init__(self, handler_factory):
        self.handler_factory = handler_factory

    @contextmanager
    def open(self):
        token = secrets.token_urlsafe(32)
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler_factory(token))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield {"url": f"http://127.0.0.1:{server.server_port}/tool-calls", "token": token}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class RemoteTools:
    """Adapter for an executor hosted separately from the inference caller."""

    def __init__(self, url, token):
        self.url = EndpointPolicy(local=False).check(url)
        self.token = token

    @contextmanager
    def open(self):
        yield {"url": self.url, "token": self.token}


class AgentRunner:
    def __init__(self, endpoint, *, policy=EndpointPolicy(), headers=None, transport=None):
        self.endpoint = policy.check(endpoint)
        self.headers = headers or {}
        self.transport = transport

    def run(self, request, tools):
        payload = {"model": MODEL, "reasoning_effort": REASONING, **request}
        with tools.open() as executor:
            payload["tool_executor"] = executor
            with httpx.Client(
                timeout=payload["timeout_seconds"] + 50,
                trust_env=False,
                headers=self.headers,
                transport=self.transport,
            ) as client:
                response = client.post(self.endpoint + "/api/v1/agent/runs", json=payload)
                response.raise_for_status()
                return response.json()


def tool_handler(handlers, token):
    """Domain tools supply functions; authentication and HTTP stay in this adapter."""
    import hmac
    import json
    import subprocess
    from contextvars import copy_context
    from http.server import BaseHTTPRequestHandler

    context = copy_context()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if self.path != "/tool-calls" or not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            ):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("Invalid callback size")
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict) or request.get("tool") not in handlers:
                    raise ValueError("Unknown tool")
                arguments = request.get("arguments")
                if not isinstance(arguments, dict):
                    raise ValueError("Tool arguments must be an object")
                result = context.copy().run(handlers[request["tool"]], arguments)
            except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
                result = {
                    "success": False,
                    "contentItems": [{"type": "inputText", "text": "Tool error: " + str(error)}],
                }
            content = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    return Handler


def health_request(endpoint, *, headers=None, transport=None):
    if transport is None:
        return httpx.get(endpoint + "/health", timeout=10, trust_env=False, headers=headers or {})
    with httpx.Client(
        timeout=10, trust_env=False, headers=headers or {}, transport=transport
    ) as client:
        return client.get(endpoint + "/health")


def configured_runner(store, endpoint, run_key):
    """Runtime selection preserves the synchronous local bridge by default."""
    import os

    if store.config.agent_gateway:
        from .agent_gateway import DurableAgentRunner

        return DurableAgentRunner(endpoint, store=store, run_key=run_key)
    remote = os.environ.get("TUBESCRIBE_REMOTE_INFERENCE") == "1"
    headers = (
        {"Authorization": "Bearer " + os.environ["TUBESCRIBE_INFERENCE_TOKEN"]}
        if os.environ.get("TUBESCRIBE_INFERENCE_TOKEN")
        else None
    )
    return AgentRunner(endpoint, policy=EndpointPolicy(local=not remote), headers=headers)


def client_options():
    import os

    return {
        "policy": EndpointPolicy(local=os.environ.get("TUBESCRIBE_REMOTE_INFERENCE") != "1"),
        "hardware": HardwarePolicy(gpu=os.environ.get("TUBESCRIBE_GPU", "RTX 4090") or None),
        "headers": {"Authorization": "Bearer " + os.environ["TUBESCRIBE_INFERENCE_TOKEN"]}
        if os.environ.get("TUBESCRIBE_INFERENCE_TOKEN")
        else None,
    }


def ready_bridge(endpoint):
    options = client_options()
    return ready(endpoint, policy=options["policy"], headers=options["headers"])
