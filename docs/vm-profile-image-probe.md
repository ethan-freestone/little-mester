# Approved profile image and overlay probe

**Date:** 2026-11-25
**Purpose:** isolated, unprivileged tool probe for the owner-approved base and
qcow2 backing behavior. This is not guest, hosted-KVM, systemd, release-trust, or
owner-host acceptance.

## Inputs and environment

- Source: Ubuntu 24.04 Noble amd64, release `2026-10-17`.
- URL: `https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-amd64.img`
- Downloaded length: `621284352` bytes.
- Expected and observed SHA-256:
  `04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94`.
- `qemu-img`: 8.2.2 (`Debian 1:8.2.2+ds-0ubuntu1.18`).
- `qemu-system-x86_64`: 8.2.2; QEMU system under test ran `-machine none -S`,
  without a guest, KVM, systemd, or host service changes.
- Download, image inspection, overlay creation, qemu-io, and QEMU probing ran as
  UID 1000, not root. Probe artifacts were kept outside the repository under a
  mode-0700 temporary directory.

## Approved base observations

Commands:

```sh
sha256sum "$BASE"
qemu-img info --output=json "$BASE"
qemu-img info --backing-chain --output=json "$BASE"
qemu-img check -f qcow2 "$BASE"
```

Observed normalized metadata:

| Property | Value |
|---|---:|
| Format | `qcow2` |
| Virtual size | `3758096384` bytes (`3.5 GiB`) |
| Stored source size | `621284352` bytes |
| QEMU allocated size | `621289472` bytes |
| Compat | `1.1` |
| Cluster size | `65536` bytes |
| Compression | `zlib` |
| Lazy refcounts | `false` |
| Refcount bits | `16` |
| Corrupt | `false` |
| Extended L2 | `false` |
| Backing chain | One image; no backing filename/format |
| External data | None (`format-specific.data` had no `data-file`) |
| `qemu-img check` | No errors |

The image was used unchanged. Its pinned digest is the trust check; this probe
does not establish an upstream signature or checksum-manifest verification.

## Sparse overlay and isolation observations

Two overlays were created from the same absolute base path with:

```sh
umask 077
qemu-img create -f qcow2 -F qcow2 -b "$BASE" \
  -o preallocation=off "$OVERLAY" 64G
```

Each reported `format=qcow2`, `backing-filename-format=qcow2`, the exact absolute
base path, and `virtual-size=68719476736` bytes. The initial qemu-img allocated
size was `200704` bytes; `stat` reported a logical file size of `197632` bytes.
`qemu-img check -f qcow2` reported no errors. Creation with `umask 077` produced
mode `0600`; an earlier control run under umask `022` produced mode `0644`, so
production creation must set and verify restrictive permissions.

With two distinct overlays, `qemu-io` wrote 4096 bytes of `0x5a` to overlay A at
offset `4294967296` (4 GiB, beyond the base's 3.5-GiB virtual end). Reading the
same range from overlay B returned all zero bytes. The base SHA-256 remained the
approved value after overlay creation and the write. This demonstrates
`qemu-img`/`qemu-io` backing isolation for these local artifacts, not guest-level
or KVM isolation.

## Metadata-negative and QEMU block-graph probes

- A qcow2 fixture created with external data emitted
  `format-specific.data.data-file` and `data-file-raw=true` in
  `qemu-img info --output=json`. A validator that checks only `format=qcow2`
  would miss this; v2 must reject external-data fields. `qemu-img info` returned
  metadata when the external-data file had mode `0444`; therefore startup must
  not rely on running that command against an existing untrusted overlay in an
  unconstrained context.
- A nested backing fixture yielded three entries from
  `qemu-img info --backing-chain --output=json`; v2 permits only the single
  overlay-to-base relationship and the standalone base.
- A temporary copy of an overlay had its embedded backing header changed with
  `qemu-img rebase -u` to a nonexistent, unapproved path. Direct
  `qemu-img info --output=json` reported the header path without requiring that
  path to exist. A QEMU 8.2.2 `-machine none -S` block graph received the exact
  already-open base (`O_RDONLY`) and overlay (`O_RDWR`) descriptors through
  `-add-fd`/`/dev/fdset`. It attached a read-only base node and explicitly named
  that node as the overlay's backing. QMP reported all four named nodes; QEMU
  exited cleanly after `quit`, without resolving the poisoned embedded path.
- The same poisoned overlay opened without an explicit backing node only after
  QEMU attempted the embedded path; QEMU exited 1 with `Could not open backing
  file` naming the unapproved nonexistent path. Thus explicit graph wiring is a
  required safety property, not an optimization.
- A separate external-data image pointed at a `0444` data file. As UID 1000,
  QEMU opened the qcow2 as writable and exited 1 with `Permission denied` for
  that external-data path. This supports the required systemd writable-path
  boundary but is not a systemd sandbox test. The start-time inspector must
  reject the external-data feature before QEMU launch and read only the already
  opened overlay descriptor; the unit sandbox is defense in depth.

The probe supports the exact base and overlay design on this QEMU version. It
does not demonstrate a boot, cloud-init, `virtio-blk` guest attachment, the
fd-only qcow2 header inspector, a systemd filesystem namespace, the supported
host distribution matrix, or hosted-KVM behavior. Those remain required
integration evidence. Keep the flattened-image validator used by the singleton
and parent VM unchanged; named-profile overlays require their own strict
validator.
