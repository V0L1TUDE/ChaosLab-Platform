# ChaosLab Experiment Report: `project-service-pod-kill`

> **Verdict**: ✅ **PASS** | **Application**: `Project Management Platform` | **Target**: `project-service` | **Fault**: `pod-kill` | **Recovery Time**: `6.26s`

## Executive Summary

| Property | Value |
| :--- | :--- |
| **Experiment** | `project-service-pod-kill` |
| **Application Group** | **Project Management Platform** |
| **Target Microservice** | `project-service` (namespace: `project-platform`) |
| **Fault Injected** | `pod-kill` |
| **Baseline Pod** | `project-service-58ddf5f844-s9jjr` (1/1) |
| **Replacement Pod** | `project-service-58ddf5f844-5rql8` |
| **Recovery Time** | `6.26s` (Threshold: `60s`) |
| **Final Verdict** | **PASS** (10/10 executed invariants satisfied) |

## Recovery Timeline Progression

| Offset | Time (UTC) | Milestone | Details |
| :---: | :---: | :--- | :--- |
| `+0.0s` | `03:55:18.382` | **Experiment Initialized** | Baseline established for project-service |
| `+0.49s` | `03:55:18.869` | **Fault Injected** | Chaos Mesh fault injected (pod-kill) |
| `+0.64s` | `03:55:19.026` | **Target Impact Detected** | Original pod project-service-58ddf5f844-s9jjr terminated/impacted |
| `+6.9s` | `03:55:25.284` | **Replacement Ready** | Service recovered (project-service-58ddf5f844-5rql8) |
| `+13.08s` | `03:55:31.464` | **Experiment Completed** | All verification rules evaluated by Oracle |

## Verification Oracle Scorecard

| Invariant Rule | Status | Observed | Expected | Details |
| :--- | :---: | :--- | :--- | :--- |
| `baseline_healthy` | **PASS** | `1` | `>=1` | Target met baseline readiness (1 ready, expected >= 1). |
| `fault_injected` | **PASS** | `Injected` | `Injected` | Chaos Mesh confirmed fault injection (AllInjected / Apply Succeeded). |
| `target_impact` | **PASS** | `True` | `True` | Target pod disappearance confirmed (project-service-58ddf5f844-s9jjr). |
| `service_recovery` | **PASS** | `1` | `>=1` | Service recovered 1 ready replica(s) (replacement: project-service-58ddf5f844-5rql8). |
| `recovery_within_timeout` | **PASS** | `6.26s` | `<=60.0s` | Service recovered in 6.26s (within limit of 60.0s). |
| `app_health_probe` | **PASS** | `200 OK` | `SERVING/200 OK` | Application health probe (HTTP) verified: status 200 OK. |
| `app_functional_check` | **PASS** | `proj-101` | `CONFIRMED_OK` | Functional transaction verified successfully (Entity ID: proj-101). |
| `app_logs_healthy` | **PASS** | `startup=True, errors=0` | `startup=True, errors=0` | Container logs confirmed clean startup (found 'Uvicorn running', 0 fatal errors). |
| `telemetry_traces_recorded` | **PASS** | `1` | `>0` | Distributed tracing active: 1 traces (3 spans) captured for 'project-service'. |
| `telemetry_error_spike_detected` | **NOT_APPLICABLE** | `N/A` | `N/A` | Error spike verification skipped (optional check not required in config). |
| `telemetry_latency_recovered` | **PASS** | `13.36ms` | `<=1000.0ms` | Latency SLA verified: p95 latency 13.36ms within threshold (<=1000.0ms). |

## Application-Level Correctness Verification

- **Health Probe (HTTP)**: `200 OK`
- **Functional Transaction Confirmation**: Processed successfully (`Entity ID: proj-101`)
- **Container Startup Logs**: `Clean initialization detected` (0 fatal exceptions)

## Distributed Tracing & Telemetry (OpenTelemetry / Jaeger)

- **Target Service**: `project-service`
- **Traces Captured**: `1` (3 spans)
- **Error Spans**: `0` (0.0% error rate)
- **Latency Distribution**: min `0.02ms` | avg `4.98ms` | **p50** `0.07ms` | **p95** `13.36ms` | max `14.83ms`
- **Sample Trace IDs**: `3b4b44b92e1e714d2a3cf1274e868992`
- **Operations Monitored**: `GET /health`, `GET /health http send`

## Kubernetes Cluster Audit Events

| Type | Reason | Object | Message |
| :---: | :--- | :--- | :--- |
| `Normal` | **Started** | `pod/project-service-58ddf5f844-s9jjr` | Container started |
| `Warning` | **Unhealthy** | `pod/project-service-58ddf5f844-s9jjr` | Readiness probe failed: Get "http://10.244.0.35:8002/health": dial tcp 10.244.0.35:8002: connect: connection refused |
| `Normal` | **Killing** | `pod/project-service-58ddf5f844-s9jjr` | Stopping container project-service |
| `Normal` | **SuccessfulCreate** | `replicaset/project-service-58ddf5f844` | Created pod: project-service-58ddf5f844-s9jjr |
| `Normal` | **SuccessfulCreate** | `replicaset/project-service-58ddf5f844` | Created pod: project-service-58ddf5f844-5rql8 |
| `Normal` | **ScalingReplicaSet** | `deployment/project-service` | Scaled up replica set project-service-58ddf5f844 from 0 to 1 |
