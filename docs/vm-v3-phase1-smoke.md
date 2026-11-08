# VM v3 Phase 1 smoke and later cutover

## Phase 1 owner smoke

**Status:** NOT_RUN. Do not execute the owner procedure below until the final integrated candidate has clean tests, independent security review, passing owner-pushed GitHub Actions checks, and explicit owner authorization. This agent is UID 1000 with `CapEff=0`; no privileged install, hard-filesystem proof, credential delivery, or VM acceptance is claimed. This source tree is not an executable trust boundary.

- **Reviewed source SHA:** `PENDING — use the final integrated candidate SHA in the owner evidence after review; do not use baseline 60f3bd59ab3ca51756b1aee523b172cae61bb7d2 as the implementation SHA.`
- **Protected installation:** owner installs that reviewed source under a root-owned, non-group/world-writable directory outside the exported workspace, for example `/opt/little-mester/v3/<reviewed-sha>/`. `lab-vm`, `lib/vm/host-launcher.sh`, `lib/vm/cloud-init.sh`, `lib/vm/proxy.py`, `lib/vm/bounded-log.py`, and `lib/vm/executable-policy.py`, plus every lexical path component and ancestor directory, must be root-owned and not group/world writable; symlinks anywhere in those installation paths are refused. The launcher uses the validated lexical paths only after this check. Keep the source SHA and installed path in the evidence. Do not run from a mutable checkout or install/refresh host packages through the launcher.
- **Protected host dependencies:** use owner-installed root-owned system executables on the fixed system `PATH`: `qemu-system-x86_64`, `qemu-img`, `virtiofsd`, `curl`, `cat`, `mv`, `ssh`, `ssh-keygen`, `ssh-keyscan`, `socat`, `python3`, `timeout`, `sha256sum`, `date`, `mktemp`, `mkdir`, `chmod`, `stat`, `id`, `grep`, `rm`, `sleep`, `readlink`, and either `genisoimage` or `mkisofs`. `cat` renders the cloud-init seed and `mv` promotes the validated SSH host key; both are checked by the executable policy before lifecycle work. KVM must be available at `/dev/kvm`; no TCG fallback.
- **Workspace storage prerequisite:** the selected workspace must be the root of a dedicated, owner-provisioned filesystem whose reported capacity is at most 8 GiB. A normal directory, bind mount exposing a larger filesystem, or user quota on a larger filesystem is not accepted. The launcher neither provisions storage nor changes host mounts. Its mount-root/`statvfs` check is only preflight; the hard write limit during the run is enforced by the owner-provisioned filesystem itself. Before acceptance, use a separate small disposable filesystem to demonstrate that writes fail with `ENOSPC` at its configured limit. Never run a capacity-filling probe on a shared host filesystem. Keep this proof separate from the 1-MiB diagnostic-log cap, which bounds helper logs but does not cap workspace writes.
- **Resource and deadline bounds:** QEMU is limited to 2 vCPUs, 4096 MiB RAM and a 24-GiB virtual guest disk. The pinned image download has a 300-second per-attempt cap (two retries); guest boot/readiness and provider response each have a 300-second cap; Docker pull/container execution each have a 180-second cap; other SSH control commands default to 30 seconds. The entire smoke has a 1200-second hard timeout, followed by a 15-second forced-termination grace. QEMU, virtiofsd and proxy helper logs are capped while receiving at 1 MiB each and retained in at most eight private run directories. Guest SSH output is separately capped at 64 KiB per command (host-key scan output at 16 KiB); overflow terminates the owned process group and fails the stage. The bounded logger pins the process-group identity, gives TERM-ignoring descendants an 8-second grace before KILL, reaps adopted descendants, and allows a 2-second post-KILL settle even if the direct child exits and descendants close both output streams. The supervisor waits up to 11 seconds after TERM and allows at most 2 seconds after its own KILL escalation; it requires a logger completion proof and verifies the logger's session is empty before reporting cleanup success. The 13-second worst-case supervisor budget stays within the outer 15-second forced-termination grace. Redirecting to a normal file then truncating after exit would permit unbounded disk growth during the run. All these helper/guest-output bounds are distinct from the externally enforced workspace filesystem limit.
- **Inputs and credentials:** select one fresh, disposable workspace on that bounded filesystem, outside the installed tree and private runtime state. Create a private mode-0600, invoking-user-owned, single-link JSON credential file outside both the installation and workspace, with exactly `{"provider":"openai","model":"openai/<model-id>","api_key":"<owner-supplied-key>"}`. Use only the owner's current, intentionally supplied OpenAI API key; never copy tokens or auth state from the old VM. Do not put credential values in shell arguments, workspace files, logs, tickets, seeds, images, or Git. The host reads the key from that file and writes it to SSH stdin; it is not an argv value or environment variable. SSH uses `-tt` to provide the pinned OpenCode 2.2.5 interactive API-key prompt, running as guest user `mester` with a clean environment: `opencode auth login openai --method key`. The [OpenCode v2 provider CLI docs](https://opencode.ai/v2/docs/cli/providers) specify this invocation; pinned v2.2.5 CLI help confirms `--method` is supported. The guest creates `/home/mester/.local/share/opencode` as mode 0700 and the database is set/checked as mode 0600, restricting guest OS access to `mester` (and guest root). The invoking host user can access the private per-run disk image while the VM is active; the image is deleted only after successful run cleanup, while ambiguous cleanup retains private run state for owner review. The key is absent from the workspace, repository, Git, diagnostic logs, cloud-init seed, downloaded base image and reusable artifacts. Phase 1 uses this owner-provided API-key file only; browser login is a later, separate cutover checklist item.

