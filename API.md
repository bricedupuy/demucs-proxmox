# Demucs API Documentation

API version: **1.3.0**

The service provides asynchronous Demucs stem separation with an optional two-stage workflow:

1. run `htdemucs` immediately for a fast result;
2. keep the original source audio;
3. schedule `htdemucs_ft` for the configured HQ processing window;
4. notify the caller by signed webhook when either stage completes;
5. optionally upload each stage directly to Cloudflare R2 / any S3-compatible store.

The built-in OpenAPI/Swagger UI is also available at `/docs` and `/redoc`.

## Authentication

All endpoints except `GET /api/v1/health` require the API key:

```http
X-API-Key: YOUR_API_KEY
```

Retrieve the key inside the LXC with:

```bash
cat /etc/demucs-api/api-key
```

Do not put the API key in query parameters.

## Base URL

On a LAN:

```text
http://DEMUX_LXC_IP:8000
```

Behind a reverse proxy, set `DEMUX_PUBLIC_BASE_URL` to the externally visible origin, for example:

```ini
DEMUX_PUBLIC_BASE_URL=https://demucs.example.com
```

Returned `download_url` values will then be absolute URLs using that origin.

## Models

Supported model identifiers:

| Model | Typical use |
| --- | --- |
| `htdemucs` | Default fast/general model |
| `htdemucs_ft` | Fine-tuned, slower HQ model |
| `htdemucs_6s` | Six-source model |
| `hdemucs_mmi` | Hybrid Demucs model |
| `mdx` | MDX model |
| `mdx_extra` | MDX Extra |
| `mdx_q` | Quantized MDX |
| `mdx_extra_q` | Quantized MDX Extra |

The default installer pre-downloads `htdemucs`. Other models are downloaded by Demucs when first used and cached under `/var/cache/demucs-api/torch`.

## Compute devices

The API can expose one or more of:

```text
cpu
cuda
xpu
```

Use `GET /api/v1/models` to see what the installed worker actually permits. CPU always remains available. CUDA/XPU are only enabled if the installer verifies that PyTorch can use the accelerator inside the LXC.

---

# Endpoints

## Health

```http
GET /api/v1/health
```

Authentication: **not required**.

Example response:

```json
{
  "status": "ok",
  "queue_depth": 0,
  "queued_fast": 0,
  "scheduled_hq": 3,
  "default_model": "htdemucs",
  "hq_default_model": "htdemucs_ft",
  "default_device": "cpu",
  "allowed_devices": ["cpu"],
  "timezone": "Europe/Paris",
  "hq_window": {
    "start_hour": 2,
    "end_hour": 7
  }
}
```

## Models and devices

```http
GET /api/v1/models
X-API-Key: YOUR_API_KEY
```

Example response:

```json
{
  "models": [
    "hdemucs_mmi",
    "htdemucs",
    "htdemucs_6s",
    "htdemucs_ft",
    "mdx",
    "mdx_extra",
    "mdx_extra_q",
    "mdx_q"
  ],
  "default": "htdemucs",
  "hq_default": "htdemucs_ft",
  "devices": ["cpu"],
  "default_device": "cpu"
}
```

## Queue

```http
GET /api/v1/queue
X-API-Key: YOUR_API_KEY
```

Returns jobs that are currently waiting or processing.

Fast jobs have priority over HQ jobs. The worker does not interrupt an HQ separation already in progress, but it will not start another HQ job while fast work is waiting.

---

# Create jobs

## Upload an audio file

```http
POST /api/v1/jobs
Content-Type: multipart/form-data
X-API-Key: YOUR_API_KEY
```

Multipart fields:

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `file` | yes | | Input audio file |
| `model` | no | `htdemucs` | Fast-stage model |
| `device` | no | worker default | Fast-stage compute device |
| `two_stems` | no | | e.g. `vocals` |
| `hq_enabled` | no | `false` | Schedule an HQ second pass |
| `hq_model` | no | `htdemucs_ft` | HQ model |
| `hq_device` | no | fast device | HQ compute device |
| `hq_not_before` | no | next HQ window | ISO-8601 datetime |
| `callback_url` | no | | Webhook endpoint |
| `callback_secret` | no | | HMAC signing secret |

Example: fast now and HQ tonight:

```bash
curl -X POST 'https://demucs.example.com/api/v1/jobs' \
  -H 'X-API-Key: YOUR_API_KEY' \
  -F 'file=@song.wav' \
  -F 'model=htdemucs' \
  -F 'hq_enabled=true' \
  -F 'hq_model=htdemucs_ft' \
  -F 'callback_url=https://app.example.com/hooks/demucs' \
  -F 'callback_secret=YOUR_WEBHOOK_SECRET'
```

## Submit a remote URL

This is useful when MeTube or another internal service exposes the completed WAV over HTTP.

