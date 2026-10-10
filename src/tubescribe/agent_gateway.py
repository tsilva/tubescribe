"""Durable run gateway for the synchronous AgentBridge external-tool contract.

The gateway owns tool callbacks, so losing a caller does not terminate its run.
An interrupted gateway marks in-flight upstream requests uncertain; it never silently
repeats them. Run this beside AgentBridge, whose current callback contract is loopback.
"""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .blobs import atomic_write
from .config import RuntimeConfig, database_path
from .execution import lease
from .inference import AgentRunner, LocalTools
from .storage import Store, durable_writes

SCHEMA = """CREATE TABLE IF NOT EXISTS agent_runs (
 id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, request TEXT NOT NULL,
 state TEXT NOT NULL, response_key TEXT, error TEXT, created REAL NOT NULL,
 updated REAL NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0)"""


class Runs:
    def __init__(self, root):
        import sqlite3

        self.root = Path(root)
        self.db = sqlite3.connect(database_path(root), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute(SCHEMA)
        self.db.commit()

    def get(self, identifier):
        row = self.db.execute("SELECT * FROM agent_runs WHERE id=?", (identifier,)).fetchone()
        return dict(row) if row else None

    def create(self, identifier, payload):
        if len(identifier) != 64 or any(c not in "0123456789abcdef" for c in identifier):
            raise ValueError("Run ID must be a SHA-256 idempotency key")
        raw = json.dumps(payload, sort_keys=True)
        fingerprint = hashlib.sha256(raw.encode()).hexdigest()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.get(identifier)
            if row:
                if row["fingerprint"] != fingerprint:
                    raise ValueError("Idempotency key reused for a different request")
                return row, False
            stamp = time.time()
            self.db.execute(
                "INSERT INTO agent_runs VALUES(?,?,?,?,?,?,?,?,?)",
                (identifier, fingerprint, raw, "queued", None, None, stamp, stamp, 0),
            )
        return self.get(identifier), True

    def update(self, identifier, **values):
        if not set(values) <= {"state", "response_key", "error", "cancelled"}:
            raise ValueError("Invalid run update")
        with self.db:
            from .execution import assert_owned

            assert_owned(self.db, self.root)
            values["updated"] = time.time()
            self.db.execute(
                "UPDATE agent_runs SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                (*values.values(), identifier),
            )

    def recover(self):
        # Status polls also reconcile leases that had not expired at gateway startup.
        for row in self.db.execute("SELECT * FROM agent_runs WHERE state='active'").fetchall():
            if self.db.execute(
                "SELECT 1 FROM claims WHERE resource=? AND expires>?",
                ("agent-run:" + row["id"], time.time()),
            ).fetchone():
                continue
            store = Store(self.root)
            try:
                with self.db:
                    self.db.execute("BEGIN IMMEDIATE")
                    # Serialize with completion and replacement ownership before repairing state.
                    current = self.get(row["id"])
                    if (
                        current["state"] != "active"
                        or self.db.execute(
                            "SELECT 1 FROM claims WHERE resource=? AND expires>?",
                            ("agent-run:" + row["id"], time.time()),
                        ).fetchone()
                    ):
                        continue
                    key = "agent-results/" + row["id"] + ".json"
                    saved = store.exists(key)
                    self.db.execute(
                        "UPDATE agent_runs SET state=?,response_key=?,error=?,updated=? WHERE id=?",
                        (
                            "cancelled"
                            if current["cancelled"]
                            else "complete"
                            if saved
                            else "uncertain",
                            key if saved else None,
                            None
                            if saved
                            else "Gateway interrupted during upstream execution; explicit operator retry required",
                            time.time(),
                            row["id"],
                        ),
                    )
            finally:
                store.db.close()
        return [r["id"] for r in self.db.execute("SELECT id FROM agent_runs WHERE state='queued'")]

    def close(self):
        self.db.close()


def execute(root, identifier, upstream, *, runner_factory=AgentRunner):
    with lease(root, "agent-run:" + identifier), closing(Runs(root)) as runs:
        row = runs.get(identifier)
        if row["state"] != "queued":
            return
        if row["cancelled"]:
            runs.update(identifier, state="cancelled")
            return
        runs.update(identifier, state="active")
        request = json.loads(row["request"])
        context = request.pop("tool_context")
        store = Store(Path(root))
        try:
            with durable_writes(store):
                kind = context["kind"]
                if kind == "postprocessing":
                    from .postprocessing import Screenshots, Transcript, make_handler

                    store.materialize(context["transcript_key"], context["transcript_sha256"])
                    srt = store.read_key(context["srt_key"], context["srt_sha256"]).decode()
                    output = store.materialize_prefix(context["output_key"])
                    video = (
                        store.materialize(context["video_key"], context["video_sha256"])
                        if context.get("video_key")
                        else None
                    )
                    screenshots = (
                        Screenshots(str(video), output, context["duration"], context["budgets"])
                        if video
                        else None
                    )
                    reader = Transcript(srt, compact=context["compact"])
                    tools = LocalTools(lambda token: make_handler(screenshots, token, reader))
                elif kind == "artifact":
                    from .artifacts import Figures, figure_handler

                    directory = store.materialize_prefix(context["source_key"])
                    figures = Figures(directory, context["resources"])
                    tools = LocalTools(lambda token: figure_handler(figures, token))
                else:
                    raise ValueError("Unknown durable tool context")
                result = runner_factory(upstream).run(request, tools)
                if kind == "postprocessing":
                    result["tool_state"] = (
                        {
                            "assets": screenshots.assets,
                            "calls": screenshots.calls,
                            "searches": screenshots.searches,
                            "full_views": screenshots.full_views,
                            "search_samples": screenshots.search_samples,
                        }
                        if screenshots
                        else None
                    )
                else:
                    result["tool_state"] = {"calls": figures.calls}
                key = "agent-results/" + identifier + ".json"
                store.blobs.put(key, json.dumps(result).encode())
                # Cancellation prevents publication by callers; a late response remains audit evidence.
                runs.update(
                    identifier,
                    state="cancelled" if runs.get(identifier)["cancelled"] else "complete",
                    response_key=key,
                )
                store.record_event(
                    identifier,
                    "agent_complete",
                    {
                        "usage": result.get("usage"),
                        "elapsed_seconds": result.get("elapsed_seconds"),
                    },
                )
        except BaseException as error:
            runs.update(identifier, state="failed", error=str(error))
        finally:
            if not store.local_blobs and context.get("video_key"):
                (store.root / context["video_key"]).unlink(missing_ok=True)
            store.db.close()


def make_gateway(root, upstream, *, port=0, runner_factory=AgentRunner):
    import hmac

    root = Path(root).resolve()
    config = RuntimeConfig.load(root)
    if not config.access_token:
        raise ValueError("Agent gateway requires TUBESCRIBE_ACCESS_TOKEN")
    executor = ThreadPoolExecutor(max_workers=config.agent_concurrency)
    # Exclusive daemon claim prevents another gateway from recovering live upstream runs.
    owner = lease(root, "agent-gateway", reentrant=False)
    owner.__enter__()
    with closing(Runs(root)) as runs:
        pending = runs.recover()
    for identifier in pending:
        executor.submit(execute, root, identifier, upstream, runner_factory=runner_factory)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, data, status=200):
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def route(self):
            if not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + config.access_token
            ):
                self.send({"error": "Authentication required"}, 401)
                return
            parts = urlsplit(self.path).path.strip("/").split("/")
            if parts == ["health"]:
                return self.send({"service": "tubescribe-agent-gateway", "protocol": "durable-v1"})
            if len(parts) != 5 or parts[:4] != ["api", "v1", "durable", "runs"]:
                return self.send({"error": "Not found"}, 404)
            identifier = parts[4]
            with closing(Runs(root)) as runs:
                if self.command == "POST":
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 1024 * 1024:
                        raise ValueError("Invalid request size")
                    payload = json.loads(self.rfile.read(length))
                    context = payload.get("tool_context", {})
                    # Only configured archive objects; never arbitrary callback URLs or filesystem paths.
                    if context.get("kind") not in {"postprocessing", "artifact"}:
                        raise ValueError("A durable tool context is required")
                    row, created = runs.create(identifier, payload)
                    if created:
                        executor.submit(
                            execute, root, identifier, upstream, runner_factory=runner_factory
                        )
                else:
                    if self.command == "GET":
                        runs.recover()
                    row = runs.get(identifier)
                    if not row:
                        return self.send({"error": "Run not found"}, 404)
                    if self.command == "DELETE":
                        runs.update(
                            identifier,
                            cancelled=1,
                            state="cancelled" if row["state"] == "queued" else row["state"],
                        )
                        row = runs.get(identifier)
                result = {
                    k: row[k] for k in ["id", "state", "error", "created", "updated", "cancelled"]
                }
                if row["state"] == "complete":
                    store = Store(root)
                    try:
                        result["result"] = json.loads(store.read_key(row["response_key"]))
                    finally:
                        store.db.close()
                self.send(result, 202 if row["state"] in {"queued", "active"} else 200)

        def handle_request(self):
            try:
                self.route()
            except (ValueError, KeyError, TypeError) as error:
                self.send({"error": str(error)}, 400)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = do_POST = do_DELETE = handle_request

    try:
        server = ThreadingHTTPServer((config.host, port), Handler)
    except BaseException:
        executor.shutdown(wait=True)
        owner.__exit__(None, None, None)
        raise
    close = server.server_close

    def stop():
        executor.shutdown(wait=True)
        owner.__exit__(None, None, None)
        close()

    server.server_close = stop
    return server