### Source-backed boundary checks

- QEMU uses `memory-backend-memfd` with `share=on` for the 4096-MiB guest RAM and connects it through `-numa node,memdev=mem`. This provides the shared guest RAM required by the vhost-user virtiofs device; it does not add another host export. The only virtiofs server receives the selected workspace as `--shared-dir`, and the guest mounts only the `workspace` tag at `/workspace`.
- The controlled host boundary starts with `#!/usr/bin/bash -p` (ignore imported Bash functions and startup files), then uses `/usr/bin/env -i` with fixed `PATH`, `HOME=/nonexistent`, and `LC_ALL=C` before bootstrap or host work. Startup necessarily trusts the owner-managed OS bootstrap executables `/usr/bin/bash`, `/usr/bin/env`, and `/usr/bin/python3`; the fixed-path Python check then verifies those paths and `/usr/bin/timeout` (including lexical/resolved components and ancestors) are root-owned and not group/world writable before timeout or lifecycle use. These are bootstrap assumptions, not dependencies installed by the launcher. The installation-path check rejects lexical symlinks before resolving/loading dependencies. The hostile-config regression is `bats --filter 'mutable checkout refuses before host execution under forged identity and workspace config' tests/vm-host-launcher.bats`: it supplies hostile `.little-mester.conf`, `BASH_ENV`, an exported `python3` function, forged authority variables, and a marker-writing payload; the mutable checkout is refused before the marker can run. The separate `intermediate installation symlink is refused before resolving its target` regression uses a temp protected-shape fixture that links an installation ancestor to the source tree and asserts the symlink refusal occurs before the root-owner check. This is a lexical-path regression, not a successful root-owned install or owner VM test.
- `vm_host_require_commands` routes all ordinary host commands and the ISO tool through `executable-policy.py`, which checks lexical ancestors and every symlink-target component while permitting protected system links such as `/usr/bin/python3`. Its host-side inventory excludes guest commands embedded in SSH/cloud-init scripts and Bash builtins; the `cat` and `mv` omissions are covered by `host command preflight rejects untrusted mv and cat before lifecycle stages`. `python3 -I tests/vm-executable-policy.test.py` covers protected temporary executables, writable-path rejection, and the case where resolved-target-only validation would accept a command reached through `/tmp`. The supervisor regressions in `tests/vm-host-launcher.bats` exercise EXIT cleanup against a TERM-ignoring descendant, preserve an unrelated process, and refuse the success marker when logger completion proof is absent.
- Host curl starts with `-q` to suppress curlrc, and every SSH call uses `-F /dev/null`, explicit identity/known-host files, `IdentityAgent=none`, `ProxyCommand=none`, and `ProxyJump=none`. The credential pipe is `vm_host_credential_key_to_stdout FILE | vm_host_ssh_bounded RUN_DIR - '... opencode auth login openai --method key ...' 60 1 1`: the SSH `-tt` prompt receives the key only on stdin, while the bounded logger counts/discards prompt output and fails if its 64-KiB cap is exceeded. SSH runs as guest user `mester` with `env -i`; `XDG_DATA_HOME` and `XDG_CONFIG_HOME` are explicitly scoped to `/home/mester`. No credential-bearing command/argv/environment, shared host auth directory, or guest workspace path is used.

