#!/usr/bin/env python3
"""Canonical protected guest launchers for persistent OpenCode sessions."""

from __future__ import annotations

import hashlib
import pathlib


GUEST_HELPER = pathlib.Path(__file__).with_name("guest-user-dirs.py").read_bytes()

TUI_LAUNCHER = b"""#!/bin/sh
set -eu
/usr/bin/python3 -I /usr/local/libexec/lm-guest-user-dirs.py prepare-auth
umask 077
exec /usr/bin/env -i HOME=/home/mester PATH=/usr/local/bin:/usr/bin:/bin TERM=${TERM:-xterm-256color} LANG=C.UTF-8 TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux XDG_DATA_HOME=/home/mester/.local/share XDG_CONFIG_HOME=/home/mester/.config OPENCODE_CONFIG=/etc/opencode/opencode.json OPENCODE_DISABLE_PROJECT_CONFIG=1 HTTP_PROXY=http://10.0.2.100:3128 HTTPS_PROXY=http://10.0.2.100:3128 http_proxy=http://10.0.2.100:3128 https_proxy=http://10.0.2.100:3128 NO_PROXY=localhost,127.0.0.1,10.0.2.100 no_proxy=localhost,127.0.0.1,10.0.2.100 OPENCODE_CLI_CONFIG_CONTENT='{"plugins":["/etc/opencode/plugins/little-mester-exit-confirm"],"keybinds":{"app.exit":"none","prompt.clear":"none"}}' /usr/bin/tmux new-session -A -s little-mester-opencode /usr/local/libexec/lm-opencode
"""

OPENCODE_RUNNER = b"""#!/bin/sh
set -eu
export XDG_CONFIG_HOME=/etc
export OPENCODE_CONFIG=/etc/opencode/opencode.json
export HTTP_PROXY=http://10.0.2.100:3128 HTTPS_PROXY=http://10.0.2.100:3128
export http_proxy=http://10.0.2.100:3128 https_proxy=http://10.0.2.100:3128
export OPENCODE_DISABLE_PROJECT_CONFIG=1
export OPENCODE_CLI_CONFIG_CONTENT='{"plugins":["/etc/opencode/plugins/little-mester-exit-confirm"],"keybinds":{"app.exit":"none","prompt.clear":"none"}}'
exec /usr/local/bin/opencode
"""

AUTH_LAUNCHER = b"""#!/bin/sh
set -eu
/usr/bin/python3 -I /usr/local/libexec/lm-guest-user-dirs.py prepare-auth
umask 077
exec /usr/bin/env -i HOME=/home/mester PATH=/usr/local/bin TERM=xterm-256color LANG=C.UTF-8 XDG_DATA_HOME=/home/mester/.local/share XDG_CONFIG_HOME=/home/mester/.config OPENCODE_CONFIG=/etc/opencode/opencode.json OPENCODE_DISABLE_PROJECT_CONFIG=1 HTTP_PROXY=http://10.0.2.100:3128 HTTPS_PROXY=http://10.0.2.100:3128 http_proxy=http://10.0.2.100:3128 https_proxy=http://10.0.2.100:3128 NO_PROXY=localhost,127.0.0.1,10.0.2.100 no_proxy=localhost,127.0.0.1,10.0.2.100 OPENCODE_CLI_CONFIG_CONTENT='{"plugins":["/etc/opencode/plugins/little-mester-exit-confirm"],"keybinds":{"app.exit":"none","prompt.clear":"none"}}' /usr/local/bin/opencode auth login openai --method chatgpt-browser
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


def readiness_checks() -> str:
    checks = ["test -x /usr/bin/tmux || exit 42", "test -x /usr/local/bin/lm-opencode || exit 43",
              "test -x /usr/local/bin/opencode || exit 44",
              "/usr/local/bin/opencode --version 2>/dev/null | grep -Fx 'opencode v2.2.5' >/dev/null || exit 44"]
    for path, digest in sorted(ASSET_SHA256.items()):
        checks.append(f"printf '%s  %s\\n' '{digest}' '{path}' | sha256sum -c - >/dev/null || exit 45")
    return "; ".join(checks)
