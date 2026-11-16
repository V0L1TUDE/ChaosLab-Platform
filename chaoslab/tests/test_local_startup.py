"""Local startup uses private snapshots and never switches the user's context."""
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import local as launcher
from local_runtime import validate_local_config


def config(server='https://127.0.0.1:1234',context='docker-desktop'):
    return {'current-context':context,'contexts':[{'name':context,'context':{'cluster':'local'}}],
            'clusters':[{'name':'local','cluster':{'server':server}}],'users':[{'name':'operator','user':{'token':'test-only-token'}}]}


class LocalRuntimeTests(unittest.TestCase):
    def test_remote_contexts_and_endpoints_are_rejected(self):
        self.assertEqual(validate_local_config(config()),'https://127.0.0.1:1234')
        for value in (config(server='https://cloud.example'),config(server='http://127.0.0.1:1234'),config(context='production')):
            with self.assertRaises(ValueError):validate_local_config(value)

    def test_snapshot_is_private_removed_and_original_environment_restored(self):
        with patch.dict(os.environ,{'KUBECONFIG':'original-config','PATH':'original-path'}), \
             patch.object(launcher,'kubectl',return_value=json.dumps(config())) as command, \
             patch.object(launcher,'kubectl_binary',return_value='/test/bin/kubectl'):
            with launcher.pinned_cluster():
                snapshot=Path(os.environ['KUBECONFIG'])
                self.assertNotEqual(str(snapshot),'original-config')
                self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode),0o600)
                self.assertEqual(os.environ['CHAOSLAB_PINNED_CONTEXT'],'docker-desktop')
                self.assertEqual(json.loads(snapshot.read_text())['current-context'],'docker-desktop')
            self.assertFalse(snapshot.exists())
            self.assertEqual(os.environ['KUBECONFIG'],'original-config')
            self.assertEqual(os.environ['PATH'],'original-path')
            self.assertEqual(command.call_args.args[:3],('config','view','--context'))
            self.assertNotIn('use-context',command.call_args.args)

    def test_snapshot_cleanup_on_startup_exception(self):
        with patch.object(launcher,'kubectl',return_value=json.dumps(config())), \
             patch.object(launcher,'kubectl_binary',return_value='/test/bin/kubectl'):
            with self.assertRaisesRegex(RuntimeError,'startup failed'):
                with launcher.pinned_cluster():
                    snapshot=Path(os.environ['KUBECONFIG'])
                    raise RuntimeError('startup failed')
            self.assertFalse(snapshot.exists())

    def test_launcher_help_works_from_another_directory(self):
        with TemporaryDirectory() as folder:
            result=subprocess.run([sys.executable,str(Path(launcher.__file__)),'--help'],cwd=folder,
                                  env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('setup-demo',result.stdout)


class StartupCommandTests(unittest.TestCase):
    def setUp(self):
        self.stack=__import__('contextlib').ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('sys.stdout',new_callable=io.StringIO))
        self.stack.enter_context(patch('sys.stderr',new_callable=io.StringIO))
        @contextmanager
        def pinned(*args):yield {'context':'docker-desktop','server':'https://127.0.0.1:1234'}
        self.stack.enter_context(patch.object(launcher,'pinned_cluster',side_effect=pinned))
        self.stack.enter_context(patch.dict(os.environ,{'CHAOSLAB_ENABLE_FAULT_RUNS':'1'}))
        self.doctor=self.stack.enter_context(patch.object(launcher,'doctor',return_value=[{'name':'test','passed':True,'detail':'ok'}]))
        from dashboard import server
        self.server=self.stack.enter_context(patch.object(server,'run_server'))

    def test_preview_start_overrides_inherited_execution_flag(self):
        def start(**kwargs):self.assertEqual(os.environ['CHAOSLAB_ENABLE_FAULT_RUNS'],'0')
        self.server.side_effect=start
        self.assertEqual(launcher.main(['start']),0)
        self.server.assert_called_once_with(host='127.0.0.1',port=8081)

    def test_enabled_start_requires_healthy_environment(self):
        self.doctor.return_value=[{'name':'node','passed':False,'detail':'offline'}]
        self.assertEqual(launcher.main(['start','--enable-fault-runs']),1)
        self.server.assert_not_called()

    def test_doctor_failure_exits_without_starting_or_deploying(self):
        self.doctor.return_value=[{'name':'node','passed':False,'detail':'offline'}]
        with patch.object(launcher,'setup_demo') as setup:
            self.assertEqual(launcher.main(['doctor']),1)
            setup.assert_not_called()
        self.server.assert_not_called()

    def test_explicit_execution_start(self):
        def start(**kwargs):self.assertEqual(os.environ['CHAOSLAB_ENABLE_FAULT_RUNS'],'1')
        self.server.side_effect=start
        self.assertEqual(launcher.main(['start','--enable-fault-runs','--port','8082']),0)
        self.server.assert_called_once_with(host='127.0.0.1',port=8082)

    def test_setup_is_explicit_and_preserves_existing_registration(self):
        from onboarding import registry
        existing={'namespace':'chaoslab-demo','app_label':'chaoslab-sample'}
        register=Mock()
        helper={'prepare_manifests':Mock(return_value=[]),'register_experiment':register}
        with patch('runpy.run_path',return_value=helper),patch.object(launcher,'kubectl') as command, \
             patch.object(registry,'ApplicationRegistry') as registry_class:
            registry_class.return_value.list_apps.return_value=[existing]
            launcher.setup_demo()
            registry_class.return_value.register.assert_not_called()
            registry_class.return_value.generate_experiment.assert_not_called()
            register.assert_called_once()
            self.assertTrue(all(call.args[0] in ('apply','annotate','-n') for call in command.call_args_list))
