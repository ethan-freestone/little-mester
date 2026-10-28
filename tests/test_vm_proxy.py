import importlib.util
import ipaddress
import tempfile
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "vm_proxy", Path(__file__).parents[1] / "lib" / "vm" / "proxy.py"
)
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


class ProxyTests(unittest.TestCase):
    def test_policy_defaults_to_public_egress_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(proxy.policy(Path(directory) / ".mester-egress"), (True, set()))

    def test_policy_reads_deny_and_allowlist_without_shell_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / ".mester-egress"
            manifest.write_text("permission=false\nallowed_domains=example.com, pypi.org\n")
            self.assertEqual(proxy.policy(manifest), (False, {"example.com", "pypi.org"}))

    def test_allowlist_accepts_domain_and_real_subdomains_only(self):
        self.assertTrue(proxy.allowed("example.com", True, {"example.com"}))
        self.assertTrue(proxy.allowed("packages.example.com", True, {"example.com"}))
        self.assertFalse(proxy.allowed("evil-example.com", True, {"example.com"}))
        self.assertFalse(proxy.allowed("example.com", False, set()))

    def test_private_and_loopback_addresses_are_not_global(self):
        for value in ("127.0.0.1", "10.0.2.2", "169.254.1.1", "::1"):
            self.assertFalse(ipaddress.ip_address(value).is_global)

    def test_project_policy_permits_build_registries_without_lookalike_domains(self):
        enabled, domains = proxy.policy(Path(__file__).parents[1] / ".mester-egress")
        for host in ("auth.docker.io", "registry-1.docker.io", "registry.npmjs.org"):
            self.assertTrue(proxy.allowed(host, enabled, domains), host)
        self.assertFalse(proxy.allowed("registry.npmjs.org.attacker.invalid", enabled, domains))


if __name__ == "__main__":
    unittest.main()
