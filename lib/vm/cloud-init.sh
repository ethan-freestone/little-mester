#!/usr/bin/env bash
# Ubuntu cloud-image provisioning. No credentials or host home directories enter
# this ISO. All outbound downloads use the host's explicit egress proxy.
set -euo pipefail

vm_cloud_init_generate() (
  set -e
  local vm_dir="$1" vm_name="$2" uid="$3" gid="$4" ssh_pubkey="$5"
  local seed_dir
  seed_dir=$(mktemp -d)
  trap 'rm -rf "$seed_dir"' EXIT
  mkdir -p "$vm_dir"

  cat > "$seed_dir/user-data" <<USERDATA
#cloud-config
hostname: ${vm_name}
ssh_pwauth: false
disable_root: true
groups:
  - mester: []
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
      apt-get install -y ca-certificates curl uidmap dbus-user-session slirp4netns fuse-overlayfs
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
      # Subordinate IDs must not overlap the real user's UID/GID.
      sed -i '/^mester:/d' /etc/subuid /etc/subgid
      echo 'mester:100000:65536' >> /etc/subuid
      echo 'mester:100000:65536' >> /etc/subgid
      loginctl enable-linger mester
      systemctl start user@${uid}.service
      sudo -iu mester env XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus dockerd-rootless-setuptool.sh install
      install -d -o mester -g mester /home/mester/.config/systemd/user/docker.service.d
      printf '[Service]\nEnvironment=HTTP_PROXY=http://10.0.2.100:3128\nEnvironment=HTTPS_PROXY=http://10.0.2.100:3128\nEnvironment=NO_PROXY=localhost,127.0.0.1,10.0.2.100\nEnvironment=DOCKERD_ROOTLESS_ROOTLESSKIT_FLAGS=--cidr=10.0.3.0/24\n' > /home/mester/.config/systemd/user/docker.service.d/proxy.conf
      chown -R mester:mester /home/mester/.config
      sudo -iu mester env XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus systemctl --user daemon-reload
      sudo -iu mester env XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus systemctl --user restart docker
      touch /var/lib/mester-provisioned
runcmd:
  - [bash, /usr/local/sbin/mester-provision]
USERDATA

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
