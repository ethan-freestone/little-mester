# VM v3 Phase 1 smoke and later cutover

## Phase 1 owner smoke

**Status:** NOT_RUN. Do not execute the owner procedure below until the final integrated candidate has clean tests, independent security review, passing owner-pushed GitHub Actions checks, and explicit owner authorization. This agent is UID 1000 with `CapEff=0`; no privileged install, hard-filesystem proof, owner browser login, or VM acceptance is claimed. This source tree is not an executable trust boundary.

- **Latest reviewed source SHA:** `8ee5e856e10a94fc5bfe724174eb316cfe08c418` (independent Sol Medium review: PASS against `f5f96bf68d00ff0079a140d76fcb805d8192fd3c`). Subsequent documentation/ticket-only commits do not change this source tree; record the final candidate SHA separately. Do not use baseline `60f3bd59ab3ca51756b1aee523b172cae61bb7d2` as the implementation SHA.
- **Reusable owner runner:** `tools/vm-owner-acceptance` is the maintained, OS-independent entry point. Run it from the exact candidate checkout with a full commit SHA containing that same runner version; it refuses a SHA/runner mismatch. It uses the fixed system executable policy and protected `lab-vm` for dependency/workspace preflight, and the protected launcher for VM/browser-auth/process lifecycle. It never installs packages or automatically removes historical state. Reports are private JSON under `/run/user/<uid>/little-mester-vm-acceptance/<fresh-id>/report.json`; they contain statuses, paths, exit codes and redacted notes, never OAuth URLs or credentials. The separate detailed blocks below document the underlying controls and owner procedure; use the runner commands below instead of copying their implementation blocks.

```bash
REPO="$PWD"
SHA='<full-reviewed-candidate-commit-sha>'
WORKSPACE='/mnt/little-mester-v3-smoke-1000'

# Explicitly authorize creation of a new, immutable protected installation.
# This stage then runs the host/dependency preflight against that installed SHA.
# Prompts for sudo on the controlling terminal; refuses to overwrite any target.
tools/vm-owner-acceptance --install --sha "$SHA" --repo "$REPO"

# Optional read-only repeat of the host/KVM/dependency and workspace preflight.
# It is available only after the SHA-specific protected installation exists.
tools/vm-owner-acceptance --preflight --sha "$SHA" --repo "$REPO" --workspace "$WORKSPACE"

# Explicitly authorize the bounded 64-MiB ENOSPC proof on the pre-existing
# owner-provisioned tmpfs. The runner removes only the inode it created.
tools/vm-owner-acceptance --storage-proof --sha "$SHA" --repo "$REPO" --workspace "$WORKSPACE"

# Explicitly authorize the real smoke. Pinned native ChatGPT browser OAuth is
# initiated automatically; a controlling TTY is required only for portal fallback.
tools/vm-owner-acceptance --run --sha "$SHA" --repo "$REPO" --workspace "$WORKSPACE" --model openai/<model-id>

# Read-only verification for the fresh run ID reported by --run.
tools/vm-owner-acceptance --verify-cleanup --sha "$SHA" --repo "$REPO" --run-id '<reported-run-id>'

# Optional combined mutating path: protected install, preflight, bounded proof,
# then VM smoke. Use only with owner authorization for every stage.
tools/vm-owner-acceptance --all --sha "$SHA" --repo "$REPO" --workspace "$WORKSPACE" --model openai/<model-id>
```

