#!/usr/bin/env python3
"""Shared guest bootstrap foundation for smoke and disk-backed system VMs."""
from __future__ import annotations

import argparse
import base64
import os
import pathlib
import re
import stat
import sys


# The provisioning sequence is shared; workspace setup is the only launch-mode
# difference. Keep smoke's virtiofs setup and package/rootless-Docker sequence.
_PROVISION = r'''#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export HTTP_PROXY=http://10.0.2.100:3128 HTTPS_PROXY=http://10.0.2.100:3128
export http_proxy=http://10.0.2.100:3128 https_proxy=http://10.0.2.100:3128
@WORKSPACE@
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
systemctl start user@@UID@.service
python3 -I @HELPER@ prepare @UID@
user_bus_ready=0
for attempt in {1..30}; do
  if [[ -d /run/user/@UID@ && ! -L /run/user/@UID@ && -S /run/user/@UID@/bus && ! -L /run/user/@UID@/bus ]]           && [[ "$(stat -c '%u:%a' /run/user/@UID@ 2>/dev/null)" == '@UID@:700' ]]           && [[ "$(stat -c '%u' /run/user/@UID@/bus 2>/dev/null)" == '@UID@' ]]           && timeout --signal=TERM --kill-after=1s 2s runuser -u mester -- env HOME=/home/mester XDG_RUNTIME_DIR=/run/user/@UID@ DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/@UID@/bus systemctl --user show-environment >/dev/null 2>&1; then
    user_bus_ready=1
    break
  fi
  sleep 1
done
if [[ "$user_bus_ready" != 1 ]]; then
  echo 'guest user systemd bus did not become ready with the expected private runtime directory' >&2
  exit 1
fi
runuser -u mester -- env HOME=/home/mester python3 -I @HELPER@ verify-write
runuser -u mester -- env HOME=/home/mester XDG_RUNTIME_DIR=/run/user/@UID@ DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/@UID@/bus systemctl --user daemon-reload
runuser -u mester -- env HOME=/home/mester XDG_RUNTIME_DIR=/run/user/@UID@ DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/@UID@/bus dockerd-rootless-setuptool.sh install
runuser -u mester -- env HOME=/home/mester XDG_RUNTIME_DIR=/run/user/@UID@ DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/@UID@/bus systemctl --user enable --now docker
npm install --global @opencode/cli@2.2.5
command -v opencode
chown root:root /usr/local/bin/opencode
chmod 0755 /usr/local/bin/opencode
touch /var/lib/mester-v3-provisioned
'''


def _safe(name: str, uid: int, ssh_key: str, workspace: str, helper: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,31}", name):
        raise ValueError("guest name is outside its fixed safe grammar")
    if type(uid) is not int or not 1 <= uid <= 999_999_999:
        raise ValueError("guest UID is outside its fixed safe range")
    if (not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?:[ \t][^\r\n]*)?", ssh_key)
            or "\n" in ssh_key or "\r" in ssh_key):
        raise ValueError("guest SSH key is malformed")
    if workspace not in ("virtiofs", "disk"):
        raise ValueError("guest workspace kind is unsupported")
    if helper not in ("/usr/local/sbin/mester-v3-user-dirs.py",
                      "/usr/local/libexec/lm-guest-user-dirs.py"):
        raise ValueError("guest user-directory helper path is not fixed")


def helper_payload() -> str:
    path = pathlib.Path(__file__).with_name("guest-user-dirs.py")
    info = os.lstat(path)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise ValueError("guest rootless directory helper is not a protected regular file")
    return base64.b64encode(path.read_bytes()).decode("ascii")


def provision_script(uid: int, workspace: str, helper: str) -> str:
    if type(uid) is not int or not 1 <= uid <= 999_999_999:
        raise ValueError("guest UID is outside its fixed safe range")
    if helper not in ("/usr/local/sbin/mester-v3-user-dirs.py",
                      "/usr/local/libexec/lm-guest-user-dirs.py"):
        raise ValueError("guest user-directory helper path is not fixed")
    if workspace == "virtiofs":
        setup = ("mkdir -p /workspace\n"
                 "grep -q '^workspace /workspace virtiofs ' /etc/fstab || "
                 "echo 'workspace /workspace virtiofs defaults 0 0' >> /etc/fstab\n"
                 "mountpoint -q /workspace || mount -t virtiofs workspace /workspace")
    elif workspace == "disk":
        setup = "install -d -o mester -g mester -m 0750 /home/mester/workspace"
    else:
        raise ValueError("guest workspace kind is unsupported")
    return _PROVISION.replace("@WORKSPACE@", setup).replace("@UID@", str(uid)).replace("@HELPER@", helper)


