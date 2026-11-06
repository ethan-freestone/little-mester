"""Fixture-only exact-version tests for the selected guest tools installer."""

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/vm-install-tools.sh"


class InstallToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.tools = self.base / "tools"
        self.bin = self.tools / "bin"
        self.bin.mkdir(parents=True)
        self.state = self.base / "installed.json"
        self.log = self.base / "pip.log"
        self.state.write_text(json.dumps([
            {"name": name, "version": "1.0"}
            for name in ("pip", "ruff", "uv", "aider-chat")
        ]), encoding="utf-8")
        self.executable(self.bin / "pip", """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_PIP_LOG"
case "${1:-}" in
  list) cat "$FAKE_PIP_STATE" ;;
  install)
    requirement="${@: -1}"
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
            self.executable(self.bin / name, "#!/usr/bin/env bash\nexit 0\n")
        self.plan = self.base / "plan.json"
        self.plan.write_text(json.dumps({
            "schema_version": 1,
            "created": int(time.time()),
            "targets_selected": ["tools"],
            "targets": [{
                "name": "tools",
                "status": "UPDATE",
                "packages": [
                    {"name": "pip", "current": "1.0", "candidate": "2.0"},
                    {"name": "ruff", "current": "1.0", "candidate": "1.0"},
                    {"name": "uv", "current": "1.0", "candidate": "1.0"},
                    {"name": "aider-chat", "current": "1.0", "candidate": "1.0"},
                ],
            }],
        }), encoding="utf-8")
        self.plan.chmod(0o600)
        self.env = os.environ.copy()
        self.env.update({
            "VM_PYTHON_TOOLS_DIR": str(self.tools),
            "VM_PYTHON_TOOLS_TEST_MODE": "1",
            "FAKE_PIP_STATE": str(self.state),
            "FAKE_PIP_LOG": str(self.log),
        })

    @staticmethod
    def executable(path, content):
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def run_installer(self, plan=None):
        return subprocess.run(
            ["bash", str(INSTALLER), str(plan or self.plan)],
            env=self.env, capture_output=True, text=True, check=False,
        )

    def test_only_changed_tool_is_installed_at_scanned_exact_version(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        installs = [line for line in self.log.read_text(encoding="utf-8").splitlines() if line.startswith("install")]
        self.assertEqual(len(installs), 1)
        self.assertIn("pip==2.0", installs[0])
        self.assertNotIn("ruff==", installs[0])
        state = {item["name"]: item["version"] for item in json.loads(self.state.read_text())}
        self.assertEqual(state["pip"], "2.0")
        self.assertEqual(state["ruff"], "1.0")

    def test_current_plan_is_noop_without_package_install(self):
        current_plan = self.base / "current.json"
        data = json.loads(self.plan.read_text())
        for item in data["targets"][0]["packages"]:
            item["candidate"] = item["current"]
        current_plan.write_text(json.dumps(data), encoding="utf-8")
        current_plan.chmod(0o600)
        result = self.run_installer(current_plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CURRENT", result.stdout)
        self.assertFalse(any(line.startswith("install") for line in self.log.read_text(encoding="utf-8").splitlines()))

    def test_drift_from_scanned_current_or_candidate_refuses_install(self):
        state = json.loads(self.state.read_text())
        state[0]["version"] = "3.0"
        self.state.write_text(json.dumps(state), encoding="utf-8")
        result = self.run_installer()
        self.assertEqual(result.returncode, 2)
        self.assertIn("changed after scan", result.stderr)
        self.assertFalse(any(line.startswith("install") for line in self.log.read_text(encoding="utf-8").splitlines()))


if __name__ == "__main__":
    unittest.main()
