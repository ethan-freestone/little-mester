#!/usr/bin/env python3
"""Canonical protected guest launchers for persistent OpenCode sessions."""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import stat


GUEST_HELPER = pathlib.Path(__file__).with_name("guest-user-dirs.py").read_bytes()

TUI_LAUNCHER_TEMPLATE = b"""#!/bin/sh
set -eu
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != '--continue' ]; }; then
  printf '%s\\n' 'REFUSED: unsupported protected OpenCode session mode' >&2
  exit 64
fi
/usr/bin/python3 -I /usr/local/libexec/lm-guest-user-dirs.py prepare-auth
umask 077
cd @WORKSPACE@
exec /usr/bin/env -i HOME=/home/mester PATH=/usr/local/bin:/usr/bin:/bin TERM=${TERM:-xterm-256color} LANG=C.UTF-8 TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux XDG_DATA_HOME=/home/mester/.local/share XDG_CONFIG_HOME=/home/mester/.config OPENCODE_CONFIG=/etc/opencode/opencode.json OPENCODE_DISABLE_PROJECT_CONFIG=1 OPENCODE_DISABLE_AUTOUPDATE=1 OPENCODE_DISABLE_SHARE=1 OPENCODE_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 SENTRY_DSN= OTEL_EXPORTER_OTLP_ENDPOINT= OTEL_EXPORTER_OTLP_HEADERS= HTTP_PROXY=http://10.0.2.100:3128 HTTPS_PROXY=http://10.0.2.100:3128 http_proxy=http://10.0.2.100:3128 https_proxy=http://10.0.2.100:3128 NO_PROXY=localhost,127.0.0.1,10.0.2.100 no_proxy=localhost,127.0.0.1,10.0.2.100 /usr/bin/tmux new-session -A -s little-mester-opencode /usr/local/libexec/lm-opencode "$@"
"""
DEFAULT_WORKSPACE = "/home/mester/workspace"
SYSTEM_WORKSPACE = "/workspace"


def assets_for_workspace(workspace_path: str = DEFAULT_WORKSPACE) -> dict:
    if workspace_path not in (DEFAULT_WORKSPACE, SYSTEM_WORKSPACE):
        raise ValueError("protected session workspace path is not one of the fixed guest locations")
    launcher = TUI_LAUNCHER_TEMPLATE.replace(b"@WORKSPACE@", workspace_path.encode("ascii"))
    return {**ASSETS, "/usr/local/bin/lm-opencode": (launcher, "0755")}


TUI_LAUNCHER = TUI_LAUNCHER_TEMPLATE.replace(b"@WORKSPACE@", DEFAULT_WORKSPACE.encode("ascii"))

OPENCODE_RUNNER = b"""#!/bin/sh
set -eu
export XDG_CONFIG_HOME=/etc
PREFERRED_MODEL=$(/usr/bin/python3 -I /usr/local/libexec/lm-guest-opencode-preferences.py model)
if [ -n "$PREFERRED_MODEL" ]; then
  OPENCODE_CONFIG=/etc/opencode/opencode-model.json
  export LM_OPENCODE_MODEL="$PREFERRED_MODEL"
else
  OPENCODE_CONFIG=/etc/opencode/opencode.json
  unset LM_OPENCODE_MODEL
fi
if [ -r /etc/opencode/plugins/little-mester-host-metrics-tui/package.json ]; then
  OPENCODE_CLI_CONFIG_CONTENT=$(/usr/bin/python3 -I /usr/local/libexec/lm-guest-opencode-preferences.py config --host-metrics)
else
  OPENCODE_CLI_CONFIG_CONTENT=$(/usr/bin/python3 -I /usr/local/libexec/lm-guest-opencode-preferences.py config)
fi
export OPENCODE_CONFIG OPENCODE_CLI_CONFIG_CONTENT
export METRICS_MODE=vm
export HTTP_PROXY=http://10.0.2.100:3128 HTTPS_PROXY=http://10.0.2.100:3128
export http_proxy=http://10.0.2.100:3128 https_proxy=http://10.0.2.100:3128
export OPENCODE_DISABLE_PROJECT_CONFIG=1
export OPENCODE_DISABLE_AUTOUPDATE=1 OPENCODE_DISABLE_SHARE=1 OPENCODE_DISABLE_TELEMETRY=1
export DO_NOT_TRACK=1 DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 OTEL_SDK_DISABLED=true
export OTEL_TRACES_EXPORTER=none OTEL_LOGS_EXPORTER=none OTEL_METRICS_EXPORTER=none
export SENTRY_DSN= OTEL_EXPORTER_OTLP_ENDPOINT= OTEL_EXPORTER_OTLP_HEADERS=
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != '--continue' ]; }; then
  printf '%s\\n' 'REFUSED: unsupported protected OpenCode session mode' >&2
  exit 64
fi
exec /usr/local/bin/opencode --standalone "$@"
"""