def build_foundation(*, name: str, uid: int, ssh_key: str, workspace: str, helper: str) -> dict:
    """Build common proxy, rootless Docker, user-directory and OpenCode setup."""
    _safe(name, uid, ssh_key, workspace, helper)
    profile = (
        "export HTTP_PROXY=http://10.0.2.100:3128\n"
        "export HTTPS_PROXY=http://10.0.2.100:3128\n"
        "export http_proxy=http://10.0.2.100:3128\n"
        "export https_proxy=http://10.0.2.100:3128\n"
        "export NO_PROXY=localhost,127.0.0.1,10.0.2.100\n"
        "export no_proxy=localhost,127.0.0.1,10.0.2.100\n"
        f"export DOCKER_HOST=unix:///run/user/{uid}/docker.sock\n"
        f"export XDG_RUNTIME_DIR=/run/user/{uid}\n")
    return {
        "hostname": name, "ssh_pwauth": False, "disable_root": True,
        "groups": ["mester"],
        "users": [{"name": "mester", "uid": uid, "primary_group": "mester",
                   "shell": "/bin/bash", "lock_passwd": True, "ssh_authorized_keys": [ssh_key]}],
        "write_files": [
            {"path": "/etc/profile.d/mester-vm.sh", "owner": "root:root", "permissions": "0644", "content": profile},
            {"path": "/etc/apt/apt.conf.d/80mester-proxy", "owner": "root:root", "permissions": "0644",
             "content": 'Acquire::http::Proxy "http://10.0.2.100:3128";\nAcquire::https::Proxy "http://10.0.2.100:3128";\n'},
            {"path": "/usr/local/sbin/mester-v3-provision", "owner": "root:root", "permissions": "0750",
             "content": provision_script(uid, workspace, helper)},
            {"path": helper, "owner": "root:root", "permissions": "0644", "encoding": "b64", "content": helper_payload()},
        ],
        "runcmd": [["bash", "/usr/local/sbin/mester-v3-provision"]],
    }


def build_legacy_parent_base(*, name: str, uid: int, ssh_key: str) -> dict:
    """Preserve the established parent base while sharing cloud-init assembly."""
    _safe(name, uid, ssh_key, "disk", "/usr/local/libexec/lm-guest-user-dirs.py")
    return {
        "hostname": name, "ssh_pwauth": False, "disable_root": True,
        "ssh_deletekeys": False,
        "ssh_keys": {},
        "users": [{"name": "mester", "uid": uid, "primary_group": "mester",
                   "groups": ["users"], "shell": "/bin/bash", "lock_passwd": True,
                   "ssh_authorized_keys": [ssh_key]}],
        "groups": ["mester"], "write_files": [],
        "packages": ["tmux", "nodejs", "npm"],
        "runcmd": [["bash", "-lc", "install -d -o mester -g mester -m 0750 /home/mester/workspace && "
                    "/usr/bin/python3 -I /usr/local/libexec/lm-guest-user-dirs.py prepare-opencode 1000 && "
                    "npm install --global @opencode/cli@2.2.5 && "
                    "test \"$(opencode --version)\" = 'opencode v2.2.5' && "
                    "chown root:root /usr/local/bin/opencode && chmod 0755 /usr/local/bin/opencode && "
                    "systemctl restart ssh"]],
    }


def render_smoke_cloud_init(name: str, uid: int, ssh_key: str) -> str:
    """Render smoke YAML with its existing user schema and virtiofs behavior."""
    helper = "/usr/local/sbin/mester-v3-user-dirs.py"
    _safe(name, uid, ssh_key, "virtiofs", helper)
    script = provision_script(uid, "virtiofs", helper)
    lines = "\n".join("      " + line for line in script.rstrip("\n").splitlines())
    payload = helper_payload()
    return f"""#cloud-config
hostname: {name}
ssh_pwauth: false
disable_root: true
groups:
  - mester
users:
  - name: mester
    uid: {uid}
    primary_group: mester
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
      - {ssh_key}
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
      export DOCKER_HOST=unix:///run/user/{uid}/docker.sock
      export XDG_RUNTIME_DIR=/run/user/{uid}
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
{lines}
  - path: /usr/local/sbin/mester-v3-user-dirs.py
    owner: root:root
    permissions: '0644'
    encoding: b64
    content: {payload}
runcmd:
  - [bash, /usr/local/sbin/mester-v3-provision]
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    smoke = sub.add_parser("smoke-cloud-init")
    smoke.add_argument("--name", required=True)
    smoke.add_argument("--uid", required=True, type=int)
    smoke.add_argument("--ssh-key", required=True)
    script = sub.add_parser("provision-script")
    script.add_argument("--uid", required=True, type=int)
    script.add_argument("--workspace", choices=("virtiofs", "disk"), required=True)
    script.add_argument("--helper", choices=("/usr/local/sbin/mester-v3-user-dirs.py", "/usr/local/libexec/lm-guest-user-dirs.py"), required=True)
    args = parser.parse_args()
    try:
        if args.action == "smoke-cloud-init":
            sys.stdout.write(render_smoke_cloud_init(args.name, args.uid, args.ssh_key))
        else:
            sys.stdout.write(provision_script(args.uid, args.workspace, args.helper))
        return 0
    except (OSError, ValueError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
