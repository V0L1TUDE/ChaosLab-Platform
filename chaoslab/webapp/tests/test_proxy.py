"""Running behind the deployment's reverse proxy: client IPs, forwarded scheme, self-signed TLS hints."""

import os
import sys
import tempfile
import unittest

from harness import CHAOSLAB_DIR, Client

sys.path.insert(0, os.path.join(CHAOSLAB_DIR, "agent"))
import agent as agent_mod  # noqa: E402


class ProxyTests(unittest.TestCase):
    def setUp(self):
        from webapp import config
        self.config = config
        self.saved = (config.TRUST_PROXY, config.PUBLIC_URL, config.TLS_MODE)
        self.c = Client()
        self.c.register()

    def tearDown(self):
        self.config.TRUST_PROXY, self.config.PUBLIC_URL, self.config.TLS_MODE = self.saved

    def create_cluster_ip(self, headers):
        status, data, _ = self.c.request("POST", "/api/clusters", {"name": "ip-test"}, headers=headers)
        self.assertEqual(status, 201, data)
        audit = self.c.get(f"/api/clusters/{data['cluster']['id']}")[1]["cluster"]["audit"]
        return [a for a in audit if a["action"] == "cluster_created"][0]["ip"], data

    def test_forwarded_ip_used_only_when_proxy_is_trusted(self):
        self.config.TRUST_PROXY = False
        ip, _ = self.create_cluster_ip({"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(ip, "127.0.0.1")                       # header ignored without a trusted proxy
        self.config.TRUST_PROXY = True
        ip, _ = self.create_cluster_ip({"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(ip, "203.0.113.9")

    def test_client_cannot_spoof_through_the_proxy(self):
        """Caddy appends the real client IP; anything to its left is attacker-controlled and must be ignored."""
        self.config.TRUST_PROXY = True
        ip, _ = self.create_cluster_ip({"X-Forwarded-For": "1.1.1.1, 198.51.100.7"})
        self.assertEqual(ip, "198.51.100.7")
        ip, _ = self.create_cluster_ip({"X-Forwarded-For": "not-an-ip; drop table"})
        self.assertEqual(ip, "127.0.0.1")

    def test_enrollment_uses_public_url_and_forwarded_scheme(self):
        self.config.PUBLIC_URL = "https://3-110-1-2.sslip.io"
        _, data = self.create_cluster_ip({})
        self.assertEqual(data["enrollment"]["server_url"], "https://3-110-1-2.sslip.io")
        self.assertNotIn("ca_url", data["enrollment"])
        self.config.PUBLIC_URL = ""
        self.config.TRUST_PROXY = False
        _, data = self.create_cluster_ip({"X-Forwarded-Proto": "https"})
        self.assertTrue(data["enrollment"]["server_url"].startswith("http://"))     # not trusted -> ignored
        self.config.TRUST_PROXY = True
        _, data = self.create_cluster_ip({"X-Forwarded-Proto": "https"})
        self.assertTrue(data["enrollment"]["server_url"].startswith("https://"))

    def test_self_signed_mode_points_agents_at_the_ca(self):
        self.config.PUBLIC_URL, self.config.TLS_MODE = "https://203-0-113-9.sslip.io", "internal"
        _, data = self.create_cluster_ip({})
        self.assertEqual(data["enrollment"]["ca_url"], "https://203-0-113-9.sslip.io/chaoslab-ca.crt")


class AgentTlsTests(unittest.TestCase):
    def test_ca_cert_must_exist_and_verification_stays_on(self):
        with self.assertRaises(ValueError):
            agent_mod.Api("https://chaoslab.example.com", ca_cert=os.path.join(tempfile.mkdtemp(), "missing.crt"))
        api = agent_mod.Api("https://chaoslab.example.com")
        self.assertIsNone(api.ssl_context)                    # default: the system's trusted CAs

    def test_ca_cert_context_keeps_hostname_checks(self):
        import ssl
        # any PEM bundle works for constructing the context; use Python's own default CA file when available
        cafile = ssl.get_default_verify_paths().cafile or ssl.get_default_verify_paths().openssl_cafile
        if not cafile or not os.path.isfile(cafile):
            self.skipTest("no CA bundle file on this system")
        api = agent_mod.Api("https://chaoslab.example.com", ca_cert=cafile)
        self.assertTrue(api.ssl_context.check_hostname)
        self.assertEqual(api.ssl_context.verify_mode, ssl.CERT_REQUIRED)


if __name__ == "__main__":
    unittest.main()
