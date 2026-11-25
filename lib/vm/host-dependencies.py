#!/usr/bin/python3
"""Capability checks and narrowly-scoped native package setup for the VM host."""

from __future__ import annotations

import dataclasses
import grp
import os
import pathlib
import re
import stat
import subprocess
import sys


PYTHON = "/usr/bin/python3"
APT = "/usr/bin/apt-get"
PACMAN = "/usr/bin/pacman"
SUDO = "/usr/bin/sudo"


@dataclasses.dataclass(frozen=True)
class Requirement:
    name: str
    executable: str | None = None
    module: str | None = None
    packages: tuple[tuple[str, str], ...] = ()
    capability: str | None = None


# Package names live only in these two small backend tables. Runtime consumers
# ask for capabilities, not distro-specific package names.
PRODUCTION_REQUIREMENTS = (
        Requirement("system Python", "/usr/bin/python3", packages=(
            ("apt", "python3"), ("pacman", "python"))),
        Requirement("Git candidate pinning", "/usr/bin/git", packages=(
            ("apt", "git"), ("pacman", "git"))),
        Requirement("curl", "/usr/bin/curl", packages=(
            ("apt", "curl"), ("pacman", "curl"))),
        Requirement("QEMU image utility", "/usr/bin/qemu-img", packages=(
            ("apt", "qemu-utils"), ("pacman", "qemu-img"))),
        Requirement("QEMU x86_64", "/usr/bin/qemu-system-x86_64", packages=(
            ("apt", "qemu-system-x86"), ("pacman", "qemu-desktop"))),
        Requirement("virtiofs daemon", capability="virtiofsd", packages=(
            ("apt", "virtiofsd"), ("pacman", "virtiofsd"))),
        Requirement("user namespace ID mapping helpers", capability="uidmap", packages=(
            ("apt", "uidmap"), ("pacman", "shadow"))),
        Requirement("ISO creator", "/usr/bin/genisoimage", packages=(
            ("apt", "genisoimage"), ("pacman", "cdrtools"))),
        Requirement("SSH client", "/usr/bin/ssh", packages=(
            ("apt", "openssh-client"), ("pacman", "openssh"))),
        Requirement("SSH key generator", "/usr/bin/ssh-keygen", packages=(
            ("apt", "openssh-client"), ("pacman", "openssh"))),
        Requirement("socat", "/usr/bin/socat", packages=(
            ("apt", "socat"), ("pacman", "socat"))),
        Requirement("systemd control", "/usr/bin/systemctl", packages=(
            ("apt", "systemd"), ("pacman", "systemd"))),
        Requirement("sudo", SUDO, packages=(
            ("apt", "sudo"), ("pacman", "sudo"))),
        Requirement("KVM device and access", capability="kvm"),
        Requirement("systemd system manager", capability="systemd-manager"),
)

