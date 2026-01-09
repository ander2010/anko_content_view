from __future__ import annotations

import logging
import os
import re
from typing import Dict, Iterable, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, request
import requests

# ------------------------------------------------------------------------------
# Setup
# ------------------------------------------------------------------------------

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("supabase-view")

CHUNK_SIZE = 64 * 1024  # 64KB chunks for efficient streaming

def env_truthy(value: str | None) -> bool:
    return bool(value and value.lower() in {"1", "true", "yes", "on"})

DEBUG_MODE = env_truthy(os.getenv("DEBUG")) or env_truthy(os.getenv("FLASK_DEBUG"))

S3_ENDPOINT = os.getenv("SUPABASE_S3_ENDPOINT", "")
S3_REGION = os.getenv("SUPABASE_S3_REGION", "")
DEFAULT_BUCKET = os.getenv("SUPABASE_S3_BUCKET", "")
S3_ACCESS_KEY = os.getenv("SUPABASE_S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.getenv("SUPABASE_S3_SECRET_KEY", "")
FILES_DIR = os.getenv("FILES_DIR", "documents").strip("/ ")
FILES_DIR_PREFIX = f"{FILES_DIR}/" if FILES_DIR else ""
ALLOWED_HOSTS = [
    host.strip().lower()
    for host in os.getenv("ALLOWED_HOSTS", "anko-swart.vercel.app").split(",")
    if host.strip()
]

if not all([S3_ENDPOINT, S3_REGION, S3_ACCESS_KEY, S3_SECRET_KEY, DEFAULT_BUCKET]):
    raise RuntimeError("Missing required Supabase S3 configuration")

s3_client = boto3.client(
    "s3",
    endpoint_url=S3_ENDPOINT,
    region_name=S3_REGION,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
    config=Config(signature_version="s3v4"),
)

app = Flask(__name__)

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def ensure_allowed_host():
    if DEBUG_MODE:
        return

    # Prefer Host header to align with typical reverse proxies
    host_header = request.headers.get("Host", "").lower()
    host = host_header.split(":", 1)[0]
    logger.info("Host check: header=%r parsed=%r allowed=%s", host_header, host, ALLOWED_HOSTS)
    if not host or host not in ALLOWED_HOSTS:
        error_response(403, "Host not allowed")

def error_response(status: int, detail: str):
    resp = jsonify({"detail": detail})
    resp.status_code = status
    return abort(resp)

def validate_user_id(user_id: str | None) -> str:
    if user_id is None:
        error_response(400, "user_id is required")

    cleaned = user_id.strip()
    if not cleaned:
        error_response(400, "user_id is required")

    if not re.match(r"^[A-Za-z0-9._-]+$", cleaned):
        error_response(400, "Invalid user_id")

    return cleaned

def validate_segments(segments: Iterable[str]) -> None:
    allowed = re.compile(r"^[A-Za-z0-9._-]+$")
    for seg in segments:
        if not allowed.match(seg):
            error_response(400, "Invalid path")

def parse_bucket_and_path(file: str | None, user_id: str | None) -> Tuple[str, str]:
    if not file:
        error_response(400, "file path is required")

    user_folder = validate_user_id(user_id)

    cleaned = file.lstrip("/")
    parts = cleaned.split("/", 1)

    bucket = DEFAULT_BUCKET
    path_after_bucket = cleaned

    if len(parts) == 2 and parts[0] != FILES_DIR:
        if parts[0] != DEFAULT_BUCKET:
            error_response(403, "Invalid bucket")
        path_after_bucket = parts[1]

    relative_path = path_after_bucket.lstrip("/")
    segments = [seg for seg in relative_path.split("/") if seg and seg != "."]
    if any(seg == ".." for seg in segments):
        error_response(400, "Invalid path")
    validate_segments(segments)

    if FILES_DIR:
        if not segments or segments[0] != FILES_DIR:
            error_response(403, "Invalid path")
        if len(segments) < 2:
            error_response(400, "user folder is required")
        if segments[1] != user_folder:
            error_response(403, "Invalid user path")
        remainder = segments[2:]
    else:
        if not segments or segments[0] != user_folder:
            error_response(403, "Invalid user path")
        remainder = segments[1:]

    if not remainder:
        error_response(400, "file path is required")

    path = "/".join([FILES_DIR, user_folder] + remainder) if FILES_DIR else "/".join([user_folder] + remainder)

    return bucket, path

def ensure_object_exists(bucket: str, path: str) -> Dict[str, str]:
    try:
        head = s3_client.head_object(Bucket=bucket, Key=path)
    except ClientError as exc:
        code = (exc.response.get("Error") or {}).get("Code")
        if code in {"404", "NotFound", "NoSuchKey"}:
            error_response(404, "File not found")
        error_response(502, f"S3 error: {code}")
    except BotoCoreError as exc:
        error_response(502, f"S3 error: {exc}")

    headers: Dict[str, str] = {}
    if head.get("ContentType"):
        headers["Content-Type"] = head["ContentType"]
    if head.get("ETag"):
        headers["ETag"] = head["ETag"]
    if head.get("LastModified"):
        headers["Last-Modified"] = head["LastModified"].strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )

    headers["Accept-Ranges"] = "bytes"
    return headers

