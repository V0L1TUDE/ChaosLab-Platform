"""Report archives preserve repeated runs and remain accessible through the API."""
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard import server
from verifier.reporter import ExperimentReporter


class ReportHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.report = {'experiment': 'sample', 'verdict': 'PASS', 'target': 'demo',
                       'recovery_time_seconds': 0, 'timestamps': {'experiment_start': '2026-11-16T10:00:00Z'},
                       'oracle_evaluation': {'passed_rules': 6, 'failed_rules': 0, 'skipped_rules': 5}}
        self.patch = patch.object(server, 'REPORTS_DIR', str(self.root))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def save(self, **changes):
        # Rendering is separately exercised by the real reports imported for live QA.
        with patch.object(ExperimentReporter, 'generate_html', return_value='<html>report</html>'), \
             patch.object(ExperimentReporter, 'generate_markdown', return_value='# report'):
            return ExperimentReporter.save_all('sample', {**self.report, **changes}, str(self.root))

    def request(self, path):
        handler = server.DashboardRequestHandler.__new__(server.DashboardRequestHandler)
        handler.path = path
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.do_GET()
        return handler.send_response.call_args.args[0], handler.wfile.getvalue()

    def test_repeated_runs_preserve_history_and_latest_compatibility(self):
        first = self.save()
        second = self.save(verdict='FAIL', timestamps={'experiment_start': '2026-11-16T11:00:00+00:00'})
        self.assertNotEqual(first['json'], second['json'])
        self.assertEqual(json.loads(Path(first['json']).read_text())['verdict'], 'PASS')
        self.assertEqual(json.loads((self.root / 'sample_report.json').read_text())['verdict'], 'FAIL')
        rows = server.get_historical_reports()
        self.assertEqual([r['verdict'] for r in rows], ['FAIL', 'PASS'])
        self.assertEqual(rows[0]['passed_checks'], 6)
        self.assertEqual(rows[0]['skipped_checks'], 5)
        self.assertEqual(rows[0]['recovery_time'], 0)
        self.assertEqual(rows[0]['timestamp'], '2026-11-16T11:00:00+00:00')
        status, body = self.request('/api/reports')
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)['reports']), 2)
        for row in rows:
            for extension in ('json', 'html', 'md'):
                status, body = self.request('/api/reports/' + row['prefix'] + '_report.' + extension)
                self.assertEqual(status, 200)
                self.assertTrue(body)

    def test_legacy_report_and_corrupt_file(self):
        (self.root / 'legacy_report.json').write_text(json.dumps(self.report))
        (self.root / 'corrupt_report.json').write_text('not json')
        rows = server.get_historical_reports()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['run_id'])
        self.assertFalse(rows[0]['has_html'])

    def test_report_paths_reject_traversal_and_symlinks(self):
        outside = self.root.parent / (self.root.name + '_outside_report.json')
        outside.write_text('private')
        self.addCleanup(outside.unlink)
        (self.root / 'linked_report.json').symlink_to(outside)
        for path in ('../' + outside.name, '%2e%2e%2f' + outside.name, 'linked_report.json', 'applications.sqlite3'):
            with self.subTest(path=path):
                status, _ = self.request('/api/reports/' + path)
                self.assertEqual(status, 404)
        self.assertEqual(server.get_historical_reports(), [])

    def test_report_names_cannot_escape_storage(self):
        with patch.object(ExperimentReporter, 'generate_html', return_value='html'), \
             patch.object(ExperimentReporter, 'generate_markdown', return_value='md'):
            paths = ExperimentReporter.save_all('../escaped', self.report, str(self.root))
        self.assertTrue(all(Path(p).resolve().is_relative_to(self.root.resolve()) for p in paths.values()))
