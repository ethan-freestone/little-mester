#!/usr/bin/env bash
# cloud-init.sh — Generate a cloud-init seed ISO for the VM.
#
# Produces a tiny ISO at <vm_dir>/seed.iso containing:
#   - user creation (mester) with the requested UID/GID
#   - Docker + compose plugin installation
#   - rootless Docker setup (subuid/subgid, lingering, socket)
#   - SSH key injection (per-VM host key + mester authorized_keys)
#   - workspace mount point
#   - proxy environment variables for model/guestfwd endpoints
#
# Usage: vm_cloud_init_generate <vm_dir> <vm_name> <uid> <gid> <ssh_pubkey>

set -euo pipefail

vm_cloud_init_generate() {
  local vm_dir="$1"
  local vm_name="$2"
  local uid="$3"
  local gid="$4"
  local ssh_pubkey="${5:-}"

  local seed_iso="$vm_dir/seed.iso"
  local seed_dir
  seed_dir="$(mktemp -d)"

  trap "rm -rf '$seed_dir'" EXIT

  # --- user-data ---
  cat > "$seed_dir/user-data" <<USERDATA
#cloud-config
hostname: ${vm_name}
fqdn: ${vm_name}

users:
  - name: mester
    uid: ${uid}
    gid: ${gid}
    groups: [users, wheel]
    shell: /bin/bash
    sudo: ['ALL=(ALL) NOPASSWD: ALL']
    lock_passwd: false
    ssh_authorized_keys:
      - ${ssh_pubkey}

ssh_genkeytypes: ['ed25519', 'rsa']

runcmd:
  # ── Ensure SSH daemon is enabled and running ────────────────────
  # Enable both sshd.service (direct) and sshd.socket (socket-activated)
  - systemctl enable sshd
  - systemctl enable sshd.socket || true
  - systemctl start sshd || true
  - systemctl start sshd.socket || true

  # ── Install Docker + compose plugin ──────────────────────────
  - |
    if command -v pacman >/dev/null 2>&1; then
      pacman -S --noconfirm docker docker-compose-v2
    elif command -v apt-get >/dev/null 2>&1; then
      apt-get update && apt-get install -y docker.io docker-compose-v2
    fi

  # ── Configure rootless Docker ────────────────────────────────
  - usermod --status locked root
  - loginctl enable-linger mester

  - |
    # subuid/subgid: allocate 65536 ids for mester
    if ! grep -q '^mester:' /etc/subuid 2>/dev/null; then
      echo "mester:${uid}:65536" >> /etc/subuid
      echo "mester:${uid}:65536" >> /etc/subgid
    fi

  - |
    # Create rootless Docker socket directory (must exist BEFORE
    # socket activation tries to bind here)
    mkdir -p /run/user/${uid}
    chown mester:mester /run/user/${uid}
    # Ensure permissions so root can create the socket too
    chmod 0755 /run/user/${uid}

  # ── Set proxy env vars for model/guestfwd endpoints ─────────
  - |
    cat > /etc/profile.d/99-mester-env.sh <<'ENVSCRIPT'
    # Model API endpoints (host via guestfwd)
    export OLLAMA_HOST=http://10.0.2.100:11434
    export LLAMACPP_HOST=http://10.0.2.100:8080

    # Egress proxy (host via guestfwd)
    export HTTPS_PROXY=http://10.0.2.100:3128
    export HTTP_PROXY=http://10.0.2.100:3128
    ENVSCRIPT

  # ── Ensure /workspace mount point exists ─────────────────────
  - mkdir -p /workspace

  # ── Create agent directory for rootless Docker data ──────────
  - |
    mkdir -p /home/mester/.local/share/docker
    chown mester:mester /home/mester/.local/share/docker

  # ── Start dockerd as rootless mester user ───────────────────
  - |
    # Start dockerd in the background for the mester user.
    # Use the rootless socket path that we configured above.
    su - mester -c "
      mkdir -p /run/user/${uid}
      dockerd --host unix:///run/user/${uid}/docker.sock --storage-driver vfs &
    " 2>/dev/null || true
    # Give dockerd a moment to start
    sleep 2

write_files:
  - path: /etc/systemd/system/multi-user.target.wants/docker.socket
    content: |
      [Unit]
      Description=Docker Socket for the rootless Docker daemon
      After=network.target local-fs.target

      [Socket]
      ListenStream=/run/user/%i/docker.sock
      SocketUser=%i
      SocketGroup=%i
      DirectoryMode=0755

      [Install]
      WantedBy=sockets.target
    owner: root:root
    permissions: '0644'

poweroff:
  date: '-1'

bootcmd:
  # Ensure /run/user/<uid> exists BEFORE socket activation tries to bind
  - [ sh, -c, 'mkdir -p /run/user/${uid} && chown mester:mester /run/user/${uid} && chmod 0755 /run/user/${uid}' ]
  # Ensure /workspace is mounted by virtiofs before cloud-init finishes
  - [ sh, -c, 'mkdir -p /workspace && mountpoint -q /workspace || true' ]
USERDATA

  # --- meta-data ---
  cat > "$seed_dir/meta-data" <<METADATA
instance-id: ${vm_name}
local-hostname: ${vm_name}
METADATA

  # --- Generate ISO ─────────────────────────────────────────────
  if command -v genisoimage >/dev/null 2>&1; then
    log_verbose "Generating cloud-init ISO with genisoimage..."
    genisoimage -rock -output "$seed_iso" -V cidata -joliet "$seed_dir/meta-data" "$seed_dir/user-data" 2>&1
  elif command -v mkisofs >/dev/null 2>&1; then
    log_verbose "Generating cloud-init ISO with mkisofs..."
    mkisofs -rock -output "$seed_iso" -V cidata -joliet "$seed_dir/meta-data" "$seed_dir/user-data" 2>&1
  else
    log "ERROR: Neither genisoimage nor mkisofs found. Install one to generate cloud-init ISO."
    return 1
  fi

  log "Seed ISO created: $seed_iso"
}
