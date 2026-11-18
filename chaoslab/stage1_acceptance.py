"""Exercise the hosted HTTP/agent workflow against the isolated Docker Desktop demo.

This opt-in check replaces one chaoslab-project pod. It creates a temporary webapp
and account, enrolls a real agent, restarts it using its saved credential, discovers
workloads, runs health/functional/tracing verification and downloads the reports.
It never deploys to the public website or modifies the default Kubernetes context.
"""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import urllib.error
import urllib.request

from local import pinned_cluster, doctor
from local_runtime import kubectl

ROOT = Path(__file__).resolve().parent


def wait_for(action, timeout=45):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            result = action()
            if result:
                return result
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last = exc
        time.sleep(1)
    raise RuntimeError(f"Timed out waiting for the hosted workflow ({last or 'not ready'})")


def stop(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=40)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def run(output):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    server = agent = None
    with pinned_cluster(), TemporaryDirectory(prefix="chaoslab-stage1-") as temporary:
        for check in doctor():
            if not check['passed']:
                raise RuntimeError(check['name'] + ': ' + check['detail'])
        faults = json.loads(kubectl('get', 'podchaos,networkchaos', '-n', 'chaoslab-demo', '-o', 'json'))
        if faults.get('items'):
            raise RuntimeError('An existing fault is present; finish it before running this check.')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        env = dict(os.environ, CHAOSLAB_DATA_DIR=str(Path(temporary) / 'data'),
                   CHAOSLAB_SHOW_DEMO_APPS='false', CHAOSLAB_LIVE_ENABLED='false',
                   CHAOSLAB_DEPLOY_ENABLED='false', CHAOSLAB_DISCOVERY_ENABLED='false',
                   CHAOSLAB_SHARED_APP_ID='', CHAOSLAB_COOKIE_SECURE='false',
                   CHAOSLAB_PUBLIC_URL=base, CHAOSLAB_TRUST_PROXY='false', PYTHONDONTWRITEBYTECODE='1')
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def api(method, path, body=None, raw=False):
            data = json.dumps(body).encode() if body is not None else None
            request = urllib.request.Request(base + path, data=data, method=method,
                                             headers={'Content-Type': 'application/json'})
            with opener.open(request, timeout=15) as response:
                payload = response.read()
            return payload if raw else json.loads(payload)

        logs = open(output / 'workflow.log', 'w', encoding='utf-8')
        try:
            server = subprocess.Popen([sys.executable, '-m', 'webapp', '--port', str(port)],
                                      cwd=ROOT, env=env, stdout=logs, stderr=logs)
            wait_for(lambda: api('GET', '/api/auth/me'))
            api('POST', '/api/auth/register', {'name': 'Stage 1 acceptance',
                'email': f'stage1-{secrets.token_hex(8)}@example.com', 'password': secrets.token_urlsafe(24)})
            enrollment = api('POST', '/api/clusters', {'name': 'Docker Desktop Stage 1'})
            cid = enrollment['cluster']['id']
            state = Path(temporary) / 'agent'
            agent_command = [sys.executable, str(ROOT / 'agent' / 'agent.py'), '--server', base,
                '--allowed-namespaces', 'chaoslab-demo', '--allowed-faults', 'pod-kill',
                '--state-dir', str(state), '--jaeger', 'chaoslab-observability/chaoslab-jaeger:16686']
            print('Connecting the real agent to the hosted API...', flush=True)
            agent = subprocess.Popen(agent_command, env=dict(env, CHAOSLAB_ENROLL_TOKEN=enrollment['enrollment']['token']),
                                     stdout=logs, stderr=logs)

            def connected():
                cluster = api('GET', f'/api/clusters/{cid}')['cluster']
                return cluster if cluster['online'] and cluster.get('inventory', {}).get('chaoslab-demo') else None

            first = wait_for(connected)
            credential = json.loads((state / 'agent.json').read_text())
            stop(agent)
            agent = subprocess.Popen(agent_command, env=dict(env, CHAOSLAB_ENROLL_TOKEN=''), stdout=logs, stderr=logs)
            wait_for(lambda: (c if (c := connected()) and c['last_seen'] != first['last_seen'] else None))
            if json.loads((state / 'agent.json').read_text()) != credential:
                raise RuntimeError('Agent restart did not reuse the enrolled credential')
            print('Agent reconnected using its stored credential.', flush=True)
            app = api('POST', f'/api/clusters/{cid}/applications',
                      {'name': 'Stage 1 Local Demo', 'namespace': 'chaoslab-demo'})['application_id']
            detail = api('GET', f'/api/applications/{app}')['application']
            services = detail['catalog']['services']
            if not any(s['name'] == 'chaoslab-project' for s in services):
                raise RuntimeError('Real project workload was not discovered')
            api('POST', f'/api/applications/{app}/probes', {'service': 'chaoslab-project', 'probes': {
                'health': {'port': 8002, 'path': '/health', 'expected_body_contains': 'SERVING'},
                'functional': {'port': 8002, 'path': '/projects', 'expected_status': 200}}})
            api('POST', f'/api/applications/{app}/telemetry', {'service': 'chaoslab-project',
                'telemetry': {'service_name': 'project-service', 'max_allowed_p95_ms': 1000}})
            print('Running one real Pod Kill on chaoslab-demo / app=chaoslab-project...', flush=True)
            exp = api('POST', '/api/experiments', {'application_id': app, 'target_service': 'chaoslab-project',
                      'fault_type': 'pod-kill', 'duration_seconds': 120, 'mode': 'live'})['experiment']['id']

            def completed():
                detail = api('GET', f'/api/experiments/{exp}')['experiment']
                return detail if detail['status'] in ('completed', 'failed') else None

            detail = wait_for(completed, timeout=240)
            (output / 'experiment.json').write_text(json.dumps(detail, indent=2))
            for fmt in ('json', 'html'):
                (output / f'report.{fmt}').write_bytes(api('GET', f'/api/experiments/{exp}/report?format={fmt}', raw=True))
            if detail['status'] != 'completed' or detail['oracle_verdict'] != 'PASS':
                raise RuntimeError(f"Experiment failed: {detail.get('error') or detail['oracle_verdict']}")
            result = detail['result']
            rules = {r['rule']: r['status_label'] for r in result['oracle']['rules']}
            for name in ('app_health_probe', 'app_functional_check', 'telemetry_traces_recorded', 'telemetry_latency_recovered'):
                if rules.get(name) != 'PASS':
                    raise RuntimeError(f'Required acceptance check did not pass: {name}')
            if result['simulated'] or result['mode'] != 'live' or 'agent' not in result['data_source']:
                raise RuntimeError('Expected real agent evidence, not simulated results')
            remaining = json.loads(kubectl('get', 'podchaos,networkchaos', '-n', 'chaoslab-demo', '-o', 'json'))
            if remaining.get('items'):
                raise RuntimeError('Fault cleanup was not confirmed')
            summary = {'status': 'PASS', 'scope': 'hosted platform code on loopback with real Docker Desktop agent',
                       'cluster': 'docker-desktop', 'namespace': 'chaoslab-demo', 'target': 'chaoslab-project',
                       'agent_reconnection': 'PASS', 'workload_discovery': 'PASS', 'report_downloads': ['html', 'json'],
                       'fault_cleanup': 'PASS', 'metrics': result['metrics'], 'oracle': result['oracle']['counts'],
                       'public_deployment_verified': False}
            (output / 'summary.json').write_text(json.dumps(summary, indent=2))
            print(json.dumps(summary, indent=2), flush=True)
        finally:
            stop(agent)
            stop(server)
            logs.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-local-experiment', action='store_true', help='Explicitly allow replacing one local demo project pod')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if not args.run_local_experiment:
        parser.error('--run-local-experiment is required; this check injects one local Pod Kill')
    run(args.output_dir)


if __name__ == '__main__':
    main()
