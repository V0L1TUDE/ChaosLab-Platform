"""Read-only preflight and exact reviewed launch policy."""
import copy
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from email.message import Message
import yaml

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dashboard import preflight, server


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'sample.yaml'
        self.config={'name':'sample','target':{'namespace':'chaoslab-demo','label':{'app':'demo'}},
                     'fault':{'type':'pod-kill','mode':'one'},'recovery':{'expected_ready_replicas':1,'timeout_seconds':60}}
        self.write()
        self.experiments=[{'name':'sample','file_path':str(self.path)}]
        p=patch.object(preflight,'discover_experiments',return_value=self.experiments);p.start();self.addCleanup(p.stop)
        p=patch.dict(os.environ,{'CHAOSLAB_ENABLE_FAULT_RUNS':'1','CHAOSLAB_ALLOWED_NAMESPACES':'chaoslab-demo'});p.start();self.addCleanup(p.stop)
        p=patch.object(preflight,'inspect_local_context',return_value={'context':'docker-desktop','server':'https://127.0.0.1:1234'});self.context=p.start();self.addCleanup(p.stop)
        self.conflict=False;self.pods_ready=True;self.filtered=True;self.daemon_ready=True;self.opted_in=True;self.permission=True
        p=patch.object(preflight,'kubectl',side_effect=self.command);self.command_mock=p.start();self.addCleanup(p.stop)

    def write(self):
        self.path.write_text(yaml.safe_dump(self.config))

    def command(self,*args,**kwargs):
        if args[:2]==('get','namespace'):
            return json.dumps({'metadata':{'annotations':{'chaos-mesh.org/inject':'enabled' if self.opted_in else 'disabled'}}})
        if args[:2]==('get','deployment'):
            return json.dumps({'spec':{'template':{'spec':{'containers':[{'env':[{'name':'ENABLE_FILTER_NAMESPACE','value':'true' if self.filtered else 'false'}]}]}}},'status':{'availableReplicas':1}})
        if args[:2]==('get','daemonset'):
            return json.dumps({'status':{'desiredNumberScheduled':1,'numberReady':1 if self.daemon_ready else 0}})
        if args[:2]==('get','pods'):
            return json.dumps({'items':[{'metadata':{'name':'demo-pod'},'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True' if self.pods_ready else 'False'}]}}]})
        if args[0]=='get': return '{"metadata":{"name":"existing"}}' if self.conflict else ''
        if args[:2]==('auth','can-i'):return 'yes' if self.permission else 'no'
        if args[:2]==('create','--dry-run=server'):return 'validated'
        raise AssertionError(args)

    def test_ready_preflight_is_read_only_and_does_not_expose_configuration(self):
        result,config=preflight.check_experiment('sample')
        self.assertTrue(result['ready'])
        self.assertEqual(result['selector'],'app=demo')
        self.assertNotIn('config',result)
        self.assertEqual(config['name'],'sample')
        self.assertTrue(result['review_token'])
        self.assertTrue(all(call.args[0] in ('get','auth') or call.args[:2]==('create','--dry-run=server') for call in self.command_mock.call_args_list))

    def test_each_readiness_blocker_prevents_launch(self):
        for field in ('pods_ready','filtered','daemon_ready','opted_in','permission'):
            with self.subTest(field=field):
                setattr(self,field,False)
                result,_=preflight.check_experiment('sample')
                self.assertFalse(result['ready'])
                setattr(self,field,True)
        self.conflict=True
        self.assertFalse(preflight.check_experiment('sample')[0]['ready'])

    def test_preview_and_unpinned_cluster_are_blocked(self):
        with patch.dict(os.environ,{'CHAOSLAB_ENABLE_FAULT_RUNS':'0'}):
            self.assertFalse(preflight.check_experiment('sample')[0]['ready'])
        self.context.side_effect=RuntimeError('cluster not pinned')
        self.command_mock.reset_mock()
        self.assertFalse(preflight.check_experiment('sample')[0]['ready'])
        self.command_mock.assert_not_called()

    def test_unknown_partial_path_and_duplicate_names_are_rejected(self):
        for name in ('sam',str(self.path),'','other'):
            self.assertFalse(preflight.check_experiment(name)[0]['ready'])
        self.experiments.append(self.experiments[0])
        self.assertFalse(preflight.check_experiment('sample')[0]['ready'])
        self.command_mock.assert_not_called()

    def test_nonapproved_namespace_and_other_faults_are_blocked(self):
        for namespace,fault in (('default','pod-kill'),('chaoslab-demo','network-delay')):
            self.config['target']['namespace']=namespace;self.config['fault']['type']=fault;self.write()
            self.assertFalse(preflight.check_experiment('sample')[0]['ready'])
        self.command_mock.assert_not_called()

    def test_configuration_changes_invalidate_review_token(self):
        first,_=preflight.check_experiment('sample')
        self.config['application']={'enabled':True,'health_probe':{'type':'http','path':'/health','port':8080}}
        self.write()
        second,_=preflight.check_experiment('sample')
        self.assertNotEqual(first['review_token'],second['review_token'])

    def test_invalid_yaml_is_a_visible_preflight_failure(self):
        self.path.write_text("target: [invalid: yaml")
        result,_=preflight.check_experiment('sample')
        self.assertFalse(result['ready'])
        self.command_mock.assert_not_called()

    def test_trace_endpoint_must_match_launcher_managed_connection(self):
        self.config['telemetry']={'enabled':True,'query_endpoint':'http://other.example:16686'}
        self.write()
        with patch.dict(os.environ,{'CHAOSLAB_JAEGER_QUERY_URL':'http://127.0.0.1:16686'}), \
             patch.object(preflight,'TelemetryClient') as client:
            client.return_value.is_connected.return_value=True
            result,_=preflight.check_experiment('sample')
        self.assertFalse(result['ready'])
        self.assertTrue(any(row['name']=='Local trace backend' and not row['passed'] for row in result['checks']))