class DurableAgentRunner:
    def __init__(self, endpoint, *, store, run_key, headers=None, transport=None):
        self.endpoint = endpoint.rstrip("/")
        self.store = store
        self.run_key = run_key
        self.headers = headers or {}
        if not self.headers and store.config.access_token:
            self.headers = {"Authorization": "Bearer " + store.config.access_token}
        self.transport = transport

    def run(self, request, tools):
        from .inference import EndpointPolicy

        EndpointPolicy(
            local=urlsplit(self.endpoint).hostname in {"127.0.0.1", "localhost", "::1"}
        ).check(self.endpoint)
        identifier = hashlib.sha256(self.run_key.encode()).hexdigest()
        # Stable ID persists before network access, permitting another caller to reconnect.
        raw = json.dumps(
            {
                "run_id": identifier,
                "request_sha256": hashlib.sha256(
                    json.dumps(request, sort_keys=True).encode()
                ).hexdigest(),
            }
        ).encode()
        self.store.blobs.put(self.run_key + "/agent-run.json", raw)
        atomic_write(self.store.root / self.run_key / "agent-run.json", raw)
        url = self.endpoint + "/api/v1/durable/runs/" + identifier
        deadline = time.monotonic() + request["timeout_seconds"] + 60
        poll = 0.2
        with httpx.Client(
            headers=self.headers, transport=self.transport, timeout=30, trust_env=False
        ) as client:
            response = client.post(url, json=request)
            response.raise_for_status()
            while True:
                row = response.json()
                if row["state"] == "complete":
                    self.store.materialize_prefix(self.run_key)
                    return row["result"]
                if row["state"] in {"failed", "uncertain", "cancelled"} or row.get("cancelled"):
                    raise RuntimeError(row.get("error") or "Agent run " + row["state"])
                if time.monotonic() > deadline:
                    raise httpx.ReadTimeout(
                        "Durable run still pending; rerun to retrieve its result"
                    )
                time.sleep(poll)
                poll = min(3, poll * 1.5)
                response = client.get(url)
                response.raise_for_status()

    def cancel(self):
        identifier = hashlib.sha256(self.run_key.encode()).hexdigest()
        with httpx.Client(headers=self.headers, timeout=10, trust_env=False) as client:
            response = client.delete(self.endpoint + "/api/v1/durable/runs/" + identifier)
            response.raise_for_status()
            return response.json()
