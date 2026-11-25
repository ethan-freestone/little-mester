#!/usr/bin/env python3
"""Non-host policy, CLI and QMP protocol tests for the systemd VM slice."""

from __future__ import annotations

import base64
import contextlib
import datetime
import hashlib
import http.client
import importlib.machinery
import importlib.util
import inspect
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import unittest
import urllib.request
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name: str, path: pathlib.Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vm = load("system_vm", ROOT / "lib/vm/system-vm.py")
owner = load("vm_owner_acceptance", ROOT / "tools/vm-owner-acceptance")
system_installer = load("vm_system_install", ROOT / "tools/vm-system-install")
contract = load("parent_session_contract", ROOT / "lib/vm/parent-session-contract.py")
bootstrap = load("guest_bootstrap", ROOT / "lib/vm/guest-bootstrap.py")
acceptance = load("system_acceptance", ROOT / "lib/vm/system-acceptance.py")


def cloud_init_result(status, *, return_code=0, extended_status=None,
                      errors=None, recoverable_errors=None):
    return return_code, json.dumps({
        "status": status,
        "extended_status": status if extended_status is None else extended_status,
        "errors": [] if errors is None else errors,
        "recoverable_errors": {} if recoverable_errors is None else recoverable_errors,
    }).encode()


class SystemdPolicyTests(unittest.TestCase):
    def test_loading_update_transaction_does_not_write_into_immutable_tree(self):
        with tempfile.TemporaryDirectory(prefix="vm-immutable-import-") as temporary:
            root = pathlib.Path(temporary)
            helper = root / "lib/vm/code-update-transaction.py"
            helper.parent.mkdir(parents=True)
            helper.write_text("sentinel = 'loaded'\n", encoding="utf-8")

            module = system_installer._code_update_transaction(root)

            self.assertEqual(module.sentinel, "loaded")
            self.assertTrue(system_installer.sys.dont_write_bytecode)
            self.assertFalse((helper.parent / "__pycache__").exists())

    def test_workspace_policy_loader_registers_dataclass_module_before_execution(self):
        with tempfile.TemporaryDirectory(prefix="vm-workspace-policy-import-") as temporary:
            root = pathlib.Path(temporary)
            runtime = root / "lib/vm/system-vm-workspace.py"
            runtime.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / "lib/vm/system-vm-workspace.py", runtime)
            real_lstat = system_installer.os.lstat

            def protected_lstat(path):
                info = real_lstat(path)
                mode = info.st_mode
                if stat.S_ISDIR(mode):
                    mode = stat.S_IFDIR | 0o755
                elif stat.S_ISREG(mode):
                    mode = stat.S_IFREG | 0o644
                return mock.Mock(st_mode=mode, st_uid=0, st_nlink=1)

            with mock.patch.object(system_installer, "WORKSPACE_RUNTIME", runtime), \
                    mock.patch.object(system_installer.os, "lstat", side_effect=protected_lstat), \
                    mock.patch.dict(sys.modules):
                sys.modules.pop("fixed_system_workspace_policy", None)
                policy = system_installer._load_workspace_policy()

                self.assertIs(sys.modules["fixed_system_workspace_policy"], policy)
                workspace = policy.Workspace(pathlib.Path("/workspace"), (1, 2), 1000)
                self.assertEqual(workspace.path, pathlib.Path("/workspace"))

    def test_workspace_daemon_acceptance_checks_kernel_nnp_not_unit_property(self):
        active = {"ActiveState": "active", "MainPID": "123"}
        with mock.patch.object(acceptance, "service_state", return_value=active), \
                mock.patch.object(pathlib.Path, "read_text", return_value="Name:\tvirtiofsd\nNoNewPrivs:\t0\n"):
            acceptance._verify_workspace_daemon_privileges()
        with mock.patch.object(acceptance, "service_state", return_value=active), \
                mock.patch.object(pathlib.Path, "read_text", return_value="NoNewPrivs:\t1\n"), \
                self.assertRaisesRegex(acceptance.AcceptanceError, "effective NoNewPrivs"):
            acceptance._verify_workspace_daemon_privileges()

    def test_static_verifier_accepts_unassigned_workspace_only_without_20_dropin(self):
        with tempfile.TemporaryDirectory(prefix="vm-unassigned-workspace-") as temporary:
            assignment = pathlib.Path(temporary) / "workspace.json"
            dropin = pathlib.Path(temporary) / "20-workspace.conf"
            policy = type("Policy", (), {"ASSIGNMENT": assignment})()
            with mock.patch.object(acceptance, "_WORKSPACE_DROPIN_PATH", str(dropin)):
                self.assertIsNone(acceptance._workspace_assignment_for_verify(policy))
                self.assertTrue(acceptance._workspace_dropin_matches(policy, None))
                guard = acceptance._FIXED_GUARD_PATHS[acceptance.WORKSPACE_UNIT]
                self.assertTrue(acceptance._exact_dropin_paths(guard, {guard}))
                dropin.write_text("[Service]\nBindPaths=/workspace:/share\n", encoding="utf-8")
                self.assertFalse(acceptance._workspace_dropin_matches(policy, None))
                self.assertFalse(acceptance._exact_dropin_paths(
                    guard + " " + str(dropin), {guard}))

    def test_live_acceptance_still_requires_a_workspace_assignment(self):
        with tempfile.TemporaryDirectory(prefix="vm-live-assignment-") as temporary:
            path = str(pathlib.Path(temporary) / "workspace.json")
            stopped = {"ActiveState": "inactive", "MainPID": "0"}
            with mock.patch.object(acceptance, "_WORKSPACE_ASSIGNMENT_PATH", path), \
                 mock.patch.object(acceptance, "verify", return_value=(stopped, stopped)), \
                 mock.patch.object(acceptance, "_lifecycle_preflight") as lifecycle, \
                    self.assertRaisesRegex(acceptance.AcceptanceError, "requires an assigned workspace"):
                acceptance.accept(pathlib.Path("/opt/candidate"))
            lifecycle.assert_not_called()

    def test_static_verifier_requires_assigned_workspace_dropin_bytes(self):
        with tempfile.TemporaryDirectory(prefix="vm-assigned-workspace-") as temporary:
            dropin = pathlib.Path(temporary) / "20-workspace.conf"
            expected = b"[Service]\nBindPaths=/workspace:/run/share\n"
            dropin.write_bytes(expected)
            assignment = type("Assignment", (), {"path": pathlib.Path("/workspace")})()
            policy = type("Policy", (), {
                "_systemd_bind_path": lambda _self, _path: expected.decode("utf-8"),
            })()
            root_file = type("Info", (), {
                "st_mode": stat.S_IFREG | 0o644, "st_uid": 0, "st_gid": 0, "st_nlink": 1,
            })()
            with mock.patch.object(acceptance, "_WORKSPACE_DROPIN_PATH", str(dropin)), \
                 mock.patch.object(acceptance.os, "lstat", return_value=root_file):
                self.assertTrue(acceptance._workspace_dropin_matches(policy, assignment))
                dropin.write_bytes(b"[Service]\nBindPaths=/other:/run/share\n")
                self.assertFalse(acceptance._workspace_dropin_matches(policy, assignment))

    def test_system_installer_discovers_root_from_protected_installed_location(self):
        for prefix in (pathlib.Path("/opt/little-mester"),
                       pathlib.Path("/srv/little-mester-ci")):
            with self.subTest(prefix=prefix):
                root = prefix / "v3" / ("a" * 40)
                entry = root / "tools/vm-system-install"

                def safe_lstat(path):
                    if pathlib.Path(path) == entry:
                        return type("Info", (), {
                            "st_mode": stat.S_IFREG | 0o755, "st_uid": 0,
                        })()
                    return type("Info", (), {
                        "st_mode": stat.S_IFDIR | 0o755, "st_uid": 0,
                    })()

                with mock.patch.object(system_installer, "__file__", str(entry)), \
                     mock.patch.object(system_installer.os, "lstat", side_effect=safe_lstat):
                    self.assertEqual(system_installer.source_root(), root)

    def test_system_installer_refuses_unsafe_alternate_prefix_ancestry(self):
        prefix = pathlib.Path("/srv/little-mester-ci")
        root = prefix / "v3" / ("b" * 40)
        entry = root / "tools/vm-system-install"

        def unsafe_lstat(path):
            path = pathlib.Path(path)
            if path == prefix:
                return type("Info", (), {
                    "st_mode": stat.S_IFDIR | 0o777, "st_uid": 0,
                })()
            if path == entry:
                return type("Info", (), {
                    "st_mode": stat.S_IFREG | 0o755, "st_uid": 0,
                })()
            return type("Info", (), {
                "st_mode": stat.S_IFDIR | 0o755, "st_uid": 0,
            })()

        with mock.patch.object(system_installer, "__file__", str(entry)), \
             mock.patch.object(system_installer.os, "lstat", side_effect=unsafe_lstat), \
             self.assertRaisesRegex(RuntimeError, "ancestry must be root-owned"):
            system_installer.source_root()

    def test_installer_cli_dispatches_exact_update_and_rollback_arguments(self):
        root = pathlib.Path("/opt/little-mester/v3/") / ("a" * 40)
        with mock.patch.object(system_installer.os, "geteuid", return_value=0), \
             mock.patch.object(system_installer, "source_root", return_value=root), \
             mock.patch.object(system_installer, "verify_source"), \
             mock.patch.object(system_installer.pwd, "getpwnam"), \
             mock.patch.dict(system_installer.os.environ, {"SUDO_USER": "owner"}), \
             mock.patch.object(system_installer.sys, "argv", [
                 str(ROOT / "tools/vm-system-install"), "--update-code-only",
                 "--previous-sha", "b" * 40]), \
             mock.patch.object(system_installer, "update_code_only") as update, \
             mock.patch.object(system_installer, "rollback_code_only") as rollback, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(system_installer.main(), 0)
        update.assert_called_once_with(root, "owner", "b" * 40)
        rollback.assert_not_called()

        with mock.patch.object(system_installer.os, "geteuid", return_value=0), \
             mock.patch.object(system_installer, "source_root", return_value=root), \
             mock.patch.object(system_installer, "verify_source"), \
             mock.patch.object(system_installer.pwd, "getpwnam"), \
             mock.patch.dict(system_installer.os.environ, {"SUDO_USER": "owner"}), \
             mock.patch.object(system_installer.sys, "argv", [
                 str(ROOT / "tools/vm-system-install"), "--rollback-code-only",
                 "--candidate-sha", "a" * 40]), \
             mock.patch.object(system_installer, "update_code_only") as update, \
             mock.patch.object(system_installer, "rollback_code_only") as rollback, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(system_installer.main(), 0)
        rollback.assert_called_once_with(root, "owner", "a" * 40)
        update.assert_not_called()

    def test_installer_and_runtime_dynamic_imports_do_not_write_bytecode_without_b(self):
        probe = textwrap.dedent("""\
            import pathlib
            import sys

            if sys.dont_write_bytecode:
                raise SystemExit("probe unexpectedly started with bytecode disabled")
            source = pathlib.Path(sys.argv[1])
            root = pathlib.Path(sys.argv[2])
            operation = sys.argv[3]
            namespace = {"__file__": str(source), "__name__": "bytecode_probe"}
            exec(compile(source.read_bytes(), str(source), "exec"), namespace)
            if operation == "installer":
                namespace["_code_update_transaction"](root)
            else:
                namespace["_guest_compatibility_module"]()
            if not sys.dont_write_bytecode:
                raise SystemExit("entrypoint did not suppress bytecode before dynamic imports")
            print("BYTECODE_SUPPRESSION=PASS")
        """)
        cases = (
            ("installer", "tools/vm-system-install", "lib/vm/code-update-transaction.py"),
            ("runtime", "lib/vm/system-vm.py", "lib/vm/guest-compatibility.py"),
        )
        with tempfile.TemporaryDirectory(prefix="vm-bytecode-entrypoint-") as temporary:
            base = pathlib.Path(temporary)
            for operation, source_relative, helper_relative in cases:
                with self.subTest(operation=operation):
                    root = base / operation
                    source = root / source_relative
                    helper = root / helper_relative
                    source.parent.mkdir(parents=True)
                    helper.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / source_relative, source)
                    shutil.copyfile(ROOT / helper_relative, helper)
                    command = [sys.executable, "-I", "-c", probe,
                               str(source), str(root), operation]
                    self.assertNotIn("-B", command)
                    result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            text=True, timeout=20, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "BYTECODE_SUPPRESSION=PASS")
                    self.assertEqual(list(root.rglob("__pycache__")), [])
                    self.assertEqual(list(root.rglob("*.pyc")), [])

    def test_workspace_unit_owner_is_substituted_into_launcher_and_user(self):
        relative = "systemd/little-mester-vm-workspace.service"
        with mock.patch.object(system_installer.pwd, "getpwnam",
                               return_value=type("Account", (), {"pw_gid": 42})()), \
             mock.patch.object(system_installer.grp, "getgrgid",
                               return_value=type("Group", (), {"gr_name": "owner-group"})()):
            payload = system_installer.source_payload(ROOT, relative, "owner").decode()
        self.assertIn("User=owner\n", payload)
        self.assertIn("Group=owner-group\n", payload)
        self.assertIn("locked-service workspace --owner=owner\n", payload)
        self.assertNotIn("@LM_VM_OWNER", payload)

    def test_update_gate_verifies_effective_locked_execstart_and_guard(self):
        unit = "little-mester-vm.service"
        values = {
            "DropInPaths": f"/etc/systemd/system/{unit}.d/90-code-update-guard.conf",
            "ExecStart": ("{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -I "
                          "/usr/lib/little-mester-vm/system-vm.py locked-service qemu ; "
                          "ignore_errors=no ; start_time=[n/a] }"),
            "ExecStartPre": (
                "{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -I "
                "/usr/lib/little-mester-vm/system-vm.py check ; ignore_errors=no ; start_time=[n/a] } ; "
                "{ path=/usr/bin/flock ; argv[]=/usr/bin/flock --shared --nonblock "
                "/etc/little-mester-vm/code-update.lock /usr/bin/true ; "
                "ignore_errors=no ; start_time=[n/a] }"),
            "ConditionPathExists": "!/etc/little-mester-vm/code-update.in-progress",
            "BindPaths": "",
            "MainPID": "0",
        }

        def show(command, **_kwargs):
            property_name = next(arg.split("=", 1)[1] for arg in command
                                 if arg.startswith("--property="))
            return subprocess.CompletedProcess(command, 0, stdout=values[property_name], stderr="")

        with mock.patch.object(system_installer.subprocess, "run", side_effect=show), \
             mock.patch.object(system_installer.pwd, "getpwnam",
                               return_value=type("Account", (), {"pw_name": "owner"})()):
            system_installer._verify_effective_unit_guard(unit, "owner", require_locked_launcher=True)
            values["ExecStart"] = "{ path=/usr/bin/qemu-system-x86_64 ; argv[]=/usr/bin/qemu-system-x86_64 ; ignore_errors=no }"
            # A legacy direct-exec unit is allowed during pre-replacement
            # guard installation, but not at successful completion.
            system_installer._verify_effective_unit_guard(unit, "owner")
            with self.assertRaisesRegex(RuntimeError, "locked-service launcher"):
                system_installer._verify_effective_unit_guard(unit, "owner", require_locked_launcher=True)

    def test_fixed_installer_requires_exact_guard_dropin_sets(self):
        guard = "/etc/systemd/system/little-mester-vm.service.d/90-code-update-guard.conf"
        self.assertTrue(system_installer._exact_effective_dropins(guard, {guard}))
        self.assertFalse(system_installer._exact_effective_dropins(
            guard + " /etc/systemd/system/little-mester-vm.service.d/extra.conf", {guard}))
        self.assertFalse(system_installer._exact_effective_dropins(guard + " " + guard, {guard}))

    def test_unassigned_workspace_installer_layout_requires_no_bind_dropin(self):
        guard = "/etc/systemd/system/little-mester-vm-workspace.service.d/90-code-update-guard.conf"
        with tempfile.TemporaryDirectory(prefix="vm-installer-unassigned-") as temporary:
            dropin = pathlib.Path(temporary) / "20-workspace.conf"
            policy = type("Policy", (), {})()
            with mock.patch.object(system_installer, "WORKSPACE_DROPIN", dropin), \
                 mock.patch.object(system_installer, "_workspace_assignment_and_policy",
                                   return_value=(policy, None)):
                expected, assignment, _policy = system_installer._verify_workspace_dropin("owner")
                self.assertEqual(expected, {guard})
                self.assertIsNone(assignment)
                dropin.write_text("[Service]\nBindPaths=/workspace:/share\n", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "must not have"):
                    system_installer._verify_workspace_dropin("owner")

    def test_assigned_workspace_installer_requires_exact_dropin_bytes_and_metadata(self):
        with tempfile.TemporaryDirectory(prefix="vm-installer-assigned-") as temporary:
            dropin = pathlib.Path(temporary) / "20-workspace.conf"
            expected_bytes = b"[Service]\nBindPaths=/workspace:/run/share\n"
            dropin.write_bytes(expected_bytes)
            assignment = type("Assignment", (), {"path": pathlib.Path("/workspace")})()
            policy = type("Policy", (), {
                "_systemd_bind_path": lambda _self, _path: expected_bytes.decode("utf-8"),
            })()
            root_file = type("Info", (), {
                "st_mode": stat.S_IFREG | 0o644, "st_uid": 0, "st_gid": 0, "st_nlink": 1,
            })()
            with mock.patch.object(system_installer, "WORKSPACE_DROPIN", dropin), \
                 mock.patch.object(system_installer, "_workspace_assignment_and_policy",
                                   return_value=(policy, assignment)), \
                 mock.patch.object(system_installer.os, "lstat", return_value=root_file):
                expected, observed_assignment, _policy = system_installer._verify_workspace_dropin("owner")
                self.assertIn(str(dropin), expected)
                self.assertIs(observed_assignment, assignment)
                dropin.write_bytes(b"[Service]\nBindPaths=/other:/run/share\n")
                with self.assertRaisesRegex(RuntimeError, "disagree"):
                    system_installer._verify_workspace_dropin("owner")

    def test_installer_verifies_unassigned_workspace_unit_is_stopped_and_unbound(self):
        unit = "little-mester-vm-workspace.service"
        guard = f"/etc/systemd/system/{unit}.d/90-code-update-guard.conf"
        lock = ["/usr/bin/flock", "--shared", "--nonblock",
                "/etc/little-mester-vm/code-update.lock", "/usr/bin/true"]
        values = {
            "DropInPaths": guard,
            "ExecStart": "",
            "ExecStartPre": ("{ path=/usr/bin/flock ; argv[]=" + " ".join(lock)
                              + " ; ignore_errors=no ; start_time=[n/a] }"),
            "ConditionPathExists": "!/etc/little-mester-vm/code-update.in-progress",
            "BindPaths": "",
            "MainPID": "0",
        }

        def show(command, **_kwargs):
            property_name = next(arg.split("=", 1)[1] for arg in command
                                 if arg.startswith("--property="))
            return subprocess.CompletedProcess(command, 0, stdout=values[property_name], stderr="")

        policy = type("Policy", (), {})()
        with mock.patch.object(system_installer.subprocess, "run", side_effect=show), \
             mock.patch.object(system_installer, "_verify_workspace_dropin",
                               return_value=({guard}, None, policy)), \
             mock.patch.object(system_installer, "_unit_state", return_value="inactive"):
            system_installer._verify_effective_unit_guard(unit, "owner")
            values["BindPaths"] = "/workspace:/run/little-mester-vm-workspace-share:rbind"
            with self.assertRaisesRegex(RuntimeError, "effective bind"):
                system_installer._verify_effective_unit_guard(unit, "owner")
            values["BindPaths"] = ""
            values["MainPID"] = "1"
            with self.assertRaisesRegex(RuntimeError, "must be stopped"):
                system_installer._verify_effective_unit_guard(unit, "owner")
            values["MainPID"] = "0"
            values["DropInPaths"] = guard + " /etc/systemd/system/extra.conf"
            with self.assertRaisesRegex(RuntimeError, "unexpected drop-in set"):
                system_installer._verify_effective_unit_guard(unit, "owner")

    def test_qemu_is_main_process_and_service_policy_has_bounded_cgroup_fallback(self):
        unit = (ROOT / "systemd/little-mester-vm.service").read_text()
        self.assertIn("Type=exec", unit)
        exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        self.assertEqual(shlex.split(exec_line[len("ExecStart="):]), [
            "+/usr/bin/python3", "-I", "/usr/lib/little-mester-vm/system-vm.py",
            "locked-service", "qemu"])
        post_line = next(line for line in unit.splitlines() if line.startswith("ExecStartPost="))
        self.assertEqual(shlex.split(post_line[len("ExecStartPost="):]), [
            "/usr/bin/python3", "-I", "/usr/lib/little-mester-vm/system-vm.py", "guest-ready"])
        self.assertEqual(system_installer.FILES["lib/vm/system-vm.py"],
                         ("/usr/lib/little-mester-vm/system-vm.py", 0o644))
        self.assertIn("def _locked_service_exec", (ROOT / "lib/vm/system-vm.py").read_text())
        self.assertIn('if args.action == "guest-ready":\n            _qemu_guest_ready()',
                      (ROOT / "lib/vm/system-vm.py").read_text())
        self.assertIn("Restart=no", unit)
        self.assertIn("KillMode=control-group", unit)
        self.assertIn("TimeoutStopSec=25s", unit)
        self.assertIn("SendSIGKILL=yes", unit)
        self.assertIn("ExecStop=/usr/bin/python3 -I /usr/lib/little-mester-vm/system-vm.py qmp-stop $MAINPID", unit)
        self.assertIn("DeviceAllow=/dev/kvm rw", unit)
        self.assertIn("SupplementaryGroups=kvm lm-vm-control", unit)
        self.assertIn("ReadWritePaths=/var/lib/little-mester-vm/disk.qcow2 /run/little-mester-vm", unit)
        self.assertNotIn("parent-supervisor", unit)
        with mock.patch.object(vm, "_verify_kvm_access"), \
             mock.patch.object(vm, "_require_fixed_file"), \
             mock.patch.object(vm, "_validate_runtime_directory"), \
             mock.patch.object(vm, "_verify_workspace_share"), \
             mock.patch.object(vm, "read_proxy_port", return_value=vm.PROXY_PORT):
            expected = vm.qemu_argv()
        self.assertTrue(expected[0].endswith("qemu-system-x86_64"))
        self.assertIn("-blockdev", expected)

    def test_fixed_service_launcher_holds_shared_lock_across_exec(self):
        class ExecReached(Exception):
            pass

        def record_exec(*args):
            events.append("exec")
            raise ExecReached

        def record_event(event):
            return lambda *args: events.append(event)

        control_gid, kvm_gid = 5150, 5151
        lock_stat = type("Stat", (), {
            "st_mode": stat.S_IFREG | 0o660, "st_nlink": 1, "st_uid": 0,
            "st_gid": control_gid,
        })()
        group_ids = {"lm-vm-control": control_gid, "kvm": kvm_gid}
        account = type("Account", (), {"pw_uid": 1101, "pw_gid": 1101})()
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            marker = pathlib.Path(tmp) / "update-marker"
            with mock.patch.object(vm, "CODE_UPDATE_MARKER", marker), \
                 mock.patch.object(vm.os, "geteuid", return_value=0), \
                 mock.patch.object(vm.os, "open", return_value=41), \
                 mock.patch.object(vm.os, "fstat", return_value=lock_stat), \
                 mock.patch.object(vm.grp, "getgrnam",
                                   side_effect=lambda name: type("Group", (), {
                                       "gr_gid": group_ids[name]})()), \
                 mock.patch.object(vm.pwd, "getpwnam", return_value=account), \
                 mock.patch.object(vm.fcntl, "flock") as flock, \
                 mock.patch.object(vm.os, "setgroups", side_effect=record_event("setgroups")) as setgroups, \
                 mock.patch.object(vm.os, "setgid", side_effect=record_event("setgid")) as setgid, \
                 mock.patch.object(vm.os, "setuid", side_effect=record_event("setuid")) as setuid, \
                 mock.patch.object(vm, "_set_no_new_privs", side_effect=record_event("nnp")) as set_no_new_privs, \
                 mock.patch.object(vm.os, "set_inheritable", side_effect=record_event("inheritable")) as inheritable, \
                 mock.patch.object(vm.os, "execv", side_effect=record_exec), \
                 mock.patch.object(vm.os, "close") as close, \
                 mock.patch.object(vm, "qemu_argv", return_value=["/usr/bin/qemu"]):
                with self.assertRaises(ExecReached):
                    vm._locked_service_exec("qemu")
        flock.assert_called_once_with(41, vm.fcntl.LOCK_SH | vm.fcntl.LOCK_NB)
        setgroups.assert_called_once_with([control_gid, kvm_gid])
        setgid.assert_called_once_with(account.pw_gid)
        setuid.assert_called_once_with(account.pw_uid)
        set_no_new_privs.assert_called_once_with()
        inheritable.assert_called_once_with(41, True)
        close.assert_called_once_with(41)
        self.assertLess(events.index("setgroups"), events.index("setgid"))
        self.assertLess(events.index("setgid"), events.index("setuid"))
        self.assertLess(events.index("setuid"), events.index("nnp"))
        self.assertLess(events.index("nnp"), events.index("inheritable"))
        self.assertLess(events.index("inheritable"), events.index("exec"))

    def test_locked_launcher_enforces_no_new_privs_for_roles_except_workspace(self):
        self.assertEqual(vm.NNP_REQUIRED_ROLES, frozenset({"qemu", "proxy", "metrics"}))
        account = type("Account", (), {"pw_uid": 1101, "pw_gid": 1101})()
        lock_stat = type("Stat", (), {
            "st_mode": stat.S_IFREG | 0o660, "st_nlink": 1, "st_uid": 0,
            "st_gid": 5150,
        })()
        with tempfile.TemporaryDirectory() as tmp:
            marker = pathlib.Path(tmp) / "update-marker"
            with mock.patch.object(vm, "CODE_UPDATE_MARKER", marker), \
                 mock.patch.object(vm.os, "geteuid", return_value=0), \
                 mock.patch.object(vm.os, "open", return_value=41), \
                 mock.patch.object(vm.os, "fstat", return_value=lock_stat), \
                 mock.patch.object(vm.grp, "getgrnam", return_value=type("Group", (), {"gr_gid": 5150})()), \
                 mock.patch.object(vm.pwd, "getpwnam", return_value=account), \
                 mock.patch.object(vm.fcntl, "flock"), \
                 mock.patch.object(vm.os, "setgroups"), \
                 mock.patch.object(vm.os, "setgid"), \
                 mock.patch.object(vm.os, "setuid"), \
                 mock.patch.object(vm, "_set_no_new_privs", side_effect=vm.Refused("prctl failed")), \
                 mock.patch.object(vm.os, "set_inheritable"), \
                 mock.patch.object(vm.os, "execv") as execv, \
                 mock.patch.object(vm.os, "close"):
                with self.assertRaisesRegex(vm.Refused, "prctl failed"):
                    vm._locked_service_exec("proxy")
        execv.assert_not_called()

    def test_set_no_new_privs_uses_checked_prctl_call(self):
        libc = mock.Mock()
        libc.prctl.return_value = 0
        with mock.patch.object(vm.ctypes, "CDLL", return_value=libc):
            vm._set_no_new_privs()
        libc.prctl.assert_called_once_with(vm.PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
        self.assertEqual(libc.prctl.argtypes, [
            vm.ctypes.c_int, vm.ctypes.c_ulong, vm.ctypes.c_ulong,
            vm.ctypes.c_ulong, vm.ctypes.c_ulong,
        ])
        self.assertIs(libc.prctl.restype, vm.ctypes.c_int)

        libc.prctl.reset_mock()
        libc.prctl.return_value = -1
        with mock.patch.object(vm.ctypes, "CDLL", return_value=libc), \
             mock.patch.object(vm.ctypes, "get_errno", return_value=1), \
             self.assertRaisesRegex(vm.Refused, "could not set no_new_privs"):
            vm._set_no_new_privs()

    def test_workspace_locked_launcher_refuses_root_owner(self):
        root_account = type("Account", (), {"pw_uid": 0, "pw_gid": 0})()
        with mock.patch.object(vm.os, "geteuid", return_value=0), \
             mock.patch.object(vm.pwd, "getpwnam", return_value=root_account), \
             self.assertRaisesRegex(vm.Refused, "root cannot be a fixed service identity"):
            vm._locked_service_exec("workspace", "root")

    def test_proxy_is_separate_and_qemu_requires_successful_proxy_probe(self):
        vm_unit = (ROOT / "systemd/little-mester-vm.service").read_text()
        proxy = (ROOT / "systemd/little-mester-vm-proxy.service").read_text()
        self.assertIn("Requires=little-mester-vm-proxy.service", vm_unit)
        self.assertIn("BindsTo=little-mester-vm-proxy.service", vm_unit)
        self.assertIn("After=little-mester-vm-proxy.service", vm_unit)
        self.assertIn("ExecStartPost=/usr/bin/python3 -I /usr/lib/little-mester-vm/system-vm.py proxy-ready", proxy)
        self.assertIn("User=lm-vm-proxy", proxy)
        self.assertIn("PartOf=little-mester-vm.service", proxy)
        self.assertIn("locked-service proxy", proxy)
        self.assertIn('"--port", str(PROXY_PORT)', (ROOT / "lib/vm/system-vm.py").read_text())
        self.assertIn("def _verify_kvm_access", (ROOT / "lib/vm/system-vm.py").read_text())

    def test_kvm_device_group_dac_is_checked_in_addition_to_deviceallow(self):
        device = type("Device", (), {"st_mode": stat.S_IFCHR | 0o660, "st_gid": 4242})()
        with mock.patch.object(vm.grp, "getgrnam", return_value=type("Group", (), {"gr_gid": 4242})()), \
             mock.patch.object(vm.os, "stat", return_value=device), \
             mock.patch.object(vm.os, "getgroups", return_value=[4242]), \
             mock.patch.object(vm.os, "access", return_value=True):
            vm._verify_kvm_access()
        no_group_rw = type("Device", (), {"st_mode": stat.S_IFCHR | 0o600, "st_gid": 4242})()
        with mock.patch.object(vm.grp, "getgrnam", return_value=type("Group", (), {"gr_gid": 4242})()), \
             mock.patch.object(vm.os, "stat", return_value=no_group_rw), \
             mock.patch.object(vm.os, "getgroups", return_value=[4242]), \
             mock.patch.object(vm.os, "access", return_value=True):
            with self.assertRaisesRegex(vm.Refused, "group-owned by kvm"):
                vm._verify_kvm_access()

    def test_service_kvm_gate_still_requires_its_own_group_and_effective_access(self):
        world_rw_device = type("Device", (), {"st_mode": stat.S_IFCHR | 0o666, "st_gid": 4242})()
        group = type("Group", (), {"gr_gid": 4242})()
        with mock.patch.object(vm.grp, "getgrnam", return_value=group), \
             mock.patch.object(vm.os, "stat", return_value=world_rw_device), \
             mock.patch.object(vm.os, "getgroups", return_value=[]), \
             mock.patch.object(vm.os, "access", return_value=True):
            with self.assertRaisesRegex(vm.Refused, "exact supplementary group"):
                vm._verify_kvm_access()
        with mock.patch.object(vm.grp, "getgrnam", return_value=group), \
             mock.patch.object(vm.os, "stat", return_value=world_rw_device), \
             mock.patch.object(vm.os, "getgroups", return_value=[4242]), \
             mock.patch.object(vm.os, "access", return_value=False) as access:
            with self.assertRaisesRegex(vm.Refused, "lacks DAC read/write access"):
                vm._verify_kvm_access()
        access.assert_called_once_with("/dev/kvm", vm.os.R_OK | vm.os.W_OK, effective_ids=True)

    def test_sudoers_and_owner_cli_have_only_fixed_unit_and_operations(self):
        sudoers = (ROOT / "sudoers/little-mester-vm").read_text().splitlines()
        entries = [line for line in sudoers if line.startswith("%")]
        self.assertEqual(entries, [
            "%lm-vm-control ALL=(root) NOPASSWD: /usr/bin/systemctl start little-mester-vm.service",
            "%lm-vm-control ALL=(root) NOPASSWD: /usr/bin/systemctl stop little-mester-vm.service",
            "%lm-vm-control ALL=(root) NOPASSWD: /usr/bin/python3 -I /usr/lib/little-mester-vm/system-vm-workspace.py configure",
            "%lm-vm-control ALL=(root) NOPASSWD: /usr/bin/stat --format=%f\\:%d\\:%i -- /var/lib/little-mester-vm/disk.qcow2",
            "%lm-vm-control ALL=(root) NOPASSWD: /usr/bin/cat -- /etc/sudoers.d/little-mester-vm",
            "%lm-vm-control ALL=(root) NOPASSWD: /usr/bin/stat --format=%f\\:%u\\:%g\\:%h -- /etc/sudoers.d/little-mester-vm",
        ])
        self.assertFalse(any(token in "\n".join(entries) for token in ("*", "?", "[", "\"")))
        policy = type("Policy", (), {
            "ASSIGNMENT": pathlib.Path("/etc/little-mester-vm/workspace.json"),
            "verify_host_assignment": staticmethod(lambda *_args, **_kwargs: object()),
        })()
        with mock.patch.object(vm, "_workspace_policy", return_value=policy), \
             mock.patch.object(vm.subprocess, "run") as run, \
             mock.patch.object(vm, "_probe_workspace") as probe:
            run.return_value.returncode = 0
            self.assertEqual(vm._fixed_exec_locked("up"), 0)
            self.assertEqual(run.call_args.args[0], [
                "/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", "little-mester-vm.service"])
            probe.assert_called_once_with()
        invalid_policy = type("Policy", (), {
            "Refused": RuntimeError,
            "verify_host_assignment": staticmethod(
                lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bad drop-in"))),
        })()
        with mock.patch.object(vm, "_workspace_policy", return_value=invalid_policy), \
                mock.patch.object(vm.subprocess, "run") as run:
            with self.assertRaisesRegex(vm.Refused, "before VM lifecycle.*bad drop-in"):
                vm._fixed_exec_locked("up")
        run.assert_not_called()
        with self.assertRaises(vm.Refused):
            vm._fixed_exec_locked("systemctl")
        with mock.patch.object(vm.sys, "argv", ["system-vm.py", "owner", "up"]), \
             mock.patch.object(vm, "_fixed_exec", return_value=0) as dispatch:
            self.assertEqual(vm.main(), 0)
            dispatch.assert_called_once_with("up")
        with mock.patch.object(vm.sys, "argv", ["system-vm.py", "owner", "up", "--unit", "evil"]):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    vm.main()
        with mock.patch.object(vm, "_system_unit_active", return_value=True), \
             mock.patch.object(vm, "_guest_ssh", return_value=0) as ssh, \
             mock.patch.object(vm, "_browser_auth", return_value=0) as auth:
            self.assertEqual(vm._fixed_exec_locked("auth"), 0)
            auth.assert_called_once_with()
            self.assertEqual(vm._fixed_exec_locked("tui"), 0)
            self.assertEqual(ssh.call_args.args[0], "/usr/local/bin/lm-opencode")

    def test_install_manifest_contains_all_fixed_assets(self):
        required = {
            "lib/vm/system-vm.py", "systemd/little-mester-vm.service",
            "lib/vm/system-vm-workspace.py", "systemd/little-mester-vm-workspace.service",
            "systemd/little-mester-vm-metrics.service",
            "lib/vm/guest-opencode-preferences.py",
            "lib/vm/system-vm-metrics.py", "lib/vm/parent-metrics.py",
            "lib/vm/parent-metrics-collect.py",
            "agent/opencode/plugins/host-metrics-tui/package.json",
            "agent/opencode/plugins/host-metrics-tui/tui.tsx",
            "agent/opencode/plugins/host-metrics-tui/metrics-endpoint.js",
            "agent/opencode/plugins/host-metrics-tui/metrics-endpoint.d.ts",
            "agent/opencode/plugins/host-metrics-tui/metrics-state.js",
            "agent/opencode/plugins/host-metrics-tui/metrics-state.d.ts",
            "systemd/little-mester-vm-proxy.service", "sudoers/little-mester-vm",
            "tools/vm-system-install", "lib/vm/guest-bootstrap.py",
            "lib/vm/guest-compatibility.py", "lib/vm/code-update-transaction.py",
            "systemd/little-mester-vm.service.d/90-code-update-guard.conf",
            "lib/vm/desktop-session-env.sh",
        }
        self.assertTrue(required.issubset(owner.INSTALL_FILES))
        self.assertIn("lib/vm/system-acceptance.py", owner.INSTALL_FILES)
        self.assertIn("lib/vm/host-dependencies.py", owner.INSTALL_FILES)
        self.assertEqual(owner.INSTALL_FILES["sudoers/little-mester-vm"], 0o444)
        self.assertTrue({"systemd", "sudoers"}.issubset(owner.INSTALL_DIRS))
        self.assertEqual(owner.INSTALL_FILES["tools/vm-system-install"], 0o755)
        installer = (ROOT / "tools/vm-system-install").read_text()
        self.assertIn("does not change host device policy", installer)
        self.assertIn('"provision"], check=True, stdin=subprocess.DEVNULL)', installer)
        self.assertIn('"lib/vm/guest-bootstrap.py": ("/usr/lib/little-mester-vm/guest-bootstrap.py"',
                      installer)
        self.assertIn('"lib/vm/system-vm.py": ("/usr/lib/little-mester-vm/system-vm.py"', installer)
        self.assertIn('"lib/vm/system-vm-workspace.py": ("/usr/lib/little-mester-vm/system-vm-workspace.py"', installer)
        self.assertIn('"lib/vm/system-vm-metrics.py":', installer)
        self.assertIn('"lib/vm/parent-metrics-collect.py":', installer)
        self.assertIn('"lib/vm/guest-opencode-preferences.py":', installer)
        self.assertIn('"agent/opencode/plugins/host-metrics-tui/tui.tsx":', installer)
        self.assertIn('"agent/opencode/plugins/host-metrics-tui/metrics-endpoint.js":', installer)
        self.assertIn('"agent/opencode/plugins/host-metrics-tui/metrics-state.js":', installer)
        self.assertIn('"systemd/little-mester-vm-metrics.service":', installer)
        self.assertIn('"systemd/little-mester-vm-workspace.service":', installer)

    def test_code_update_is_explicit_stopped_and_state_preserving(self):
        installer = (ROOT / "tools/vm-system-install").read_text()
        for required in (
            'args[0] == "--update-code-only" and args[1] == "--previous-sha"',
            "def update_code_only(root: pathlib.Path, owner: str, previous_sha: str)",
            '"little-mester-vm-workspace.service"',
            "before_state = _state_fingerprint(state_paths)",
            "if _state_fingerprint(state_paths) != before_state:",
            '"/usr/bin/systemctl", "daemon-reload"',
            "services remain stopped",
            "UPDATE_ADDITIONALS",
            "ensure_dir(pathlib.Path(\"/etc/systemd/system/little-mester-vm-workspace.service.d\")",
            "txn.publish_snapshot(snapshot_path, target_map, candidate_sha=root.name",
            "txn.restore_snapshot(snapshot_path, target_map, candidate",
            "--rollback-code-only",
            "CODE_UPDATE_LOCK",
            "CODE_UPDATE_MARKER",
            '"systemd/little-mester-vm.service"',
            '"lib/vm/system-vm.py"',
            "_install_permanent_interlock_assets",
            "preserve_candidate_paths=frozenset(",
        ):
            self.assertIn(required, installer)
        self.assertIn("--update-code-only", installer)
        self.assertIn('"lib/vm/host-dependencies.py": ("/usr/lib/little-mester-vm/host-dependencies.py"', installer)
        self.assertIn('Parse the exact pinned policy before creating accounts', installer)
        self.assertIn('preflight(root, owner)', installer)
        self.assertLess(installer.index('preflight(root, owner)'),
                        installer.index('ensure_group("lm-vm")'))

    def test_all_fixed_units_have_maintenance_marker_and_nonblocking_lock_guard(self):
        units = ("little-mester-vm.service", "little-mester-vm-proxy.service",
                 "little-mester-vm-workspace.service", "little-mester-vm-metrics.service")
        for unit in units:
            path = ROOT / "systemd" / f"{unit}.d/90-code-update-guard.conf"
            text = path.read_text()
            self.assertIn("ConditionPathExists=!/etc/little-mester-vm/code-update.in-progress", text)
            self.assertIn("flock --shared --nonblock /etc/little-mester-vm/code-update.lock", text)
            self.assertIn("/usr/bin/true", text)

    def test_compatibility_check_precedes_readiness_and_normal_use_requires_active_unit(self):
        calls = []
        compatibility_context = []
        status_results = [cloud_init_result("running"), cloud_init_result("done"), 0, 0]

        def ssh_side_effect(command, **_kwargs):
            if command == vm.CLOUD_INIT_STATUS_COMMAND:
                calls.append("cloud-init-running" if len(calls) == 0 else "cloud-init-done")
                return status_results.pop(0)
            if command == vm.GUEST_PROVISIONED_COMMAND:
                calls.append("provision-marker")
                return status_results.pop(0)
            calls.append("readiness")
            return status_results.pop(0)

        with mock.patch.object(vm, "_guest_ssh", side_effect=ssh_side_effect) as guest_ssh, \
             mock.patch.object(vm.time, "sleep"), \
             mock.patch.object(vm, "_check_guest_compatibility",
                               side_effect=lambda **kwargs: (calls.append("compat"),
                                                            compatibility_context.append(kwargs))):
            # Exercise the real readiness orchestration while isolating transport.
            vm._ensure_guest_ready()
            self.assertEqual(guest_ssh.call_count, 4)
        self.assertEqual(calls, ["cloud-init-running", "cloud-init-done", "provision-marker",
                                 "compat", "readiness"])
        self.assertEqual(set(compatibility_context[0]), {"cloud_init_ready_wait_ms"})
        self.assertIsInstance(compatibility_context[0]["cloud_init_ready_wait_ms"], int)
        self.assertGreaterEqual(compatibility_context[0]["cloud_init_ready_wait_ms"], 0)
        status_calls = [call for call in guest_ssh.call_args_list
                        if call.args[0] == vm.CLOUD_INIT_STATUS_COMMAND]
        self.assertEqual(len(status_calls), 2)
        for call in status_calls:
            self.assertEqual(call.kwargs["capture_limit"], vm.GUEST_COMPAT_MAX_OUTPUT)
            self.assertLessEqual(call.kwargs["timeout"], 15)
        self.assertIn("test ! -L /var/lib/mester-v3-provisioned", vm.GUEST_PROVISIONED_COMMAND)

    def test_cloud_init_status_classifier_requires_consistent_clean_completion(self):
        cases = (
            (cloud_init_result("not started"), "RUNNING"),
            (cloud_init_result("running"), "RUNNING"),
            (cloud_init_result("done"), "DONE"),
            (cloud_init_result("running", return_code=2,
                               extended_status="degraded running"), "ERROR"),
            (cloud_init_result("done", return_code=2,
                               extended_status="degraded done"), "ERROR"),
            (cloud_init_result("done", errors=["provisioning failed"]), "ERROR"),
            (cloud_init_result("done", recoverable_errors={"WARNING": ["degraded"]}), "ERROR"),
            (cloud_init_result("error", return_code=1,
                               extended_status="error - done"), "ERROR"),
            (cloud_init_result("disabled"), "ERROR"),
            (cloud_init_result("running", extended_status="done"), "INCONCLUSIVE"),
            ((0, b"not-json"), "INCONCLUSIVE"),
            ((0, b'{"status":"running","status":"done"}'), "INCONCLUSIVE"),
            ((0, b"x" * (vm.GUEST_COMPAT_MAX_OUTPUT + 1)), "INCONCLUSIVE"),
        )
        for result, expected in cases:
            with self.subTest(result=result[0]):
                self.assertEqual(vm._cloud_init_status_state(*result), expected)

    def test_running_cloud_init_never_reaches_compatibility_before_deadline(self):
        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                self.now += seconds

        clock = Clock()
        with mock.patch.object(vm, "SSH_READY_TIMEOUT", 2.5), \
             mock.patch.object(vm.time, "monotonic", side_effect=clock.monotonic), \
             mock.patch.object(vm.time, "sleep", side_effect=clock.sleep), \
             mock.patch.object(vm, "_guest_ssh", return_value=cloud_init_result("running")) as ssh, \
             mock.patch.object(vm, "_check_guest_compatibility") as compatibility_check:
            with self.assertRaisesRegex(vm.Refused, "did not complete before its bound"):
                vm._ensure_guest_ready()
        self.assertEqual(ssh.call_count, 2)
        compatibility_check.assert_not_called()

    def test_ssh_transport_recovery_stays_within_provisioning_gate(self):
        results = [(255, b""), vm.GuestSSHTimeout("bounded request timeout"),
                   cloud_init_result("running"), cloud_init_result("done"), 0]
        with mock.patch.object(vm, "_guest_ssh", side_effect=results) as ssh, \
             mock.patch.object(vm.time, "sleep"):
            vm._wait_for_guest_provisioning()
        self.assertEqual(ssh.call_count, 5)
        self.assertEqual(ssh.call_args_list[-1].args[0], vm.GUEST_PROVISIONED_COMMAND)

    def test_status_response_overflow_fails_closed_without_compatibility(self):
        with mock.patch.object(vm, "_guest_ssh",
                               side_effect=vm.Refused("fixed guest compatibility response exceeded its bound")) as ssh, \
             mock.patch.object(vm, "_check_guest_compatibility") as compatibility_check:
            with self.assertRaisesRegex(vm.Refused, "response exceeded its bound"):
                vm._ensure_guest_ready()
        ssh.assert_called_once_with(
            vm.CLOUD_INIT_STATUS_COMMAND, timeout=15.0,
            capture_limit=vm.GUEST_COMPAT_MAX_OUTPUT)
        compatibility_check.assert_not_called()

    def test_cloud_init_error_and_missing_provision_marker_block_compatibility(self):
        cases = (
            ([cloud_init_result("done", return_code=2,
                                extended_status="degraded done")],
             "unsuccessful or degraded completion"),
            ([cloud_init_result("done"), 1], "completion marker is missing or unsafe"),
        )
        for results, error in cases:
            with self.subTest(error=error):
                calls = []

                def ssh_side_effect(command, **_kwargs):
                    calls.append(command)
                    return results.pop(0)

                with mock.patch.object(vm, "_guest_ssh", side_effect=ssh_side_effect), \
                     mock.patch.object(vm.time, "sleep"), \
                     mock.patch.object(vm, "_check_guest_compatibility") as compatibility_check:
                    with self.assertRaisesRegex(vm.Refused, error):
                        vm._ensure_guest_ready()
                compatibility_check.assert_not_called()
                if error.startswith("unsuccessful"):
                    self.assertEqual(calls, [vm.CLOUD_INIT_STATUS_COMMAND])
                else:
                    self.assertEqual(calls, [vm.CLOUD_INIT_STATUS_COMMAND,
                                             vm.GUEST_PROVISIONED_COMMAND])

    def test_guest_compatibility_nonmatch_fails_startup_closed(self):
        checker = type("Checker", (), {
            "SYSTEM_WORKSPACE": "/workspace",
            "build_inspection_command": staticmethod(lambda _workspace: "fixed inspect"),
            "expected_inventory": staticmethod(lambda: {"/workspace": {"kind": "mount"}}),
            "classify_inventory": staticmethod(lambda *_args, **_kwargs:
                                                ("INCONCLUSIVE", ("response-invalid",))),
            "format_failure_evidence": staticmethod(lambda *_args, **_kwargs:
                                                      "Guest compatibility evidence: bounded"),
        })()
        with mock.patch.object(vm, "_guest_compatibility_module", return_value=checker), \
             mock.patch.object(vm, "_guest_ssh", return_value=(0, b"{}")) as ssh:
            error_output = io.StringIO()
            with contextlib.redirect_stderr(error_output):
                with self.assertRaisesRegex(vm.Refused, "normal VM use is blocked"):
                    vm._check_guest_compatibility(cloud_init_ready_wait_ms=17)
        ssh.assert_called_once()
        self.assertIn("Guest compatibility: INCONCLUSIVE", error_output.getvalue())
        self.assertIn("Guest compatibility evidence: bounded", error_output.getvalue())

    def test_compatibility_match_does_not_emit_failure_evidence(self):
        evidence = mock.Mock(return_value="Guest compatibility evidence: unexpected")
        checker = type("Checker", (), {
            "SYSTEM_WORKSPACE": "/workspace",
            "build_inspection_command": staticmethod(lambda _workspace: "fixed inspect"),
            "expected_inventory": staticmethod(lambda: {"/workspace": {"kind": "mount"}}),
            "classify_inventory": staticmethod(lambda *_args, **_kwargs: ("MATCH", ())),
            "format_failure_evidence": evidence,
        })()
        with mock.patch.object(vm, "_guest_compatibility_module", return_value=checker), \
             mock.patch.object(vm, "_guest_ssh", return_value=(0, b"inventory")) as ssh:
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                vm._check_guest_compatibility()
        self.assertEqual(stdout.getvalue(), "Guest compatibility: MATCH\n")
        self.assertEqual(stderr.getvalue(), "")
        evidence.assert_not_called()
        ssh.assert_called_once()

    def test_compatibility_failure_diagnostic_errors_do_not_bypass_refusal(self):
        checker = type("Checker", (), {
            "SYSTEM_WORKSPACE": "/workspace",
            "build_inspection_command": staticmethod(lambda _workspace: "fixed inspect"),
            "expected_inventory": staticmethod(lambda: {"/workspace": {"kind": "mount"}}),
            "classify_inventory": staticmethod(lambda *_args, **_kwargs:
                                                ("MISMATCH", ("workspace-mount:mismatch",))),
            "format_failure_evidence": staticmethod(lambda *_args, **_kwargs:
                (_ for _ in ()).throw(RuntimeError("untrusted diagnostic detail"))),
        })()
        with mock.patch.object(vm, "_guest_compatibility_module", return_value=checker), \
             mock.patch.object(vm, "_guest_ssh", return_value=(0, b"{}")):
            error_output = io.StringIO()
            with contextlib.redirect_stderr(error_output):
                with self.assertRaisesRegex(vm.Refused, "normal VM use is blocked"):
                    vm._check_guest_compatibility()
        self.assertIn("unavailable (diagnostic-error)", error_output.getvalue())
        self.assertNotIn("untrusted diagnostic detail", error_output.getvalue())

    def test_normal_guest_routes_refuse_when_startup_gate_is_not_active(self):
        with mock.patch.object(vm, "_system_unit_active", return_value=False), \
             mock.patch.object(vm, "_guest_ssh") as ssh, \
             mock.patch.object(vm, "_browser_auth") as auth:
            for action in ("ssh", "auth", "tui"):
                with self.subTest(action=action), self.assertRaisesRegex(vm.Refused, "not active"):
                    vm._fixed_exec_locked(action)
        ssh.assert_not_called()
        auth.assert_not_called()

    def test_owner_acceptance_is_only_an_adapter_to_lab_vm(self):
        with mock.patch.object(owner.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(owner.owner_system_vm(pathlib.Path("/opt/fixed"), "down"), 0)
            self.assertEqual(run.call_args.args[0], ["/opt/fixed/lab-vm", "system", "down"])
        with self.assertRaises(owner.GateError):
            owner.owner_system_vm(pathlib.Path("/opt/fixed"), "down;id")

    def test_lifecycle_acceptance_requires_explicit_consent_without_tty(self):
        with mock.patch.object(owner.sys.stdin, "isatty", return_value=False), \
                self.assertRaisesRegex(owner.GateError, "requires --approve-system-vm-lifecycle"):
            owner.owner_system_vm(pathlib.Path("/opt/fixed"), "accept")

    def test_owner_acceptance_cli_has_read_only_verify_and_explicit_accept_modes(self):
        cli = (ROOT / "tools/vm-owner-acceptance").read_text()
        self.assertIn('choices=("verify", "accept", "up", "down", "status", "ssh", "auth", "tui")', cli)
        self.assertIn('"--approve-system-vm-lifecycle"', cli)

    def test_acceptance_refuses_ambiguous_start_states_without_stopping_vm(self):
        with mock.patch.object(acceptance, "service_state", return_value={
                "ActiveState": "inactive", "MainPID": "0"}), \
                self.assertRaisesRegex(acceptance.AcceptanceError, "lifecycle state is ambiguous"):
            acceptance._initial_running_state(
                {"ActiveState": "active", "MainPID": "123"},
                {"ActiveState": "inactive", "MainPID": "0"})
        with mock.patch.object(acceptance, "service_state", return_value={
                "ActiveState": "inactive", "MainPID": "0"}), \
                self.assertRaisesRegex(acceptance.AcceptanceError, "unit state is ambiguous"):
            acceptance._initial_running_state(
                {"ActiveState": "active", "MainPID": "0"},
                {"ActiveState": "active", "MainPID": "456"})

    def test_acceptance_failure_restores_preexisting_running_state(self):
        with mock.patch.object(acceptance, "run") as run, \
                mock.patch.object(acceptance, "service_state", return_value={
                    "ActiveState": "active", "MainPID": "987"}):
            acceptance._restore_initial_state(pathlib.Path("/opt/candidate"), True)
        self.assertEqual(run.call_args.args[0], [
            "/opt/candidate/lab-vm", "system", "up"])
        self.assertEqual(run.call_args.kwargs["timeout"], 1005)

    def test_acceptance_restoration_waits_for_dependency_shutdown_to_settle(self):
        stopped = {"ActiveState": "inactive", "MainPID": "0", "Result": "success"}
        stopping = {"ActiveState": "deactivating", "MainPID": "987", "Result": "success"}
        states = [stopping, stopped, stopped, stopped] + [stopped] * 12
        with mock.patch.object(acceptance, "run"), \
                mock.patch.object(acceptance, "service_state", side_effect=states) as state, \
                mock.patch.object(acceptance.time, "sleep") as sleep:
            acceptance._restore_initial_state(pathlib.Path("/opt/candidate"), False)
        self.assertEqual(state.call_count, 16)
        self.assertEqual(sleep.call_count, 3)

    def test_acceptance_restoration_fails_closed_when_services_do_not_settle(self):
        stopping = {"ActiveState": "deactivating", "MainPID": "987", "Result": "success"}
        with mock.patch.object(acceptance, "run"), \
                mock.patch.object(acceptance, "service_state", return_value=stopping), \
                mock.patch.object(acceptance.time, "monotonic", side_effect=[0, 0, 0, 0, 2]), \
                mock.patch.object(acceptance.time, "sleep"), \
                self.assertRaisesRegex(acceptance.AcceptanceError, "did not settle"):
            acceptance._restore_initial_state(pathlib.Path("/opt/candidate"), False, timeout=1)

    def test_acceptance_requires_effective_control_group_before_lifecycle_preflight(self):
        fake_pexpect = type("Pexpect", (), {})()
        runtime_dir = type("Info", (), {
            "st_mode": stat.S_IFDIR | 0o700, "st_uid": 1234,
        })()
        with mock.patch.dict("sys.modules", {"pexpect": fake_pexpect}), \
             mock.patch.object(acceptance.os.path, "isfile", return_value=True), \
             mock.patch.object(acceptance.os, "access", return_value=True), \
             mock.patch.object(acceptance.os, "getuid", return_value=1234), \
             mock.patch.object(acceptance.os, "getgroups", return_value=[99]), \
             mock.patch.object(acceptance.os, "getegid", return_value=100), \
             mock.patch.object(acceptance.os, "lstat", return_value=runtime_dir), \
             mock.patch.object(acceptance.grp, "getgrnam", return_value=type("Group", (), {"gr_gid": 42})()), \
             self.assertRaisesRegex(acceptance.AcceptanceError, "lacks effective lm-vm-control"):
            acceptance._lifecycle_preflight(pathlib.Path("/opt/candidate"))

    def test_acceptance_accepts_effective_supplementary_control_group_and_checks_key_metadata(self):
        fake_pexpect = type("Pexpect", (), {})()
        runtime_dir = type("Info", (), {
            "st_mode": stat.S_IFDIR | 0o700, "st_uid": 1234,
        })()
        control_key = type("Info", (), {
            "st_mode": stat.S_IFREG | 0o640, "st_uid": 0, "st_gid": 42, "st_nlink": 1,
        })()
        known_hosts = type("Info", (), {
            "st_mode": stat.S_IFREG | 0o644, "st_uid": 0, "st_gid": 0, "st_nlink": 1,
        })()
        with mock.patch.dict("sys.modules", {"pexpect": fake_pexpect}), \
             mock.patch.object(acceptance.os.path, "isfile", return_value=True), \
             mock.patch.object(acceptance.os, "access", return_value=True), \
             mock.patch.object(acceptance.os, "getuid", return_value=1234), \
             mock.patch.object(acceptance.os, "getgroups", return_value=[42]), \
             mock.patch.object(acceptance.os, "getegid", return_value=100), \
             mock.patch.object(acceptance.os, "lstat", side_effect=[runtime_dir, control_key, known_hosts]), \
             mock.patch.object(acceptance.grp, "getgrnam", return_value=type("Group", (), {"gr_gid": 42})()):
            acceptance._lifecycle_preflight(pathlib.Path("/opt/candidate"))

    def test_tui_acceptance_executes_installed_owner_interface_directly(self):
        class FakeChild:
            exitstatus = 0
            signalstatus = None

            def expect(self, _pattern, timeout=None):
                return None

            def sendcontrol(self, _value):
                return None

            def send(self, _value):
                return None

            def close(self, force=False):
                return None

            def isalive(self):
                return False

        child = FakeChild()
        fake_pexpect = type("Pexpect", (), {"EOF": object(), "spawn": mock.Mock(return_value=child)})()
        with mock.patch.dict("sys.modules", {"pexpect": fake_pexpect}):
            acceptance._tui_check(pathlib.Path("/opt/candidate"))
        args, kwargs = fake_pexpect.spawn.call_args
        self.assertEqual(args[:2], ("/opt/candidate/lab-vm", ["system", "tui"]))
        self.assertEqual(kwargs["encoding"], None)
        self.assertEqual(kwargs["timeout"], 90)

    def test_owner_acceptance_no_longer_depends_on_sg_but_ci_group_activation_remains(self):
        acceptance_source = (ROOT / "lib/vm/system-acceptance.py").read_text()
        ci_source = (ROOT / "tests/ci-kvm-system-unit.py").read_text()
        self.assertNotIn("/usr/bin/sg", acceptance_source)
        self.assertIn('"/usr/bin/sg", "lm-vm-control", "-c"', ci_source)

    def test_workspace_acceptance_requires_bind_compatible_home_isolation(self):
        acceptance_source = (ROOT / "lib/vm/system-acceptance.py").read_text()
        unit_source = (ROOT / "systemd/little-mester-vm-workspace.service").read_text()
        self.assertIn('workspace_properties.get("ProtectHome") == "tmpfs"', acceptance_source)
        self.assertIn("bind_matches = workspace_policy.effective_bind_paths_match(",
                      acceptance_source)
        self.assertIn('compare(WORKSPACE_UNIT, "BindPaths",', acceptance_source)
        self.assertIn("ProtectHome=tmpfs", unit_source)
        self.assertIn("ProtectSystem=strict", unit_source)

    def test_normal_acceptance_stop_rejects_systemd_fallback_failure(self):
        with mock.patch.object(acceptance, "service_state", side_effect=[
                {"ActiveState": "failed", "MainPID": "0", "Result": "exit-code"},
                {"ActiveState": "inactive", "MainPID": "0", "Result": "success"}]), \
                self.assertRaisesRegex(acceptance.AcceptanceError, "failed state"):
            acceptance._require_normal_stop()

    def test_normal_acceptance_stop_waits_for_proxy_deactivation_then_requires_clean_exit(self):
        vm_stopped = {"ActiveState": "inactive", "MainPID": "0", "Result": "success"}
        proxy_stopping = {"ActiveState": "deactivating", "MainPID": "4321", "Result": "success"}
        proxy_stopped = {"ActiveState": "inactive", "MainPID": "0", "Result": "success"}
        with mock.patch.object(acceptance, "service_state", side_effect=[
                vm_stopped, proxy_stopping, vm_stopped, vm_stopped,
                proxy_stopped, vm_stopped, vm_stopped, vm_stopped]) as state, \
                mock.patch.object(acceptance.time, "sleep") as sleep:
            acceptance._require_normal_stop()
        self.assertEqual(state.call_count, 8)
        sleep.assert_called_once()

    def test_normal_acceptance_stop_times_out_if_deactivating_unit_never_exits(self):
        vm_stopped = {"ActiveState": "inactive", "MainPID": "0", "Result": "success"}
        proxy_stopping = {"ActiveState": "deactivating", "MainPID": "4321", "Result": "success"}
        with mock.patch.object(acceptance, "service_state", side_effect=[
                vm_stopped, proxy_stopping, vm_stopped, vm_stopped]), \
                mock.patch.object(acceptance.time, "monotonic",
                                  side_effect=[10.0, 10.0, 10.0, 41.0]), \
                self.assertRaisesRegex(acceptance.AcceptanceError, "did not complete within 30s"):
            acceptance._require_normal_stop(timeout=30)

    def test_normal_acceptance_stop_rejects_inactive_unit_with_live_mainpid(self):
        vm_unexpected = {"ActiveState": "inactive", "MainPID": "4321", "Result": "success"}
        proxy_stopped = {"ActiveState": "inactive", "MainPID": "0", "Result": "success"}
        with mock.patch.object(acceptance, "service_state", side_effect=[
                vm_unexpected, proxy_stopped, proxy_stopped, proxy_stopped]), \
                self.assertRaisesRegex(acceptance.AcceptanceError, "unexpected live MainPID"):
            acceptance._require_normal_stop()

    def test_normal_acceptance_stop_rejects_missing_mainpid_evidence(self):
        vm_without_pid = {"ActiveState": "inactive", "Result": "success"}
        with mock.patch.object(acceptance, "service_state", return_value=vm_without_pid), \
                self.assertRaisesRegex(acceptance.AcceptanceError, "omitted MainPID"):
            acceptance._require_normal_stop()

    def test_effective_systemd_command_parser_preserves_exact_argument_boundaries(self):
        value = ('{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -I '
                 '/usr/lib/little-mester-vm/system-vm.py qmp-stop $MAINPID ; '
                 'ignore_errors=no ; start_time=[x] }')
        self.assertEqual(acceptance._effective_argv(value), (
            "/usr/bin/python3", "/usr/bin/python3 -I /usr/lib/little-mester-vm/system-vm.py qmp-stop $MAINPID"))
        qemu = ('{ path=/usr/bin/qemu-system-x86_64 ; argv[]=/usr/bin/qemu-system-x86_64 '
                '-netdev user,id=net0,guestfwd=cmd:socat - TCP:127.0.0.1:32128,restrict=on ; '
                'ignore_errors=no ; start_time=[x] }')
        self.assertEqual(acceptance._effective_argv(qemu), (
            "/usr/bin/qemu-system-x86_64",
            "/usr/bin/qemu-system-x86_64 -netdev user,id=net0,guestfwd=cmd:socat - TCP:127.0.0.1:32128,restrict=on"))
        with self.assertRaisesRegex(acceptance.AcceptanceError, "effective command"):
            acceptance._effective_argv("ExecStart=/usr/bin/false")
        multiple = value + ' { path=/usr/bin/false ; argv[]=/usr/bin/false ; ignore_errors=no }'
        with self.assertRaisesRegex(acceptance.AcceptanceError, "multiple effective commands"):
            acceptance._effective_argv(multiple)
        first = value.replace("start_time=[x]", "start_time=[x]")
        second = ('{ path=/usr/bin/true ; argv[]=/usr/bin/true ; '
                  'ignore_errors=no ; start_time=[y] }')
        self.assertEqual(acceptance._effective_argvs(first + " ; " + second), (
            ("/usr/bin/python3", "/usr/bin/python3 -I /usr/lib/little-mester-vm/system-vm.py qmp-stop $MAINPID"),
            ("/usr/bin/true", "/usr/bin/true")))
        with self.assertRaisesRegex(acceptance.AcceptanceError, "extra effective"):
            acceptance._effective_argvs(first + " ; injected ; " + second)

    def test_effective_properties_preserve_only_repeated_exec_start_pre(self):
        check = ("{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -I "
                 "/usr/lib/little-mester-vm/system-vm.py check ; ignore_errors=no }")
        guard = ("{ path=/usr/bin/flock ; argv[]=/usr/bin/flock --shared --nonblock "
                 "/etc/little-mester-vm/code-update.lock /usr/bin/true ; ignore_errors=no }")
        unexpected = "{ path=/usr/bin/false ; argv[]=/usr/bin/false ; ignore_errors=no }"
        output = f"User=lm-vm\nExecStartPre={check}\nExecStartPre={guard}\n"
        properties = acceptance._parse_effective_properties(output)
        self.assertEqual(acceptance._effective_argvs(properties["ExecStartPre"]), (
            ("/usr/bin/python3", "/usr/bin/python3 -I /usr/lib/little-mester-vm/system-vm.py check"),
            ("/usr/bin/flock", "/usr/bin/flock --shared --nonblock "
             "/etc/little-mester-vm/code-update.lock /usr/bin/true"),
        ))
        expected = acceptance._effective_argvs(properties["ExecStartPre"])
        def require_exact_pre(output):
            parsed = acceptance._parse_effective_properties(output)
            commands = acceptance._effective_argvs(parsed["ExecStartPre"])
            if commands != expected:
                raise acceptance.AcceptanceError("effective command-list mismatch")
            return commands

        for malformed in (f"User=lm-vm\nExecStartPre={guard}\nExecStartPre={check}\n",
                           f"User=lm-vm\nExecStartPre={check}\n",
                           f"User=lm-vm\nExecStartPre={check}\nExecStartPre={guard}\nExecStartPre={guard}\n",
                           f"User=lm-vm\nExecStartPre={check}\nExecStartPre={guard}\nExecStartPre={unexpected}\n"):
            with self.subTest(output=malformed), self.assertRaises(acceptance.AcceptanceError):
                require_exact_pre(malformed)
        extra_property = acceptance._parse_effective_properties(
            f"ExecStartPre={check}\nExecStartPre={guard}\nExecStart=/usr/bin/false\n")
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance._effective_argv(extra_property["ExecStart"])

    def test_effective_properties_still_reject_duplicate_scalar_and_other_keys(self):
        for output in ("User=lm-vm\nUser=other\n", "ExecStartPost=one\nExecStartPost=two\n"):
            with self.subTest(output=output), self.assertRaisesRegex(
                    acceptance.AcceptanceError, "duplicate effective property"):
                acceptance._parse_effective_properties(output)

    def test_duplicate_effective_property_preserves_other_observations(self):
        with self.assertRaises(acceptance.EffectivePropertiesError) as raised:
            acceptance._parse_effective_properties(
                "User=lm-vm\nUser=other\nNoNewPrivileges=yes\n")
        self.assertEqual(raised.exception.property_names, ("User",))
        self.assertEqual(raised.exception.properties["User"], "lm-vm")
        self.assertEqual(raised.exception.properties["NoNewPrivileges"], "yes")

    def test_effective_property_parser_accepts_captured_systemd_255_show_output(self):
        # Captured verbatim from systemctl show on systemd 255.4-1ubuntu8.18;
        # dynamic ExecStart fields are real output, not verifier-generated input.
        output = (
            "ExecStart={ path=systemd-tmpfiles ; argv[]=systemd-tmpfiles "
            "--create --remove --boot --exclude-prefix=/dev ; ignore_errors=no ; "
            "start_time=[Sat 2026-11-21 09:50:36 UTC] ; "
            "stop_time=[Sat 2026-11-21 09:50:36 UTC] ; pid=498 ; code=exited ; status=0 }\n"
            "User=\nLoadState=loaded\n"
            "FragmentPath=/usr/lib/systemd/system/systemd-tmpfiles-setup.service\n"
        )
        properties = acceptance._parse_effective_properties(output)
        self.assertEqual(properties["LoadState"], "loaded")
        self.assertEqual(acceptance._effective_argv(properties["ExecStart"]), (
            "systemd-tmpfiles",
            "systemd-tmpfiles --create --remove --boot --exclude-prefix=/dev",
        ))

    def test_effective_policy_diagnostics_are_safe_stable_and_bounded(self):
        self.assertEqual(acceptance._safe_effective_observation("NoNewPrivileges", "maybe"),
                         "<unrecognized>")
        self.assertEqual(acceptance._safe_effective_observation("User", "Bearer ghp_secret"),
                         "<unexpected identity>")
        self.assertEqual(acceptance._safe_effective_observation(
            "ExecStart", "--token=ghp_secret\n\x1b[31mforged"),
            "MISMATCH (command details omitted)")
        records = [
            {"unit": unit, "property": "NoNewPrivileges", "expected": "yes",
             "observed": acceptance._safe_effective_observation("NoNewPrivileges", "maybe")}
            for unit in reversed(acceptance.SYSTEM_UNITS)
        ]
        rendered = acceptance._render_effective_policy_mismatches(records)
        self.assertLessEqual(len(rendered.encode("utf-8")), acceptance._POLICY_DIAGNOSTIC_LIMIT)
        decoded = json.loads(rendered.removeprefix("systemd_effective_mismatches="))
        self.assertEqual([item["unit"] for item in decoded["mismatches"]],
                         list(acceptance.SYSTEM_UNITS))
        self.assertEqual(decoded["mismatch_count"], len(acceptance.SYSTEM_UNITS))

        many = records * 100
        truncated = acceptance._render_effective_policy_mismatches(many)
        self.assertLessEqual(len(truncated.encode("utf-8")), acceptance._POLICY_DIAGNOSTIC_LIMIT)
        truncated_data = json.loads(truncated.removeprefix("systemd_effective_mismatches="))
        self.assertGreater(truncated_data["omitted_count"], 0)
        self.assertEqual(truncated_data["mismatch_count"], len(many))
        self.assertNotIn("ghp_secret", rendered + truncated)

        with mock.patch.object(acceptance, "_POLICY_DIAGNOSTIC_LIMIT", 700):
            boundary = acceptance._render_effective_policy_mismatches(records * 1000)
            self.assertLessEqual(len(boundary.encode("utf-8")), 700)
            boundary_data = json.loads(boundary.removeprefix("systemd_effective_mismatches="))
            self.assertEqual(boundary_data["omitted_count"],
                             boundary_data["mismatch_count"] - len(boundary_data["mismatches"]))

    def test_conditions_observation_exposes_only_safe_state_labels(self):
        raw_payload = f"{acceptance.CONFIG}/code-update.in-progress"
        for label in ("present", "missing", "conflicting", "lookup-failed",
                      "unavailable", "malformed/unsupported", "identity-mismatch",
                      "load-state-mismatch"):
            with self.subTest(label=label):
                self.assertEqual(acceptance._safe_effective_observation("Conditions", label), label)
        for value in (raw_payload, f"a(sbbsi) 1 ConditionPathExists {raw_payload}",
                      "secret payload", "unexpected-label"):
            with self.subTest(value=value):
                observed = acceptance._safe_effective_observation("Conditions", value)
                self.assertEqual(observed, "malformed/unsupported")
                self.assertNotIn(value, observed)

    def test_systemd_dbus_unit_object_path_parser_accepts_captured_output(self):
        output = 'o "/org/freedesktop/systemd1/unit/dbus_2eservice"'
        self.assertEqual(acceptance._parse_systemd_unit_object_path(output),
                         "/org/freedesktop/systemd1/unit/dbus_2eservice")
        self.assertEqual(acceptance._expected_systemd_unit_object_path("debug-shell.service"),
                         "/org/freedesktop/systemd1/unit/debug_2dshell_2eservice")
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance._expected_systemd_unit_object_path("../debug-shell.service")
        for malformed in (
            "", 'o "not-an-object-path"',
            'o "/org/freedesktop/systemd1/unit/dbus_2eservice" extra',
            's "/org/freedesktop/systemd1/unit/dbus_2eservice"',
            'o /org/freedesktop/systemd1/unit/dbus_2eservice',
        ):
            with self.subTest(output=malformed), self.assertRaises(acceptance.AcceptanceError):
                acceptance._parse_systemd_unit_object_path(malformed)

    def test_effective_conditions_json_parser_accepts_captured_systemd_255_output(self):
        # Captured from busctl --system --json=short on systemd 255.4.
        output = (b'{"type":"a(sbbsi)","data":[["ConditionPathExists",false,true,'
                  b'"/etc/ssh/sshd_not_to_be_run",1]]}')
        parsed = acceptance._parse_effective_conditions_json(output)
        self.assertEqual(parsed, (("ConditionPathExists", False, True,
                                   "/etc/ssh/sshd_not_to_be_run", 1),))

    def test_effective_conditions_json_rejects_unsupported_or_malformed_data(self):
        malformed = [
            b'{"type":"a(sbbsi)","data":[]} trailing',
            b'{"type":"a(sbbsi)","data":[],"result":true}',
            b'{"type":"a(sbbsi)"}',
            b'{"type":"a(sbbsi)","data":[],"extra":0}',
            b'{"type":"a(sbbsi)","type":"a(sbbsi)","data":[]}',
            b'{"type":"a(ssbbi)","data":[]}',
            b'{"type":"a(sbbsi)","data":null}',
            b'{"type":"z","data":[]}',
            b'{"type":"a(sbbsi)","data":[[]]}',
            b'{"type":"a(sbbsi)","data":[["ConditionPathExists",false,true,"/marker"]]}',
            b'{"type":"a(sbbsi)","data":[["ConditionPathExists",false,true,"/marker",1,2]]}',
            b'{"type":"a(sbbsi)","data":[["ConditionPathExists",0,true,"/marker",1]]}',
            b'{"type":"a(sbbsi)","data":[["ConditionPathExists",false,true,"/marker",true]]}',
            b'{"type":"a(sbbsi)","data":[["ConditionPathExists",false,true,"/marker",2147483648]]}',
            b'{"type":"a(sbbsi)","data":[["ConditionPathExists",false,true,"/marker",-2147483649]]}',
            b'{"type":"a(sbbsi)","data":[["ConditionPathExists",false,true,"/marker",NaN]]}',
        ]
        for output in malformed:
            with self.subTest(output=output), self.assertRaises(acceptance.AcceptanceError):
                acceptance._parse_effective_conditions_json(output)
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance._parse_effective_conditions_json(b" " * 65537)

        valid_record = ["other", False, True, "/unrelated", 1]
        exact_limit = json.dumps({"type": "a(sbbsi)", "data": [valid_record]},
                                 separators=(",", ":" )).encode()
        padded = exact_limit + b" " * (16384 - len(exact_limit))
        self.assertEqual(len(padded), 16384)
        self.assertEqual(len(acceptance._parse_effective_conditions_json(padded)), 1)
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance._parse_effective_conditions_json(padded + b" ")

        records = [["other", False, True, f"/unrelated/{index}", 1] for index in range(128)]
        self.assertEqual(len(acceptance._parse_effective_conditions_json(
            json.dumps({"type": "a(sbbsi)", "data": records}).encode())), 128)

        records = [["other", False, True, f"/unrelated/{index}", 1] for index in range(129)]
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance._parse_effective_conditions_json(
                json.dumps({"type": "a(sbbsi)", "data": records}).encode())

    def test_systemd_string_property_json_is_typed_bounded_and_unambiguous(self):
        self.assertEqual(acceptance._parse_systemd_string_property_json(
            b'{"type":"s","data":"loaded"}'), "loaded")
        malformed = (
            b'{"type":"s","data":"loaded"} trailing',
            b'{"type":"s","data":"loaded","extra":0}',
            b'{"type":"s","type":"s","data":"loaded"}',
            b'{"type":"u","data":"loaded"}',
            b'{"type":"s","data":1}',
            b'{"type":"s","data":null}',
            b'{"type":"s","data":NaN}',
            b'\xff',
            b" " * 4097,
        )
        for output in malformed:
            with self.subTest(output=output), self.assertRaises(acceptance.AcceptanceError):
                acceptance._parse_systemd_string_property_json(output)

    def test_maintenance_condition_state_requires_one_exact_negated_marker(self):
        marker = "/etc/little-mester-vm/code-update.in-progress"
        expected = ("ConditionPathExists", False, True, marker, 1)
        self.assertEqual(acceptance._maintenance_condition_state((expected,), marker), "present")
        self.assertEqual(acceptance._maintenance_condition_state(
            (("ConditionPathExists", False, True, marker, 0),), marker), "present")
        self.assertEqual(acceptance._maintenance_condition_state((), marker), "missing")

        wrong_marker_tuples = (
            ("ConditionFileNotEmpty", False, True, marker, 1),
            ("ConditionPathExists", True, True, marker, 1),
            ("ConditionPathExists", False, False, marker, 1),
        )
        for condition in wrong_marker_tuples:
            with self.subTest(condition=condition):
                self.assertEqual(acceptance._maintenance_condition_state((condition,), marker),
                                 "conflicting")

        for condition in (
            ("ConditionPathExists", False, True, "/etc/other-marker", 1),
            ("ConditionPathExists", False, True, f"!{marker}", 1),
        ):
            with self.subTest(condition=condition):
                self.assertEqual(acceptance._maintenance_condition_state((condition,), marker),
                                 "missing")

        for conditions in (
            (expected, expected),
            (expected, ("ConditionPathExists", False, False, marker, 0)),
        ):
            with self.subTest(conditions=conditions):
                self.assertEqual(acceptance._maintenance_condition_state(conditions, marker),
                                 "conflicting")

        unrelated = ("ConditionPathExists", False, False, "/etc/optional", 0)
        self.assertEqual(acceptance._maintenance_condition_state((unrelated, expected), marker),
                         "present")

    def test_effective_maintenance_condition_loads_then_checks_identity_state_and_policy(self):
        fixed_unit = "little-mester-vm.service"
        object_path = "/org/freedesktop/systemd1/unit/little_2dmester_2dvm_2eservice"
        marker = f"{acceptance.CONFIG}/code-update.in-progress"
        load_call = ["/usr/bin/busctl", "--system", "call", "org.freedesktop.systemd1",
                     "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                     "LoadUnit", "s", fixed_unit]
        identity_call = ["/usr/bin/busctl", "--system", "--json=short", "get-property",
                         "org.freedesktop.systemd1", object_path,
                         "org.freedesktop.systemd1.Unit", "Id"]
        load_state_call = ["/usr/bin/busctl", "--system", "--json=short", "get-property",
                           "org.freedesktop.systemd1", object_path,
                           "org.freedesktop.systemd1.Unit", "LoadState"]
        conditions_call = ["/usr/bin/busctl", "--system", "--json=short", "get-property",
                           "org.freedesktop.systemd1", object_path,
                           "org.freedesktop.systemd1.Unit", "Conditions"]

        def string_reply(command, value):
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"type": "s", "data": value}).encode(), b"")

        replies = [
            subprocess.CompletedProcess(load_call, 0,
                                        f'o "{object_path}"'.encode(), b""),
            string_reply(identity_call, fixed_unit),
            string_reply(load_state_call, "loaded"),
            subprocess.CompletedProcess(conditions_call, 0, json.dumps({
                "type": "a(sbbsi)", "data": [
                    ["ConditionPathExists", False, True, marker, 1],
                ]}).encode(), b""),
        ]
        with mock.patch.object(acceptance, "run", side_effect=replies) as run:
            self.assertEqual(acceptance._effective_maintenance_condition(fixed_unit), "present")
        self.assertEqual(run.call_args_list, [
            mock.call(load_call, operation="load effective systemd unit"),
            mock.call(identity_call, operation="read systemd unit Id"),
            mock.call(load_state_call, operation="read systemd unit LoadState"),
            mock.call(conditions_call, operation="read effective systemd Conditions"),
        ])

        for failed_call, expected_label in enumerate(
                ("lookup-failed", "unavailable", "unavailable", "unavailable")):
            with self.subTest(failed_call=failed_call):
                outcomes = list(replies)
                outcomes[failed_call] = acceptance.AcceptanceError("busctl failed")
                with mock.patch.object(acceptance, "run", side_effect=outcomes) as run:
                    self.assertEqual(acceptance._effective_maintenance_condition(fixed_unit),
                                     expected_label)
                self.assertEqual(run.call_count, failed_call + 1)

        for payload, label in ((b'{"type":"a(sbbsi)","data":[]}', "missing"),
                               (json.dumps({"type": "a(sbbsi)", "data": [
                                   ["ConditionPathExists", True, True, marker, 1],
                               ]}).encode(), "conflicting"),
                               (b'{bad json', "malformed/unsupported"),
                               (b'\xff', "malformed/unsupported"),
                               (b'{"type":"a(sbbsi)","data":null}', "malformed/unsupported")):
            with self.subTest(label=label):
                replies[3] = subprocess.CompletedProcess(conditions_call, 0, payload, b"")
                with mock.patch.object(acceptance, "run", side_effect=replies):
                    result = acceptance._effective_maintenance_condition(fixed_unit)
                self.assertEqual(result, label)

    def test_effective_maintenance_condition_rejects_wrong_path_identity_and_load_state(self):
        unit = acceptance.UNIT
        object_path = acceptance._expected_systemd_unit_object_path(unit)
        load = subprocess.CompletedProcess([], 0, f'o "{object_path}"'.encode(), b"")

        def string_reply(value):
            return subprocess.CompletedProcess([], 0, json.dumps(
                {"type": "s", "data": value}).encode(), b"")

        wrong_path = subprocess.CompletedProcess([], 0,
            b'o "/org/freedesktop/systemd1/unit/other_2eservice"', b"")
        with mock.patch.object(acceptance, "run", return_value=wrong_path) as run:
            self.assertEqual(acceptance._effective_maintenance_condition(unit),
                             "malformed/unsupported")
        run.assert_called_once()

        with mock.patch.object(acceptance, "run", side_effect=[load, string_reply("other.service")]) as run:
            self.assertEqual(acceptance._effective_maintenance_condition(unit), "identity-mismatch")
        self.assertEqual(run.call_count, 2)

        for state in ("not-found", "masked", "error"):
            with self.subTest(load_state=state), mock.patch.object(
                    acceptance, "run", side_effect=[load, string_reply(unit), string_reply(state)]) as run:
                self.assertEqual(acceptance._effective_maintenance_condition(unit),
                                 "load-state-mismatch")
            self.assertEqual(run.call_count, 3)

        malformed_identity = subprocess.CompletedProcess([], 0,
            b'{"type":"u","data":"little-mester-vm.service"}', b"")
        with mock.patch.object(acceptance, "run", side_effect=[load, malformed_identity]) as run:
            self.assertEqual(acceptance._effective_maintenance_condition(unit),
                             "malformed/unsupported")
        self.assertEqual(run.call_count, 2)

    @unittest.skipUnless(os.environ.get("LM_RUN_REAL_SYSTEMD_UNIT_TEST") == "1",
                         "opt-in: loads one installed unit definition without starting it")
    def test_live_unloaded_systemd_unit_loads_without_starting(self):
        unit = "debug-shell.service"
        unit_file = pathlib.Path("/usr/lib/systemd/system/debug-shell.service")
        if not unit_file.is_file():
            self.skipTest("debug-shell.service is not installed")

        get_unit = ["/usr/bin/busctl", "--system", "call", "org.freedesktop.systemd1",
                    "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                    "GetUnit", "s", unit]
        before = subprocess.run(get_unit, stdin=subprocess.DEVNULL,
                                capture_output=True, check=False,
                                env={**os.environ, "LC_ALL": "C"})
        if before.returncode == 0:
            self.skipTest("debug-shell.service is already resident; unloaded precondition unavailable")
        self.assertIn(b"Unit debug-shell.service not loaded", before.stderr)

        result = acceptance._effective_maintenance_condition(unit)
        self.assertIn(result, {"present", "missing", "conflicting"})

        object_path = acceptance._expected_systemd_unit_object_path(unit)
        values = {}
        for property_name in ("Id", "LoadState", "ActiveState"):
            command = ["/usr/bin/busctl", "--system", "--json=short", "get-property",
                       "org.freedesktop.systemd1", object_path,
                       "org.freedesktop.systemd1.Unit", property_name]
            response = subprocess.run(command, stdin=subprocess.DEVNULL,
                                      capture_output=True, check=False,
                                      env={**os.environ, "LC_ALL": "C"})
            self.assertEqual(response.returncode, 0, response.stderr.decode("utf-8", "replace"))
            values[property_name] = acceptance._parse_systemd_string_property_json(response.stdout)
        self.assertEqual(values, {
            "Id": unit, "LoadState": "loaded", "ActiveState": "inactive",
        })

    def test_system_acceptance_report_contains_protected_lifecycle_gates(self):
        report = owner.report_template("a" * 40, "system_vm_accept", pathlib.Path("attempt"))
        self.assertEqual(report["evidence"]["restoration_outcome"], "NOT_REQUIRED")
        self.assertIn("candidate", report["gates"])
        self.assertIn("deployed_candidate_verification", report["gates"])
        self.assertIn("same_disk_persistence", report["gates"])
        self.assertIn("final_vm_state", report["gates"])

    def test_acceptance_command_failure_has_bounded_sanitized_operation_diagnostic(self):
        result = subprocess.CompletedProcess(
            ["/fixed/command"], 17, b"", (
                b"safe failure detail\n"
                b"Permission denied (publickey).\n"
                b"https://alice:secret@example.com/repo\n"
                b"https://abc123456789@example.com/repo\n"
                b"Bearer ghp_1234567890abcdefghijklmnop\n"
                b"github_pat_abcdefghijklmnopqrstuvwxyz1234567890\n"
                b"-----BEGIN OPENSSH PRIVATE KEY-----\nprivate-key-material\n"
                b"-----END OPENSSH PRIVATE KEY-----\n"
                b"-----BEGIN OPENSSH PRIVATE KEY-----\nincomplete-private-key-material\n"
                b"ghp_1234567890abcdefghijklmnop\n" + b"x" * 5000))
        with mock.patch.object(acceptance.subprocess, "run", return_value=result):
            with self.assertRaises(acceptance.AcceptanceError) as raised:
                acceptance.run(["/fixed/command"], operation="verify_persistence_marker_after_restart")
        message = str(raised.exception)
        self.assertIn("verify_persistence_marker_after_restart", message)
        self.assertIn("exit=17", message)
        self.assertIn("safe failure detail", message)
        self.assertNotIn("Permission denied", message)
        self.assertNotIn("alice:secret", message)
        self.assertNotIn("abc123456789", message)
        self.assertNotIn("ghp_1234567890", message)
        self.assertNotIn("github_pat_abcdefghijklmnopqrstuvwxyz", message)
        self.assertNotIn("private-key-material", message)
        self.assertNotIn("incomplete-private-key-material", message)
        self.assertLess(len(message), 1400)

    def test_system_acceptance_failure_diagnostic_is_printed_to_ci_output(self):
        report = owner.report_template("a" * 40, "system_vm_accept", pathlib.Path("attempt"))
        report["evidence"]["failure_diagnostic"] = {
            "stage": "same_disk_persistence",
            "operation": "restart_vm_on_same_disk",
            "exception_type": "AcceptanceError",
            "reason": "restart_vm_on_same_disk failed (exit=1)",
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            owner.print_report(report)
        self.assertIn('system_vm_failure_diagnostic={"exception_type":"AcceptanceError"', output.getvalue())
        self.assertIn('"operation":"restart_vm_on_same_disk"', output.getvalue())

    def test_failure_report_write_error_does_not_skip_initial_state_restoration(self):
        stopped = {"ActiveState": "inactive", "MainPID": "0", "Result": "success"}

        def progress(name, **_evidence):
            if name == "failure_diagnostic":
                raise OSError("report storage unavailable")

        with mock.patch.object(acceptance, "verify", return_value=(stopped, stopped)), \
                mock.patch.object(acceptance, "_require_live_workspace_assignment"), \
                mock.patch.object(acceptance, "_initial_running_state", return_value=False), \
                mock.patch.object(acceptance, "_lifecycle_preflight"), \
                mock.patch.object(acceptance, "_proxy_policy_checks"), \
                mock.patch.object(acceptance, "disk_identity", return_value=(1, 2)), \
                mock.patch.object(acceptance, "run", side_effect=[
                    acceptance.AcceptanceError("initial VM start failed"),
                    subprocess.CompletedProcess([], 0, b"", b""),
                ]) as run, \
                mock.patch.object(acceptance, "service_state", return_value=stopped):
            with self.assertRaisesRegex(acceptance.AcceptanceError, "start_initial_vm"):
                acceptance.accept(pathlib.Path("/opt/candidate"), progress=progress)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.kwargs["operation"], "restore_initial_vm_state_down")

    def test_system_disk_paths_are_new_fixed_paths_and_never_parent_v3(self):
        self.assertEqual(str(vm.DISK), "/var/lib/little-mester-vm/disk.qcow2")
        self.assertEqual(str(vm.SEED), "/var/lib/little-mester-vm/seed.iso")
        self.assertNotIn("parent-v3", str(vm.DISK))
        self.assertNotIn("v3-smoke", str(vm.DISK))
        self.assertNotIn("/workspace", str(vm.DISK))

    def test_acceptance_persistence_marker_is_on_guest_disk_and_verified_before_removal(self):
        source = inspect.getsource(acceptance.accept)
        self.assertRegex(
            source,
            r'marker\s*=\s*f"\.lm-system-accept-[^" ]*',
        )
        self.assertNotIn('marker = f"/workspace/', source)
        self.assertNotIn('marker = f"/var/lib/', source)
        self.assertIn('GUEST_PERSISTENCE_STATE = "/home/mester/.local/state"',
                      inspect.getsource(acceptance))

        tags = list(re.finditer(r'(?m)^\s*operation = "([^"]+)"', source))
        positions = {match.group(1): match.start() for match in tags}

        def operation_segment(name, stop_name=None):
            start = positions[name]
            if stop_name is not None:
                end = positions[stop_name]
            else:
                end = next((match.start() for match in tags if match.start() > start), len(source))
            return source[start:end]

        create = operation_segment("create_and_verify_persistence_marker")
        self.assertIn("_guest_persistence_marker_command(", create)
        self.assertIn('"create"', create)
        self.assertNotIn("mkdir", create)

        proof = operation_segment("verify_persistence_marker_after_restart")
        self.assertIn("_guest_ssh(", proof)
        self.assertIn("_guest_persistence_marker_command(", proof)
        self.assertIn('"verify"', proof)

        remove_position = positions["remove_persistence_marker"]
        failure_position = source.index("except BaseException as failure:")
        self.assertLess(positions["verify_persistence_marker_after_restart"], remove_position)
        self.assertLess(remove_position, failure_position)
        remove = operation_segment("remove_persistence_marker")
        self.assertIn("_guest_ssh(", remove)
        self.assertIn('"remove"', remove)

    def test_guest_persistence_marker_script_uses_no_follow_dirfds_and_virtio_blk_topology(self):
        script = acceptance.GUEST_PERSISTENCE_MARKER_SCRIPT
        for component in ('"home"', '"mester"', '".local"', '"state"'):
            self.assertIn(component, script)
        self.assertIn("os.open(component", script)
        self.assertIn("dir_fd=fds[-1]", script)
        self.assertIn("os.O_DIRECTORY | os.O_NOFOLLOW", script)
        self.assertIn("os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW", script)
        self.assertIn("os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK", script)
        self.assertIn("os.unlink(name, dir_fd=state_fd)", script)
        self.assertIn("mnt_id", script)
        self.assertIn("st_dev", script)
        self.assertIn("virtiofs", script)
        self.assertIn("virtio_blk", script)
        self.assertIn('SYSFS_ROOT = "/sys"', script)
        self.assertIn("/dev/block", script)
        self.assertNotIn("/dev/vda", script)
        self.assertNotIn("mkdir", script)

    def test_guest_persistence_marker_script_accepts_virtio_blk_and_uses_verified_dirfd(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            guest_root = base / "guest"
            sysfs_root = base / "sys"
            marker = ".lm-system-accept-testnonce"
            expected = "systemd-owner-acceptance-testnonce"
            (guest_root / "home/mester/.local/state").mkdir(parents=True, mode=0o700)
            (guest_root / "home").chmod(0o755)
            (guest_root / "home/mester").chmod(0o755)
            for path in (guest_root / "home/mester/.local", guest_root / "home/mester/.local/state"):
                path.chmod(0o700)
            whole_disk = sysfs_root / "devices/pci0000:00/virtio0/block/vdb"
            (whole_disk / "device").mkdir(parents=True)
            driver = sysfs_root / "bus/virtio/drivers/virtio_blk"
            driver.mkdir(parents=True)
            (whole_disk / "device/driver").symlink_to(driver)
            root_partition = whole_disk / "vdb1"
            root_partition.mkdir()
            (root_partition / "partition").write_text("1\n")
            device = os.stat(guest_root).st_dev
            dev_block = sysfs_root / "dev/block"
            dev_block.mkdir(parents=True)
            (dev_block / f"{os.major(device)}:{os.minor(device)}").symlink_to(root_partition)

            script = acceptance.GUEST_PERSISTENCE_MARKER_SCRIPT.replace(
                'ROOT_PATH = "/"', f"ROOT_PATH = {str(guest_root)!r}").replace(
                'SYSFS_ROOT = "/sys"', f"SYSFS_ROOT = {str(sysfs_root)!r}")

            def invoke(action):
                return subprocess.run(
                    ["python3", "-c", script, action, marker, expected],
                    text=True, capture_output=True, check=False)

            created = invoke("create")
            self.assertEqual(created.returncode, 0, created.stderr)
            marker_path = guest_root / "home/mester/.local/state" / marker
            self.assertEqual(marker_path.read_text(), expected)
            verified = invoke("verify")
            self.assertEqual(verified.returncode, 0, verified.stderr)
            removed = invoke("remove")
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertFalse(marker_path.exists())

    def test_guest_persistence_marker_refuses_symlink_at_every_path_component(self):
        components = ("home", "mester", ".local", "state")
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            sysfs_root = base / "sys"
            script_template = acceptance.GUEST_PERSISTENCE_MARKER_SCRIPT
            expected = "systemd-owner-acceptance-testnonce"
            marker = ".lm-system-accept-testnonce"
            for index, component in enumerate(components):
                guest_root = base / f"guest-{index}"
                target = base / f"outside-{index}"
                target.mkdir()
                parent = guest_root
                parent.mkdir()
                for prior in components[:index]:
                    parent = parent / prior
                    parent.mkdir(mode=0o700)
                (parent / component).symlink_to(target, target_is_directory=True)
                script = script_template.replace(
                    'ROOT_PATH = "/"', f"ROOT_PATH = {str(guest_root)!r}").replace(
                    'SYSFS_ROOT = "/sys"', f"SYSFS_ROOT = {str(sysfs_root)!r}")
                result = subprocess.run(
                    ["python3", "-c", script, "create", marker, expected],
                    text=True, capture_output=True, check=False)
                self.assertNotEqual(result.returncode, 0, component)
                self.assertFalse((target / marker).exists(), component)

    def test_guest_persistence_marker_fails_closed_for_non_virtio_blk_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            guest_root = base / "guest"
            sysfs_root = base / "sys"
            (guest_root / "home/mester/.local/state").mkdir(parents=True, mode=0o700)
            (guest_root / "home").chmod(0o755)
            (guest_root / "home/mester").chmod(0o755)
            for path in (guest_root / "home/mester/.local", guest_root / "home/mester/.local/state"):
                path.chmod(0o700)
            block = sysfs_root / "devices/pci0000:00/block/vdb"
            (block / "device").mkdir(parents=True)
            other_driver = sysfs_root / "bus/pci/drivers/other"
            other_driver.mkdir(parents=True)
            (block / "device/driver").symlink_to(other_driver)
            device = os.stat(guest_root).st_dev
            dev_block = sysfs_root / "dev/block"
            dev_block.mkdir(parents=True)
            (dev_block / f"{os.major(device)}:{os.minor(device)}").symlink_to(block)
            script = acceptance.GUEST_PERSISTENCE_MARKER_SCRIPT.replace(
                'ROOT_PATH = "/"', f"ROOT_PATH = {str(guest_root)!r}").replace(
                'SYSFS_ROOT = "/sys"', f"SYSFS_ROOT = {str(sysfs_root)!r}")
            result = subprocess.run(
                ["python3", "-c", script, "create", ".lm-system-accept-testnonce",
                 "systemd-owner-acceptance-testnonce"],
                text=True, capture_output=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((guest_root / "home/mester/.local/state/.lm-system-accept-testnonce").exists())

    def test_system_guest_seed_uses_canonical_parent_cloud_init_and_session_contract(self):
        data = bootstrap.build_foundation(
            name="little-mester-system-vm", uid=1000,
            ssh_key="ssh-ed25519 AAAA control", workspace="virtiofs",
            helper="/usr/local/libexec/lm-guest-user-dirs.py")
        contract.add_protected_session(
            data, host_private="private-key\n", host_public="ssh-ed25519 AAAA host\n",
            control_public="ssh-ed25519 AAAA control\n", workspace_path=contract.SYSTEM_WORKSPACE)
        self.assertEqual(data["hostname"], "little-mester-system-vm")
        self.assertTrue(data["ssh_keys"]["ed25519_private"].startswith("private-key"))
        files = {item["path"]: item for item in data["write_files"]}
        self.assertIn("/usr/local/bin/lm-opencode", files)
        self.assertIn("/usr/local/libexec/lm-opencode-auth", files)
        self.assertIn("/usr/local/libexec/lm-guest-user-dirs.py", files)
        self.assertIn("/etc/opencode/plugins/little-mester-exit-confirm/tui.tsx", files)
        self.assertIn("/etc/opencode/plugins/little-mester-host-metrics-tui/tui.tsx", files)
        self.assertIn("/etc/opencode/plugins/little-mester-host-metrics-tui/metrics-endpoint.js", files)
        self.assertIn("/etc/opencode/plugins/little-mester-host-metrics-tui/metrics-state.js", files)
        self.assertIn("/etc/opencode/opencode-model.json", files)
        model_config = json.loads(files["/etc/opencode/opencode-model.json"]["content"])
        self.assertEqual(model_config["model"], "{env:LM_OPENCODE_MODEL}")
        provision = files["/usr/local/sbin/mester-v3-provision"]["content"]
        self.assertIn("docker-ce-rootless-extras", provision)
        self.assertIn("dockerd-rootless-setuptool.sh install", provision)
        self.assertIn("prepare 1000", provision)
        self.assertIn("prepare 1000", provision)
        self.assertIn("@opencode/cli@2.2.5", provision)
        self.assertIn("apt-get install -y --no-install-recommends nodejs libnode-dev build-essential",
                      provision)
        self.assertNotIn("nodejs npm", provision)
        self.assertIn("python3 -I /usr/local/libexec/mester-vm-guest-bootstrap.py install-npm",
                      provision)
        embedded_bootstrap = files[bootstrap.NPM_GUEST_HELPER]
        self.assertEqual(base64.b64decode(embedded_bootstrap["content"]),
                         (ROOT / "lib/vm/guest-bootstrap.py").read_bytes())
        self.assertEqual(embedded_bootstrap["owner"], "root:root")
        self.assertEqual(embedded_bootstrap["permissions"], "0755")
        self.assertIn("prepare-auth", files["/usr/local/bin/lm-opencode"]["content"])
        self.assertIn("cd /workspace", files["/usr/local/bin/lm-opencode"]["content"])
        self.assertIn("lm-guest-opencode-preferences.py model", files["/usr/local/libexec/lm-opencode"]["content"])
        self.assertIn("workspace /workspace virtiofs", provision)
        self.assertNotIn("/home/mester/workspace", provision)
        system_source = (ROOT / "lib/vm/system-vm.py").read_text()
        self.assertIn("bootstrap.build_foundation(", system_source)
        self.assertIn("contract.add_protected_session(", system_source)
        self.assertFalse(data["ssh_pwauth"])
        self.assertTrue(data["disable_root"])
        self.assertTrue(any(item["path"] == "/etc/apt/apt.conf.d/80mester-proxy"
                            for item in data["write_files"]))
        legacy = contract.parent_cloud_init_data(
            host_private="private-key\n", host_public="ssh-ed25519 AAAA host\n",
            control_public="ssh-ed25519 AAAA control\n")
        legacy_launcher = next(item["content"] for item in legacy["write_files"]
                               if item.get("path") == "/usr/local/bin/lm-opencode")
        self.assertIn("cd /home/mester/workspace", legacy_launcher)
        self.assertNotIn("--host-metrics", legacy_launcher)
        legacy_profile = next(item["content"] for item in legacy["write_files"]
                              if item.get("path") == "/etc/profile.d/little-mester-opencode.sh")
        self.assertNotIn("host-metrics-tui", legacy_profile)
        with self.assertRaises(ValueError):
            bootstrap.build_foundation(name="bad/name", uid=1000, ssh_key="key",
                                       workspace="disk", helper="/usr/local/libexec/lm-guest-user-dirs.py")

    def test_smoke_and_system_use_same_provisioning_builder_with_workspace_specific_adaptation(self):
        helper = "/usr/local/sbin/mester-v3-user-dirs.py"
        smoke_script = bootstrap.provision_script(1234, "virtiofs", helper)
        smoke_yaml = bootstrap.render_smoke_cloud_init(
            "smoke-v3", 1234,
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE7zJmK1W1h3V4eA7qQKX8U4n9x0z8m1c2b3d4e5f6g7 test")
        system_script = bootstrap.provision_script(
            1000, "virtiofs", "/usr/local/libexec/lm-guest-user-dirs.py")
        package_json_normalization = (
            "python3 -I /usr/local/libexec/mester-vm-guest-bootstrap.py "
            "normalize-opencode-package-json")
        for provision in (smoke_script, system_script):
            with self.subTest(provision="shared npm install"):
                self.assertLess(
                    provision.index("npm install --global @opencode/cli@2.2.5"),
                    provision.index(package_json_normalization))
        self.assertIn("guest-bootstrap.py\" smoke-cloud-init", (ROOT / "lib/vm/cloud-init.sh").read_text())
        self.assertIn("apt-get install -y docker-ce docker-ce-cli containerd.io", smoke_script)
        self.assertIn("apt-get install -y docker-ce docker-ce-cli containerd.io", system_script)
        self.assertIn("/workspace virtiofs", smoke_script)
        self.assertIn("workspace /workspace virtiofs", system_script)
        self.assertNotIn("/home/mester/workspace", system_script)
        self.assertIn("mountpoint -q /workspace", vm._guest_workspace_mount.__code__.co_consts[1])
        self.assertNotIn("sudo -n", vm._guest_workspace_mount.__code__.co_consts[1])
        self.assertIn("export DOCKER_HOST=unix:///run/user/1234/docker.sock", smoke_yaml)
        self.assertIn("NO_PROXY=localhost,127.0.0.1,10.0.2.100", smoke_yaml)
        self.assertIn("disable_root: true", smoke_yaml)
        self.assertIn("docker-ce-rootless-extras", smoke_yaml)
        self.assertIn("nodejs libnode-dev build-essential", smoke_yaml)
        self.assertNotIn("apt-get install -y ca-certificates curl uidmap dbus-user-session slirp4netns fuse-overlayfs kmod iptables nodejs npm",
                         smoke_yaml)
        bootstrap_start = smoke_yaml.index(
            "  - path: /usr/local/libexec/mester-vm-guest-bootstrap.py\n")
        bootstrap_end = smoke_yaml.find("\n  - path:", bootstrap_start + 1)
        bootstrap_block = smoke_yaml[bootstrap_start:bootstrap_end]
        bootstrap_match = re.search(
            r"(?m)^    content: ([A-Za-z0-9+/=]+)$", bootstrap_block)
        self.assertIsNotNone(bootstrap_match)
        embedded_source = base64.b64decode(bootstrap_match.group(1)).decode("utf-8")
        self.assertIn('NPM_CHANNEL_URL = "https://registry.npmjs.org/npm/next-10"',
                      embedded_source)
        generated = bootstrap.build_foundation(
            name="system-test", uid=1000, ssh_key="ssh-ed25519 AAAA test",
            workspace="disk", helper="/usr/local/libexec/lm-guest-user-dirs.py")
        self.assertEqual(
            base64.b64decode(next(item for item in generated["write_files"]
                                  if item["path"] == bootstrap.NPM_GUEST_HELPER)["content"]),
            (ROOT / "lib/vm/guest-bootstrap.py").read_bytes())

    def test_opencode_package_json_normalization_sets_0644_idempotently(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="vm-opencode-package-") as temporary:
            target = pathlib.Path(temporary) / "package.json"
            target.write_text('{"version":"2.2.5"}\n', encoding="utf-8")
            target.chmod(0o755)
            inspected = [
                mock.Mock(st_mode=stat.S_IFREG | 0o755, st_uid=0, st_gid=0),
                mock.Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0, st_gid=0),
                mock.Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0, st_gid=0),
                mock.Mock(st_mode=stat.S_IFREG | 0o644, st_uid=0, st_gid=0),
            ]
            with mock.patch.object(
                    bootstrap.os, "fstat", side_effect=inspected) as fstat:
                bootstrap.normalize_opencode_package_json(target)
                bootstrap.normalize_opencode_package_json(target)
            self.assertEqual(fstat.call_count, 4)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_opencode_package_json_normalization_refuses_invalid_targets_without_chmod(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="vm-opencode-package-") as temporary:
            root = pathlib.Path(temporary)
            target = root / "package.json"
            target.write_text('{"version":"2.2.5"}\n', encoding="utf-8")
            target.chmod(0o755)

            symlink = root / "package-link.json"
            symlink.symlink_to(target)
            with self.assertRaises(bootstrap.NpmBootstrapError):
                bootstrap.normalize_opencode_package_json(symlink)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

            directory = root / "package-directory"
            directory.mkdir(mode=0o755)
            with self.assertRaises(bootstrap.NpmBootstrapError):
                bootstrap.normalize_opencode_package_json(directory)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)

            for uid, gid in ((1000, 0), (0, 1000)):
                with self.subTest(uid=uid, gid=gid):
                    synthetic_info = mock.Mock(
                        st_mode=stat.S_IFREG | 0o755, st_uid=uid, st_gid=gid)
                    with mock.patch.object(
                            bootstrap.os, "fstat", return_value=synthetic_info):
                        with self.assertRaisesRegex(
                                bootstrap.NpmBootstrapError, "root:root"):
                            bootstrap.normalize_opencode_package_json(target)
                    self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_npm_channel_metadata_and_sha512_are_strictly_validated(self):
        artifact = b"npm fixture artifact"
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(artifact).digest()).decode("ascii")
        metadata = {
            "name": "npm",
            "version": "10.9.11",
            "engines": {"node": "^18.17.0 || >=20.5.0"},
            "dist": {
                "tarball": "https://registry.npmjs.org/npm/-/npm-10.9.11.tgz",
                "integrity": integrity,
                "unpackedSize": 123,
            },
        }
        resolved = bootstrap.validate_npm_metadata(metadata)
        self.assertEqual(resolved["version"], "10.9.11")
        self.assertEqual(resolved["node_range"], "^18.17.0 || >=20.5.0")
        bootstrap.verify_npm_integrity(artifact, integrity)
        with self.assertRaises(bootstrap.NpmBootstrapError):
            bootstrap.verify_npm_integrity(artifact + b"tampered", integrity)
        for update in (
            {"version": "11.0.0"},
            {"version": "10.9.11-rc.1"},
            {"dist": {**metadata["dist"], "tarball": "https://attacker.invalid/npm.tgz"}},
            {"dist": {**metadata["dist"], "integrity": "sha1-deadbeef"}},
            {"engines": {}},
        ):
            invalid = {**metadata, **update}
            with self.subTest(update=update), self.assertRaises(bootstrap.NpmBootstrapError):
                bootstrap.validate_npm_metadata(invalid)

    def test_npm_global_prefix_setup_preserves_other_and_user_configuration(self):
        with tempfile.TemporaryDirectory(prefix="lm-npm-global-config-") as temporary:
            root = pathlib.Path(temporary)
            config = root / "usr/etc/npmrc"
            bootstrap._ensure_global_prefix_config(config)
            self.assertEqual(config.read_text(), "prefix=/usr/local\n")

            config.write_text("cache=/tmp/npm-cache\n")
            bootstrap._ensure_global_prefix_config(config)
            self.assertEqual(config.read_text(),
                             "cache=/tmp/npm-cache\nprefix=/usr/local\n")

            custom = root / "custom.npmrc"
            custom.write_text("prefix=/custom/global\ncache=/tmp/npm-cache\n")
            custom.chmod(0o644)
            before = custom.read_bytes()
            bootstrap._ensure_global_prefix_config(custom)
            self.assertEqual(custom.read_bytes(), before)

            target = root / "target.npmrc"
            target.write_text("prefix=/usr/local\n")
            linked = root / "linked.npmrc"
            linked.symlink_to(target)
            with self.assertRaises(bootstrap.NpmBootstrapError):
                bootstrap._ensure_global_prefix_config(linked)

    def test_npm_policy_accepts_prefix_derived_globalconfig_and_rejects_bad_values(self):
        version = "10.9.11"
        node_range = "^18.17.0 || >=20.5.0"
        package_files = {
            "package/package.json": json.dumps({
                "name": "npm", "version": version,
                "engines": {"node": node_range},
                "bin": {"npm": "bin/npm-cli.js", "npx": "bin/npx-cli.js"},
            }).encode(),
            "package/bin/npm-cli.js": b"#!/usr/bin/env node\n",
            "package/bin/npx-cli.js": b"#!/usr/bin/env node\n",
            "package/node_modules/node-gyp/bin/node-gyp.js": b"#!/usr/bin/env node\n",
            "package/node_modules/semver/package.json": b'{"name":"semver","version":"7.6.3"}',
        }
        archive_bytes = io.BytesIO()
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            for name, content in package_files.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                member.mode = 0o755 if name.endswith(".js") else 0o644
                archive.addfile(member, io.BytesIO(content))
        artifact = archive_bytes.getvalue()
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(artifact).digest()).decode("ascii")
        metadata = {
            "name": "npm", "version": version, "engines": {"node": node_range},
            "dist": {
                "tarball": f"https://registry.npmjs.org/npm/-/npm-{version}.tgz",
                "integrity": integrity,
                "unpackedSize": sum(map(len, package_files.values())),
            },
        }
        metadata_bytes = json.dumps(metadata).encode()

        class RegistryResponse(io.BytesIO):
            def __init__(self, body, url):
                super().__init__(body)
                self.url = url

            def geturl(self):
                return self.url

        cases = (
            ("expected", version, "/usr/local", "/usr/local/etc/npmrc", False),
            ("bad-version", "10.9.10", "/usr/local", "/usr/local/etc/npmrc", True),
            ("bad-prefix", version, "/opt/npm", "/usr/etc/npmrc", True),
            ("bad-globalconfig", version, "/usr/local", "/opt/etc/npmrc", True),
        )
        for label, actual_version, actual_prefix, confirmed_config, should_fail in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory(
                    prefix="lm-npm-policy-") as temporary:
                root = pathlib.Path(temporary)
                home = root / "home"
                home.mkdir()
                install_root = root / "usr/local/lib/node_modules"
                bin_dir = root / "usr/local/bin"
                selected_config = root / "usr/etc/npmrc"
                calls = {"globalconfig": 0}

                def registry_response(_opener, url, *, timeout, accept):
                    body = (metadata_bytes if url == bootstrap.NPM_CHANNEL_URL else artifact)
                    return RegistryResponse(body, url)

                def run_npm(_node, _cli, *arguments, env=None,
                            actual_version=actual_version, actual_prefix=actual_prefix,
                            selected_config=selected_config,
                            confirmed_config=confirmed_config, calls=calls):
                    if arguments == ("--version",):
                        return actual_version
                    if arguments == ("config", "get", "prefix"):
                        return actual_prefix
                    if arguments == ("config", "get", "globalconfig"):
                        calls["globalconfig"] += 1
                        return (str(selected_config) if calls["globalconfig"] == 1
                                else confirmed_config)
                    self.fail(f"unexpected npm query: {arguments!r}")

                path = f"{bin_dir}:/usr/bin:/bin"
                with mock.patch.dict(os.environ, {
                        "HOME": str(home), "PATH": path, "TOKEN": "do-not-log",
                }, clear=True), \
                        mock.patch.object(bootstrap, "_open_registry",
                                          side_effect=registry_response), \
                        mock.patch.object(bootstrap, "_node_version", return_value="18.19.1"), \
                        mock.patch.object(bootstrap, "_verify_node_engine"), \
                        mock.patch.object(bootstrap, "_run_npm", side_effect=run_npm), \
                        contextlib.redirect_stderr(io.StringIO()) as diagnostics:
                    if should_fail:
                        with self.assertRaisesRegex(
                                bootstrap.NpmBootstrapError,
                                "installed npm version or global prefix"):
                            bootstrap.install_npm(
                                install_root=str(install_root), bin_dir=str(bin_dir),
                                node="/usr/bin/node", opener=object())
                    else:
                        result = bootstrap.install_npm(
                            install_root=str(install_root), bin_dir=str(bin_dir),
                            node="/usr/bin/node", opener=object())
                        self.assertEqual(result["version"], version)
                        self.assertEqual(result["prefix"], "/usr/local")
                        self.assertEqual(result["globalconfig"], str(selected_config))

                if should_fail:
                    output = diagnostics.getvalue()
                    self.assertIn("npm bootstrap policy diagnostic:", output)
                    self.assertIn("expected_version='10.9.11'", output)
                    self.assertIn(f"observed_version='{actual_version}'", output)
                    self.assertIn("expected_prefix='/usr/local'", output)
                    self.assertIn(f"observed_prefix='{actual_prefix}'", output)
                    self.assertIn("npm_cli_realpath=", output)
                    self.assertIn("path_npm_realpath=", output)
                    self.assertNotIn("do-not-log", output)
                    self.assertLess(len(output), 4000)
                else:
                    self.assertEqual(diagnostics.getvalue(), "")
                    self.assertEqual(selected_config.read_text(), "prefix=/usr/local\n")

    def test_npm_tarball_extraction_refuses_paths_outside_package(self):
        with tempfile.TemporaryDirectory(prefix="lm-npm-tar-safety-") as temporary:
            root = pathlib.Path(temporary)
            archive_path = root / "unsafe.tgz"
            with tarfile.open(archive_path, "w:gz") as archive:
                entry = tarfile.TarInfo("package/../../outside")
                entry.size = 1
                archive.addfile(entry, io.BytesIO(b"x"))
            with self.assertRaises(bootstrap.NpmBootstrapError):
                bootstrap._safe_extract_npm(archive_path, root / "extract")
            self.assertFalse((root / "outside").exists())

    def test_installed_session_contract_resolves_colocated_plugin_assets(self):
        with tempfile.TemporaryDirectory(prefix="lm-session-install-layout-") as temporary:
            installed = pathlib.Path(temporary) / "libexec/little-mester-vm"
            plugin_dir = installed / "plugins"
            plugin_dir.mkdir(parents=True)
            metrics_plugin_dir = installed / "host-metrics-tui"
            metrics_plugin_dir.mkdir()
            shutil.copyfile(ROOT / "lib/vm/guest-user-dirs.py", installed / "guest-user-dirs.py")
            shutil.copyfile(ROOT / "lib/vm/guest-opencode-preferences.py",
                            installed / "guest-opencode-preferences.py")
            shutil.copyfile(ROOT / "lib/vm/guest-bootstrap.py", installed / "guest-bootstrap.py")
            shutil.copyfile(ROOT / "lib/vm/parent-session-contract.py",
                            installed / "parent-session-contract.py")
            source_plugins = ROOT / "agent/opencode/plugins/little-mester-exit-confirm"
            shutil.copyfile(source_plugins / "package.json", plugin_dir / "package.json")
            shutil.copyfile(source_plugins / "tui.tsx", plugin_dir / "tui.tsx")
            source_metrics_plugin = ROOT / "agent/opencode/plugins/host-metrics-tui"
            shutil.copyfile(source_metrics_plugin / "package.json", metrics_plugin_dir / "package.json")
            shutil.copyfile(source_metrics_plugin / "tui.tsx", metrics_plugin_dir / "tui.tsx")
            shutil.copyfile(source_metrics_plugin / "metrics-endpoint.js", metrics_plugin_dir / "metrics-endpoint.js")
            shutil.copyfile(source_metrics_plugin / "metrics-endpoint.d.ts", metrics_plugin_dir / "metrics-endpoint.d.ts")
            shutil.copyfile(source_metrics_plugin / "metrics-state.js", metrics_plugin_dir / "metrics-state.js")
            shutil.copyfile(source_metrics_plugin / "metrics-state.d.ts", metrics_plugin_dir / "metrics-state.d.ts")
            (plugin_dir / "package.json").chmod(0o644)
            (plugin_dir / "tui.tsx").chmod(0o644)
            for asset in metrics_plugin_dir.iterdir():
                asset.chmod(0o644)
            installed_contract = load("installed_parent_session_contract",
                                      installed / "parent-session-contract.py")
            self.assertEqual(installed_contract.PLUGIN_ROOT, plugin_dir)
            data = installed_contract.parent_cloud_init_data(
                host_private="private-key\n", host_public="ssh-ed25519 AAAA host\n",
                control_public="ssh-ed25519 AAAA control\n")
            self.assertTrue(any(item["path"].endswith("/tui.tsx") for item in data["write_files"]))

    def test_qmp_handshake_powerdown_and_pidfd_wait(self):
        with tempfile.TemporaryDirectory(prefix="lm-qmp-test-") as directory:
            path = pathlib.Path(directory) / "qmp.sock"
            transcript = pathlib.Path(directory) / "commands"
            server = textwrap.dedent("""
                import os, socket, sys
                path, transcript = sys.argv[1:]
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                server.bind(path); os.chmod(path, 0o600); server.listen(1)
                connection, _ = server.accept()
                connection.sendall(b'{"QMP":{"version":{},"capabilities":[]}}\\r\\n')
                with open(transcript, 'w') as output:
                    for _ in range(2):
                        line = connection.makefile('rb').readline()
                        output.write(line.decode()); output.flush()
                        connection.sendall(b'{"return":{}}\\r\\n')
                connection.close(); server.close()
            """)
            process = subprocess.Popen(["/usr/bin/python3", "-c", server, str(path), str(transcript)])
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not path.exists():
                time.sleep(0.01)
            try:
                with mock.patch.object(vm, "QMP_SOCKET_PATH", path), \
                     mock.patch.object(vm, "_validate_runtime_directory"):
                    vm.qmp_powerdown_and_wait(process.pid, timeout_seconds=3)
                self.assertEqual(process.wait(timeout=2), 0)
                self.assertEqual(transcript.read_text().splitlines(), [
                    '{"execute":"qmp_capabilities"}', '{"execute":"system_powerdown"}'])
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_qmp_timeout_is_reported_for_systemd_cgroup_fallback(self):
        source = (ROOT / "lib/vm/system-vm.py").read_text()
        self.assertIn("QEMU did not exit after bounded QMP system_powerdown; systemd KILL fallback required", source)
        self.assertIn("TimeoutStopSec=25s", (ROOT / "systemd/little-mester-vm.service").read_text())

    def test_qmp_powerdown_timeout_returns_failure_for_systemd_fallback(self):
        with tempfile.TemporaryDirectory(prefix="lm-qmp-timeout-") as directory:
            path = pathlib.Path(directory) / "qmp.sock"
            server = textwrap.dedent("""
                import os, socket, sys, time
                path = sys.argv[1]
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                server.bind(path); os.chmod(path, 0o600); server.listen(1)
                connection, _ = server.accept()
                connection.sendall(b'{"QMP":{"version":{},"capabilities":[]}}\\r\\n')
                stream = connection.makefile('rb')
                for _ in range(2):
                    stream.readline()
                    connection.sendall(b'{"return":{}}\\r\\n')
                time.sleep(3)
            """)
            process = subprocess.Popen(["/usr/bin/python3", "-c", server, str(path)])
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not path.exists():
                time.sleep(0.01)
            try:
                with mock.patch.object(vm, "QMP_SOCKET_PATH", path), \
                     mock.patch.object(vm, "_validate_runtime_directory"):
                    with self.assertRaisesRegex(vm.Refused, "systemd KILL fallback required"):
                        vm.qmp_powerdown_and_wait(process.pid, timeout_seconds=0.2)
            finally:
                process.kill()
                process.wait()


