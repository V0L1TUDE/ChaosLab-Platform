# ChaosLab Online MVP

A multi-user web front for the ChaosLab resilience engine: accounts, application registry,
experiment configuration, live monitoring, Verification Oracle results and downloadable reports.

Stdlib-only backend (`sqlite3`, `http.server`) plus PyYAML (already required by the engine).
The frontend is a no-build vanilla-JS SPA. The original dashboard (`chaoslab/dashboard`) and CLI
(`chaoslab/runner.py`) are untouched.

## Run locally

```bash
pip install pyyaml            # only dependency (already needed by the engine)
cd chaoslab
python -m webapp              # http://127.0.0.1:8090
python -m webapp --port 9000 --host 0.0.0.0
```

Open the URL, register an account, and try **New experiment → Demo execution**.
For **Live execution** you need `kubectl` pointing at a cluster that runs the Online Boutique demo
in namespace `default` and has Chaos Mesh installed (same prerequisites as `python chaoslab/runner.py run`).
The app checks this itself and disables Live mode with the reason if it is not available.

Tests (isolated temp DB, no cluster needed):

```bash
cd chaoslab && python -m unittest discover -s webapp/tests
```

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `CHAOSLAB_HOST` / `CHAOSLAB_PORT` | `127.0.0.1` / `8090` | Bind address |
| `CHAOSLAB_DATA_DIR` | `chaoslab/webapp/data` | Root for DB, uploads, reports (git-ignored) |
| `CHAOSLAB_DB_PATH` | `<data>/chaoslab.db` | SQLite file |
| `CHAOSLAB_UPLOAD_DIR` | `<data>/uploads` | Uploaded files when `CHAOSLAB_STORAGE=local` |
| `CHAOSLAB_SHOW_DEMO_APPS` | `false` | Expose the 3 built-in demo apps to every user. **Off by default**: users only see their own apps (uploads, sandbox apps, connected clusters) |
| `CHAOSLAB_DEPLOY_ENABLED` | = `CHAOSLAB_LIVE_ENABLED` | Allow deploying uploaded manifests to the cluster |
| `CHAOSLAB_DEPLOY_TTL_MINUTES` | `120` | Auto-undeploy age (`0` disables) |
| `CHAOSLAB_DEPLOY_TIMEOUT_SECONDS` | `180` | Wait for workloads to become Ready |
| `CHAOSLAB_MAX_DEPLOYED_PER_USER` / `_TOTAL` | `2` / `6` | Concurrent deployed apps |
| `CHAOSLAB_MAX_REPLICAS` | `3` | Max replicas per uploaded Deployment |
| `CHAOSLAB_ALLOWED_REGISTRIES` | docker.io, ghcr.io, gcr.io, quay.io, registry.k8s.io, public.ecr.aws, mcr.microsoft.com | Allowed image registries (comma separated) |
| `CHAOSLAB_STORAGE` | `local` | `local` or `s3` for uploaded application files |
| `CHAOSLAB_S3_BUCKET` | (none) | Required for `s3`. Private bucket name |
| `CHAOSLAB_S3_REGION` | AWS default | Bucket region |
| `CHAOSLAB_S3_PREFIX` | `uploads/` | Key prefix; the app only writes/deletes under it |
| `CHAOSLAB_S3_SSE` | `AES256` | `AES256`, `aws:kms` or `none` (server-side encryption) |
| `CHAOSLAB_S3_KMS_KEY_ID` | (none) | KMS key id/alias when `aws:kms` |
| `CHAOSLAB_S3_ENDPOINT_URL` | (none) | MinIO / LocalStack / S3-compatible endpoint |
| `CHAOSLAB_REPORT_DIR` | `<data>/reports` | Generated reports |
| `CHAOSLAB_MAX_UPLOAD_MB` | `10` | Upload size limit |
| `CHAOSLAB_SESSION_TTL_SECONDS` | `604800` | Session lifetime |
| `CHAOSLAB_COOKIE_SECURE` | `false` | Set `true` behind HTTPS (adds `Secure` to the cookie) |
| `CHAOSLAB_DISCOVERY_ENABLED` | `true` | Read services from the cluster (`kubectl get deployments -A`); `false` forces the static list |
| `CHAOSLAB_LIVE_ENABLED` | `true` | `false` disables Live mode (recommended on shared/public deployments) |
| `CHAOSLAB_SIM_SPEED` | `1.0` | Simulation pacing multiplier (about 25 s per run at 1.0) |
| `CHAOSLAB_MAX_ACTIVE_PER_USER` | `3` | Concurrent experiments per user |
| `CHAOSLAB_ACCESS_LOG` | unset | Set to print HTTP access logs |

