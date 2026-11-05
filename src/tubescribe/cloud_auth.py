"""Trusted Agent Vault launcher for cloud workers; never fetches service keys."""

import hashlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

INSTANCE = "https://app.infisical.com"
BUNDLE = "local-tubescribe-openrouter"
PROXY = "127.0.0.1:17323"
_PROXY_LOCK = threading.Lock()


def machine_token(client, env):
    """Use workload OIDC where available, or a scoped machine identity."""
    token_file = env.get("VERCEL_OIDC_TOKEN_FILE") or env.get("TUBESCRIBE_OIDC_TOKEN_FILE")
    jwt = Path(token_file).read_text().strip() if token_file else env.get("VERCEL_OIDC_TOKEN")
    if jwt:
        identity = env.get("TUBESCRIBE_INFISICAL_IDENTITY_ID", "")
        if not re.fullmatch(r"[0-9a-f-]{36}", identity):
            raise ValueError("Configure the cloud Infisical machine identity")
        endpoint = "/api/v1/auth/oidc-auth/login"
        body = {"identityId": identity, "jwt": jwt}
    else:
        endpoint = "/api/v1/auth/universal-auth/login"
        body = {
            "clientId": env.get("INFISICAL_UNIVERSAL_AUTH_CLIENT_ID"),
            "clientSecret": env.get("INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET"),
        }
        if not all(body.values()):
            raise ValueError("Configure cloud workload authentication for Agent Vault")
    response = client.post(
        INSTANCE + endpoint, **({"data": body} if jwt else {"json": body})
    )
    response.raise_for_status()
    token = response.json().get("accessToken")
    if not isinstance(token, str) or not token:
        raise ValueError("Cloud workload authentication failed")
    return token


def launch(payload, *, probe=False, env=None, client=None, run=None):
    env = dict(os.environ if env is None else env)
    shared_session = env.get("TUBESCRIBE_VAULT_AUTH", "oidc") == "session"
    fingerprint = env.get("TUBESCRIBE_VAULT_CA_FINGERPRINT", "")
    if not re.fullmatch(
        r"(?i)(?:sha256:)?(?:[0-9a-f]{64}|(?:[0-9a-f]{2}:){31}[0-9a-f]{2})", fingerprint
    ):
        raise ValueError("Configure the reviewed cloud proxy CA fingerprint")
    # The service client receives only a short-lived proxy capability, a fake
    # API key and public CA. Database and proxy-administration keys stay out.
    child_env = {k: env[k] for k in (
        "PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH", "PYTHONUNBUFFERED", "TMPDIR"
    ) if k in env}
    from .openrouter import PLACEHOLDER

    child_env["OPENROUTER_API_KEY"] = PLACEHOLDER
    if env.get("TUBESCRIBE_OPENROUTER_TIMEOUT"):
        child_env["TUBESCRIBE_OPENROUTER_TIMEOUT"] = "210"
    owned = client is None
    client = client or httpx.Client(trust_env=False, timeout=20)
    session_id = token = None
    try:
        if shared_session:
            capability = env.get("INFISICAL_AGENT_VAULT_SESSION_TOKEN")
            if not capability:
                raise ValueError("Configure the dedicated TubeScribe session capability")
            child_env["INFISICAL_AGENT_VAULT_SESSION_TOKEN"] = capability
        else:
            token = machine_token(client, env)
            headers = {"Authorization": "Bearer " + token}
            response = client.post(
                INSTANCE + "/api/v1/agent-vault/sessions", headers=headers,
                json={"accessBundle": BUNDLE, "ttl": "15m", "label": "tubescribe-cloud"},
            )
            response.raise_for_status()
            session = response.json()["session"]
            session_id = session["id"]
            child_env["INFISICAL_AGENT_VAULT_SESSION_TOKEN"] = session["token"]
        with TemporaryDirectory(prefix="tubescribe-vault-") as temporary:
            command = [
                env.get("TUBESCRIBE_INFISICAL_CLI", "infisical"), "agent-vault", "run", "--domain=" + INSTANCE,
                "--proxy=" + PROXY, "--ca-fingerprint=" + fingerprint,
                "--ca-file=" + str(Path(temporary) / "ca.pem"),
                "--no-skills", "--silent", "--telemetry=false", "--log-level=error", "--",
                sys.executable, "-m", "tubescribe.cloud_auth" if probe else "tubescribe.openrouter",
            ]
            if shared_session:
                # One provisioned capability serves concurrent Functions. Only
                # trusted administration may revoke it, never a single caller.
                command.insert(command.index("--"), "--keep-session")
            if probe:
                command.append("--probe-client")
            result = (run or subprocess.run)(
                command, input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=child_env, timeout=650, check=False,
            )
            if result.returncode:
                raise ValueError("Cloud Agent Vault transport failed")
            # Native CLI information stays on stderr; only a valid response
            # envelope can leave this launcher.
            parsed = json.loads(result.stdout)
            if not isinstance(parsed, dict) or "status" not in parsed:
                raise ValueError("Invalid cloud proxy response")
            return parsed
    finally:
        if session_id and token:
            try:
                client.post(
                    INSTANCE + "/api/v1/agent-vault/sessions/" + session_id + "/revoke",
                    headers={"Authorization": "Bearer " + token},
                ).raise_for_status()
            except Exception:
                # A failed revoke cannot mask an already-paid model response.
                # The capability still expires after its fixed 15-minute TTL.
                pass
        if owned:
            client.close()


