"""Fixture-only guest update scan/apply tests; never access a live VM."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts/vm-update.sh"


def payload_hash(source: Path) -> str:
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; shift; vm_guest_payload_hash "$@"', "_",
         str(ROOT / "lib/vm/guest.sh"), str(source), "docker", "opencode",
         "latest", "0", "0", "none", "", ""],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class GuestUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.guest = self.base / "guest"
        self.guest.mkdir(mode=0o700)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.proc = self.guest / "proc"
        self.proc.mkdir(parents=True)
        self.workspace = self.guest / "workspace"
        self.workspace.mkdir()
        self.npm_log = self.base / "npm.log"
        self.pip_log = self.base / "pip.log"
        self.pip_state = self.base / "pip-state.json"
        self.apt_log = self.base / "apt-get.log"
        self.apt_drained = self.base / "apt-drained"
        self.version_file = self.base / "opencode-version"
        self.version_file.write_text("2.2.5\n", encoding="utf-8")

        for relative in ("lib/vm", "scripts", "agent/opencode", "little-mester-mcp/src"):
            (self.workspace / relative).mkdir(parents=True, exist_ok=True)
        for relative in (
            "lib/vm/cloud-init.sh", "lib/vm/guest.sh", "lib/vm/guest-bootstrap.sh", "scripts/vm-opencode-config.py",
            "scripts/vm-install-tools.sh", "scripts/vm-opencode-tui-check.py",
            "scripts/vm-python-tools.sh", "scripts/vm-update.sh",
        ):
            destination = self.workspace / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, destination)
            destination.chmod(0o755)
        (self.workspace / "agent/opencode/opencode.json").write_text("{}\n", encoding="utf-8")
        (self.workspace / "little-mester-mcp/package.json").write_text(
            '{"name":"fixture-mcp","version":"1.0.0"}\n', encoding="utf-8"
        )
        (self.workspace / "little-mester-mcp/package-lock.json").write_text("{}\n", encoding="utf-8")
        (self.workspace / "little-mester-mcp/src/index.ts").write_text("export {}\n", encoding="utf-8")
        self.source_canary = self.workspace / "preserve.txt"
        self.source_canary.write_text("source-canary\n", encoding="utf-8")

        self.put_guest_file("usr/local/sbin/lab-vm-guest-bootstrap", ROOT / "lib/vm/guest-bootstrap.sh")
        self.put_guest_file("usr/local/sbin/lab-vm-python-tools", ROOT / "scripts/vm-python-tools.sh")
        self.put_guest_file("usr/local/sbin/lab-vm-update", ROOT / "scripts/vm-update.sh")
        self.put_guest_file("usr/local/sbin/lab-vm-install-tools", ROOT / "scripts/vm-install-tools.sh")
        self.put_guest_file("usr/local/libexec/lab-vm-opencode-config.py", ROOT / "scripts/vm-opencode-config.py")
        self.put_guest_file("usr/local/libexec/lab-vm-guest-hash.sh", ROOT / "lib/vm/guest.sh")
        self.put_guest_file("usr/local/libexec/lab-vm-opencode-tui-check.py", ROOT / "scripts/vm-opencode-tui-check.py")
        (self.guest / "etc/apt/apt.conf.d").mkdir(parents=True)

        tools = self.guest / "opt/little-mester/python-tools/bin"
        tools.mkdir(parents=True)
        self.put_executable(tools / "pip", """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_PIP_LOG"
case "${1:-}" in
  list) cat "$FAKE_PIP_STATE" ;;
  index)
    package="${@: -1}"
    version="$(python3 -c 'import json,os,sys; print(json.loads(os.environ["FAKE_PIP_CANDIDATES"])[sys.argv[1]])' "$package")"
    printf '%s (%s)\\n' "$package" "$version" ;;
  install)
    requirement="${@: -1}"
    [[ "${FAKE_PIP_FAIL:-}" != "$requirement" ]] || exit 19
    python3 - "$FAKE_PIP_STATE" "$requirement" <<'PY'
import json,sys
path,requirement=sys.argv[1:]
name,version=requirement.split("==",1)
data=json.load(open(path,encoding="utf-8"))
for item in data:
    if item["name"].lower().replace("_","-")==name.lower().replace("_","-"):
        item["version"]=version
