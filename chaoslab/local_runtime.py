"""Local Docker Desktop runtime checks shared by startup and dashboard preflight."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlparse

LOCAL_CONTEXT = 'docker-desktop'
LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1', 'kubernetes.docker.internal'}


def kubectl_binary():
    found = shutil.which('kubectl')
    if found:
        return found
    bundled = Path('/Applications/Docker.app/Contents/Resources/bin/kubectl')
    if bundled.is_file():
        return str(bundled)
    raise RuntimeError('kubectl is missing. Install and start Docker Desktop with Kubernetes enabled.')


def kubectl(*args, input_text=None, timeout=10):
    try:
        result = subprocess.run([kubectl_binary(), *args], input=input_text, capture_output=True,
                                text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError('Kubernetes command unavailable or timed out') from error
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:350] or 'Kubernetes command failed')
    return result.stdout


def validate_local_config(config, expected_context=LOCAL_CONTEXT):
    if expected_context != LOCAL_CONTEXT or config.get('current-context') != expected_context:
        raise ValueError('This local launcher supports the docker-desktop context only.')
    contexts = config.get('contexts') or []
    selected = next((item['context'] for item in contexts if item.get('name') == expected_context), {})
    cluster = next((item.get('cluster', {}) for item in config.get('clusters', [])
                    if item.get('name') == selected.get('cluster')), {})
    endpoint = urlparse(cluster.get('server', ''))
    if endpoint.scheme != 'https' or endpoint.hostname not in LOCAL_HOSTS:
        raise ValueError('The selected Kubernetes API must be a local Docker Desktop HTTPS endpoint.')
    return endpoint.geturl()


def inspect_local_context(require_pinned=False):
    expected = os.environ.get('CHAOSLAB_PINNED_CONTEXT', '')
    if require_pinned and (expected != LOCAL_CONTEXT or not os.environ.get('KUBECONFIG')):
        raise RuntimeError('Start with python chaoslab/local.py start to pin the local cluster.')
    config = json.loads(kubectl('config', 'view', '--minify', '-o', 'json'))
    endpoint = validate_local_config(config, expected or LOCAL_CONTEXT)
    return {'context': LOCAL_CONTEXT, 'server': endpoint}


from contextlib import contextmanager
import socket
import time
import urllib.request


@contextmanager
def managed_trace_connection(port=16686):
    """Own exactly one loopback forward; never reuse or leave a foreign process."""
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise RuntimeError("Trace query port is occupied; choose another --trace-port") from error
    process = subprocess.Popen([kubectl_binary(), "port-forward", "--address", "127.0.0.1",
                                "-n", "chaoslab-observability", "svc/chaoslab-jaeger", f"{port}:16686"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    previous = os.environ.get("CHAOSLAB_JAEGER_QUERY_URL")
    url = f"http://127.0.0.1:{port}"
    try:
        ready = False
        for _ in range(30):
            if process.poll() is not None:
                break
            try:
                with urllib.request.urlopen(url + "/api/v3/services", timeout=1) as response:
                    ready = response.status == 200 and isinstance(json.load(response).get("services"), list)
            except (OSError, ValueError, AttributeError):
                pass
            if ready:
                break
            time.sleep(0.2)
        if not ready:
            raise RuntimeError("Local Jaeger forward did not become ready")
        os.environ["CHAOSLAB_JAEGER_QUERY_URL"] = url
        yield url
    finally:
        if previous is None:
            os.environ.pop("CHAOSLAB_JAEGER_QUERY_URL", None)
        else:
            os.environ["CHAOSLAB_JAEGER_QUERY_URL"] = previous
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
