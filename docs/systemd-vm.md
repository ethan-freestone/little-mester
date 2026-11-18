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

The equivalent owner-acceptance adapter is:

```sh
tools/vm-owner-acceptance --system-vm <operation> --sha <commit> --repo <source-repository>
```

Both dispatch to `lab-vm`; neither accepts a unit, disk path, QEMU option,
arbitrary systemd property, or remote command. `up` waits for cloud-init, SSH, and the existing protected parent
session-contract readiness checks. `auth` uses the existing bounded browser
callback bridge.

The proxy is a separate `lm-vm-proxy` service. Its bounded TCP readiness probe
must pass before QEMU starts; losing the proxy stops the VM unit. QEMU shutdown
uses a fixed private QMP socket and waits for the exact MainPID to exit. A
failed/expired QMP exchange returns failure; systemd then applies the declared
control-group TERM/KILL fallback. Automatic restart is disabled.
