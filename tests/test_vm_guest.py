"""Fixture-only guest bootstrap, payload hash and readiness tests."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUEST_SH = ROOT / "lib/vm/guest.sh"
BOOTSTRAP = ROOT / "lib/vm/guest-bootstrap.sh"


def run_hash(source, *selectors):
    command = [
        "bash", "-c", 'source "$1"; shift; vm_guest_payload_hash "$@"',
        "_", str(GUEST_SH), str(source), *map(str, selectors),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class GuestReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.digest = "a" * 64

    def validate(self, rc, cloud_status, marker):
        result = subprocess.run(
            [
                "bash", "-c", 'source "$1"; shift; vm_guest_readiness_validate "$@"',
                "_", str(GUEST_SH), str(rc), json.dumps(cloud_status), json.dumps(marker), self.digest,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return result

    def test_cloud_init_recoverable_errors_and_nonzero_exit_block_readiness(self):
        clean = {"status": "done", "extended_status": "done", "errors": [], "recoverable_errors": {}}
        marker = {
            "status": "ready",
            "payload_sha256": self.digest,
            "workspace_mount": "virtiofs",
            "agent": "opencode",
            "mcp_build": True,
            "opencode_version": "2.2.5",
            "aider_version": "aider 0.1.0",
            "model_provider": "none",
            "model_port": None,
            "model_id": "",
        }
        self.assertEqual(self.validate(0, clean, marker).returncode, 0)
        for status in (
            dict(clean, status="degraded done"),
            dict(clean, errors=["module failed"]),
            dict(clean, recoverable_errors={"users-groups": ["recoverable"]}),
            dict(clean, extended_status="error"),
        ):
            with self.subTest(status=status):
                self.assertNotEqual(self.validate(0, status, marker).returncode, 0)
        self.assertNotEqual(self.validate(2, clean, marker).returncode, 0)
        self.assertNotEqual(self.validate(0, clean, dict(marker, payload_sha256="b" * 64)).returncode, 0)

    def test_payload_hash_is_relative_complete_and_mode_sensitive(self):
        first = self.base / "source-a"
        second = self.base / "source-b"
        self.make_hash_tree(first)
        shutil.copytree(first, second, symlinks=True)
        baseline = run_hash(first, "none", "opencode", "latest", 0, 2)
        self.assertEqual(baseline, run_hash(second, "none", "opencode", "latest", 0, 2))

        (first / "little-mester-mcp/src/engine.ts").write_text("changed input\n", encoding="utf-8")
        changed_content = run_hash(first, "none", "opencode", "latest", 0, 2)
        self.assertNotEqual(baseline, changed_content)
        (first / "little-mester-mcp/src/engine.ts").write_text("fixture little-mester-mcp/src/engine.ts\n", encoding="utf-8")
        path = first / "agent/opencode/plugins/host-metrics/index.ts"
        path.chmod(path.stat().st_mode ^ 0o100)
        changed_mode = run_hash(first, "none", "opencode", "latest", 0, 2)
        self.assertNotEqual(baseline, changed_mode)
        path.chmod(path.stat().st_mode ^ 0o100)
        self.assertNotEqual(baseline, run_hash(first, "none", "opencode", "2.2.5", 0, 2))
        self.assertNotEqual(baseline, run_hash(first, "none", "opencode", "latest", 1, 2))
        self.assertNotEqual(baseline, run_hash(first, "none", "opencode", "latest", 0, 3))

        link = first / "agent/opencode/plugins/logo-link.txt"
        link.unlink()
        (self.base / "outside.txt").write_text("outside source tree\n", encoding="utf-8")
        os.symlink("../../../outside.txt", link)
        with self.assertRaises(AssertionError):
            run_hash(first, "none", "opencode", "latest", 0, 2)
        link.unlink()
        os.symlink("../little-mester-logo.txt", link)
        self.assertEqual(baseline, run_hash(first, "none", "opencode", "latest", 0, 2))

        (first / "little-mester-mcp/dist").mkdir()
        (first / "little-mester-mcp/dist/generated.js").write_text("ignored build output\n", encoding="utf-8")
        self.assertEqual(baseline, run_hash(first, "none", "opencode", "latest", 0, 2))
        self.assertNotEqual(baseline, run_hash(first, "docker", "opencode", "latest", 0, 2))

    def make_hash_tree(self, root):
        for name in (
            "lib/vm/cloud-init.sh",
            "lib/vm/guest.sh",
            "lib/vm/guest-bootstrap.sh",
            "scripts/vm-opencode-config.py",
            "scripts/vm-install-tools.sh",
            "scripts/vm-opencode-tui-check.py",
            "scripts/vm-python-tools.sh",
            "scripts/vm-update.sh",
            "agent/opencode/opencode.json",
            "agent/opencode/little-mester-logo.txt",
            "agent/opencode/plugins/host-metrics/index.ts",
            "little-mester-mcp/package.json",
            "little-mester-mcp/package-lock.json",
            "little-mester-mcp/src/engine.ts",
            "little-mester-mcp/docs/ticket-v1.json",
        ):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fixture {name}\n", encoding="utf-8")
        os.symlink("../little-mester-logo.txt", root / "agent/opencode/plugins/logo-link.txt")


class GuestBootstrapFixtures(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.guest = self.base / "guest"
        self.guest.mkdir()
        self.workspace = self.guest / "workspace"
        self.workspace.mkdir()
        for relative in (
            "lib/vm/cloud-init.sh",
            "lib/vm/guest.sh",
            "lib/vm/guest-bootstrap.sh",
            "scripts/vm-opencode-config.py",
            "scripts/vm-install-tools.sh",
            "scripts/vm-opencode-tui-check.py",
            "scripts/vm-python-tools.sh",
            "scripts/vm-update.sh",
        ):
            target = self.workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        (self.workspace / "agent/opencode/plugins").mkdir(parents=True)
        (self.workspace / "little-mester-mcp/src").mkdir(parents=True)
        (self.workspace / "agent/opencode/opencode.json").write_text(
            json.dumps({
                "$schema": "https://opencode.ai/config.json",
                "mcpServers": {
                    "little-mester": {
                        "command": "ts-node",
                        "args": ["src/mcp-server.ts"],
                        "cwd": "/workspace/little-mester-mcp",
                        "env": {"TICKET_DIR": "/workspace/docs/tickets"},
                    }
                },
                "provider": {
                    "ollama": {
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {"baseURL": "http://ollama:11434/v1"},
                        "models": {"qwen3:8b": {}},
                    },
                    "llamacpp": {
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {"baseURL": "http://llamacpp:11434/v1"},
                        "models": {"fixture-coder": {}},
                    },
                },
                "model": "llamacpp/fixture-coder",
                "plugins": [],
                "permissions": [{"action": "edit", "resource": "secrets/*", "effect": "deny"}],
            }, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "agent/opencode/little-mester-logo.txt").write_text("fixture logo\n", encoding="utf-8")
        os.symlink(
            "../little-mester-logo.txt",
            self.workspace / "agent/opencode/plugins/little-mester-logo.txt",
        )
        (self.workspace / "little-mester-mcp/package.json").write_text(
            json.dumps({"name": "fixture-mcp", "scripts": {"build": "tsc"}}), encoding="utf-8"
        )
        (self.workspace / "little-mester-mcp/package-lock.json").write_text("{}\n", encoding="utf-8")
        (self.workspace / "little-mester-mcp/src/mcp-server.ts").write_text("export {}\n", encoding="utf-8")

        etc = self.guest / "etc"
        (etc / "apt/apt.conf.d").mkdir(parents=True)
        (etc / "apt/apt.conf.d/80lab-vm-proxy").write_text("Acquire::http::Proxy \"http://10.0.2.100:3128\";\n", encoding="utf-8")
        (etc / "os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n', encoding="utf-8")
        home = self.guest / "home/mester"
        home.mkdir(parents=True)
        tools = self.guest / "usr/local/sbin/lab-vm-python-tools"
        tools.parent.mkdir(parents=True)
        tools.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            "mkdir -p \"$VM_PYTHON_TOOLS_DIR/bin\"\n"
            "for tool in pip pip3 ruff uv; do printf '#!/bin/sh\\nexit 0\\n' > \"$VM_PYTHON_TOOLS_DIR/bin/$tool\"; chmod 0755 \"$VM_PYTHON_TOOLS_DIR/bin/$tool\"; done\n"
            "cat > \"$VM_PYTHON_TOOLS_DIR/bin/aider\" <<'INNER'\n#!/usr/bin/env bash\ncase \"${1:-}\" in --version) echo 'aider 0.1.0' ;; --help) echo '--model <model>' ;; esac\nINNER\nchmod 0755 \"$VM_PYTHON_TOOLS_DIR/bin/aider\"\n",
            encoding="utf-8",
        )
        tools.chmod(0o755)
        renderer = self.guest / "usr/local/libexec/lab-vm-opencode-config.py"
        renderer.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts/vm-opencode-config.py", renderer)
        shutil.copy2(ROOT / "lib/vm/guest.sh", renderer.parent / "lab-vm-guest-hash.sh")

        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.npm_log = self.base / "npm.log"
        self.apt_log = self.base / "apt.log"
        self.system_log = self.base / "system.log"
        self.write_fake("apt-get", '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$FAKE_APT_LOG"\n')
        self.write_fake("node", '#!/usr/bin/env bash\n[[ "${1:-}" == --version ]] && { echo v22.4.1; exit 0; }\nexit 0\n')
        self.write_fake(
            "npm",
            """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_NPM_LOG"