json.dump(data,open(path,"w",encoding="utf-8"))
PY
    ;;
  *) exit 2 ;;
esac
""")
        for name in ("pip3", "ruff", "uv", "aider"):
            self.put_executable(tools / name, "#!/usr/bin/env bash\nexit 0\n")

        self.put_executable(self.bin / "npm", """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_NPM_LOG"
case "${1:-}" in
  view) printf '\"%s\"\\n' "${FAKE_OPENCODE_CANDIDATE:-2.2.5}" ;;
  install) spec="${@: -1}"; printf '%s\\n' "${spec##*@}" > "$FAKE_VERSION_FILE" ;;
  ci|run)
    action="$1"; prefix=''
    while (($#)); do if [[ "$1" == --prefix ]]; then prefix="$2"; shift 2; else shift; fi; done
    [[ "${FAKE_NPM_FAIL_BUILD:-0}" != 1 || "$action" != run ]] || exit 23
    mkdir -p "$prefix/dist"
    printf 'fixture build\\n' > "$prefix/dist/mcp-server.js" ;;
  *) exit 2 ;;
esac
""")
        self.put_executable(self.bin / "opencode", """#!/usr/bin/env bash
if [[ "${1:-}" == --version ]]; then printf 'OpenCode %s\\n' "$(cat "$FAKE_VERSION_FILE")"; exit 0; fi
if [[ "${1:-}" == run && "${2:-}" == --help ]]; then echo --auto; exit 0; fi
exit 2
""")
        self.put_executable(self.bin / "apt-get", """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_APT_LOG"
if [[ "${@: -1}" == update ]]; then
  for i in $(seq 1 12000); do printf 'metadata line %s\\n' "$i"; done
  : > "$FAKE_APT_DRAINED"
elif [[ " $* " == *" install "* && "${FAKE_APT_FAIL:-0}" == 1 ]]; then exit 17
fi
""")
        self.put_executable(self.bin / "apt", """#!/usr/bin/env bash
[[ "${1:-}" == list && "${2:-}" == --upgradable ]] || exit 2
printf 'Listing...\\n'
printf 'curl/noble-updates 8.0 amd64 [upgradable from: 7.0]\\n'
printf 'docker-ce/noble 28.1 amd64 [upgradable from: 27.0]\\n'
""")
        self.put_executable(self.bin / "docker", """#!/usr/bin/env bash
[[ "${1:-}" == ps && "${2:-}" == --quiet ]] || exit 2
[[ "${FAKE_DOCKER_FAIL:-0}" != 1 ]] || exit 17
printf '%s' "${FAKE_DOCKER_RUNNING:-}"
""")
        self.put_executable(self.bin / "runuser", """#!/usr/bin/env bash
set -e
if [[ "${1:-}" == -u ]]; then shift 2; fi
if [[ "${1:-}" == -- ]]; then shift; fi
exec "$@"
""")

        self.marker = self.guest / "var/lib/lab-vm/guest-ready.json"
        self.marker.parent.mkdir(parents=True)
        self.marker.write_text(json.dumps({
            "schema_version": 1, "status": "ready", "agent": "opencode",
            "workload_runtime": "docker", "payload_sha256": payload_hash(self.workspace),
            "opencode_version": "2.2.5", "opencode_version_selector": "latest",
            "aider_version": "aider 0.1.0", "model_provider": "none", "model_port": None,
            "model_id": "", "workspace_mount": "virtiofs", "mcp_build": True,
            "recursion_tools": False, "depth": 0, "max_depth": 0,
        }) + "\n", encoding="utf-8")
        self.marker.chmod(0o644)
        self.manifest = self.marker.parent / "install-manifest.json"
        self.manifest.write_text('{"schema_version":1}\n', encoding="utf-8")
        self.manifest.chmod(0o600)
        config = self.guest / "home/mester/.config/opencode"
        config.mkdir(parents=True)
        (config / "opencode.json").write_text('{"old":"generated"}\n', encoding="utf-8")
        (config / "cli.json").write_text('{"plugins":[]}\n', encoding="utf-8")
        runtime = self.guest / "home/mester/.local/share/little-mester/guest-runtime"
        (runtime / "opencode").mkdir(parents=True)
        (runtime / "mcp").mkdir()
        (runtime / "opencode/installed-canary").write_text("old-runtime\n", encoding="utf-8")
        self.auth = self.guest / "home/mester/.local/share/opencode/opencode.db"
        self.auth.parent.mkdir(parents=True)
        self.auth.write_bytes(b"synthetic-auth-canary\0")
        self.disk = self.guest / "disk.qcow2"
        self.disk.write_bytes(b"overlay-canary")

        self.env = os.environ.copy()
        self.pip_state.write_text(json.dumps([
            {"name": name, "version": "1.0"} for name in ("pip", "ruff", "uv", "aider-chat")
        ]), encoding="utf-8")
        self.env.update({
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "VM_UPDATE_ROOT": str(self.guest), "VM_UPDATE_TEST_MODE": "1",
            "VM_UPDATE_PROC_ROOT": str(self.proc), "VM_UPDATE_APT_GET_BIN": "apt-get",
            "VM_UPDATE_APT_BIN": "apt", "VM_UPDATE_NPM_BIN": "npm",
            "VM_UPDATE_OPENCODE_BIN": "opencode", "VM_UPDATE_DOCKER_BIN": "docker",
            "VM_UPDATE_UID": "1000", "VM_UPDATE_GID": "1000",
            "FAKE_NPM_LOG": str(self.npm_log), "FAKE_PIP_LOG": str(self.pip_log),
            "FAKE_PIP_STATE": str(self.pip_state),
            "FAKE_APT_LOG": str(self.apt_log), "FAKE_APT_DRAINED": str(self.apt_drained),
            "FAKE_VERSION_FILE": str(self.version_file), "FAKE_OPENCODE_CANDIDATE": "2.2.5",
            "FAKE_PIP_CANDIDATES": json.dumps({n: "1.0" for n in ("pip", "ruff", "uv", "aider-chat")}),
        })

    def put_guest_file(self, relative, source):
        path = self.guest / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)
        path.chmod(0o755)
        return path

    @staticmethod
    def put_executable(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def run_update(self, *args, extra_env=None):
        return subprocess.run(
            ["bash", str(UPDATER), *args], env={**self.env, **(extra_env or {})},
            capture_output=True, text=True, check=False,
        )

    def scan(self, targets="opencode,config,tools", *, auth_enabled="0", extra_env=None):
        result = self.run_update("scan", "--targets", targets, "--auth-enabled", auth_enabled, extra_env=extra_env)
        if result.returncode:
            raise AssertionError(f"scan failed: {result.stderr}\n{result.stdout}")
        return json.loads(result.stdout)

    def apply(self, plan, *, extra_env=None):
        return self.run_update("apply", "--plan", plan["plan_id"], extra_env=extra_env)

    def npm_installs(self):
        if not self.npm_log.exists():
            return []
        return [line for line in self.npm_log.read_text(encoding="utf-8").splitlines() if line.startswith("install ")]

    def test_default_scan_is_read_only_and_uses_setup_payload_hash(self):
        plan = self.scan()
        self.assertEqual(plan["targets_selected"], ["opencode", "config", "tools"])
        self.assertEqual([item["status"] for item in plan["targets"]], ["CURRENT"] * 3)
        config = plan["targets"][1]
        self.assertEqual(config["current_hash"], payload_hash(self.workspace))
        self.assertEqual(config["candidate_hash"], payload_hash(self.workspace))
        self.assertEqual(self.npm_installs(), [])
        self.assertFalse(any(line.startswith("install") for line in self.pip_log.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(self.source_canary.read_text(encoding="utf-8"), "source-canary\n")
        self.assertEqual(self.auth.read_bytes(), b"synthetic-auth-canary\0")

    def test_current_apply_is_a_noop_and_discards_only_the_scan_plan(self):
        plan = self.scan()
        result = self.apply(plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "CURRENT")
        self.assertFalse((self.guest / "run/lab-vm-update" / f"{plan['plan_id']}.json").exists())
        self.assertEqual(self.npm_installs(), [])
        self.assertFalse(any(line.startswith("install") for line in self.pip_log.read_text(encoding="utf-8").splitlines()))

    def test_failed_scan_never_creates_an_applicable_plan(self):
        result = self.run_update(
            "scan", "--targets", "opencode", "--auth-enabled", "0",
            extra_env={"FAKE_OPENCODE_CANDIDATE": "not-a-version"},
        )
        self.assertNotEqual(result.returncode, 0)
        plans = self.guest / "run/lab-vm-update"
        self.assertFalse(plans.exists() and list(plans.glob("*.json")))
        self.assertEqual(self.npm_installs(), [])

    def test_cancel_discards_plan_without_install(self):
        plan = self.scan(extra_env={"FAKE_OPENCODE_CANDIDATE": "2.2.6"})
        result = self.run_update("cancel", "--plan", plan["plan_id"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "CANCELLED")
        self.assertEqual(self.version_file.read_text(encoding="utf-8"), "2.2.5\n")
        self.assertEqual(self.npm_installs(), [])

    def test_unsupported_auth_upgrade_is_blocked_before_install(self):
        plan = self.scan(auth_enabled="1", extra_env={"FAKE_OPENCODE_CANDIDATE": "2.3.0"})
        self.assertEqual(plan["targets"][0]["status"], "BLOCKED")
        result = self.apply(plan)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.version_file.read_text(encoding="utf-8"), "2.2.5\n")
        self.assertEqual(self.npm_installs(), [])
        self.assertEqual(self.auth.read_bytes(), b"synthetic-auth-canary\0")

    def test_apply_rechecks_active_tui_using_full_argv_and_tty(self):
        plan = self.scan(extra_env={"FAKE_OPENCODE_CANDIDATE": "2.2.6"})
        process = self.proc / "999"
        process.mkdir()
        (process / "cmdline").write_bytes(b"opencode\0tui\0")
        (process / "exe").symlink_to("/usr/local/bin/opencode")
        (process / "stat").write_text("999 (opencode) S 1 2 3 34816 7\n", encoding="ascii")
        result = self.apply(plan)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("terminal session blocks", result.stderr)
        self.assertEqual(self.version_file.read_text(encoding="utf-8"), "2.2.5\n")
        self.assertEqual(self.npm_installs(), [])

    def test_apt_scan_drains_full_output_and_reports_exact_current_and_candidate(self):
        plan = self.scan("system")
        self.assertTrue(self.apt_drained.exists())
        self.assertEqual(plan["targets"][0]["status"], "UPDATE")
        self.assertEqual(plan["targets"][0]["packages"], [
            {"name": "curl", "current": "7.0", "candidate": "8.0"},
        ])
        self.assertNotIn("install", self.apt_log.read_text(encoding="utf-8"))

    def test_system_apply_refuses_active_docker_workload(self):
        plan = self.scan("system")
        result = self.apply(plan, extra_env={"FAKE_DOCKER_RUNNING": "container-id"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("workloads block", result.stderr)
        self.assertNotIn("install -y", self.apt_log.read_text(encoding="utf-8"))

    def test_docker_apply_uses_only_the_scanned_docker_candidate(self):
        plan = self.scan("docker")
        self.assertEqual(plan["targets"][0]["packages"], [
            {"name": "docker-ce", "current": "27.0", "candidate": "28.1"},
        ])
        result = self.apply(plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        installs = [line for line in self.apt_log.read_text(encoding="utf-8").splitlines() if "install" in line]
        self.assertEqual(len(installs), 1)
        self.assertIn("docker-ce=28.1", installs[0])
        self.assertNotIn("curl=8.0", installs[0])

    def test_tools_apply_is_selected_only_and_exact_versioned(self):
        candidates = {"pip": "2.0", "ruff": "1.0", "uv": "1.0", "aider-chat": "1.0"}
        extra = {"FAKE_PIP_CANDIDATES": json.dumps(candidates)}
        plan = self.scan("tools", extra_env=extra)
        changed = [item for item in plan["targets"][0]["packages"] if item["current"] != item["candidate"]]
        self.assertEqual(changed, [{"name": "pip", "current": "1.0", "candidate": "2.0"}])
        result = self.apply(plan, extra_env=extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        installs = [line for line in self.pip_log.read_text(encoding="utf-8").splitlines() if line.startswith("install")]
        self.assertEqual(len(installs), 1)
        self.assertIn("pip==2.0", installs[0])
        self.assertFalse(self.npm_log.exists())

    def test_config_apply_uses_shared_hash_and_preserves_auth_workspace_and_overlay(self):
        (self.workspace / "agent/opencode/opencode.json").write_text('{"plugins":[]}\n', encoding="utf-8")
        expected = payload_hash(self.workspace)
        plan = self.scan("config")
        self.assertEqual(plan["targets"][0]["candidate_hash"], expected)
        self.assertEqual(plan["targets"][0]["status"], "UPDATE")
        result = self.apply(plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((self.guest / "var/lib/lab-vm/update-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["payload_sha256"], expected)
        self.assertEqual(self.source_canary.read_text(encoding="utf-8"), "source-canary\n")
        self.assertEqual(self.auth.read_bytes(), b"synthetic-auth-canary\0")
        self.assertEqual(self.disk.read_bytes(), b"overlay-canary")
        self.assertTrue((self.guest / "home/mester/.local/share/little-mester/guest-runtime/mcp/dist/mcp-server.js").is_file())

    def test_config_failure_retains_previous_runtime_config_auth_and_source(self):
        (self.workspace / "agent/opencode/opencode.json").write_text('{"plugins":[]}\n', encoding="utf-8")
        runtime_file = self.guest / "home/mester/.local/share/little-mester/guest-runtime/opencode/installed-canary"
        config_file = self.guest / "home/mester/.config/opencode/opencode.json"
        old_runtime, old_config = runtime_file.read_bytes(), config_file.read_bytes()
        plan = self.scan("config")
        result = self.apply(plan, extra_env={"FAKE_NPM_FAIL_BUILD": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(runtime_file.read_bytes(), old_runtime)
        self.assertEqual(config_file.read_bytes(), old_config)
        self.assertEqual(self.auth.read_bytes(), b"synthetic-auth-canary\0")
        self.assertEqual(self.source_canary.read_text(encoding="utf-8"), "source-canary\n")
        self.assertFalse((self.guest / "var/lib/lab-vm/update-state.json").exists())

    def test_opencode_apply_commits_exact_version_without_touching_auth(self):
        plan = self.scan("opencode", extra_env={"FAKE_OPENCODE_CANDIDATE": "2.2.6"})
        result = self.apply(plan, extra_env={"FAKE_OPENCODE_CANDIDATE": "2.2.6"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"status": "UPDATED", "opencode_version": "2.2.6"})
        self.assertEqual(json.loads(self.marker.read_text())["opencode_version"], "2.2.6")
        self.assertEqual(json.loads(self.manifest.read_text())["opencode_version"], "2.2.6")
        self.assertEqual(self.auth.read_bytes(), b"synthetic-auth-canary\0")
        self.assertEqual(self.disk.read_bytes(), b"overlay-canary")

    def test_tools_partial_failure_retains_auth_workspace_and_profile_artifacts(self):
        candidates = {"pip": "2.0", "ruff": "2.0", "uv": "1.0", "aider-chat": "1.0"}
        extra = {"FAKE_PIP_CANDIDATES": json.dumps(candidates)}
        plan = self.scan("tools", extra_env=extra)
        result = self.apply(plan, extra_env={**extra, "FAKE_PIP_FAIL": "ruff==2.0"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PARTIAL", result.stderr)
        self.assertEqual(self.auth.read_bytes(), b"synthetic-auth-canary\0")
        self.assertEqual(self.source_canary.read_text(encoding="utf-8"), "source-canary\n")
        self.assertEqual(self.disk.read_bytes(), b"overlay-canary")

    def test_warm_lifecycle_paths_do_not_invoke_updater(self):
        lifecycle = (ROOT / "lib/vm/lifecycle.sh").read_text(encoding="utf-8")
        up = lifecycle.split("vm_lifecycle_up() {", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("vm_lifecycle_update", up)
        self.assertNotIn("lab-vm-update", up)
        validators = (
            r"""plan_id="\$\(printf .*? -c '\n(.*?)\n' "\$targets_csv"\)""",
            r"""printf '%s\\n' "\$scan_json" \| "\$VM_LIFECYCLE_PYTHON" -c '\n(.*?)\n'\n""",
        )
        for pattern in validators:
            match = re.search(pattern, lifecycle, re.DOTALL)
            self.assertIsNotNone(match, "lifecycle update response validator was not found")
            compile(match.group(1), "<lifecycle-update>", "exec")


if __name__ == "__main__":
    unittest.main()
