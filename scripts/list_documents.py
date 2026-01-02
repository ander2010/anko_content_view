#!/usr/bin/env python
"""
List objects under the documents/ prefix in the configured Supabase S3 bucket.
Uses .env values: SUPABASE_S3_ENDPOINT, SUPABASE_S3_REGION, SUPABASE_S3_BUCKET,
SUPABASE_S3_ACCESS_KEY, SUPABASE_S3_SECRET_KEY.
"""

from __future__ import annotations

import argparse
import os
from typing import Iterable

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv


def load_env() -> None:
    load_dotenv()


def make_client():
    endpoint = os.getenv("SUPABASE_S3_ENDPOINT", "")
    region = os.getenv("SUPABASE_S3_REGION", "")
    access_key = os.getenv("SUPABASE_S3_ACCESS_KEY", "")
    secret_key = os.getenv("SUPABASE_S3_SECRET_KEY", "")

    missing = [k for k, v in {
        "SUPABASE_S3_ENDPOINT": endpoint,
        "SUPABASE_S3_REGION": region,
        "SUPABASE_S3_ACCESS_KEY": access_key,
        "SUPABASE_S3_SECRET_KEY": secret_key,
    }.items() if not v]
    if missing:
        raise SystemExit(f"Missing required env vars: {', '.join(missing)}")

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4"),
    )


def list_objects(client, bucket: str, prefix: str, limit: int | None) -> Iterable[str]:
    paginator = client.get_paginator("list_objects_v2")
    seen = 0

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        contents = page.get("Contents", [])
        for obj in contents:
            key = obj.get("Key")
            if key:
                yield key
                seen += 1
                if limit is not None and seen >= limit:
                    return


def main() -> None:
    load_env()
    files_dir = os.getenv("FILES_DIR", "documents").strip("/ ")
    default_prefix = f"{files_dir}/" if files_dir else ""

    parser = argparse.ArgumentParser(description="List files under the configured files directory in the Supabase bucket")
    parser.add_argument("--bucket", default=os.getenv("SUPABASE_S3_BUCKET", ""), help="Bucket name (default: env)")
    parser.add_argument("--prefix", default=default_prefix, help=f"Prefix to filter (default: {default_prefix or 'root'})")
    parser.add_argument("--limit", type=int, default=None, help="Max number of keys to show")
    args = parser.parse_args()

    if not args.bucket:
        raise SystemExit("Bucket is required (set SUPABASE_S3_BUCKET or pass --bucket)")

    client = make_client()

    try:
        for key in list_objects(client, args.bucket, args.prefix, args.limit):
            print(key)
    except (ClientError, BotoCoreError) as exc:
        raise SystemExit(f"Error listing objects: {exc}") from exc


if __name__ == "__main__":
    main()
