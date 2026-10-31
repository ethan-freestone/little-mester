#!/usr/bin/env bash
# Ubuntu cloud-image provisioning. No credentials or host home directories enter
# this ISO. All outbound downloads use the host's explicit egress proxy.
set -euo pipefail

vm_cloud_init_subid_script() {
  cat <<'SUBIDS'
#!/usr/bin/env bash
set -euo pipefail
user="${1:-mester}"
start=100000
count=65536
end=$((start + count - 1))
subuid_file="${MESTER_SUBUID_FILE:-/etc/subuid}"
subgid_file="${MESTER_SUBGID_FILE:-/etc/subgid}"

ensure_range() {
  local file="$1" option="$2" label="$3"
  if [[ ! -e "$file" ]]; then
    install -m 0644 /dev/null "$file"
  fi
  if awk -F: -v user="$user" -v start="$start" -v count="$count" \
      '$1 == user && $2 == start && $3 == count { found=1 } END { exit !found }' "$file"; then
    return 0
  fi
  if awk -F: -v user="$user" -v start="$start" -v end="$end" \
      '$1 != user && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ { last=$2+$3-1; if ($2 <= end && last >= start) { print; conflict=1 } } END { exit !conflict }' "$file"; then
    echo "ERROR: cannot allocate ${label} range ${start}-${end} to ${user}; it overlaps another account:" >&2
    awk -F: -v user="$user" -v start="$start" -v end="$end" \
      '$1 != user && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ { last=$2+$3-1; if ($2 <= end && last >= start) print }' "$file" >&2
    return 1
  fi
  usermod "$option" "${start}-${end}" "$user"
  if ! awk -F: -v user="$user" -v start="$start" -v count="$count" \
      '$1 == user && $2 == start && $3 == count { found=1 } END { exit !found }' "$file"; then
    echo "ERROR: ${label} range ${start}-${end} for ${user} was not recorded in ${file}" >&2
    return 1
  fi
}

ensure_range "$subuid_file" --add-subuids UID
ensure_range "$subgid_file" --add-subgids GID
SUBIDS
}