### Owner-only execution procedure

This procedure is an owner handoff, not authorization for this agent to run host commands. The candidate SHA below must remain pending until the Track A fix is integrated and the final candidate commit has passed the gate above. The owner performs every host command. Do not install from or execute a mutable checkout; do not run `lab-vm` through `sudo`; do not install host packages, change VM lifecycle state, or replace the old VM.

#### Pin and positively verify the protected installation

In one unprivileged owner Bash shell, keep the variables below for all procedure blocks, replace `REPO` with the owner's repository clone, and replace the SHA only after the final integrated commit exists and its owner-pushed CI checks pass. The archive includes only the trusted CLI and its required runtime files; no source is executed from the mutable checkout.

```bash
set -euo pipefail
REPO='/path/to/owner/repository-clone'
CANDIDATE_SHA='<PENDING-final-integrated-commit-SHA>'
[[ "$CANDIDATE_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$(git -C "$REPO" rev-parse "$CANDIDATE_SHA^{commit}")" == "$CANDIDATE_SHA" ]]
INSTALL_ROOT="/opt/little-mester/v3/$CANDIDATE_SHA"
sudo /usr/bin/python3 -I - "$CANDIDATE_SHA" <<'PY'
import os, re, stat, sys

def refuse(message):
    raise SystemExit(f"REFUSED: {message}")

def open_protected_dir(parent_fd, component, expected_uid, create_missing):
    if component in (".", "..") or not re.fullmatch(r"[A-Za-z0-9._-]+", component):
        refuse("unsafe installation path component")
    created = False
    try:
        info = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if not create_missing:
            refuse("required installation ancestor is missing")
        os.mkdir(component, 0o755, dir_fd=parent_fd)
        created = True
        info = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != expected_uid
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or (not created and not info.st_mode & stat.S_IXOTH)):
        refuse(f"installation ancestor is not protected/traversable: {component}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    child_fd = os.open(component, flags, dir_fd=parent_fd)
    opened = os.fstat(child_fd)
    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
        os.close(child_fd)
        refuse(f"installation ancestor changed while opening: {component}")
    if created:
        os.fchmod(child_fd, 0o755)
        opened = os.fstat(child_fd)
    if (not stat.S_ISDIR(opened.st_mode) or opened.st_uid != expected_uid
            or opened.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or not opened.st_mode & stat.S_IXOTH):
        os.close(child_fd)
        refuse(f"installation ancestor failed protected-mode check: {component}")
    return child_fd

def create_install_root(parent_fd, sha, expected_uid=0):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        refuse("candidate must be a full lowercase commit SHA")
    try:
        os.stat(sha, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        refuse("candidate install path already exists; refusing to overwrite")
    return open_protected_dir(parent_fd, sha, expected_uid, create_missing=True)

sha = sys.argv[1]
if os.geteuid() != 0:
    refuse("protected installation preparation requires the owner-authorized root command")
root_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
root_info = os.fstat(root_fd)
if (root_info.st_uid != 0 or root_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not root_info.st_mode & stat.S_IXOTH):
    os.close(root_fd)
    refuse("filesystem root is not protected/traversable")
parent_fd = root_fd
try:
    for component in ("opt", "little-mester", "v3"):
        next_fd = open_protected_dir(parent_fd, component, 0, create_missing=True)
        if parent_fd != root_fd:
            os.close(parent_fd)
        parent_fd = next_fd
    install_fd = create_install_root(parent_fd, sha)
    os.close(install_fd)
finally:
    if parent_fd != root_fd:
        os.close(parent_fd)
    os.close(root_fd)
PY
git -C "$REPO" archive --format=tar "$CANDIDATE_SHA" \
  lab-vm \
  lib/vm/host-launcher.sh \
  lib/vm/cloud-init.sh \
  lib/vm/proxy.py \
  lib/vm/bounded-log.py \
  lib/vm/executable-policy.py \
  | sudo tar --no-same-owner --same-permissions --keep-old-files -xf - -C "$INSTALL_ROOT"
test -x "$INSTALL_ROOT/lab-vm"
for file in lib/vm/host-launcher.sh lib/vm/cloud-init.sh lib/vm/proxy.py lib/vm/bounded-log.py lib/vm/executable-policy.py; do
  test -f "$INSTALL_ROOT/$file"
done
namei -l "$INSTALL_ROOT/lab-vm" "$INSTALL_ROOT/lib/vm/host-launcher.sh" \
  "$INSTALL_ROOT/lib/vm/cloud-init.sh" "$INSTALL_ROOT/lib/vm/proxy.py" \
  "$INSTALL_ROOT/lib/vm/bounded-log.py" "$INSTALL_ROOT/lib/vm/executable-policy.py"
```

