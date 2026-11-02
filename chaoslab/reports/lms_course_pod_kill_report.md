# ChaosLab Experiment Report: `lms-course-pod-kill`

> **Verdict**: ✅ **PASS** | **Application**: `Learning Management System (LMS)` | **Target**: `course-service` | **Fault**: `pod-kill` | **Recovery Time**: `7.43s`

## Executive Summary

| Property | Value |
| :--- | :--- |
| **Experiment** | `lms-course-pod-kill` |
| **Application Group** | **Learning Management System (LMS)** |
| **Target Microservice** | `course-service` (namespace: `learning-platform`) |
| **Fault Injected** | `pod-kill` |
| **Baseline Pod** | `course-service-5dbf4b5855-jrwdx` (1/1) |
| **Replacement Pod** | `course-service-5dbf4b5855-2dhjx` |
| **Recovery Time** | `7.43s` (Threshold: `60s`) |
| **Final Verdict** | **PASS** (10/10 executed invariants satisfied) |

## Recovery Timeline Progression

| Offset | Time (UTC) | Milestone | Details |
| :---: | :---: | :--- | :--- |
| `+0.0s` | `03:53:38.116` | **Experiment Initialized** | Baseline established for course-service |
| `+0.52s` | `03:53:38.636` | **Fault Injected** | Chaos Mesh fault injected (pod-kill) |
| `+0.67s` | `03:53:38.789` | **Target Impact Detected** | Original pod course-service-5dbf4b5855-jrwdx terminated/impacted |
| `+8.11s` | `03:53:46.222` | **Replacement Ready** | Service recovered (course-service-5dbf4b5855-2dhjx) |
| `+14.46s` | `03:53:52.578` | **Experiment Completed** | All verification rules evaluated by Oracle |

## Verification Oracle Scorecard

| Invariant Rule | Status | Observed | Expected | Details |
| :--- | :---: | :--- | :--- | :--- |
| `baseline_healthy` | **PASS** | `1` | `>=1` | Target met baseline readiness (1 ready, expected >= 1). |
| `fault_injected` | **PASS** | `Injected` | `Injected` | Chaos Mesh confirmed fault injection (AllInjected / Apply Succeeded). |
| `target_impact` | **PASS** | `True` | `True` | Target pod disappearance confirmed (course-service-5dbf4b5855-jrwdx). |
| `service_recovery` | **PASS** | `1` | `>=1` | Service recovered 1 ready replica(s) (replacement: course-service-5dbf4b5855-2dhjx). |
| `recovery_within_timeout` | **PASS** | `7.43s` | `<=60.0s` | Service recovered in 7.43s (within limit of 60.0s). |
| `app_health_probe` | **PASS** | `200 OK` | `SERVING/200 OK` | Application health probe (HTTP) verified: status 200 OK. |
| `app_functional_check` | **PASS** | `crs-101` | `CONFIRMED_OK` | Functional transaction verified successfully (Entity ID: crs-101). |
| `app_logs_healthy` | **PASS** | `startup=True, errors=0` | `startup=True, errors=0` | Container logs confirmed clean startup (found 'Uvicorn running', 0 fatal errors). |
| `telemetry_traces_recorded` | **PASS** | `1` | `>0` | Distributed tracing active: 1 traces (3 spans) captured for 'course-service'. |
| `telemetry_error_spike_detected` | **NOT_APPLICABLE** | `N/A` | `N/A` | Error spike verification skipped (optional check not required in config). |
| `telemetry_latency_recovered` | **PASS** | `11.45ms` | `<=1000.0ms` | Latency SLA verified: p95 latency 11.45ms within threshold (<=1000.0ms). |

## Application-Level Correctness Verification

- **Health Probe (HTTP)**: `200 OK`
- **Functional Transaction Confirmation**: Processed successfully (`Entity ID: crs-101`)
- **Container Startup Logs**: `Clean initialization detected` (0 fatal exceptions)

## Distributed Tracing & Telemetry (OpenTelemetry / Jaeger)

- **Target Service**: `course-service`
- **Traces Captured**: `1` (3 spans)
- **Error Spans**: `0` (0.0% error rate)
- **Latency Distribution**: min `0.02ms` | avg `4.26ms` | **p50** `0.05ms` | **p95** `11.45ms` | max `12.72ms`
- **Sample Trace IDs**: `a9eddffc6920533027589d721f750f95`
- **Operations Monitored**: `GET /health`, `GET /health http send`

## Kubernetes Cluster Audit Events

| Type | Reason | Object | Message |
| :---: | :--- | :--- | :--- |
| `Normal` | **Killing** | `pod/course-service-5dbf4b5855-txfkh` | Stopping container course-service |
| `Normal` | **SuccessfulCreate** | `replicaset/course-service-5dbf4b5855` | Created pod: course-service-5dbf4b5855-txfkh |
| `Normal` | **SuccessfulCreate** | `replicaset/course-service-5dbf4b5855` | Created pod: course-service-5dbf4b5855-nhw7l |
| `Normal` | **SuccessfulCreate** | `replicaset/course-service-5dbf4b5855` | Created pod: course-service-5dbf4b5855-jrwdx |
| `Normal` | **SuccessfulCreate** | `replicaset/course-service-5dbf4b5855` | Created pod: course-service-5dbf4b5855-2dhjx |
| `Normal` | **ScalingReplicaSet** | `deployment/course-service` | Scaled up replica set course-service-5dbf4b5855 from 0 to 1 |
