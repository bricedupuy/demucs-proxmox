#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${DEMUX_API_REPO:?DEMUX_API_REPO is required}"
BRANCH="${DEMUX_API_BRANCH:-main}"
BASE_URL="https://raw.githubusercontent.com/${REPO}/${BRANCH}"
APP_DIR="/opt/demucs-api"
DATA_DIR="/var/lib/demucs-api"
CACHE_DIR="/var/cache/demucs-api"
CONF_DIR="/etc/demucs-api"

INSTALL_DEVICE="${DEMUX_INSTALL_DEVICE:-cpu}"
DEFAULT_MODEL="${DEMUX_INSTALL_MODEL:-htdemucs}"
PRELOAD_MODEL="${DEMUX_PRELOAD_MODEL:-true}"
TORCH_INDEX_URL="${DEMUX_TORCH_INDEX_URL:-}"
CPU_JOBS="${DEMUX_INSTALL_CPU_JOBS:-6}"
HQ_MODEL="${DEMUX_INSTALL_HQ_MODEL:-htdemucs_ft}"
TIMEZONE="${DEMUX_INSTALL_TIMEZONE:-Europe/Paris}"
HQ_START_HOUR="${DEMUX_INSTALL_HQ_START_HOUR:-2}"
HQ_END_HOUR="${DEMUX_INSTALL_HQ_END_HOUR:-7}"

export DEBIAN_FRONTEND=noninteractive
msg(){ printf '\n==> %s\n' "$*"; }
warn(){ printf '\nWARNING: %s\n' "$*" >&2; }

msg "Updating Debian"
apt-get update
apt-get -y dist-upgrade

msg "Installing dependencies"
apt-get install -y --no-install-recommends \
  ca-certificates curl ffmpeg git python3 python3-venv python3-pip sqlite3 libsndfile1 tzdata

msg "Creating service account and directories"
id demucs-api >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin demucs-api
mkdir -p "$APP_DIR" "$DATA_DIR/jobs" "$CACHE_DIR/torch" "$CONF_DIR"
chown -R demucs-api:demucs-api "$DATA_DIR" "$CACHE_DIR"

msg "Downloading application files"
curl -fsSL "$BASE_URL/app/main.py" -o "$APP_DIR/main.py"
curl -fsSL "$BASE_URL/app/requirements.txt" -o "$APP_DIR/requirements.txt"
curl -fsSL "$BASE_URL/app/VERSION" -o "$APP_DIR/VERSION"

msg "Creating Python environment"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip wheel setuptools

case "$INSTALL_DEVICE" in
  cpu)
    TORCH_INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cpu}"
    ;;
  cuda)
    TORCH_INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cu130}"
    ;;
  xpu)
    TORCH_INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/xpu}"
    ;;
  *)
    warn "Unknown compute backend '$INSTALL_DEVICE'; falling back to CPU"
    INSTALL_DEVICE="cpu"
    TORCH_INDEX_URL="https://download.pytorch.org/whl/cpu"
    ;;
esac

msg "Installing PyTorch backend: $INSTALL_DEVICE"
"$APP_DIR/.venv/bin/pip" install --index-url "$TORCH_INDEX_URL" torch torchaudio
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# Verify the requested accelerator from inside the actual LXC. If it is not usable,
# keep the installation functional by switching the service to CPU.
ACTIVE_DEVICE="$INSTALL_DEVICE"
if [[ "$INSTALL_DEVICE" == "cuda" ]]; then
  if ! "$APP_DIR/.venv/bin/python" - <<'PY' >/dev/null 2>&1
import torch
raise SystemExit(0 if torch.cuda.is_available() else 1)
PY
  then
    warn "CUDA was requested but torch.cuda.is_available() is false inside the LXC. Falling back to CPU. Check Proxmox GPU passthrough and NVIDIA user-space driver compatibility."
    ACTIVE_DEVICE="cpu"
  fi
elif [[ "$INSTALL_DEVICE" == "xpu" ]]; then
  if ! "$APP_DIR/.venv/bin/python" - <<'PY' >/dev/null 2>&1
import torch
raise SystemExit(0 if hasattr(torch, "xpu") and torch.xpu.is_available() else 1)
PY
  then
    warn "Intel XPU was requested but torch.xpu.is_available() is false inside the LXC. Falling back to CPU."
    ACTIVE_DEVICE="cpu"
  fi
fi

# If an explicitly requested accelerator is unusable, reinstall the CPU wheels so
# fallback behavior is deterministic rather than depending on the GPU wheel build.
if [[ "$INSTALL_DEVICE" != "cpu" && "$ACTIVE_DEVICE" == "cpu" ]]; then
  msg "Installing CPU PyTorch fallback"
  "$APP_DIR/.venv/bin/pip" install --upgrade --force-reinstall --index-url https://download.pytorch.org/whl/cpu torch torchaudio
  TORCH_INDEX_URL="https://download.pytorch.org/whl/cpu"
fi

ALLOWED_DEVICES="cpu"
[[ "$ACTIVE_DEVICE" != "cpu" ]] && ALLOWED_DEVICES="cpu,${ACTIVE_DEVICE}"

if [[ ! -f "$CONF_DIR/api-key" ]]; then
  python3 - <<'PY' >"$CONF_DIR/api-key"
import secrets
print(secrets.token_hex(32))
PY
  chmod 600 "$CONF_DIR/api-key"
fi
API_KEY=$(cat "$CONF_DIR/api-key")

