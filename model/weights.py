"""Fetch model weights from S3 to local disk once, at startup.

Why S3 and not baked into the image: weights are 1.5-6 GB. In the image they would be
re-pushed on every code change and pulled through the NAT gateway (paid per GB) on every
start; from S3 they come through the free gateway endpoint, and the host volume keeps them
across task restarts. scripts/upload_models.sh puts them there.
"""

import logging
from pathlib import Path

log = logging.getLogger("model.weights")


def fetch(bucket: str, prefix: str, dest: str) -> int:
    """Download every object under s3://bucket/prefix that is missing locally or has a
    different size. Returns the number of files downloaded."""
    import boto3

    s3 = boto3.client("s3")
    downloaded = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix) :].lstrip("/")
            if not rel or rel.endswith("/"):
                continue
            target = Path(dest) / rel
            if target.exists() and target.stat().st_size == obj["Size"]:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            log.info("downloading s3://%s/%s (%d MB)", bucket, obj["Key"], obj["Size"] >> 20)
            partial = target.with_suffix(target.suffix + ".part")
            s3.download_file(bucket, obj["Key"], str(partial))
            partial.rename(target)
            downloaded += 1
    return downloaded
