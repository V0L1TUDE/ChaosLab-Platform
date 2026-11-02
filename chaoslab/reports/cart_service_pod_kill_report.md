# ChaosLab Experiment Report: `cart-service-pod-kill`

> **Verdict**: ✅ **PASS** | **Application**: `Google Online Boutique` | **Target**: `cartservice` | **Fault**: `pod-kill` | **Recovery Time**: `23.0s`

## Executive Summary

| Property | Value |
| :--- | :--- |
| **Experiment** | `cart-service-pod-kill` |
| **Application Group** | **Google Online Boutique** |
| **Target Microservice** | `cartservice` (namespace: `default`) |
| **Fault Injected** | `pod-kill` |
| **Baseline Pod** | `cartservice-9479b6c-pcljw` (1/1) |
| **Replacement Pod** | `cartservice-9479b6c-zsr9w` |
| **Recovery Time** | `23.0s` (Threshold: `60s`) |
| **Final Verdict** | **PASS** (7/7 executed invariants satisfied) |

## Recovery Timeline Progression

| Offset | Time (UTC) | Milestone | Details |
| :---: | :---: | :--- | :--- |
| `+0.0s` | `03:54:07.443` | **Experiment Initialized** | Baseline established for cartservice |
| `+0.48s` | `03:54:07.925` | **Fault Injected** | Chaos Mesh fault injected (pod-kill) |
| `+0.65s` | `03:54:08.094` | **Target Impact Detected** | Original pod cartservice-9479b6c-pcljw terminated/impacted |
| `+23.66s` | `03:54:31.099` | **Replacement Ready** | Service recovered (cartservice-9479b6c-zsr9w) |
| `+26.25s` | `03:54:33.693` | **Experiment Completed** | All verification rules evaluated by Oracle |

## Verification Oracle Scorecard

| Invariant Rule | Status | Observed | Expected | Details |
| :--- | :---: | :--- | :--- | :--- |
| `baseline_healthy` | **PASS** | `1` | `>=1` | Target met baseline readiness (1 ready, expected >= 1). |
| `fault_injected` | **PASS** | `Injected` | `Injected` | Chaos Mesh confirmed fault injection (AllInjected / Apply Succeeded). |
| `target_impact` | **PASS** | `True` | `True` | Target pod disappearance confirmed (cartservice-9479b6c-pcljw). |
| `service_recovery` | **PASS** | `1` | `>=1` | Service recovered 1 ready replica(s) (replacement: cartservice-9479b6c-zsr9w). |
| `recovery_within_timeout` | **PASS** | `23.00s` | `<=60.0s` | Service recovered in 23.00s (within limit of 60.0s). |
| `app_health_probe` | **PASS** | `SERVING` | `SERVING/200 OK` | Application health probe (GRPC) verified: status SERVING. |
| `app_functional_check` | **NOT_APPLICABLE** | `N/A` | `N/A` | Functional transaction test not configured for this experiment. |
| `app_logs_healthy` | **PASS** | `startup=True, errors=0` | `startup=True, errors=0` | Container logs confirmed clean startup (found 'Application started', 0 fatal errors). |
| `telemetry_traces_recorded` | **NOT_APPLICABLE** | `N/A` | `N/A` | Distributed tracing verification skipped (not enabled in config). |
| `telemetry_error_spike_detected` | **NOT_APPLICABLE** | `N/A` | `N/A` | Error spike verification skipped (optional check not required in config). |
| `telemetry_latency_recovered` | **NOT_APPLICABLE** | `N/A` | `N/A` | Latency SLA verification skipped (not enabled in config). |

## Application-Level Correctness Verification

- **Health Probe (GRPC)**: `SERVING`
- **Container Startup Logs**: `Clean initialization detected` (0 fatal exceptions)

## Kubernetes Cluster Audit Events

| Type | Reason | Object | Message |
| :---: | :--- | :--- | :--- |
| `Normal` | **Scheduled** | `pod/cartservice-9479b6c-zsr9w` | Successfully assigned default/cartservice-9479b6c-zsr9w to desktop-control-plane |
| `Normal` | **Pulled** | `pod/cartservice-9479b6c-zsr9w` | Container image "us-central1-docker.pkg.dev/online-boutique-ci/microservices-demo/cartservice:v0.10.6" already present on machine and can be accessed by the pod |
| `Normal` | **Created** | `pod/cartservice-9479b6c-zsr9w` | Container created |
| `Normal` | **Started** | `pod/cartservice-9479b6c-zsr9w` | Container started |
| `Warning` | **Unhealthy** | `pod/cartservice-9479b6c-zsr9w` | Liveness probe failed: timeout: health rpc did not complete within 1s |
| `Normal` | **SuccessfulCreate** | `replicaset/cartservice-9479b6c` | Created pod: cartservice-9479b6c-zsr9w |