Every invocation creates a new evidence directory. `--verify-cleanup` is read-only with respect to VM state and never signals processes or unlinks socket/PID files; it checks `/proc/net/unix` for a listener even if the socket path or per-run short socket directory has disappeared. Cleanup verification is accepted only for a uniquely reported successful run; an early startup failure remains BLOCKED/NOT_RUN, not cleanup PASS. Preserve failed run directories for owner review. The retained `virtiofs.socket-path` file binds the run to its private per-run socket directory under `/run/user/<uid>/lmv3-<run-id>/`; the socket and `.pid` evidence are in that volatile directory when present. PID-file contents are never process authority. A later attempt gets an exclusive collision-resistant run directory and short-path allocation and refuses pre-existing socket/PID paths.
- **Protected installation:** owner installs that reviewed source under a root-owned, non-group/world-writable directory outside the exported workspace, for example `/opt/little-mester/v3/<reviewed-sha>/`. `lab-vm`, `lib/vm/host-launcher.sh`, `lib/vm/cloud-init.sh`, `lib/vm/proxy.py`, `lib/vm/bounded-log.py`, `lib/vm/executable-policy.py`, `lib/vm/browser-auth-session.py`, `lib/vm/virtiofs-socket.py`, and `lib/vm/guest-user-dirs.py`, plus every lexical path component and ancestor directory, must be root-owned and not group/world writable; symlinks anywhere in those installation paths are refused. The launcher uses the validated lexical paths only after this check. Keep the source SHA and installed path in the evidence. Do not run from a mutable checkout or install/refresh host packages through the launcher.
- **Protected host dependencies:** use owner-installed root-owned system executables on the fixed system `PATH` for ordinary runtime commands: `qemu-system-x86_64`, `qemu-img`, `curl`, `cat`, `mv`, `ssh`, `ssh-keygen`, `ssh-keyscan`, `socat`, `python3`, `timeout`, `sha256sum`, `date`, `mktemp`, `mkdir`, `chmod`, `stat`, `id`, `grep`, `rm`, `sleep`, `readlink`, and either `genisoimage` or `mkisofs`. `virtiofsd` need not be on `PATH`: selection checks its `command -v` result there first, then `/usr/lib/virtiofsd`, `/usr/libexec/virtiofsd`, and `/usr/lib/qemu/virtiofsd` in that order; only a candidate accepted by the installed executable policy is usable. `cat` renders the cloud-init seed and `mv` promotes the validated SSH host key; both are checked by the executable policy before lifecycle work. KVM must be available at `/dev/kvm`; no TCG fallback.
- **Workspace storage prerequisite:** the selected workspace must be the root of a dedicated, owner-provisioned filesystem whose reported capacity is at most 8 GiB. A normal directory, bind mount exposing a larger filesystem, or user quota on a larger filesystem is not accepted. It must not overlap `/run/user/<uid>` (the protected private short-socket runtime root), the home-state directory, or the installed tree; this prevents exporting the socket/PID evidence to the guest. The launcher neither provisions storage nor changes host mounts. Its mount-root/`statvfs` check is only preflight; the hard write limit during the run is enforced by the owner-provisioned filesystem itself. Before acceptance, use a separate small disposable filesystem to demonstrate that writes fail with `ENOSPC` at its configured limit. Never run a capacity-filling probe on a shared host filesystem. Keep this proof separate from the 1-MiB diagnostic-log cap, which bounds helper logs but does not cap workspace writes.
- **Resource and deadline bounds:** QEMU is limited to 2 vCPUs, 4096 MiB RAM and a 24-GiB virtual guest disk. Pinned image download is capped at 300 seconds per attempt with two retries (at most 900 seconds); guest SSH/key readiness has its own 300-second cap. After SSH is ready, cloud-init provisioning and its completion-marker wait have a separate 600-second cap. A timeout (status 124) is reported as provisioning timeout; other cloud-init failure, SSH transport failure, missing completion marker/workspace mount, rootful socket, and rootless-Docker readiness failures have distinct sanitized diagnostics. The smoke has a hard 3600-second whole-run cap, including download, boot, provisioning, OAuth, model and Docker stages. Browser authentication is capped at 600 seconds and its allowance is reduced when needed to reserve the provider's 300 seconds, Docker pull/container's 180 seconds each, and six bounded SSH controls for auth setup/verification and Docker inspection. Across all phases the configured worst case is about 3360 seconds, leaving 240 seconds for lifecycle and cleanup overhead; both the inner stage accounting and outer `timeout` enforce the finite cap. Other SSH control commands default to 30 seconds. QEMU, virtiofsd and proxy helper logs are capped while receiving at 1 MiB each and retained in at most eight private run directories. Guest SSH output is separately capped at 64 KiB per command (host-key scan output at 16 KiB); overflow terminates the owned process group and fails the stage. The bounded logger pins the process-group identity, gives TERM-ignoring descendants an 8-second grace before KILL, reaps adopted descendants, and allows a 2-second post-KILL settle even if the direct child exits and descendants close both output streams. The supervisor waits up to 11 seconds after TERM and allows at most 2 seconds after its own KILL escalation; it requires a logger completion proof and verifies the logger's session is empty before reporting cleanup success. The 13-second worst-case supervisor budget stays within the outer 15-second forced-termination grace. Redirecting to a normal file then truncating after exit would permit unbounded disk growth during the run. All these helper/guest-output bounds are distinct from the externally enforced workspace filesystem limit.

