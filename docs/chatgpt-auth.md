# VM-native OpenCode authentication

## Compatibility entry: OpenCode v2.2.5

The selected candidate is **OpenCode v2.2.5**. On 2026-11-04, the owner supplied
the guest's `opencode --version` and help output from `opencode auth` and its
`list`, `login`, `export`, and `import` subcommands. No login, account listing,
export, import, or credential-store read was performed for this qualification.

The supplied help confirms:

| Command | v2.2.5 contract used here |
|---|---|
| `auth list` | `--format json` is available; output describes integrations and connections. |
| `auth login` | Optional integration target, `--method`, and repeatable `--answer` are available. |
| `auth export` | An integration target scopes export; omitting it exports every integration. Output contains secrets. |
| `auth import` | Reads a file, or stdin when no file argument is supplied. |

The tagged upstream source was reviewed for the candidate behavior:

- [`chatgpt.ts` at v2.2.5](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/core/src/plugin/provider/chatgpt.ts)
  binds the ChatGPT callback to `127.0.0.1` on an OS-selected port, uses
  `/auth/callback`, and includes that exact redirect URI in the authorize URL.
- [`export.ts`](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/cli/src/commands/handlers/auth/export.ts)
  filters credential export by the resolved integration target and serializes
  credential records to stdout.
- [`import.ts`](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/cli/src/commands/handlers/auth/import.ts)
  accepts stdin and imports credential records without replacing matching IDs.
- [`list.ts`](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/cli/src/commands/handlers/auth/list.ts)
  returns integration connection metadata in JSON mode; it is not used as a
  secret-export or credential-database interface.
- [`anthropic.ts`](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/ai/src/providers/anthropic.ts)
  accepts the runtime `ANTHROPIC_API_KEY` provider credential.
- [`moonshot.ts`](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/ai/src/providers/moonshot.ts)
  is the Moonshot/Kimi provider; this adapter supplies the supported
  `MOONSHOT_API_KEY` environment name at agent runtime.

This is a narrow, source-reviewed compatibility entry—not a stable upstream API
guarantee. Browser forwarding is enabled only for the qualified version and
validated URL shape. Unknown versions and callback shapes fail closed. The
v2.2.5 CLI exposes no standalone `auth status` subcommand, so this implementation
does not claim to observe OAuth success or cancellation: it bounds each callback
forward with a 120-second lease (configurable up to 600 seconds) and closes all
session resources when the agent session exits or is interrupted. Immediate
success/cancel cleanup remains a live compatibility gate.

## Native ChatGPT browser handoff

The guest runs native OpenCode as `mester`. With browser authentication enabled,
the host creates one private MCP bridge and one SSH control session for that
agent session. The guest's temporary `xdg-open`/`gio` shim can invoke only the
bridge's `open_auth_url` capability. The bridge accepts only the qualified
OpenAI authorization endpoint, validates the single loopback `redirect_uri`,
adds the exact callback port as a `127.0.0.1`-to-`127.0.0.1` SSH forward, and
probes that listener with a harmless `GET /` before opening the unchanged URL in
the host browser. It does not receive the OAuth callback or expose a general
browser, command, file, or network capability.

Each agent invocation has an independent random bearer token, host listener,
and lease. The bearer value is transferred to the guest over SSH stdin and kept
in a private temporary file; it is not placed in a command argument or log.
Callback URLs, request bodies, OAuth state, codes, and tokens are never logged.
An occupied callback port or failed forward prevents the browser from opening.

The guest agent keeps its native credential store in the profile overlay.
Suspending and resuming preserves that state. `auth_forward=off` transfers no
credentials. `auto` selects only implemented adapters; an explicit unsupported
provider selector is an error, not a silent no-op. For v2.2.5, OpenAI is the
reviewed native export/import adapter. Anthropic and Kimi use runtime API-key
adapters; they do not read or copy OpenCode's auth database. Their private escrow
files have the exact JSON shape
`{"schema_version":1,"provider":"anthropic|kimi","api_key":"..."}`.
The guest validates the file and injects `ANTHROPIC_API_KEY` or
`MOONSHOT_API_KEY` only into the selected native agent process; values are not
command-line arguments or logs. No API-key adapter is used for ChatGPT browser
OAuth. In the default `auto` mode, an unreviewed OpenCode version skips the
host-browser bridge; explicit `--browser-auth` fails closed.

## Selectors and escrow files

The optional private JSON config is `$MESTER_STATE_DIR/config.json` (the default
state root is `${XDG_DATA_HOME:-$HOME/.local/share}/little-mester`). The default
is `"auth_forward":"off"`; to enable only ChatGPT/OpenAI export/import, use
`"auth_forward":["openai"]`. `"auto"` enables every implemented adapter whose
host escrow is present. An explicit provider list can include `openai`,
`anthropic`, and/or `kimi`. `auth_escrow_dir` defaults to the state root's
`secrets` directory. It must remain outside the checkout and every selected
workspace; the tool creates a missing final directory only beneath an already
private parent. These fields are selectors and paths only, not secret values.

For Anthropic or Kimi, place a private, mode-0600 JSON file named
`anthropic.json` or `kimi.json` inside the escrow directory using the schema
above. Keep the directory mode 0700 and owned by the current user. For OpenAI,
the tool creates the provider file itself from the scoped guest export; do not
manually generate or edit that export. Launch a guest with the qualified
OpenCode version, then use `./lab-vm agent --browser-auth` (or the default
`auto` handoff) for host-browser login. `--no-browser-auth` skips the browser
bridge; it does not disable configured credential import. A login performed
inside the VM remains stored in that profile's overlay.

## Credential escrow and destructive operations

OpenCode's export contains secrets. Never run an unscoped export: with no target
it exports every integration. Host escrow is outside the workspace and checkout,
owned by the current user, mode-restricted, and never mounted into the guest.
Transfers use the verified control SSH channel and stdin. Export is written to a
private temporary file, structurally validated without printing it, fsynced, and
atomically replaced only after every selected export validates. A failed export or
unsafe escrow path blocks the lifecycle delete/rebuild hook before profile
deletion. `suspend` preserves the overlay and does not force an export. The
replacement CLI still leaves `down`/`rebuild` dispatch fail-closed at this
checkpoint. A stopped guest is never woken just to export: resume it explicitly
and retry, preserving its overlay in the meantime.

An escrow import is marked per profile after success and is not repeated over a
guest's newer credentials on each launch. `off` performs no import. The profile
overlay remains the primary persistence boundary; escrow is an explicit optional
copy for destructive lifecycle operations, not a database copy.

Do not put auth exports in a workspace, Git checkout, image, cloud-init seed,
generated config, test fixture, or log. Tests use synthetic credential records
and canary strings only. A real login, refreshed-token proof, browser callback,
and persistence check are owner-controlled acceptance gates and have not been
performed by these fixture tests.
