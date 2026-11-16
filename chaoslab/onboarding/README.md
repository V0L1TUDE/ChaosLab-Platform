# ChaosLab Onboarding — Local MVP

**Scope:** register an *already deployed* Kubernetes workload, inspect matching pods,
and generate a compatible Pod Kill experiment YAML for the existing ChaosLab
CLI/dashboard. This is **not** public SaaS or multi-user authentication. It does
not deploy uploaded code, install an agent, or execute a fault when registering.

## Run

Activate the project's Python environment (Python 3.12, PyYAML installed) and
from repository root run:

```sh
PYTHONDONTWRITEBYTECODE=1 python -c "from chaoslab.dashboard.server import run_server; run_server(host='127.0.0.1',port=8080)"
```

Visit `http://127.0.0.1:8080/onboarding`. The existing dashboard remains at `/`.
The onboarding page creates application metadata in a local SQLite database,
ignored by Git. Configurations generated from registrations are saved as
`chaoslab/onboarding/data/experiments/registered-*.yaml`, ignored by Git, and automatically
appear in `python chaoslab/runner.py list` and `/api/experiments`.

By default, only `chaoslab-demo` is an allowed target namespace. The operator
can set `CHAOSLAB_ALLOWED_NAMESPACES` to an explicit comma-separated set of
**isolated, approved test namespaces**. Never include `default` or system
namespaces. Register a workload with `app=<label>` matching its deployed pods.

### Optional local sample (after Kubernetes is correctly installed)

**Only on a disposable local Kubernetes context you own:**

```sh
kubectl config current-context
kubectl apply -f chaoslab/onboarding/demo/demo-app.yaml
kubectl -n chaoslab-demo rollout status deploy/chaoslab-sample
kubectl -n chaoslab-demo get pods -l app=chaoslab-sample
```

Then register:
- Name: `ChaosLab Sample`
- Namespace: `chaoslab-demo`
- Pod label: `chaoslab-sample`
- Expected ready replicas: `2`

Click **Check matching pods** to compare Ready, non-terminating pods with the
registered replica count. Running pods without a Ready condition do not count.
An empty result usually means the namespace or `app` label does not match the
deployed workload. The existing `demo-service` registration does not match this
sample; register `chaoslab-sample` with two expected replicas for this manifest.
Then click **Generate Pod Kill draft**. No fault is
executed by either operation. To see the generated draft in the original
runner, use `PYTHONDONTWRITEBYTECODE=1 python chaoslab/runner.py list`.

**Do not run a generated Pod Kill experiment yet** unless a sandbox cluster,
Chaos Mesh compatibility, target scope, fallback cleanup, and current kubectl
context have been verified. Pod Kill terminates pods; a duration setting does
not reverse termination. Existing experiment-run API is now disabled by default;
local test operators can explicitly opt-in with `CHAOSLAB_ENABLE_FAULT_RUNS=1`
after making those checks. This opt-in is *not* authentication.

### Unit tests (offline)

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s chaoslab/tests -p 'test_*.py' -v
```

### Security and limitations

- Local-only server bound to loopback. Never expose this server via a public
  tunnel or cloud proxy without replacing it with real authentication,
  authorization, CSRF protection, and per-customer isolation.
- Registration does not verify customer ownership of arbitrary Kubernetes
  resources. Restrict allowed namespaces to disposable test workloads.
- Pod discovery uses **read-only** `kubectl get pods` and shows the active
  kubeconfig context's results. It does not set or switch clusters.
- There is no arbitrary image upload, manifest deployment API, hosted agent,
  multi-tenant RBAC, public signup, automatic remote provisioning, or security
  vulnerability scanner in this version.
- Generated configs use pod readiness verification only by default; optional HTTP
  checks can be configured during draft generation.
  Application-specific HTTP/gRPC probes and telemetry need to be configured
  for each workload before claiming full application-level correctness.
- Existing engine cleanup/cancellation needs hardening before production use.

### Optional HTTP verification

Expand **Optional HTTP health check** on a registered application and enable
**Verify HTTP response after recovery** before generating its draft. Enter the
pod-local port and health path, plus the expected successful HTTP status. For
`ChaosLab Local Demo`, use port `8080`, path `/`, and status `200`.

The API accepts `health_probe: {type: http, port: 8080, path: /,
expected_status: 200, timeout: 5}` in the experiment-generation JSON object.
This stores the check in the generated YAML, not the application registry.
Regenerating without the option replaces the draft with readiness-only checks.
Only local paths, GET requests, successful expected statuses, and bounded
ports/timeouts are accepted by onboarding. The selected container must provide
Python or curl. A rejected status or unreachable endpoint fails the health rule;
pod readiness alone cannot satisfy an enabled HTTP check. This does not verify
business transactions or distributed traces.