AUTH_LAUNCHER = b"""#!/bin/sh
set -eu
/usr/bin/python3 -I /usr/local/libexec/lm-guest-user-dirs.py prepare-auth
OPENCODE_CLI_CONFIG_CONTENT=$(/usr/bin/python3 -I /usr/local/libexec/lm-guest-opencode-preferences.py config)
umask 077
exec /usr/bin/env -i HOME=/home/mester PATH=/usr/local/bin TERM=xterm-256color LANG=C.UTF-8 XDG_DATA_HOME=/home/mester/.local/share XDG_CONFIG_HOME=/home/mester/.config OPENCODE_CONFIG=/etc/opencode/opencode.json OPENCODE_DISABLE_PROJECT_CONFIG=1 OPENCODE_DISABLE_AUTOUPDATE=1 OPENCODE_DISABLE_SHARE=1 OPENCODE_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1 SENTRY_DSN= OTEL_EXPORTER_OTLP_ENDPOINT= OTEL_EXPORTER_OTLP_HEADERS= HTTP_PROXY=http://10.0.2.100:3128 HTTPS_PROXY=http://10.0.2.100:3128 http_proxy=http://10.0.2.100:3128 https_proxy=http://10.0.2.100:3128 NO_PROXY=localhost,127.0.0.1,10.0.2.100 no_proxy=localhost,127.0.0.1,10.0.2.100 OPENCODE_CLI_CONFIG_CONTENT="$OPENCODE_CLI_CONFIG_CONTENT" /usr/local/bin/opencode auth login openai --method chatgpt-browser
"""


ASSETS = {
    "/usr/local/bin/lm-opencode": (TUI_LAUNCHER, "0755"),
    "/usr/local/libexec/lm-opencode": (OPENCODE_RUNNER, "0755"),
    "/usr/local/libexec/lm-opencode-auth": (AUTH_LAUNCHER, "0755"),
    "/usr/local/libexec/lm-guest-user-dirs.py": (GUEST_HELPER, "0644"),
    "/usr/local/libexec/lm-guest-opencode-preferences.py":
        (pathlib.Path(__file__).with_name("guest-opencode-preferences.py").read_bytes(), "0644"),
}

INSTALL_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SOURCE_PLUGIN_ROOT = INSTALL_ROOT / "agent/opencode/plugins/little-mester-exit-confirm"
PLUGIN_ROOT = (_SOURCE_PLUGIN_ROOT if _SOURCE_PLUGIN_ROOT.is_dir()
               else pathlib.Path(__file__).with_name("plugins"))
_SOURCE_METRICS_PLUGIN_ROOT = INSTALL_ROOT / "agent/opencode/plugins/host-metrics-tui"
METRICS_PLUGIN_ROOT = (_SOURCE_METRICS_PLUGIN_ROOT if _SOURCE_METRICS_PLUGIN_ROOT.is_dir()
                       else pathlib.Path(__file__).with_name("host-metrics-tui"))
