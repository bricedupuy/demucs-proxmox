# Demucs API for Proxmox LXC

A Community-Scripts-style Proxmox VE installer for a dedicated **Debian 13 LXC** running **Demucs 4.1.0** behind a FastAPI REST API.

It is designed for an orchestrated workflow such as:

```text
URL -> MeTube -> Demucs fast pass -> R2
                     |
                     +-> scheduled nightly HQ pass -> R2 -> signed webhook
```

## Features

- Debian 13 unprivileged LXC
- Default / Advanced guided creation
- Configurable CT ID, vCPU, RAM, swap, storage, bridge, VLAN and IP addressing
- Demucs 4.1.0
- Guided model choice with optional model pre-download
- CPU backend by default
- Optional/experimental NVIDIA CUDA and Intel XPU installation paths with runtime verification and CPU fallback
- FastAPI REST API + built-in OpenAPI UI
- API-key authentication
- Persistent SQLite job and webhook state
- One Demucs separation at a time
- Fast-job priority over scheduled HQ work
- `htdemucs` now + `htdemucs_ft` nightly upgrade workflow
- Signed webhooks with retry persistence
- Direct file upload
- Remote HTTP/HTTPS audio input (including internal MeTube URLs)
- Optional direct Cloudflare R2/S3 upload
- Automatic job cleanup
- Reverse-proxy-aware public URLs and trusted forwarding configuration
- `/usr/bin/update` updater inside the LXC
- systemd service

Full endpoint documentation is in **[API.md](API.md)**.

## Repository layout

```text
ct/demucs-api.sh                 Proxmox LXC creator + in-LXC updater
install/demucs-api-install.sh    First-install script
app/main.py                      REST API, scheduler, worker and webhooks
app/requirements.txt             Python dependencies
app/VERSION                      API version
json/demucs-api.json             Community Scripts-style metadata
API.md                           API and reverse-proxy documentation
```

## Install

Run on the Proxmox VE host as root:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/bricedupuy/demucs-proxmox/main/ct/demucs-api.sh)"
```

Default installation:

```text
Debian 13
Unprivileged LXC
6 vCPU
8 GB RAM
2 GB swap
24 GB disk
DHCP / vmbr0
CPU compute
htdemucs default model
htdemucs pre-downloaded
htdemucs_ft HQ model
HQ window 02:00-07:00 Europe/Paris
```

Advanced setup adds model selection, model preloading, compute backend, PyTorch wheel index and HQ scheduling settings in addition to the container/network options.

## GPU support

The guided installer offers:

```text
CPU
Auto-detect
NVIDIA CUDA
Intel XPU
```

CPU is the production-safe default.

GPU passthrough in LXC depends on the Proxmox host, driver versions and device permissions. The installer performs basic device passthrough and then verifies the accelerator **inside the container** with PyTorch. If CUDA/XPU is not actually usable, the service falls back to CPU instead of leaving a broken API.

`Auto-detect` currently selects CUDA when `/dev/nvidia0` exists; otherwise it selects CPU. It deliberately does not auto-select Intel XPU because older Intel iGPUs such as UHD 630 expose `/dev/dri` but are not suitable PyTorch XPU targets.

## Fast + nightly HQ processing

A job can ask for an immediate fast result and a scheduled HQ upgrade:

```json
{
  "source_url": "http://metube.internal/downloads/song.wav",
  "fast": {
    "model": "htdemucs"
  },
  "hq": {
    "enabled": true,
    "model": "htdemucs_ft"
  },
  "callback_url": "https://app.example.com/hooks/demucs",
  "callback_secret": "SHARED_SECRET"
}
```

The worker processes `htdemucs` immediately. The original source is retained and the `htdemucs_ft` pass becomes eligible during the configured HQ window.

The external service can either poll `GET /api/v1/jobs/{id}` or receive signed events such as:

```text
fast.completed
hq.started
hq.completed
hq.failed
```

## Cloudflare R2

R2 is optional. When enabled, fast and HQ results are uploaded separately:

```text
demucs/JOB_ID/fast/...
demucs/JOB_ID/hq/...
```

Configure `/etc/demucs-api/demucs-api.env`:

```ini
DEMUX_R2_ENABLED=true
DEMUX_R2_ENDPOINT=https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com
DEMUX_R2_ACCESS_KEY_ID=...
DEMUX_R2_SECRET_ACCESS_KEY=...
DEMUX_R2_BUCKET=your-bucket
DEMUX_R2_PREFIX=demucs
```

Use an R2 token limited to only the required bucket.

## Reverse proxy

Do not expose LXC port 8000 directly to the Internet. Put TLS/authentication/rate controls at your reverse proxy and keep the API key enabled.

When the proxy is configured, set:

```ini
DEMUX_PUBLIC_BASE_URL=https://demucs.example.com
DEMUX_TRUST_PROXY_HEADERS=true
DEMUX_FORWARDED_ALLOW_IPS=10.0.0.20
```

Use the actual IP of the reverse proxy for `DEMUX_FORWARDED_ALLOW_IPS`, not `*`, unless port 8000 is separately restricted to the proxy.

For large direct uploads, raise the proxy request-body limit (the API default is 1024 MB). Demucs execution itself is asynchronous, so you do not need a multi-hour reverse-proxy request timeout while separation runs.

See [API.md](API.md) for Nginx/Caddy examples and security notes.

## API key

```bash
pct exec <CTID> -- cat /etc/demucs-api/api-key
```

## Configuration

Main file:

```text
/etc/demucs-api/demucs-api.env
```

Notable options:

```ini
DEMUX_DEFAULT_MODEL=htdemucs
DEMUX_HQ_DEFAULT_MODEL=htdemucs_ft
DEMUX_DEVICE=cpu
DEMUX_ALLOWED_DEVICES=cpu
DEMUX_CPU_JOBS=6
DEMUX_MAX_UPLOAD_MB=1024
DEMUX_JOB_TTL_HOURS=48
DEMUX_TIMEZONE=Europe/Paris
DEMUX_HQ_START_HOUR=2
DEMUX_HQ_END_HOUR=7
DEMUX_PUBLIC_BASE_URL=
DEMUX_TRUST_PROXY_HEADERS=false
DEMUX_FORWARDED_ALLOW_IPS=127.0.0.1
DEMUX_ALLOW_PRIVATE_SOURCE_URLS=true
DEMUX_ALLOW_PRIVATE_CALLBACK_URLS=false
```

Restart after editing:

```bash
systemctl restart demucs-api
```

## Update

Inside the LXC:

```bash
update
```

The updater pulls the latest application files and requirements from the configured GitHub repository and restarts the service.

## Useful commands

```bash
systemctl status demucs-api
journalctl -u demucs-api -f
demucs-api-info
update
```

Interactive API documentation after install:

```text
http://LXC_IP:8000/docs
```

## Security notes

- Keep the API key private.
- Prefer HTTPS at the reverse proxy.
- Firewall port 8000 to your reverse proxy and trusted internal services.
- Remote source URLs are allowed to reach private addresses by default because internal MeTube is a target use case. Treat this as an SSRF-sensitive capability and give API credentials only to trusted callers.
- Private webhook URLs are disabled by default.
- Configure a per-integration webhook secret and verify `X-Demucs-Signature` on the receiving server.

## Community Scripts compatibility

The project follows the Community Scripts shape and user experience (`ct/`, `install/`, `json/`, guided defaults/advanced options, and an in-container `update` command) but remains self-contained and does not depend on their repository at runtime.

## License

MIT
