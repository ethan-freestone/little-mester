#!/usr/bin/env python3
"""Offline parent-up orchestration tests; all host executables are faked."""

import contextlib
import importlib.util
import pathlib
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("parent_launcher", ROOT / "lib/vm/parent-launcher.py")
parent_launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parent_launcher)
STATE_SPEC = importlib.util.spec_from_file_location("parent_state", ROOT / "lib/vm/parent-state.py")
parent_state = importlib.util.module_from_spec(STATE_SPEC)
STATE_SPEC.loader.exec_module(parent_state)


class ParentLauncherTests(unittest.TestCase):
    def test_disabled_launch_gate_refuses_before_loading_or_tool_discovery(self):
        with (mock.patch.object(parent_launcher, "PARENT_LAUNCH_ENABLED", False),
              mock.patch.object(parent_launcher, "_load", side_effect=AssertionError("must not load")),
              mock.patch.object(parent_launcher, "_free_loopback_port",
                                side_effect=AssertionError("must not allocate"))):
            with self.assertRaisesRegex(parent_launcher.Refused, "disabled"):
                parent_launcher.up()

    def test_running_up_is_idempotent_without_provisioning_or_second_supervisor(self):
        status = {"status": "RUNNING", "parent_id": "default", "run_id": "a" * 32,
                  "disk": {"virtual_capacity_gib": 64}}
        with (mock.patch.object(parent_launcher, "_load", return_value=SimpleNamespace(
                    read_status=lambda **_kwargs: status)),
              mock.patch.object(parent_launcher, "_free_loopback_port",
                                side_effect=AssertionError("must not allocate another port")),
              mock.patch.object(parent_launcher, "_spawn_supervisor",
                                side_effect=AssertionError("must not spawn another supervisor"))):
            self.assertIs(parent_launcher.up(64), status)

    def test_concurrent_duplicate_up_refuses_on_lifecycle_lock_before_second_writer(self):
        class State:
            PARENT_ID = "default"
            Refused = parent_state.Refused

            @staticmethod
            def read_status(*_args, **_kwargs):
                return {"status": "STOPPED", "disk": {"virtual_capacity_gib": 64}}

            @staticmethod
            @contextlib.contextmanager
            def lifecycle_lock(*_args, **_kwargs):
                raise parent_state.Refused("parent lifecycle lock is held by first up")
                yield

        with (mock.patch.object(parent_launcher, "_load", return_value=State),
              mock.patch.object(parent_launcher, "_free_loopback_port", return_value=23456),
              mock.patch.object(parent_launcher, "_spawn_supervisor",
                                side_effect=AssertionError("second writer must not start"))):
            with self.assertRaisesRegex(parent_launcher.Refused, "lifecycle lock is held"):
                parent_launcher.up(64)

    def test_owner_up_pipeline_uses_state_lock_provision_seed_then_supervisor(self):
        events = []

        class State:
            PARENT_ID = "default"
            Refused = parent_state.Refused

            @staticmethod
            @contextlib.contextmanager
            def lifecycle_lock(*, home=None):
                events.append(("lock", home))
                yield 42

            @staticmethod
            def read_status(*, home=None):
                if sum(event[0] == "read" for event in events) < 2:
                    events.append(("read", "NOT_CREATED"))
                    return {"status": "NOT_CREATED"}
                events.append(("read", "RUNNING"))
                return {"status": "RUNNING", "parent_id": "default",
                        "run_id": "0123456789abcdef0123456789abcdef",
                        "disk": {"virtual_capacity_gib": 64}}

            @staticmethod
            def initialize(capacity, *, home, lock_fd):
                events.append(("initialize", capacity, lock_fd))
                return {"disk": {"provisioned": False}}

        class Provision:
            Refused = RuntimeError

            @staticmethod
            def create_initial_disk(**kwargs):
                events.append(("provision", kwargs["capacity_gib"], kwargs["lock_fd"]))
                return {"disk": {"provisioned": True}}

        class Supervisor:
            @staticmethod
            def _protected_tool(name, candidates):
                events.append(("tool", name, candidates))
                return f"/mock/{name}"

            @staticmethod
            def _prepare_parent_seed(state, lock_fd, home):
                events.append(("seed", lock_fd, home))

        fake_proc = SimpleNamespace(poll=lambda: None)
        with (mock.patch.object(parent_launcher, "_load", side_effect=[State, Provision, Supervisor]),
              mock.patch.object(parent_launcher, "_free_loopback_port", return_value=23456),
              mock.patch.object(parent_launcher.secrets, "token_hex",
                                return_value="0123456789abcdef0123456789abcdef"),
              mock.patch.object(parent_launcher, "_spawn_supervisor", return_value=fake_proc) as spawn):
            status = parent_launcher.up(64, home="/tmp/test-home", startup_timeout_seconds=1)

        self.assertEqual(status["status"], "RUNNING")
        self.assertEqual([event[0] for event in events],
                         ["read", "lock", "read", "initialize", "tool", "tool", "provision", "seed", "read"])
        self.assertEqual(spawn.call_args.args[1], 42)
        self.assertEqual(spawn.call_args.args[3], 23456)

    def test_launcher_deadline_grace_does_not_preempt_supervisor_readiness_completion(self):
        clock = [0.0]
        events = []

        class State:
            PARENT_ID = "default"
            Refused = parent_state.Refused
            stop_requests = 0
            reads = 0

            @staticmethod
            @contextlib.contextmanager
            def lifecycle_lock(*, home=None):
                yield 42

            @classmethod
            def read_status(cls, *, home=None):
                cls.reads += 1
                if cls.reads <= 2:
                    return {"status": "NOT_CREATED"}
                if clock[0] >= 1.1:
                    events.append(("readiness-completed", clock[0]))
                    return {"status": "RUNNING", "parent_id": "default",
                            "run_id": "0123456789abcdef0123456789abcdef",
                            "disk": {"virtual_capacity_gib": 64}}
                return {"status": "STARTING", "run_id": "0123456789abcdef0123456789abcdef"}

            @classmethod
            def request_supervisor_stop(cls, **_kwargs):
                cls.stop_requests += 1
                raise AssertionError("outer timeout must not preempt readiness")

            @staticmethod
            def initialize(*_args, **_kwargs):
                return {"disk": {"provisioned": False}}

        class Provision:
            Refused = RuntimeError

            @staticmethod
            def create_initial_disk(**_kwargs):
                return {"disk": {"provisioned": True}}

        class Supervisor:
            @staticmethod
            def _protected_tool(name, _candidates):
                return f"/mock/{name}"

            @staticmethod
            def _prepare_parent_seed(*_args):
                return None

        fake_proc = SimpleNamespace(poll=lambda: None)

        def sleep(seconds):
            clock[0] += seconds

        with (mock.patch.object(parent_launcher, "_load", side_effect=[State, Provision, Supervisor]),
              mock.patch.object(parent_launcher, "_free_loopback_port", return_value=23456),
              mock.patch.object(parent_launcher.secrets, "token_hex",
                                return_value="0123456789abcdef0123456789abcdef"),
              mock.patch.object(parent_launcher, "_spawn_supervisor", return_value=fake_proc),
              mock.patch.object(parent_launcher.time, "monotonic", side_effect=lambda: clock[0]),
              mock.patch.object(parent_launcher.time, "sleep", side_effect=sleep)):
            status = parent_launcher.up(64, home="/tmp/test-home", startup_timeout_seconds=1)

        self.assertEqual(status["status"], "RUNNING")
        self.assertEqual(State.stop_requests, 0)
        self.assertEqual(events, [("readiness-completed", 1.2)])

    def test_launcher_refreshes_status_after_reaping_failed_supervisor(self):
        statuses = iter((
            {"status": "NOT_CREATED"},
            {"status": "NOT_CREATED"},
            {"status": "AMBIGUOUS"},  # stale zombie projection before poll()
            {"status": "STOPPED"},    # durable status after poll() reaps supervisor
            {"status": "STOPPED"},    # failure handler's fresh eligibility check
        ))

        class State:
            PARENT_ID = "default"
            Refused = parent_state.Refused

            @staticmethod
            @contextlib.contextmanager
            def lifecycle_lock(*, home=None):
                yield 42

            @staticmethod
            def read_status(*, home=None):
                return next(statuses)

            @staticmethod
            def initialize(*_args, **_kwargs):
                return {"disk": {"provisioned": False}}

        class Provision:
            Refused = RuntimeError

            @staticmethod
            def create_initial_disk(**_kwargs):
                return {"disk": {"provisioned": True}}

        class Supervisor:
            @staticmethod
            def _protected_tool(name, _candidates):
                return f"/mock/{name}"

            @staticmethod
            def _prepare_parent_seed(*_args):
                return None

        fake_proc = SimpleNamespace(poll=lambda: 17)
        with (mock.patch.object(parent_launcher, "_load", side_effect=[State, Provision, Supervisor]),
              mock.patch.object(parent_launcher, "_free_loopback_port", return_value=23456),
              mock.patch.object(parent_launcher.secrets, "token_hex",
                                return_value="0123456789abcdef0123456789abcdef"),
              mock.patch.object(parent_launcher, "_spawn_supervisor", return_value=fake_proc)):
            with self.assertRaisesRegex(parent_launcher.Refused,
                                        "parent supervisor/startup failed; status is STOPPED"):
                parent_launcher.up(64, startup_timeout_seconds=1)

    def test_supervisor_spawn_commits_handoff_under_the_existing_lifecycle_lock(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp) / "home"
            home.mkdir(mode=0o700)
            os.chmod(home, 0o700)
            parent_state.initialize(home=home)
            captured = {}

            class FakeProcess:
                def poll(self):
                    return None

                def terminate(self):
                    pass

            def fake_popen(argv, **kwargs):
                captured["argv"] = argv
                captured["handoff"] = os.dup(argv_fd := kwargs["pass_fds"][1])
                captured["lock_fd"] = kwargs["pass_fds"][0]
                return FakeProcess()

            with parent_state.lifecycle_lock(home=home) as lock_fd:
                with mock.patch.object(parent_launcher.subprocess, "Popen", side_effect=fake_popen):
                    parent_launcher._spawn_supervisor(parent_state, lock_fd,
                        "0123456789abcdef0123456789abcdef", 23456, home=str(home))
                self.assertEqual(os.read(captured["handoff"], 7), b"COMMIT\n")
            os.close(captured["handoff"])
            self.assertIn("--ssh-port", captured["argv"])
            self.assertIn("23456", captured["argv"])


if __name__ == "__main__":
    unittest.main()
