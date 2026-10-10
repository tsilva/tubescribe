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
    def get(self, key: str) -> bytes: ...
    def stat(self, key: str) -> dict: ...
    def keys(self, prefix: str = "") -> list[str]: ...
    def delete(self, key: str) -> None: ...
    def put_file(self, key: str, source: Path) -> ArtifactRef: ...
    def copy_to(self, key: str, target: Path, checksum: str | None = None) -> ArtifactRef: ...


def file_digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def copy_verified(source, target, checksum=None):
    import shutil

    source, target = Path(source), Path(target)
    digest = file_digest(source)
    if checksum and digest != checksum:
        raise ValueError("Artifact content does not match its reference")
    if source.resolve() == target.resolve():
        return digest
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as incoming:
            shutil.copyfileobj(incoming, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if file_digest(name) != digest:
            raise ValueError("Source changed during copy")
        os.replace(name, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)
    return digest


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

    def put_file(self, key, source):
        checksum = copy_verified(source, self.path(key))
        return ArtifactRef(checked_key(key), checksum)

    def copy_to(self, key, target, checksum=None):
        digest = copy_verified(self.path(key), target, checksum)
        return ArtifactRef(checked_key(key), digest)

    def stat(self, key):
        stat = self.path(key).stat()
        return {"bytes": stat.st_size, "modified_at": stat.st_mtime}

    def get(self, key):
        return self.path(key).read_bytes()

    def keys(self, prefix=""):
        return [
            p.relative_to(self.root).as_posix()
            for p in sorted(self.root.rglob("*"))
            if p.is_file()
            and not p.is_symlink()
            and p.relative_to(self.root).as_posix().startswith(prefix)
        ]

    def delete(self, key):
        self.path(key).unlink(missing_ok=True)

    def read(self, ref):
        content = self.get(ref.key)
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

    def put_file(self, key, source):
        return self.put(key, Path(source).read_bytes())

    def copy_to(self, key, target, checksum=None):
        content = self.get(key)
        digest = hashlib.sha256(content).hexdigest()
        if checksum and checksum != digest:
            raise ValueError("Artifact content does not match its reference")
        atomic_write(target, content)
        return ArtifactRef(checked_key(key), digest)

    def stat(self, key):
        return {"bytes": len(self.get(key)), "modified_at": 0}

    def get(self, key):
        try:
            return self.objects[checked_key(key)]
        except KeyError as error:
            raise FileNotFoundError(key) from error

    def keys(self, prefix=""):
        return sorted(k for k in self.objects if k.startswith(prefix))

    def delete(self, key):
        self.objects.pop(checked_key(key), None)

    def read(self, ref):
        content = self.get(ref.key)
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


class DirectoryObjects:
    """Persistent object adapter exposing keys and bytes, never filesystem paths.

    A separate durable volume exercises the same interface as a remote object adapter.
    """

    def __init__(self, directory):
        self._local = LocalArtifacts(directory)

    def put(self, key, content):
        return self._local.put(key, content)

    def read(self, ref):
        return self._local.read(ref)

    def put_file(self, key, source):
        return self._local.put_file(key, source)

    def copy_to(self, key, target, checksum=None):
        return self._local.copy_to(key, target, checksum)

    def stat(self, key):
        return self._local.stat(key)

    def get(self, key):
        return self._local.get(key)

    def keys(self, prefix=""):
        return self._local.keys(prefix)

    def delete(self, key):
        self._local.delete(key)