def cli_path():
    """Only the checksum-pinned public binary is cached; never service credentials."""
    target = Path("/tmp/tubescribe-cli-0.44.13/infisical")
    if target.is_file():
        return str(target)
    url = "https://github.com/Infisical/cli/releases/download/v0.44.13/cli_0.44.13_linux_amd64.tar.gz"
    expected = "8cba1083fb452f3344f31f188dc239310e29d9abcb99d1209c19594bc3faf939"
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read(100 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Agent Vault CLI checksum differs from the reviewed binary")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        binary = archive.extractfile("infisical").read()
    from .blobs import atomic_write

    atomic_write(target, binary)
    target.chmod(0o700)
    return str(target)


@contextmanager
def workflow_proxy(env):
    if not env.get("INFISICAL_AGENT_VAULT_PROXY_TOKEN"):
        raise ValueError("Configure the dedicated cloud Agent Vault proxy")
    executable = cli_path()
    env["TUBESCRIBE_INFISICAL_CLI"] = executable
    with TemporaryDirectory(prefix="tubescribe-cloud-proxy-") as directory:
        proxy_env = {k: env[k] for k in (
            "PATH", "HOME", "LANG", "INFISICAL_AGENT_VAULT_PROXY_TOKEN"
        ) if k in env}
        proxy = subprocess.Popen([
            executable, "agent-vault", "proxy", "--domain=" + INSTANCE,
            "--port=17323", "--data-dir=" + directory,
            "--silent", "--telemetry=false", "--log-level=error",
        ], env=proxy_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline and proxy.poll() is None:
                try:
                    with socket.create_connection(("127.0.0.1", 17323), timeout=1):
                        break
                except OSError:
                    time.sleep(.1)
            else:
                raise ValueError("Dedicated cloud proxy is unavailable")
            yield
        finally:
            proxy.terminate()
            try:
                proxy.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proxy.kill()
                proxy.wait()


def workflow_send(request, *, probe=False):
    """Native Agent Vault inside a bounded Function; never inside a media VM."""
    from vercel.headers import get_headers

    env = dict(os.environ)
    token = (get_headers() or {}).get("x-vercel-oidc-token") or env.get("VERCEL_OIDC_TOKEN")
    if env.get("TUBESCRIBE_VAULT_AUTH", "oidc") != "session" and not token and not env.get("VERCEL_OIDC_TOKEN_FILE"):
        raise ValueError("Fresh workload identity is required; local credential fallback is disabled")
    if token:
        env["VERCEL_OIDC_TOKEN"] = token
    env["TUBESCRIBE_OPENROUTER_TIMEOUT"] = "210"
    payload = json.dumps(request).encode()
    # Concurrent requests in the same Function instance cannot share a proxy port.
    with _PROXY_LOCK, workflow_proxy(env):
        return launch(payload, env=env, probe=probe)


def main():
    try:
        if sys.argv[1:] == ["--probe-client"]:
            from .openrouter import PLACEHOLDER

            if os.environ.get("OPENROUTER_API_KEY") != PLACEHOLDER:
                raise ValueError("Proxy placeholder required")
            if not os.environ.get("HTTPS_PROXY") or not os.environ.get("SSL_CERT_FILE"):
                raise ValueError("Proxy and process-local CA required")
            print(json.dumps({"status": 200, "proxy_ready": True}))
            return 0
        probe = sys.argv[1:] == ["--probe"]
        from .openrouter import MAX_REQUEST_BYTES

        payload = b"" if probe else sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if not probe and (not payload or len(payload) > MAX_REQUEST_BYTES):
            raise ValueError("Invalid proxy request size")
        print(json.dumps(launch(payload, probe=probe)))
        return 0
    except Exception:
        # Provider exceptions can include credential-bearing request details.
        print("Cloud Agent Vault request failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
