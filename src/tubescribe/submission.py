"""One durable submission interface for CLI execution and dashboard dispatch."""

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass

from .execution import lease, submission_gate


@dataclass(frozen=True)
class JobRequest:
    command: str
    options: dict

    def encode(self):
        if self.command not in ("work", "postprocess", "diarize", "artifacts"):
            raise ValueError("Unknown worker command")
        return json.dumps(
            {"command": self.command, "options": self.normalized()}, sort_keys=True, default=str
        )

    def normalized(self):
        defaults = {
            "profile": "turbo-v1",
            "max_attempts": 3,
            "workflow": "summary",
            "mode": "video",
            "postprocessing": "video",
        }
        return {
            k: sorted(set(v)) if k == "video" and isinstance(v, list) else v
            for k, v in self.options.items()
            if v is not None
            and v is not False
            and (k not in defaults or v != defaults[k])
            and (k != "endpoint" or self.command == "work")
        }

    @property
    def dedupe(self):
        return hashlib.sha256(self.encode().encode()).hexdigest()


class LocalProcess:
    def __init__(self, *, spawn=None):
        self.spawn = spawn or subprocess.Popen
        self.children = {}

    def start(self, root, request, submission):
        command = [
            sys.executable,
            "-m",
            "tubescribe.cli",
            "--data",
            str(root),
            "--submission",
            submission,
        ]
        endpoint = request.options.get("endpoint")
        if endpoint:
            command += ["--endpoint", endpoint]
        command.append(request.command)
        for key, value in request.options.items():
            if key == "endpoint" or value is None or value is False:
                continue
            flag = "--" + key.replace("_", "-")
            if value is True:
                command.append(flag)
            elif isinstance(value, list):
                for item in value:
                    command += [flag, str(item)]
            else:
                command += [flag, str(value)]
        folder = root / "quality" / "worker-logs"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / (submission + ".log")).open("ab") as handle:
            child = self.spawn(
                command, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True
            )
            self.children[child.pid] = child
            return child.pid


class Inline:
    """Local CLI adapter: execution remains synchronous and returns its result."""

    def __init__(self, operation):
        self.operation = operation

    def execute(self):
        return self.operation()


class Dispatcher:
    def __init__(self, store, *, process=None):
        self.store = store
        self.process = process or LocalProcess()

    def submit(self, request, *, background=False):
        with submission_gate(self.store.root, "submit:" + request.dedupe):
            identifier, created = self.store.enqueue(request.encode(), request.dedupe)
            if background and created and not self.store.config.queued:
                try:
                    pid = self.process.start(self.store.root, request, identifier)
                    self.store.update_submission(identifier, pid=pid)
                except BaseException as error:
                    self.store.update_submission(identifier, state="failed", error=str(error))
                    raise
            return identifier

    def execute(self, request, adapter, *, identifier=None):
        identifier = identifier or self.submit(request)
        with lease(self.store.root, "submission:" + identifier):
            row = self.store.submission(identifier)
            if not row or row["request"] != request.encode():
                raise ValueError("Submission does not match this worker request")
            if row["state"] == "complete" and row["result"]:
                return json.loads(row["result"])
            if row["state"] not in {"queued", "active"}:
                raise ValueError("Submission cannot execute in its current state")
            self.store.update_submission(identifier, state="active", pid=os.getpid())
            try:
                while request.options.get("wait") and self.store.owned("archive"):
                    time.sleep(3)
                from .execution import CURRENT_SUBMISSION

                token = CURRENT_SUBMISSION.set(identifier)
                try:
                    self.store.record_event(
                        identifier, "queue_started", {"queue_seconds": time.time() - row["created"]}
                    )
                    result = adapter.execute()
                finally:
                    CURRENT_SUBMISSION.reset(token)
            except BaseException as error:
                self.store.update_submission(
                    identifier, state="interrupted", error=str(error), pid=None
                )
                raise
            self.store.update_submission(
                identifier,
                state="failed" if result.get("failed") else "complete",
                pid=None,
                result=json.dumps(result),
            )
            return result


def execute_cli(store, args, operation):
    excluded = {"data", "submission", "command", "list_workflows"}
    options = {k: v for k, v in vars(args).items() if k not in excluded}
    request = JobRequest(args.command, json.loads(json.dumps(options, default=str)))
    return Dispatcher(store).execute(request, Inline(operation), identifier=args.submission)


def consume(store, *, once=False, poll=None, process=None):
    """Independent dispatcher; durable queued requests survive the web process."""
    process = process or LocalProcess()
    completed = failed = 0
    with lease(store.root, "dispatcher", reentrant=False):
        while True:
            store.reclaim_submissions()
            rows = store.queued_submissions()
            for row in rows:
                request = JobRequest(**json.loads(row["request"]))
                try:
                    pid = process.start(store.root, request, row["id"])
                    store.update_submission(row["id"], pid=pid)
                    # Keep archive capacity conservative; child handles submission/job fencing.
                    if process.spawn is subprocess.Popen:
                        while True:
                            child = process.children.get(pid)
                            if child is not None and child.poll() is not None:
                                break
                            current = store.submission(row["id"])
                            if current["state"] not in {"queued", "active"}:
                                break
                            try:
                                os.kill(pid, 0)
                            except ProcessLookupError:
                                break
                            time.sleep(poll or store.config.worker_poll)
                    completed += 1
                except Exception as error:
                    store.update_submission(row["id"], state="failed", error=str(error), pid=None)
                    failed += 1
            if once:
                return {"dispatched": completed, "failed": failed}
            time.sleep(poll or store.config.worker_poll)