- **Cloud-init users/groups schema:** the generated seed targets Ubuntu's pinned cloud-init 26.1 family. The `users_groups` schema accepts the host UID as an integer and `primary_group` as a string; it does not accept `{name, gid}` records under top-level `groups`, and an explicitly empty per-user `groups: []` violates its non-empty list constraint. The guest creates the user's same-name primary group with its normal system GID; only the UID needs to match the host for the mode-0700 workspace. No password login or root SSH is enabled. The regression is `bats --filter 'generated cloud-init uses cloud-init 26.1 users-groups schema for the host UID' tests/vm-host-launcher.bats`; it checks actual generator output against the documented 26.1 schema shape without requiring cloud-init tooling on the owner host.
- **Rootless user configuration provisioning:** before invoking the rootless Docker setup tool, cloud-init installs the protected `guest-user-dirs.py` helper into the guest and uses descriptor-relative, no-follow checks to prepare each `/home/mester` config directory individually. It validates the home owner/primary group and mode; only the known directory nodes may be normalized from root ownership, never recursively chowned. Symlinks, unexpected owners, writable ancestors, or a home-ownership mismatch fail with a diagnostic. The helper also creates the proxy unit and daemon JSON through verified directory descriptors using exclusive no-follow file creation, preserving the existing proxy values without following/replacing unexpected files. A bounded check requires the private `/run/user/<uid>` directory, owned bus socket, and successful `systemctl --user show-environment` (each query is capped at 2 seconds, with a 1-second kill grace, across at most 30 attempts); immediately before daemon reload/setup, the helper runs as `mester` and creates/removes private regular-file probes directly under `/home/mester` and `~/.config/systemd/user`. This is a source/tested provisioning guard, not evidence that a real guest successfully started Docker. The failed owner run's available console evidence did not establish `/home/mester` metadata; the intermediate-directory explanation is source-backed and reproduced in isolated fixtures, not asserted as observed guest `stat` output.
- **Inputs and authentication:** select one fresh, disposable workspace on that bounded filesystem, outside the installed tree and private runtime state. Select the model with `--model openai/<model-id>`; it is not a credential. The smoke invokes the pinned v2.2.5 supported command `opencode auth login openai --method chatgpt-browser`; it does not simulate `/connect` keystrokes, use API keys, or use device-code auth. No API key or API credits are used. The host accepts only `https://auth.openai.com/oauth/authorize` with the exact v2.2.5 query shape, client ID, S256 PKCE and one byte-exact callback URI, `http://localhost:1455/auth/callback` or `http://localhost:1457/auth/callback`. It holds the URL until an owned SSH master is verified and a forward explicitly listening on host `127.0.0.1:<port>` is ready; the guest destination is `localhost:<same-port>` because pinned OpenCode binds its callback server to `localhost`. If host `localhost` resolves to `::1`, the launcher reserves that exact IPv6 port with a non-listening socket before installing the IPv4 forward; this blocks a concurrent IPv6 listener without widening SSH's explicit `127.0.0.1` bind. Failure to reserve the IPv6 callback path fails closed. The host verifies all `localhost` resolutions are loopback and probes the IPv4 loopback listener. SSH disables agent forwarding, remote/dynamic forwarding, SSH configuration, proxy commands and proxy jumps. The validated URL is sent transiently through the invoking owner's `org.freedesktop.portal.OpenURI` desktop portal using the spec-compliant request path; it is not placed in process arguments, environment, logs, reports or persistent files by the launcher. A portal may hand the URL to a browser that retains it in browser history. If the portal is absent or declines, a controlling TTY is required to show the live-terminal manual URL fallback; without a TTY the flow fails closed without printing the URL. The normal interactive OpenCode TUI remains separate and unchanged; its guest-to-host `o` shortcut is deferred. OpenCode stores tokens in its guest SQLite database under `/home/mester/.local/share/opencode` (directory mode 0700, database mode 0600). That database is on the disposable per-run disk, which is destroyed after verified cleanup; persistence across VM restart is out of scope. Never copy auth state from the old VM. For manual logout before teardown, inspect `opencode auth list`, then use the supported `opencode auth logout openai <account-label>` command. Do not edit the SQLite database or automate logout.

- **Auth-store trust and revocation limits:** modes 0700/0600 protect the guest auth store from other unprivileged guest accounts, not from other processes running as `mester`, guest root, or the host owner while able to inspect the live disk. The disposable disk is destroyed after verified cleanup. `opencode auth logout openai <account-label>` removes a local credential; this smoke does not establish remote session revocation or secure erasure. If exposure is suspected, the owner must use OpenAI account security/revoke-sessions controls manually. Such account-wide revocation **may invalidate the working old-VM session**; preserve the old VM untouched but expect that it may need reauthentication. Never automate logout/revocation or access or alter the old VM. Persistence across VM restart remains out of scope and is a separate concern.

### Source-backed boundary checks

