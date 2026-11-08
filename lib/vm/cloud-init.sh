#!/usr/bin/env bash
# Minimal direct-guest bootstrap for the v3 smoke VM. No host credentials,
# workspace config, nested identity, or lifecycle controls are provisioned.

vm_host_cloud_init() {
  local name="${1:?VM name required}" uid="${2:?host uid required}"
  local gid="${3:?host gid required}" ssh_key="${4:?SSH key required}"
  [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,31}$ ]] || return 2
  [[ "$uid" =~ ^[1-9][0-9]{0,8}$ && "$gid" =~ ^[1-9][0-9]{0,8}$ ]] || return 2
  [[ "$ssh_key" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || return 2
  [[ "$ssh_key" != *$'\n'* && "$ssh_key" != *$'\r'* ]] || return 2

  cat <<USERDATA
#cloud-config
hostname: ${name}
ssh_pwauth: false
disable_root: true
groups:
  - name: mester
    gid: ${gid}
users:
  - name: mester
    uid: ${uid}
    primary_group: mester
    groups: []
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
      install -d -o mester -g mester -m 0700 /home/mester/.config/systemd/user/docker.service.d /home/mester/.config/docker
      cat > /home/mester/.config/systemd/user/docker.service.d/80-mester-proxy.conf <<'PROXY'
      [Service]
      Environment=HTTP_PROXY=http://10.0.2.100:3128
      Environment=HTTPS_PROXY=http://10.0.2.100:3128
      Environment=NO_PROXY=localhost,127.0.0.1,10.0.2.100
      Environment=DOCKERD_ROOTLESS_ROOTLESSKIT_FLAGS=--cidr=10.0.3.0/24
      PROXY
      chown mester:mester /home/mester/.config/systemd/user/docker.service.d/80-mester-proxy.conf
      chmod 0600 /home/mester/.config/systemd/user/docker.service.d/80-mester-proxy.conf
      cat > /home/mester/.config/docker/daemon.json <<'DOCKER_PROXY'
      {"proxies":{"http-proxy":"http://10.0.2.100:3128","https-proxy":"http://10.0.2.100:3128","no-proxy":"localhost,127.0.0.1,10.0.2.100"}}
      DOCKER_PROXY
      chown mester:mester /home/mester/.config/docker/daemon.json
      chmod 0600 /home/mester/.config/docker/daemon.json
      runuser -u mester -- env HOME=/home/mester XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus systemctl --user daemon-reload
      runuser -u mester -- env HOME=/home/mester XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus dockerd-rootless-setuptool.sh install
      runuser -u mester -- env HOME=/home/mester XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus systemctl --user enable --now docker
      npm install --global @opencode/cli@2.2.5
      command -v opencode
      install -d -o mester -g mester -m 0700 /home/mester/.local/share/opencode
      chown root:root /usr/local/bin/opencode
      chmod 0755 /usr/local/bin/opencode
      touch /var/lib/mester-v3-provisioned
runcmd:
  - [bash, /usr/local/sbin/mester-v3-provision]
USERDATA
}
