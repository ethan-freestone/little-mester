# Fixed systemd VM slice

This is a separate, fresh-disk VM from the older owner-managed parent and from
the transient smoke runs. It uses only `/var/lib/little-mester-vm/disk.qcow2`
and `seed.iso`; it never adopts an existing VM disk. The system unit's
`ExecStart` is the fixed QEMU binary itself, with a direct, explicitly locked
qcow2 block node, so QEMU is the unit's MainPID. The existing parent launcher
remains available as an owner-facing compatibility path.

Smoke and system guests use the shared `lib/vm/guest-bootstrap.py` foundation
for the pinned proxy configuration, rootless Docker, user-directory checks,
and OpenCode provisioning. Smoke selects its existing virtiofs workspace;
the persistent system VM selects a guest-disk workspace and layers the
protected parent session assets/readiness contract on top. Hosted KVM CI and
smoke also obtain their QEMU machine, block, and network arguments from the
same protected `parent-qemu.py` builder; CI retains its separate disposable
lifecycle owner.

After the exact candidate is installed under `/opt/little-mester/v3/<commit>`,
an owner may explicitly install the fixed account, units, sudoers file, config,
and fresh disk with:

```sh
sudo /opt/little-mester/v3/<commit>/tools/vm-system-install
```

The protected runtime is installed under `/usr/lib/little-mester-vm`, a path
available on both Ubuntu and merged-`/usr` distributions such as Arch. The
installer verifies root ownership and non-writable ancestry before creating any
host account, directory, or file. Installation requires the host already to
have `/dev/kvm` group-owned by the existing `kvm` group with group read/write
permission, plus the listed QEMU,
image, ISO, SSH, and socat tools. The installer deliberately does not change
device rules, install packages, enable or start units, or remove pre-existing
artifacts. It downloads and verifies the pinned base image only when explicitly
invoked. Any pre-existing VM disk, seed, identity, or partial-provisioning
artifact is preserved and blocks the installer for owner inspection; the
installer does not attempt automatic state adoption or repair. If the owner is
newly added to `lm-vm-control`, a new login is needed for group
membership to take effect.

The fixed owner interface is:

```sh
lab-vm system up|down|status|ssh|auth|tui
```

The legacy `./lab` command also has a thin VM dispatcher. It pins the current
checkout's full Git commit and forwards operations to the existing owner
acceptance/runtime path; it does not implement a second VM controller:

```sh
./lab vm up|down|status|ssh|auth|tui|verify|accept
./lab vm prepare-dependencies production|acceptance|system-install
```

The shared host capability checker supports apt on Ubuntu and pacman on Arch
and CachyOS (the distributions mapped and validated here). It checks fixed
executable paths, `/usr/bin/python3` imports, KVM access, and a responsive
systemd manager. Production dependencies do not
include `pexpect`; acceptance does. When packages are missing, it previews the
native transaction with versions, asks separately for package approval, then
rechecks. Apt targets the reviewed versions and leaves its final native summary
and confirmation visible; pacman likewise leaves its final transaction and
confirmation visible, with no noninteractive auto-confirmation. Decline any
unexpected replacement, removal, or upgrade. The bootstrap never runs a
system upgrade or removes packages. Pacman setup refuses when `pacman -Qu`
reports pending upgrades and never runs `-Sy`; it
uses only cached sync databases and cannot prove remote database freshness.
The owner must have recently completed a full system upgrade against those
repositories and must decline if that is uncertain; the tool never performs
that maintenance itself. Unsupported distribution identities, unsafe
previews, non-installable requirements, and
noninteractive missing-dependency cases fail without attempting installation.
Read-only `verify` and owner preflight do not invoke package setup. Pinned
deployment and VM lifecycle approvals remain separate.
Hosted CI reads the same apt package definitions for its VM host and local
acceptance utilities while retaining independent job setup and cleanup.

The SHA-pinned owner-acceptance command offers read-only verification and an
explicit lifecycle acceptance mode:

```sh
tools/vm-owner-acceptance --system-vm verify --sha <commit> --repo <source-repository>
tools/vm-owner-acceptance --system-vm accept --sha <commit> --repo <source-repository>
```

`verify` never changes lifecycle state. `accept` requires the exact interactive
confirmation or `--approve-system-vm-lifecycle`, checks the deployed candidate,
network restrictions, OpenCode TUI, and same-disk persistence across restart,
then leaves the VM running. Hosted CI invokes this same installed command, then
runs CI-only forced-stop fault injection and disposable cleanup. Neither this
command nor the fixed owner interface accepts a unit, disk path, QEMU option,
arbitrary systemd property, or remote command. `up` waits for cloud-init, SSH,
and the existing protected parent
session-contract readiness checks. `auth` uses the existing bounded browser
callback bridge.

The proxy is a separate `lm-vm-proxy` service. Its bounded TCP readiness probe
must pass before QEMU starts; losing the proxy stops the VM unit. QEMU shutdown
uses a fixed private QMP socket and waits for the exact MainPID to exit. A
failed/expired QMP exchange returns failure; systemd then applies the declared
control-group TERM/KILL fallback. Automatic restart is disabled.