```http
POST /api/v1/jobs/url
Content-Type: application/json
X-API-Key: YOUR_API_KEY
```

Example:

```json
{
  "source_url": "http://metube.internal/downloads/song.wav",
  "fast": {
    "model": "htdemucs",
    "device": "cpu"
  },
  "hq": {
    "enabled": true,
    "model": "htdemucs_ft"
  },
  "callback_url": "https://app.example.com/hooks/demucs",
  "callback_secret": "YOUR_WEBHOOK_SECRET"
}
```

If `hq.not_before` is omitted, the API schedules the HQ pass for the next configured HQ window. With the defaults, that is the next time the local `Europe/Paris` clock enters **02:00-07:00**.

An explicit timestamp is also accepted:

```json
{
  "hq": {
    "enabled": true,
    "model": "htdemucs_ft",
    "not_before": "2026-10-01T02:00:00+02:00"
  }
}
```

### MeTube workflow

A typical orchestrator can implement:

```text
source URL
   -> MeTube download / WAV conversion
   -> obtain MeTube/internal WAV URL
   -> POST /api/v1/jobs/url
   -> receive fast.completed webhook
   -> use fast stems
   -> receive hq.completed webhook later
   -> switch application metadata to HQ stems
```

If R2 is enabled on the Demucs worker, the orchestrator never needs to proxy the WAV or returned stems through itself.

---

# Job status

```http
GET /api/v1/jobs/{job_id}
X-API-Key: YOUR_API_KEY
```

Example after the fast pass has completed:

```json
{
  "id": "06ba97c7-87f5-4a53-b208-f1ab34bf9fac",
  "status": "hq_scheduled",
  "source_name": "song.wav",
  "two_stems": null,
  "error": null,
  "fast": {
    "status": "completed",
    "model": "htdemucs",
    "device": "cpu",
    "completed_at": 1790851200.0,
    "files": [
      {
        "name": "vocals.wav",
        "download_url": "https://demucs.example.com/api/v1/jobs/06ba97c7-87f5-4a53-b208-f1ab34bf9fac/files/fast/vocals.wav"
      }
    ],
    "r2": null
  },
  "hq": {
    "enabled": true,
    "status": "scheduled",
    "model": "htdemucs_ft",
    "device": "cpu",
    "not_before_iso": "2026-10-01T02:00:00+02:00",
    "files": [],
    "r2": null
  },
  "callback_configured": true
}
```

Important overall `status` values include:

```text
queued
fast_processing
hq_scheduled
hq_processing
completed
fast_completed
failed
```

`fast_completed` means the fast result is still valid but the requested HQ upgrade failed.

---

# HQ scheduling

## Add/reschedule an HQ pass

An HQ upgrade can be added after the original job was created:

```http
POST /api/v1/jobs/{job_id}/hq
Content-Type: application/json
X-API-Key: YOUR_API_KEY
```

```json
{
  "model": "htdemucs_ft",
  "device": "cpu",
  "not_before": "2026-10-01T02:00:00+02:00"
}
```

`not_before` may be omitted to use the next configured HQ window.

## Cancel a scheduled HQ pass

```http
DELETE /api/v1/jobs/{job_id}/hq
X-API-Key: YOUR_API_KEY
```

A currently running HQ pass cannot be cancelled through this endpoint.

## HQ window behavior

Default configuration:

```ini
DEMUX_TIMEZONE=Europe/Paris
DEMUX_HQ_START_HOUR=2
DEMUX_HQ_END_HOUR=7
```

HQ work only **starts** while the configured window is open. A separation already running at the end of the window is allowed to finish. Only one HQ job is fed into the worker at a time, preventing the whole nightly backlog from being pre-queued past the end of the window.

Fast work always receives a higher queue priority than waiting HQ work.

---

# Results

## Download a stem

```http
GET /api/v1/jobs/{job_id}/files/{quality}/{filename}
X-API-Key: YOUR_API_KEY
```

`quality` is either:

```text
fast
hq
```

Examples:

```text
/api/v1/jobs/JOB_ID/files/fast/vocals.wav
/api/v1/jobs/JOB_ID/files/hq/vocals.wav
```

## Logs

Fast-stage log:

```http
GET /api/v1/jobs/{job_id}/log/fast
```

HQ-stage log:

```http
GET /api/v1/jobs/{job_id}/log/hq
```

For compatibility, `/api/v1/jobs/{job_id}/log` returns the fast-stage log.

## Delete a job

```http
DELETE /api/v1/jobs/{job_id}
X-API-Key: YOUR_API_KEY
```

The input file, local outputs and notification records are removed. Objects already uploaded to R2 are **not** deleted automatically.

---

# Webhooks

When `callback_url` is supplied, the API can emit:

