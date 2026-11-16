"""Repeatable local ChaosLab setup, diagnostics and loopback-only startup."""
import argparse
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import sys
import uuid
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
CHAOSLAB = ROOT / 'chaoslab'
if str(CHAOSLAB) not in sys.path:
    sys.path.insert(0, str(CHAOSLAB))
from local_runtime import LOCAL_CONTEXT, kubectl, kubectl_binary, validate_local_config, managed_trace_connection


@contextmanager
def pinned_cluster(context=LOCAL_CONTEXT):
    """Private snapshot: never update the operator's default context or config."""
    raw = kubectl('config', 'view', '--context', context, '--minify', '--flatten', '--raw', '-o', 'json')
    config = json.loads(raw)
    validate_local_config(config, context)
    previous = {key: os.environ.get(key) for key in ('KUBECONFIG', 'CHAOSLAB_PINNED_CONTEXT', 'PATH')}
    with TemporaryDirectory(prefix='chaoslab-session-') as temporary:
        snapshot = Path(temporary) / 'kubeconfig.json'
        # The containing directory is private; the snapshot contains credentials.
        descriptor = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(raw)
        os.environ['KUBECONFIG'] = str(snapshot)
        os.environ['CHAOSLAB_PINNED_CONTEXT'] = context
        os.environ['PATH'] = str(Path(kubectl_binary()).parent) + os.pathsep + os.environ.get('PATH', '')
        try:
            yield {'context': context, 'server': validate_local_config(config, context)}
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def doctor():
    checks = []
    def check(name, action):
        try:
            passed, detail = action()
        except (RuntimeError, ValueError, KeyError, TypeError) as error:
            passed, detail = False, str(error)
        checks.append({'name': name, 'passed': bool(passed), 'detail': detail})
    def nodes():
        items = json.loads(kubectl('get', 'nodes', '-o', 'json'))['items']
        ready = sum(any(c.get('type') == 'Ready' and c.get('status') == 'True'
                        for c in node.get('status', {}).get('conditions', [])) for node in items)
        return ready > 0 and ready == len(items), f'{ready}/{len(items)} nodes ready'
    check('Kubernetes nodes', nodes)
    from dashboard.preflight import _controller_ready, _daemon_ready
    check('Chaos Mesh controller', lambda: (_controller_ready(json.loads(kubectl('get','deployment','chaos-controller-manager','-n','chaos-mesh','-o','json'))),
                                          'Controller must be available with namespace filtering enabled'))
    check('Chaos Mesh daemon', lambda: (_daemon_ready(json.loads(kubectl('get','daemonset','chaos-daemon','-n','chaos-mesh','-o','json'))),
                                      'Daemon must be ready on all scheduled nodes'))
    check('Local target namespace', lambda: (json.loads(kubectl('get','namespace','chaoslab-demo','-o','json'))['metadata'].get('annotations',{}).get('chaos-mesh.org/inject') == 'enabled',
                                           'chaoslab-demo must opt into Chaos Mesh; use setup-demo after installing Chaos Mesh'))
    check('Persistent project storage', lambda: (json.loads(kubectl('get','pvc','chaoslab-project-data','-n','chaoslab-demo','-o','json')).get('status',{}).get('phase') == 'Bound',
                                                'Project-data PVC must be Bound; run setup-demo to provision it'))
    check('Local tracing', lambda: (json.loads(kubectl('get','deployment','chaoslab-jaeger','-n','chaoslab-observability','-o','json')).get('status',{}).get('availableReplicas',0) >= 1,
                                  'Jaeger must be available in chaoslab-observability'))
    return checks