- QEMU uses `memory-backend-memfd` with `share=on` for the 4096-MiB guest RAM and connects it through `-numa node,memdev=mem`. This provides the shared guest RAM required by the vhost-user virtiofs device; it does not add another host export. The only virtiofs server receives the selected workspace as `--shared-dir`, and the guest mounts only the `workspace` tag at `/workspace`.
- The controlled host boundary starts with `#!/usr/bin/bash -p` (ignore imported Bash functions and startup files), then uses `/usr/bin/env -i` with fixed `PATH`, `HOME=/nonexistent`, and `LC_ALL=C` before bootstrap or host work. Startup necessarily trusts the owner-managed OS bootstrap executables `/usr/bin/bash`, `/usr/bin/env`, and `/usr/bin/python3`; the fixed-path Python check then verifies those paths and `/usr/bin/timeout` (including lexical/resolved components and ancestors) are root-owned and not group/world writable before timeout or lifecycle use. These are bootstrap assumptions, not dependencies installed by the launcher. The installation-path check rejects lexical symlinks before resolving/loading dependencies. The hostile-config regression is `bats --filter 'mutable checkout refuses before host execution under forged identity and workspace config' tests/vm-host-launcher.bats`: it supplies hostile `.little-mester.conf`, `BASH_ENV`, an exported `python3` function, forged authority variables, and a marker-writing payload; the mutable checkout is refused before the marker can run. The separate `intermediate installation symlink is refused before resolving its target` regression uses a temp protected-shape fixture that links an installation ancestor to the source tree and asserts the symlink refusal occurs before the root-owner check. This is a lexical-path regression, not a successful root-owned install or owner VM test.
- `vm_host_require_commands` routes all ordinary host commands and the ISO tool through `executable-policy.py`, which checks lexical ancestors and every symlink-target component while permitting protected system links such as `/usr/bin/python3`. Its host-side inventory excludes guest commands embedded in SSH/cloud-init scripts and Bash builtins; the `cat` and `mv` omissions are covered by `host command preflight rejects untrusted mv and cat before lifecycle stages`. `python3 -I tests/vm-executable-policy.test.py` covers protected temporary executables, writable-path rejection, and the case where resolved-target-only validation would accept a command reached through `/tmp`. The supervisor regressions in `tests/vm-host-launcher.bats` exercise EXIT cleanup against a TERM-ignoring descendant, preserve an unrelated process, and refuse the success marker when logger completion proof is absent.
- Host curl starts with `-q` to suppress curlrc, and every SSH call uses `-F /dev/null`, explicit identity/known-host files, `IdentityAgent=none`, `ProxyCommand=none`, and `ProxyJump=none`. The browser-auth smoke uses one authenticated SSH master with `ControlPersist=no`, `-T` (no PTY), no agent/X11 forwarding, and a separately controlled single local forward. It runs pinned OpenCode's supported noninteractive `auth login openai --method chatgpt-browser`; it is distinct from and does not replace the ordinary interactive TUI. `browser-auth-session.py` validates the exact v2.2.5 OAuth endpoint, required query parameters and fixed localhost callback variants. The host side of the forward is explicitly `127.0.0.1:<validated-port>`; the guest destination is `localhost:<same-port>` to match pinned provider binding. If the system resolves `localhost` to IPv6, the helper reserves `::1:<port>` with a non-listening socket before exposing the callback URL, preventing another process from binding the IPv6 callback while retaining SSH's explicit IPv4-only listener. It verifies localhost resolves only to loopback, probes the IPv4 listener, and verifies the owned SSH process before calling the desktop portal. The portal uses `OpenURI` with the D-Bus-defined sender-derived request path and subscribes before opening. A host listener collision, forwarding failure, callback race, portal refusal without a usable fallback, cancellation, timeout or unverifiable cleanup fails closed. SSH output is consumed under a byte bound and discarded rather than persisted; the OAuth URL is not written to process arguments, environment, logs, reports or files. A portal failure can expose the validated URL only on the live owner terminal as a manual fallback when a TTY exists; non-TTY mode fails closed. `tools/vm-owner-acceptance` preserves only an owner-owned standard session D-Bus socket/runtime directory through its worker shells for portal access. The normal TUI's host-browser `o` capability remains deferred; no guest-to-host command bridge is added.

### Owner-only execution procedure

This procedure is an owner handoff, not authorization for an agent to run host commands. Set the final candidate SHA only after the exact source tree has been independently reviewed and the owner-pushed candidate CI is green. The owner performs every host command. Do not install from or execute a mutable checkout; do not run `lab-vm` through `sudo`; do not install host packages, change legacy VM lifecycle state, or replace the old VM.

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
  lib/vm/browser-auth-session.py \
  lib/vm/virtiofs-socket.py \
  lib/vm/guest-user-dirs.py \
  | sudo /usr/bin/python3 -I -c 'import os,sys; os.umask(0o077); os.execv("/usr/bin/tar", ["/usr/bin/tar", "--no-same-owner", "--no-same-permissions", "--keep-old-files", "-xf", "-", "-C", sys.argv[1]])' "$INSTALL_ROOT"
sudo /usr/bin/python3 -I - "$INSTALL_ROOT" 0 0 <<'PY'
import os, stat, sys

root = sys.argv[1]
expected_uid, expected_gid = map(int, sys.argv[2:4])
directory_children = {
    ".": {"lab-vm", "lib"},
    "lib": {"vm"},
    "lib/vm": {
        "host-launcher.sh", "cloud-init.sh", "proxy.py", "bounded-log.py",
        "executable-policy.py", "browser-auth-session.py", "virtiofs-socket.py", "guest-user-dirs.py",
    },
}
file_modes = {
    "lab-vm": 0o755,
    "lib/vm/host-launcher.sh": 0o644,
    "lib/vm/cloud-init.sh": 0o644,
    "lib/vm/proxy.py": 0o644,
    "lib/vm/bounded-log.py": 0o644,
    "lib/vm/executable-policy.py": 0o644,
    "lib/vm/browser-auth-session.py": 0o644,
    "lib/vm/virtiofs-socket.py": 0o644,
    "lib/vm/guest-user-dirs.py": 0o644,
}
dir_modes = {".": 0o755, "lib": 0o755, "lib/vm": 0o755}
dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC

