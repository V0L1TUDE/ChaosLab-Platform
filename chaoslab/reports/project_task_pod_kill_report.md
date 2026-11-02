# ChaosLab Experiment Report: `project-task-pod-kill`

> **Verdict**: ✅ **PASS** | **Application**: `Project Management Platform` | **Target**: `task-service` | **Fault**: `pod-kill` | **Recovery Time**: `7.42s`

## Executive Summary

| Property | Value |
| :--- | :--- |
| **Experiment** | `project-task-pod-kill` |
| **Application Group** | **Project Management Platform** |
| **Target Microservice** | `task-service` (namespace: `project-platform`) |
| **Fault Injected** | `pod-kill` |
| **Baseline Pod** | `task-service-847574b4d6-6sktv` (1/1) |
| **Replacement Pod** | `task-service-847574b4d6-nrvd7` |
| **Recovery Time** | `7.42s` (Threshold: `60s`) |
| **Final Verdict** | **PASS** (10/10 executed invariants satisfied) |

## Recovery Timeline Progression

| Offset | Time (UTC) | Milestone | Details |
| :---: | :---: | :--- | :--- |
| `+0.0s` | `03:55:44.539` | **Experiment Initialized** | Baseline established for task-service |
| `+0.54s` | `03:55:45.082` | **Fault Injected** | Chaos Mesh fault injected (pod-kill) |
| `+0.7s` | `03:55:45.239` | **Target Impact Detected** | Original pod task-service-847574b4d6-6sktv terminated/impacted |
| `+8.12s` | `03:55:52.656` | **Replacement Ready** | Service recovered (task-service-847574b4d6-nrvd7) |
| `+14.4s` | `03:55:58.936` | **Experiment Completed** | All verification rules evaluated by Oracle |

## Verification Oracle Scorecard

| Invariant Rule | Status | Observed | Expected | Details |
| :--- | :---: | :--- | :--- | :--- |
| `baseline_healthy` | **PASS** | `1` | `>=1` | Target met baseline readiness (1 ready, expected >= 1). |
| `fault_injected` | **PASS** | `Injected` | `Injected` | Chaos Mesh confirmed fault injection (AllInjected / Apply Succeeded). |
| `target_impact` | **PASS** | `True` | `True` | Target pod disappearance confirmed (task-service-847574b4d6-6sktv). |
| `service_recovery` | **PASS** | `1` | `>=1` | Service recovered 1 ready replica(s) (replacement: task-service-847574b4d6-nrvd7). |
| `recovery_within_timeout` | **PASS** | `7.42s` | `<=60.0s` | Service recovered in 7.42s (within limit of 60.0s). |
| `app_health_probe` | **PASS** | `200 OK` | `SERVING/200 OK` | Application health probe (HTTP) verified: status 200 OK. |
| `app_functional_check` | **PASS** | `task-test-probe` | `CONFIRMED_OK` | Functional transaction verified successfully (Entity ID: task-test-probe). |
| `app_logs_healthy` | **PASS** | `startup=True, errors=0` | `startup=True, errors=0` | Container logs confirmed clean startup (found 'Uvicorn running', 0 fatal errors). |
| `telemetry_traces_recorded` | **PASS** | `1` | `>0` | Distributed tracing active: 1 traces (3 spans) captured for 'task-service'. |
| `telemetry_error_spike_detected` | **NOT_APPLICABLE** | `N/A` | `N/A` | Error spike verification skipped (optional check not required in config). |
| `telemetry_latency_recovered` | **PASS** | `10.40ms` | `<=1000.0ms` | Latency SLA verified: p95 latency 10.40ms within threshold (<=1000.0ms). |

## Application-Level Correctness Verification

- **Health Probe (HTTP)**: `200 OK`
- **Functional Transaction Confirmation**: Processed successfully (`Entity ID: task-test-probe`)
- **Container Startup Logs**: `Clean initialization detected` (0 fatal exceptions)

## Distributed Tracing & Telemetry (OpenTelemetry / Jaeger)

- **Target Service**: `task-service`
- **Traces Captured**: `1` (3 spans)
- **Error Spans**: `0` (0.0% error rate)
- **Latency Distribution**: min `0.02ms` | avg `3.87ms` | **p50** `0.05ms` | **p95** `10.4ms` | max `11.55ms`
- **Sample Trace IDs**: `76a8b3f04ed22b5cfd00aef3a5080c6b`
- **Operations Monitored**: `GET /health`, `GET /health http send`

## Kubernetes Cluster Audit Events

| Type | Reason | Object | Message |
| :---: | :--- | :--- | :--- |
| `Normal` | **Created** | `pod/task-service-847574b4d6-nrvd7` | Container created |
| `Normal` | **Started** | `pod/task-service-847574b4d6-nrvd7` | Container started |
| `Normal` | **SuccessfulCreate** | `replicaset/task-service-847574b4d6` | Created pod: task-service-847574b4d6-8dttd |
| `Normal` | **SuccessfulCreate** | `replicaset/task-service-847574b4d6` | Created pod: task-service-847574b4d6-6sktv |
| `Normal` | **SuccessfulCreate** | `replicaset/task-service-847574b4d6` | Created pod: task-service-847574b4d6-nrvd7 |
| `Normal` | **ScalingReplicaSet** | `deployment/task-service` | Scaled up replica set task-service-847574b4d6 from 0 to 1 |
