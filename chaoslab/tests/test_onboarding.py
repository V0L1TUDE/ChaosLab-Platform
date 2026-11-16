"""Offline onboarding tests: no real Kubernetes or Chaos Mesh operations."""
import os
import io
import json
from email.message import Message
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

CHAOSLAB_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CHAOSLAB_DIR))
from onboarding.registry import ApplicationRegistry, ApplicationError


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_dir = Path(self.tmp.name)
        self.allowed = patch.dict(os.environ, {"CHAOSLAB_ALLOWED_NAMESPACES": "chaoslab-demo"})
        self.allowed.start()
        self.addCleanup(self.allowed.stop)
        self.registry = ApplicationRegistry(self.tmp_dir / "db.sqlite3", self.tmp_dir / "experiments")
        self.payload = {"name": "Test Shop", "namespace": "chaoslab-demo", "app_label": "test-shop", "expected_replicas": 2}

    def test_registration_is_saved(self):
        record = self.registry.register(self.payload)
        self.assertEqual(self.registry.get(record["id"])["name"], "Test Shop")
        self.assertEqual(len(self.registry.list_apps()), 1)

    def test_namespace_allowlist(self):
        for ns in ("default", "kube-system", "chaoslab-system", "secret-ns"):
            with self.subTest(ns=ns), self.assertRaises(ApplicationError):
                self.registry.register({**self.payload, "namespace": ns})

    def test_label_validation(self):
        for label in ("", "*", "test/shop", "UPPER"):
            with self.subTest(label=label), self.assertRaises(ApplicationError):
                self.registry.register({**self.payload, "app_label": label})

    def test_duplicate_target_rejected(self):
        self.registry.register(self.payload)
        with self.assertRaises(ApplicationError):
            self.registry.register(self.payload)

    def test_experiment_draft_valid_and_discoverable_by_name(self):
        app = self.registry.register(self.payload)
        draft = self.registry.generate_experiment(app["id"])
        self.assertEqual(draft["status"], "DRAFT_NOT_EXECUTED")
        self.assertTrue((self.tmp_dir / "experiments" / f"{draft['name']}.yaml").exists())
        import yaml
        with (self.tmp_dir / "experiments" / f"{draft['name']}.yaml").open() as stream:
            config = yaml.safe_load(stream)
        self.assertEqual(config["target"]["label"], {"app": "test-shop"})
        self.assertEqual(config["recovery"]["expected_ready_replicas"], 2)

    def test_pod_discovery_uses_read_only_kubectl(self):
        app = self.registry.register(self.payload)
        with patch("onboarding.registry.subprocess.run") as mocked:
            mocked.return_value.returncode = 0
            mocked.return_value.stdout = '{"items": [{"metadata": {"name": "test-shop-pod"}, "status": {"phase": "Running"}}]}'
            result = self.registry.discover_pods(app["id"])
            self.assertEqual(result["count"], 1)
            command = mocked.call_args.args[0]
            self.assertEqual(command[:3], ["kubectl", "get", "pods"])
            self.assertIn("app=test-shop", command)

    def test_readiness_requires_ready_condition_and_nonterminating_pods(self):
        app = self.registry.register(self.payload)
        def pod(phase="Running", ready="True", deleting=False):
            return {"metadata": {"name": "pod", **({"deletionTimestamp": "now"} if deleting else {})},
                    "status": {"phase": phase, "conditions": [{"type": "Ready", "status": ready}]}}
        cases = [([], 0, False), ([pod(ready="False")], 0, False),
                 ([pod(deleting=True), pod(phase="Pending")], 0, False),
                 ([pod()], 1, False), ([pod(), pod()], 2, True)]
        for pods, count, ready in cases:
            with self.subTest(pods=pods), patch("onboarding.registry.subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = json.dumps({"items": pods})
                result = self.registry.discover_pods(app["id"])
                self.assertEqual(result["ready_count"], count)
                self.assertEqual(result["workload_ready"], ready)
                self.assertEqual(result["expected_replicas"], 2)

    def test_discovery_handles_missing_tools_and_malformed_responses(self):
        app = self.registry.register(self.payload)
        with patch("onboarding.registry.subprocess.run", side_effect=FileNotFoundError):
            self.assertFalse(self.registry.discover_pods(app["id"])["connected"])
        for response in ('null', '{}', '{"items": null}', '{"items": [null]}', 'not json'):
            with self.subTest(response=response), patch("onboarding.registry.subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = response
                self.assertFalse(self.registry.discover_pods(app["id"])["connected"])

    def test_unknown_application_rejected(self):
        with self.assertRaises(ApplicationError):
            self.registry.generate_experiment("aaaaaaaaaaaa")


class OnboardingIntegrationTests(unittest.TestCase):
    """Exercise real registration, draft generation, discovery and API handlers offline."""

    setUp = OnboardingTests.setUp

    def request(self, method, path, payload=None, origin=None):
        from dashboard.server import DashboardRequestHandler
        handler = DashboardRequestHandler.__new__(DashboardRequestHandler)
        handler.path = path
        handler.headers = Message()
        handler.headers['Host'] = '127.0.0.1:8080'
        if origin:
            handler.headers['Origin'] = origin
        body = json.dumps(payload).encode() if payload is not None else b''
        handler.headers['Content-Type'] = 'application/json'
        handler.headers['Content-Length'] = str(len(body))
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        with patch('dashboard.server.ApplicationRegistry', return_value=self.registry):
            getattr(handler, 'do_' + method)()
        return handler.send_response.call_args.args[0], json.loads(handler.wfile.getvalue())

    def test_registration_to_dashboard_without_builtin_directory(self):
        import runner
        status, response = self.request('POST', '/api/onboarding/apps', self.payload)
        self.assertEqual(status, 201)
        app_id = response['application']['id']
        status, response = self.request('POST', f'/api/onboarding/apps/{app_id}/experiment', {})
        self.assertEqual(status, 201)
        name = response['experiment']['name']
        with patch.object(runner, 'EXPERIMENTS_DIR', str(self.tmp_dir / 'missing')), \
             patch.object(runner, 'ONBOARDING_EXPERIMENTS_DIR', str(self.registry.experiments_dir)):
            status, response = self.request('GET', '/api/experiments')
            self.assertEqual(status, 200)
            self.assertEqual([e['name'] for e in response['experiments']], [name])
            path = runner.find_experiment_path(name)
            from verifier.engine import load_experiment_config, generate_chaos_manifest
            _, kind, manifest = generate_chaos_manifest(load_experiment_config(path))
            self.assertEqual(kind, 'PodChaos')
            self.assertEqual(manifest['spec']['selector']['namespaces'], ['chaoslab-demo'])
            self.assertEqual(manifest['spec']['selector']['labelSelectors'], {'app': 'test-shop'})

    def test_cross_origin_registration_does_not_write(self):
        status, _ = self.request('POST', '/api/onboarding/apps', self.payload, 'https://example.com')
        self.assertEqual(status, 403)
        self.assertEqual(self.registry.list_apps(), [])

    def test_fault_runs_disabled_by_default(self):
        with patch.dict(os.environ, {'CHAOSLAB_ENABLE_FAULT_RUNS': '0'}), \
             patch('dashboard.server._EXECUTION_MANAGER.start') as start:
            status, _ = self.request('POST', '/api/run', {'experiment': 'test'})
        self.assertEqual(status, 403)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
