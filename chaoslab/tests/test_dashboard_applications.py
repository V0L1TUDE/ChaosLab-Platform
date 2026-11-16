"""Live dashboard reflects local registrations and discovered experiment targets."""
import json
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard import server
import runner


class DashboardApplicationTests(unittest.TestCase):
    def setUp(self):
        self.experiments = [{'namespace':'demo-tests','target':'project','app_group':'Project Demo'}]
        self.apps = [{'namespace':'new-team','app_label':'shop','name':'Registered Shop'}]
        p = patch.object(server, 'discover_experiments', return_value=self.experiments)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(server, 'ApplicationRegistry')
        registry = p.start(); self.addCleanup(p.stop)
        registry.return_value.list_apps.return_value = self.apps
        p = patch.object(server, '_CLUSTER_CACHE', {})
        p.start(); self.addCleanup(p.stop)

    def pod(self, name, namespace, ready=True, deleting=False):
        return {'metadata':{'name':name+'-pod','namespace':namespace,'labels':{'app':name},
                            **({'deletionTimestamp':'now'} if deleting else {})},
                'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True' if ready else 'False'}],
                          'containerStatuses':[{'ready':True,'restartCount':1},{'ready':True,'restartCount':2}]}}

    def test_catalog_includes_registrations_without_drafts_and_configured_targets(self):
        namespaces, groups = server.get_application_catalog()
        self.assertIn('new-team', namespaces)
        self.assertIn('demo-tests', namespaces)
        self.assertIn('default', namespaces)
        self.assertEqual(groups[('new-team','shop')],'Registered Shop')
        self.assertIn('new-team',[n['id'] for n in server.get_available_namespaces()])

    def test_live_status_includes_dynamic_apps_and_preserves_readiness(self):
        items=[self.pod('shop','new-team'), self.pod('project','demo-tests',ready=False),
               self.pod('other','demo-tests',deleting=True), self.pod('controller','chaos-mesh')]
        with patch.object(server.subprocess,'run') as command:
            command.return_value.returncode=0
            command.return_value.stdout=json.dumps({'items':items})
            result=server.get_cluster_status()
        self.assertTrue(result['connected'])
        self.assertEqual(result['total_pods'],3)
        self.assertEqual(result['ready_pods'],1)
        self.assertFalse(result['all_healthy'])
        pods={p['app']:p for p in result['pods']}
        self.assertEqual(pods['shop']['app_group'],'Registered Shop')
        self.assertEqual(pods['project']['app_group'],'Project Demo')
        self.assertEqual(pods['other']['app_group'],'demo-tests')
        self.assertEqual(pods['shop']['restarts'],3)
        self.assertEqual(command.call_args.args[0],['kubectl','get','pods','-A','-o','json'])

    def test_new_namespace_bypasses_previous_catalog_cache(self):
        with patch.object(server.subprocess,'run') as command:
            command.return_value.returncode=0
            command.return_value.stdout='{"items":[]}'
            server.get_cluster_status()
            self.apps.append({'namespace':'added-later','app_label':'new','name':'New'})
            server.get_cluster_status()
            self.assertEqual(command.call_count,2)

    def test_demo_registration_is_idempotent_discoverable_and_preserves_edits(self):
        demo=Path(__file__).resolve().parents[1]/'onboarding'/'demo'
        register=runpy.run_path(str(demo/'prepare_project_demo.py'))['register_experiment']
        with TemporaryDirectory() as temporary:
            directory=Path(temporary)
            target=register(directory)
            self.assertEqual(register(directory),target)
            with patch.object(runner,'EXPERIMENTS_DIR',str(directory/'missing')), \
                 patch.object(runner,'ONBOARDING_EXPERIMENTS_DIR',str(directory)):
                self.assertEqual([e['name'] for e in runner.discover_experiments()],['local-project-create-readback'])
                self.assertEqual(Path(runner.find_experiment_path('local-project-create-readback')),target)
            target.write_text('operator edits')
            with self.assertRaises(ValueError):
                register(directory)
            self.assertEqual(target.read_text(),'operator edits')