class StageTimerTests(unittest.TestCase):
    """Contract tests for the proposed safe, allowlisted stage timer API.

    Assumed API: StageTimer(stream, wall_clock=..., monotonic=...) exposes a
    ``timed_stage(label)`` context manager. It emits compact JSON records as
    ``system_vm_stage=<json>`` lines. The source implementation is intentionally
    not part of this tests-only change.
    """

    class FlushRecordingStream(io.StringIO):
        def __init__(self):
            super().__init__()
            self.flush_snapshots = []

        def flush(self):
            self.flush_snapshots.append(self.getvalue())
            super().flush()

    @staticmethod
    def fixed_wall_clock():
        values = iter((
            datetime.datetime(2026, 11, 21, 12, 0, 0, tzinfo=datetime.timezone.utc),
            datetime.datetime(2026, 11, 21, 12, 0, 1, 250000, tzinfo=datetime.timezone.utc),
            datetime.datetime(2026, 11, 21, 12, 0, 2, tzinfo=datetime.timezone.utc),
            datetime.datetime(2026, 11, 21, 12, 0, 2, 500000, tzinfo=datetime.timezone.utc),
        ))
        return lambda: next(values)

    @staticmethod
    def line(event, label, timestamp, **extra):
        record = {"event": event, "label": label, "timestamp": timestamp, **extra}
        return "system_vm_stage=" + json.dumps(record, separators=(",", ":")) + "\n"

    def test_allowlisted_records_are_exact_utc_paired_timed_and_flushed(self):
        output = self.FlushRecordingStream()
        timer = acceptance.StageTimer(
            output, wall_clock=self.fixed_wall_clock(), monotonic=iter((4.0, 4.125, 8.0, 8.010)).__next__)

        with timer.timed_stage("guest_readiness_ssh"):
            pass
        with timer.timed_stage("guest_readiness_ssh"):
            pass

        expected = (
            self.line("START", "guest_readiness_ssh", "2026-11-21T12:00:00Z")
            + self.line("END", "guest_readiness_ssh", "2026-11-21T12:00:01.250000Z",
                        elapsed_ms=125, outcome="PASS")
            + self.line("START", "guest_readiness_ssh", "2026-11-21T12:00:02Z")
            + self.line("END", "guest_readiness_ssh", "2026-11-21T12:00:02.500000Z",
                        elapsed_ms=10, outcome="PASS")
        )
        self.assertEqual(output.getvalue(), expected)
        expected_records = expected.splitlines(keepends=True)
        self.assertEqual(output.flush_snapshots, [
            "".join(expected_records[:count]) for count in range(1, len(expected_records) + 1)])
        records = [json.loads(line.partition("=")[2]) for line in output.getvalue().splitlines()]
        self.assertEqual([(row["event"], row["label"]) for row in records], [
            ("START", "guest_readiness_ssh"), ("END", "guest_readiness_ssh"),
            ("START", "guest_readiness_ssh"), ("END", "guest_readiness_ssh")])
        self.assertTrue(all(row["timestamp"].endswith("Z") for row in records))

    def test_failure_records_fail_and_reraises_original_baseexception(self):
        output = io.StringIO()
        failure = KeyboardInterrupt("secret command and exception detail")
        timer = acceptance.StageTimer(
            output,
            wall_clock=lambda: datetime.datetime(2026, 11, 21, tzinfo=datetime.timezone.utc),
            monotonic=iter((1.0, 3.5)).__next__)
        with self.assertRaises(KeyboardInterrupt) as raised:
            with timer.timed_stage("network_boundaries"):
                raise failure
        self.assertIs(raised.exception, failure)
        self.assertEqual(output.getvalue(),
                         self.line("START", "network_boundaries", "2026-11-21T00:00:00Z")
                         + self.line("END", "network_boundaries", "2026-11-21T00:00:00Z",
                                     elapsed_ms=2500, outcome="FAIL"))

    def test_invalid_label_is_rejected_before_any_output(self):
        output = io.StringIO()
        timer = acceptance.StageTimer(output, wall_clock=self.fixed_wall_clock(), monotonic=lambda: 0)
        with self.assertRaises(ValueError):
            with timer.timed_stage("run-command-with-secret-token"):
                self.fail("invalid stage must not enter")
        self.assertEqual(output.getvalue(), "")

    def test_default_clock_requests_utc_and_naive_injected_clock_is_rejected(self):
        output = io.StringIO()
        timestamp = datetime.datetime(2026, 11, 21, tzinfo=datetime.timezone.utc)
        with mock.patch.object(acceptance.datetime, "datetime") as clock:
            clock.now.return_value = timestamp
            timer = acceptance.StageTimer(output, monotonic=iter((0.0, 0.0)).__next__)
            with timer.timed_stage("network_boundaries"):
                pass
            clock.now.assert_called_with(datetime.timezone.utc)

        output = io.StringIO()
        timer = acceptance.StageTimer(
            output, wall_clock=lambda: datetime.datetime(2026, 11, 21), monotonic=lambda: 0)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            with timer.timed_stage("network_boundaries"):
                pass
        self.assertEqual(output.getvalue(), "")

    def test_records_never_include_command_exception_or_secret_values(self):
        output = io.StringIO()
        timer = acceptance.StageTimer(
            output,
            wall_clock=lambda: datetime.datetime(2026, 11, 21, tzinfo=datetime.timezone.utc),
            monotonic=iter((0.0, 0.001)).__next__)
        with self.assertRaisesRegex(RuntimeError, "command-secret-value"):
            with timer.timed_stage("persistence_fixture"):
                raise RuntimeError("command-secret-value ghp_1234567890abcdefghijklmnop")
        rendered = output.getvalue()
        self.assertIn('"outcome":"FAIL"', rendered)
        for forbidden in ("command-secret-value", "ghp_1234567890", "RuntimeError", "secret"):
            self.assertNotIn(forbidden, rendered)


