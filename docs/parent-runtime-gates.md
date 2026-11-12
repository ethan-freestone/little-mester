# Owner-only persistent-parent runtime gates

**Owner-reported PASS ×2 for probe revision `37f5fb622135831a5195615e175086c8053b0992`.**
The owner ran the corrected scratch-only QEMU/qemu-img gate twice on the owner host with
QEMU/qemu-img 11.1.3; both reported PASS and removed their scratch artifacts. This clears
only inherited output-FD conversion, explicit qcow2 inspection, QEMU fdset opening, and
independent-writer lock refusal for that host/tool version. It does not enable the parent
launcher or certify a VM. Do not rerun the owner-only gate autonomously.

## Scratch QEMU/qemu-img gate

From the candidate checkout, after explicit owner approval:

```sh
python3 -I tools/vm-parent-runtime-gate.py --owner-approved
```

The script invokes only the already-installed `qemu-img` and `qemu-system-x86_64` found
on the host's trusted system PATH. It creates a fresh mode-0700 directory under the
system temporary directory containing a 16-MiB raw source and one qcow2 output. It
checks qemu-img conversion into a pre-created inherited output FD, then inspects that
scratch image using explicit `-f qcow2`, expected virtual size, and no backing/external
data file. It starts a paused `-S`, TCG-only QEMU with `-nodefaults`, no network device,
no monitor/display/serial, and the same fdset/blockdev/`locking=on` pattern as the
parent builder (the gate names each inherited descriptor by its actual FD number so
subprocess-internal descriptors cannot collide with FD 3). A second paused QEMU attempts to open the same scratch disk; the gate
passes that check only when the second process exits nonzero with a lock diagnostic.
Output and diagnostics are printed. No image or VM state outside the fresh scratch
directory is opened or changed; it never uses KVM, the legacy VM, the actual parent
disk, or credentials. It installs nothing and does not change launcher source.

All commands have bounded timeouts. The script tracks only its own QEMU subprocesses
with pidfds; cleanup sends TERM and, only for a still-running paused scratch QEMU,
bounded KILL. Scratch files are removed only after both QEMU processes are confirmed
exited. On an ambiguous result, unexpected output, timeout, missing tool or cleanup
failure it reports NOT_PASS/REFUSED, does not reinterpret the result, and preserves
the scratch path for owner inspection. A PASS establishes only these local binary and
scratch-filesystem behaviors; it is not full parent/guest acceptance.

## NVIDIA collector/timing probe — PENDING OWNER RESULT

Run only on the owner's actual host, with owner approval, after the scratch gate (or
as a separately approved read-only hardware probe). This reads NVIDIA telemetry and
does not change device settings, start a VM, authenticate, or access project data:

```sh
set -eu
command -v nvidia-smi
timeout --signal=TERM --kill-after=1 5 \
  nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,memory.total,memory.used,memory.free \
  --format=csv,noheader,nounits
for n in 1 2 3 4 5 6 7 8 9 10; do
  /usr/bin/time -f 'sample_wall_seconds=%e' timeout --signal=TERM --kill-after=1 5 \
    nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,memory.total,memory.used,memory.free \
    --format=csv,noheader,nounits >/dev/null
done
timeout --signal=TERM --kill-after=1 5 nvidia-smi --version
```

Preserve the command output, tool version, and all ten timing lines. This bounded probe
informs whether polling can fit the 500-ms budget; it is not metrics acceptance and does
not authorize changing the collection mechanism or adding egress.

## Combined return

The owner has now supplied the QEMU gate output twice. Do not repeat parent provisioning
or use the actual persistent-parent disk. The four scratch compatibility checks listed
above are satisfied for the tested owner host/tool version only. The NVIDIA probe remains
pending and informs the later M3 GPU sampling choice. The integrated owner-invoked
`lab-vm parent up` path is now present, but the scratch result is not guest or lifecycle acceptance; use
the separate [owner-run lifecycle procedure](parent-lifecycle-acceptance.md) for that.

`lab-vm parent stop` waits at most 15 seconds by default. A timeout does not cancel the
supervisor's graceful shutdown: a verified RUNNING QEMU may still be in its bounded QMP
shutdown wait (up to 60 seconds). After a stop timeout or refusal, check
`lab-vm parent status` before retrying; no signal escalation is performed, and an ambiguous result
requires owner review rather than disk cleanup.
