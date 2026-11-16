"""Offline HTTP health checks, draft API integration and failure verdicts."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verifier.probes import HttpProber, ProbeDispatcher
from verifier.oracle import ApplicationHealthRule, RuleStatus
import test_onboarding as onboarding_tests


class HttpHealthTests(unittest.TestCase):
    def test_python_probe_executes_and_quotes_the_url(self):
        response = Mock()
        response.status = 200
        response.read.return_value = b'healthy'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        def run(command, **kwargs):
            script = command[-1]
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch('urllib.request.urlopen', return_value=response):
                exec(compile(script, '<probe>', 'exec'), {})
            return subprocess.CompletedProcess(command, 0, output.getvalue(), '')
        with patch('verifier.probes.run_command', side_effect=run):
            result = HttpProber.probe_health('pod', 'chaoslab-demo', {'path': "/health?label=it's", 'port': 8080})
        self.assertTrue(result['passed'])
        self.assertEqual(result['body'], 'healthy')

    def test_curl_fallback_is_bounded_and_checks_status(self):
        for status, passed in ((200, True), (503, False)):
            with self.subTest(status=status), patch('verifier.probes.run_command') as run:
                run.side_effect = [subprocess.CompletedProcess([], 127, '', 'python not found'),
                                   subprocess.CompletedProcess([], 0, f'body\n{{"status": {status}}}', '')]
                result = HttpProber.probe_health('pod', 'chaoslab-demo', {'path': '/', 'timeout': 3})
                self.assertEqual(result['passed'], passed)
                command = run.call_args.args[0]
                self.assertEqual(command[command.index('--max-time')+1], '3')
                self.assertIn('--connect-timeout', command)

    def test_unreachable_probe_fails(self):
        with patch('verifier.probes.run_command', return_value=subprocess.CompletedProcess([], 1, '', 'unreachable')):
            self.assertFalse(HttpProber.probe_health('pod', 'chaoslab-demo', {})['passed'])

    def test_dispatcher_and_oracle_reject_failed_http_health(self):
        config = {'application': {'enabled': True, 'health_probe': {'type': 'http', 'path': '/', 'port': 8080}}}
        with patch('verifier.probes.HttpProber.probe_health', return_value={'passed': False, 'health_status': 'HTTP 503'}):
            evidence = ProbeDispatcher.probe_application('pod', 'chaoslab-demo', config)
        result = ApplicationHealthRule().evaluate({'application': evidence}, config)
        self.assertEqual(result.status, RuleStatus.FAIL)


class HealthDraftTests(unittest.TestCase):
    setUp = onboarding_tests.OnboardingTests.setUp
    request = onboarding_tests.OnboardingIntegrationTests.request

    def test_api_generates_http_config(self):
        import yaml
        app = self.registry.register(self.payload)
        status, response = self.request('POST', f'/api/onboarding/apps/{app["id"]}/experiment', {
            'health_probe': {'type': 'http', 'path': '/', 'port': 8080, 'expected_status': 200, 'timeout': 5}})
        self.assertEqual(status, 201)
        draft = yaml.safe_load((self.registry.experiments_dir / (response['experiment']['name'] + '.yaml')).read_text())
        self.assertTrue(draft['application']['enabled'])
        self.assertEqual(draft['application']['health_probe']['path'], '/')
        self.assertEqual(draft['target']['label'], {'app': 'test-shop'})

    def test_invalid_health_options_preserve_existing_draft(self):
        app = self.registry.register(self.payload)
        draft = self.registry.generate_experiment(app['id'])
        path = self.registry.experiments_dir / (draft['name'] + '.yaml')
        original = path.read_bytes()
        for spec in ('http', {'port': True}, {'port': 65536}, {'timeout': 11},
                     {'path': 'http://remote/health'}, {'path': '//remote'}, {'path': '/\nhealth'},
                     {'expected_status': 503}, {'type': 'exec'}, {'endpoint': 'http://remote'}):
            with self.subTest(spec=spec):
                status, _ = self.request('POST', f'/api/onboarding/apps/{app["id"]}/experiment', {'health_probe': spec})
                self.assertEqual(status, 400)
                self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
