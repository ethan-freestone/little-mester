#!/usr/bin/env python3
"""Exercise signed named-profile install, real KVM boot/SSH, and graceful stop.

This qualification is intended only for a fresh disposable hosted-KVM runner
after the fixed production installer has enrolled its unprivileged owner.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import pty
import pwd
import re
import select
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time


REPO = pathlib.Path(__file__).resolve().parent.parent
PROFILE_NAME = "ci-profile"
TRUST_KEY = pathlib.Path("/etc/little-mester-vm/profile-control-trust.pem")
OWNER_RECORD = pathlib.Path("/etc/little-mester-vm/profile-control-owner.json")
CONTROL_HELPER = pathlib.Path("/usr/libexec/little-mester-vm/profile-control")
CONTROL_SUDOERS = pathlib.Path("/etc/sudoers.d/little-mester-vm-profile-control")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
PROFILE_STARTED = False


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load fixed test policy: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CONTROL = load_module("ci_profile_control", REPO / "lib/vm/profile-control.py")
SINGLETON_CI = load_module("ci_kvm_system_unit", REPO / "tests/ci-kvm-system-unit.py")
CI_INSTALL_PREFIX = SINGLETON_CI.CI_INSTALL_PREFIX
CI_INSTALL_PARENT = SINGLETON_CI.CI_INSTALL_PARENT


def run(argv: list[str], *, timeout: int = 60, check: bool = True,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=timeout,
                            check=False, env=env)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(argv)}\n"
                           f"{result.stdout[-5000:]}")
    if result.stdout.strip():
        print(result.stdout.rstrip())
    return result


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def root_lstat(path: pathlib.Path) -> dict | None:
    """Inspect one protected target as root without elevating the harness."""
    probe = (
        "import json, os, sys\n"
        "try:\n"
        " info = os.lstat(sys.argv[1])\n"
        "except FileNotFoundError:\n"
        " raise SystemExit(3)\n"
        "except OSError as error:\n"
        " print(f'{type(error).__name__}: {error}', file=sys.stderr)\n"
        " raise SystemExit(2)\n"
        "print(json.dumps({'mode': info.st_mode, 'uid': info.st_uid, 'gid': info.st_gid,"
        " 'nlink': info.st_nlink, 'size': info.st_size}))\n"
    )
    result = subprocess.run(
        ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-c", probe, str(path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=10, check=False)
    if result.returncode == 3:
        return None
    if result.returncode != 0:
        raise RuntimeError(f"privileged read-only path inspection failed for {path.name}: "
                           f"{result.stderr.strip()}")
    return json.loads(result.stdout)


def root_read_file(path: pathlib.Path, *, limit: int) -> bytes:
    """Read one bounded protected regular file for a CI assertion only."""
    reader = (
        "import os, stat, sys\n"
        "fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)\n"
        "try:\n"
        " info = os.fstat(fd)\n"
        " if not stat.S_ISREG(info.st_mode) or info.st_size > int(sys.argv[2]):\n"
        "  raise SystemExit('protected file is not a bounded regular file')\n"
        " data = bytearray()\n"
        " while True:\n"
        "  block = os.read(fd, min(65536, int(sys.argv[2]) + 1 - len(data)))\n"
        "  if not block: break\n"
        "  data.extend(block)\n"
        "  if len(data) > int(sys.argv[2]):\n"
        "   raise SystemExit('protected file exceeds its read bound')\n"
        " sys.stdout.buffer.write(data)\n"
        "finally:\n"
        " os.close(fd)\n"
    )
    result = subprocess.run(
        ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-c", reader,
         str(path), str(limit)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=10, check=False)
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"privileged bounded read failed for {path.name}: {detail}")
    return result.stdout


def assert_absent(path: pathlib.Path) -> None:
    if root_lstat(path) is not None:
        raise RuntimeError(f"fresh hosted profile target already exists; preserving it: {path}")


def assert_kvm() -> None:
    try:
        info = os.stat("/dev/kvm", follow_symlinks=False)
    except OSError as error:
        raise RuntimeError("named profile qualification requires hosted /dev/kvm") from error
    require(stat.S_ISCHR(info.st_mode) and os.access("/dev/kvm", os.R_OK | os.W_OK),
            "named profile qualification requires real readable/writable KVM")


def create_signed_bundle(candidate: pathlib.Path, directory: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path,
                                                                                 pathlib.Path, pathlib.Path]:
    private_key = directory / "ephemeral-profile-signing-key.pem"
    public_key = directory / "ephemeral-profile-signing-key.pub.pem"
    bundle_path = directory / "profile-control.tar"
    signature_path = directory / "profile-control.tar.sig"
    subprocess.run(["/usr/bin/openssl", "genpkey", "-algorithm", "ED25519",
                    "-out", str(private_key)], check=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    subprocess.run(["/usr/bin/openssl", "pkey", "-in", str(private_key), "-pubout",
                    "-out", str(public_key)], check=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)

    entries = (
        ("lib/vm/profile-control.py", 0o755),
        ("sudoers/little-mester-vm-profile-control", 0o440),
    )
    with bundle_path.open("wb") as output:
        with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for relative, mode in entries:
                payload = (candidate / relative).read_bytes()
                info = tarfile.TarInfo(relative)
                info.mode = mode
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
    os.chmod(private_key, 0o600)
    os.chmod(public_key, 0o600)
    os.chmod(bundle_path, 0o600)
    subprocess.run(["/usr/bin/openssl", "pkeyutl", "-sign", "-rawin", "-inkey",
                    str(private_key), "-in", str(bundle_path), "-out", str(signature_path)],
                   check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=20)
    require(signature_path.stat().st_size == 64, "ephemeral Ed25519 signature was not 64 bytes")
    os.chmod(signature_path, 0o600)
    return private_key, public_key, bundle_path, signature_path


def pty_ssh(env: dict[str, str], *, timeout: int = 90) -> None:
    pid, master = pty.fork()
    if pid == 0:
        os.execve(str(REPO / "lab"), [str(REPO / "lab"), "vm", "profile", "ssh", PROFILE_NAME], env)
    output = bytearray()
    deadline = time.monotonic() + timeout
    sent_exit = False
    status = None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], min(1.0, deadline - time.monotonic()))
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    output.extend(chunk)
                    if len(output) > 32768:
                        del output[:-16384]
                    if not sent_exit and (b"$ " in output or b"# " in output):
                        os.write(master, b"exit\n")
                        sent_exit = True
                elif status is None:
                    waited, raw_status = os.waitpid(pid, os.WNOHANG)
                    if waited == pid:
                        status = raw_status
                        break
            if not sent_exit and time.monotonic() + 5 >= deadline:
                os.write(master, b"exit\n")
                sent_exit = True
            waited, raw_status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                status = raw_status
                break
        if status is None:
            os.killpg(pid, signal.SIGTERM)
            try:
                _, status = os.waitpid(pid, 0)
            except ChildProcessError:
                status = None
            raise RuntimeError(f"public profile SSH session exceeded {timeout}s: "
                               f"{output.decode(errors='replace')[-2000:]}")
        exit_code = os.waitstatus_to_exitcode(status)
        require(exit_code == 0, "public profile SSH command failed: "
                + output.decode(errors="replace")[-2000:])
        require(sent_exit, "public profile SSH did not reach an interactive shell prompt")
    finally:
        try:
            os.close(master)
        except OSError:
            pass


def cleanup_profile(owner_uid: int, sha: str) -> None:
    instance = CONTROL._instance(PROFILE_NAME, owner_uid, sha)
    unit_file = pathlib.Path("/etc/systemd/system") / instance.vm_unit
    proxy_file = pathlib.Path("/etc/systemd/system") / instance.proxy_unit
    for path in (unit_file, proxy_file):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != 0 or info.st_gid != 0
                or stat.S_IMODE(info.st_mode) != 0o644 or info.st_nlink != 1):
            raise RuntimeError(f"preserving changed profile service unit during cleanup: {path}")
    if not unit_file.exists() and not proxy_file.exists():
        print("profile_cleanup=PASS no_profile_unit=true")
        return
    if unit_file.exists():
        run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", instance.vm_unit],
            timeout=60, check=False)
    if proxy_file.exists():
        run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", instance.proxy_unit],
            timeout=30, check=False)
    for unit in (instance.vm_unit, instance.proxy_unit):
        result = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "show",
                      "--property=ActiveState", "--value", unit], check=False, timeout=10)
        require(result.returncode == 0 and result.stdout.strip() in ("inactive", "failed"),
                f"named profile service remains active after hosted cleanup: {unit}")
    print("profile_cleanup=PASS stopped=true persistent_profile_state=retained")


def qualification() -> None:
    assert_kvm()
    sha = os.environ.get("GITHUB_SHA", "")
    require(SHA_RE.fullmatch(sha) is not None, "GITHUB_SHA must be a complete lowercase commit SHA")
    owner_uid = os.getuid()
    owner = pwd.getpwuid(owner_uid)
    candidate = CI_INSTALL_PARENT / sha
    instance = CONTROL._instance(PROFILE_NAME, owner_uid, sha)

    paths = (
        TRUST_KEY, OWNER_RECORD, CONTROL_HELPER, CONTROL_SUDOERS,
        pathlib.Path("/etc/little-mester-vm/profiles"),
        pathlib.Path("/var/lib/little-mester-vm/profiles"),
        pathlib.Path("/run/little-mester-vm-profiles"),
        pathlib.Path("/etc/systemd/system") / instance.vm_unit,
        pathlib.Path("/etc/systemd/system") / instance.proxy_unit,
        pathlib.Path("/var/lib/little-mester-vm/bases") / "04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94",
    )
    for path in paths:
        assert_absent(path)
    assert_absent(candidate)
    for username in (instance.vm_user, instance.proxy_user):
        try:
            pwd.getpwnam(username)
        except KeyError:
            pass
        else:
            raise RuntimeError(f"preserving pre-existing disposable service account: {username}")
        try:
            __import__("grp").getgrnam(username)
        except KeyError:
            pass
        else:
            raise RuntimeError(f"preserving pre-existing disposable service group: {username}")

    run([str(REPO / "tools/vm-owner-acceptance"), "--install",
         "--sha", sha, "--prefix", str(CI_INSTALL_PREFIX),
         "--repo", str(REPO)], timeout=180)
    require(candidate.is_dir(), "owner candidate installer did not publish the pinned runtime")
    print(f"profile_candidate_install=PASS sha={sha} owner={owner.pw_name}")

    # Enroll the owner and install fixed host prerequisites through the same
    # production installer used by the singleton acceptance path. This job has
    # its own fresh runner, so it cannot rely on the default-instance job.
    run(["/usr/bin/sudo", "-n", str(candidate / "tools/vm-system-install")], timeout=300)
    print("profile_host_enrollment=PASS installer=production-vm-system-install")

    started = False
    with tempfile.TemporaryDirectory(prefix="lm-profile-signing-",
                                     dir=os.environ.get("RUNNER_TEMP")) as raw_temp:
        temp = pathlib.Path(raw_temp)
        _, public_key, bundle, signature = create_signed_bundle(candidate, temp)
        run(["/usr/bin/sudo", "-n", "/usr/bin/install", "-o", "root", "-g", "root",
             "-m", "0644", str(public_key), str(TRUST_KEY)], timeout=15)
        run(["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I",
             str(candidate / "tools/vm-system-install"), "--install-profile-control",
             "--bundle", str(bundle), "--signature", str(signature)], timeout=90)
        require(CONTROL_HELPER.is_file() and CONTROL_SUDOERS.is_file(),
                "signed helper installation did not publish its exact control files")
        print("profile_control_install=PASS signature=ephemeral-ed25519 key=runner-only")

        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": owner.pw_dir,
               "LANG": "C", "LC_ALL": "C", "TERM": "xterm-256color"}
        run([str(REPO / "lab"), "vm", "profile", "install", PROFILE_NAME,
             "--prefix", str(CI_INSTALL_PREFIX)],
            timeout=600, env=env)
        mapping_path = pathlib.Path("/etc/little-mester-vm/profiles") / PROFILE_NAME / "profile.json"
        mapping = json.loads(root_read_file(mapping_path, limit=1024 * 1024))
        require(mapping.get("state") == "ready" and mapping.get("transaction") is None
                and mapping.get("runtime", {}).get("sha") == sha
                and mapping.get("provisioning", {}).get("artifacts", {}).get("disk", {}).get("virtual_size")
                == 64 * 1024**3,
                "named profile did not publish a ready mapping pinned to the exact candidate")
        print("profile_install=PASS mapping=v2 state=ready sparse_overlay=64GiB")

        global PROFILE_STARTED
        PROFILE_STARTED = True
        run([str(REPO / "lab"), "vm", "profile", "up", PROFILE_NAME], timeout=1050, env=env)
        run([str(REPO / "lab"), "vm", "profile", "status", PROFILE_NAME], timeout=30, env=env)
        pty_ssh(env)
        print("profile_ssh=PASS strict_host_key_pinned_interactive_session=true")
        run([str(REPO / "lab"), "vm", "profile", "down", PROFILE_NAME], timeout=90, env=env)
        PROFILE_STARTED = False

        states = []
        for unit in (instance.vm_unit, instance.proxy_unit):
            result = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "show",
                          "--property=ActiveState", "--value", unit], timeout=10)
            states.append(result.stdout.strip())
        require(all(state in ("inactive", "failed") for state in states),
                f"profile services did not stop cleanly: {states}")
        persistent_files = (instance.disk, instance.seed, mapping_path)
        for path in persistent_files:
            info = root_lstat(path)
            require(info is not None and stat.S_ISREG(info["mode"]),
                    f"profile down removed or changed persistent file: {path.name}")
        require(root_lstat(instance.proxy_port_file) is None,
                "profile proxy port marker remained after down")
        print("profile_down=PASS systemd_stopped=true persistent_disk_and_seed=retained")

    print("named_profile_kvm_qualification=PASS path=signed-install/install/up/ssh/down kvm=real")


def main() -> int:
    sha = os.environ.get("GITHUB_SHA", "")
    if not SHA_RE.fullmatch(sha):
        print("profile qualification requires GITHUB_SHA", file=sys.stderr)
        return 2
    if sys.argv[1:] == ["--cleanup"]:
        try:
            cleanup_profile(os.getuid(), sha)
            return 0
        except BaseException as error:  # noqa: BLE001 - cleanup is an explicit hosted gate
            print(f"profile_cleanup=FAIL error={type(error).__name__}: {error}", file=sys.stderr)
            return 1
    if sys.argv[1:]:
        print("unsupported hosted profile qualification argument", file=sys.stderr)
        return 2
    try:
        qualification()
        return 0
    except BaseException as error:  # noqa: BLE001 - retain evidence but stop a started guest
        print(f"named_profile_kvm_qualification=FAIL error={type(error).__name__}: {error}",
              file=sys.stderr)
        return 1
    finally:
        if PROFILE_STARTED:
            try:
                cleanup_profile(os.getuid(), sha)
            except BaseException as error:  # noqa: BLE001
                print(f"profile_cleanup=FAIL error={type(error).__name__}: {error}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
