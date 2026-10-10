"""One durable submission interface for CLI execution and dashboard dispatch."""

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass

from .execution import assert_owned, lease, submission_gate


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
            k: v
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
            return self.spawn(
                command, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True
            ).pid


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
        db = self.store.db
        stamp = time.time()
        # A queue launch is short; deduplication is durable across dashboard restarts.
        with submission_gate(self.store.root, "submit:" + request.dedupe):
            with db:
                assert_owned(db, self.store.root)
                row = db.execute(
                    "SELECT id,pid,updated FROM submissions WHERE dedupe=? AND state IN ('queued','active')",
                    (request.dedupe,),
                ).fetchone()
                if row:
                    owned = db.execute(
                        "SELECT 1 FROM claims WHERE resource=? AND expires>?",
                        ("submission:" + row[0], stamp),
                    ).fetchone()
                    if owned or row[2] > stamp - 90:
                        return row[0]
                    db.execute(
                        "UPDATE submissions SET state='interrupted',updated=? WHERE id=?",
                        (stamp, row[0]),
                    )
                identifier = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO submissions VALUES(?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        request.dedupe,
                        request.encode(),
                        "queued",
                        None,
                        None,
                        stamp,
                        stamp,
                    ),
                )
            if background:
                try:
                    pid = self.process.start(self.store.root, request, identifier)
                    with db:
                        db.execute(
                            "UPDATE submissions SET pid=?,updated=? WHERE id=?",
                            (pid, time.time(), identifier),
                        )
                except BaseException as error:
                    with db:
                        db.execute(
                            "UPDATE submissions SET state='failed',error=?,updated=? WHERE id=?",
                            (str(error), time.time(), identifier),
                        )
                    raise
            return identifier

    def execute(self, request, adapter, *, identifier=None):
        identifier = identifier or self.submit(request)
        db = self.store.db
        row = db.execute(
            "SELECT request,state FROM submissions WHERE id=?", (identifier,)
        ).fetchone()
        if not row or row[0] != request.encode() or row[1] not in ("queued", "active"):
            raise ValueError("Submission does not match this worker request")
        with lease(self.store.root, "submission:" + identifier):
            with db:
                assert_owned(db, self.store.root)
                db.execute(
                    "UPDATE submissions SET state='active',pid=?,updated=? WHERE id=?",
                    (os.getpid(), time.time(), identifier),
                )
            try:
                # Waiting submissions heartbeat independently while archive capacity is full.
                while (
                    request.options.get("wait")
                    and db.execute(
                        "SELECT 1 FROM claims WHERE resource='archive' AND expires>?",
                        (time.time(),),
                    ).fetchone()
                ):
                    time.sleep(3)
                result = adapter.execute()
            except BaseException as error:
                with db:
                    db.execute(
                        "UPDATE submissions SET state='interrupted',error=?,updated=? WHERE id=?",
                        (str(error), time.time(), identifier),
                    )
                raise
            with db:
                assert_owned(db, self.store.root)
                db.execute(
                    "UPDATE submissions SET state=?,updated=? WHERE id=?",
                    ("failed" if result.get("failed") else "complete", time.time(), identifier),
                )
            return result


def execute_cli(store, args, operation):
    excluded = {"data", "submission", "command", "list_workflows"}
    options = {k: v for k, v in vars(args).items() if k not in excluded}
    request = JobRequest(args.command, json.loads(json.dumps(options, default=str)))
    return Dispatcher(store).execute(request, Inline(operation), identifier=args.submission)
