# VM v3 Phase 1 smoke and later cutover

## Phase 1 owner smoke

**Status:** NOT_RUN until Sol reviews the implementation diff/tests and the owner authorizes a fresh Dev VM. This source tree is not an executable trust boundary.

- **Reviewed source SHA:** `PENDING — record the implementation commit SHA after review; do not use baseline 60f3bd59ab3ca51756b1aee523b172cae61bb7d2 as the implementation SHA.`
- **Protected installation:** owner installs that reviewed source under a root-owned, non-group/world-writable directory outside the exported workspace, for example `/opt/little-mester/v3/<reviewed-sha>/`. `lab-vm`, `lib/vm/host-launcher.sh`, `lib/vm/cloud-init.sh`, `lib/vm/proxy.py`, and `lib/vm/bounded-log.py`, plus every ancestor directory, must be root-owned and not group/world writable. Keep the source SHA and installed path in the evidence. Do not run from a mutable checkout or install/refresh host packages through the launcher.
- **Protected host dependencies:** use owner-installed root-owned system executables on the fixed system `PATH`: `qemu-system-x86_64`, `qemu-img`, `virtiofsd`, `curl`, `ssh`, `ssh-keygen`, `ssh-keyscan`, `socat`, `python3`, `timeout`, `sha256sum`, `date`, `mktemp`, `mkdir`, `chmod`, `stat`, `id`, `grep`, `rm`, `sleep`, `readlink`, and either `genisoimage` or `mkisofs`. KVM must be available at `/dev/kvm`; no TCG fallback.
- **Workspace storage prerequisite:** the selected workspace must be the root of a dedicated, owner-provisioned filesystem whose reported capacity is at most 8 GiB. A normal directory, bind mount exposing a larger filesystem, or user quota on a larger filesystem is not accepted. The launcher neither provisions storage nor changes host mounts; arrange this prerequisite through the host's normal owner-controlled storage process. This is the finite-storage boundary for exported workspace writes, not a generic host quota manager.
- **Inputs:** select one fresh, disposable workspace on that bounded filesystem, outside the installed tree and private runtime state. Create a private mode-0600, invoking-user-owned, single-link JSON credential file outside both the installation and workspace, with exactly `{"provider":"openai","model":"openai/<model-id>","api_key":"<owner-supplied-key>"}`. Do not put credential values in shell arguments, workspace files, logs, tickets, seeds, images, or Git. The launcher pipes the key over SSH to the guest OpenCode API-key prompt and stores it in the guest-private OpenCode database. The [OpenCode v2 provider CLI docs](https://opencode.ai/v2/docs/cli/providers) specify `opencode auth login openai --method key`; the pinned v2.2.5 CLI help confirms `--method` is supported.

Run from the protected installation:

```sh
./lab-vm smoke --workspace "$V3_SMOKE_WORKSPACE" --credentials-file "$V3_PROVIDER_CREDENTIALS"
```

Record exact command, installed source SHA/path, exit status, and redacted output. A pass requires all independent proofs in the same new guest: `VM_UP`, an actual provider response `VM_RESPONSE_OK`, rootless daemon evidence `ROOTLESS_DOCKER_OK`, and an actually executed container `DOCKER_CONTAINER_OK` with exit 0. Preserve only the private bounded diagnostics under `~/.local/state/little-mester/v3-smoke/` (at most eight retained runs and 1 MiB per log); ambiguous disk/process state blocks another run for owner review. Do not publish secrets or raw credential-bearing terminal output. Never replace or alter the existing VM. Unit, Bats, proxy, CLI-help, and disposable fake-credential checks are not host acceptance.

## Owner-observed cutover checklist — after Phase 1 only

This is a separate operational gate, not a Phase 1 implementation or merge criterion. The owner alone decides whether to cut over, using a fresh v3 runtime at a recorded **committed, owner-pushed SHA** and preserving the old VM.

- [ ] Before browser login, record where auth state is stored, which process/user can read it, how the agent uses it, and the exact required public endpoints. Do not copy tokens from the old VM or broaden egress.
- [ ] Owner completes browser-based OpenAI login in the new VM and observes a genuine OpenAI response.
- [ ] Existing Little Mester MCP works; tickets and decisions are available.
- [ ] Sol-to-Luna delegation is bounded and works; repository and branch are correct.
- [ ] Rootless Docker works; useful logs are available; configuration/tickets are sufficient to reconstruct context after restart.
- [ ] Owner preserves the old VM until all observations pass. Any browser-auth, persistence, or endpoint gap is a future change request; do not silently expand Phase 1.
