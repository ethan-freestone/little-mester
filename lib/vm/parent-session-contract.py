#!/usr/bin/env python3
"""Canonical protected guest launchers for persistent OpenCode sessions."""

from __future__ import annotations

import hashlib
import pathlib


GUEST_HELPER = pathlib.Path(__file__).with_name("guest-user-dirs.py").read_bytes()

TUI_LAUNCHER = b"""#!/bin/sh
set -eu
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != '--continue' ]; }; then
  printf '%s\\n' 'REFUSED: unsupported protected OpenCode session mode' >&2
  exit 64
fi
/usr/bin/python3 -I /usr/local/libexec/lm-guest-user-dirs.py prepare-auth
umask 077
cd /home/mester/workspace
exec /usr/bin/env -i HOME=/home/mester PATH=/usr/local/bin:/usr/bin:/bin TERM=${TERM:-xterm-256color} LANG=C.UTF-8 TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux XDG_DATA_HOME=/home/mester/.local/share XDG_CONFIG_HOME=/home/mester/.config OPENCODE_CONFIG=/etc/opencode/opencode.json OPENCODE_DISABLE_PROJECT_CONFIG=1 OPENCODE_DISABLE_AUTOUPDATE=1 OPENCODE_DISABLE_SHARE=1 OPENCODE_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 SENTRY_DSN= OTEL_EXPORTER_OTLP_ENDPOINT= OTEL_EXPORTER_OTLP_HEADERS= HTTP_PROXY=http://10.0.2.100:3128 HTTPS_PROXY=http://10.0.2.100:3128 http_proxy=http://10.0.2.100:3128 https_proxy=http://10.0.2.100:3128 NO_PROXY=localhost,127.0.0.1,10.0.2.100 no_proxy=localhost,127.0.0.1,10.0.2.100 OPENCODE_CLI_CONFIG_CONTENT='{"plugins":["/etc/opencode/plugins/little-mester-exit-confirm"],"keybinds":{"app.exit":"none","prompt.clear":"none"}}' /usr/bin/tmux new-session -A -s little-mester-opencode /usr/local/libexec/lm-opencode "$@"
"""

OPENCODE_RUNNER = b"""#!/bin/sh
set -eu
export XDG_CONFIG_HOME=/etc
export OPENCODE_CONFIG=/etc/opencode/opencode.json
export HTTP_PROXY=http://10.0.2.100:3128 HTTPS_PROXY=http://10.0.2.100:3128
export http_proxy=http://10.0.2.100:3128 https_proxy=http://10.0.2.100:3128
export OPENCODE_DISABLE_PROJECT_CONFIG=1
export OPENCODE_DISABLE_AUTOUPDATE=1 OPENCODE_DISABLE_SHARE=1 OPENCODE_DISABLE_TELEMETRY=1
export DO_NOT_TRACK=1 DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 OTEL_SDK_DISABLED=true
export OTEL_TRACES_EXPORTER=none OTEL_LOGS_EXPORTER=none OTEL_METRICS_EXPORTER=none
export SENTRY_DSN= OTEL_EXPORTER_OTLP_ENDPOINT= OTEL_EXPORTER_OTLP_HEADERS=
export OPENCODE_CLI_CONFIG_CONTENT='{"plugins":["/etc/opencode/plugins/little-mester-exit-confirm"],"keybinds":{"app.exit":"none","prompt.clear":"none"}}'
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != '--continue' ]; }; then
  printf '%s\\n' 'REFUSED: unsupported protected OpenCode session mode' >&2
  exit 64
fi
exec /usr/local/bin/opencode --standalone "$@"
"""

AUTH_LAUNCHER = b"""#!/bin/sh
set -eu
/usr/bin/python3 -I /usr/local/libexec/lm-guest-user-dirs.py prepare-auth
umask 077
exec /usr/bin/env -i HOME=/home/mester PATH=/usr/local/bin TERM=xterm-256color LANG=C.UTF-8 XDG_DATA_HOME=/home/mester/.local/share XDG_CONFIG_HOME=/home/mester/.config OPENCODE_CONFIG=/etc/opencode/opencode.json OPENCODE_DISABLE_PROJECT_CONFIG=1 OPENCODE_DISABLE_AUTOUPDATE=1 OPENCODE_DISABLE_SHARE=1 OPENCODE_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 SENTRY_DSN= OTEL_EXPORTER_OTLP_ENDPOINT= OTEL_EXPORTER_OTLP_HEADERS= HTTP_PROXY=http://10.0.2.100:3128 HTTPS_PROXY=http://10.0.2.100:3128 http_proxy=http://10.0.2.100:3128 https_proxy=http://10.0.2.100:3128 NO_PROXY=localhost,127.0.0.1,10.0.2.100 no_proxy=localhost,127.0.0.1,10.0.2.100 OPENCODE_CLI_CONFIG_CONTENT='{"plugins":["/etc/opencode/plugins/little-mester-exit-confirm"],"keybinds":{"app.exit":"none","prompt.clear":"none"}}' /usr/local/bin/opencode auth login openai --method chatgpt-browser
"""


ASSETS = {
    "/usr/local/bin/lm-opencode": (TUI_LAUNCHER, "0755"),
    "/usr/local/libexec/lm-opencode": (OPENCODE_RUNNER, "0755"),
    "/usr/local/libexec/lm-opencode-auth": (AUTH_LAUNCHER, "0755"),
    "/usr/local/libexec/lm-guest-user-dirs.py": (GUEST_HELPER, "0644"),
}

INSTALL_ROOT = pathlib.Path(__file__).resolve().parents[2]
PLUGIN_ROOT = INSTALL_ROOT / "agent/opencode/plugins/little-mester-exit-confirm"
PROTECTED_PLUGIN_ASSETS = {
    f"/etc/opencode/plugins/little-mester-exit-confirm/{name}":
        (PLUGIN_ROOT / name).read_bytes()
    for name in ("package.json", "tui.tsx")
}
ASSET_SHA256 = {
    path: hashlib.sha256(content).hexdigest()
    for path, (content, _) in ASSETS.items()
}
ASSET_SHA256.update({
    path: hashlib.sha256(content).hexdigest()
    for path, content in PROTECTED_PLUGIN_ASSETS.items()
})


def readiness_checks(*, include_opencode: bool = True) -> str:
    checks = ["test -x /usr/bin/tmux || exit 42", "test -x /usr/local/bin/lm-opencode || exit 43"]
    if include_opencode:
        checks.extend([
            "if [ ! -x /usr/local/bin/opencode ]; then printf '%s\\n' 'KVM_M2_READINESS_FAIL check=opencode-executable path=/usr/local/bin/opencode'; exit 44; fi",
            "opencode_version=$(/usr/local/bin/opencode --version 2>/dev/null) || { printf '%s\\n' 'KVM_M2_READINESS_FAIL check=opencode-version-command'; exit 44; }",
            "if [ \"$opencode_version\" != 'opencode v2.2.5' ]; then printf '%s\\n' 'KVM_M2_READINESS_FAIL check=opencode-version expected=opencode-v2.2.5'; exit 44; fi",
        ])
    for path, digest in sorted(ASSET_SHA256.items()):
        checks.append(f"printf '%s  %s\\n' '{digest}' '{path}' | sha256sum -c - >/dev/null || exit 45")
    return "; ".join(checks)