The dirfd walk checks every existing lexical component before creating its child, refuses symlinks or user/group/world-writable ancestors, creates only missing root-owned/traversable directories, and refuses any pre-existing candidate install directory. Archive extraction is reached only afterward and `tar --keep-old-files` also refuses overwrites. If extraction fails, do not execute or retry into the partial tree; stop for owner review. Stop unless `namei` shows no symlink in any path and every file, directory, and ancestor is root-owned and not group/world writable. The launcher's own protected-install check repeats this invariant before loading code. No `cp` from the mutable checkout, symlinked install path, package install, or runtime-path override is allowed.

Before allocating the workspace or starting QEMU, prove the owner-installed runtime commands resolve through the fixed system `PATH` and pass the installed executable policy. This is read-only; if any command is absent or refused, stop and have the owner provision it through the normal host process (never through this launcher).

```bash
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
for command in qemu-system-x86_64 qemu-img virtiofsd curl cat mv ssh ssh-keygen ssh-keyscan socat python3 timeout sha256sum date mktemp mkdir chmod stat id grep rm sleep readlink; do
  executable="$(command -v "$command")"
  /usr/bin/python3 -I "$INSTALL_ROOT/lib/vm/executable-policy.py" "$executable"
  printf '%s\t%s\n' "$command" "$executable"
done
iso_tool="$(command -v genisoimage || command -v mkisofs)"
/usr/bin/python3 -I "$INSTALL_ROOT/lib/vm/executable-policy.py" "$iso_tool"
printf 'ISO tool\t%s\n' "$iso_tool"
test -c /dev/kvm && test -r /dev/kvm && test -w /dev/kvm
```

The fixed bootstrap paths `/usr/bin/bash`, `/usr/bin/env`, `/usr/bin/python3`, and `/usr/bin/timeout` must also be owner-managed system executables. `lab-vm` verifies their protected paths before lifecycle work. The runtime preflight above records the resolved executable paths and exercises the same lexical/symlink-target policy used by the smoke; it does not prove a VM run.

#### Provision and prove the bounded workspace

Use a new, otherwise unused mountpoint outside the repository, installation, and private state. The mount itself is the selected workspace (not a directory on a shared filesystem). Ensure no guest or other process can write to this fresh, mode-0700 mount during the proof or smoke. The only capacity-filling operation below targets this verified 64-MiB tmpfs; never run it on `/`, a home directory, the repository, a bind mount, or any shared filesystem.

```bash
WORKSPACE="/mnt/little-mester-v3-smoke-$(id -u)"
test ! -e "$WORKSPACE" && test ! -L "$WORKSPACE"
sudo install -d -o root -g root -m 0700 "$WORKSPACE"
sudo mount -t tmpfs -o "size=64m,nosuid,nodev,noexec,mode=0700,uid=$(id -u),gid=$(id -g)" tmpfs "$WORKSPACE"
findmnt --noheadings --output SOURCE,TARGET,FSTYPE,SIZE,OPTIONS --target "$WORKSPACE"
stat -c '%u:%g:%a %n' "$WORKSPACE"
```