```text
fast.completed
fast.failed
hq.started
hq.completed
hq.failed
```

Example payload:

```json
{
  "event": "hq.completed",
  "job_id": "06ba97c7-87f5-4a53-b208-f1ab34bf9fac",
  "timestamp": "2026-10-01T02:18:43+02:00",
  "job": {
    "id": "06ba97c7-87f5-4a53-b208-f1ab34bf9fac",
    "status": "completed",
    "fast": {},
    "hq": {}
  }
}
```

## Webhook signatures

If `callback_secret` was supplied, the raw request body is signed with HMAC-SHA256.

Header:

```http
X-Demucs-Signature: sha256=HEX_DIGEST
```

Verification pseudo-code:

```python
expected = hmac.new(secret.encode(), raw_request_body, hashlib.sha256).hexdigest()
valid = hmac.compare_digest(received_signature, f"sha256={expected}")
```

Always verify the signature against the **raw HTTP request body**, before JSON re-serialization.

## Retry policy

Webhook delivery is persisted in SQLite and retried independently from the Demucs processing queue.

Default attempts occur approximately:

```text
immediately
+30 seconds
+2 minutes
+10 minutes
+1 hour
```

The default maximum is five attempts. Polling `GET /api/v1/jobs/{job_id}` remains the authoritative fallback if every webhook attempt fails.

Inspect deliveries with:

```http
GET /api/v1/jobs/{job_id}/notifications
X-API-Key: YOUR_API_KEY
```

---

# Cloudflare R2 / S3-compatible output

Enable in `/etc/demucs-api/demucs-api.env`:

```ini
DEMUX_R2_ENABLED=true
DEMUX_R2_ENDPOINT=https://ACCOUNT_ID.r2.cloudflarestorage.com
DEMUX_R2_ACCESS_KEY_ID=...
DEMUX_R2_SECRET_ACCESS_KEY=...
DEMUX_R2_BUCKET=your-bucket
DEMUX_R2_PREFIX=demucs
```

Objects use separate fast/HQ prefixes:

```text
demucs/JOB_ID/fast/vocals.wav
demucs/JOB_ID/fast/drums.wav
demucs/JOB_ID/hq/vocals.wav
demucs/JOB_ID/hq/drums.wav
```

The job response returns bucket/object keys. Use credentials restricted to only the required bucket and operations.

---

# Reverse proxy deployment

The application should continue listening on the private LXC address/port, normally `0.0.0.0:8000`, while the reverse proxy handles TLS.

Recommended settings:

```ini
DEMUX_PUBLIC_BASE_URL=https://demucs.example.com
DEMUX_TRUST_PROXY_HEADERS=true
DEMUX_FORWARDED_ALLOW_IPS=10.0.0.20
```

`DEMUX_FORWARDED_ALLOW_IPS` should be the IP address of the reverse proxy that directly connects to the LXC. Do not use `*` unless the API port is separately firewalled so that **only** your trusted proxy can reach it.

After changing configuration:

```bash
systemctl restart demucs-api
```

## Nginx example

```nginx
server {
    listen 443 ssl http2;
    server_name demucs.example.com;

    client_max_body_size 1100M;

    location / {
        proxy_pass http://DEMUX_LXC_IP:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Only needed for long file uploads/downloads, not Demucs processing itself.
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
```

The separation itself is asynchronous, so the proxy does **not** need a multi-hour timeout while Demucs runs.

## Caddy example

```caddyfile
demucs.example.com {
    reverse_proxy DEMUX_LXC_IP:8000
}
```

Raise request-body limits in any middleware/WAF placed in front of Caddy if you intend to upload large WAV files through the public endpoint.

## Firewall recommendation

If the reverse proxy is on another server, restrict TCP/8000 on the LXC/Proxmox firewall so only:

- the reverse proxy; and
- explicitly trusted internal orchestrators

can reach it directly.

---

# Source URL security

Remote-source jobs intentionally support private URLs because the primary use case includes an internal MeTube server:

```ini
DEMUX_ALLOW_PRIVATE_SOURCE_URLS=true
```

This is an SSRF-sensitive feature. Keep the API key restricted to trusted services.

Private webhook destinations are disabled by default:

```ini
DEMUX_ALLOW_PRIVATE_CALLBACK_URLS=false
```

Set it to `true` only if your webhook receiver is also on the private network.

---

# Error codes

Common responses:

| HTTP | Meaning |
| ---: | --- |
| 202 | Job accepted/scheduled |
| 400 | Invalid model/device/URL/schedule |
| 401 | Missing or invalid API key |
| 404 | Job/file not found |
| 409 | Requested action conflicts with a running stage |
| 413 | Input exceeds configured maximum size |
| 422 | Invalid JSON/form schema |

Processing failures do not normally change the original submission response. They are reflected later in the job state and optional webhook event.
