from __future__ import annotations

import logging
import os
import re
from typing import Dict, Iterable, Tuple
from urllib.parse import unquote, urlencode, urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, redirect, request
import requests
from cryptography.fernet import Fernet, InvalidToken

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

_fernet = None

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

def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = os.getenv("USER_ID_ENC_KEY")
        if not key:
            raise RuntimeError("Missing USER_ID_ENC_KEY env var")
        _fernet = Fernet(key.encode("utf-8"))
    return _fernet

def decrypt_user_id(token: str) -> int:
    """
    Decrypt token back to user_id.
    Raises ValueError if token is invalid/tampered.
    """
    f = _get_fernet()
    try:
        raw = f.decrypt(token.encode("utf-8"))
        return int(raw.decode("utf-8"))
    except (InvalidToken, ValueError) as e:
        raise ValueError("Invalid user_id token") from e


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

def append_vary_header(headers, value: str) -> None:
    current = headers.get("Vary")
    if not current:
        headers["Vary"] = value
        return

    values = [part.strip() for part in current.split(",") if part.strip()]
    if value not in values:
        values.append(value)
        headers["Vary"] = ", ".join(values)

def get_allowed_origin() -> str | None:
    origin = request.headers.get("Origin")
    if not origin:
        return None

    parsed = urlparse(origin)
    host = parsed.netloc.split("@")[-1].split(":", 1)[0].lower()
    if parsed.scheme not in {"http", "https"} or not host:
        return None

    if host not in ALLOWED_HOSTS:
        return None

    return f"{parsed.scheme}://{parsed.netloc}"

def validate_user_id(user_id: str | None) -> str:
    if user_id is None:
        error_response(400, "user_id is required")

    cleaned = user_id.strip()
    if not cleaned:
        error_response(400, "user_id is required")

    if not re.match(r"^[A-Za-z0-9._-]+$", cleaned):
        error_response(400, "Invalid user_id")

    return cleaned

def user_id_from_seed(seed: str | None) -> str:
    """Decrypt seed token and return a validated user_id string."""
    if seed is None:
        error_response(400, "seed is required")

    token_raw = seed.strip()
    if not token_raw:
        error_response(400, "seed is required")

    try:
        token = unquote(token_raw)
        decrypted_user_id = str(decrypt_user_id(token))
    except ValueError:
        error_response(400, "Invalid seed")

    return validate_user_id(decrypted_user_id)

def validate_segments(segments: Iterable[str]) -> None:
    allowed = re.compile(r"^[A-Za-z0-9._-]+$")
    for seg in segments:
        if not allowed.match(seg):
            error_response(400, "Invalid path")

def parse_bucket_and_path(
    file: str | None, expected_user_id: str | None, expected_bucket: str | None = None
) -> Tuple[str, str]:
    if not file:
        error_response(400, "file path is required")

    cleaned = file.lstrip("/")
    parts = cleaned.split("/", 1)
    if len(parts) < 2:
        error_response(400, "bucket is required in file path")

    bucket_in_path, path_after_bucket = parts
    if expected_bucket and bucket_in_path != expected_bucket:
        error_response(403, "Invalid bucket")
    bucket = bucket_in_path

    if FILES_DIR and not path_after_bucket.startswith(FILES_DIR_PREFIX):
        error_response(403, "Invalid path")

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
        user_folder = validate_user_id(segments[1])
        remainder = segments[2:]
    else:
        if not segments:
            error_response(403, "Invalid user path")
        user_folder = validate_user_id(segments[0])
        remainder = segments[1:]

    if expected_user_id and user_folder != expected_user_id:
        error_response(403, "Invalid user path")

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

def resolve_view_path(path: str) -> str:
    if path.lower().endswith(".pdf"):
        return path

    base_path, _ = os.path.splitext(path)
    return f"{base_path}.pdf"

@app.after_request
def add_cors_headers(response: Response) -> Response:
    origin = get_allowed_origin()
    if not origin:
        return response

    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = request.headers.get(
        "Access-Control-Request-Headers",
        "Range, Content-Type",
    )
    response.headers["Access-Control-Expose-Headers"] = (
        "Accept-Ranges, Content-Disposition, Content-Length, Content-Range, "
        "Content-Type, ETag, Last-Modified"
    )
    response.headers["Access-Control-Max-Age"] = "3600"
    append_vary_header(response.headers, "Origin")
    append_vary_header(response.headers, "Access-Control-Request-Headers")
    return response

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

def parse_page_number(page_value: str | None) -> int:
    if page_value is None or not page_value.strip():
        return 1

    try:
        page = int(page_value)
    except ValueError:
        return 1

    return page if page > 0 else 1

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

    user_id = user_id_from_seed(request.args.get("seed"))
    bucket, path = parse_bucket_and_path(
        file_param,
        expected_user_id=user_id,
        expected_bucket=DEFAULT_BUCKET,
    )
    original_path = path
    path = resolve_view_path(path)
    logger.info("View request resolved path: original=%r resolved=%r", original_path, path)
    meta = ensure_object_exists(bucket, path)
    signed_url = build_presigned_url(bucket, path)

    page_raw = request.args.get("page")
    if path.lower().endswith(".pdf") and page_raw is not None:
        page = parse_page_number(page_raw)
        params = [(k, v) for k, values in request.args.lists() if k != "page" for v in values]
        query = urlencode(params, doseq=True)
        # Use a relative redirect so reverse-proxy path prefixes (e.g. /content_view/)
        # are preserved by the client.
        location = f"{f'?{query}' if query else ''}#page={page}"
        return redirect(location, code=302)

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

    user_id = user_id_from_seed(request.args.get("seed"))
    bucket, path = parse_bucket_and_path(
        file_param,
        expected_user_id=user_id,
        expected_bucket=DEFAULT_BUCKET,
    )
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