case "${1:-}" in
            view) printf '"%s"\\n' "${FAKE_OPENCODE_VERSION:-2.2.5}" ;;
  install) exit 0 ;;
  ci)
    if [[ -n "${FAKE_NPM_FAIL_CI_ONCE_FILE:-}" && ! -e "$FAKE_NPM_FAIL_CI_ONCE_FILE" ]]; then
      : > "$FAKE_NPM_FAIL_CI_ONCE_FILE"
      exit 17
    fi
    prefix=''
    while (($#)); do if [[ "$1" == --prefix ]]; then prefix="$2"; shift 2; else shift; fi; done
    mkdir -p "$prefix/dist"
    printf 'fixture build\\n' > "$prefix/dist/mcp-server.js"
    ;;
  run)
    if [[ "${FAKE_NPM_FAIL_BUILD:-0}" == 1 ]]; then exit 23; fi
    prefix=''
    while (($#)); do if [[ "$1" == --prefix ]]; then prefix="$2"; shift 2; else shift; fi; done
    mkdir -p "$prefix/dist"
    printf 'fixture build\\n' > "$prefix/dist/mcp-server.js"
    ;;
  *) exit 2 ;;
esac
""",
        )
        self.write_fake(
            "opencode",
            '#!/usr/bin/env bash\n'
            'if [[ "${1:-}" == --version ]]; then echo "${FAKE_OPENCODE_VERSION:-2.2.5}"; exit 0; fi\n'
            'if [[ "${1:-}" == run && "${2:-}" == --help ]]; then echo "--auto Auto-approve permissions not explicitly denied"; exit 0; fi\n'
            'exit 2\n',
        )
        self.write_fake("docker", '#!/usr/bin/env bash\n[[ "${1:-}" == compose && "${2:-}" == version ]] && { echo "Docker Compose version fixture"; exit 0; }\nexit 2\n')
        self.write_fake("runuser", '#!/usr/bin/env bash\nset -e\nif [[ "${1:-}" == -u ]]; then shift 2; fi\nif [[ "${1:-}" == -- ]]; then shift; fi\nexec "$@"\n')
        self.write_fake("mountpoint", '#!/usr/bin/env bash\nexit 0\n')
        self.write_fake(
            "findmnt",
            '#!/usr/bin/env bash\nif [[ "${3:-}" == FSTYPE ]]; then echo virtiofs; else echo "/workspace virtiofs"; fi\n',
        )
        self.write_fake("stat", '#!/usr/bin/env bash\necho 1000:1000\n')
        self.write_fake("systemctl", '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$FAKE_SYSTEM_LOG"\n')
        self.write_fake("usermod", '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$FAKE_SYSTEM_LOG"\n')
        self.write_fake(
            "curl",
            '#!/usr/bin/env bash\nwhile (($#)); do if [[ "$1" == --output ]]; then shift; printf "fixture public repository key\\n" > "$1"; fi; shift; done\n',
        )
        for name in ("qemu-system-x86_64", "qemu-img", "virtiofsd", "cloud-localds", "cloud-init", "genisoimage", "socat", "bats", "shellcheck"):
            self.write_fake(name, '#!/usr/bin/env bash\necho fixture-tool\n')

    def write_fake(self, name, content):
        path = self.bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def run_bootstrap(
        self, *, runtime="none", agent="opencode", provider="none", model_port="",
        model_id="", depth=0, max_depth=0, extra_env=None, payload_hash=None,
    ):
        env = os.environ.copy()
        env.update({
            "PATH": f"{self.bin}:{env.get('PATH', '')}",
            "VM_GUEST_ROOT": str(self.guest),
            "VM_GUEST_TEST_MODE": "1",
            "VM_GUEST_RUNTIME": runtime,
            "VM_GUEST_NPM_BIN": "npm",
            "VM_GUEST_NODE_BIN": "node",
            "VM_GUEST_OPENCODE_BIN": "opencode",
            "VM_GUEST_AIDER_BIN": str(self.guest / "opt/little-mester/python-tools/bin/aider"),
            "VM_GUEST_APT_GET_BIN": "apt-get",
            "VM_GUEST_CURL_BIN": "curl",
            "VM_GUEST_RUNUSER_BIN": "runuser",
            "VM_GUEST_MOUNTPOINT_BIN": "mountpoint",
            "VM_GUEST_FINDMNT_BIN": "findmnt",
            "VM_GUEST_STAT_BIN": "stat",
            "VM_GUEST_SYSTEMCTL_BIN": "systemctl",
            "VM_GUEST_USERMOD_BIN": "usermod",
            "VM_GUEST_PYTHON_TOOLS": "/usr/local/sbin/lab-vm-python-tools",
            "VM_GUEST_CONFIG_RENDERER": "/usr/local/libexec/lab-vm-opencode-config.py",
            "FAKE_NPM_LOG": str(self.npm_log),
            "FAKE_APT_LOG": str(self.apt_log),
            "FAKE_SYSTEM_LOG": str(self.system_log),
            "FAKE_OPENCODE_VERSION": "2.2.5",
        })
        if extra_env:
            env.update(extra_env)
        expected_hash = payload_hash or run_hash(
            self.workspace, runtime, agent, "latest", depth, max_depth, provider, model_port, model_id
        )
        args = [
            "--runtime", runtime,
            "--agent", agent,
            "--depth", str(depth),
            "--max-depth", str(max_depth),
            "--opencode-version", "latest",
            "--payload-sha256", expected_hash,
            "--uid", "1000",
            "--gid", "1000",
            "--workspace", "/workspace",
            "--source-root", "/workspace",
            "--proxy", "http://10.0.2.100:3128",
            "--model-provider", provider,
            "--model-port", model_port,
            "--model-id", model_id,
        ]
        return subprocess.run(["bash", str(BOOTSTRAP), *args], env=env, capture_output=True, text=True, check=False)

    def test_runtime_none_skips_docker_entirely(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        apt = self.apt_log.read_text(encoding="utf-8").lower()
        self.assertNotIn("docker", apt)
        self.assertFalse((self.guest / "etc/systemd/system/docker.service.d").exists())
        self.assertFalse((self.guest / "home/mester/.docker/config.json").exists())
        self.assertFalse(self.system_log.exists())
        marker = json.loads((self.guest / "var/lib/lab-vm/guest-ready.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["workload_runtime"], "none")
        self.assertFalse(marker["recursion_tools"])

    def test_runtime_docker_installs_rootful_compose_and_explicit_proxy(self):
        docker_config = self.guest / "home/mester/.docker/config.json"
        docker_config.parent.mkdir(parents=True)
        docker_config.write_text(json.dumps({"credsStore": "fixture", "proxies": {"other": {"httpProxy": "keep"}}}), encoding="utf-8")

        result = self.run_bootstrap(runtime="docker")
        self.assertEqual(result.returncode, 0, result.stderr)
        apt_calls = self.apt_log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(any("docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin" in line for line in apt_calls))
        self.assertTrue(all("Acquire::http::Proxy=http://10.0.2.100:3128" in line for line in apt_calls))
        self.assertTrue(all("Acquire::https::Proxy=http://10.0.2.100:3128" in line for line in apt_calls))
        dropin = (self.guest / "etc/systemd/system/docker.service.d/80-lab-vm-proxy.conf").read_text(encoding="utf-8")
        self.assertIn('Environment="HTTP_PROXY=http://10.0.2.100:3128"', dropin)
        self.assertIn('Environment="HTTPS_PROXY=http://10.0.2.100:3128"', dropin)
        self.assertIn('Environment="NO_PROXY=localhost,127.0.0.1,10.0.2.100"', dropin)
        self.assertIn("enable --now docker.service", self.system_log.read_text(encoding="utf-8"))
        self.assertIn("-aG docker mester", self.system_log.read_text(encoding="utf-8"))
        saved = json.loads(docker_config.read_text(encoding="utf-8"))
        self.assertEqual(saved["credsStore"], "fixture")
        self.assertEqual(saved["proxies"]["other"], {"httpProxy": "keep"})
        self.assertEqual(saved["proxies"]["default"]["httpProxy"], "http://10.0.2.100:3128")
        self.assertEqual(saved["proxies"]["default"]["httpsProxy"], "http://10.0.2.100:3128")
        self.assertEqual(docker_config.stat().st_mode & 0o777, 0o600)

    def test_success_marker_blocks_warm_bootstrap_and_install(self):
        first = self.run_bootstrap()
        self.assertEqual(first.returncode, 0, first.stderr)
        before = self.npm_log.read_text(encoding="utf-8")

        second = self.run_bootstrap()
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("successful bootstrap marker already exists", second.stderr)
        self.assertEqual(self.npm_log.read_text(encoding="utf-8"), before)

    def test_native_harness_uses_private_mcp_and_not_an_agent_container(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        private = self.guest / "home/mester/.local/share/little-mester/guest-runtime"
        config = json.loads((self.guest / "home/mester/.config/opencode/opencode.json").read_text(encoding="utf-8"))
        mcp = config["mcp"]["servers"]["little-mester"]
        self.assertEqual(mcp["command"], ["node", "dist/mcp-server.js"])
        self.assertEqual(mcp["cwd"], str(private / "mcp"))
        self.assertNotIn("/workspace/little-mester-mcp", mcp["cwd"])
        self.assertEqual(mcp["environment"]["TICKET_DIR"], "/workspace/docs/tickets")
        self.assertTrue((private / "mcp/package-lock.json").is_file())
        self.assertTrue((private / "mcp/dist/mcp-server.js").is_file())
        logo_link = private / "opencode/plugins/little-mester-logo.txt"
        self.assertTrue(logo_link.is_symlink())
        self.assertEqual(logo_link.read_text(encoding="utf-8"), "fixture logo\n")
        npm_calls = self.npm_log.read_text(encoding="utf-8")
        self.assertIn("ci --prefix", npm_calls)
        self.assertIn("run build --prefix", npm_calls)
        self.assertIn("install --global --no-audit --no-fund @opencode/cli@2.2.5", npm_calls)
        self.assertNotIn("docker run", BOOTSTRAP.read_text(encoding="utf-8"))
        self.assertEqual((self.guest / "home/mester/.config/opencode/opencode.json").stat().st_mode & 0o777, 0o600)

    def test_aider_and_selected_provider_are_recorded_without_unused_local_api_config(self):
        result = self.run_bootstrap(
            agent="aider", provider="ollama", model_port="11434", model_id="qwen3:8b"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = json.loads((self.guest / "var/lib/lab-vm/guest-ready.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["agent"], "aider")
        self.assertEqual(marker["model_provider"], "ollama")
        self.assertEqual(marker["model_port"], 11434)
        self.assertEqual(marker["model_id"], "qwen3:8b")
        config = json.loads((self.guest / "home/mester/.config/opencode/opencode.json").read_text(encoding="utf-8"))
        self.assertEqual(config["model"], "ollama/qwen3:8b")
        self.assertIn("ollama", config["providers"])
        self.assertNotIn("llamacpp", config["providers"])

    def test_latest_resolves_once_even_when_first_attempt_fails(self):
        fail_once = self.base / "fail-ci-once"
        result = self.run_bootstrap(extra_env={"FAKE_NPM_FAIL_CI_ONCE_FILE": str(fail_once)})
        self.assertNotEqual(result.returncode, 0)
        marker = self.guest / "var/lib/lab-vm/guest-ready.json"
        self.assertFalse(marker.exists())
        manifest_path = self.guest / "var/lib/lab-vm/install-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["opencode_version"], "2.2.5")

        result = self.run_bootstrap(extra_env={"FAKE_NPM_FAIL_CI_ONCE_FILE": str(fail_once)})
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.npm_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(line.startswith("view @opencode/cli@latest version --json") for line in calls), 1)
        self.assertEqual(sum("install --global" in line for line in calls), 2)
        self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["opencode_version"], "2.2.5")

    def test_bootstrap_failure_never_writes_success_marker(self):
        result = self.run_bootstrap(extra_env={"FAKE_NPM_FAIL_BUILD": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.guest / "var/lib/lab-vm/guest-ready.json").exists())

    def test_payload_mismatch_refuses_before_package_operations(self):
        result = self.run_bootstrap(payload_hash="d" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs from the host-selected payload hash", result.stderr)
        self.assertFalse(self.apt_log.exists())
        self.assertFalse(self.npm_log.exists())
        self.assertFalse((self.guest / "var/lib/lab-vm/guest-ready.json").exists())

    def test_recursion_tools_are_installed_only_when_recursion_is_enabled(self):
        result = self.run_bootstrap(depth=0, max_depth=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        apt = self.apt_log.read_text(encoding="utf-8")
        for package in ("qemu-system-x86", "qemu-utils", "virtiofsd", "cloud-image-utils", "cloud-init", "genisoimage", "socat", "bats", "shellcheck"):
            self.assertIn(package, apt)
        marker = json.loads((self.guest / "var/lib/lab-vm/guest-ready.json").read_text(encoding="utf-8"))
        self.assertTrue(marker["recursion_tools"])


class PythonToolsTests(unittest.TestCase):
    def test_python_tools_use_an_isolated_venv_and_explicit_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            tools_dir = base / "private-tools"
            fake_python = base / "fake-python"
            fake_python.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == -m && "${2:-}" == venv && "${3:-}" == --help ]]; then exit 0; fi
if [[ "${1:-}" == -m && "${2:-}" == venv ]]; then
  mkdir -p "$3/bin"
  cat > "$3/bin/python" <<'INNER'
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_PIP_LOG"
for tool in pip pip3 ruff uv; do printf '#!/bin/sh\\nexit 0\\n' > "$FAKE_TOOLS_DIR/bin/$tool"; chmod 0755 "$FAKE_TOOLS_DIR/bin/$tool"; done
printf '#!/bin/sh\\nexit 0\\n' > "$FAKE_TOOLS_DIR/bin/aider"; chmod 0755 "$FAKE_TOOLS_DIR/bin/aider"
INNER
  chmod 0755 "$3/bin/python"
fi
""",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            log = base / "pip.log"
            env = os.environ.copy()
            env.update({
                "VM_PYTHON_TOOLS_DIR": str(tools_dir),
                "VM_PYTHON_TOOLS_PYTHON": str(fake_python),
                "FAKE_PIP_LOG": str(log),
                "FAKE_TOOLS_DIR": str(tools_dir),
            })
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/vm-python-tools.sh"), "install"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text(encoding="utf-8")
            self.assertIn("--proxy http://10.0.2.100:3128", calls)
            self.assertIn("aider-chat", calls)
            for tool in ("pip", "pip3", "ruff", "uv", "aider"):
                self.assertTrue((tools_dir / "bin" / tool).is_file())


if __name__ == "__main__":
    unittest.main()