PROTECTED_PLUGIN_ASSETS = {
    f"/etc/opencode/plugins/little-mester-exit-confirm/{name}":
        (PLUGIN_ROOT / name).read_bytes()
    for name in ("package.json", "tui.tsx")
}
METRICS_PLUGIN_ASSETS = {
    f"/etc/opencode/plugins/little-mester-host-metrics-tui/{name}":
        (METRICS_PLUGIN_ROOT / name).read_bytes()
    for name in ("package.json", "tui.tsx", "metrics-endpoint.js", "metrics-endpoint.d.ts")
}
PROTECTED_CONFIG_MODEL = json.dumps({
    "autoupdate": False,
    "share": "disabled",
    "permission": {"edit": "ask", "bash": "ask"},
    "model": "{env:LM_OPENCODE_MODEL}",
}, separators=(",", ":"))
ASSET_SHA256 = {
    path: hashlib.sha256(content).hexdigest()
    for path, (content, _) in ASSETS.items()
}
ASSET_SHA256.update({
    path: hashlib.sha256(content).hexdigest()
    for path, content in PROTECTED_PLUGIN_ASSETS.items()
})


def readiness_checks(*, include_opencode: bool = True,
                     workspace_path: str = DEFAULT_WORKSPACE) -> str:
    checks = ["test -x /usr/bin/tmux || exit 42", "test -x /usr/local/bin/lm-opencode || exit 43"]
    if include_opencode:
        checks.extend([
            "if [ ! -x /usr/local/bin/opencode ]; then printf '%s\\n' 'KVM_M2_READINESS_FAIL check=opencode-executable path=/usr/local/bin/opencode'; exit 44; fi",
            "opencode_version=$(/usr/local/bin/opencode --version 2>/dev/null) || { printf '%s\\n' 'KVM_M2_READINESS_FAIL check=opencode-version-command'; exit 44; }",
            "if [ \"$opencode_version\" != 'opencode v2.2.5' ]; then printf '%s\\n' 'KVM_M2_READINESS_FAIL check=opencode-version expected=opencode-v2.2.5'; exit 44; fi",
        ])
    workspace_assets = assets_for_workspace(workspace_path)
    digests = {path: hashlib.sha256(content).hexdigest()
               for path, (content, _mode) in workspace_assets.items()}
    digests.update({path: digest for path, digest in ASSET_SHA256.items()
                    if path != "/usr/local/bin/lm-opencode"})
    digests["/etc/opencode/opencode-model.json"] = hashlib.sha256(
        PROTECTED_CONFIG_MODEL.encode("utf-8")).hexdigest()
    digests.update({path: hashlib.sha256(content).hexdigest()
                    for path, content in PROTECTED_PLUGIN_ASSETS.items()})
    if workspace_path == SYSTEM_WORKSPACE:
        digests.update({path: hashlib.sha256(content).hexdigest()
                        for path, content in METRICS_PLUGIN_ASSETS.items()})
    for path, digest in sorted(digests.items()):
        checks.append(f"printf '%s  %s\\n' '{digest}' '{path}' | sha256sum -c - >/dev/null || exit 45")
    return "; ".join(checks)


