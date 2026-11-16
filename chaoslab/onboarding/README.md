# ChaosLab Onboarding — Local MVP

**Scope:** register an *already deployed* Kubernetes workload, inspect matching pods,
and generate a compatible Pod Kill experiment YAML for the existing ChaosLab
CLI/dashboard. This is **not** public SaaS or multi-user authentication. It does
not deploy uploaded code, install an agent, or execute a fault when registering.

## Run

For repeatable setup, diagnostics, preview startup, and reviewed local execution,
see [Local development](../LOCAL_DEVELOPMENT.md).

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
local test operators can explicitly opt-in using
`python chaoslab/local.py start --enable-fault-runs` after making those checks.
The launcher pins the cluster; setting an environment flag alone is insufficient. This opt-in is *not* authentication.

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
- Cleanup is attempted on verification errors and KeyboardInterrupt after a
  confirmed fault creation. The engine waits up to 20 seconds for deletion and
  fails visibly if cleanup cannot be confirmed. Process termination (SIGKILL),
  machine failure, or losing cluster access still require operator inspection.

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

### Review experiment history

The dashboard's **Historical Experiment Reports** section lists each archived run,
its verdict, recovery time, and passed/failed/skipped check counts. HTML, Markdown,
and JSON links open the exact run. Times display in the browser's local timezone.
Reports created before run archiving remain visible as legacy latest results;
unavailable check counts are shown explicitly.

New engine reports are archived under `chaoslab/reports/runs/<run-id>/` and ignored
by Git. Existing `<experiment>_report.*` latest exports are retained for the CLI
and quality gate. Report timestamps come from experiment evidence rather than
file-copy times. Saving a report does not run an experiment.

### Functional response checks

Expand **Optional functional response check** on a registered application. Enable
it and provide a pod-local path/port plus expected response text, a top-level JSON
key, or both. Text matching is case-sensitive. A requested JSON key must exist
with a non-null value; an unrelated `id` field cannot satisfy it. Both assertions
must pass when both are supplied. Onboarding permits read-only GET requests only.

For the nginx local demo, use path `/`, port `8080`, and text `Welcome to nginx!`.
This checks the expected page contents after recovery, not a business transaction.
API callers pass a `functional_check` object alongside `health_probe` to the draft
endpoint. Supported fields: `type: http`, `method: GET`, `path`, `port`,
`expected_status` (200–299), `timeout` (1–10 seconds), `expected_body_pattern`, and
`expected_json_key`. At least one response assertion is required. Regenerating
without this option removes it from the draft. Python or curl must be available
inside the selected container. Existing manually configured transaction probes
are preserved; onboarding does not expose write operations.

### Local project create/read-back experiment

The repository's project and user services can be exercised in `chaoslab-demo`
without publishing an image or exposing an external port. From the repository
root, with the project's Python environment active:

```sh
python chaoslab/onboarding/demo/prepare_project_demo.py > /tmp/chaoslab-project-demo.yaml
kubectl --context docker-desktop apply -f /tmp/chaoslab-project-demo.yaml
kubectl --context docker-desktop -n chaoslab-demo rollout status deployment/chaoslab-user
kubectl --context docker-desktop -n chaoslab-demo rollout status deployment/chaoslab-project
```

The manifest helper embeds current repository source in ConfigMaps and installs
its pinned dependencies in each pod's temporary volume. This is a local demo
recipe; it is not an image-based production deployment. Both services run as
non-root users and their services are cluster-internal only. The existing namespace
must already exist, and Chaos Mesh namespace filtering must explicitly permit it.

`chaoslab/onboarding/demo/project_transaction_experiment.yaml` is a dedicated
operator-reviewed configuration, separate from read-only onboarding. Its target
is only `app=chaoslab-project` in `chaoslab-demo`. After one pod is replaced, it
checks `/health`, POSTs a disposable project, GETs the returned ID, and verifies
its name, owner ID and resolved owner name. The `readback` option fails the
functional rule if the returned entity ID or expected fields differ.

Run it only on the verified disposable local cluster using a kubeconfig pinned
to that cluster. The local deployment uses SQLite when `PROJECT_DB_PATH` is set and retains
its volume across pod replacement. Other deployments retain the previous
in-memory mode. The older create/read-back experiment checks new transactions;
use `local-project-persistence-tracing` to verify a record created before the fault. Repeated tests create disposable
records; removing a pod loses its records. Telemetry is not configured.

### Dashboard discovery for the local transaction demo

Run `prepare_project_demo.py --register-experiment` to opt the reviewed local
transaction configuration into CLI/dashboard discovery. The helper still prints
workload manifests to stdout; registration is reported on stderr. It stores the
configuration under ignored `onboarding/data/experiments/`, without executing it.
Registration is idempotent and refuses to overwrite an edited local configuration.
Without the flag, no experiment is registered.

The dashboard's namespace filter and live topology include namespaces discovered
from registered applications and experiment configurations. Registered names label
their matching workloads. Unregistered workloads in a discovered namespace display
the namespace rather than being mislabeled as Online Boutique. Namespace discovery
does not grant permission to execute a fault. Fault execution remains a separate
explicit operator opt-in.

### Fault cleanup and conflicting runs

A run now creates its Chaos Mesh resource instead of replacing an existing one.
If the same resource name already exists, creation fails and that existing
resource is left untouched. Inspect it and its owner before deciding what to do.
After successful creation, cleanup runs even when probing or telemetry raises an
exception or the CLI receives KeyboardInterrupt. No completed verdict is emitted
until deletion is confirmed. A cleanup timeout is an execution failure, not a pass.
Reports from completed runs include `cleanup.confirmed: true` in JSON evidence.

This does not implement a dashboard Cancel button or a crash-recovery controller.
If creation returns an ambiguous transport error, or the process/machine stops
abruptly, inspect the named resource in the pinned local context. Do not blindly
repeat injection while its state is uncertain. Deleting a Pod Kill resource does
not undo pod termination; Kubernetes workload recovery is checked separately.


### Persistent records and correlated tracing

The local setup now includes a single-replica SQLite project service and a separate
Jaeger v2 backend. `python chaoslab/local.py setup-demo` provisions both and
registers `local-project-persistence-tracing`. The matching source configuration is
`demo/project_persistence_experiment.yaml`. It verifies pre-fault data survival,
a new post-recovery transaction, and that transaction's exact cross-service trace.
The new persistence rule is N/A for older experiments unless explicitly configured.

`application.persistence_check` specifies a POST `create` probe, a `read_path`
containing `{id}`, and `verify_fields`. The baseline record is created and read
before injection and compared after recovery. `storage_claim` names the PVC checked
by local dashboard preflight. `telemetry.required_services` and
`require_transaction_trace: true` require the same generated trace to contain the
configured services. The launcher-managed query URL is used by default; dashboard
preflight rejects a conflicting explicitly configured query endpoint.

Keep `chaoslab-project-data` to retain records; setup never deletes this PVC.
Trace storage is transient in this demo. See [Local development](../LOCAL_DEVELOPMENT.md)
for backup handling, startup, port forwarding, and verification boundaries.

The reviewed demo sets `telemetry.emit_client_span: true` and requires
`chaoslab-verifier` alongside the two application services. This opt-in uses the
in-pod OpenTelemetry SDK and `COLLECTOR_ADDR` to export the client parent span;
other custom images must provide that SDK before using the option. Receipt is
verified from Jaeger, rather than inferred from the exporter subprocess exit.
