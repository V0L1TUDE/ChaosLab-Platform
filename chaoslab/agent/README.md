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
| Jobs are data, never commands | The server sends a declarative config; the agent rebuilds a minimal config from a whitelist (one `app` label selector, fixed fault types). Application probes (which could carry `exec` commands) and telemetry are always disabled |
| Outbound only | The agent polls; nothing connects in. Plain `http://` to a non-local server is refused unless `--insecure-allow-http` |
| Least privilege | `agent-rbac.yaml`: read pods/events/deployments, create/delete Chaos Mesh `podchaos`/`networkchaos` **only in the allowed namespaces**; no delete-pod, exec, secrets, or cluster-wide write (verified with `kubectl auth can-i`) |
| Revocable, scoped credentials | One-time enrollment token → revocable agent credential (hashed server-side) → one-time per-job token valid only for that job's lease |
| Cluster identity | The credential is bound to your cluster's kube-system UID; use from a different cluster is rejected and audited. One cluster can belong to one ChaosLab account |
| Emergency stop | Press **Emergency stop** in the web app (cluster page): queued jobs are cancelled, the agent deletes the running fault within seconds, new jobs are blocked until you resume. **Ctrl+C** on the agent also removes the fault |
| Audit | Every enrollment, registration, job, stop/resume, and rejected attempt is recorded and visible on the cluster page |

What it sends to ChaosLab: pod names, readiness, Chaos Mesh status, Kubernetes events for the target, replica counts
(inventory of Deployments in allowed namespaces). It never sends kubeconfig, secrets, environment variables or application data.

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
| `--insecure-allow-http` | | off |

## Limitations (v1)
* Infrastructure invariants only (baseline, injection, impact, recovery, recovery time). Application probes and telemetry
  invariants show **N/A** for customer clusters: probes can run commands, so they are disabled until there is a safe,
  declarative probe format.
* Network faults: the engine's recovery time for network chaos is nominal (see the main README); it is shown as not measured.
* One experiment per cluster at a time; one agent per cluster.
* Evidence is trusted to the extent the agent is trusted (the server re-runs the Oracle on it but cannot independently
  observe your cluster). Run the agent from an image/source you control.
