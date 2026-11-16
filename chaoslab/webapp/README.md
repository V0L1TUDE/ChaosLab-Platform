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
| `CHAOSLAB_UPLOAD_DIR` | `<data>/uploads` | Uploaded application files |
| `CHAOSLAB_REPORT_DIR` | `<data>/reports` | Generated reports |
| `CHAOSLAB_MAX_UPLOAD_MB` | `10` | Upload size limit |
| `CHAOSLAB_SESSION_TTL_SECONDS` | `604800` | Session lifetime |
| `CHAOSLAB_COOKIE_SECURE` | `false` | Set `true` behind HTTPS (adds `Secure` to the cookie) |
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
Stored outside the DB (`<uploads>/user_<id>/<uuid>.<ext>`), metadata in `applications`. Validation: extension
allow-list, size limit, content sniffing (ZIP/gzip/TAR structure, UTF-8 text, JSON parse), archive path-traversal
and link checks, executable/shebang rejection, sanitized display filename. Files are **never extracted or executed**,
and cannot be downloaded back through the API. Uploaded apps are registered only; experiments run on the built-in demo.

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
* Uploaded applications are not deployed or executed; only the built-in Online Boutique runs.
* Live mode targets one shared cluster with a global one-run-at-a-time lock; any signed-in user may run Live
  faults against it (disable with `CHAOSLAB_LIVE_ENABLED=false` on shared deployments).
* Live latency time-series need Jaeger telemetry; otherwise only readiness is charted. The engine's network-fault path
  has a fixed 5 s observation window and a nominal recovery value (existing engine behaviour).
* Stdlib HTTP server, SQLite and in-memory login throttling suit a single instance, not horizontal scale.
* No password reset / e-mail verification / roles; no CSRF tokens beyond SameSite + Origin checks.

## Roadmap to a production platform
1. Deploy uploaded apps into per-user namespaces (manifest/Helm/container import) with resource quotas, then
   register them as additional `ExecutionBackend` targets.
2. Remote execution agents (outbound-connected) for customer clusters; job queue instead of worker threads.
3. Postgres + migrations, S3-style artifact storage, a real WSGI/ASGI server behind TLS, shared rate limiting.
4. Teams/roles, API tokens for CI quality gates (reuse `chaoslab/gate.py`), email verification and password reset.
5. Time-series from Prometheus/Jaeger for every Live run, scheduled experiments, trends across runs.
6. Replace the simulation with real execution wherever possible; keep it only as a sandbox demo mode.
