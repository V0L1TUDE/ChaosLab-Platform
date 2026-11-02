# ChaosLab Experiment Report: `checkout-service-pod-kill`

> **Verdict**: ✅ **PASS** | **Application**: `Google Online Boutique` | **Target**: `checkoutservice` | **Fault**: `pod-kill` | **Recovery Time**: `2.83s`

## Executive Summary

| Property | Value |
| :--- | :--- |
| **Experiment** | `checkout-service-pod-kill` |
| **Application Group** | **Google Online Boutique** |
| **Target Microservice** | `checkoutservice` (namespace: `default`) |
| **Fault Injected** | `pod-kill` |
| **Baseline Pod** | `checkoutservice-8775ddc9-g6g4b` (1/1) |
| **Replacement Pod** | `checkoutservice-8775ddc9-wsxjz` |
| **Recovery Time** | `2.83s` (Threshold: `60s`) |
| **Final Verdict** | **PASS** (9/9 executed invariants satisfied) |

## Recovery Timeline Progression

| Offset | Time (UTC) | Milestone | Details |
| :---: | :---: | :--- | :--- |
| `+0.0s` | `03:54:33.842` | **Experiment Initialized** | Baseline established for checkoutservice |
| `+0.54s` | `03:54:34.387` | **Fault Injected** | Chaos Mesh fault injected (pod-kill) |
| `+0.77s` | `03:54:34.613` | **Target Impact Detected** | Original pod checkoutservice-8775ddc9-g6g4b terminated/impacted |
| `+3.6s` | `03:54:37.438` | **Replacement Ready** | Service recovered (checkoutservice-8775ddc9-wsxjz) |
| `+9.31s` | `03:54:43.149` | **Experiment Completed** | All verification rules evaluated by Oracle |

## Verification Oracle Scorecard

| Invariant Rule | Status | Observed | Expected | Details |
| :--- | :---: | :--- | :--- | :--- |
| `baseline_healthy` | **PASS** | `1` | `>=1` | Target met baseline readiness (1 ready, expected >= 1). |
| `fault_injected` | **PASS** | `Injected` | `Injected` | Chaos Mesh confirmed fault injection (AllInjected / Apply Succeeded). |
| `target_impact` | **PASS** | `True` | `True` | Target pod disappearance confirmed (checkoutservice-8775ddc9-g6g4b). |
| `service_recovery` | **PASS** | `1` | `>=1` | Service recovered 1 ready replica(s) (replacement: checkoutservice-8775ddc9-wsxjz). |
| `recovery_within_timeout` | **PASS** | `2.83s` | `<=60.0s` | Service recovered in 2.83s (within limit of 60.0s). |
| `app_health_probe` | **PASS** | `SERVING` | `SERVING/200 OK` | Application health probe (GRPC) verified: status SERVING. |
| `app_functional_check` | **NOT_APPLICABLE** | `N/A` | `N/A` | Functional transaction test not configured for this experiment. |
| `app_logs_healthy` | **PASS** | `startup=True, errors=0` | `startup=True, errors=0` | Container logs confirmed clean startup (found 'starting to listen on tcp', 0 fatal errors). |
| `telemetry_traces_recorded` | **PASS** | `1` | `>0` | Distributed tracing active: 1 traces (1 spans) captured for 'checkoutservice'. |
| `telemetry_error_spike_detected` | **NOT_APPLICABLE** | `N/A` | `N/A` | Error spike verification skipped (optional check not required in config). |
| `telemetry_latency_recovered` | **PASS** | `1.05ms` | `<=1000.0ms` | Latency SLA verified: p95 latency 1.05ms within threshold (<=1000.0ms). |

## Application-Level Correctness Verification

- **Health Probe (GRPC)**: `SERVING`
- **Container Startup Logs**: `Clean initialization detected` (0 fatal exceptions)

## Distributed Tracing & Telemetry (OpenTelemetry / Jaeger)

- **Target Service**: `checkoutservice`
- **Traces Captured**: `1` (1 spans)
- **Error Spans**: `0` (0.0% error rate)
- **Latency Distribution**: min `1.05ms` | avg `1.05ms` | **p50** `1.05ms` | **p95** `1.05ms` | max `1.05ms`
- **Sample Trace IDs**: `9d957cdea27c70b436d23e95c4c4694b`
- **Operations Monitored**: `grpc.health.v1.Health/Check`

## Kubernetes Cluster Audit Events

| Type | Reason | Object | Message |
| :---: | :--- | :--- | :--- |
| `Normal` | **Pulled** | `pod/checkoutservice-8775ddc9-wsxjz` | Container image "us-central1-docker.pkg.dev/online-boutique-ci/microservices-demo/checkoutservice:v0.10.6" already present on machine and can be accessed by the pod |
| `Normal` | **Created** | `pod/checkoutservice-8775ddc9-wsxjz` | Container created |
| `Normal` | **Started** | `pod/checkoutservice-8775ddc9-wsxjz` | Container started |
| `Warning` | **Unhealthy** | `pod/checkoutservice-8775ddc9-wsxjz` | Readiness probe failed: timeout: failed to connect service "10.244.0.48:5050" within 1s: context deadline exceeded |
| `Normal` | **SuccessfulCreate** | `replicaset/checkoutservice-8775ddc9` | Created pod: checkoutservice-8775ddc9-g6g4b |
| `Normal` | **SuccessfulCreate** | `replicaset/checkoutservice-8775ddc9` | Created pod: checkoutservice-8775ddc9-wsxjz |