REQUIREMENTS = {
    "production": PRODUCTION_REQUIREMENTS,
    "acceptance": (
        # Acceptance adds its tests/tools without making pexpect a normal runtime dependency.
        *PRODUCTION_REQUIREMENTS,
        Requirement("Python pexpect module", module="pexpect", packages=(
            ("apt", "python3-pexpect"), ("pacman", "python-pexpect"))),
        Requirement("SSH client", "/usr/bin/ssh", packages=(
            ("apt", "openssh-client"), ("pacman", "openssh"))),
        Requirement("socket inspection", "/usr/bin/ss", packages=(
            ("apt", "iproute2"), ("pacman", "iproute2"))),
        Requirement("file metadata inspection", "/usr/bin/stat", packages=(
            ("apt", "coreutils"), ("pacman", "coreutils"))),
    ),
    "system-install": (
        *PRODUCTION_REQUIREMENTS,
        Requirement("sudo policy validator", "/usr/sbin/visudo", packages=(
            ("apt", "sudo"), ("pacman", "sudo"))),
        Requirement("system group/account tools", "/usr/sbin/groupadd", packages=(
            ("apt", "passwd"), ("pacman", "shadow"))),
        Requirement("system user creation", "/usr/sbin/useradd", packages=(
            ("apt", "passwd"), ("pacman", "shadow"))),
        Requirement("system user modification", "/usr/sbin/usermod", packages=(
            ("apt", "passwd"), ("pacman", "shadow"))),
    ),
    "ci": (
        *PRODUCTION_REQUIREMENTS,
        Requirement("Python pexpect module", module="pexpect", packages=(
            ("apt", "python3-pexpect"), ("pacman", "python-pexpect"))),
        Requirement("socket inspection", "/usr/bin/ss", packages=(
            ("apt", "iproute2"), ("pacman", "iproute2"))),
        Requirement("file metadata inspection", "/usr/bin/stat", packages=(
            ("apt", "coreutils"), ("pacman", "coreutils"))),
        Requirement("cloud image utilities", "/usr/bin/cloud-localds", packages=(
            ("apt", "cloud-image-utils"),)),
        Requirement("OpenSSH server", "/usr/sbin/sshd", packages=(
            ("apt", "openssh-server"),)),
        Requirement("virtiofs daemon", packages=(("apt", "virtiofsd"),)),
        Requirement("terminal capability compiler", "/usr/bin/infocmp", packages=(
            ("apt", "ncurses-bin"),)),
    ),
    "ci-tools": (
        Requirement("Python pexpect module", module="pexpect", packages=(
            ("apt", "python3-pexpect"),)),
        Requirement("socket inspection", "/usr/bin/ss", packages=(
            ("apt", "iproute2"),)),
        Requirement("OpenSSH server", packages=(
            ("apt", "openssh-server"),)),
        Requirement("terminal capability compiler", "/usr/bin/infocmp", packages=(
            ("apt", "ncurses-bin"),)),
    ),
}


