import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import local as launcher
from verifier.reporter import ExperimentReporter


class LocalMigrationTests(unittest.TestCase):
    ready={'items':[{'metadata':{'name':'old-project'},'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}}]}
    def test_legacy_records_have_a_private_backup(self):
        records=[{'id':'proj-104','name':'existing'}]
        with TemporaryDirectory() as temporary,patch.object(launcher,'CHAOSLAB',Path(temporary)), \
             patch.object(launcher,'kubectl',side_effect=[json.dumps(self.ready),json.dumps(records)]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.backup_memory_projects(),records)
            backups=list(Path(temporary).glob('onboarding/data/backups/*.json'))
            self.assertEqual(len(backups),1)
            self.assertEqual(json.loads(backups[0].read_text()),records)
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode),0o600)

    def test_existing_sqlite_store_needs_no_data_replacement(self):
        with patch.object(launcher,'kubectl',side_effect=[json.dumps(self.ready),'null']):
            self.assertIsNone(launcher.backup_memory_projects())

    def test_unready_legacy_service_stops_before_changes(self):
        pods={'items':[{'metadata':{'name':'old-project'},'status':{'phase':'Pending'}}]}
        with patch.object(launcher,'kubectl',return_value=json.dumps(pods)) as command:
            with self.assertRaisesRegex(RuntimeError,'refusing to update'):
                launcher.backup_memory_projects()
            command.assert_called_once()

    def test_restore_preserves_identifiers_in_the_import(self):
        records=[{'id':'original','name':'saved'}]
        with patch.object(launcher,'kubectl',return_value='restored') as command,contextlib.redirect_stdout(io.StringIO()):
            launcher.restore_projects(records)
        self.assertEqual(json.loads(command.call_args.kwargs['input_text']),records)
        self.assertIn('deployment/chaoslab-project',command.call_args.args)


class PersistentReportTests(unittest.TestCase):
    def test_html_and_markdown_show_record_and_trace_evidence(self):
        data={'experiment':'durability','verdict':'PASS','application':{'enabled':True,'persistence':{
            'enabled':True,'passed':True,'record_id':'same-id','before':{'id':'same-id','name':'<saved>'},
            'after':{'id':'same-id','name':'<saved>'}}},
              'telemetry':{'available':True,'traces_count':1,'spans_count':2,'requested_trace_id':'a'*32,
                           'services_observed':['project-service','user-service'],'trace_query_url':'http://127.0.0.1:16686'}}
        html=ExperimentReporter.generate_html(data)
        markdown=ExperimentReporter.generate_markdown(data)
        self.assertIn('Persistent Data Recovery',html)
        self.assertIn('&lt;saved&gt;',html)
        self.assertNotIn('"name": "<saved>"',html)
        self.assertIn('/trace/'+'a'*32,html)
        self.assertIn('Pre-fault record',markdown)
        self.assertIn('user-service',markdown)