vm_cloud_init_user_data() {
  local vm_name="$1" uid="$2" gid="$3" ssh_pubkey="$4"
  local subid_script
  subid_script="$(vm_cloud_init_subid_script)"
  cat <<USERDATA
#cloud-config
hostname: ${vm_name}
ssh_pwauth: false
disable_root: true
groups:
  - mester
users:
  - name: mester
    uid: ${uid}
    primary_group: mester
    groups: [sudo]
    shell: /bin/bash
    sudo: ['ALL=(ALL) NOPASSWD:ALL']
    lock_passwd: true
    ssh_authorized_keys:
      - ${ssh_pubkey}
write_files:
  - path: /etc/apt/apt.conf.d/80mester-proxy
    content: |
      Acquire::http::Proxy "http://10.0.2.100:3128";
      Acquire::https::Proxy "http://10.0.2.100:3128";
  - path: /etc/profile.d/mester.sh
    content: |
      export HTTP_PROXY=http://10.0.2.100:3128
      export HTTPS_PROXY=http://10.0.2.100:3128
      export http_proxy=http://10.0.2.100:3128
      export https_proxy=http://10.0.2.100:3128
      export NO_PROXY=localhost,127.0.0.1,10.0.2.100
      export no_proxy=localhost,127.0.0.1,10.0.2.100
      export OLLAMA_HOST=http://10.0.2.100:11434
      export LLAMACPP_HOST=http://10.0.2.100:8080
      export DOCKER_HOST=unix:///run/user/${uid}/docker.sock
      export XDG_RUNTIME_DIR=/run/user/${uid}
  - path: /etc/modules-load.d/mester-rootless.conf
    content: |
      nf_tables
  - path: /usr/local/sbin/mester-load-netfilter
    permissions: '0755'
    content: |
      #!/bin/bash
      set -euo pipefail
      # Ubuntu noble uses iptables-nft. Rootless users cannot load this module.
      if ! modprobe nf_tables; then
        echo "ERROR: guest kernel \$(uname -r) cannot load nf_tables; rootless Docker requires it. Check matching guest kernel modules before retrying." >&2
        exit 1
      fi
  - path: /usr/local/sbin/mester-configure-subids
    permissions: '0755'
    content: |
$(printf '%s\n' "$subid_script" | sed 's/^/      /')
  - path: /usr/local/sbin/mester-provision
    permissions: '0755'
    content: |
      #!/bin/bash
      set -euxo pipefail
      . /etc/profile.d/mester.sh
      # Match workspace ownership, including hosts with a non-default GID.
      groupmod -g ${gid} mester
      mkdir -p /workspace
      echo 'workspace /workspace virtiofs defaults 0 0' >> /etc/fstab
      mountpoint -q /workspace || mount -t virtiofs workspace /workspace
      export DEBIAN_FRONTEND=noninteractive
      apt-get update
      apt-get install -y ca-certificates curl uidmap dbus-user-session slirp4netns fuse-overlayfs kmod iptables
      install -m 0755 -d /etc/apt/keyrings
      curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
      chmod a+r /etc/apt/keyrings/docker.asc
      echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable' > /etc/apt/sources.list.d/docker.list
      # Prevent package postinst from starting a rootful daemon.
      printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
      chmod +x /usr/sbin/policy-rc.d
      apt-get update
      apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin docker-buildx-plugin docker-ce-rootless-extras
      systemctl disable --now docker.service docker.socket containerd.service
      rm /usr/sbin/policy-rc.d
      # Configure and validate subordinate mappings before rootless Docker setup.
      /usr/local/sbin/mester-configure-subids mester
      /usr/local/sbin/mester-load-netfilter
      loginctl enable-linger mester
      systemctl start user@${uid}.service
      # Install the drop-in BEFORE the installer starts Docker for the first time.
      install -d -o mester -g mester /home/mester/.config/systemd/user/docker.service.d
      printf '[Service]\nEnvironment=HTTP_PROXY=http://10.0.2.100:3128\nEnvironment=HTTPS_PROXY=http://10.0.2.100:3128\nEnvironment=NO_PROXY=localhost,127.0.0.1,10.0.2.100\nEnvironment=DOCKERD_ROOTLESS_ROOTLESSKIT_FLAGS=--cidr=10.0.3.0/24\n' > /home/mester/.config/systemd/user/docker.service.d/proxy.conf
      install -d -o mester -g mester /home/mester/.config/docker
      cat > /home/mester/.config/docker/daemon.json <<'DOCKER_PROXY'
      {
        "proxies": {
          "http-proxy": "http://10.0.2.100:3128",
          "https-proxy": "http://10.0.2.100:3128",
          "no-proxy": "localhost,127.0.0.1,10.0.2.100"
        }
      }
      DOCKER_PROXY
      chown -R mester:mester /home/mester/.config
      sudo -iu mester env XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus systemctl --user daemon-reload
      sudo -iu mester env XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus dockerd-rootless-setuptool.sh install
      # Restart only starts the daemon in this boot. Ensure the lingered user
      # manager starts it on every subsequent VM boot too.
      sudo -iu mester env XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus systemctl --user daemon-reload
      sudo -iu mester env XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus systemctl --user enable docker
      sudo -iu mester env XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus systemctl --user restart docker
      touch /var/lib/mester-provisioned
runcmd:
  - [bash, /usr/local/sbin/mester-provision]
USERDATA
}

vm_cloud_init_generate() (
  set -e
  local vm_dir="$1" vm_name="$2" uid="$3" gid="$4" ssh_pubkey="$5"
  local seed_dir
  seed_dir=$(mktemp -d)
  trap 'rm -rf "$seed_dir"' EXIT
  mkdir -p "$vm_dir"

  vm_cloud_init_user_data "$vm_name" "$uid" "$gid" "$ssh_pubkey" > "$seed_dir/user-data"

  printf 'instance-id: %s\nlocal-hostname: %s\n' "$vm_name" "$vm_name" > "$seed_dir/meta-data"
  local iso_tool
  if command -v genisoimage >/dev/null 2>&1; then
    iso_tool=genisoimage
  elif command -v mkisofs >/dev/null 2>&1; then
    iso_tool=mkisofs
  else
    log "ERROR: genisoimage or mkisofs is required"
    return 1
  fi
  "$iso_tool" -quiet -rock -joliet -output "$vm_dir/seed.iso" -V cidata "$seed_dir/user-data" "$seed_dir/meta-data"
  log "Seed ISO created: $vm_dir/seed.iso"
)
