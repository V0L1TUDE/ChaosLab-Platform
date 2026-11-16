# ChaosLab local workflow

This workflow supports Docker Desktop Kubernetes (`docker-desktop`) on this Mac.
It binds the dashboard to `127.0.0.1`; it is a single-operator local MVP.

## Prerequisites

Use the project's Python 3.12 environment and install its local dependency:

```sh
python -m pip install -r chaoslab/requirements-local.txt
```

Docker Desktop must be running with Kubernetes enabled. Chaos Mesh must already
be installed for fault tests. The reviewed local Helm values are in
`chaoslab/onboarding/demo/chaos-mesh-values.yaml`; our verified chart version is
2.8.8. Installing that controller is a separate privileged setup step, never a
side effect of starting the dashboard. The launcher does not install tools or
switch/reset/create clusters.

## Diagnose, set up, start

Run these commands from repository root with the project environment active:

```sh
python chaoslab/local.py doctor
python chaoslab/local.py setup-demo
python chaoslab/local.py start
```

`doctor` checks the local node, controller namespace filtering, daemon readiness,
the demo namespace opt-in, a Bound project-data PVC, and local Jaeger readiness. It exits with a nonzero code for blockers.
`setup-demo` is an explicit deployment operation: it applies only the reviewed
sample, project, and user services to `chaoslab-demo`, installs the cluster-internal
Jaeger v2 backend in `chaoslab-observability`, opts the demo namespace into
Chaos Mesh, registers the transaction experiment, and waits for readiness.
Existing registrations and edited local experiment configurations are preserved.
A ready legacy in-memory project deployment is backed up before updating and its
records are imported with their original IDs. Backups remain under ignored
`chaoslab/onboarding/data/backups/` with private file permissions. If a legacy
service exists but is not ready, setup stops rather than silently replacing it.
The project/user dependency downloads require Internet access on first startup
and replacement. No service is published outside the cluster.

`start` serves preview mode at <http://127.0.0.1:8081>. It does not deploy anything
or enable injection. Use `--port 8082` if the port is occupied. The script also
works by absolute path from another working directory. Ctrl+C closes the server;
if a run is active, it first waits for the bounded experiment and cleanup.

## Explicitly enable reviewed local runs

```sh
python chaoslab/local.py start --enable-fault-runs
```

This requires healthy startup diagnostics. Select an experiment in Chaos Studio,
click **Check readiness**, review the exact context, namespace and label selector,
and check the target-review box. Then click **Run reviewed experiment**.
Preflight checks are read-only, including server-side dry-run validation. The
server repeats them before launch. The API rejects stale configuration reviews,
file paths, partial names, unapproved namespaces, concurrent runs, missing target
confirmation, or existing conflicting faults. Browser reviews expire after one
minute. Changing the selected experiment clears the review. Only one-pod
Pod Kill is enabled through the dashboard; other faults remain CLI-only.

The worker runs a copy of the reviewed configuration, not a file re-read after
launch. Report history and check counts update automatically after completion.
A failed engine callback or cleanup error is displayed as a failed execution.

## Cluster and data boundaries

Each command copies the selected local kubeconfig into a private temporary
session file (permissions 0600). Its credentials are never printed. The original
kubeconfig/current context remain unchanged, and the session file is deleted
when the command finishes. The local API endpoint must use HTTPS and resolve to
a local Docker Desktop host name or loopback address. All subprocesses inherit
the pinned session file, including the fault engine and probes.

The run opt-in and review token are not authentication. Do not tunnel or expose
this server publicly. There is no distributed lock across multiple dashboard
processes; run one execution-enabled dashboard per local cluster. A pre-existing
fault is refused rather than replaced. Process/machine failure or an ambiguous
create timeout still requires operator inspection. There is no Cancel button.

Local application records, drafts, and per-run reports are ignored by Git. Latest
exports for built-in experiment suites retain their previous behavior. The local project demo now uses SQLite on a 1 GiB persistent volume. Its single
replica uses the Recreate rollout strategy; the PVC is retained by setup and
across pod replacement. This is not a multi-replica production database or a
backup/disaster-recovery system. Other deployments keep in-memory mode unless
`PROJECT_DB_PATH` is explicitly set.


## Persistent-data and distributed-trace verification

`setup-demo` registers **local-project-persistence-tracing**. Select it in the
dashboard, pass readiness review, and launch only in explicit execution mode.
It creates and reads a baseline project **before** injection, replaces the project
pod, verifies the same ID and configured contents afterward, and verifies a new
create/read-back transaction. The persistent-data rule fails if readiness returns
but the old record is absent or changed. Failed baseline seeding prevents injection.

The same post-recovery functional workflow carries a generated sampled W3C trace
ID. The oracle requires that exact trace to contain spans from `project-service`
and `user-service`; unrelated health checks and old traces cannot satisfy it.
The reviewed demo also exports the real verification client span as
`chaoslab-verifier`, using the OpenTelemetry SDK already installed inside the
application image. All three services are required in the correlated trace, so
the client parent is present and Jaeger can render a complete tree.
Target-service server spans determine latency. Missing telemetry, unavailable
Jaeger, malformed data, or no target spans cannot pass a zero-latency SLA.
Export retries are bounded. Fault-window errors are optional and are not claimed
when that rule is not configured.

Jaeger 2.23.0 uses the stable v3 OTLP JSON query API. Its image is from the official
`jaegertracing/jaeger` repository. The demo backend uses transient trace storage;
traces are lost if Jaeger restarts. It is separate from the project's persistent
record store. See [Jaeger APIs](https://www.jaegertracing.io/docs/2.23/architecture/apis/).
The launcher owns a loopback-only Jaeger forward, closes it on exit, and never
reuses or terminates another process's forward. Use `--trace-port 16687` if 16686
is occupied. The dashboard's Jaeger and trace links use the managed port.
Standalone CLI users must provide their own query connection explicitly; the
telemetry client no longer silently launches background processes.

HTML, Markdown and JSON reports contain the persisted record's before/after
evidence, correlated service names and trace ID, and check outcomes. These data
and per-run archives remain local and excluded from Git. Preserving a volume does
not test disk loss, node loss, database corruption, or backup restoration. Observed
latency is from the verification transactions, not a sustained load test.