class DependencyError(RuntimeError):
    pass


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _capability(requirement: Requirement) -> tuple[bool, str]:
    if requirement.executable:
        okay = _executable(requirement.executable)
        return okay, requirement.executable
    if requirement.module:
        try:
            result = subprocess.run(
                [PYTHON, "-I", "-c", f"import {requirement.module}"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=False,
            )
        except OSError:
            return False, f"{PYTHON}: import {requirement.module} unavailable"
        return result.returncode == 0, f"{PYTHON}: import {requirement.module}"
    if requirement.capability == "kvm":
        try:
            info = os.stat("/dev/kvm", follow_symlinks=False)
            gid = grp.getgrnam("kvm").gr_gid
            okay = (stat.S_ISCHR(info.st_mode) and info.st_gid == gid
                    and stat.S_IMODE(info.st_mode) & 0o060 == 0o060)
            # This is a service prerequisite, not an owner-process access test:
            # systemd runs the VM as lm-vm with SupplementaryGroups=kvm.
            # Actual access under that identity is enforced by system-vm.py.
            return okay, "/dev/kvm must be a kvm-group character device with group read/write access"
        except (OSError, KeyError):
            return False, "/dev/kvm must be a kvm-group character device with group read/write access"
    if requirement.capability == "virtiofsd":
        for path in ("/usr/bin/virtiofsd", "/usr/libexec/virtiofsd",
                     "/usr/lib/qemu/virtiofsd", "/usr/lib/virtiofsd"):
            if _executable(path):
                return True, path
        return False, "virtiofsd must exist at one of the fixed supported paths"
    if requirement.capability == "uidmap":
        missing_helpers = [path for path in ("/usr/bin/newuidmap", "/usr/bin/newgidmap")
                           if not _executable(path)]
        if missing_helpers:
            return False, "required namespace mapping helpers are unavailable: " + ", ".join(missing_helpers)
        return True, "/usr/bin/newuidmap and /usr/bin/newgidmap"
    if requirement.capability == "systemd-manager":
        if not pathlib.Path("/run/systemd/system").is_dir() or not _executable("/usr/bin/systemctl"):
            return False, "responsive systemd system manager"
        try:
            result = subprocess.run(["/usr/bin/systemctl", "show", "--property=Version", "--value"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=5, check=False)
            okay = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            okay = False
        return okay, "responsive systemd system manager"
    return False, "unknown host capability"


def _os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in pathlib.Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.startswith("#"):
                continue
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    except OSError:
        pass
    return values


def detect_backend() -> str:
    release = _os_release()
    distro = release.get("ID", "").lower()
    if distro in {"arch", "cachyos"}:
        if _executable(PACMAN):
            return "pacman"
        raise DependencyError(f"{distro} requires the supported pacman backend at {PACMAN}, which is unavailable")
    if distro in {"ubuntu", "debian"}:
        if _executable(APT):
            return "apt"
        raise DependencyError(f"{distro} requires the supported apt backend at {APT}, which is unavailable")
    present = [name for name, path in (("apt", APT), ("pacman", PACMAN)) if _executable(path)]
    if len(present) == 1:
        # Package-manager presence alone does not establish compatible package
        # names; unsupported identities must not be guessed.
        raise DependencyError(f"{present[0]} is present, but this Linux distribution is not in the validated package mapping")
    if present:
        raise DependencyError("multiple supported package managers are present but the distribution family is ambiguous")
    raise DependencyError("no supported package manager found (validated mappings: Ubuntu or Debian/apt and Arch or CachyOS/pacman)")


def missing(scope: str) -> tuple[list[Requirement], list[str]]:
    if scope not in REQUIREMENTS:
        raise DependencyError(f"unknown dependency scope: {scope}")
    absent = []
    non_installable = []
    for requirement in REQUIREMENTS[scope]:
        okay, actual = _capability(requirement)
        if okay:
            continue
        absent.append(requirement)
        if requirement.capability:
            non_installable.append(actual)
    return absent, non_installable


def _package_installed(backend: str, package: str) -> bool:
    if backend == "apt":
        result = subprocess.run(
            ["/usr/bin/dpkg-query", "-W", "-f=${db:Status-Status}", package],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "installed"
    result = subprocess.run([PACMAN, "-Q", package], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0


def _requested_packages(backend: str, absent: list[Requirement]) -> list[str]:
    packages = []
    for requirement in absent:
        mapping = dict(requirement.packages)
        package = mapping.get(backend)
        if not package:
            raise DependencyError(f"no {backend} package mapping for missing prerequisite: {requirement.name}")
        if _package_installed(backend, package):
            raise DependencyError(
                f"{requirement.name} is unavailable although package {package} is installed; refusing to reinstall or replace it"
            )
        if package not in packages:
            packages.append(package)
    return packages


def package_names(backend: str, scope: str) -> list[str]:
    """Return the deduplicated package definitions for a noninteractive caller such as CI."""
    if backend not in ("apt", "pacman") or scope not in REQUIREMENTS:
        raise DependencyError("unsupported backend or dependency scope")
    packages = []
    for requirement in REQUIREMENTS[scope]:
        package = dict(requirement.packages).get(backend)
        if requirement.packages and not package:
            raise DependencyError(f"no {backend} package mapping for {scope} prerequisite: {requirement.name}")
        if package and package not in packages:
            packages.append(package)
    return packages


def _run(argv: list[str], *, capture: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL if capture else None,
                              stdout=subprocess.PIPE if capture else None,
                              stderr=subprocess.STDOUT if capture else None,
                              text=True, check=False)
    except OSError as error:
        raise DependencyError(f"could not run {argv[0]}: {error}") from error


def _apt_plan(packages: list[str], *, display: bool = False) -> tuple[str, ...]:
    result = _run([APT, "-s", "install", "--no-install-recommends", "--no-upgrade",
                   "--no-remove", "--", *packages])
    if result.returncode:
        raise DependencyError("apt could not safely simulate the requested transaction; no package changes were made\n" + result.stdout)
    if display and result.stdout:
        print("apt transaction preview:", file=sys.stderr)
        print(result.stdout, file=sys.stderr, end="" if result.stdout.endswith("\n") else "\n")
    additions: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("Remv ") or line.startswith("Inst ") and " [" in line:
            raise DependencyError("apt preview includes a removal or upgrade; refusing the transaction")
        match = re.match(r"Inst ([^ ]+)(?: \[[^]]+\])? \(([^ )]+)", line)
        if match:
            additions.append(f"{match.group(1)}={match.group(2)}")
    names = {entry.split("=", 1)[0] for entry in additions}
    if not set(packages).issubset(names):
        raise DependencyError("apt preview did not include every requested package; refusing the transaction")
    return tuple(sorted(additions))


def _pacman_plan(packages: list[str], *, display: bool = False) -> tuple[str, ...]:
    # Never use -y or -u: in Arch, package installs against stale sync state can
    # constitute a partial upgrade. Print-only resolution must expose exactly
    # the additions before the owner is asked to approve anything.
    upgrades = _run([PACMAN, "-Qu"])
    # _run merges stderr into stdout. Pacman may use status 1 for a legitimate
    # empty query result; any output (including diagnostics) or any other
    # status remains ambiguous and must fail closed.
    if upgrades.returncode not in (0, 1) or upgrades.stdout != "":
        raise DependencyError(
            "pacman reports pending system upgrades or cannot verify package state; "
            "refusing a partial upgrade. Perform normal full system maintenance separately.\n"
            + (upgrades.stdout or "")
        )
    result = _run([PACMAN, "-S", "--needed", "--print", "--print-format", "%n %v", "--", *packages])
    output = result.stdout
    if result.returncode or re.search(r"(?i)conflict|replac|remov|upgrade|synchroniz.*database", output):
        raise DependencyError("pacman could not prove a safe install-only transaction; do not run -Sy or a partial upgrade\n" + output)
    if display and output:
        print("pacman transaction preview:", file=sys.stderr)
        print(output, file=sys.stderr, end="" if output.endswith("\n") else "\n")
    additions = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 2 and re.fullmatch(r"[A-Za-z0-9@._+-]+", fields[0]):
            additions.append(f"{fields[0]}={fields[1]}")
    names = {entry.split("=", 1)[0] for entry in additions}
    if not set(packages).issubset(names):
        raise DependencyError("pacman preview did not include every requested package; refusing the transaction")
    # If the sync plan contains any already-installed package, this operation
    # would upgrade/replace it. Fail closed instead of partial-upgrading.
    installed = [entry.split("=", 1)[0] for entry in additions
                 if _package_installed("pacman", entry.split("=", 1)[0])]
    if installed:
        raise DependencyError("pacman preview would change installed packages (unsupported partial upgrade): " + ", ".join(installed))
    return tuple(sorted(additions))


def _plan(backend: str, packages: list[str], *, display: bool = False) -> tuple[str, ...]:
    return (_apt_plan(packages, display=display) if backend == "apt"
            else _pacman_plan(packages, display=display))


def ensure(scope: str, *, interactive: bool | None = None) -> None:
    absent, non_installable = missing(scope)
    backend = detect_backend()
    if not absent:
        print(f"Host prerequisites ({scope}): PASS")
        return
    print(f"Missing {scope} prerequisites:", file=sys.stderr)
    installable = []
    for requirement in absent:
        _, actual = _capability(requirement)
        kind = "host capability (not installable as a package)" if requirement.capability else "package-backed prerequisite"
        print(f"  - {requirement.name}: {actual} [{kind}]", file=sys.stderr)
        if not requirement.capability:
            installable.append(requirement)
    if non_installable:
        print("Non-installable host blockers: " + "; ".join(non_installable), file=sys.stderr)
    if not installable:
        raise DependencyError("no missing prerequisites can be installed as packages; address the host capability blockers above")
    packages = _requested_packages(backend, installable)
    plan = _plan(backend, packages, display=True)
    if os.geteuid() != 0 and not _executable(SUDO):
        raise DependencyError("sudo is unavailable; no safe privilege path exists for package installation (use an authorized root session)")
    print(f"Proposed {backend} package transaction (install only; no upgrades/removals):")
    for package in plan:
        print(f"  + {package}")
    if backend == "pacman":
        print("Pacman uses only its cached sync databases here; this tool will not refresh them or run a system upgrade.")
        print("Approve only if you recently completed a full system upgrade against these repositories; otherwise decline and perform maintenance separately.")
        print("Pacman will display and request confirmation for its final transaction; decline any replacement, removal, or upgrade.")
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stderr.isatty()
    if not interactive:
        raise DependencyError("missing prerequisites; noninteractive mode will not request or assume package-install approval")
    try:
        answer = input("Approve this host package installation? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt) as error:
        raise DependencyError("package installation was not approved; no package changes were made") from error
    if answer not in ("y", "yes"):
        raise DependencyError("package installation declined; no package changes were made")

    # Re-resolve immediately before privilege escalation. Any changed plan is
    # rejected; the native manager keeps its own transaction output visible.
    if _plan(backend, packages) != plan:
        raise DependencyError("package transaction changed after approval; refusing to execute it")
    privilege = [] if os.geteuid() == 0 else [SUDO]
    if backend == "apt":
        versions = {entry.split("=", 1)[0]: entry.split("=", 1)[1] for entry in plan}
        exact_targets = [f"{package}={versions[package]}" for package in packages]
        print("Review the native apt transaction summary before its final confirmation; decline if it differs from the preview.")
        command = [*privilege, APT, "install", "--no-install-recommends", "--no-upgrade", "--no-remove", "--", *exact_targets]
    else:
        versions = {entry.split("=", 1)[0]: entry.split("=", 1)[1] for entry in plan}
        exact_targets = [f"{package}={versions[package]}" for package in packages]
        command = [*privilege, PACMAN, "-S", "--needed", "--", *exact_targets]
    try:
        result = _run(command, capture=False)
    except KeyboardInterrupt as error:
        raise DependencyError(
            f"{backend} package installation was interrupted; no workflow will continue, and package state may need owner inspection"
        ) from error
    if result.returncode:
        raise DependencyError(f"{backend} package installation failed or was interrupted (exit {result.returncode}); inspect the package-manager output")
    still_missing, _ = missing(scope)
    if still_missing:
        names = ", ".join(requirement.name for requirement in still_missing)
        raise DependencyError(f"post-install prerequisite recheck failed: {names}")
    print(f"Host prerequisites ({scope}): PASS after installation")


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scope", choices=tuple(REQUIREMENTS))
    parser.add_argument("--check-only", action="store_true", help="detect and report missing requirements without installing")
    parser.add_argument("--list-packages", choices=("apt", "pacman"), help="print mapped package names for a noninteractive caller")
    args = parser.parse_args(argv)
    try:
        if args.list_packages:
            print("\n".join(package_names(args.list_packages, args.scope)))
            return 0
        if args.check_only:
            detect_backend()
            absent, non_installable = missing(args.scope)
            if absent:
                for item in absent:
                    okay, actual = _capability(item)
                    print(f"MISSING {item.name}: {actual}")
                if non_installable:
                    print("NON_INSTALLABLE: " + "; ".join(non_installable))
                return 1
            print(f"Host prerequisites ({args.scope}): PASS")
            return 0
        ensure(args.scope)
        return 0
    except KeyboardInterrupt:
        print("DEPENDENCIES BLOCKED: interrupted; package state may need owner inspection", file=sys.stderr)
        return 130
    except (DependencyError, OSError, subprocess.SubprocessError) as error:
        print(f"DEPENDENCIES BLOCKED: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