class LaunchPolicyTests(unittest.TestCase):
    def setUp(self):
        self.summary={'ready':True,'checks':[],'review_token':'current'}
        self.config={'name':'sample','target':{'namespace':'chaoslab-demo'}}
        p=patch.dict(os.environ,{'CHAOSLAB_ENABLE_FAULT_RUNS':'1'});p.start();self.addCleanup(p.stop)
        p=patch.object(server,'check_experiment',return_value=(self.summary,self.config));self.preflight=p.start();self.addCleanup(p.stop)
        p=patch.object(server,'_EXECUTION_MANAGER');self.manager=p.start();self.addCleanup(p.stop)
        self.manager.get_state.return_value={'running':False}
        self.manager.start.return_value=(True,'started')

    def request(self,payload,origin=None,host='127.0.0.1:8081',content_type='application/json'):
        handler=server.DashboardRequestHandler.__new__(server.DashboardRequestHandler)
        handler.path='/api/run';handler.headers=Message();handler.headers['Host']=host
        if origin:handler.headers['Origin']=origin
        body=json.dumps(payload).encode();handler.headers['Content-Type']=content_type;handler.headers['Content-Length']=str(len(body))
        handler.rfile=io.BytesIO(body);handler.wfile=io.BytesIO()
        handler.send_response=Mock();handler.send_header=Mock();handler.end_headers=Mock()
        handler.do_POST()
        return handler.send_response.call_args.args[0],json.loads(handler.wfile.getvalue())

    def test_only_fresh_reviewed_exact_config_can_start(self):
        status,_=self.request({'name':'sample','review_token':'current','confirm_target':True})
        self.assertEqual(status,200)
        self.manager.start.assert_called_once_with('sample',config=self.config)
        self.preflight.assert_called_once_with('sample')

    def test_stale_review_missing_review_and_changed_readiness_block(self):
        for payload in ({'name':'sample'}, {'name':'sample','review_token':'stale','confirm_target':True},
                        {'name':'sample','review_token':'current','confirm_target':'yes'}):
            self.assertEqual(self.request(payload)[0],409)
        self.summary['ready']=False
        self.assertEqual(self.request({'name':'sample','review_token':'current','confirm_target':True})[0],409)
        self.manager.start.assert_not_called()

    def test_no_default_experiment_or_malformed_body_launch(self):
        for payload in ({},[],None,{'name':123}):
            self.assertEqual(self.request(payload)[0],400)
        self.manager.start.assert_not_called()

    def test_preview_cross_origin_and_untrusted_host_block(self):
        with patch.dict(os.environ,{'CHAOSLAB_ENABLE_FAULT_RUNS':'0'}):
            self.assertEqual(self.request({'name':'sample'})[0],403)
        self.assertEqual(self.request({'name':'sample'},origin='https://other.example')[0],403)
        self.assertEqual(self.request({'name':'sample'},host='other.example')[0],403)
        self.manager.start.assert_not_called()

    def test_running_experiment_blocks_concurrent_launch(self):
        self.manager.get_state.return_value={'running':True}
        self.assertEqual(self.request({'name':'sample'})[0],409)
        self.preflight.assert_not_called()
        self.manager.start.assert_not_called()

    def test_execution_manager_copies_config_and_uses_nondaemon_worker(self):
        manager=server.ExecutionManager()
        config={'name':'snapshot','nested':{'path':'/original'}}
        with patch.object(server.threading,'Thread') as thread:
            manager.start('snapshot',config=config)
            config['nested']['path']='/edited'
            self.assertFalse(thread.call_args.kwargs['daemon'])
            self.assertEqual(thread.call_args.kwargs['args'][1]['nested']['path'],'/original')

    def test_shutdown_waits_for_active_worker(self):
        worker=Mock();worker.is_alive.return_value=True;self.manager.worker=worker
        with patch.object(server,'HTTPServer') as http,patch('sys.stdout',new_callable=io.StringIO):
            http.return_value.serve_forever.side_effect=KeyboardInterrupt
            server.run_server(port=1234)
        worker.join.assert_called_once()


class ExecutionFailureDisplayTests(unittest.TestCase):
    def test_failed_engine_callback_sets_visible_failed_state(self):
        manager=server.ExecutionManager()
        def fail(config,progress_callback):
            progress_callback('failed',100,'Baseline not ready')
            return False
        with patch.object(server,'run_experiment',side_effect=fail):
            manager._run_worker(None,config={'name':'sample'})
        state=manager.get_state()
        self.assertFalse(state['running'])
        self.assertEqual(state['verdict'],'FAIL')
        self.assertEqual(state['error'],'Baseline not ready')