class HostMetricsAcceptanceTests(unittest.TestCase):
    @staticmethod
    def valid_payload():
        return {
            "source": "host", "run_id": "a" * 32, "sequence": 9,
            "timestamp": "2026-11-20T00:00:00.000Z",
            "cpu": None, "memory": None, "gpu": [],
            "availability": {
                "cpu": "unsupported", "memory": "permission_denied",
                "gpus": "no_device", "temperatures": "no_sensors",
            },
            "temperatures": None,
        }

    def setUp(self):
        with mock.patch.object(acceptance, "_guest_ssh") as guest_ssh:
            acceptance._host_metrics_check()
        guest_ssh.assert_called_once()
        self.command = guest_ssh.call_args.args[0]
        self.operation = guest_ssh.call_args.kwargs["operation"]
        argv = shlex.split(self.command)
        self.assertEqual(argv[:3], ["python3", "-I", "-c"])
        self.assertEqual(len(argv), 4)
        self.script = argv[3]
        self.last_events = []

    def execute_captured_script(self, payload, *, body=None, headers=None, post_status=405):
        if body is None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if headers is None:
            headers = {
                "Cache-Control": "no-store",
                "Content-Length": str(len(body)),
            }
        events = self.last_events = []

        class GetResponse:
            def __init__(self):
                self.headers = headers

            def read(self, limit=-1):
                events.append(("get-read", limit))
                return body if limit < 0 else body[:limit]

            def close(self):
                events.append("get-close")

        get_response = GetResponse()

        def urlopen(url, *, timeout):
            self.assertEqual(url, "http://10.0.2.100:19532/metrics")
            self.assertEqual(timeout, 3)
            events.append("get-request")
            return get_response

        class PostResponse:
            status = post_status

        class Connection:
            def __init__(self, host, port, *, timeout):
                if (host, port, timeout) != ("10.0.2.100", 19532, 3):
                    raise AssertionError("unexpected host metrics POST endpoint")
                events.append("post-connection")

            def request(self, method, target):
                if (method, target) != ("POST", "/metrics"):
                    raise AssertionError("unexpected host metrics POST request")
                events.append("post-request")

            def getresponse(self):
                events.append("post-response")
                return PostResponse()

            def close(self):
                events.append("post-close")

        with mock.patch.object(urllib.request, "urlopen", side_effect=urlopen), \
                mock.patch.object(http.client, "HTTPConnection", Connection):
            exec(compile(self.script, "<captured-host-metrics-command>", "exec"), {})
        return events

    def assert_rejected(self, payload, *, body=None, headers=None, post_status=405,
                        post_expected=False):
        with self.assertRaises((AssertionError, TypeError, ValueError)):
            self.execute_captured_script(payload, body=body, headers=headers,
                                         post_status=post_status)
        self.assertIn("get-close", self.last_events)
        self.assertEqual("post-request" in self.last_events, post_expected)
        if post_expected:
            self.assertIn("post-close", self.last_events)

    def test_captured_command_checks_contract_and_consumes_post_405(self):
        self.assertEqual(self.operation, "verify_host_metrics_transport")
        events = self.execute_captured_script(self.valid_payload())
        self.assertEqual(events, [
            "get-request", ("get-read", 16385), "get-close",
            "post-connection", "post-request", "post-response", "post-close",
        ])
        for sequence in (1, 2**63 - 1):
            with self.subTest(sequence=sequence):
                payload = self.valid_payload()
                payload["sequence"] = sequence
                self.execute_captured_script(payload)

    def test_allowed_availability_statuses_and_unavailable_nulls_are_accepted(self):
        statuses = (
            "available", "unsupported", "no_device", "no_sensors", "timeout",
            "permission_denied", "collector_failed", "malformed",
        )
        categories = ("cpu", "memory", "gpus", "temperatures")
        for index, status in enumerate(statuses):
            with self.subTest(status=status):
                payload = self.valid_payload()
                payload["availability"][categories[index % len(categories)]] = status
                self.execute_captured_script(payload)

    def test_exact_schema_rejects_legacy_missing_and_extra_fields(self):
        legacy = self.valid_payload()
        legacy.pop("run_id")
        legacy.pop("sequence")
        with self.subTest(case="legacy seven-field"):
            self.assert_rejected(legacy)

        for key in ("run_id", "sequence"):
            with self.subTest(missing=key):
                payload = self.valid_payload()
                del payload[key]
                self.assert_rejected(payload)

        payload = self.valid_payload()
        payload["unexpected"] = True
        self.assert_rejected(payload)

    def test_invalid_run_ids_and_sequences_are_rejected(self):
        for run_id in ("", None, 123, "a" * 31, "a" * 33,
                       "A" * 32, "g" * 32, "a" * 31 + "!"):
            with self.subTest(run_id=run_id):
                payload = self.valid_payload()
                payload["run_id"] = run_id
                self.assert_rejected(payload)

        for sequence in (True, None, "9", 9.0, 0, -1, 2**63):
            with self.subTest(sequence=sequence):
                payload = self.valid_payload()
                payload["sequence"] = sequence
                self.assert_rejected(payload)

    def test_existing_transport_source_gpu_availability_and_method_gates_remain(self):
        body = json.dumps(self.valid_payload(), separators=(",", ":")).encode()
        oversized = body + b" " * (16385 - len(body))
        self.assertEqual(len(oversized), 16385)
        self.assert_rejected(self.valid_payload(), body=oversized)

        cases = []
        payload = self.valid_payload()
        payload["source"] = "guest"
        cases.append(("wrong source", payload))
        payload = self.valid_payload()
        payload["gpu"] = None
        cases.append(("non-list GPU", payload))
        payload = self.valid_payload()
        del payload["availability"]["gpus"]
        cases.append(("missing availability", payload))
        payload = self.valid_payload()
        payload["availability"]["extra"] = "available"
        cases.append(("extra availability", payload))
        payload = self.valid_payload()
        payload["availability"]["cpu"] = "unknown"
        cases.append(("unknown status", payload))
        for label, invalid in cases:
            with self.subTest(case=label):
                self.assert_rejected(invalid)

        self.assert_rejected(self.valid_payload(), post_status=200, post_expected=True)

    def test_get_cache_and_content_length_headers_are_enforced(self):
        body = json.dumps(self.valid_payload(), separators=(",", ":")).encode()
        invalid_headers = (
            ("missing Cache-Control", {"Content-Length": str(len(body))}),
            ("wrong Cache-Control", {"Cache-Control": "private", "Content-Length": str(len(body))}),
            ("missing Content-Length", {"Cache-Control": "no-store"}),
            ("malformed Content-Length", {"Cache-Control": "no-store", "Content-Length": "not-a-length"}),
            ("mismatched Content-Length", {"Cache-Control": "no-store", "Content-Length": str(len(body) + 1)}),
        )
        for label, headers in invalid_headers:
            with self.subTest(case=label):
                self.assert_rejected(self.valid_payload(), headers=headers)


if __name__ == "__main__":
    unittest.main()