def refuse(message):
    raise SystemExit(f"REFUSED: extracted install tree {message}")

def open_dir(parent_fd, name, display):
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        refuse(f"contains a non-directory at {display}")
    fd = os.open(name, dir_flags, dir_fd=parent_fd)
    after = os.fstat(fd)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        os.close(fd)
        refuse(f"changed while opening {display}")
    return fd

def validate_tree(fd, relative):
    expected = directory_children[relative]
    if set(os.listdir(fd)) != expected:
        refuse(f"has unexpected or missing entries at {relative}")
    for name in sorted(expected):
        child = name if relative == "." else f"{relative}/{name}"
        if child in directory_children and child != ".":
            child_fd = open_dir(fd, name, child)
            try:
                validate_tree(child_fd, child)
            finally:
                os.close(child_fd)
        else:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                refuse(f"contains a non-regular file at {child}")

root_fd = os.open(root, dir_flags)
try:
    validate_tree(root_fd, ".")

    def normalize_dir(fd, relative):
        for name in sorted(directory_children[relative]):
            child = name if relative == "." else f"{relative}/{name}"
            if child in directory_children and child != ".":
                child_fd = open_dir(fd, name, child)
                try:
                    normalize_dir(child_fd, child)
                finally:
                    os.close(child_fd)
            else:
                file_fd = os.open(name, file_flags, dir_fd=fd)
                try:
                    os.fchown(file_fd, expected_uid, expected_gid)
                    os.fchmod(file_fd, file_modes[child])
                    result = os.fstat(file_fd)
                    if (not stat.S_ISREG(result.st_mode) or result.st_uid != expected_uid
                            or result.st_gid != expected_gid
                            or stat.S_IMODE(result.st_mode) != file_modes[child]):
                        refuse(f"failed ownership/mode verification at {child}")
                finally:
                    os.close(file_fd)
        os.fchown(fd, expected_uid, expected_gid)
        os.fchmod(fd, dir_modes[relative])
        result = os.fstat(fd)
        if (result.st_uid != expected_uid or result.st_gid != expected_gid
                or stat.S_IMODE(result.st_mode) != dir_modes[relative]):
            refuse(f"failed directory ownership/mode verification at {relative}")

    normalize_dir(root_fd, ".")
finally:
    os.close(root_fd)
PY
test -x "$INSTALL_ROOT/lab-vm"
for file in lib/vm/host-launcher.sh lib/vm/cloud-init.sh lib/vm/proxy.py lib/vm/bounded-log.py lib/vm/executable-policy.py lib/vm/browser-auth-session.py lib/vm/virtiofs-socket.py lib/vm/guest-user-dirs.py; do
  test -f "$INSTALL_ROOT/$file"
done
namei -l "$INSTALL_ROOT/lab-vm" "$INSTALL_ROOT/lib/vm/host-launcher.sh" \
  "$INSTALL_ROOT/lib/vm/cloud-init.sh" "$INSTALL_ROOT/lib/vm/proxy.py" \
  "$INSTALL_ROOT/lib/vm/bounded-log.py" "$INSTALL_ROOT/lib/vm/executable-policy.py" \
  "$INSTALL_ROOT/lib/vm/browser-auth-session.py" "$INSTALL_ROOT/lib/vm/virtiofs-socket.py" \
  "$INSTALL_ROOT/lib/vm/guest-user-dirs.py"
