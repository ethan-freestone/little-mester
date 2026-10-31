# Ticket Store

The ticket store is the authoritative source of truth for project work tracking.

## ⚠️ Agents: DO NOT EDIT DIRECTLY

**If you are an AI agent (opencode, Claude Code, Codex, or any other), this rule applies to you:**

All ticket data (`docs/tickets/*.json`, `_index.json`, `_audit.json`) MUST only be modified
through the ticket-engine CLI or MCP tools. Direct file edits are forbidden.

- **CLI:** `./little-mester-mcp/scripts/ticket-engine <action> ...`
- **MCP tools:** `mester_tickets_*` tools

### Why
The engine maintains derived files (`_index.json`, `_audit.json`) and a PID lock atomically.
Direct edits cause silent drift. This is a workspace invariant for every agent.

### What's allowed
- **Reading** ticket files (cat, grep, editor views) — always fine.
- **Editing files outside** `docs/tickets/` — product code, configs, docs, scripts.

### What requires the engine
- Create, modify, delete any ticket file
- Change status, acceptance criteria, tests, evidence, comments
- Archive a ticket
- Modify `_index.json` or `_audit.json`

For the full decision with rationale and cross-agent enforcement details, see:
[docs/DECISIONS.md §B.4](../DECISIONS.md#b4-ticket-edits--mcpcli-only-enforced-across-all-agent-implementations)