def parent_cloud_init_data(*, host_private: str, host_public: str,
                           control_public: str,
                           hostname: str = "little-mester-parent") -> dict:
    """Compatibility adapter for the legacy parent guest's unchanged base."""
    import importlib.util

    bootstrap_path = pathlib.Path(__file__).with_name("guest-bootstrap.py")
    spec = importlib.util.spec_from_file_location("parent_guest_bootstrap", bootstrap_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("shared guest bootstrap generator is unavailable")
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    data = bootstrap.build_legacy_parent_base(
        name=hostname, uid=1000, ssh_key=control_public.strip())
    return add_protected_session(data, host_private=host_private,
                                 host_public=host_public, control_public=control_public)


def add_protected_session(data: dict, *, host_private: str, host_public: str,
                          control_public: str,
                          workspace_path: str = DEFAULT_WORKSPACE) -> dict:
    """Layer protected SSH, OpenCode, proxy and session assets over a guest base."""
    for value in (host_private, host_public, control_public):
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            raise ValueError("invalid fixed guest SSH key material")
    if not isinstance(data, dict) or not isinstance(data.get("write_files"), list):
        raise ValueError("guest bootstrap base has an invalid write_files collection")
    data["ssh_pwauth"] = False
    data["disable_root"] = True
    data["ssh_deletekeys"] = False
    data["ssh_keys"] = {"ed25519_private": host_private,
                        "ed25519_public": host_public.strip()}
    plugin_files = []
    plugin_destinations = [
        ("package.json", "/etc/opencode/plugins/little-mester-exit-confirm/package.json"),
        ("tui.tsx", "/etc/opencode/plugins/little-mester-exit-confirm/tui.tsx"),
    ]
    if workspace_path == SYSTEM_WORKSPACE:
        plugin_destinations.extend((
            ("package.json", "/etc/opencode/plugins/little-mester-host-metrics-tui/package.json"),
            ("tui.tsx", "/etc/opencode/plugins/little-mester-host-metrics-tui/tui.tsx"),
            ("metrics-endpoint.js", "/etc/opencode/plugins/little-mester-host-metrics-tui/metrics-endpoint.js"),
            ("metrics-endpoint.d.ts", "/etc/opencode/plugins/little-mester-host-metrics-tui/metrics-endpoint.d.ts"),
        ))
    for name, destination in plugin_destinations:
        source_root = (METRICS_PLUGIN_ROOT if "host-metrics-tui" in destination else PLUGIN_ROOT)
        source = source_root / name
        payload = source.read_bytes()
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            raise ValueError(f"protected OpenCode plugin asset is unsafe: {name}")
        plugin_files.append({"path": destination, "owner": "root:root", "permissions": "0644",
                             "encoding": "b64",
                             "content": base64.b64encode(payload).decode("ascii")})
    protected_config = json.dumps(
        {"autoupdate": False, "share": "disabled", "permission": {"edit": "ask", "bash": "ask"}},
        separators=(",", ":"))
    cli_config = json.dumps({
        "plugins": ["/etc/opencode/plugins/little-mester-exit-confirm"],
        "keybinds": {"app.exit": "none", "prompt.clear": "none"},
    }, separators=(",", ":"))
    protected_files = [
            {"path": "/etc/ssh/sshd_config.d/99-little-mester-parent.conf", "owner": "root:root",
             "permissions": "0644", "content": "HostKeyAlgorithms ssh-ed25519\n"},
            {"path": "/etc/profile.d/little-mester-parent.sh", "owner": "root:root",
             "permissions": "0644", "content": "export HTTP_PROXY=http://10.0.2.100:3128\n"
             "export HTTPS_PROXY=http://10.0.2.100:3128\nexport http_proxy=http://10.0.2.100:3128\n"
             "export https_proxy=http://10.0.2.100:3128\n"},
            {"path": "/etc/apt/apt.conf.d/90little-mester-parent-proxy", "owner": "root:root",
             "permissions": "0644", "content": 'Acquire::http::Proxy "http://10.0.2.100:3128";\n'
             'Acquire::https::Proxy "http://10.0.2.100:3128";\n'},
             {"path": "/etc/opencode/opencode.json", "owner": "root:root", "permissions": "0644",
              "content": protected_config},
             {"path": "/etc/opencode/opencode-model.json", "owner": "root:root",
              "permissions": "0644", "content": PROTECTED_CONFIG_MODEL},
            {"path": "/etc/profile.d/little-mester-opencode.sh", "owner": "root:root",
             "permissions": "0644", "content": "export OPENCODE_CONFIG=/etc/opencode/opencode.json\n"
             f"export OPENCODE_CLI_CONFIG_CONTENT='{cli_config}'\n"
             "export OPENCODE_DISABLE_PROJECT_CONFIG=1\n"},
             *[{"path": path, "owner": "root:root", "permissions": mode,
                "content": content.decode("utf-8")} for path, (content, mode)
               in assets_for_workspace(workspace_path).items()],
             *plugin_files,
    ]
    existing_paths = {item.get("path") for item in data["write_files"] if isinstance(item, dict)}
    data["write_files"].extend(item for item in protected_files if item["path"] not in existing_paths)
    return data