```

The dirfd walk checks every existing lexical component before creating its child, refuses symlinks or user/group/world-writable ancestors, creates only missing root-owned/traversable directories, and refuses any pre-existing candidate install directory. Extraction runs with root umask 077 and `--no-same-permissions`, so archive modes and the caller's umask cannot create group/world-writable entries, even transiently. The following dirfd-anchored normalizer requires exactly the nine expected regular files and three directories, rejects symlinks/special/extra entries, explicitly sets root ownership and exact 0755/0644 modes, and verifies each result. `tar --keep-old-files` also refuses overwrites. If extraction or normalization fails, do not execute or retry into the partial tree; stop for owner review. Stop unless `namei` shows no symlink in any path and every file, directory, and ancestor is root-owned and not group/world writable. The launcher's own protected-install check repeats this invariant before loading code. No `cp` from the mutable checkout, symlinked install path, package install, or runtime-path override is allowed.

#### Repair the already-extracted candidate, only after read-only verification

**Historical recovery only — do not use this block for the current short-socket candidate.** The following commands are tied to old candidate `ce88ae5b4fe706f90244ebc60562354c5ba36da4` and its seven-file layout, which predates `virtiofs-socket.py`. A new correction candidate must use the normal SHA-specific `--install` flow above and its current nine-file inventory; do not repair or reuse the historical tree for the new candidate. For the old tree only, do not rerun its create/extract block: it correctly refuses an existing target. The owner may retain and repair that tree in place only if all checks below pass **and** the owner has placed the host in a trusted maintenance window: no untrusted process may have write access through GID 0 or be concurrently modifying this tree. The owner must establish this from the host's user/group and process-management context; if uncertain, stop rather than chmod. The checks verify that historical exact SHA path, protected ancestors, exact seven-file tree, root ownership, no symlinks/special/extra entries, and byte-for-byte contents against that historical commit before changing permissions. If any check fails, stop and preserve the tree for review; do not use a recursive chmod or overwrite it.

```bash
set -euo pipefail
export LC_ALL=C
REPO='/path/to/owner/repository-clone'
CANDIDATE_SHA='ce88ae5b4fe706f90244ebc60562354c5ba36da4'
INSTALL_ROOT="/opt/little-mester/v3/$CANDIDATE_SHA"
[[ "$INSTALL_ROOT" == /opt/little-mester/v3/ce88ae5b4fe706f90244ebc60562354c5ba36da4 ]]
[[ "$(git -C "$REPO" rev-parse "$CANDIDATE_SHA^{commit}")" == "$CANDIDATE_SHA" ]]
for path in / /opt /opt/little-mester /opt/little-mester/v3 "$INSTALL_ROOT"; do
  test ! -L "$path"
  test "$(sudo stat -c '%F:%u:%g' "$path")" = 'directory:0:0'
  sudo /usr/bin/python3 -I -c 'import os,stat,sys; m=os.stat(sys.argv[1],follow_symlinks=False).st_mode; sys.exit(bool(m & (stat.S_IWGRP|stat.S_IWOTH)))' "$path"
done
expected="$(printf '%s\n' 'lab-vm f' 'lib d' 'lib/vm d' \
  'lib/vm/host-launcher.sh f' 'lib/vm/cloud-init.sh f' 'lib/vm/proxy.py f' \
  'lib/vm/bounded-log.py f' 'lib/vm/executable-policy.py f' \
  'lib/vm/browser-auth-session.py f' | LC_ALL=C sort)"
actual="$(sudo find "$INSTALL_ROOT" -xdev -mindepth 1 -printf '%P %y\n' | LC_ALL=C sort)"
test "$actual" = "$expected"
test -z "$(sudo find "$INSTALL_ROOT" -xdev \( ! -uid 0 -o ! -gid 0 \) -print -quit)"
for file in lab-vm lib/vm/host-launcher.sh lib/vm/cloud-init.sh lib/vm/proxy.py \
            lib/vm/bounded-log.py lib/vm/executable-policy.py lib/vm/browser-auth-session.py; do
  git -C "$REPO" show "$CANDIDATE_SHA:$file" | cmp - "$INSTALL_ROOT/$file"
done

# Exact paths only; do not recurse or change contents/ownership.
sudo chmod 0755 "$INSTALL_ROOT" "$INSTALL_ROOT/lib" "$INSTALL_ROOT/lib/vm" "$INSTALL_ROOT/lab-vm"
sudo chmod 0644 "$INSTALL_ROOT/lib/vm/host-launcher.sh" "$INSTALL_ROOT/lib/vm/cloud-init.sh" \
  "$INSTALL_ROOT/lib/vm/proxy.py" "$INSTALL_ROOT/lib/vm/bounded-log.py" \
  "$INSTALL_ROOT/lib/vm/executable-policy.py" "$INSTALL_ROOT/lib/vm/browser-auth-session.py"

for path in "$INSTALL_ROOT" "$INSTALL_ROOT/lib" "$INSTALL_ROOT/lib/vm" "$INSTALL_ROOT/lab-vm"; do
  test "$(sudo stat -c '%u:%g:%a' "$path")" = '0:0:755'
done
for path in "$INSTALL_ROOT/lib/vm/host-launcher.sh" "$INSTALL_ROOT/lib/vm/cloud-init.sh" \
            "$INSTALL_ROOT/lib/vm/proxy.py" "$INSTALL_ROOT/lib/vm/bounded-log.py" \
            "$INSTALL_ROOT/lib/vm/executable-policy.py" "$INSTALL_ROOT/lib/vm/browser-auth-session.py"; do
  test "$(sudo stat -c '%u:%g:%a' "$path")" = '0:0:644'
done
namei -l "$INSTALL_ROOT/lab-vm" "$INSTALL_ROOT/lib/vm/host-launcher.sh" \
  "$INSTALL_ROOT/lib/vm/cloud-init.sh" "$INSTALL_ROOT/lib/vm/proxy.py" \
  "$INSTALL_ROOT/lib/vm/bounded-log.py" "$INSTALL_ROOT/lib/vm/executable-policy.py" \
  "$INSTALL_ROOT/lib/vm/browser-auth-session.py"
