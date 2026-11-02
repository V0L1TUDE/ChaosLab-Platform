# ChaosLab Experiment Report: `project-task-network-delay`

> **Verdict**: ✅ **PASS** | **Application**: `Project Management Platform` | **Target**: `task-service` | **Fault**: `network-delay` | **Recovery Time**: `0.5s`

## Executive Summary

| Property | Value |
| :--- | :--- |
| **Experiment** | `project-task-network-delay` |
| **Application Group** | **Project Management Platform** |
| **Target Microservice** | `task-service` (namespace: `project-platform`) |
| **Fault Injected** | `network-delay` |
| **Baseline Pod** | `task-service-847574b4d6-6sktv` (1/1) |
| **Replacement Pod** | `task-service-847574b4d6-6sktv` |
| **Recovery Time** | `0.5s` (Threshold: `60s`) |
| **Final Verdict** | **PASS** (8/8 executed invariants satisfied) |

## Recovery Timeline Progression

| Offset | Time (UTC) | Milestone | Details |
| :---: | :---: | :--- | :--- |
| `+0.0s` | `03:55:31.579` | **Experiment Initialized** | Baseline established for task-service |
| `+0.49s` | `03:55:32.072` | **Fault Injected** | Chaos Mesh fault injected (network-delay) |
| `+5.49s` | `03:55:37.073` | **Replacement Ready** | Service recovered (task-service-847574b4d6-6sktv) |
| `+12.83s` | `03:55:44.412` | **Experiment Completed** | All verification rules evaluated by Oracle |

## Verification Oracle Scorecard

| Invariant Rule | Status | Observed | Expected | Details |
| :--- | :---: | :--- | :--- | :--- |
| `baseline_healthy` | **PASS** | `1` | `>=1` | Target met baseline readiness (1 ready, expected >= 1). |
| `fault_injected` | **PASS** | `Injected` | `Injected` | Chaos Mesh confirmed fault injection (AllInjected / Apply Succeeded). |
| `target_impact` | **NOT_APPLICABLE** | `N/A` | `N/A` | Pod termination not applicable for network fault 'network-delay'. |
| `service_recovery` | **PASS** | `1` | `>=1` | Service recovered 1 ready replica(s) (replacement: task-service-847574b4d6-6sktv). |
| `recovery_within_timeout` | **PASS** | `0.50s` | `<=60.0s` | Service recovered in 0.50s (within limit of 60.0s). |
| `app_health_probe` | **PASS** | `200 OK` | `SERVING/200 OK` | Application health probe (HTTP) verified: status 200 OK. |
| `app_functional_check` | **PASS** | `task-test-probe` | `CONFIRMED_OK` | Functional transaction verified successfully (Entity ID: task-test-probe). |
| `app_logs_healthy` | **NOT_APPLICABLE** | `N/A` | `N/A` | Container logs verification skipped (not enabled in config). |
| `telemetry_traces_recorded` | **PASS** | `3` | `>0` | Distributed tracing active: 3 traces (18 spans) captured for 'task-service'. |
| `telemetry_error_spike_detected` | **NOT_APPLICABLE** | `N/A` | `N/A` | Error spike verification skipped (optional check not required in config). |
| `telemetry_latency_recovered` | **PASS** | `706.55ms` | `<=1500.0ms` | Latency SLA verified: p95 latency 706.55ms within threshold (<=1500.0ms). |

## Application-Level Correctness Verification

- **Health Probe (HTTP)**: `200 OK`
- **Functional Transaction Confirmation**: Processed successfully (`Entity ID: task-test-probe`)
- **Container Startup Logs**: `Not detected` (0 fatal exceptions)

## Distributed Tracing & Telemetry (OpenTelemetry / Jaeger)

- **Target Service**: `task-service`
- **Traces Captured**: `3` (18 spans)
- **Error Spans**: `0` (0.0% error rate)
- **Latency Distribution**: min `0.01ms` | avg `137.2ms` | **p50** `0.04ms` | **p95** `706.55ms` | max `1227.51ms`
- **Sample Trace IDs**: `eae0fd4f826489edc220714c5ecefb08`, `3efd48687a39ec4feeee9e73beddd90e`, `84b56b7c471a53bf2532fb9b977fb037`
- **Operations Monitored**: `GET`, `GET /health`, `GET /health http send`, `GET /projects/{project_id}`, `GET /projects/{project_id} http send`

## Kubernetes Cluster Audit Events

| Type | Reason | Object | Message |
| :---: | :--- | :--- | :--- |
| `Normal` | **Started** | `pod/task-service-847574b4d6-8dttd` | Container started |
| `Warning` | **Unhealthy** | `pod/task-service-847574b4d6-8dttd` | Readiness probe failed: Get "http://10.244.0.36:8003/health": dial tcp 10.244.0.36:8003: connect: connection refused |
| `Normal` | **Killing** | `pod/task-service-847574b4d6-8dttd` | Stopping container task-service |
| `Normal` | **SuccessfulCreate** | `replicaset/task-service-847574b4d6` | Created pod: task-service-847574b4d6-8dttd |
| `Normal` | **SuccessfulCreate** | `replicaset/task-service-847574b4d6` | Created pod: task-service-847574b4d6-6sktv |
| `Normal` | **ScalingReplicaSet** | `deployment/task-service` | Scaled up replica set task-service-847574b4d6 from 0 to 1 |
