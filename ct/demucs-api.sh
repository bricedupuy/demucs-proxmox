#!/usr/bin/env bash
set -Eeuo pipefail

APP="Demucs API"
SLUG="demucs-api"
var_tags="${var_tags:-audio;ai;demucs}"
var_cpu="${var_cpu:-6}"
var_ram="${var_ram:-8192}"
var_disk="${var_disk:-24}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
var_unprivileged="${var_unprivileged:-1}"

# Change this after uploading the project to GitHub, e.g. myuser/demucs-proxmox
DEFAULT_REPO="bricedupuy/demucs-proxmox"
REPO="${DEMUX_API_REPO:-$DEFAULT_REPO}"
BRANCH="${DEMUX_API_BRANCH:-main}"
BASE_URL="https://raw.githubusercontent.com/${REPO}/${BRANCH}"

red='\033[0;31m'; green='\033[0;32m'; yellow='\033[1;33m'; blue='\033[0;34m'; reset='\033[0m'
info(){ echo -e "${blue}i${reset} $*"; }
ok(){ echo -e "${green}✓${reset} $*"; }
warn(){ echo -e "${yellow}!${reset} $*"; }
die(){ echo -e "${red}✗${reset} $*" >&2; exit 1; }

check_repo() {
  [[ "$REPO" != CHANGE_ME/* ]] || die "Set DEFAULT_REPO in ct/demucs-api.sh to your GitHub repo (owner/name) before using the installer."
}

update_inside_container() {
  [[ -f /etc/demucs-api/repository.env ]] && source /etc/demucs-api/repository.env
  REPO="${DEMUX_API_REPO:-$REPO}"
  BRANCH="${DEMUX_API_BRANCH:-$BRANCH}"
  BASE_URL="https://raw.githubusercontent.com/${REPO}/${BRANCH}"
  check_repo
  [[ -d /opt/demucs-api ]] || die "No Demucs API installation found."

  info "Stopping Demucs API"
  systemctl stop demucs-api

  info "Updating application files"
  curl -fsSL "$BASE_URL/app/main.py" -o /opt/demucs-api/main.py
  curl -fsSL "$BASE_URL/app/requirements.txt" -o /opt/demucs-api/requirements.txt
  curl -fsSL "$BASE_URL/app/VERSION" -o /opt/demucs-api/VERSION

  info "Updating Python dependencies"
  /opt/demucs-api/.venv/bin/python -m pip install -q --upgrade pip
  TORCH_URL="${DEMUX_TORCH_INDEX_URL:-https://download.pytorch.org/whl/cpu}"
  /opt/demucs-api/.venv/bin/pip install -q --upgrade --index-url "$TORCH_URL" torch torchaudio
  /opt/demucs-api/.venv/bin/pip install -q --upgrade -r /opt/demucs-api/requirements.txt

  systemctl daemon-reload
  systemctl restart demucs-api
  ok "Demucs API updated to $(cat /opt/demucs-api/VERSION)"
  echo
  systemctl --no-pager --full status demucs-api | sed -n '1,12p'
  exit 0
}

# The same ct script doubles as the in-container updater.
if ! command -v pveversion >/dev/null 2>&1; then
  update_inside_container
fi

[[ $EUID -eq 0 ]] || die "Run this script as root on the Proxmox host."
check_repo
command -v pct >/dev/null 2>&1 || die "This script must run on a Proxmox VE host."

header() {
  clear || true
  cat <<'ART'
  ____                             ___    ____  ____
 |  _ \  ___ _ __ ___  _   _  ___/ _ \  |  _ \|  _ \
 | | | |/ _ \ '_ ` _ \| | | |/ __| | | | | |_) | | | |
 | |_| |  __/ | | | | | |_| | (__| |_| | |  __/| |_| |
 |____/ \___|_| |_| |_|\__,_|\___|\___/  |_|   |____/
ART
  echo "            Proxmox LXC installer"
  echo
}

next_id(){ pvesh get /cluster/nextid 2>/dev/null || echo 100; }
first_storage(){ pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active" {print $1; exit}'; }
first_template_storage(){ pvesm status -content vztmpl 2>/dev/null | awk 'NR>1 && $3=="active" {print $1; exit}'; }
prompt_default() { local label="$1" default="$2" value; read -r -p "$label [$default]: " value; printf '%s' "${value:-$default}"; }

choose_model() {
  echo "Default Demucs model"
  echo "  1) htdemucs       (recommended / fast)"
  echo "  2) htdemucs_ft    (higher quality / slower)"
  echo "  3) htdemucs_6s    (6 stems)"
  echo "  4) hdemucs_mmi"
  echo "  5) mdx_extra"
  read -r -p "Select [1]: " model_choice
  case "${model_choice:-1}" in
    1) MODEL="htdemucs" ;;
    2) MODEL="htdemucs_ft" ;;
    3) MODEL="htdemucs_6s" ;;
    4) MODEL="hdemucs_mmi" ;;
    5) MODEL="mdx_extra" ;;
    *) MODEL="htdemucs" ;;
  esac
}

choose_compute() {
  echo "Compute backend"
  echo "  1) CPU (recommended / universal)"
  echo "  2) Auto-detect (CUDA if an NVIDIA device is present, otherwise CPU)"
  echo "  3) NVIDIA CUDA (experimental LXC passthrough)"
  echo "  4) Intel XPU (experimental; modern supported Intel GPUs only)"
  read -r -p "Select [1]: " compute_choice
  case "${compute_choice:-1}" in
    2)
      if [[ -e /dev/nvidia0 ]]; then COMPUTE="cuda"; else COMPUTE="cpu"; fi
      ;;
    3) COMPUTE="cuda" ;;
    4) COMPUTE="xpu" ;;
    *) COMPUTE="cpu" ;;
  esac

  case "$COMPUTE" in
    cuda) TORCH_INDEX="https://download.pytorch.org/whl/cu130" ;;
    xpu) TORCH_INDEX="https://download.pytorch.org/whl/xpu" ;;
    *) TORCH_INDEX="https://download.pytorch.org/whl/cpu" ;;
  esac
}

choose_settings() {
  header
  echo "1) Default settings"
  echo "2) Advanced settings"
  echo
  read -r -p "Select [1]: " mode
  mode="${mode:-1}"

  CTID="$(next_id)"
  HOSTNAME="demucs-api"
  CORES="$var_cpu"
  RAM="$var_ram"
  SWAP="2048"
  DISK="$var_disk"
  STORAGE="$(first_storage)"
  TEMPLATE_STORAGE="$(first_template_storage)"
  BRIDGE="vmbr0"
  VLAN=""
  IPV4="dhcp"
  GATEWAY=""
  IPV6="auto"
  ONBOOT="1"
  MODEL="htdemucs"
  HQ_MODEL="htdemucs_ft"
  PRELOAD="true"
  COMPUTE="cpu"
  TORCH_INDEX="https://download.pytorch.org/whl/cpu"
  TIMEZONE="Europe/Paris"
  HQ_START="2"
  HQ_END="7"

  [[ -n "$STORAGE" ]] || die "No Proxmox storage with rootdir content is available."
  [[ -n "$TEMPLATE_STORAGE" ]] || die "No Proxmox storage with vztmpl content is available."

  if [[ "$mode" == "2" ]]; then
    echo
    CTID="$(prompt_default 'Container ID' "$CTID")"
    HOSTNAME="$(prompt_default 'Hostname' "$HOSTNAME")"
    CORES="$(prompt_default 'vCPU cores' "$CORES")"
    RAM="$(prompt_default 'RAM (MB)' "$RAM")"
    SWAP="$(prompt_default 'Swap (MB)' "$SWAP")"
    DISK="$(prompt_default 'Root disk (GB)' "$DISK")"
    STORAGE="$(prompt_default 'Rootfs storage' "$STORAGE")"
    TEMPLATE_STORAGE="$(prompt_default 'Template storage' "$TEMPLATE_STORAGE")"
    BRIDGE="$(prompt_default 'Network bridge' "$BRIDGE")"
    VLAN="$(prompt_default 'VLAN tag (blank for none)' "$VLAN")"
    IPV4="$(prompt_default 'IPv4 (dhcp or CIDR, e.g. 192.168.1.50/24)' "$IPV4")"
    if [[ "$IPV4" != "dhcp" ]]; then GATEWAY="$(prompt_default 'IPv4 gateway' "192.168.1.1")"; fi
    IPV6="$(prompt_default 'IPv6 (auto, dhcp, or none)' "$IPV6")"
    echo
    choose_model
    read -r -p "Pre-download $MODEL during installation? [Y/n]: " preload_answer
    [[ "${preload_answer:-Y}" =~ ^[Nn]$ ]] && PRELOAD="false"
    echo
    choose_compute
    if [[ "$COMPUTE" != "cpu" ]]; then
      TORCH_INDEX="$(prompt_default 'PyTorch wheel index' "$TORCH_INDEX")"
    fi
    echo
    HQ_MODEL="$(prompt_default 'Nightly HQ model' "$HQ_MODEL")"
    HQ_START="$(prompt_default 'HQ processing window start hour' "$HQ_START")"
    HQ_END="$(prompt_default 'HQ processing window end hour' "$HQ_END")"
    TIMEZONE="$(prompt_default 'Scheduler timezone' "$TIMEZONE")"
  fi

  echo
  echo "Configuration"
  echo "  CT ID:          $CTID"
  echo "  Hostname:       $HOSTNAME"
  echo "  OS:             Debian 13"
  echo "  Unprivileged:   yes"
  echo "  CPU:            $CORES"
  echo "  RAM:            ${RAM} MB"
  echo "  Swap:           ${SWAP} MB"
  echo "  Disk:           ${DISK} GB ($STORAGE)"
  echo "  Bridge:         $BRIDGE"
  echo "  VLAN:           ${VLAN:-none}"
  echo "  IPv4:           $IPV4"
  echo "  Model:          $MODEL"
  echo "  Preload model:  $PRELOAD"
  echo "  Compute:        $COMPUTE"
  echo "  HQ model:       $HQ_MODEL"
  echo "  HQ window:      ${HQ_START}:00-${HQ_END}:00 ($TIMEZONE)"
  echo
  [[ "$COMPUTE" == "cpu" ]] || warn "GPU passthrough in an LXC depends on host drivers/device permissions. The installer will verify PyTorch access and fall back to CPU if the accelerator is not usable."
  read -r -p "Create the container? [Y/n]: " confirm
  [[ ! "${confirm:-Y}" =~ ^[Nn]$ ]] || exit 0
}

find_template() {
  info "Refreshing Debian template list"
  pveam update >/dev/null
  TEMPLATE=$(pveam available --section system | awk '/debian-13-standard_/ {print $2}' | sort -V | tail -1)
  [[ -n "$TEMPLATE" ]] || die "Could not find a Debian 13 standard template."
  if ! pveam list "$TEMPLATE_STORAGE" | awk '{print $1}' | grep -q "/$TEMPLATE$"; then
    info "Downloading $TEMPLATE to $TEMPLATE_STORAGE"
    pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
  fi
  TEMPLATE_PATH="${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}"
}

create_container() {
  pct status "$CTID" >/dev/null 2>&1 && die "CT $CTID already exists."

  local net="name=eth0,bridge=${BRIDGE},ip=${IPV4}"
  [[ "$IPV6" == "none" ]] && net+=",ip6=manual" || net+=",ip6=${IPV6}"
  [[ -n "$VLAN" ]] && net+=",tag=${VLAN}"
  [[ -n "$GATEWAY" ]] && net+=",gw=${GATEWAY}"

  info "Creating LXC $CTID"
  pct create "$CTID" "$TEMPLATE_PATH" \
    --hostname "$HOSTNAME" \
    --cores "$CORES" \
    --memory "$RAM" \
    --swap "$SWAP" \
    --rootfs "${STORAGE}:${DISK}" \
    --net0 "$net" \
    --unprivileged 1 \
    --onboot "$ONBOOT" \
    --ostype debian \
    --features keyctl=1 \
    --start 1
}

configure_gpu_passthrough() {
  [[ "$COMPUTE" != "cpu" ]] || return 0
  info "Configuring experimental $COMPUTE device passthrough"
  pct stop "$CTID"
  local conf="/etc/pve/lxc/${CTID}.conf"

  if [[ "$COMPUTE" == "xpu" ]]; then
    if [[ ! -d /dev/dri ]]; then
      warn "No /dev/dri exists on the Proxmox host; Intel XPU cannot be passed through."
      COMPUTE="cpu"
      TORCH_INDEX="https://download.pytorch.org/whl/cpu"
    else
      printf '%s\n' 'lxc.cgroup2.devices.allow: c 226:* rwm' >>"$conf"
      printf '%s\n' 'lxc.mount.entry: /dev/dri dev/dri none bind,optional,create=dir' >>"$conf"
    fi
  elif [[ "$COMPUTE" == "cuda" ]]; then
    if [[ ! -e /dev/nvidia0 ]]; then
      warn "No /dev/nvidia0 exists on the Proxmox host; CUDA cannot be passed through."
      COMPUTE="cpu"
      TORCH_INDEX="https://download.pytorch.org/whl/cpu"
    else
      for dev in /dev/nvidia*; do
        [[ -c "$dev" ]] || continue
        major=$(stat -c '%t' "$dev")
        major=$((16#$major))
        grep -qxF "lxc.cgroup2.devices.allow: c ${major}:* rwm" "$conf" || \
          printf 'lxc.cgroup2.devices.allow: c %s:* rwm\n' "$major" >>"$conf"
        printf 'lxc.mount.entry: %s %s none bind,optional,create=file\n' "$dev" "${dev#/}" >>"$conf"
      done
      warn "CUDA also requires compatible NVIDIA user-space driver libraries inside the LXC. If PyTorch cannot initialize CUDA, installation will fall back to CPU."
    fi
  fi
  pct start "$CTID"
}

wait_network() {
  info "Waiting for container networking"
  for _ in $(seq 1 30); do
    if pct exec "$CTID" -- bash -lc 'getent hosts deb.debian.org >/dev/null 2>&1'; then return 0; fi
    sleep 2
  done
  die "Container has no working network/DNS."
}

install_app() {
  local tmp="/tmp/demucs-api-install-${CTID}.sh"
  info "Fetching application installer"
  curl -fsSL "$BASE_URL/install/demucs-api-install.sh" -o "$tmp"
  pct push "$CTID" "$tmp" /root/demucs-api-install.sh -perms 0755
  rm -f "$tmp"

  info "Installing Demucs API inside CT $CTID"
  pct exec "$CTID" -- env \
    DEMUX_API_REPO="$REPO" \
    DEMUX_API_BRANCH="$BRANCH" \
    DEMUX_INSTALL_DEVICE="$COMPUTE" \
    DEMUX_TORCH_INDEX_URL="$TORCH_INDEX" \
    DEMUX_INSTALL_MODEL="$MODEL" \
    DEMUX_PRELOAD_MODEL="$PRELOAD" \
    DEMUX_INSTALL_CPU_JOBS="$CORES" \
    DEMUX_INSTALL_HQ_MODEL="$HQ_MODEL" \
    DEMUX_INSTALL_TIMEZONE="$TIMEZONE" \
    DEMUX_INSTALL_HQ_START_HOUR="$HQ_START" \
    DEMUX_INSTALL_HQ_END_HOUR="$HQ_END" \
    bash /root/demucs-api-install.sh
  pct exec "$CTID" -- rm -f /root/demucs-api-install.sh
}

show_result() {
  IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')
  echo
  ok "${APP} installation completed"
  echo "  CT ID:      $CTID"
  echo "  IP:         ${IP:-unknown}"
  echo "  API:        http://${IP:-CONTAINER_IP}:8000"
  echo "  API docs:   http://${IP:-CONTAINER_IP}:8000/docs"
  echo "  API key:    pct exec $CTID -- cat /etc/demucs-api/api-key"
  echo "  Info:       pct exec $CTID -- demucs-api-info"
  echo "  Update:     pct exec $CTID -- update"
  echo "  Logs:       pct exec $CTID -- journalctl -u demucs-api -f"
}

choose_settings
find_template
create_container
configure_gpu_passthrough
wait_network
install_app
show_result