```

This one-time recovery applies only to the exact recorded tree during the stated trusted maintenance window. If path, ownership, contents, layout, or symlink checks differ from the reported evidence, or writer quiescence cannot be established, stop before `chmod`. After successful verification and normalization, resume the normal read-only dependency/KVM preflight; no KVM or VM action is part of this correction.

Before allocating the workspace or starting QEMU, prove ordinary owner-installed runtime commands resolve through the fixed system `PATH` and pass the installed executable policy; for `virtiofsd`, check the `command -v` result on that `PATH` first, then `/usr/lib/virtiofsd`, `/usr/libexec/virtiofsd`, and `/usr/lib/qemu/virtiofsd` in that order, accepting only candidates that pass the installed executable policy. This is read-only; if any command is absent or refused, stop and have the owner provision it through the normal host process (never through this launcher).

```bash
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
for command in qemu-system-x86_64 qemu-img curl cat mv ssh ssh-keygen ssh-keyscan socat python3 timeout sha256sum date mktemp mkdir chmod stat id grep rm sleep readlink; do
  executable="$(command -v "$command")"
  /usr/bin/python3 -I "$INSTALL_ROOT/lib/vm/executable-policy.py" "$executable"
  printf '%s\t%s\n' "$command" "$executable"
done
iso_tool="$(command -v genisoimage || command -v mkisofs)"
/usr/bin/python3 -I "$INSTALL_ROOT/lib/vm/executable-policy.py" "$iso_tool"
printf 'ISO tool\t%s\n' "$iso_tool"
virtiofsd_path="$(command -v virtiofsd 2>/dev/null || true)"
virtiofsd_candidates=()
if [[ -n "$virtiofsd_path" ]]; then
  virtiofsd_candidates+=("$virtiofsd_path")
fi
virtiofsd_candidates+=(/usr/lib/virtiofsd /usr/libexec/virtiofsd /usr/lib/qemu/virtiofsd)
virtiofsd_executable=''
for candidate in "${virtiofsd_candidates[@]}"; do
  [[ -e "$candidate" || -L "$candidate" ]] || continue
  if /usr/bin/python3 -I "$INSTALL_ROOT/lib/vm/executable-policy.py" "$candidate" >/dev/null 2>&1; then
    virtiofsd_executable="$candidate"
    break
  fi
done
if [[ -z "$virtiofsd_executable" ]]; then
  printf 'STOP: no virtiofsd candidate passed the installed executable policy\n' >&2
  exit 1
fi
printf 'virtiofsd\t%s\n' "$virtiofsd_executable"
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

#### Run the browser-based smoke

No credential file, API key or API credits are used. Choose the owner-approved OpenAI model ID. The protected launcher runs pinned OpenCode v2.2.5 noninteractive `opencode auth login openai --method chatgpt-browser`; it is not a guest TUI `/connect` flow. The owner completes sign-in in the host browser opened through the desktop portal. The OAuth URL is never written to command arguments, environment, logs, reports or persistent files. If the portal is unavailable or declines, a controlling TTY is required for the live-terminal manual fallback; do not copy the URL into notes, commands, screenshots or evidence. Cancellation, callback/forward rejection, timeout or unverifiable cleanup fails the smoke.

```bash
PRIVATE_DIR="$(readlink -f -- "/run/user/$(id -u)")"
test "$(stat -c '%u:%a' "$PRIVATE_DIR")" = "$(id -u):700"
STATE_ROOT="$HOME/.local/state/little-mester/v3-smoke"
case "$WORKSPACE/" in "$PRIVATE_DIR/"*) printf 'STOP: private evidence directory overlaps the workspace\n' >&2; exit 1;; esac
case "$PRIVATE_DIR/" in "$WORKSPACE/"*) printf 'STOP: private evidence directory overlaps the workspace\n' >&2; exit 1;; esac
case "$INSTALL_ROOT/" in "$PRIVATE_DIR/"*) printf 'STOP: private evidence directory overlaps the install\n' >&2; exit 1;; esac
case "$PRIVATE_DIR/" in "$INSTALL_ROOT/"*) printf 'STOP: private evidence directory overlaps the install\n' >&2; exit 1;; esac
case "$STATE_ROOT/" in "$PRIVATE_DIR/"*) printf 'STOP: private evidence directory overlaps runtime state\n' >&2; exit 1;; esac
case "$PRIVATE_DIR/" in "$STATE_ROOT/"*) printf 'STOP: private evidence directory overlaps runtime state\n' >&2; exit 1;; esac
MODEL='openai/gpt-6-luna#max'
[[ "$MODEL" =~ ^openai/[A-Za-z0-9._-]{1,100}(#[A-Za-z0-9._-]{1,32})?$ ]]
```

