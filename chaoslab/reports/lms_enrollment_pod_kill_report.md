# ChaosLab Experiment Report: `lms-enrollment-pod-kill`

> **Verdict**: ✅ **PASS** | **Application**: `Learning Management System (LMS)` | **Target**: `enrollment-service` | **Fault**: `pod-kill` | **Recovery Time**: `7.58s`

## Executive Summary

| Property | Value |
| :--- | :--- |
| **Experiment** | `lms-enrollment-pod-kill` |
| **Application Group** | **Learning Management System (LMS)** |
| **Target Microservice** | `enrollment-service` (namespace: `learning-platform`) |
| **Fault Injected** | `pod-kill` |
| **Baseline Pod** | `enrollment-service-5899bc9774-h626w` (1/1) |
| **Replacement Pod** | `enrollment-service-5899bc9774-5xmjx` |
| **Recovery Time** | `7.58s` (Threshold: `60s`) |
| **Final Verdict** | **PASS** (10/10 executed invariants satisfied) |

## Recovery Timeline Progression

| Offset | Time (UTC) | Milestone | Details |
| :---: | :---: | :--- | :--- |
| `+0.0s` | `03:53:52.731` | **Experiment Initialized** | Baseline established for enrollment-service |
| `+0.53s` | `03:53:53.260` | **Fault Injected** | Chaos Mesh fault injected (pod-kill) |
| `+0.72s` | `03:53:53.449` | **Target Impact Detected** | Original pod enrollment-service-5899bc9774-h626w terminated/impacted |
| `+8.3s` | `03:54:01.027` | **Replacement Ready** | Service recovered (enrollment-service-5899bc9774-5xmjx) |
| `+14.59s` | `03:54:07.320` | **Experiment Completed** | All verification rules evaluated by Oracle |

## Verification Oracle Scorecard

| Invariant Rule | Status | Observed | Expected | Details |
| :--- | :---: | :--- | :--- | :--- |
| `baseline_healthy` | **PASS** | `1` | `>=1` | Target met baseline readiness (1 ready, expected >= 1). |
| `fault_injected` | **PASS** | `Injected` | `Injected` | Chaos Mesh confirmed fault injection (AllInjected / Apply Succeeded). |
| `target_impact` | **PASS** | `True` | `True` | Target pod disappearance confirmed (enrollment-service-5899bc9774-h626w). |
| `service_recovery` | **PASS** | `1` | `>=1` | Service recovered 1 ready replica(s) (replacement: enrollment-service-5899bc9774-5xmjx). |
| `recovery_within_timeout` | **PASS** | `7.58s` | `<=60.0s` | Service recovered in 7.58s (within limit of 60.0s). |
| `app_health_probe` | **PASS** | `200 OK` | `SERVING/200 OK` | Application health probe (HTTP) verified: status 200 OK. |
| `app_functional_check` | **PASS** | `enr-test-probe` | `CONFIRMED_OK` | Functional transaction verified successfully (Entity ID: enr-test-probe). |
| `app_logs_healthy` | **PASS** | `startup=True, errors=0` | `startup=True, errors=0` | Container logs confirmed clean startup (found 'Uvicorn running', 0 fatal errors). |
| `telemetry_traces_recorded` | **PASS** | `1` | `>0` | Distributed tracing active: 1 traces (3 spans) captured for 'enrollment-service'. |
| `telemetry_error_spike_detected` | **NOT_APPLICABLE** | `N/A` | `N/A` | Error spike verification skipped (optional check not required in config). |
| `telemetry_latency_recovered` | **PASS** | `12.63ms` | `<=1000.0ms` | Latency SLA verified: p95 latency 12.63ms within threshold (<=1000.0ms). |

## Application-Level Correctness Verification

- **Health Probe (HTTP)**: `200 OK`
- **Functional Transaction Confirmation**: Processed successfully (`Entity ID: enr-test-probe`)
- **Container Startup Logs**: `Clean initialization detected` (0 fatal exceptions)

## Distributed Tracing & Telemetry (OpenTelemetry / Jaeger)

- **Target Service**: `enrollment-service`
- **Traces Captured**: `1` (3 spans)
- **Error Spans**: `0` (0.0% error rate)
- **Latency Distribution**: min `0.01ms` | avg `4.7ms` | **p50** `0.05ms` | **p95** `12.63ms` | max `14.03ms`
- **Sample Trace IDs**: `aa5aa5c701569b81f424566fd20a8851`
- **Operations Monitored**: `GET /health`, `GET /health http send`

## Kubernetes Cluster Audit Events

| Type | Reason | Object | Message |
| :---: | :--- | :--- | :--- |
| `Normal` | **Started** | `pod/enrollment-service-5899bc9774-h626w` | Container started |
| `Warning` | **Unhealthy** | `pod/enrollment-service-5899bc9774-h626w` | Readiness probe failed: Get "http://10.244.0.40:8013/health": dial tcp 10.244.0.40:8013: connect: connection refused |
| `Normal` | **Killing** | `pod/enrollment-service-5899bc9774-h626w` | Stopping container enrollment-service |
| `Normal` | **SuccessfulCreate** | `replicaset/enrollment-service-5899bc9774` | Created pod: enrollment-service-5899bc9774-h626w |
| `Normal` | **SuccessfulCreate** | `replicaset/enrollment-service-5899bc9774` | Created pod: enrollment-service-5899bc9774-5xmjx |
| `Normal` | **ScalingReplicaSet** | `deployment/enrollment-service` | Scaled up replica set enrollment-service-5899bc9774 from 0 to 1 |