Before the proof, confirm the `findmnt` row has this exact target, `tmpfs`, a 64-MiB size, and `nosuid,nodev,noexec,mode=700` plus the invoking UID/GID; `stat` must show the invoking UID/GID and mode `700`. Then run the bounded proof. It refuses unless the exact path is a canonical tmpfs mount root with those options, an empty owner-private directory, and `statvfs` capacity no greater than 64 MiB. It creates one exclusive no-follow file and offers at most 64 MiB of chunk writes plus exactly one 1-byte boundary write (64 MiB + 1 attempted bytes), requires an observed `errno.ENOSPC`, and removes the probe in `finally`. The unprivileged mocked regression below tests only the byte bound; it does not replace this real owner-only proof. Do not proceed if it refuses or fails to observe ENOSPC; do not retry on another path.

```bash
/usr/bin/python3 -I - "$WORKSPACE" <<'PY'
import errno, os, pathlib, re, stat, sys

limit = 64 * 1024 * 1024
raw = pathlib.Path(sys.argv[1])
root = raw.resolve(strict=True)
def fail(message):
    raise SystemExit(f"REFUSED: {message}")

if raw != root or not os.path.ismount(root):
    fail("workspace is not the canonical mount root")
info = os.lstat(root)
if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
        or info.st_gid != os.getgid() or stat.S_IMODE(info.st_mode) != 0o700):
    fail("workspace mount root is not invoking-user-owned mode 0700")
matches = []
for line in pathlib.Path("/proc/self/mountinfo").read_text().splitlines():
    fields = line.split()
    separator = fields.index("-")
    if fields[4] == str(root):
        matches.append((fields, separator))
if len(matches) != 1:
    fail("workspace does not have exactly one mountinfo entry")
fields, separator = matches[0]
if fields[separator + 1] != "tmpfs":
    fail("workspace filesystem is not tmpfs")
options = set(fields[5].split(",")) | set(fields[separator + 3].split(","))
required = {"nosuid", "nodev", "noexec", "mode=700", f"uid={os.getuid()}", f"gid={os.getgid()}"}
if not required.issubset(options):
    fail("tmpfs lacks required isolation, ownership, or mode options")
size_option = next((item[5:] for item in options if item.startswith("size=")), "")
match = re.fullmatch(r"([0-9]+)([kKmMgG]?)", size_option)
if not match:
    fail("could not verify tmpfs size option")
scale = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[match.group(2).lower()]
if int(match.group(1)) * scale != limit:
    fail("tmpfs size option is not exactly 64 MiB")
usage = os.statvfs(root)
if not 0 < usage.f_blocks * usage.f_frsize <= limit:
    fail("filesystem capacity exceeds the 64-MiB hard bound")
def is_empty(path):
    with os.scandir(path) as entries:
        return next(entries, None) is None

def write_until_enospc(fd, byte_limit, writer=None):
    writer = os.write if writer is None else writer
    attempted = written = 0
    confirmed = False
    while attempted < byte_limit:
        requested = min(1024 * 1024, byte_limit - attempted)
        attempted += requested
        try:
            amount = writer(fd, b"\0" * requested)
        except OSError as error:
            if error.errno != errno.ENOSPC:
                raise
            confirmed = True
            break
        if not 0 < amount <= requested:
            fail("bounded write returned an invalid byte count")
        written += amount
    if not confirmed:
        attempted += 1
        try:
            amount = writer(fd, b"\0")
        except OSError as error:
            if error.errno != errno.ENOSPC:
                raise
            confirmed = True
        else:
            if amount != 1:
                fail("one-byte ENOSPC boundary write returned an invalid count")
            written += amount
    if not confirmed:
        fail("64-MiB-plus-one-byte write cap reached without observing ENOSPC")
    return written, attempted

if not is_empty(root):
    fail("fresh workspace is not empty")

probe = root / "enospc-proof.bin"
fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
try:
    written, attempted = write_until_enospc(fd, limit)
    print(f"ENOSPC_CONFIRMED after {written} bytes; {attempted} bytes attempted at most")
finally:
    os.close(fd)
    probe.unlink()
if not is_empty(root):
    fail("ENOSPC probe did not leave the fresh workspace empty")
PY
```

