# Dockerfile — why one for both agents

## Decision

Keep a **single** Dockerfile at `agent/Dockerfile` instead of splitting into
`agent/opencode/Dockerfile` and `agent/aider/Dockerfile`.

### Why one file

1. Both agents share the same base dependencies: git, curl, ripgrep, nodejs/npm,
   NVIDIA container toolkit compatibility (via host config).
2. The lab script already selects which binary to invoke per-run via `--agent`.
3. One image to build and maintain; fewer Docker contexts to understand.

### How splitting works today

The agents diverge only in their binary dependencies:

| Dep | opencode | aider |
|-----|----------|-------|
| Binary | `opencode` (npm) | `aider-chat` (pip) |
| Config | `opencode/opencode.json` → `/etc/opencode/opencode/opencode.json` | (none mounted yet) |
| Runtime choice | CMD `"opencode"` / env override | `CMD ["aider"]` |

lab picks the binary with this section:

```bash
case "$AGENT" in
  aider|opencode) ;;
  *) die "unknown agent: $AGENT (expected aider or opencode)" ;;
esac
...
dc run --rm agent "$AGENT" "$@"
```

### What would trigger a split

- When one agent starts needing different apt/npm/python system packages consistently.
- When per-agent image size becomes meaningful (e.g., >2x difference).
- When build times diverge enough that hot-swapping agents requires separate contexts.
- When security profiles diverge (different cap sets, tmpfs mounts, etc.).

### Config locations for v2 migration

| Agent | Old path | New path |
|-------|----------|----------|
| opencode | `agent/opencode.json` | `agent/opencode/opencode.json` |

The Dockerfile COPY and OPENCODE_CONFIG env var are updated to reflect this change.
