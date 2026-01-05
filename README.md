# Supabase S3 viewer

A tiny Flask service that streams and proxies Supabase S3 objects so users can view or download files without exposing your S3 credentials.

## Configuration

Set the following environment variables (see `.env.example` pattern if you have one):

- `SUPABASE_S3_ENDPOINT` – Supabase storage S3 endpoint URL (e.g., `https://<project>.supabase.co/storage/v1/s3`)
- `SUPABASE_S3_REGION` – Region for the storage bucket
- `SUPABASE_S3_BUCKET` – Default bucket name
- `SUPABASE_S3_ACCESS_KEY` / `SUPABASE_S3_SECRET_KEY` – S3 access credentials
- `FILES_DIR` – (optional) Base folder to scope access, defaults to `documents`
- `LOG_LEVEL` – (optional) Logging level, defaults to `INFO`
- `DEBUG` or `FLASK_DEBUG` – (optional) When truthy, disables bearer auth requirement
- `ALLOWED_HOSTS` – Comma-separated list of hostnames allowed to reach the service (default: `anko-swart.vercel.app`)

## Run

### Local

```bash
pip install -r requirements.txt
export SUPABASE_S3_ENDPOINT=...
export SUPABASE_S3_REGION=...
export SUPABASE_S3_BUCKET=...
export SUPABASE_S3_ACCESS_KEY=...
export SUPABASE_S3_SECRET_KEY=...
flask --app app run --host 0.0.0.0 --port 8085
```

### Docker

```bash
docker build -t supabase-s3-view .
docker run -p 8085:8085 \
  -e SUPABASE_S3_ENDPOINT=... \
  -e SUPABASE_S3_REGION=... \
  -e SUPABASE_S3_BUCKET=... \
  -e SUPABASE_S3_ACCESS_KEY=... \
  -e SUPABASE_S3_SECRET_KEY=... \
  supabase-s3-view
```

## Usage

Two endpoints accept `GET` requests:

- `/view?file=<path>` – Streams the object and forces inline rendering.
- `/download?file=<path>` – Streams the object with `attachment` disposition.

Notes:

- When `DEBUG`/`FLASK_DEBUG` is falsy, requests must come from an allowed `Host` header (default `anko-swart.vercel.app`); no Authorization header is required.
- Paths are restricted to the `FILES_DIR` prefix. If you include the bucket name in `file`, it must match `SUPABASE_S3_BUCKET`.
- Range requests are forwarded, so partial downloads work.

### Examples

View a PDF inline:

```bash
curl "http://localhost:8085/view?file=reports/invoice.pdf" \
  -H "Authorization: Bearer <token>"
```

Download a file:

```bash
curl -L "http://localhost:8085/download?file=reports/data.csv" \
  -H "Authorization: Bearer <token>" \
  -o data.csv
```

Request from a specific bucket name in the path (must equal the configured bucket):

```bash
curl "http://localhost:8085/view?file=my-bucket/reports/invoice.pdf" \
  -H "Authorization: Bearer <token>"
```
