"""Functional response assertions must fail for wrong content or missing keys."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verifier.probes import HttpProber, ProbeDispatcher
from verifier.oracle import ApplicationFunctionalRule, RuleStatus
import test_onboarding as onboarding_tests


class FunctionalProbeTests(unittest.TestCase):
    def probe(self, body, spec, status=200):
        with patch('verifier.probes.run_command', return_value=subprocess.CompletedProcess([], 0, json.dumps({'status': status, 'body': body}), '')):
            return HttpProber.execute_functional_test('pod', 'chaoslab-demo', spec)

    def test_expected_text_and_http_status(self):
        spec = {'expected_body_pattern': 'Welcome to nginx!'}
        self.assertTrue(self.probe('Welcome to nginx!', spec)['passed'])
        self.assertFalse(self.probe('welcome to nginx!', spec)['passed'])
        result = self.probe('maintenance', spec)
        self.assertFalse(result['passed'])
        self.assertIn('response text', result['error'])
        self.assertFalse(self.probe('Welcome to nginx!', spec, 503)['passed'])

    def test_requested_key_cannot_fall_back_to_an_unrelated_id(self):
        spec = {'expected_json_key': 'order_id'}
        for body in ('{"id":"unrelated"}', '{"order_id":null}', 'not json'):
            self.assertFalse(self.probe(body, spec)['passed'])
        result = self.probe('{"order_id":0}', spec)
        self.assertTrue(result['passed'])
        self.assertEqual(result['transaction_id'], '0')
        self.assertTrue(self.probe('[{"order_id":"abc"}]', spec)['passed'])

    def test_both_assertions_must_pass(self):
        spec = {'expected_json_key': 'order_id', 'expected_body_pattern': 'confirmed'}
        self.assertFalse(self.probe('{"order_id":1,"state":"pending"}', spec)['passed'])
        self.assertTrue(self.probe('{"order_id":1,"state":"confirmed"}', spec)['passed'])

    def test_curl_supports_minimal_containers_with_bounded_timeouts(self):
        with patch('verifier.probes.run_command') as run:
            run.side_effect = [subprocess.CompletedProcess([], 127, '', 'python not found'),
                               subprocess.CompletedProcess([], 0, 'Welcome to nginx!\n{"status": 200}', '')]
            result = HttpProber.execute_functional_test('pod', 'chaoslab-demo', {'expected_body_pattern': 'Welcome to nginx!', 'timeout': 3})
            self.assertTrue(result['passed'])
            command = run.call_args.args[0]
            self.assertEqual(command[command.index('--max-time')+1], '3')

    def test_python_transaction_script_remains_valid_with_quoted_path(self):
        response = Mock()
        response.status = 201
        response.read.return_value = b'{"order_id":"confirmed"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        def run(command, **kwargs):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch('urllib.request.urlopen', return_value=response):
                try:
                    exec(compile(command[-1], '<probe>', 'exec'), {})
                except SystemExit as exit_signal:
                    self.assertEqual(exit_signal.code, 0)
            return subprocess.CompletedProcess(command, 0, output.getvalue(), '')
        with patch('verifier.probes.run_command', side_effect=run):
            result = HttpProber.execute_functional_test('pod', 'chaoslab-demo', {
                'path': "/orders?label=it's", 'method': 'POST', 'body': {'item': 'test'},
                'expected_status': 201, 'expected_json_key': 'order_id'})
        self.assertTrue(result['passed'])

    def test_failed_response_fails_functional_oracle(self):
        config = {'application': {'enabled': True, 'functional_check': {'type': 'http', 'expected_body_pattern': 'expected'}}}
        with patch('verifier.probes.K8sProber.probe_readiness', return_value={'passed': True}), \
             patch('verifier.probes.run_command', return_value=subprocess.CompletedProcess([], 0, '{"status":200,"body":"wrong"}', '')):
            evidence = ProbeDispatcher.probe_application('pod', 'chaoslab-demo', config)
        self.assertEqual(ApplicationFunctionalRule().evaluate({'application': evidence}, config).status, RuleStatus.FAIL)


class FunctionalDraftTests(unittest.TestCase):
    setUp = onboarding_tests.OnboardingTests.setUp
    request = onboarding_tests.OnboardingIntegrationTests.request

    def test_api_enables_functional_check_with_health_probe(self):
        import yaml
        app = self.registry.register(self.payload)
        status, response = self.request('POST', f'/api/onboarding/apps/{app["id"]}/experiment', {
            'health_probe': {'path': '/', 'port': 8080},
            'functional_check': {'path': '/', 'port': 8080, 'expected_body_pattern': 'Welcome to nginx!'}})
        self.assertEqual(status, 201)
        data = yaml.safe_load((self.registry.experiments_dir / (response['experiment']['name']+'.yaml')).read_text())
        self.assertTrue(data['application']['enabled'])
        self.assertEqual(data['application']['health_probe']['type'], 'http')
        self.assertEqual(data['application']['functional_check']['method'], 'GET')

    def test_invalid_settings_preserve_draft(self):
        app = self.registry.register(self.payload)
        draft = self.registry.generate_experiment(app['id'])
        path = self.registry.experiments_dir / (draft['name']+'.yaml')
        original = path.read_bytes()
        for spec in ({}, {'expected_body_pattern': ''}, {'expected_json_key': None},
                     {'expected_body_pattern':'ok','method':'POST'}, {'expected_body_pattern':'ok','body':{}},
                     {'expected_json_key':'id','path':'http://remote/'}, {'expected_json_key':'id','timeout':True}):
            with self.subTest(spec=spec):
                status, _ = self.request('POST', f'/api/onboarding/apps/{app["id"]}/experiment', {'functional_check':spec})
                self.assertEqual(status, 400)
                self.assertEqual(path.read_bytes(), original)
