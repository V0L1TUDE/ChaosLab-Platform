"""Repeatable local ChaosLab setup, diagnostics and loopback-only startup."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
CHAOSLAB = ROOT / 'chaoslab'
if str(CHAOSLAB) not in sys.path:
    sys.path.insert(0, str(CHAOSLAB))
from local_runtime import LOCAL_CONTEXT, kubectl, kubectl_binary, validate_local_config


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
    return checks


def setup_demo():
    import runpy
    from onboarding.registry import ApplicationRegistry
    demo = CHAOSLAB / 'onboarding' / 'demo'
    kubectl('apply', '-f', str(demo / 'demo-app.yaml'), timeout=30)
    helper = runpy.run_path(str(demo / 'prepare_project_demo.py'))
    import yaml
    kubectl('apply', '-f', '-', input_text=yaml.safe_dump_all(helper['prepare_manifests']()), timeout=30)
    kubectl('annotate', 'namespace', 'chaoslab-demo', 'chaos-mesh.org/inject=enabled', '--overwrite')
    for deployment in ('chaoslab-sample', 'chaoslab-user', 'chaoslab-project'):
        print(f'Waiting for {deployment}...', flush=True)
        kubectl('-n','chaoslab-demo','rollout','status','deployment/'+deployment,'--timeout=180s',timeout=190)
    helper['register_experiment']()
    registry = ApplicationRegistry()
    targets = {(app['namespace'],app['app_label']) for app in registry.list_apps()}
    if ('chaoslab-demo','chaoslab-sample') not in targets:
        record = registry.register({'name':'ChaosLab Local Demo','namespace':'chaoslab-demo',
                                    'app_label':'chaoslab-sample','expected_replicas':2})
        registry.generate_experiment(record['id'], health_probe={'path':'/','port':8080},
                                     functional_check={'path':'/','port':8080,'expected_body_pattern':'Welcome to nginx!'})
    print('Local demos are ready. Existing registrations and edited drafts were preserved.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('doctor','setup-demo','start'))
    parser.add_argument('--context', choices=(LOCAL_CONTEXT,), default=LOCAL_CONTEXT)
    parser.add_argument('--port', type=int, default=8081)
    parser.add_argument('--enable-fault-runs', action='store_true', help='Explicitly permit preflight-gated local Pod Kill runs')
    args = parser.parse_args(argv)
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