No secrets are needed or exposed. Passwords are hashed with scrypt; session tokens are random and only
their SHA-256 is stored.

## Architecture

```
static/ (SPA)  ->  http_core.py (router, cookies, CSRF origin check, headers)
                     routes_auth / routes_apps / routes_experiments / routes_reports / routes_dashboard
                     auth.py  uploads.py  catalog.py  reports.py  db.py (SQLite)
                     runner_service.py  --- ExecutionBackend contract (execution.py) ---
                                              |- live_backend.py  -> chaoslab/verifier/engine.py (Chaos Mesh, kubectl, probes, Jaeger, Oracle)
                                              `- sim_backend.py   -> predefined data -> the SAME VerificationOracle
```

### Reused from the existing ChaosLab project
`verifier/engine.py` (`verify_experiment`, progress callback, Chaos Mesh manifests, recovery tracking, probes,
telemetry), `verifier/oracle.py` (all 11 invariants and PASS/FAIL/SKIPPED/NOT_APPLICABLE semantics, used by *both*
modes), `verifier/reporter.py` (Live runs also expose the engine's native HTML report as `engine-html`),
the curated experiment YAMLs in `experiments/` (their application/telemetry probes are used for Live runs of
the matching service+fault), and the Online Boutique deployment as the demo application.

### Bring your own cluster: the ChaosLab agent
Users connect THEIR cluster instead of letting this server run workloads. Full guide: [`../agent/README.md`](../agent/README.md).

```
browser ──▶ web app (users, apps, jobs, Oracle, reports)  ◀── HTTPS, outbound only ──  agent  ──kubectl──▶ customer cluster
```
1. **Connect**: Clusters → name it → a one-time enrollment token (single use, 60 min, hashed at rest). Start the agent with it
   (`python agent.py --server URL --token cle_… --allowed-namespaces ns1,ns2`). The agent swaps it for a revocable credential and
   reports its cluster identity (kube-system UID, one account per cluster), allowed namespaces and the Deployments in them.
2. **Register targets explicitly**: an application = (cluster, namespace) and only for namespaces the agent allows. Services come from the
   agent's inventory (datastores etc. are not targetable). Nothing else can be targeted.
3. **Run**: Live experiments on such apps become a **job**. The agent long-polls, leases it (one active job per cluster), re-validates it
   against its own local policy, runs the existing engine, and streams events and samples. It returns raw **evidence**; the server evaluates
   it with its own Oracle (a differing agent verdict is noted, never trusted). Simulated mode works without the agent.
4. **Control**: per-cluster **Emergency stop** (cancels queued jobs, makes the agent delete the running fault within seconds, blocks new jobs
   until Resume; enqueue is refused while paused, closing the stop-vs-enqueue race), token regeneration, removal (history kept), and an
   append-only **audit log** (enrollment, registrations, job lifecycle, stops, rejected/mismatched credentials).

Agent API (Bearer auth, outbound from the agent): `POST /agent/v1/enroll`, `/agent/v1/poll` (heartbeat + inventory in, job/cancel/paused out),
`/agent/v1/jobs/{id}/events`, `/agent/v1/jobs/{id}/result`. Credentials: enrollment token `cle_…` (one-time) → agent credential `cla_…`
→ per-job token `clj_…` (valid only for that job until its lease expires). Enrollment attempts are throttled per IP.
Probes: `POST /api/applications/{id}/probes {service, probes|null}` stores validated declarative probes (HTTP GET health [required], functional GET, log substrings; same whitelist as the agent, with a parity test). They become the job's `application` block: the agent executes them (pre-flight before the fault, again after recovery) and the server's Oracle evaluates the app invariants from the evidence.
User API: `GET/POST /api/clusters`, `GET/DELETE /api/clusters/{id}`, `POST /api/clusters/{id}/{enrollment|emergency-stop|resume|applications}`.
Extra tables: `clusters`, `jobs`, `audit_log`; `applications.cluster_id`, `experiments.cluster_id`.
Env: `CHAOSLAB_PUBLIC_URL` (shown in install commands), `CHAOSLAB_AGENT_ONLINE_SECONDS` (45), `CHAOSLAB_AGENT_QUEUE_TIMEOUT_SECONDS` (120),
`CHAOSLAB_ENROLL_TTL_MINUTES` (60), `CHAOSLAB_MAX_CLUSTERS_PER_USER` (5). **Serve the app over HTTPS in production** (agents refuse plain
HTTP to non-local servers) and set `CHAOSLAB_COOKIE_SECURE=true`.

### Uploaded applications: validate, deploy, test (strict sandbox)
Upload Kubernetes manifests (a `.yaml`, or a `.zip` containing `.yaml` files; see `examples/hello-two-tier.yaml`).

1. **Validate (at upload, in memory)**: `manifests.py` accepts only `Deployment`, `Service` (ClusterIP) and `ConfigMap`, and only a
   whitelisted subset of their fields. It rejects privileged containers, host network/PID/paths, capabilities, Secrets, PVCs, RBAC,
   Ingress, NodePort/LoadBalancer, init containers, service accounts, any unknown field, YAML anchors/aliases, oversized or
   malformed files. Images must pin a tag (not `latest`) or digest and come from an allowed registry. Replicas, CPU and memory are capped.
   A missing `app` label is added automatically (ChaosLab targets pods by it). The sanitized result is stored in the DB:
   what is deployed is exactly what was validated. Status becomes `validated` or `invalid` (with every error listed).
2. **Deploy (button, background job)**: namespace `cl-app-<id>` (derived from the numeric id) with Pod Security `baseline`
   enforcement, ResourceQuota, LimitRange, a default-deny NetworkPolicy (in-namespace + DNS only), and workloads hardened at
   apply time (no service-account token, no service links, seccomp RuntimeDefault, no privilege escalation).
   It waits for readiness; on failure it reports pod reasons (e.g. `ImagePullBackOff`), cleans up and allows a retry.
3. **Test**: services are discovered from the sandbox namespace; Live experiments run the real engine against it; Simulated runs
   work as soon as the manifests validate. Live verification of uploaded apps covers the infrastructure invariants; application and
   telemetry invariants are `N/A` (no probes are known for arbitrary apps).
4. **Clean up**: Undeploy, deleting the app, or the TTL reaper (default 120 min) removes the namespace. Teardown only deletes a
   namespace carrying ChaosLab's managed label **and** the matching app id, and is refused while an experiment is running.

Limits (env vars in the table above): per-user and global deployed-app caps, deploy timeout, TTL, allowed registries, max replicas.
Verified on a real cluster: the privileged-pod attempt is rejected by Pod Security admission, in-namespace traffic works and
internet egress is blocked (NetworkPolicy enforcement depends on the cluster's CNI; kindnet enforces it).

### Storage backends (local or S3)
`storage.py` defines `put(key, data) -> ref` and an idempotent `delete(ref)`. Validation always runs first; only
validated bytes reach the backend. The database records which backend holds each file (`applications.storage_backend`)
and its reference (`file_path` = local path or S3 key), so switching backends never orphans or misroutes old files.

```bash
pip install boto3                      # only needed for S3
export CHAOSLAB_STORAGE=s3 CHAOSLAB_S3_BUCKET=my-bucket CHAOSLAB_S3_REGION=ap-south-1
python -m webapp                       # credentials: IAM role, AWS_* env vars or `aws configure` profile
```
Credentials are never configured in the app or sent to the browser. Recommended AWS setup: a **private** bucket with
Block Public Access on, default encryption, no public/website access, and an IAM policy for the app limited to
`s3:PutObject`, `s3:DeleteObject` on `arn:aws:s3:::BUCKET/uploads/*` and `s3:ListBucket`/`s3:GetBucketLocation`
on the bucket (the startup health check uses `head_bucket`). Objects are written with `Content-Type:
application/octet-stream` and SSE; the app has no endpoint that reads uploaded files back.
Behaviour: bad config (no bucket / no boto3) stops startup with a clear error; an unreachable bucket only warns at
startup and uploads return `503` without creating a record; if the S3 delete fails the record is kept (`502`) so the
delete can be retried.

### Built-in applications and service discovery
Three built-in applications are seeded (`catalog.BUILTIN_APPS`): Online Boutique (`default`), Learning Management
(`learning-platform`) and Project Management (`project-platform`). Their services are **discovered from the
cluster** with one cached (10 s) `kubectl get deployments -A -o json`: the `app` label is the target, with desired/ready
replicas shown. A small static table only adds metadata (language, protocol, description); services it does not know are
still listed with `unknown` metadata. Datastores, Jaeger and the load generator are listed but not targetable. If the
cluster or the namespace is unavailable the list falls back to the static table, the UI says so, and **Live execution is
refused** for that application (Simulated still works). Curated YAMLs are matched per namespace + service + fault, so
Live runs of e.g. `course-service` or `task-service` reuse their real HTTP health/functional probes.

### Live vs Simulated (kept logically separate)
* **Live**: writes the resolved experiment as a normal ChaosLab YAML, calls `engine.verify_experiment`, maps its
  progress callbacks to platform stages, and samples target pod readiness every 2 s (real observations). Failures
  (cluster down, baseline not ready, Chaos Mesh error) become a `failed` experiment with the real error and
  report verdict `NOT_EVALUATED` (never a silent pass). One live run at a time (shared cluster).
* **Simulated**: plays a predefined lifecycle (baseline → fault → impact → recovery → verification → Oracle) with
  predefined metrics; the simulated evidence is evaluated by the real Oracle. Every event, sample, invariant, UI
  panel, report banner and download filename is marked **SIMULATED**. Nothing is injected. One predefined scenario
  (`checkoutservice` + Network Failure) yields an Oracle **FAIL** so failures can be demonstrated.
* Both return the same result structure (`execution.build_result_common`), so a real engine can replace the
  simulation by registering another `ExecutionBackend`; the API and frontend do not change.

### Application uploads
Stored outside the DB through the storage backend (key `user_<id>/<uuid>.<ext>`), metadata in `applications`. Validation: extension
allow-list, size limit, content sniffing (ZIP/gzip/TAR structure, UTF-8 text, JSON parse), archive path-traversal
and link checks, executable/shebang rejection, sanitized display filename. Files are **never extracted or executed**,
and cannot be downloaded back through the API. Kubernetes manifest uploads can additionally be deployed (next section).

## Database schema

SQLite, created automatically on first start (`db.py`).

| Table | Columns |
| --- | --- |
| `users` | id, name, email (unique, NOCASE), password_hash, created_at |
| `sessions` | token_hash (PK), user_id → users, created_at, expires_at |
| `applications` | id, user_id → users (NULL = built-in), name, description, file_path, original_filename, file_size, upload_type, status, slug, created_at |
| `experiments` | id, user_id, application_id, target_service, fault_type, duration_seconds, config_json, mode (`live`/`simulated`), status (`pending`/`running`/`completed`/`failed`), current_stage, percent, error, oracle_verdict, recovery_seconds, result_json, created_at, started_at, completed_at |
| `experiment_events` | id, experiment_id, ts, stage, level, message, simulated, data (metric samples) |
| `reports` | id, experiment_id, user_id, format (`json`/`html`/`engine-html`), file_path, overall_result, created_at |

Status model: `completed` = the run finished and the Oracle produced a verdict (PASS or FAIL);
`failed` = execution error, no verdict. Interrupted runs are marked `failed` on server restart.

## API

All endpoints are JSON; authentication is the `chaoslab_session` HttpOnly cookie. State-changing requests require
`application/json` (or multipart for uploads) and a same-origin `Origin`. Other users' records return `404`.

| Method & path | Description |
| --- | --- |
| `POST /api/auth/register` `{name,email,password}` | Create account and session (201) |
| `POST /api/auth/login` `{email,password}` | Sign in (generic error, throttled) |
| `POST /api/auth/logout` · `GET /api/auth/me` | Sign out · current user (or `null`) |
| `GET /api/dashboard` | Totals, application list, recent experiments |
| `GET /api/applications` · `POST /api/applications` (multipart `name`, `description`, `file`) | List own + built-in · upload |
| `GET /api/applications/{id}` · `DELETE /api/applications/{id}` | Detail (+ catalog & history) · delete own upload |
| `GET /api/capabilities` | Whether Live / Simulated can run, with reasons |
| `POST /api/experiments` `{application_id,target_service,fault_type,duration_seconds,mode}` | Validate and start (201); `fault_type` ∈ `pod-kill`, `network-delay`, `network-failure`; duration 10–120 |
| `GET /api/experiments[?application_id=]` | Own experiments |
| `GET /api/experiments/{id}[?after_event=n]` | State, events, samples, result and Oracle (polled by the monitor) |
| `GET /api/experiments/{id}/report?format=json\|html\|engine-html[&download=1]` | View / download report |
| `POST /api/experiments/{id}/report` | Regenerate the report |
| `GET /api/reports` | Report history |
| `GET /api/oracle/invariants` | Invariant catalog (read from the real Oracle) and status meanings |

Error codes: `400` validation, `401` unauthenticated, `403` forbidden/cross-origin, `404` not found/not yours,
`409` live run already active, `413` too large, `415` bad type, `429` throttled/too many runs, `503` Live unavailable.

## Oracle availability by mode

Statuses stay distinct: **PASS**, **FAIL**, **SKIP** (configured but no evidence) and **N/A** (not applicable).
SKIP and N/A are never counted as PASS; the UI and reports state how many invariants actually executed.
Infrastructure invariants are always evaluated in Live mode. Application and telemetry invariants only run in Live
mode for the curated experiments that configure probes (e.g. `paymentservice` pod-kill: Jaeger required), and are
`N/A` otherwise. Simulated runs evaluate all groups on predefined evidence (still `N/A` for `target_impact` on network faults).

## Remaining limitations
* Customer clusters (agent): infrastructure + application invariants via declarative HTTP GET/log probes (no gRPC, no telemetry/Jaeger yet),
  one experiment per cluster, and evidence is only as trustworthy as the agent the customer runs. No agent auto-update,
  no mTLS/key pinning yet, and the control plane is a single process (SQLite + threads).
* Uploaded apps must be Kubernetes manifests (Deployment/Service/ConfigMap with registry images): no Secrets, volumes beyond
  emptyDir/ConfigMap, Ingress, StatefulSets, init containers, or building from source. Application/telemetry invariants are N/A for them.
* The sandbox shares the cluster's nodes and kernel (namespace + admission + quota + network policy isolation, not a VM boundary);
  do not expose deployment to untrusted public users without stronger isolation (gVisor/Kata, dedicated node pool).
* Live mode targets one shared cluster with a global one-run-at-a-time lock; any signed-in user may run Live
  faults against it (disable with `CHAOSLAB_LIVE_ENABLED=false` on shared deployments).
* Live network faults: the engine does not measure recovery (it reports a nominal 0.5 s after a fixed 5 s window), so
  recovery time is shown as "not measured" for them.
* Live latency time-series need Jaeger telemetry; otherwise only readiness is charted. The engine's network-fault path
  has a fixed 5 s observation window and a nominal recovery value (existing engine behaviour).
* Stdlib HTTP server, SQLite and in-memory login throttling suit a single instance, not horizontal scale.
* No password reset / e-mail verification / roles; no CSRF tokens beyond SameSite + Origin checks.

## Roadmap to a production platform
1. Extend uploads beyond manifests (Helm charts, image-only import, source builds), add user-supplied HTTP probes, then
   register them as additional `ExecutionBackend` targets.
2. Remote execution agents (outbound-connected) for customer clusters; job queue instead of worker threads.
3. Postgres + migrations, S3-style artifact storage, a real WSGI/ASGI server behind TLS, shared rate limiting.
4. Teams/roles, API tokens for CI quality gates (reuse `chaoslab/gate.py`), email verification and password reset.
5. Time-series from Prometheus/Jaeger for every Live run, scheduled experiments, trends across runs.
6. Replace the simulation with real execution wherever possible; keep it only as a sandbox demo mode.
