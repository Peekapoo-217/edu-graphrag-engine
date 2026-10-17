"""Create the configured private bucket in a local S3-compatible store."""

from __future__ import annotations

import os
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError


def ensure_bucket(client, bucket: str) -> None:
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if status != 404 and code not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        try:
            client.create_bucket(Bucket=bucket)
        except ClientError as create_error:
            create_code = str(create_error.response.get("Error", {}).get("Code", ""))
            if create_code not in {"BucketAlreadyExists", "BucketAlreadyOwnedByYou"}:
                raise


def main() -> None:
    endpoint = os.environ["OBJECT_STORAGE_ENDPOINT"]
    bucket = os.environ["OBJECT_STORAGE_BUCKET"]
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["OBJECT_STORAGE_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["OBJECT_STORAGE_SECRET_ACCESS_KEY"],
        region_name=os.getenv("OBJECT_STORAGE_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )
    last_error = None
    for _ in range(30):
        try:
            ensure_bucket(client, bucket)
            return
        except EndpointConnectionError as exc:
            last_error = exc
            time.sleep(2)
    raise RuntimeError("Object storage endpoint did not become available") from last_error


if __name__ == "__main__":
    main()