cat >"$CONF_DIR/demucs-api.env" <<EOFENV
DEMUX_API_KEY=${API_KEY}
DEMUX_DATA_DIR=${DATA_DIR}
DEMUX_DEFAULT_MODEL=${DEFAULT_MODEL}
DEMUX_HQ_DEFAULT_MODEL=${HQ_MODEL}
DEMUX_DEVICE=${ACTIVE_DEVICE}
DEMUX_ALLOWED_DEVICES=${ALLOWED_DEVICES}
DEMUX_CPU_JOBS=${CPU_JOBS}
DEMUX_MAX_UPLOAD_MB=1024
DEMUX_JOB_TTL_HOURS=48
DEMUX_BIND=0.0.0.0
DEMUX_PORT=8000
DEMUX_TIMEZONE=${TIMEZONE}
DEMUX_HQ_START_HOUR=${HQ_START_HOUR}
DEMUX_HQ_END_HOUR=${HQ_END_HOUR}
DEMUX_PUBLIC_BASE_URL=
DEMUX_TRUST_PROXY_HEADERS=false
DEMUX_FORWARDED_ALLOW_IPS=127.0.0.1
DEMUX_ALLOW_PRIVATE_SOURCE_URLS=true
DEMUX_ALLOW_PRIVATE_CALLBACK_URLS=false
DEMUX_WEBHOOK_MAX_ATTEMPTS=5
DEMUX_WEBHOOK_TIMEOUT_SECONDS=15
DEMUX_R2_ENABLED=false
DEMUX_R2_ENDPOINT=
DEMUX_R2_ACCESS_KEY_ID=
DEMUX_R2_SECRET_ACCESS_KEY=
DEMUX_R2_BUCKET=
DEMUX_R2_PREFIX=demucs
EOFENV
chmod 600 "$CONF_DIR/demucs-api.env"

cat >"$CONF_DIR/repository.env" <<EOFREPO
DEMUX_API_REPO=${REPO}
DEMUX_API_BRANCH=${BRANCH}
DEMUX_TORCH_INDEX_URL=${TORCH_INDEX_URL}
EOFREPO

cat >/usr/local/bin/demucs-api-run <<'EOFRUN'
#!/usr/bin/env bash
set -Eeuo pipefail
source /etc/demucs-api/demucs-api.env
args=(main:app --host "$DEMUX_BIND" --port "$DEMUX_PORT")
if [[ "${DEMUX_TRUST_PROXY_HEADERS:-false}" == "true" ]]; then
  args+=(--proxy-headers --forwarded-allow-ips "${DEMUX_FORWARDED_ALLOW_IPS:-127.0.0.1}")
else
  args+=(--no-proxy-headers)
fi
exec /opt/demucs-api/.venv/bin/uvicorn "${args[@]}"
EOFRUN
chmod 0755 /usr/local/bin/demucs-api-run

cat >/etc/systemd/system/demucs-api.service <<'EOFSVC'
[Unit]
Description=Demucs REST API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=demucs-api
Group=demucs-api
WorkingDirectory=/opt/demucs-api
EnvironmentFile=/etc/demucs-api/demucs-api.env
Environment=TORCH_HOME=/var/cache/demucs-api/torch
ExecStart=/usr/local/bin/demucs-api-run
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/var/lib/demucs-api /var/cache/demucs-api

[Install]
WantedBy=multi-user.target
EOFSVC

cat >/usr/bin/update <<'EOFUPD'
#!/usr/bin/env bash
set -Eeuo pipefail
source /etc/demucs-api/repository.env
exec bash -c "$(curl -fsSL https://raw.githubusercontent.com/${DEMUX_API_REPO}/${DEMUX_API_BRANCH}/ct/demucs-api.sh)"
EOFUPD
chmod +x /usr/bin/update

cat >/usr/bin/demucs-api-info <<'EOFINFO'
#!/usr/bin/env bash
set -e
source /etc/demucs-api/demucs-api.env
printf 'Demucs API version: '; cat /opt/demucs-api/VERSION
printf 'Demucs version: '; /opt/demucs-api/.venv/bin/python -c 'import demucs; print(getattr(demucs, "__version__", "unknown"))'
printf 'PyTorch: '; /opt/demucs-api/.venv/bin/python -c 'import torch; print(torch.__version__)'
printf 'Python: '; /opt/demucs-api/.venv/bin/python --version
printf 'Compute backend: %s\n' "$DEMUX_DEVICE"
printf 'Allowed devices: %s\n' "$DEMUX_ALLOWED_DEVICES"
printf 'Default model: %s\n' "$DEMUX_DEFAULT_MODEL"
printf 'HQ model/window: %s / %s:00-%s:00 %s\n' "$DEMUX_HQ_DEFAULT_MODEL" "$DEMUX_HQ_START_HOUR" "$DEMUX_HQ_END_HOUR" "$DEMUX_TIMEZONE"
printf 'Endpoint: http://%s:%s\n' "$(hostname -I | awk '{print $1}')" "$DEMUX_PORT"
printf 'API key: '; cat /etc/demucs-api/api-key
EOFINFO
chmod +x /usr/bin/demucs-api-info

chown -R root:root "$APP_DIR"
systemctl daemon-reload
systemctl enable demucs-api

if [[ "$PRELOAD_MODEL" == "true" ]]; then
  msg "Pre-downloading Demucs model: $DEFAULT_MODEL"
  TORCH_HOME="$CACHE_DIR/torch" "$APP_DIR/.venv/bin/python" - "$DEFAULT_MODEL" <<'PY' || warn "Model pre-download failed; Demucs will retry on first use."
import sys
from demucs.pretrained import get_model
get_model(sys.argv[1])
print(f"Cached {sys.argv[1]}")
PY
  chown -R demucs-api:demucs-api "$CACHE_DIR"
fi

systemctl start demucs-api
sleep 2
systemctl is-active --quiet demucs-api

msg "Installation complete"
demucs-api-info
