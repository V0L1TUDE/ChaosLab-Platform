"""Execution cleanup runs on failure and never turns unconfirmed deletion into PASS."""
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'verifier'))
import engine


class ExecutionCleanupTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.config = {'name':'cleanup-test','target':{'namespace':'chaoslab-demo','label':{'app':'demo'}},
                       'fault':{'type':'pod-kill','mode':'one'},'recovery':{'timeout_seconds':60,'expected_ready_replicas':1}}
        old = {'name':'old','ready':'1/1','status':'Running'}
        new = {'name':'new','ready':'1/1','status':'Running'}
        self.pods = self.stack.enter_context(patch.object(engine,'get_pods',side_effect=[[old],[new],[new]]))
        self.ready = self.stack.enter_context(patch.object(engine,'get_ready_pods',side_effect=[[old],[new],[new]]))
        self.apply = self.stack.enter_context(patch.object(engine,'apply_chaos',return_value=(True,'owned-fault','created','PodChaos')))
        self.cleanup = self.stack.enter_context(patch.object(engine,'cleanup_chaos',return_value=True))
        self.stack.enter_context(patch.object(engine,'get_chaos_mesh_evidence',return_value={'injected':True}))
        self.stack.enter_context(patch.object(engine,'get_kubernetes_events',return_value=[]))
        self.probe = self.stack.enter_context(patch.object(engine,'probe_application',return_value={'enabled':False}))
        self.stack.enter_context(patch.object(engine.time,'sleep'))
        self.save = self.stack.enter_context(patch.object(engine.ExperimentReporter,'save_all',return_value={'html':'h','markdown':'m','json':'j'}))
        self.events=[]

    def run_experiment(self):
        return engine.run_experiment(self.config,lambda phase,*args:self.events.append(phase))

    def test_probe_error_and_keyboard_interrupt_still_cleanup(self):
        # Each interruption receives an independent setup through unittest subtests.
        for failure in (RuntimeError('probe failed'), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                self.pods.side_effect=[[{'name':'old','ready':'1/1','status':'Running'}],
                                       [{'name':'new','ready':'1/1','status':'Running'}],
                                       [{'name':'new','ready':'1/1','status':'Running'}]]
                self.ready.side_effect=[[{'name':'old'}],[{'name':'new'}],[{'name':'new'}]]
                self.probe.side_effect=failure
                self.cleanup.reset_mock()
                with self.assertRaises(type(failure)):
                    self.run_experiment()
                self.cleanup.assert_called_once_with('owned-fault','chaoslab-demo','PodChaos')
                self.save.assert_not_called()
                self.assertNotIn('completed',self.events)

    def test_cleanup_failure_blocks_report_and_completion(self):
        self.cleanup.side_effect=RuntimeError('cleanup unconfirmed')
        with self.assertRaisesRegex(RuntimeError,'cleanup unconfirmed'):
            self.run_experiment()
        self.save.assert_not_called()
        self.assertNotIn('completed',self.events)

    def test_report_error_occurs_after_cleanup(self):
        def fail_report(*args):
            self.cleanup.assert_called_once()
            raise OSError('disk full')
        self.save.side_effect=fail_report
        with self.assertRaisesRegex(OSError,'disk full'):
            self.run_experiment()
        self.cleanup.assert_called_once()
        self.assertNotIn('completed',self.events)

    def test_success_report_contains_confirmed_cleanup(self):
        self.run_experiment()
        self.cleanup.assert_called_once()
        self.assertTrue(self.save.call_args.args[1]['cleanup']['confirmed'])
        self.assertIn('completed',self.events)

    def test_unhealthy_baseline_does_not_delete_any_resource(self):
        self.ready.side_effect=None
        self.ready.return_value=[]
        self.assertFalse(self.run_experiment())
        self.apply.assert_not_called()
        self.cleanup.assert_not_called()

    def test_failed_creation_does_not_delete_unowned_resource(self):
        self.apply.return_value=(False,'existing-fault','AlreadyExists','PodChaos')
        self.assertFalse(self.run_experiment())
        self.cleanup.assert_not_called()
        self.save.assert_not_called()

    def persistence_config(self):
        self.config['application']={'enabled':True,'persistence_check':{'enabled':True,
            'create':{'method':'POST','path':'/projects','port':8002},
            'read_path':'/projects/{id}','verify_fields':['id','name']}}

    def test_unverifiable_persistence_baseline_prevents_injection(self):
        self.persistence_config()
        with patch.object(engine.PersistenceProber,'prepare',return_value={'passed':False}):
            self.assertFalse(self.run_experiment())
        self.apply.assert_not_called()
        self.cleanup.assert_not_called()

    def test_lost_data_fails_even_when_pod_readiness_recovers(self):
        self.persistence_config()
        seed={'passed':True,'record_id':'before-fault','expected':{'id':'before-fault','name':'saved'}}
        with patch.object(engine.PersistenceProber,'prepare',return_value=seed), \
             patch.object(engine.PersistenceProber,'verify',return_value={'enabled':True,'passed':False,'record_id':'before-fault','error':'record lost'}):
            self.assertFalse(self.run_experiment())
        self.assertEqual(self.save.call_args.args[1]['verdict'],'FAIL')
        self.assertTrue(self.save.call_args.args[1]['replacement_ready'])
        self.cleanup.assert_called_once()


class CleanupCommandTests(unittest.TestCase):
    def test_cleanup_waits_for_deletion_with_bounded_timeout(self):
        with patch.object(engine,'run_command',return_value=subprocess.CompletedProcess([],0,'deleted','')) as run:
            self.assertTrue(engine.cleanup_chaos('owned','chaoslab-demo','PodChaos'))
        self.assertIn('--wait=true',run.call_args.args[0])
        self.assertIn('--timeout=20s',run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['timeout'],25)

    def test_cleanup_command_failure_is_visible(self):
        with patch.object(engine,'run_command',return_value=subprocess.CompletedProcess([],124,'','timeout')):
            with self.assertRaisesRegex(RuntimeError,'Fault cleanup unconfirmed.*owned.*chaoslab-demo'):
                engine.cleanup_chaos('owned','chaoslab-demo','PodChaos')

    def test_creation_uses_create_and_never_predeletes(self):
        config={'name':'test','target':{'namespace':'chaoslab-demo','label':{'app':'demo'}},'fault':{'type':'pod-kill'}}
        with patch.object(engine,'run_command',return_value=subprocess.CompletedProcess([],1,'','AlreadyExists')) as run:
            result=engine.apply_chaos(config)
        self.assertFalse(result[0])
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0],['kubectl','create','-f','-'])
