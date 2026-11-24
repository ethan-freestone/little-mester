# Phase 1 owner functional acceptance — #54 / #55

This checklist is for the owner after the exact reviewed candidate has a green
hosted KVM run. It does not authorize touching or upgrading an existing disk.
Use the protected, SHA-pinned installation procedure only after the owner has
approved a non-destructive deployment plan for that specific disk. Keep the
current persistent-parent VM and disk untouched until then. Stop on any identity
mismatch, unexpected prompt, lost state, or cleanup ambiguity; preserve evidence
and do not improvise recovery.

## Already covered by automated evidence

The hosted run establishes disposable real-KVM boot, strict SSH, protected
OpenCode configuration and startup, tmux attach/reattach, synthetic workspace
and OpenCode-state persistence across reboot, a **sampled** restricted-egress
check, and synthetic OAuth outcomes through the real bridge. These checks do
not establish owner-parent behavior, real provider access, or real credentials.

## Owner-only checks

Using the owner-approved protected install and the exact persistent-parent disk
reserved for acceptance:

1. **Identity and reconnect (#54):** record the reviewed source SHA, installed
   SHA, run/supervisor/QEMU identities, and disk device/inode. Start/reconnect
   only via the documented parent commands in
   [`parent-lifecycle-acceptance.md`](../parent-lifecycle-acceptance.md). Confirm
   the intended VM and original disk; do not proceed on an identity mismatch.
2. **Workspace and TUI (#54/#55):** verify the initial workspace and working
   directory are the intended project root. Attach, disconnect, and reattach
   tmux; make a harmless edit in a disposable test file and verify it in the
   same workspace. Exercise the exit-confirmation keys and cancellation/confirm
   paths; verify ordinary shell commands execute and output returns.
3. **Real authentication and model (#55):** use the normal protected OpenCode
   TUI/browser flow with the owner's approved account. Confirm successful
   access to an owner-approved model and complete one harmless coding
   interaction. Do not provide secrets to CI or retain auth URLs/tokens in
   reports.
4. **History and persistence (#54/#55):** create a real conversation, exit and
   reopen it, then perform the documented ordinary stop/restart on the same
   approved disk. Verify the workspace change, conversation history, and
   authentication still work. A synthetic SQLite fixture is not a substitute.
5. **Security and interaction:** verify protected config/plugin assets remain
   root-owned and guest-user immutable; project-local config/plugins do not
   override them. Confirm required interactive prompts and cancellation. Review
   the owner-approved terminal clipboard policy and run only its harmless
   clipboard canary; hosted assertions do not fully prove clipboard behavior.
6. **Lifecycle and cleanup (#54):** use the ticket's current ordered gates and
   the parent lifecycle runbook; confirm status, reconnect, duplicate-up
   refusal/idempotence, graceful stop, same-disk restart, and clean recovery
   preview. Do not force-kill processes, delete state, replace disks, or run
   recovery execution unless the current ticket explicitly authorizes that
   exact owner action.

Record owner observations and remaining failures against authoritative tickets
#54 and #55. Do not mark their criteria passed merely because CI, synthetic
OAuth, or source review passed. The latest ticket handoff supersedes this
summary for exact commands and stop conditions.
