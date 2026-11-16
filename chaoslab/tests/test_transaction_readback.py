"""A transaction passes only when the created entity can be read back correctly."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verifier.probes import HttpProber


class TransactionReadbackTests(unittest.TestCase):
    spec = {'method':'POST', 'path':'/projects', 'port':8002,
            'headers':{'Content-Type':'application/json'}, 'body':{'name':'test'},
            'expected_status':201, 'expected_json_key':'id',
            'readback':{'path':'/projects/{id}', 'expected_json':{'name':'test','owner_name':'Alice Chen'}}}

    def response(self, body, status):
        return subprocess.CompletedProcess([], 0, json.dumps({'status':status,'body':json.dumps(body)}), '')

    def test_create_then_read_same_entity_and_expected_fields(self):
        with patch('verifier.probes.run_command') as run:
            run.side_effect = [self.response({'id':'proj/104'},201), self.response({'id':'proj/104','name':'test','owner_name':'Alice Chen'},200)]
            result = HttpProber.execute_functional_test('pod','chaoslab-demo',self.spec)
            self.assertTrue(result['passed'])
            self.assertTrue(result['readback']['passed'])
            self.assertIn('/projects/proj%2F104',run.call_args.args[0][-1])
            self.assertEqual(run.call_count,2)

    def test_readback_must_match_id_and_all_expected_fields(self):
        for body in ({'id':'other','name':'test','owner_name':'Alice Chen'},
                     {'id':'proj-104','name':'wrong','owner_name':'Alice Chen'},
                     {'id':'proj-104','name':'test','owner_name':'Team Member'},
                     {'id':'proj-104','name':'test'}):
            with self.subTest(body=body), patch('verifier.probes.run_command') as run:
                run.side_effect=[self.response({'id':'proj-104'},201),self.response(body,200)]
                result=HttpProber.execute_functional_test('pod','chaoslab-demo',self.spec)
                self.assertFalse(result['passed'])
                self.assertIn('Read-back',result['error'])

    def test_readback_http_error_fails_transaction(self):
        with patch('verifier.probes.run_command') as run:
            run.side_effect=[self.response({'id':'proj-104'},201),self.response({'detail':'not found'},404)]
            self.assertFalse(HttpProber.execute_functional_test('pod','chaoslab-demo',self.spec)['passed'])

    def test_failed_creation_does_not_read_back_or_repeat_write(self):
        with patch('verifier.probes.run_command',return_value=self.response({'detail':'rejected'},400)) as run:
            self.assertFalse(HttpProber.execute_functional_test('pod','chaoslab-demo',self.spec)['passed'])
            run.assert_called_once()

    def test_missing_readback_placeholder_fails_without_followup(self):
        spec={**self.spec,'readback':{'path':'/projects/static'}}
        with patch('verifier.probes.run_command',return_value=self.response({'id':'proj-104'},201)) as run:
            self.assertFalse(HttpProber.execute_functional_test('pod','chaoslab-demo',spec)['passed'])
            run.assert_called_once()


class TransactionDemoTests(unittest.TestCase):
    def test_demo_targets_only_its_local_workloads(self):
        import runpy
        import yaml
        demo = Path(__file__).resolve().parents[1] / "onboarding" / "demo"
        prepare = runpy.run_path(str(demo / "prepare_project_demo.py"))["prepare_manifests"]
        manifests = prepare()
        self.assertEqual(len(manifests), 7)
        self.assertTrue(all(item["metadata"]["namespace"] == "chaoslab-demo" for item in manifests))
        for item in manifests:
            if item["kind"] == "Deployment":
                pod = item["spec"]["template"]["spec"]
                self.assertFalse(pod["automountServiceAccountToken"])
                self.assertTrue(pod["securityContext"]["runAsNonRoot"])
            if item["kind"] == "Service":
                self.assertEqual(item["spec"].get("type", "ClusterIP"), "ClusterIP")
        config = yaml.safe_load((demo / "project_transaction_experiment.yaml").read_text())
        self.assertEqual(config["target"], {"namespace":"chaoslab-demo", "label":{"app":"chaoslab-project"}})
        self.assertEqual(config["fault"], {"type":"pod-kill", "mode":"one"})