The maintained `tools/vm-owner-acceptance` runner is the preferred entry point and handles protected install, preflight, bounded storage proof, native browser-auth smoke and cleanup verification. If following the lower-level procedure, create a new mode-0600 evidence log outside the workspace and runtime state, then invoke only the protected installation as the unprivileged owner. The portal receives the URL; normal captured stdout/stderr contain only sanitized session status and smoke markers. Only the documented live-terminal fallback may display the URL.

```bash
EVIDENCE="$PRIVATE_DIR/v3-smoke-$CANDIDATE_SHA.log"
umask 077
set -o noclobber
exec {evidence_fd}> "$EVIDENCE"
test "$(stat -c %a "$EVIDENCE")" = 600
if "$INSTALL_ROOT/lab-vm" smoke --workspace "$WORKSPACE" --model "$MODEL" >&"$evidence_fd" 2>&1; then
  smoke_status=0
else
  smoke_status=$?
fi
exec {evidence_fd}>&-
printf 'smoke exit status: %s\n' "$smoke_status"
cat "$EVIDENCE"
test "$smoke_status" -eq 0
```

A pass requires exit 0, all five acceptance markers from the same fresh guest (`ROOTLESS_DOCKER_OK`, `VM_UP`, `BROWSER_AUTH_UI_OK`, `VM_RESPONSE_OK`, `DOCKER_CONTAINER_OK`), and the final `Smoke acceptance stages completed.` line. `ROOTLESS_DOCKER_OK` is emitted only after the guest reports rootless Docker on the per-user socket and no rootful socket; `BROWSER_AUTH_UI_OK` follows successful noninteractive browser OAuth and verified auth-store permissions; `VM_RESPONSE_OK` requires a real model completion; and `DOCKER_CONTAINER_OK` follows an actually executed container. Record the exact candidate SHA, installed path, protected executable paths, command, exit status, and redacted output. Never include the URL or OAuth values in evidence.

#### Verify cleanup; preserve rollback

Copy the exact `Run state:` path from the private evidence log into `RUN_DIR`. Successful cleanup must have emitted the final completion line only after exact-process identity cleanup, bounded-logger process-group proof, browser-auth cleanup proof, `/proc/net/unix` listener-absence check and SSH control-socket removal. Inspect the run directory (owner UID, mode 0700) and confirm it contains the private bounded `qemu.log`, `virtiofsd.log`, and `proxy.log` (each mode 0600 and at most 1 MiB), plus `virtiofs.socket-path`. The helper validates any short socket/PID artifacts under `/run/user/<uid>/`; it checks for a live listener even if the socket pathname was unlinked or the per-run short socket directory disappeared. PID-file contents are evidence only and must never be used to signal a process. The disk image, download, seed, metadata, SSH keys, known-host files, auth URL/port/proof files, process identity/completion files, and command records must be absent. Any matching process, ambiguous status, unexpected artifact, or missing completion marker is a STOP condition; absence of a matching process alone is not proof of complete cleanup. Do not kill an unrelated PID, force/lazy-unmount, or delete ambiguous state. The maintained `tools/vm-owner-acceptance --verify-cleanup` performs this read-only inventory check for a uniquely bound successful owner-runner report; early startup failure cannot be upgraded to cleanup PASS.

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

The workspace listing must be empty. The two final commands unmount only this exact tmpfs normally and remove its empty mountpoint; if unmount fails, stop for owner review (never use `umount -l` or `-f`). Preserve the bounded diagnostic logs until reviewed. The old VM and its credentials/runtime remain untouched; any failed gate means no cutover, no replacement, and no rollback action against the old VM. The disposable v3 disk and its guest auth database are destroyed after verified cleanup. If an owner is manually operating a still-running guest outside this smoke, `opencode auth list` shows saved accounts and `opencode auth logout openai <account-label>` logs out one account; the smoke never invokes logout. Unit, Bats, proxy, CLI-help, and synthetic fake-target checks are not host acceptance.

## Owner-observed cutover checklist — after Phase 1 only

This is a separate operational gate, not a Phase 1 implementation or merge criterion. The owner alone decides whether to cut over, using a fresh v3 runtime at a recorded **committed, owner-pushed SHA** and preserving the old VM.

- [ ] Before browser login, record where auth state is stored, which process/user can read it, how the agent uses it, and the exact required public endpoints. Do not copy tokens from the old VM or broaden egress.
- [ ] Owner completes browser-based OpenAI login in the new VM and observes a genuine OpenAI response.
- [ ] Existing Little Mester MCP works; tickets and decisions are available.
- [ ] Sol-to-Luna delegation is bounded and works; repository and branch are correct.
- [ ] Rootless Docker works; useful logs are available; configuration/tickets are sufficient to reconstruct context after restart.
- [ ] Owner preserves the old VM until all observations pass. Any browser-auth, persistence, or endpoint gap is a future change request; do not silently expand Phase 1.
