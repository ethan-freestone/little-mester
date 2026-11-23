#!/usr/bin/env python3
"""Shared guest bootstrap foundation for smoke and disk-backed system VMs."""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request

NPM_CHANNEL_URL = "https://registry.npmjs.org/npm/next-10"
NPM_REGISTRY_ORIGIN = "https://registry.npmjs.org"
NPM_VERSION_RE = re.compile(r"10\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
NPM_MAX_METADATA_BYTES = 1024 * 1024
NPM_MAX_TARBALL_BYTES = 32 * 1024 * 1024
NPM_MAX_UNPACKED_BYTES = 64 * 1024 * 1024
NPM_NODE = "/usr/bin/node"
NPM_INSTALL_ROOT = "/usr/local/lib/node_modules"
NPM_BIN_DIR = "/usr/local/bin"
NPM_PREFIX = "/usr/local"
NPM_GUEST_HELPER = "/usr/local/libexec/mester-vm-guest-bootstrap.py"


class NpmBootstrapError(RuntimeError):
    """Refuse to install npm when registry metadata or artifacts are unexpected."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Registry metadata and tarballs must be served from their canonical URLs."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _registry_opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler(), _NoRedirect())


def _read_bounded(response, maximum: int) -> bytes:
    data = response.read(maximum + 1)
    if len(data) > maximum:
        raise NpmBootstrapError("npm registry response exceeded its size limit")
    return data


def _decode_sha512_sri(value: str) -> bytes:
    if not isinstance(value, str):
        raise NpmBootstrapError("npm metadata omitted SHA-512 integrity")
    tokens = value.split()
    if len(tokens) != 1 or not tokens[0].startswith("sha512-"):
        raise NpmBootstrapError("npm metadata integrity is not a single SHA-512 SRI value")
    encoded = tokens[0][len("sha512-"):]
    try:
        digest = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise NpmBootstrapError("npm metadata SHA-512 SRI value is malformed") from error
    if len(digest) != hashlib.sha512().digest_size or base64.b64encode(digest).decode("ascii") != encoded:
        raise NpmBootstrapError("npm metadata SHA-512 SRI value has the wrong digest length")
    return digest


def validate_npm_metadata(metadata: object) -> dict:
    """Validate one resolved npm `next-10` document before fetching its tarball."""
    if not isinstance(metadata, dict) or metadata.get("name") != "npm":
        raise NpmBootstrapError("npm channel returned unexpected package metadata")
    version = metadata.get("version")
    if not isinstance(version, str) or not NPM_VERSION_RE.fullmatch(version):
        raise NpmBootstrapError("npm next-10 channel did not resolve to a stable 10.x.y release")
    engines = metadata.get("engines")
    node_range = engines.get("node") if isinstance(engines, dict) else None
    if not isinstance(node_range, str) or not node_range.strip() or len(node_range) > 512:
        raise NpmBootstrapError("npm metadata omitted a bounded Node engine range")
    dist = metadata.get("dist")
    if not isinstance(dist, dict):
        raise NpmBootstrapError("npm metadata omitted distribution data")
    tarball = dist.get("tarball")
    expected_tarball = f"{NPM_REGISTRY_ORIGIN}/npm/-/npm-{version}.tgz"
    if tarball != expected_tarball:
        raise NpmBootstrapError("npm metadata tarball URL is not the canonical registry artifact")
    integrity = dist.get("integrity")
    digest = _decode_sha512_sri(integrity)
    unpacked_size = dist.get("unpackedSize")
    if (type(unpacked_size) is not int or not 0 < unpacked_size <= NPM_MAX_UNPACKED_BYTES):
        raise NpmBootstrapError("npm metadata unpacked size is missing or outside the safety bound")
    return {
        "version": version,
        "node_range": node_range,
        "tarball": expected_tarball,
        "integrity": integrity,
        "digest": digest,
        "unpacked_size": unpacked_size,
    }


def verify_npm_integrity(artifact: bytes, integrity: str) -> None:
    expected = _decode_sha512_sri(integrity)
    if hashlib.sha512(artifact).digest() != expected:
        raise NpmBootstrapError("downloaded npm tarball did not match registry SHA-512 integrity")


def _open_registry(opener, url: str, *, timeout: int, accept: str):
    request = urllib.request.Request(
        url, headers={"Accept": accept, "User-Agent": "little-mester-vm-bootstrap"})
    response = opener.open(request, timeout=timeout)
    if response.geturl() != url:
        response.close()
        raise NpmBootstrapError("npm registry response changed its canonical URL")
    return response


def _validate_archive_paths(archive: tarfile.TarFile) -> list:
    members = archive.getmembers()
    if not members:
        raise NpmBootstrapError("npm tarball was empty")
    declared = 0
    for member in members:
        name = pathlib.PurePosixPath(member.name)
        if (name.is_absolute() or not name.parts or name.parts[0] != "package"
                or any(part in ("", ".", "..") for part in name.parts)):
            raise NpmBootstrapError("npm tarball contained a path outside its package directory")
        if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
            raise NpmBootstrapError("npm tarball contained a special filesystem entry")
        if member.isfile():
            declared += member.size
            if member.size < 0 or declared > NPM_MAX_UNPACKED_BYTES:
                raise NpmBootstrapError("npm tarball exceeded its extraction size limit")
    return members


def _safe_extract_npm(archive_path: pathlib.Path, extract_root: pathlib.Path) -> pathlib.Path:
    extract_root.mkdir(mode=0o700)
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = _validate_archive_paths(archive)
            archive.extractall(path=extract_root, members=members, filter="data")
    except (OSError, tarfile.TarError, ValueError) as error:
        if isinstance(error, NpmBootstrapError):
            raise
        raise NpmBootstrapError("npm tarball could not be safely extracted") from error
    package = extract_root / "package"
    if not package.is_dir() or package.is_symlink():
        raise NpmBootstrapError("npm tarball did not contain a regular package directory")
    unpacked = 0
    for path in package.rglob("*"):
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            unpacked += info.st_size
        elif stat.S_ISDIR(info.st_mode):
            continue
        elif stat.S_ISLNK(info.st_mode):
            target = path.resolve(strict=True)
            if not target.is_relative_to(package.resolve()):
                raise NpmBootstrapError("npm tarball contained a link outside its package directory")
        else:
            raise NpmBootstrapError("npm tarball extracted a special filesystem entry")
        if unpacked > NPM_MAX_UNPACKED_BYTES:
            raise NpmBootstrapError("npm tarball exceeded its extraction size limit")
    return package


def _package_json(package: pathlib.Path) -> dict:
    try:
        data = json.loads((package / "package.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise NpmBootstrapError("npm package metadata was missing or malformed") from error
    if not isinstance(data, dict):
        raise NpmBootstrapError("npm package metadata was not an object")
    return data


def _validate_extracted_npm(package: pathlib.Path, metadata: dict) -> pathlib.Path:
    package_data = _package_json(package)
    if (package_data.get("name") != "npm"
            or package_data.get("version") != metadata["version"]
            or not isinstance(package_data.get("engines"), dict)
            or package_data["engines"].get("node") != metadata["node_range"]):
        raise NpmBootstrapError("verified npm artifact identity differed from registry metadata")
    binaries = package_data.get("bin")
    if not isinstance(binaries, dict) or binaries.get("npm") != "bin/npm-cli.js" or binaries.get("npx") != "bin/npx-cli.js":
        raise NpmBootstrapError("verified npm artifact did not declare the expected npm/npx entry points")
    executable_entries = ("bin/npm-cli.js", "bin/npx-cli.js",
                          "node_modules/node-gyp/bin/node-gyp.js")
    for relative in (*executable_entries, "node_modules/semver/package.json"):
        path = package / relative
        try:
            info = path.lstat()
        except OSError as error:
            raise NpmBootstrapError(f"verified npm artifact omitted {relative}") from error
        if (not stat.S_ISREG(info.st_mode)
                or (relative in executable_entries and not info.st_mode & 0o111)):
            raise NpmBootstrapError(f"verified npm artifact entry has an unsafe type or mode: {relative}")
    return package / "node_modules/semver"


def _node_version(node: str) -> str:
    try:
        result = subprocess.run([node, "--version"], text=True, capture_output=True,
                                check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NpmBootstrapError("installed Node runtime could not be queried") from error
    version = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+", version):
        raise NpmBootstrapError("installed Node runtime reported an invalid version")
    return version.removeprefix("v")


def _verify_node_engine(node: str, semver_directory: pathlib.Path,
                        node_version: str, node_range: str) -> None:
    expression = (
        "const semver=require(process.argv[1]);"
        "const version=process.argv[2], range=process.argv[3];"
        "if (!semver.valid(version) || !semver.validRange(range) || "
        "!semver.satisfies(version, range)) process.exit(1);"
    )
    try:
        result = subprocess.run(
            [node, "-e", expression, str(semver_directory), node_version, node_range],
            text=True, capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NpmBootstrapError("npm Node-engine compatibility could not be verified") from error
    if result.returncode != 0:
        detail = result.stderr.strip()[:300]
        raise NpmBootstrapError(
            "resolved npm release does not support the installed Node runtime"
            + (f": {detail}" if detail else ""))


def _ensure_global_prefix_config(path: pathlib.Path) -> None:
    if not path.is_absolute():
        raise NpmBootstrapError("npm selected a non-absolute global configuration path")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    parent_info = path.parent.lstat()
    if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
            or parent_info.st_uid != os.geteuid()
            or parent_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise NpmBootstrapError("npm global configuration directory is not protected")
    try:
        info = path.lstat()
    except FileNotFoundError:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, 0o644)
        except FileExistsError:
            return _ensure_global_prefix_config(path)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(f"prefix={NPM_PREFIX}\n")
            output.flush()
            os.fsync(output.fileno())
        return
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise NpmBootstrapError("global npm configuration is not a regular file")
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise NpmBootstrapError("global npm configuration could not be read") from error
    if re.search(r"(?m)^\s*prefix\s*=", content):
        return
    updated = content
    if updated and not updated.endswith("\n"):
        updated += "\n"
    updated += f"prefix={NPM_PREFIX}\n"
    fd, temporary = tempfile.mkstemp(prefix=".npmrc-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, stat.S_IMODE(info.st_mode))
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _atomic_symlink(target: pathlib.Path, directory: pathlib.Path, name: str) -> None:
    destination = directory / name
    fd, temporary = tempfile.mkstemp(prefix=f".{name}-", dir=directory)
    os.close(fd)
    os.unlink(temporary)
    try:
        os.symlink(os.path.relpath(target, directory), temporary)
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _run_npm(node: str, npm_cli: pathlib.Path, *arguments: str, env=None) -> str:
    try:
        result = subprocess.run([node, str(npm_cli), *arguments], text=True,
                                capture_output=True, check=False, timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NpmBootstrapError("verified npm CLI could not be executed") from error
    if result.returncode != 0:
        detail = result.stderr.strip()[:300]
        raise NpmBootstrapError("verified npm CLI validation failed"
                                + (f": {detail}" if detail else ""))
    return result.stdout.strip()


def _bounded_diagnostic_value(value: object, *, maximum: int = 180) -> str:
    rendered = str(value).encode("unicode_escape").decode("ascii")
    if len(rendered) > maximum:
        return rendered[:maximum] + "..."
    return rendered


def _npm_policy_diagnostic(*, expected_version: str, actual_version: str,
                           expected_prefix: str, actual_prefix: str,
                           node: str, node_version: str, npm_cli: pathlib.Path,
                           selected_global_config: str,
                           confirmed_global_config: str) -> None:
    """Emit only fixed npm policy fields; never dump environment or config contents."""
    path_npm = shutil.which("npm")
    fields = (
        ("expected_version", expected_version),
        ("observed_version", actual_version),
        ("expected_prefix", expected_prefix),
        ("observed_prefix", actual_prefix),
        ("node_version", node_version),
        ("node_path", node),
        ("node_realpath", os.path.realpath(node)),
        ("npm_cli_path", str(npm_cli)),
        ("npm_cli_realpath", os.path.realpath(npm_cli)),
        ("path_npm", path_npm or "not-found"),
        ("path_npm_realpath", os.path.realpath(path_npm) if path_npm else "not-found"),
        ("globalconfig_initial", selected_global_config),
        ("globalconfig_reported", confirmed_global_config),
    )
    details = " ".join(
        f"{name}={_bounded_diagnostic_value(value)!r}" for name, value in fields)
    print(f"npm bootstrap policy diagnostic: {details}", file=sys.stderr)


def install_npm(*, install_root: str = NPM_INSTALL_ROOT, bin_dir: str = NPM_BIN_DIR,
                global_config: str | None = None, prefix: str = NPM_PREFIX,
                node: str = NPM_NODE, opener=None) -> dict:
    """Install a verified moving npm 10 artifact; never fall back to another channel."""
    if prefix != NPM_PREFIX:
        raise NpmBootstrapError("npm global prefix must remain the protected /usr/local path")
    opener = _registry_opener() if opener is None else opener
    try:
        with _open_registry(opener, NPM_CHANNEL_URL, timeout=30,
                            accept="application/json") as response:
            raw_metadata = _read_bounded(response, NPM_MAX_METADATA_BYTES)
        metadata = validate_npm_metadata(json.loads(raw_metadata))
    except NpmBootstrapError:
        raise
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as error:
        raise NpmBootstrapError("npm next-10 metadata could not be resolved") from error

    node_version = _node_version(node)
    artifact = bytearray()
    try:
        with _open_registry(opener, metadata["tarball"], timeout=60,
                            accept="application/octet-stream") as response:
            while True:
                chunk = response.read(128 * 1024)
                if not chunk:
                    break
                artifact.extend(chunk)
                if len(artifact) > NPM_MAX_TARBALL_BYTES:
                    raise NpmBootstrapError("npm tarball exceeded its download size limit")
    except NpmBootstrapError:
        raise
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise NpmBootstrapError("npm next-10 tarball could not be downloaded") from error
    verify_npm_integrity(bytes(artifact), metadata["integrity"])

    root = pathlib.Path(install_root)
    binaries = pathlib.Path(bin_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o755)
    binaries.mkdir(parents=True, exist_ok=True, mode=0o755)
    for directory in (root, binaries):
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            raise NpmBootstrapError("npm installation directory is not a real directory")

    with tempfile.TemporaryDirectory(prefix=".npm-bootstrap-", dir=root) as temporary_name:
        temporary_root = pathlib.Path(temporary_name)
        archive_path = temporary_root / "npm.tgz"
        archive_path.write_bytes(artifact)
        package = _safe_extract_npm(archive_path, temporary_root / "extract")
        semver_directory = _validate_extracted_npm(package, metadata)
        _verify_node_engine(node, semver_directory, node_version, metadata["node_range"])

        digest_id = base64.b16encode(metadata["digest"][:8]).decode("ascii").lower()
        installed_package = root / f"npm-{metadata['version']}-{digest_id}"
        if os.path.lexists(installed_package):
            if installed_package.is_symlink() or not installed_package.is_dir():
                raise NpmBootstrapError("existing npm install target is not a regular directory")
            _validate_extracted_npm(installed_package, metadata)
        else:
            os.replace(package, installed_package)

    npm_cli = installed_package / "bin/npm-cli.js"
    npx_cli = installed_package / "bin/npx-cli.js"
    node_gyp_cli = installed_package / "node_modules/node-gyp/bin/node-gyp.js"
    _atomic_symlink(npm_cli, binaries, "npm")
    _atomic_symlink(npx_cli, binaries, "npx")
    _atomic_symlink(node_gyp_cli, binaries, "node-gyp")

    actual_version = _run_npm(node, npm_cli, "--version")
    npm_environment = os.environ.copy()
    if global_config is None:
        if "NPM_CONFIG_GLOBALCONFIG" in npm_environment:
            raise NpmBootstrapError("global npm configuration override is not allowed during provisioning")
    else:
        npm_environment["NPM_CONFIG_GLOBALCONFIG"] = str(global_config)
    selected_global_config = _run_npm(
        node, npm_cli, "config", "get", "globalconfig", env=npm_environment)
    _ensure_global_prefix_config(pathlib.Path(selected_global_config))
    actual_prefix = _run_npm(node, npm_cli, "config", "get", "prefix", env=npm_environment)
    confirmed_global_config = _run_npm(
        node, npm_cli, "config", "get", "globalconfig", env=npm_environment)
    # npm derives its reported default globalconfig path from the effective
    # prefix. Setting prefix=/usr/local can therefore move this reported path
    # from the initial node prefix (/usr/etc/npmrc) to /usr/local/etc/npmrc.
    # The initial file remains the one npm loaded; either that path or the
    # effective-prefix-derived path is expected, but unrelated movement is not.
    prefix_global_config = str(pathlib.Path(actual_prefix) / "etc/npmrc")
    global_config_is_expected = confirmed_global_config in (
        selected_global_config, prefix_global_config)
    if (actual_version != metadata["version"] or actual_prefix != prefix
            or not global_config_is_expected):
        _npm_policy_diagnostic(
            expected_version=metadata["version"], actual_version=actual_version,
            expected_prefix=prefix, actual_prefix=actual_prefix,
            node=node, node_version=node_version, npm_cli=npm_cli,
            selected_global_config=selected_global_config,
            confirmed_global_config=confirmed_global_config)
        raise NpmBootstrapError("installed npm version or global prefix did not match policy")
    print("npm bootstrap: channel=next-10 "
          f"version={metadata['version']} integrity={metadata['integrity']} "
          f"node={node_version} prefix={actual_prefix} globalconfig={selected_global_config}")
    return {"version": metadata["version"], "integrity": metadata["integrity"],
            "node": node_version, "prefix": actual_prefix,
            "globalconfig": selected_global_config}


# The provisioning sequence is shared; workspace setup is the only launch-mode
# difference. Keep smoke's virtiofs setup and package/rootless-Docker sequence.
_PROVISION = r'''#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export HTTP_PROXY=http://10.0.2.100:3128 HTTPS_PROXY=http://10.0.2.100:3128
export http_proxy=http://10.0.2.100:3128 https_proxy=http://10.0.2.100:3128
@WORKSPACE@
apt-get update
apt-get install -y ca-certificates curl uidmap dbus-user-session slirp4netns fuse-overlayfs kmod iptables
apt-get install -y --no-install-recommends nodejs libnode-dev build-essential
python3 -I @NPM_BOOTSTRAP@ install-npm
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
    return (_PROVISION.replace("@WORKSPACE@", setup)
            .replace("@UID@", str(uid)).replace("@HELPER@", helper)
            .replace("@NPM_BOOTSTRAP@", NPM_GUEST_HELPER))


def bootstrap_payload() -> str:
    path = pathlib.Path(__file__).with_name("guest-bootstrap.py")
    info = os.lstat(path)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise ValueError("guest npm bootstrap source is not a protected regular file")
    return base64.b64encode(path.read_bytes()).decode("ascii")


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
            {"path": NPM_GUEST_HELPER, "owner": "root:root", "permissions": "0755",
             "encoding": "b64", "content": bootstrap_payload()},
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
  - path: {NPM_GUEST_HELPER}
    owner: root:root
    permissions: '0755'
    encoding: b64
    content: {bootstrap_payload()}
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
    sub.add_parser("install-npm")
    args = parser.parse_args()
    try:
        if args.action == "smoke-cloud-init":
            sys.stdout.write(render_smoke_cloud_init(args.name, args.uid, args.ssh_key))
        elif args.action == "provision-script":
            sys.stdout.write(provision_script(args.uid, args.workspace, args.helper))
        else:
            if os.geteuid() != 0:
                raise NpmBootstrapError("npm bootstrap requires root")
            install_npm()
        return 0
    except (NpmBootstrapError, OSError, ValueError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