#### Create the private credential and run the smoke

Use the owner's current API key, never an old-VM token. The model is read from an explicitly opened controlling `/dev/tty`; the key prompt requires `getpass` on that TTY, treats `GetPassWarning` as fatal, and never falls back to the heredoc/script stdin. Both prompts fail closed if a usable TTY is absent. The key is neither a command argument nor environment value. The file is created exclusively at mode 0600 under the invoking user's verified mode-0700 `/run/user/<uid>`, outside the `/mnt` workspace, `/opt` install, repository, and `~/.local/state` runtime-state directory. The adjacent evidence log contains smoke output only, never credential contents.

```bash
PRIVATE_DIR="$(readlink -f -- "/run/user/$(id -u)")"
test "$(stat -c '%u:%a' "$PRIVATE_DIR")" = "$(id -u):700"
REPO_ROOT="$(readlink -f -- "$(git -C "$REPO" rev-parse --show-toplevel)")"
case "$REPO_ROOT/" in "$PRIVATE_DIR/"*) printf 'STOP: private credential directory overlaps the repository\n' >&2; exit 1;; esac
case "$PRIVATE_DIR/" in "$REPO_ROOT/"*) printf 'STOP: private credential directory overlaps the repository\n' >&2; exit 1;; esac
CREDENTIALS="$PRIVATE_DIR/v3-smoke-openai-$CANDIDATE_SHA.json"
/usr/bin/python3 -I - "$CREDENTIALS" <<'PY'
import getpass, json, os, pathlib, sys, warnings

def read_owner_credentials(opener=open, password_reader=getpass.getpass, pass_warning=getpass.GetPassWarning):
    try:
        tty = opener("/dev/tty", "r+", encoding="utf-8")
    except OSError as error:
        raise SystemExit("REFUSED: an interactive controlling TTY is required") from error
    try:
        if not tty.isatty():
            raise SystemExit("REFUSED: /dev/tty is not a usable terminal")
        tty.write("OpenAI model ID (openai/<model-id>): ")
        tty.flush()
        model = tty.readline().rstrip("\r\n")
        if not model:
            raise SystemExit("REFUSED: model ID is required")
        with warnings.catch_warnings():
            warnings.simplefilter("error", pass_warning)
            try:
                key = password_reader("Current owner-supplied OpenAI API key (input hidden): ", stream=tty)
            except pass_warning as error:
                raise SystemExit("REFUSED: getpass could not guarantee hidden TTY input") from error
        return model, key
    finally:
        tty.close()

model, key = read_owner_credentials()
path = pathlib.Path(sys.argv[1])
if path.exists() or path.is_symlink():
    raise SystemExit("REFUSED: credential path already exists; choose a fresh private path")
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
os.fchmod(fd, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    json.dump({"provider": "openai", "model": model, "api_key": key}, stream)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
PY
stat -c '%u:%g:%a %n' "$CREDENTIALS"
```

The `stat` line must show the invoking owner and `600`; do not display or copy the JSON contents. Create a new, mode-0600 evidence log outside the workspace and runtime state, then invoke only the protected installation as the unprivileged owner:

```bash
EVIDENCE="$PRIVATE_DIR/v3-smoke-$CANDIDATE_SHA.log"
umask 077
set -o noclobber
exec {evidence_fd}> "$EVIDENCE"
test "$(stat -c %a "$EVIDENCE")" = 600
if "$INSTALL_ROOT/lab-vm" smoke --workspace "$WORKSPACE" --credentials-file "$CREDENTIALS" >&"$evidence_fd" 2>&1; then
  smoke_status=0
else
  smoke_status=$?
fi
exec {evidence_fd}>&-
printf 'smoke exit status: %s\n' "$smoke_status"
cat "$EVIDENCE"
test "$smoke_status" -eq 0
```

