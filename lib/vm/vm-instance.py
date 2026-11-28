#!/usr/bin/env python3
"""Validated identities and derived paths for the fixed VM and named profiles."""

from __future__ import annotations

import dataclasses
import hashlib
import pathlib
import re


PROFILE_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
COMMIT_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
RESERVED_NAMES = frozenset({
    "system", "profiles", "installations", "prune", "bootstrap", "control",
    "prepare-dependencies",
})
PROFILE_PORT_FIRST = 22223
PROFILE_PORT_LAST_START = 23220


class InvalidInstance(ValueError):
    """A protected instance record cannot safely describe a VM instance."""


@dataclasses.dataclass(frozen=True)
class Instance:
    """Small runtime descriptor; all privileged paths are fixed or derived."""

    kind: str
    name: str
    owner_uid: int | None
    runtime_sha: str | None
    vm_uid: int | None
    vm_gid: int | None
    proxy_uid: int | None
    proxy_gid: int | None
    cpus: int
    memory_mib: int
    ssh_port: int
    proxy_port: int
    metrics_port: int
    state_root: pathlib.Path
    config_root: pathlib.Path
    vm_state: pathlib.Path
    client_state: pathlib.Path
    qmp_runtime: pathlib.Path
    proxy_runtime: pathlib.Path
    workspace_runtime: pathlib.Path
    disk: pathlib.Path
    seed: pathlib.Path
    control_key: pathlib.Path
    host_key: pathlib.Path
    known_hosts: pathlib.Path
    base_path: pathlib.Path | None
    vm_user: str
    proxy_user: str
    vm_unit: str
    proxy_unit: str
    workspace_unit: str
    metrics_unit: str
    workspace_path: pathlib.Path | None

    @property
    def qmp_socket(self) -> pathlib.Path:
        return self.qmp_runtime / "qmp.sock"

    @property
    def proxy_port_file(self) -> pathlib.Path:
        return self.proxy_runtime / "port"

    @property
    def workspace_socket(self) -> pathlib.Path:
        return self.workspace_runtime / "virtiofs.sock"

    @classmethod
    def singleton(cls) -> "Instance":
        """The existing fixed system VM identity, paths, resources and ports."""
        state = pathlib.Path("/var/lib/little-mester-vm")
        config = pathlib.Path("/etc/little-mester-vm")
        qmp = pathlib.Path("/run/little-mester-vm")
        proxy = pathlib.Path("/run/lm-vm-proxy")
        workspace = pathlib.Path("/run/little-mester-vm-workspace")
        return cls(
            kind="singleton", name="little-mester-system-vm", owner_uid=None,
            runtime_sha=None, vm_uid=None, vm_gid=None, proxy_uid=None, proxy_gid=None,
            cpus=2, memory_mib=4096, ssh_port=22222, proxy_port=32128, metrics_port=32129,
            state_root=state, config_root=config, vm_state=state, client_state=config,
            qmp_runtime=qmp, proxy_runtime=proxy, workspace_runtime=workspace,
            disk=state / "disk.qcow2", seed=state / "seed.iso",
            control_key=config / "control_ed25519", host_key=config / "ssh_host_ed25519_key",
            known_hosts=config / "known_hosts", base_path=None,
            vm_user="lm-vm", proxy_user="lm-vm-proxy",
            vm_unit="little-mester-vm.service",
            proxy_unit="little-mester-vm-proxy.service",
            workspace_unit="little-mester-vm-workspace.service",
            metrics_unit="little-mester-vm-metrics.service", workspace_path=None)

    @classmethod
    def profile(cls, *, name: str, owner_uid: int, runtime_sha: str,
                vm_uid: int, vm_gid: int, proxy_uid: int, proxy_gid: int,
                base_source_sha256: str, base_manifest_sha256: str, ssh_port: int,
                cpus: int = 2, memory_mib: int = 4096,
                workspace_path: str | None = None) -> "Instance":
        """Validate mapping values and derive every privileged path/name locally.

        The caller must first read and validate the protected mapping. Neither a
        mapping nor a public CLI request can choose a filesystem path, unit,
        service name, QEMU option, or account name.
        """
        if (not isinstance(name, str) or not PROFILE_NAME.fullmatch(name)
                or name in RESERVED_NAMES):
            raise InvalidInstance("profile name is outside the allowed grammar")
        for label, value in (("owner UID", owner_uid), ("VM UID", vm_uid),
                             ("VM GID", vm_gid), ("proxy UID", proxy_uid),
                             ("proxy GID", proxy_gid)):
            if type(value) is not int:
                raise InvalidInstance(f"profile {label} must be an integer")
        if (not 1 <= owner_uid < 2**32 - 1
                or not 1 <= vm_uid < 1000 or not 1 <= proxy_uid < 1000):
            raise InvalidInstance("profile owner/service UID is outside its allowed range")
        if not 1 <= vm_gid < 1000 or not 1 <= proxy_gid < 1000:
            raise InvalidInstance("profile service GID is outside its allowed range")
        if vm_uid == proxy_uid or vm_gid == proxy_gid:
            raise InvalidInstance("profile VM and proxy identities must be distinct")
        if not isinstance(runtime_sha, str) or not COMMIT_SHA.fullmatch(runtime_sha):
            raise InvalidInstance("profile runtime pin must be a complete lowercase commit SHA")
        if (not isinstance(base_source_sha256, str)
                or not SHA256.fullmatch(base_source_sha256)
                or not isinstance(base_manifest_sha256, str)
                or not SHA256.fullmatch(base_manifest_sha256)):
            raise InvalidInstance("profile approved-base identity is malformed")
        if type(cpus) is not int or not 1 <= cpus <= 16:
            raise InvalidInstance("profile CPU allocation is outside its fixed safe range")
        if type(memory_mib) is not int or not 256 <= memory_mib <= 65536:
            raise InvalidInstance("profile memory allocation is outside its fixed safe range")
        if (type(ssh_port) is not int or not PROFILE_PORT_FIRST <= ssh_port <= PROFILE_PORT_LAST_START
                or (ssh_port - PROFILE_PORT_FIRST) % 3 != 0):
            raise InvalidInstance("profile SSH port is not an available start of a reserved port block")
        if workspace_path is not None and (
                not isinstance(workspace_path, str) or not workspace_path.startswith("/")
                or any(character in workspace_path for character in "\0\n\r")):
            raise InvalidInstance("profile workspace observation must be an absolute safe path")

        identity = hashlib.sha256(f"{owner_uid}:{name}".encode("ascii")).hexdigest()[:12]
        state = pathlib.Path("/var/lib/little-mester-vm/profiles") / name
        config = pathlib.Path("/etc/little-mester-vm/profiles") / name
        runtime = pathlib.Path("/run/little-mester-vm-profiles") / name
        vm_state = state / "vm"
        client_state = state / "client"
        vm_unit = f"little-mester-vm-profile-{identity}.service"
        return cls(
            kind="profile", name=name, owner_uid=owner_uid, runtime_sha=runtime_sha,
            vm_uid=vm_uid, vm_gid=vm_gid, proxy_uid=proxy_uid, proxy_gid=proxy_gid,
            cpus=cpus, memory_mib=memory_mib, ssh_port=ssh_port,
            proxy_port=ssh_port + 1, metrics_port=ssh_port + 2,
            state_root=state, config_root=config, vm_state=vm_state,
            client_state=client_state, qmp_runtime=runtime / "vm",
            proxy_runtime=runtime / "proxy", workspace_runtime=runtime / "workspace",
            disk=vm_state / "disk.qcow2", seed=vm_state / "seed.iso",
            control_key=client_state / "control_ed25519",
            host_key=vm_state / "ssh_host_ed25519_key",
            known_hosts=client_state / "known_hosts",
            base_path=(pathlib.Path("/var/lib/little-mester-vm/bases")
                       / base_source_sha256 / "base.qcow2"),
            vm_user=f"lmv-{identity}", proxy_user=f"lmp-{identity}",
            vm_unit=vm_unit,
            proxy_unit=vm_unit.removesuffix(".service") + "-proxy.service",
            workspace_unit=vm_unit.removesuffix(".service") + "-workspace.service",
            metrics_unit=vm_unit.removesuffix(".service") + "-metrics.service",
            workspace_path=None if workspace_path is None else pathlib.Path(workspace_path))
