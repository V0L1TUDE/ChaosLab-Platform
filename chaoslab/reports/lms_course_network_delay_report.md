# ChaosLab Experiment Report: `lms-course-network-delay`

> **Verdict**: ✅ **PASS** | **Application**: `Learning Management System (LMS)` | **Target**: `course-service` | **Fault**: `network-delay` | **Recovery Time**: `0.5s`

## Executive Summary

| Property | Value |
| :--- | :--- |
| **Experiment** | `lms-course-network-delay` |
| **Application Group** | **Learning Management System (LMS)** |
| **Target Microservice** | `course-service` (namespace: `learning-platform`) |
| **Fault Injected** | `network-delay` |
| **Baseline Pod** | `course-service-5dbf4b5855-jrwdx` (1/1) |
| **Replacement Pod** | `course-service-5dbf4b5855-jrwdx` |
| **Recovery Time** | `0.5s` (Threshold: `60s`) |
| **Final Verdict** | **PASS** (8/8 executed invariants satisfied) |

## Recovery Timeline Progression

| Offset | Time (UTC) | Milestone | Details |
| :---: | :---: | :--- | :--- |
| `+0.0s` | `03:53:26.429` | **Experiment Initialized** | Baseline established for course-service |
| `+0.44s` | `03:53:26.873` | **Fault Injected** | Chaos Mesh fault injected (network-delay) |
| `+5.44s` | `03:53:31.873` | **Replacement Ready** | Service recovered (course-service-5dbf4b5855-jrwdx) |
| `+11.54s` | `03:53:37.974` | **Experiment Completed** | All verification rules evaluated by Oracle |

## Verification Oracle Scorecard

| Invariant Rule | Status | Observed | Expected | Details |
| :--- | :---: | :--- | :--- | :--- |
| `baseline_healthy` | **PASS** | `1` | `>=1` | Target met baseline readiness (1 ready, expected >= 1). |
| `fault_injected` | **PASS** | `Injected` | `Injected` | Chaos Mesh confirmed fault injection (AllInjected / Apply Succeeded). |
| `target_impact` | **NOT_APPLICABLE** | `N/A` | `N/A` | Pod termination not applicable for network fault 'network-delay'. |
| `service_recovery` | **PASS** | `1` | `>=1` | Service recovered 1 ready replica(s) (replacement: course-service-5dbf4b5855-jrwdx). |
| `recovery_within_timeout` | **PASS** | `0.50s` | `<=60.0s` | Service recovered in 0.50s (within limit of 60.0s). |
| `app_health_probe` | **PASS** | `200 OK` | `SERVING/200 OK` | Application health probe (HTTP) verified: status 200 OK. |
| `app_functional_check` | **PASS** | `crs-101` | `CONFIRMED_OK` | Functional transaction verified successfully (Entity ID: crs-101). |
| `app_logs_healthy` | **NOT_APPLICABLE** | `N/A` | `N/A` | Container logs verification skipped (not enabled in config). |
| `telemetry_traces_recorded` | **PASS** | `3` | `>0` | Distributed tracing active: 3 traces (9 spans) captured for 'course-service'. |
| `telemetry_error_spike_detected` | **NOT_APPLICABLE** | `N/A` | `N/A` | Error spike verification skipped (optional check not required in config). |
| `telemetry_latency_recovered` | **PASS** | `1.18ms` | `<=1500.0ms` | Latency SLA verified: p95 latency 1.18ms within threshold (<=1500.0ms). |

## Application-Level Correctness Verification

- **Health Probe (HTTP)**: `200 OK`
- **Functional Transaction Confirmation**: Processed successfully (`Entity ID: crs-101`)
- **Container Startup Logs**: `Not detected` (0 fatal exceptions)

## Distributed Tracing & Telemetry (OpenTelemetry / Jaeger)

- **Target Service**: `course-service`
- **Traces Captured**: `3` (9 spans)
- **Error Spans**: `0` (0.0% error rate)
- **Latency Distribution**: min `0.01ms` | avg `0.37ms` | **p50** `0.03ms` | **p95** `1.18ms` | max `1.28ms`
- **Sample Trace IDs**: `b7a6bb8a8d9021eb8143655915e39327`, `520b685d02715b39ed3e71451bd25883`, `65c9762eca0384610ec23399d99a040e`
- **Operations Monitored**: `GET /health`, `GET /health http send`

## Kubernetes Cluster Audit Events

| Type | Reason | Object | Message |
| :---: | :--- | :--- | :--- |
| `Normal` | **Started** | `pod/course-service-5dbf4b5855-txfkh` | Container started |
| `Normal` | **Killing** | `pod/course-service-5dbf4b5855-txfkh` | Stopping container course-service |
| `Normal` | **SuccessfulCreate** | `replicaset/course-service-5dbf4b5855` | Created pod: course-service-5dbf4b5855-txfkh |
| `Normal` | **SuccessfulCreate** | `replicaset/course-service-5dbf4b5855` | Created pod: course-service-5dbf4b5855-nhw7l |
| `Normal` | **SuccessfulCreate** | `replicaset/course-service-5dbf4b5855` | Created pod: course-service-5dbf4b5855-jrwdx |
| `Normal` | **ScalingReplicaSet** | `deployment/course-service` | Scaled up replica set course-service-5dbf4b5855 from 0 to 1 |
