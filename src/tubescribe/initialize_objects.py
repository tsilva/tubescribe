"""Provision the explicit rehearsal bucket, with bounded startup retries."""

import os
import time


def main():
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    bucket = os.environ["TUBESCRIBE_S3_BUCKET"]
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["TUBESCRIBE_S3_ENDPOINT"],
        config=Config(
            connect_timeout=2,
            read_timeout=3,
            retries={"max_attempts": 0},
            s3={"addressing_style": "path"},
        ),
    )
    for attempt in range(30):
        try:
            try:
                client.head_bucket(Bucket=bucket)
            except ClientError as error:
                if error.response["Error"]["Code"] not in {"404", "NoSuchBucket", "NotFound"}:
                    raise
                client.create_bucket(Bucket=bucket)
            return
        except (BotoCoreError, ClientError):
            if attempt == 29:
                raise RuntimeError("Rehearsal object store did not become ready") from None
            time.sleep(1)


if __name__ == "__main__":
    main()
