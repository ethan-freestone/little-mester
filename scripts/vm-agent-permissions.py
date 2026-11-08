#!/usr/bin/env python3
"""Audit/apply the diagnosed guest-only OpenCode directory-mode correction."""

import argparse
import importlib.util
import os
import shlex
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("repair_helpers", ROOT / "scripts/vm-recover-prestart.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)

# No package reinstall, recursive chmod, credentials, runtime edits or services.
GUEST_PROGRAM = '''
import os,pwd,stat,sys
from pathlib import Path
stage = "initialization"
observed = ""
def require(condition):
    if not condition:
        raise ValueError
def metadata(info):
    return " mode=" + oct(stat.S_IMODE(info.st_mode)) + " uid=" + str(info.st_uid) + " links=" + str(info.st_nlink)
try:
    apply = sys.argv[1] in ("apply", "apply-home")
    repair_home = sys.argv[1] == "apply-home"
    base = Path("/usr/lib/node_modules/@opencode")
    directories = [base, base/"cli", base/"cli/bin"]
    executable = base/"cli/bin/opencode.exe"
    link = Path("/usr/bin/opencode")
    stage = "root-user"
    require(os.geteuid() == 0)
    stage = "bin-symlink"
    require(link.is_symlink() and os.readlink(link) == "../lib/node_modules/@opencode/cli/bin/opencode.exe")
    for index,directory in enumerate([Path("/usr"), Path("/usr/lib"), base.parent, *directories]):
        stage,observed = "directory-" + str(index) + "-stat", ""
        info = directory.lstat()
        observed = metadata(info)
        stage = "directory-" + str(index) + "-type-owner"
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0)
        stage = "directory-" + str(index) + "-canonical"
        require(directory.resolve(strict=True) == directory)
        stage = "directory-" + str(index) + "-mode"
        require(stat.S_IMODE(info.st_mode) in (0o700, 0o755))
    stage,observed = "executable-stat", ""
    info = executable.lstat()
    observed = metadata(info)
    stage = "executable-canonical"
    require(executable.resolve(strict=True) == executable)
    stage = "executable-type-owner"
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0)
    stage = "executable-mode"
    require(stat.S_IMODE(info.st_mode) == 0o755)
    # The packaged binary can be hardlinked. We never write/chmod that inode;
    # link count is not authority for changing only the three parent directories.
    home_parents = []
    if repair_home:
        stage,observed = "home-account", ""
        account = pwd.getpwnam("mester")
        require(account.pw_uid > 0)
        home = Path("/home/mester")
        home_info = home.lstat()
        require(stat.S_ISDIR(home_info.st_mode) and home_info.st_uid == account.pw_uid)
        require(home.resolve(strict=True) == home)
        home_parents = [home/".local", home/".local/share", home/".config"]
        for index,path in enumerate(home_parents):
            stage,observed = "home-parent-" + str(index), ""
            info = path.lstat()
            observed = metadata(info)
            require(stat.S_ISDIR(info.st_mode) and path.resolve(strict=True) == path)
            require(info.st_uid in (0,account.pw_uid))
            require(stat.S_IMODE(info.st_mode) in (0o700,0o755))
    if not apply:
        # Fixed-path metadata only: do not enumerate or read OpenCode/auth data.
        for label,relative in (("home", ""), ("local", ".local"),
                               ("share", ".local/share"), ("opencode", ".local/share/opencode"),
                               ("config", ".config"), ("cache", ".cache")):
            path = Path("/home/mester") / relative
            stage,observed = "home-" + label + "-stat", ""
            try:
                home_info = path.lstat()
            except FileNotFoundError:
                print("guest_home_" + label + "=absent")
                continue
            kind = "directory" if stat.S_ISDIR(home_info.st_mode) else "other"
            print("guest_home_" + label + "=" + kind + metadata(home_info))
    # Validate everything before the first chmod. Add no write permissions.
    if apply:
        for index,directory in enumerate(directories):
            stage,observed = "chmod-" + str(index), ""
            os.chmod(directory, 0o755, follow_symlinks=False)
        for index,path in enumerate(home_parents):
            stage,observed = "home-chown-" + str(index), ""
            os.chown(path,account.pw_uid,account.pw_gid,follow_symlinks=False)
        if repair_home:
            print("guest_home_parent_ownership=corrected")
    print("guest_opencode_directory_modes=" + ("corrected" if apply else "verified-shape"))
except (OSError, ValueError, RuntimeError, KeyError):
    print("REFUSED: guest installation check=" + stage + observed, file=sys.stderr)
    sys.exit(2)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--repair-home", action="store_true", help="with --apply, correct only the three diagnosed XDG parent owners")
    args = parser.parse_args()
    if args.repair_home and not args.apply:
        parser.error("--repair-home requires --apply")
    paths, process = helpers.paths, helpers.process
    paths.profile_name(args.profile)
    paths.profile_name(args.run_id)
    root = paths.state_root(args.state_root, must_exist=True)
    data = paths.profile_read(root, args.profile)
    helpers.require(data["status"] == "ready", "profile is not ready")
    profile = root / "vm/profiles" / args.profile
    run = profile / "runs" / args.run_id
    paths.private_entry(run)
    runtime = data["runtime_identity"]
    identity = {key: runtime[key] for key in ("pid", "start_ticks", "executable", "argv")}
    record = process.registry_read(run / "processes.json", run.name)
    roles = [role for role, item in record["processes"].items()
             if role.startswith("qemu_") and item["identity"] == identity]
    helpers.require(len(roles) == 1 and process.identity_state(identity) == "running",
                    "exact ready run identity is not running")
    helpers.require(process.listener_owned_by(identity["pid"], runtime["ssh_port"]),
                    "exact QEMU does not own the SSH listener")
    mode = "apply-home" if args.repair_home else ("apply" if args.apply else "audit")
    remote = "sudo -n python3 -c " + shlex.quote(GUEST_PROGRAM) + " " + mode
    if args.apply:
        # Execute as mester only after correction; --version never starts a TUI.
        remote += " && /usr/bin/opencode --version"
    command = ["bash", "-c", '''
set -euo pipefail
unset VM_SSH_CONTROL_PATH
source "$1/lib/vm/ssh.sh"
vm_ssh_listener_verified "$2/processes.json" "$3" "$4" "$6" || exit 2
vm_ssh_noninteractive "$5/control-key" "$2/known_hosts" "$6" 20 mester@127.0.0.1 "$7"
''', "_", str(ROOT), str(run), run.name, roles[0], str(profile), str(runtime["ssh_port"]), remote]
    env = {key: value for key, value in os.environ.items() if not key.startswith("VM_SSH_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(command, env=env, check=False, timeout=25).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print("REFUSED: guest permission correction: " + type(error).__name__, file=sys.stderr)
        sys.exit(2)
