#!/usr/bin/env bash
# Minimal direct-guest bootstrap for the v3 smoke VM. No host credentials,
# workspace config, nested identity, or lifecycle controls are provisioned.

vm_host_cloud_init() {
  local name="${1:?VM name required}" uid="${2:?host uid required}"
  local ssh_key="${3:?SSH key required}"
  local guest_dirs_helper="${VM_HOST_SCRIPT_ROOT:?}/lib/vm/guest-user-dirs.py"
  local guest_dirs_payload
  [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,31}$ ]] || return 2
  [[ "$uid" =~ ^[1-9][0-9]{0,8}$ ]] || return 2
  [[ "$ssh_key" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || return 2
  [[ "$ssh_key" != *$'\n'* && "$ssh_key" != *$'\r'* ]] || return 2
  guest_dirs_payload="$(python3 -I - "$guest_dirs_helper" <<'PY'
import base64
import os
import stat
import sys

path = sys.argv[1]
info = os.lstat(path)
if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
    raise SystemExit("guest rootless directory helper is not a protected regular file")
with open(path, "rb") as stream:
    print(base64.b64encode(stream.read()).decode("ascii"))
PY
  )" || return 1
  [[ "$guest_dirs_payload" =~ ^[A-Za-z0-9+/]+=*$ ]] || return 1

  cat <<USERDATA
#cloud-config
hostname: ${name}
ssh_pwauth: false
disable_root: true
groups:
  - mester
users:
  - name: mester
    uid: ${uid}
    primary_group: mester
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
      - ${ssh_key}
write_files:
  - path: /etc/profile.d/mester-vm.sh
    owner: root:root
    permissions: '0644'
    content: |
      export HTTP_PROXY=http://10.0.2.100:3128
      export HTTPS_PROXY=http://10.0.2.100:3128
      export http_proxy=http://10.0.2.100:3128
      export https_proxy=http://10.0.2.100:3128
      export NO_PROXY=localhost,127.0.0.1,10.0.2.100
      export no_proxy=localhost,127.0.0.1,10.0.2.100
      export DOCKER_HOST=unix:///run/user/${uid}/docker.sock
      export XDG_RUNTIME_DIR=/run/user/${uid}
  - path: /etc/apt/apt.conf.d/80mester-proxy
    owner: root:root
    permissions: '0644'
    content: |
      Acquire::http::Proxy "http://10.0.2.100:3128";
      Acquire::https::Proxy "http://10.0.2.100:3128";
  - path: /usr/local/sbin/mester-v3-provision
    owner: root:root
    permissions: '0750'
    content: |
      #!/usr/bin/env bash
      set -euo pipefail
      export DEBIAN_FRONTEND=noninteractive
      export HTTP_PROXY=http://10.0.2.100:3128 HTTPS_PROXY=http://10.0.2.100:3128
      export http_proxy=http://10.0.2.100:3128 https_proxy=http://10.0.2.100:3128
      mkdir -p /workspace
      grep -q '^workspace /workspace virtiofs ' /etc/fstab || echo 'workspace /workspace virtiofs defaults 0 0' >> /etc/fstab
      mountpoint -q /workspace || mount -t virtiofs workspace /workspace
      apt-get update
      apt-get install -y ca-certificates curl uidmap dbus-user-session slirp4netns fuse-overlayfs kmod iptables nodejs npm
      modprobe nf_tables
      install -m 0755 -d /etc/apt/keyrings
      curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
      chmod 0644 /etc/apt/keyrings/docker.asc
      echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable' > /etc/apt/sources.list.d/docker.list
      printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
      chmod 0755 /usr/sbin/policy-rc.d
      apt-get update
      apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin docker-buildx-plugin docker-ce-rootless-extras
      systemctl disable --now docker.service docker.socket containerd.service || true
      rm -f /usr/sbin/policy-rc.d
      usermod --add-subuids 100000-165535 mester
      usermod --add-subgids 100000-165535 mester
      loginctl enable-linger mester
      systemctl start user@${uid}.service
      python3 -I /usr/local/sbin/mester-v3-user-dirs.py prepare ${uid}
      user_bus_ready=0
      for attempt in {1..30}; do
        if [[ -d /run/user/${uid} && ! -L /run/user/${uid} && -S /run/user/${uid}/bus && ! -L /run/user/${uid}/bus ]] \
          && [[ "\$(stat -c '%u:%a' /run/user/${uid} 2>/dev/null)" == '${uid}:700' ]] \
          && [[ "\$(stat -c '%u' /run/user/${uid}/bus 2>/dev/null)" == '${uid}' ]] \
          && timeout --signal=TERM --kill-after=1s 2s runuser -u mester -- env HOME=/home/mester XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus systemctl --user show-environment >/dev/null 2>&1; then
          user_bus_ready=1
          break
        fi
        sleep 1
      done
      if [[ "\$user_bus_ready" != 1 ]]; then
        echo 'guest user systemd bus did not become ready with the expected private runtime directory' >&2
        exit 1
      fi
      runuser -u mester -- env HOME=/home/mester python3 -I /usr/local/sbin/mester-v3-user-dirs.py verify-write
      runuser -u mester -- env HOME=/home/mester XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus systemctl --user daemon-reload
      runuser -u mester -- env HOME=/home/mester XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus dockerd-rootless-setuptool.sh install
      runuser -u mester -- env HOME=/home/mester XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus systemctl --user enable --now docker
      npm install --global @opencode/cli@2.2.5
      command -v opencode
      chown root:root /usr/local/bin/opencode
      chmod 0755 /usr/local/bin/opencode
      touch /var/lib/mester-v3-provisioned
  - path: /usr/local/sbin/mester-v3-user-dirs.py
    owner: root:root
    permissions: '0644'
    encoding: b64
    content: ${guest_dirs_payload}
runcmd:
  - [bash, /usr/local/sbin/mester-v3-provision]
USERDATA
}
