# ChaosLab — Hosted Multi-Tenant Resilience Testing Platform

## Overview

ChaosLab has been extended into a **hosted multi-tenant Resilience Testing Platform (SaaS)** implementing **Option A: Bring Your Own Cluster (BYOC)**. 

The platform enables distributed engineering teams to continuously test, verify, and monitor the fault tolerance of their microservices against real-world infrastructure failures directly from a hosted dashboard.

```
┌────────────────────────────────────────────────────────┐
│              ChaosLab Hosted Web Dashboard             │
│    (Registration, App Hub, Cluster Hub, Scorecards)    │
└───────────────────────────┬────────────────────────────┘
                            │ HTTPS / JWT
                            ▼
┌────────────────────────────────────────────────────────┐
│             ChaosLab SaaS Control Plane                │
│    (FastAPI / Database / Job Scheduler / Audit Log)    │
└───────────────────────────┬────────────────────────────┘
                            │ Outbound HTTPS (Polling & Heartbeat)
                            ▼
┌────────────────────────────────────────────────────────┐
│             ChaosLab Kubernetes Agent (BYOC)           │
│     (Outbound Daemon, Chaos Mesh, Oracle Engine)       │
└────────────────────────────────────────────────────────┘
```

---

## 1. Key Platform Features

1. **Multi-Tenant SaaS Control Plane**:
   - Secure registration, login, and salted PBKDF2-HMAC-SHA256 password hashing.
   - RFC 7519 compliant JSON Web Token (JWT) session and API authentication.
   - Strict tenant isolation: organizations can only manage and inspect their own resources.
   - Role-Based Access Control (Admin, Developer, Viewer).
   - Platform audit log tracking all events and destructive actions.

2. **Bring Your Own Cluster (BYOC)**:
   - Connect any Kubernetes cluster (EKS, GKE, AKS, KinD, Minikube).
   - **Outbound-only agent architecture**: requires zero open inbound firewall ports, zero ingresses, and zero external load balancers.
   - Cryptographic, single-use cluster enrollment tokens.

3. **Universal Verification Oracle**:
   - Integrates with ChaosLab's deterministic Oracle engine.
   - Evaluates 10+ invariants across target impact, replacement readiness, recovery SLA, application health probes, functional transactions, and OpenTelemetry trace spikes.
   - Never fabricates results: genuine verification data with measured recovery durations.

4. **Hosted Web Dashboard**:
   - Modern dark-mode single-page application (SPA).
   - Live execution progress tracking and terminal log stream.
   - Interactive Oracle Scorecards and downloadable report artifacts (JSON, HTML, Markdown).

5. **Strict Local Mode Preservation**:
   - The CLI runner (`python chaoslab/runner.py`) remains 100% operational for local development and offline runs.

---

## 2. Quickstart Guide

### Option 1: Running the SaaS Platform Locally

1. **Activate the Virtual Environment**:
   ```bash
   cd ~/ChaosLab-Workspace/ChaosLab-Platform
   source .venv/bin/activate
   ```

2. **Launch the SaaS Control Plane & Dashboard**:
   ```bash
   python chaoslab/platform/backend/server.py 8000
   ```
   - Control Plane API: `http://localhost:8000/api/v1/health`
   - Hosted Web Dashboard: `http://localhost:8000/`

3. **Register and Connect a Cluster**:
   - Open `http://localhost:8000/` in your browser.
   - Create an account or sign in.
   - Navigate to **Clusters** and click **"+ Connect Cluster"**.
   - Copy the generated enrollment token (e.g. `cl-enroll-xxxxx`).

4. **Run the Cluster Agent**:
   ```bash
   python chaoslab/platform/agent/agent.py --server http://localhost:8000 --token cl-enroll-xxxxx
   ```
   The agent enrolls the cluster, starts transmitting periodic heartbeats, and polls for resilience experiment jobs.

5. **Launch a Resilience Experiment**:
   - In the dashboard, navigate to **Launch Experiment**.
   - Select your application, target cluster, and experiment template (e.g. `payment-service-pod-kill`).
   - Click **Dispatch Experiment to Cluster**.
   - Watch the live execution tracker and inspect the resulting Oracle Scorecard.

---

## 3. Deployment with Docker & Docker Compose

### Self-Hosted SaaS Stack via Docker Compose
```bash
docker compose -f docker-compose.platform.yaml up -d
```
This launches the ChaosLab Control Plane with persistent volumes for database state and report artifacts on port `8000`.

### Deploying the Cluster Agent into Kubernetes
```bash
# 1. Apply Least-Privilege RBAC
kubectl apply -f chaoslab/platform/agent/k8s/agent-rbac.yaml

# 2. Deploy Agent Daemon
kubectl apply -f chaoslab/platform/agent/k8s/agent-deployment.yaml
```

---

## 4. Automated Test Validation

The platform includes full automated test suites across all stages:

```bash
# 1. SaaS Backend & Multi-Tenant Authentication Tests
python tests/test_saas_backend.py

# 2. Kubernetes Cluster Agent & Execution Tests
python tests/test_agent_execution.py

# 3. Results Ingestion, Reports & Multi-Tenancy Tests
python tests/test_results_and_multitenancy.py

# 4. Complete 10-Step End-to-End Validation Journey
python tests/test_e2e_platform.py
```

---

## 5. Security & RBAC Architecture

- **Least-Privilege RBAC**: The agent ServiceAccount has zero cluster-admin privileges and cannot access secrets outside `chaoslab-system`.
- **No Arbitrary Code Execution**: The agent only processes schema-validated declarative YAML experiment manifests (`PodChaos`, `NetworkChaos`). Raw shell commands are rejected.
- **Cryptographic Single-Use Tokens**: Cluster enrollment tokens expire after 15 minutes and can only be consumed once.
- **Audit Logging**: Every disruptive chaos action, application update, and report download is permanently recorded in the `audit_logs` table.

---

## 6. Future Option B Roadmap: Fully Managed Testing Infrastructure

While Option A (BYOC) allows teams to test their private Kubernetes clusters securely, the future **Option B** roadmap introduces:
- **Managed Ephemeral Test Clusters**: ChaosLab dynamically provisions isolated cloud test clusters on demand (via GKE/EKS APIs).
- **Automated Synthetic Workload Injection**: Pre-configured e-commerce and microservice topologies spun up for automated benchmark runs.
- **Automated Pull Request Testing**: Integration with GitHub Actions and GitLab CI to spin up an ephemeral cluster per PR, run resilience gates, and tear down automatically.
