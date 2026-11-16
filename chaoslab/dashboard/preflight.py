"""Read-only launch checks. Passing preflight is not authentication or authorization."""
import hashlib
import json
import os
import yaml

from runner import discover_experiments
from engine import load_experiment_config, generate_chaos_manifest, get_label_selector
from local_runtime import inspect_local_context, kubectl
from onboarding.registry import allowed_namespaces


def check_experiment(name):
    checks = []
    summary = {'experiment': name, 'ready': False, 'checks': checks, 'review_token': None}
    config = None

    def check(label, passed, detail):
        checks.append({'name': label, 'passed': bool(passed), 'detail': detail})

    def read(*args):
        return json.loads(kubectl(*args))

    def finish():
        summary['ready'] = bool(checks) and all(item['passed'] for item in checks)
        return summary, config

    check('Execution mode', os.environ.get('CHAOSLAB_ENABLE_FAULT_RUNS') == '1',
          'Execution enabled by the local operator.' if os.environ.get('CHAOSLAB_ENABLE_FAULT_RUNS') == '1'
          else 'Preview mode: restart the local launcher with --enable-fault-runs to permit execution.')
    matches = [item for item in discover_experiments() if item['name'] == name]
    if len(matches) != 1:
        check('Experiment', False, 'Choose one exact, uniquely named discovered experiment; file paths and partial names are not accepted.')
        return finish()
    try:
        config = load_experiment_config(matches[0]['file_path'])
        check('Experiment configuration', True, 'Configuration is valid.')
    except (ValueError, OSError, TypeError, KeyError, yaml.YAMLError) as error:
        check('Experiment configuration', False, str(error))
        return finish()
    ns = config['target']['namespace']
    selector = get_label_selector(config['target']['label'])
    summary.update({'namespace': ns, 'selector': selector, 'fault': config['fault']['type'],
                    'mode': config['fault']['mode'], 'expected_replicas': config['recovery']['expected_ready_replicas']})
    check('Fault scope', config['fault']['type'] == 'pod-kill',
          'Dashboard execution supports one-pod Pod Kill only; other faults remain CLI-only.')
    check('Approved namespace', ns in allowed_namespaces(), f'Target namespace: {ns}.')
    if not checks[-1]['passed'] or config['fault']['type'] != 'pod-kill':
        return finish()
    try:
        context = inspect_local_context(require_pinned=True)
        summary.update(context)
        check('Pinned local cluster', True, f'{context["context"]} — {context["server"]}')
    except (RuntimeError, ValueError, KeyError, TypeError) as error:
        check('Pinned local cluster', False, str(error))
        return finish()
    tasks = [
        ('Namespace opt-in', lambda: read('get', 'namespace', ns, '-o', 'json')['metadata'].get('annotations', {}).get('chaos-mesh.org/inject') == 'enabled',
         'The target namespace must explicitly opt into Chaos Mesh injection.'),
        ('Chaos Mesh controller', lambda: _controller_ready(read('get', 'deployment', 'chaos-controller-manager', '-n', 'chaos-mesh', '-o', 'json')),
         'Controller must be available with namespace filtering enabled.'),
        ('Chaos Mesh daemon', lambda: _daemon_ready(read('get', 'daemonset', 'chaos-daemon', '-n', 'chaos-mesh', '-o', 'json')),
         'Chaos Mesh daemon must be ready on every scheduled node.'),
    ]
    for label, action, detail in tasks:
        try:
            check(label, action(), detail)
        except (RuntimeError, ValueError, KeyError, TypeError) as error:
            check(label, False, str(error))
    try:
        pods = read('get', 'pods', '-n', ns, '-l', selector, '-o', 'json')['items']
        ready = [pod for pod in pods if not pod['metadata'].get('deletionTimestamp')
                 and pod.get('status', {}).get('phase') == 'Running'
                 and any(c.get('type') == 'Ready' and c.get('status') == 'True'
                         for c in pod.get('status', {}).get('conditions', []))]
        summary.update({'matching_pods': [p['metadata']['name'] for p in pods], 'ready_pods': len(ready)})
        check('Workload readiness', len(ready) >= summary['expected_replicas'],
              f'{len(ready)} ready of {len(pods)} matching pods; expected at least {summary["expected_replicas"]}.')
    except (RuntimeError, ValueError, KeyError, TypeError) as error:
        check('Workload readiness', False, str(error))
    resource_name, kind, manifest = generate_chaos_manifest(config)
    try:
        existing = kubectl('get', kind, resource_name, '-n', ns, '--ignore-not-found', '-o', 'json')
        check('No conflicting fault', not existing.strip(), f'{kind}/{resource_name} must not already exist.')
        for verb in ('create', 'delete'):
            permitted = kubectl('auth', 'can-i', verb, kind, '-n', ns).strip() == 'yes'
            check(f'Permission to {verb}', permitted, f'Operator must be allowed to {verb} {kind} in {ns}.')
        # Server-side validation only: it creates no fault resource.
        kubectl('create', '--dry-run=server', '-f', '-', input_text=json.dumps(manifest))
        check('Server validation', True, 'The cluster accepts this manifest in dry-run mode.')
    except (RuntimeError, ValueError) as error:
        check('Fault API validation', False, str(error))
    summary['review_token'] = hashlib.sha256(json.dumps({'config': config, 'context': context}, sort_keys=True).encode()).hexdigest()
    return finish()


def _controller_ready(deployment):
    env = deployment['spec']['template']['spec']['containers'][0].get('env', [])
    filtered = any(item.get('name') == 'ENABLE_FILTER_NAMESPACE' and item.get('value') == 'true' for item in env)
    return deployment.get('status', {}).get('availableReplicas', 0) >= 1 and filtered


def _daemon_ready(daemonset):
    status = daemonset.get('status', {})
    desired = status.get('desiredNumberScheduled', 0)
    return desired > 0 and status.get('numberReady', 0) >= desired
