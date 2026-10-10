"""Content storage independent of SQLite and delivery URLs."""

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol


@dataclass(frozen=True)
class ArtifactRef:
    key: str
    sha256: str


def checked_key(key):
    path = PurePosixPath(key)
    if not key or path.is_absolute() or ".." in path.parts or "\\" in key:
        raise ValueError("Artifact key must be relative and confined")
    return path.as_posix()


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(name).unlink(missing_ok=True)


class ArtifactStore(Protocol):
    def put(self, key: str, content: bytes) -> ArtifactRef: ...
    def read(self, ref: ArtifactRef) -> bytes: ...


class LocalArtifacts:
    def __init__(self, root, *, writer=atomic_write):
        self.root = Path(root).resolve()
        self.writer = writer

    def path(self, key):
        path = (self.root / checked_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Artifact path escapes the store")
        return path

    def put(self, key, content):
        path = self.path(key)
        if path.is_file() and path.read_bytes() == content:
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
            directory = path.parent
            while directory.is_relative_to(self.root):
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                if directory == self.root:
                    break
                directory = directory.parent
        else:
            self.writer(path, content)
        return ArtifactRef(checked_key(key), hashlib.sha256(content).hexdigest())

    def read(self, ref):
        content = self.path(ref.key).read_bytes()
        if hashlib.sha256(content).hexdigest() != ref.sha256:
            raise ValueError("Artifact content does not match its reference")
        return content


class MemoryArtifacts:
    """Executable storage adapter for interface tests and ephemeral archives."""

    def __init__(self):
        self.objects = {}

    def put(self, key, content):
        self.objects[checked_key(key)] = bytes(content)
        return ArtifactRef(checked_key(key), hashlib.sha256(content).hexdigest())

    def read(self, ref):
        content = self.objects[checked_key(ref.key)]
        if hashlib.sha256(content).hexdigest() != ref.sha256:
            raise ValueError("Artifact content does not match its reference")
        return content


def publish_bundle(blobs, directory, key, *, writer=atomic_write):
    """Publish verified content before callers commit a database pointer."""
    files = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.name != "manifest.json" and not path.name.endswith(".tmp"):
            if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
                raise ValueError("Bundle contains an external resource")
            relative = path.relative_to(directory).as_posix()
            content = path.read_bytes()
            ref = blobs.put(checked_key(key + "/" + relative), content)
            # A broken adapter must never produce a published pointer.
            if blobs.read(ref) != content:
                raise ValueError("Artifact store failed publication verification")
            files[relative] = ref.sha256
    content = json.dumps({"files": files}, sort_keys=True).encode()
    writer(directory / "manifest.json", content)
    ref = blobs.put(key + "/manifest.json", content)
    blobs.read(ref)
    return ref
