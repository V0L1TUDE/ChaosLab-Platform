# ChaosLab Agent

Run resilience experiments in **your own** Kubernetes cluster from the ChaosLab web app, without giving ChaosLab
access to the cluster. The agent runs next to your cluster, calls ChaosLab **outbound only**, executes approved
experiments locally with the ChaosLab engine (Chaos Mesh), and sends back evidence. The web app verifies that
evidence with its own Verification Oracle and builds the report.

```
 ChaosLab web app (control plane)                    Your environment
 ───────────────────────────────                    ─────────────────────────────────
  users · apps · jobs · Oracle · reports   ◀─ HTTPS (outbound from the agent) ─  ChaosLab agent  ──kubectl──▶  your cluster
  (never connects to your cluster)                                              (+ Chaos Mesh, your apps)
```

## What the agent will and will not do

| Guarantee | How |
| --- | --- |
| Only touches namespaces **you** list | `--allowed-namespaces` is **required** and enforced by the agent itself on every job, in addition to the server's checks. A job for any other namespace is refused and reported |
| Only injects faults you allow | `--allowed-faults` (default `pod-kill,network-delay,network-loss`) and `--max-duration` (default 120 s) |
| Jobs are data, never commands | The server sends a declarative config; the agent rebuilds a minimal config from a whitelist (one `app` label selector, fixed fault types). The engine's own probes (which paste config values into scripts run inside your containers) and telemetry are never enabled |
| Probes can't run code | Application checks are declarative and read-only: **HTTP GET** to a port of a Ready pod through a temporary `kubectl port-forward` on 127.0.0.1, and `kubectl logs` substring checks. Strict whitelist (port range, URL-safe path, status range, bounded strings), no redirects followed, bodies capped, no `exec`. `--no-probes` turns them off |
| Outbound only | The agent polls; nothing connects in. Plain `http://` to a non-local server is refused unless `--insecure-allow-http` |
| Least privilege | `agent-rbac.yaml`: read pods/events/deployments/logs, `pods/portforward` create (for the GET probes), create/delete Chaos Mesh `podchaos`/`networkchaos` **only in the allowed namespaces**; no delete-pod, exec, secrets, or cluster-wide write. Verified by running the agent itself under that service account |
| Revocable, scoped credentials | One-time enrollment token → revocable agent credential (hashed server-side) → one-time per-job token valid only for that job's lease |
| Cluster identity | The credential is bound to your cluster's kube-system UID; use from a different cluster is rejected and audited. One cluster can belong to one ChaosLab account |
| Emergency stop | Press **Emergency stop** in the web app (cluster page): queued jobs are cancelled, the agent deletes the running fault within seconds, new jobs are blocked until you resume. **Ctrl+C** on the agent also removes the fault |
| Audit | Every enrollment, registration, job, stop/resume, and rejected attempt is recorded and visible on the cluster page |

What it sends to ChaosLab: pod names, readiness, Chaos Mesh status, Kubernetes events for the target, replica counts
(inventory of Deployments in allowed namespaces). It never sends kubeconfig, secrets, environment variables or application data.

## Application probes (verify the app, not just the pods)
On a cluster application's page, **Verification probes** lets you describe, per service:
* **Health** (required): `GET :port/path` must return the expected status (optionally containing some text)
* **Functional** (optional): a second GET, e.g. a read-only API call
* **Logs** (optional): text that must appear (startup) and text that must not (panic, fatal error)

How it runs: the agent probes once **before** the fault. If that already fails (wrong port/path, app unhealthy) the job stops with
*"Pre-flight probe failed, so NO fault was injected"*. Otherwise the fault runs, and after recovery the agent probes again (retrying up
to 25 s while the replacement warms up). The evidence goes to the server, whose Oracle then evaluates `app_health_probe`,
`app_functional_check` and `app_logs_healthy` (PASS/FAIL; N/A when not configured; SKIP if configured but the agent had no probe evidence).
Only GET requests are possible; use read-only endpoints.

