#!/usr/bin/env bash
set -euo pipefail

# Robust installer for HueMix-Link (systemd).
# Supports: install, uninstall, --yes, --dry-run, --force

APP_DIR="/opt/huemix-link"
SERVICE_USER="huemix-link"
SERVICE_NAME="huemix-link"
PYTHON_BIN="python3"
DRY_RUN=0
FORCE=0
DELETE=0
NO_RESTART=0
ASSUME_YES=0
LOCAL=1
EXT_PORT=80
OLD_HOSTNAME_FILE="$APP_DIR/.prev-hostname"
HTTPS=1

# Colors for nicer output
bold=$(echo -en "\e[1m")
white=$(echo -en "\e[97m")
blue=$(echo -en "\e[94m")
green=$(echo -en "\e[92m")
red=$(echo -en "\e[91m")
magenta=$(echo -en "\e[95m")
cyan=$(echo -en "\e[96m")
yellow=$(echo -en "\e[93m")
gray=$(echo -en "\e[90m")
reset=$(echo -en "\e[0m")

# Message helpers with visual markers and distinct colors
log()  { printf '%b\n' "${blue}● ${reset}${white}$*${reset}" >&2; }
die()  { 
  printf '%b\n' "${bold}${red}ERROR:${reset} ${red}$*${reset}" >&2
  # Remove VERSION file if it exists to prevent false "already installed" on next run
  [ -f "$APP_DIR/VERSION" ] && rm -f "$APP_DIR/VERSION" 2>/dev/null || true
  exit 1
}
success() { printf '%b\n' "${green}${reset}${white}$*${reset}" >&2; }
warn()    { printf '%b\n' "${yellow}${reset}${white}$*${reset}" >&2; }
debug()   { printf '%b\n' "${gray}$*${reset}" >&2; }

print_header() {
  local src_ver
  src_ver=$(get_repo_version 2>/dev/null || echo "")
  printf '%b\n' "${yellow}=====================================================${reset}"
  if [ -n "$src_ver" ]; then
    printf '%b\n' "${yellow}         HueMix-Link Installer${reset} ${yellow}v${src_ver}${reset}"
  else
    printf '%b\n' "${yellow}         HueMix-Link Installer${reset}"
  fi
  printf '%b\n' "${yellow}=====================================================${reset}"
}

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log "DRY-RUN: $*"; return 0
  fi
  "$@"
}

usage() {
  cat <<EOF
${blue}${bold}Usage:${reset} $0 [${magenta}install${reset}|${magenta}uninstall${reset}] [${gray}--yes${reset}] [${gray}--dry-run${reset}] [${gray}--force${reset}] [${gray}--delete${reset}] [${gray}--no-restart${reset}] [${gray}--show-version${reset}] [${gray}--help${reset}]

  ${blue}install${reset}         Install or update the application (${magenta}default${reset})
  ${blue}uninstall${reset}       Remove service and application files

  ${gray}--yes${reset}           Non-interactive; assume yes to prompts
  ${gray}--dry-run${reset}       Show actions without making changes
  ${gray}--force${reset}         Force recreate venv and overwrite files
  ${gray}--delete${reset}        When installing, delete config files in app dir
  ${gray}--no-restart${reset}    Do not stop or restart the service during update
  ${gray}--show-version${reset}  Show installed and source version information and exit
  ${gray}--no-local${reset}      Do not create huemixlink.local host entry
  ${gray}--no-https${reset}      Disable HTTPS (only serve HTTP on specified port)
  ${gray}--port <port>${reset}   External HTTP port (default: ${EXT_PORT})
  ${gray}-h|--help${reset}       Show this help message
EOF
}

parse_args() {
  MODE=install
  while [ "$#" -gt 0 ]; do
    case "$1" in
      install|uninstall) MODE="$1"; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      --force) FORCE=1; shift ;;
      --delete) DELETE=1; shift ;;
      --no-restart) NO_RESTART=1; shift ;;
      --no-https) HTTPS=0; shift ;;
      --port) shift; EXT_PORT="$1"; shift ;;
      --show-version) SHOW_VERSION=1; shift ;;
      --no-local) LOCAL=0; shift ;;
      --yes|-y) ASSUME_YES=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "Unknown argument: $1" ;;
    esac
  done
}

# VERSION (repo-controlled)
SHOW_VERSION=0
get_repo_version() {
  if [ -f VERSION ]; then
    head -n 1 VERSION 2>/dev/null || echo ""
  else
    echo ""
  fi
}