def backup_memory_projects():
    """Preserve legacy in-memory records before setup changes their pod template."""
    pods = json.loads(kubectl('get','pods','-n','chaoslab-demo','-l','app=chaoslab-project','-o','json'))['items']
    if not pods:
        return None
    ready = [pod for pod in pods if pod.get('status',{}).get('phase') == 'Running'
             and not pod['metadata'].get('deletionTimestamp')
             and any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in pod.get('status',{}).get('conditions',[]))]
    if not ready:
        raise RuntimeError('Existing project service is not ready; refusing to update it without checking its data.')
    script = ("import json,urllib.request; "
              "health=json.load(urllib.request.urlopen('http://127.0.0.1:8002/health',timeout=3)); "
              "records=json.load(urllib.request.urlopen('http://127.0.0.1:8002/projects',timeout=5)) if health.get('storage','memory')=='memory' else None; "
              "print(json.dumps(records))")
    records = json.loads(kubectl('exec',ready[0]['metadata']['name'],'-n','chaoslab-demo','--','python','-c',script,timeout=15))
    if records is None:
        return None
    if not isinstance(records,list) or any(not isinstance(record,dict) or not record.get('id') for record in records):
        raise RuntimeError('Legacy project backup returned invalid records; setup stopped.')
    folder = CHAOSLAB / 'onboarding' / 'data' / 'backups'
    folder.mkdir(parents=True,exist_ok=True)
    backup = folder / ('project-records-' + uuid.uuid4().hex + '.json')
    descriptor = os.open(backup,os.O_WRONLY | os.O_CREAT | os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
        json.dump(records,stream,indent=2)
    print(f'Preserved {len(records)} legacy project records in {backup}',flush=True)
    return records


def restore_projects(records):
    if records is None:
        return
    script = ("import json,sys,os; from storage import ProjectStore; records=json.load(sys.stdin); "
              "store=ProjectStore(os.environ['PROJECT_DB_PATH'],{item['id']:item for item in records}); "
              "assert all(store.get(item['id'])==item for item in records), 'Restored records differ; retain backup and inspect the database'; "
              "print('Legacy project records restored and verified')")
    print(kubectl('exec','-i','deployment/chaoslab-project','-n','chaoslab-demo','--','python','-c',script,
                  input_text=json.dumps(records),timeout=30).strip(),flush=True)


def setup_demo():
    import runpy
    from onboarding.registry import ApplicationRegistry
    demo = CHAOSLAB / 'onboarding' / 'demo'
    kubectl('apply', '-f', str(demo / 'jaeger-local.yaml'), timeout=30)
    kubectl('apply', '-f', str(demo / 'demo-app.yaml'), timeout=30)
    legacy_records = backup_memory_projects()
    helper = runpy.run_path(str(demo / 'prepare_project_demo.py'))
    import yaml
    kubectl('apply', '-f', '-', input_text=yaml.safe_dump_all(helper['prepare_manifests']()), timeout=30)
    kubectl('annotate', 'namespace', 'chaoslab-demo', 'chaos-mesh.org/inject=enabled', '--overwrite')
    for deployment in ('chaoslab-sample', 'chaoslab-user', 'chaoslab-project'):
        print(f'Waiting for {deployment}...', flush=True)
        kubectl('-n','chaoslab-demo','rollout','status','deployment/'+deployment,'--timeout=180s',timeout=190)
    restore_projects(legacy_records)
    helper['register_experiment']()
    helper['register_experiment'](persistence=True)
    registry = ApplicationRegistry()
    targets = {(app['namespace'],app['app_label']) for app in registry.list_apps()}
    if ('chaoslab-demo','chaoslab-sample') not in targets:
        record = registry.register({'name':'ChaosLab Local Demo','namespace':'chaoslab-demo',
                                    'app_label':'chaoslab-sample','expected_replicas':2})
        registry.generate_experiment(record['id'], health_probe={'path':'/','port':8080},
                                     functional_check={'path':'/','port':8080,'expected_body_pattern':'Welcome to nginx!'})
    kubectl('-n','chaoslab-observability','rollout','status','deployment/chaoslab-jaeger','--timeout=180s',timeout=190)
    print('Local demos are ready. Existing registrations and edited drafts were preserved.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('doctor','setup-demo','start'))
    parser.add_argument('--context', choices=(LOCAL_CONTEXT,), default=LOCAL_CONTEXT)
    parser.add_argument('--port', type=int, default=8081)
    parser.add_argument('--trace-port', type=int, default=16686, help='Loopback Jaeger query port')
    parser.add_argument('--enable-fault-runs', action='store_true', help='Explicitly permit preflight-gated local Pod Kill runs')
    args = parser.parse_args(argv)
    if not 1 <= args.trace_port <= 65535 or args.trace_port == args.port:
        parser.error('Trace port must be 1–65535 and different from dashboard port')
    if not 1 <= args.port <= 65535:
        parser.error('Port must be between 1 and 65535')
    try:
        with pinned_cluster(args.context) as context:
            print(f'Local cluster: {context["context"]} ({context["server"]})', flush=True)
            if args.command == 'setup-demo':
                setup_demo()
                return 0
            checks = doctor()
            for item in checks:
                print(f'{"PASS" if item["passed"] else "BLOCKED"}: {item["name"]}: {item["detail"]}', flush=True)
            if args.command == 'doctor':
                return 0 if all(item['passed'] for item in checks) else 1
            if args.enable_fault_runs and not all(item['passed'] for item in checks):
                print('Execution startup blocked. Resolve diagnostics or start in preview mode.', file=sys.stderr)
                return 1
            os.environ['CHAOSLAB_ENABLE_FAULT_RUNS'] = '1' if args.enable_fault_runs else '0'
            print('Execution enabled: every run requires fresh preflight and target review.' if args.enable_fault_runs
                  else 'Preview mode: fault execution is disabled.', flush=True)
            from dashboard.server import run_server
            tracing_ready = any(item['name'] == 'Local tracing' and item['passed'] for item in checks)
            with managed_trace_connection(args.trace_port) if tracing_ready else nullcontext():
                run_server(host='127.0.0.1', port=args.port)
            return 0
    except ImportError as error:
        print("Missing local dependency. Install chaoslab/requirements-local.txt in your Python environment.", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError, OSError) as error:
        print(f'Local setup blocked: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
