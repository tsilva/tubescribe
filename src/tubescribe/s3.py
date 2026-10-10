"""S3-compatible durable objects with streamed, verified scratch materialization."""

import hashlib
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .blobs import ArtifactRef, StorageUnavailable, checked_key, file_digest


class S3Artifacts:
    def __init__(self, bucket, *, endpoint=None, client=None):
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    connect_timeout=10,
                    read_timeout=60,
                    retries={"mode": "standard", "max_attempts": 3},
                ),
            )
        self.bucket, self.client = bucket, client

    @contextmanager
    def request(self, key, *, missing=True):
        from boto3.exceptions import RetriesExceededError, S3UploadFailedError
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            yield
        except ClientError as error:
            if missing and error.response["Error"]["Code"] in {"NoSuchKey", "NotFound", "404"}:
                # HEAD cannot distinguish a missing key from an unavailable bucket.
                # Confirm the bucket before allowing recovery to infer missing content.
                try:
                    self.client.head_bucket(Bucket=self.bucket)
                except (ClientError, BotoCoreError) as bucket_error:
                    raise StorageUnavailable("Object bucket unavailable") from bucket_error
                raise FileNotFoundError(key) from error
            raise StorageUnavailable("Object store request failed") from error
        except (BotoCoreError, S3UploadFailedError, RetriesExceededError) as error:
            raise StorageUnavailable("Object store unavailable") from error

    def probe(self):
        with self.request(self.bucket, missing=False):
            self.client.head_bucket(Bucket=self.bucket)

    def put(self, key, content):
        key = checked_key(key)
        digest = hashlib.sha256(content).hexdigest()
        with self.request(key):
            self.client.put_object(
                Bucket=self.bucket, Key=key, Body=content, Metadata={"sha256": digest}
            )
        return ArtifactRef(key, digest)

    def put_file(self, key, source):
        key = checked_key(key)
        digest = file_digest(source)
        with self.request(key):
            self.client.upload_file(
                str(source), self.bucket, key, ExtraArgs={"Metadata": {"sha256": digest}}
            )
        if file_digest(source) != digest:
            raise ValueError("Source changed during upload")
        return ArtifactRef(key, digest)

    def stat(self, key):
        key = checked_key(key)
        with self.request(key):
            value = self.client.head_object(Bucket=self.bucket, Key=key)
        return {
            "bytes": value["ContentLength"],
            "modified_at": value["LastModified"].timestamp(),
            "sha256": value.get("Metadata", {}).get("sha256"),
        }

    def get(self, key):
        key = checked_key(key)
        with self.request(key):
            value = self.client.get_object(Bucket=self.bucket, Key=key)
            try:
                data = value["Body"].read()
            finally:
                value["Body"].close()
        expected = value.get("Metadata", {}).get("sha256")
        if expected and hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("Object checksum mismatch")
        return data

    def read(self, ref):
        data = self.get(ref.key)
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ValueError("Artifact content does not match its reference")
        return data

    def copy_to(self, key, target, checksum=None):
        key, target = checked_key(key), Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as output, self.request(key):
                value = self.client.get_object(Bucket=self.bucket, Key=key)
                digest = hashlib.sha256()
                try:
                    for chunk in value["Body"].iter_chunks(chunk_size=1024 * 1024):
                        digest.update(chunk)
                        output.write(chunk)
                finally:
                    value["Body"].close()
                actual = digest.hexdigest()
                expected = checksum or value.get("Metadata", {}).get("sha256")
                if expected and actual != expected:
                    raise ValueError("Artifact content does not match its reference")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return ArtifactRef(key, actual)

    def keys(self, prefix=""):
        names = []
        with self.request(prefix):
            for page in self.client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=prefix
            ):
                names.extend(checked_key(item["Key"]) for item in page.get("Contents", []))
        return sorted(names)

    def delete(self, key):
        key = checked_key(key)
        with self.request(key):
            self.client.delete_object(Bucket=self.bucket, Key=key)