show_versions() {
  INSTALLED=""
  if [ -f "$APP_DIR/VERSION" ]; then
    INSTALLED=$(head -n 1 "$APP_DIR/VERSION" 2>/dev/null || echo "")
  fi
  SOURCE=$(get_repo_version)
  if [ -n "$INSTALLED" ]; then
    printf '%b\n' "${blue}${bold}Installed:${reset} ${magenta}v${INSTALLED}${reset}"
  fi
  if [ -n "$SOURCE" ]; then
    printf '%b\n' "${blue}${bold}Source:${reset} ${magenta}v${SOURCE}${reset}"
  fi
  if [ -z "$INSTALLED" ] && [ -z "$SOURCE" ]; then
    printf '%b\n' "${blue}${bold}Version:${reset} ${gray}none${reset}"
  fi
}


confirm() {
  [ "$ASSUME_YES" -eq 1 ] && return 0
  printf "%s [y/N]: " "$1" >&2
  read -r ans
  case "$ans" in
    [Yy]|[Yy][Ee][Ss]) return 0 ;;
    *) return 1 ;;
  esac
}

detect_python() {
  for p in python3.11 python3.10 python3.9 python3; do
    if command -v "$p" >/dev/null 2>&1; then
      PYTHON_BIN="$p"
      break
    fi
  done
  # Validate venv module
  if ! "$PYTHON_BIN" -c "import venv" >/dev/null 2>&1; then
    warn "$PYTHON_BIN venv module not found. Attempting to install..."
    
    # Determine the venv package name based on Python version
    PYTHON_VERSION=$("$PYTHON_BIN" -c "import sys; print(f'python{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "python3")
    VENV_PACKAGE="${PYTHON_VERSION}-venv"
    
    log "Installing ${VENV_PACKAGE}..."
    if [ "$DRY_RUN" -eq 1 ]; then
      log "DRY-RUN: would run apt install -y ${VENV_PACKAGE}"
    else
      if command -v apt >/dev/null 2>&1; then
        apt update -y -qq || warn "Failed to update package list"
        if apt install -y "$VENV_PACKAGE"; then
          success "Successfully installed ${VENV_PACKAGE}"
        else
          die "Failed to install ${VENV_PACKAGE}. Please install it manually: sudo apt install ${VENV_PACKAGE}"
        fi
      elif command -v yum >/dev/null 2>&1; then
        # For RHEL/CentOS/Fedora systems
        if yum install -y "$PYTHON_BIN-devel"; then
          success "Successfully installed ${PYTHON_BIN}-devel"
        else
          die "Failed to install ${PYTHON_BIN}-devel. Please install it manually: sudo yum install ${PYTHON_BIN}-devel"
        fi
      else
        die "Package manager not found. Please manually install venv support: apt install ${VENV_PACKAGE}"
      fi
    fi
    
    # Re-validate venv module after installation
    if ! "$PYTHON_BIN" -c "import venv" >/dev/null 2>&1; then
      die "$PYTHON_BIN still does not support venv module after installation attempt. Please check your Python installation."
    fi
  fi
}

ensure_root() {
  if [ "$(id -u)" -ne 0 ]; then
    die "This installer must be run using sudo (e.g., 'sudo bash install.sh')."
  fi

  if [ -z "${SUDO_USER-}" ]; then
    die "Do not run this script as the root user directly. Run it via sudo from your user account, e.g. 'sudo bash install.sh'."
  fi
}

create_service_user() {
  if id -u "$SERVICE_USER" >/dev/null 2>&1; then
    log "Service user $SERVICE_USER already exists"
    return 0
  fi
  if command -v useradd >/dev/null 2>&1; then
    run useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER" || true
  elif command -v adduser >/dev/null 2>&1; then
    # Debian-style adduser batch mode
    run adduser --system --no-create-home --home "$APP_DIR" --shell /usr/sbin/nologin --group "$SERVICE_USER" || true
  else
    die "No useradd/adduser command found to create service user"
  fi
}

setup_serial_access() {
  log "Configuring serial access for ${SERVICE_USER}"

  local groups_added=0
  local serial_groups=(dialout uucp tty lock plugdev)

  for grp in "${serial_groups[@]}"; do
    if ! getent group "$grp" >/dev/null 2>&1; then
      continue
    fi

    if id -nG "$SERVICE_USER" | tr ' ' '\n' | grep -qx "$grp"; then
      continue
    fi

    if run usermod -a -G "$grp" "$SERVICE_USER"; then
      groups_added=1
      debug "Added ${SERVICE_USER} to group: ${grp}"
    else
      warn "Failed to add ${SERVICE_USER} to group ${grp}"
    fi
  done

  if [ "$groups_added" -eq 1 ]; then
    success "Serial groups updated for ${SERVICE_USER}"
  else
    log "Serial groups already configured for ${SERVICE_USER}"
  fi

  # Ensure common USB serial devices are group-accessible.
  if getent group dialout >/dev/null 2>&1; then
    local rule_file="/etc/udev/rules.d/99-huemixlink-serial.rules"
    if [ "$DRY_RUN" -eq 1 ]; then
      log "DRY-RUN: would write ${rule_file} and reload udev rules"
    else
      cat > "$rule_file" <<'EOF'
SUBSYSTEM=="tty", KERNEL=="ttyUSB*", MODE="0660", GROUP="dialout"
SUBSYSTEM=="tty", KERNEL=="ttyACM*", MODE="0660", GROUP="dialout"
EOF
      run udevadm control --reload-rules || true
      run udevadm trigger --subsystem-match=tty || true
    fi
  fi
}

copy_files() {
  log "Syncing files to ${APP_DIR}"
  run mkdir -p "$APP_DIR"

  if command -v rsync >/dev/null 2>&1; then
    RSYNC_OPTS=( -a )
    if [ "$DELETE" -eq 1 ]; then
      RSYNC_OPTS+=(--delete)
    fi
    RSYNC_EXCLUDES=()
    if [ "$DELETE" -eq 0 ]; then
      RSYNC_EXCLUDES+=( --exclude='data/' )
    fi
    if [ -d "./python" ]; then
      run rsync "${RSYNC_OPTS[@]}" "${RSYNC_EXCLUDES[@]}" ./python/ "$APP_DIR/"
    else
      warn "Warning: ./python directory not found; nothing to copy"
    fi
    [ -f ./LICENSE ] && run rsync "${RSYNC_OPTS[@]}" ./LICENSE "$APP_DIR/"
    [ -f ./README.md ] && run rsync "${RSYNC_OPTS[@]}" ./README.md "$APP_DIR/"
    [ -f ./VERSION ] && run rsync "${RSYNC_OPTS[@]}" ./VERSION "$APP_DIR/"
  else
    # Fallback without rsync: use cp for simplicity
    if [ -d "./python" ]; then
      run mkdir -p "$APP_DIR"
      if [ "$DELETE" -eq 0 ]; then
        run bash -c "cd ./python && tar -cf - --exclude='data' . | (cd \"$APP_DIR\" && tar -xpf -)"
      else
        run cp -a ./python/. "$APP_DIR/" || true
      fi
    else
      warn "Warning: ./python directory not found; nothing to copy"
    fi
    [ -f ./LICENSE ] && run cp -a ./LICENSE "$APP_DIR/" || true
    [ -f ./README.md ] && run cp -a ./README.md "$APP_DIR/" || true
    [ -f ./VERSION ] && run cp -a ./VERSION "$APP_DIR/" || true
    if [ "$DELETE" -eq 1 ]; then
      log "--delete requested but rsync not available; cannot remove extraneous files"
    fi
  fi
}

create_venv_and_deps() {
  VENV="$APP_DIR/venv"
  if [ "$FORCE" -eq 1 ] && [ -d "$VENV" ]; then
    log "Removing existing venv (force)"
    run rm -rf "$VENV"
  fi
  if [ ! -d "$VENV" ]; then
    log "Creating virtualenv with ${PYTHON_BIN}"
    run "$PYTHON_BIN" -m venv "$VENV"
  else
    log "Using existing virtualenv"
  fi
  run "$VENV/bin/python" -m pip install --upgrade pip setuptools wheel --quiet
  if [ -f "$APP_DIR/requirements.txt" ]; then
    run "$VENV/bin/python" -m pip install -r "$APP_DIR/requirements.txt" --quiet
  else
    log "No requirements.txt found; skipping pip install"
  fi
}

setup_permissions() {
  log "Setting ownership and permissions"
  run mkdir -p "$APP_DIR/data"
  run chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
  run find "$APP_DIR/data" -type d -exec chmod 750 {} + 2>/dev/null || true
  run find "$APP_DIR/data" -type f -exec chmod 640 {} + 2>/dev/null || true
}

setup_sudoers_restart() {
  local SUDOERS_FILE="/etc/sudoers.d/${SERVICE_NAME}-restart"
  log "Configuring passwordless sudo for service restart"
  if [ "$DRY_RUN" -eq 1 ]; then
    log "DRY-RUN: would write ${SUDOERS_FILE}"
    return 0
  fi
  cat > "$SUDOERS_FILE" <<EOF
# Allow ${SERVICE_USER} to restart the ${SERVICE_NAME} service without a password
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart ${SERVICE_NAME}
EOF
  chmod 440 "$SUDOERS_FILE"
  success "Passwordless sudo configured for service restart"
}

install_systemd_unit() {
  if ! command -v systemctl >/dev/null 2>&1; then
    log "systemctl not found; skipping systemd unit installation"
    return 0
  fi
  UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
  log "Installing systemd unit to $UNIT_PATH"
  if [ "$DRY_RUN" -eq 1 ]; then
    log "DRY-RUN: would write systemd unit to $UNIT_PATH"
    return 0
  fi
  cat > "$UNIT_PATH" <<EOF
[Unit]
Description=HueMix-Link service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/main.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
}

setup_https_cert() {
  [ "$HTTPS" -eq 0 ] && return
  [ "$DRY_RUN" -eq 1 ] && { log "DRY-RUN: would setup HTTPS certificate"; return; }

  CERT_DIR="/etc/haproxy/certs"
  
  log "Setting up self-signed HTTPS certificate..."
  
  # Detect server IP address
  SERVER_IP=$(hostname -I | awk '{print $1}')
  if [ -z "$SERVER_IP" ]; then
    SERVER_IP="192.168.1.100"  # fallback
    warn "Could not detect IP address, using ${SERVER_IP}"
  fi
  
  run mkdir -p "$CERT_DIR"
  
  if [ ! -f "$CERT_DIR/huemix.pem" ]; then
    log "Generating self-signed certificate for ${SERVER_IP} and huemixlink.local..."
    
    # Generate self-signed certificate valid for 10 years with multiple SANs
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
      -keyout "$CERT_DIR/huemix.key" \
      -out "$CERT_DIR/huemix.crt" \
      -subj "/C=US/ST=State/L=City/O=HueMix-Link/CN=${SERVER_IP}" \
      -addext "subjectAltName=IP:${SERVER_IP},DNS:huemixlink.local,DNS:localhost,IP:127.0.0.1" 2>/dev/null
    
    # Combine certificate and key for HAProxy
    cat "$CERT_DIR/huemix.crt" "$CERT_DIR/huemix.key" > "$CERT_DIR/huemix.pem"
    chmod 600 "$CERT_DIR/huemix.pem"
    
    success "Self-signed certificate generated for ${SERVER_IP} and huemixlink.local"
  else
    log "Certificate already exists"
  fi
}

setup_proxy() {
  TARGET_PORT=5001
  HAPROXY_CFG="/etc/haproxy/haproxy.cfg"
  TAG_BEGIN="# BEGIN HUEMIX-LINK"
  TAG_END="# END HUEMIX-LINK"

  [ "$DRY_RUN" -eq 1 ] && { log "DRY-RUN: would configure HAProxy"; return; }

  log "Configuring HAProxy: ${EXT_PORT} -> 127.0.0.1:${TARGET_PORT}"

  if ! command -v haproxy >/dev/null 2>&1; then
    log "Installing HAProxy..."
    run apt update -y
    run apt install -y haproxy
  fi

  # Remove old block if present (reinstall-safe)
  run sed -i "/$TAG_BEGIN/,/$TAG_END/d" "$HAPROXY_CFG"

  if [ "$HTTPS" -eq 1 ]; then
    CERT_FILE="/etc/haproxy/certs/huemix.pem"
    
    log "Configuring HAProxy with HTTP and HTTPS..."
    
    cat >> "$HAPROXY_CFG" <<EOF

$TAG_BEGIN
frontend huemixlink_front
    bind *:${EXT_PORT}
    bind *:443 ssl crt ${CERT_FILE}
    mode http
    default_backend huemixlink_back

backend huemixlink_back
    mode http
    server huemixlink 127.0.0.1:${TARGET_PORT} check
$TAG_END
EOF
    success "HAProxy configured with HTTP and HTTPS"
  else
    log "Configuring HAProxy with HTTP..."
    
    cat >> "$HAPROXY_CFG" <<EOF

$TAG_BEGIN
frontend huemixlink_front
    bind *:${EXT_PORT}
    mode http
    default_backend huemixlink_back

backend huemixlink_back
    mode http
    server huemixlink 127.0.0.1:${TARGET_PORT} check
$TAG_END
EOF
    success "HAProxy configured: http://<server_ip>:${EXT_PORT} -> 127.0.0.1:${TARGET_PORT}"
  fi

  run systemctl enable haproxy >/dev/null 2>&1
  run systemctl --quiet restart haproxy 2>&1 | grep -v "backend huemixlink_back has no server available" || true
}

service_update_finish() {
  # Called after unit file is written. Decide whether to restart existing service or enable/start new unit.
  if ! command -v systemctl >/dev/null 2>&1; then
    log "systemctl not available; skipping service control"
    return 0
  fi
  run systemctl daemon-reload
  if [ "$SERVICE_EXISTS" -eq 1 ]; then
    if [ "$WAS_ACTIVE" -eq 1 ]; then
      if [ "$NO_RESTART" -eq 0 ]; then
        log "Restarting ${SERVICE_NAME}.service"
        run systemctl restart "${SERVICE_NAME}.service" || true
      else
        log "--no-restart specified; leaving existing service running"
      fi
    else
      log "Service existed but was not active; not starting it"
    fi
  else
    run systemctl enable "${SERVICE_NAME}.service" || true
    if [ "$NO_RESTART" -eq 0 ]; then
      run systemctl start "${SERVICE_NAME}.service" || true
    else
      log "Service enabled but not started due to --no-restart"
    fi
  fi
}

setup_local_domain() {
  [ "$DRY_RUN" -eq 1 ] && { log "DRY-RUN: would setup Avahi"; return; }

  [ ! -d "$APP_DIR" ] && mkdir -p "$APP_DIR"
  [ ! -f "$OLD_HOSTNAME_FILE" ] && hostnamectl --static > "$OLD_HOSTNAME_FILE"

  log "Setting hostname to huemixlink..."
  hostnamectl set-hostname huemixlink
  sed -i "s/127.0.1.1.*/127.0.1.1 huemixlink/" /etc/hosts

  # Install Avahi
  if ! command -v avahi-daemon >/dev/null 2>&1; then
    log "Installing Avahi daemon..."
    run apt update -y >/dev/null 2>&1
    run apt install -y avahi-daemon avahi-utils >/dev/null 2>&1
  fi

  run systemctl enable avahi-daemon.service avahi-daemon.socket
  run systemctl restart avahi-daemon.service avahi-daemon.socket

  success "huemixlink.local advertised via mDNS"
}

cleanup_local_domain() {
  CURRENT_HOST=$(hostnamectl --static)
  [ "$CURRENT_HOST" != "huemixlink" ] && return
  confirm "Remove huemixlink.local (Avahi) and restore hostname?" || { log "Skipping"; return; }

  # Restore old hostname
  if [ -f "$OLD_HOSTNAME_FILE" ]; then
    OLD_HN=$(cat "$OLD_HOSTNAME_FILE")
    run hostnamectl set-hostname "$OLD_HN"
    
    sed -i "s/127.0.1.1.*/127.0.1.1 $OLD_HN/" /etc/hosts
    
    rm -f "$OLD_HOSTNAME_FILE"
    success "Hostname restored to $OLD_HN"
  fi

  # Stop Avahi
  if systemctl list-unit-files avahi-daemon.service &>/dev/null; then
    run systemctl stop avahi-daemon.service avahi-daemon.socket || true
    run systemctl disable avahi-daemon.service avahi-daemon.socket || true
  fi
}

uninstall() {
  [ "$DRY_RUN" -eq 1 ] && { log "DRY-RUN: would uninstall ${SERVICE_NAME}"; return; }
  
  if [ ! -d "$APP_DIR" ] && ! -f "/etc/systemd/system/${SERVICE_NAME}.service" && ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    log "${SERVICE_NAME} does not appear to be installed. Nothing to uninstall."
    return 0
  fi

  confirm "Are you sure you want to completely uninstall ${SERVICE_NAME} and remove all files, users, and system modifications?" || { log "Uninstall aborted"; return; }

  log "Uninstalling ${SERVICE_NAME}"
  run systemctl stop "${SERVICE_NAME}.service" || true
  run systemctl disable "${SERVICE_NAME}.service" || true
  run rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
  run rm -f "/etc/sudoers.d/${SERVICE_NAME}-restart"
  run systemctl daemon-reload || true

  cleanup_local_domain

  [ -d "$APP_DIR" ] && confirm "Remove $APP_DIR?" && run rm -rf "$APP_DIR"

  id -u "$SERVICE_USER" >/dev/null 2>&1 && confirm "Remove service user $SERVICE_USER?" && run userdel "$SERVICE_USER" || true

  if command -v haproxy >/dev/null 2>&1; then
    HAPROXY_CFG="/etc/haproxy/haproxy.cfg"
    TAG_BEGIN="# BEGIN HUEMIX-LINK"
    TAG_END="# END HUEMIX-LINK"

    log "Removing HAProxy HueMix-Link routing"
    run sed -i "/$TAG_BEGIN/,/$TAG_END/d" "$HAPROXY_CFG"
    run systemctl restart haproxy || true
  fi

  log "Uninstall complete"
}

main_install() {
  ensure_root
  print_header
  detect_python

  SOURCE=$(get_repo_version)
  INSTALLED=""
  [ -f "$APP_DIR/VERSION" ] && INSTALLED=$(head -n 1 "$APP_DIR/VERSION" 2>/dev/null || echo "")

  if [ -n "$SOURCE" ]; then
    if [ -n "$INSTALLED" ] && [ "$INSTALLED" != "$SOURCE" ]; then
      log "Updating v${INSTALLED} -> v${SOURCE}"
    elif [ -z "$INSTALLED" ]; then
      log "Installing v${SOURCE}"
    else
      [ "$FORCE" -eq 1 ] && warn "Reinstalling v${SOURCE}" || { log "Already installed. Use --force"; return; }
    fi
  fi
  WAS_ACTIVE=0
  SERVICE_EXISTS=0
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl list-unit-files "${SERVICE_NAME}.service" &>/dev/null; then
      SERVICE_EXISTS=1
    fi
    if [ "$SERVICE_EXISTS" -eq 1 ] && systemctl is-active --quiet "${SERVICE_NAME}.service"; then
      WAS_ACTIVE=1
      if [ "$NO_RESTART" -eq 0 ]; then
        log "Stopping existing service ${SERVICE_NAME}.service for update"
        run systemctl stop "${SERVICE_NAME}.service" || true
      else
        log "--no-restart specified; leaving service running"
      fi
    fi
  fi
  
  [ -f "$APP_DIR/VERSION" ] && rm -f "$APP_DIR/VERSION"

  create_service_user
  setup_serial_access
  copy_files
  create_venv_and_deps
  setup_permissions
  setup_sudoers_restart
  install_systemd_unit
  setup_https_cert
  setup_proxy

  if [ "$LOCAL" -eq 1 ]; then
    setup_local_domain
  fi
  service_update_finish
  
  success "Installation complete!"
  log "Check status with: systemctl status ${SERVICE_NAME}"
  echo ""
  
  if [ "$HTTPS" -eq 1 ]; then
    # Get server IP
    SERVER_IP=$(hostname -I | awk '{print $1}')
    
    log "Access your site via HTTP or HTTPS:"
    echo ""
    log "  HTTP:"
    if [ "$LOCAL" -eq 1 ]; then
      log "    http://huemixlink.local:${EXT_PORT}"
    fi
    log "    http://${SERVER_IP}:${EXT_PORT}"
    echo ""
    log "  HTTPS (for Web Serial API):"
    if [ "$LOCAL" -eq 1 ]; then
      log "    https://huemixlink.local"
    fi
    log "    https://${SERVER_IP}"
    echo ""
  elif [ "$LOCAL" -eq 1 ]; then
    log "Access at: http://huemixlink.local:${EXT_PORT}"
    echo ""
  else
    SERVER_IP=$(hostname -I | awk '{print $1}')
    log "Access at: http://${SERVER_IP}:${EXT_PORT}"
    echo ""
  fi
}

# Entry
parse_args "$@"

if [ "${MODE:-install}" = uninstall ]; then
  ensure_root
  uninstall
  exit 0
fi

if [ "$SHOW_VERSION" -eq 1 ]; then
  show_versions
  exit 0
fi

main_install