A pass requires exit 0, all four markers from the same fresh guest (`ROOTLESS_DOCKER_OK`, `VM_UP`, `VM_RESPONSE_OK`, `DOCKER_CONTAINER_OK`), and the final `Smoke acceptance stages completed.` line. `ROOTLESS_DOCKER_OK` is emitted only after the guest reports rootless Docker on the per-user socket and no rootful socket; `DOCKER_CONTAINER_OK` follows an actually executed container. Record the exact candidate SHA, installed path, protected executable paths, command, exit status, and redacted output. Do not publish the evidence log or raw provider/auth diagnostics without reviewing it for sensitive data.

#### Verify cleanup; preserve rollback

Copy the exact `Run state:` path from the private evidence log into `RUN_DIR`. Successful cleanup must have emitted the final completion line only after exact-PID cleanup and bounded-logger process-group proof. Inspect the run directory (owner UID, mode 0700) and confirm it contains only the private bounded `qemu.log`, `virtiofsd.log`, and `proxy.log` (each mode 0600 and at most 1 MiB); the disk image, download, seed, metadata, SSH keys, known-host files, process identity/completion files, sockets, and command records must be absent. Check that no same-owner process command line contains this unique run path; any match, ambiguous status, unexpected artifact, or missing completion marker is a STOP condition. Do not kill an unrelated PID, force/lazy-unmount, or delete ambiguous state.

```bash
RUN_DIR='/copy/the/Run state/path/from/the/private/evidence/log'
test "$(stat -c '%u:%a' "$RUN_DIR")" = "$(id -u):700"
find "$RUN_DIR" -mindepth 1 -maxdepth 1 -printf '%f %m %s bytes\n' | sort
/usr/bin/python3 -I - "$RUN_DIR" <<'PY'
import os, pathlib, sys

needle = os.fsencode(sys.argv[1].rstrip("/") + "/")
own_uid = os.getuid()
matches = []
with os.scandir("/proc") as processes:
    for entry in processes:
        if not entry.name.isdigit() or int(entry.name) in (os.getpid(), os.getppid()):
            continue
        process = pathlib.Path(entry.path)
        try:
            status = (process / "status").read_text()
            uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
            if int(uid_line.split()[1]) != own_uid:
                continue
            argv = (process / "cmdline").read_bytes()
        except FileNotFoundError:
            continue
        except (OSError, StopIteration, ValueError) as error:
            raise SystemExit(f"STOP: process ownership/argv could not be verified: {entry.name}") from error
        if needle in argv:
            matches.append(entry.name)
if matches:
    raise SystemExit("STOP: owner processes still reference run state: " + ",".join(matches))
print("No same-owner process command line references the run path.")
PY
find "$WORKSPACE" -mindepth 1 -maxdepth 1 -print
sudo umount -- "$WORKSPACE"
sudo rmdir -- "$WORKSPACE"
```

The workspace listing must be empty. The two final commands unmount only this exact tmpfs normally and remove its empty mountpoint; if unmount fails, stop for owner review (never use `umount -l` or `-f`). Preserve the bounded diagnostic logs until reviewed. The old VM and its credentials/runtime remain untouched; any failed gate means no cutover, no replacement, and no rollback action against the old VM. Remove the owner credential file only if the owner no longer needs it; ordinary unlink is not secure erasure, and any suspected exposure requires key revocation. Unit, Bats, proxy, CLI-help, and disposable fake-credential checks are not host acceptance.

## Owner-observed cutover checklist — after Phase 1 only

This is a separate operational gate, not a Phase 1 implementation or merge criterion. The owner alone decides whether to cut over, using a fresh v3 runtime at a recorded **committed, owner-pushed SHA** and preserving the old VM.

- [ ] Before browser login, record where auth state is stored, which process/user can read it, how the agent uses it, and the exact required public endpoints. Do not copy tokens from the old VM or broaden egress.
- [ ] Owner completes browser-based OpenAI login in the new VM and observes a genuine OpenAI response.
- [ ] Existing Little Mester MCP works; tickets and decisions are available.
- [ ] Sol-to-Luna delegation is bounded and works; repository and branch are correct.
- [ ] Rootless Docker works; useful logs are available; configuration/tickets are sufficient to reconstruct context after restart.
- [ ] Owner preserves the old VM until all observations pass. Any browser-auth, persistence, or endpoint gap is a future change request; do not silently expand Phase 1.