def build_presigned_url(bucket: str, path: str) -> str:
    try:
        return s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": path},
            ExpiresIn=300,
        )
    except Exception as exc:
        error_response(502, f"Presign failed: {exc}")

def stream_from_supabase(
    url: str,
    range_header: str | None,
) -> Tuple[Iterable[bytes], int, Dict[str, str]]:

    session = requests.Session()
    headers = {}
    if range_header:
        headers["Range"] = range_header

    try:
        resp = session.get(
            url,
            headers=headers,
            stream=True,
            timeout=(10, 600),
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        session.close()
        error_response(502, f"Upstream error: {exc}")

    upstream_headers = {
        k: v
        for k, v in resp.headers.items()
        if k.lower()
        in {
            "content-type",
            "content-range",
            "accept-ranges",
            "etag",
            "last-modified",
        }
    }

    def generate():
        try:
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    yield chunk
        finally:
            resp.close()
            session.close()

    return generate(), resp.status_code, upstream_headers

def build_headers(
    meta: Dict[str, str],
    upstream: Dict[str, str],
    path: str,
    inline: bool,
) -> Dict[str, str]:
    headers = {**meta, **upstream}
    filename = path.rsplit("/", 1)[-1]
    disposition = "inline" if inline else "attachment"
    headers["Content-Disposition"] = f'{disposition}; filename="{filename}"'

    # Encourage browsers to render PDFs instead of downloading when S3 lacks a type.
    if path.lower().endswith(".pdf") and headers.get("Content-Type") in (None, "binary/octet-stream", "application/octet-stream"):
        headers["Content-Type"] = "application/pdf"

    # Remove Content-Length to prevent Flask/Werkzeug buffering
    headers.pop("Content-Length", None)
    return headers

# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------

@app.route("/view", methods=["GET"])
def view_file():
    file_param = request.args.get("file")
    if not file_param:
        error_response(400, "file path is required")

    ensure_allowed_host()

    bucket, path = parse_bucket_and_path(file_param, request.args.get("user_id"))
    meta = ensure_object_exists(bucket, path)
    signed_url = build_presigned_url(bucket, path)

    generator, status, upstream = stream_from_supabase(
        signed_url,
        request.headers.get("Range"),
    )

    headers = build_headers(meta, upstream, path, inline=True)

    return Response(
        generator,
        status=status,
        headers=headers,
        mimetype=headers.get("Content-Type", "application/pdf"),
        direct_passthrough=True,
    )

@app.route("/download", methods=["GET"])
def download_file():
    file_param = request.args.get("file")
    if not file_param:
        error_response(400, "file path is required")

    ensure_allowed_host()

    bucket, path = parse_bucket_and_path(file_param, request.args.get("user_id"))
    meta = ensure_object_exists(bucket, path)
    signed_url = build_presigned_url(bucket, path)

    generator, status, upstream = stream_from_supabase(
        signed_url,
        request.headers.get("Range"),
    )

    headers = build_headers(meta, upstream, path, inline=False)

    return Response(
        generator,
        status=status,
        headers=headers,
        mimetype=headers.get("Content-Type", "application/octet-stream"),
        direct_passthrough=True,
    )

# ------------------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8085)