## Tracing checks (telemetry invariants)
If your services send traces to Jaeger, start the agent with `--jaeger NAMESPACE/SERVICE:PORT` (for example
`--jaeger default/jaeger:16686`). Then, under **Tracing checks** on the application page, choose per service:
* the name of the service in Jaeger (defaults to the service name) and the maximum p95 latency after recovery;
* optionally a **caller** (e.g. `task-service` `GET /tasks/test`) that the agent requests once per second while the fault
  is active, and "require the fault to be visible as error spans".

After the experiment the agent queries Jaeger **read-only** through a temporary port-forward to that Service and reports
traces, p95 latency and error spans; the server's Oracle evaluates `telemetry_traces_recorded`,
`telemetry_latency_recovered` and `telemetry_error_spike_detected`. Where Jaeger runs is decided **only** by the agent's
flag; a job that names any endpoint is refused. If tracing cannot be collected (no `--jaeger`, Jaeger down), the three
checks show **SKIP**, never a false PASS or FAIL. With probes and tracing configured, all 11 invariants are evaluated.

Why the caller matters: when the target pod is killed it cannot record its own errors. Its callers can: in the
reference app, `task-service` keeps answering `200` (graceful degradation) while tracing shows the failed internal calls.

## Prerequisites
* A Kubernetes cluster with **Chaos Mesh** installed and your application running.
* `kubectl` configured for that cluster (or run in-cluster with `agent-rbac.yaml`).
* Python 3.9+ with `pyyaml`, and the ChaosLab repo (the agent uses `../verifier`), **or** the Docker image.

## Quick start (process)
1. In the web app: **Clusters → Connect a cluster**, name it, copy the one-time command.
2. Run it where `kubectl` reaches your cluster:
```bash
pip install pyyaml
python agent.py --server https://chaoslab.example.com --token cle_XXXX --allowed-namespaces shop,payments
```
3. The cluster turns **online**. Register an application (namespace + name), then **New experiment** → pick it → **Live**.

The token works once; afterwards the credential is stored in `--state-dir` (default `~/.chaoslab-agent`, mode 600).
Restarting the agent reuses it. To rotate: **New enrollment token** on the cluster page (the old credential stops working).

## Run in your cluster (recommended)
```bash
docker build -f chaoslab/agent/Dockerfile -t chaoslab-agent:1.0.0 .      # from the repo root; push to your registry
kubectl apply -f chaoslab/agent/agent-rbac.yaml                         # then repeat the Role/RoleBinding per namespace
kubectl -n chaoslab-agent create secret generic chaoslab-agent-token --from-literal=token=cle_XXXX
kubectl apply -f chaoslab/agent/agent-deployment.yaml                   # set CHAOSLAB_SERVER / CHAOSLAB_ALLOWED_NAMESPACES
```
The pod runs as non-root with a read-only service-account scope and a 16 MiB volume for its credential.

## Options (flag / environment variable)
| Flag | Env | Default |
| --- | --- | --- |
| `--server` | `CHAOSLAB_SERVER` | required |
| `--token` | `CHAOSLAB_ENROLL_TOKEN` | first start only |
| `--allowed-namespaces` | `CHAOSLAB_ALLOWED_NAMESPACES` | **required** |
| `--allowed-faults` | `CHAOSLAB_ALLOWED_FAULTS` | pod-kill,network-delay,network-loss |
| `--max-duration` | `CHAOSLAB_MAX_DURATION` | 120 |
| `--state-dir` | `CHAOSLAB_STATE_DIR` | `~/.chaoslab-agent` |
| `--jaeger` | `CHAOSLAB_JAEGER` | off (`NAMESPACE/SERVICE:PORT` of the Jaeger query Service) |
| `--insecure-allow-http` | | off |

## Limitations (v1)
* gRPC services can't be probed yet (HTTP GET and logs only); probe a gRPC app through an HTTP health endpoint if it has one.
* Tracing checks support Jaeger's query API (other backends such as Tempo are not supported yet).
* Network faults: the engine's recovery time for network chaos is nominal (see the main README); it is shown as not measured.
* One experiment per cluster at a time; one agent per cluster.
* Evidence is trusted to the extent the agent is trusted (the server re-runs the Oracle on it but cannot independently
  observe your cluster). Run the agent from an image/source you control.
