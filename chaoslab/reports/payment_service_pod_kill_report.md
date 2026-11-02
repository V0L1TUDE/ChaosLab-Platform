# ChaosLab Experiment Report: `payment-service-pod-kill`

> **Verdict**: ✅ **PASS** | **Application**: `Google Online Boutique` | **Target**: `paymentservice` | **Fault**: `pod-kill` | **Recovery Time**: `23.41s`

## Executive Summary

| Property | Value |
| :--- | :--- |
| **Experiment** | `payment-service-pod-kill` |
| **Application Group** | **Google Online Boutique** |
| **Target Microservice** | `paymentservice` (namespace: `default`) |
| **Fault Injected** | `pod-kill` |
| **Baseline Pod** | `paymentservice-879f99cd5-h6ssh` (1/1) |
| **Replacement Pod** | `paymentservice-879f99cd5-fh5gt` |
| **Recovery Time** | `23.41s` (Threshold: `60s`) |
| **Final Verdict** | **PASS** (10/10 executed invariants satisfied) |

## Recovery Timeline Progression

| Offset | Time (UTC) | Milestone | Details |
| :---: | :---: | :--- | :--- |
| `+0.0s` | `03:54:43.269` | **Experiment Initialized** | Baseline established for paymentservice |
| `+0.49s` | `03:54:43.761` | **Fault Injected** | Chaos Mesh fault injected (pod-kill) |
| `+0.68s` | `03:54:43.952` | **Target Impact Detected** | Original pod paymentservice-879f99cd5-h6ssh terminated/impacted |
| `+24.09s` | `03:55:07.363` | **Replacement Ready** | Service recovered (paymentservice-879f99cd5-fh5gt) |
| `+34.99s` | `03:55:18.260` | **Experiment Completed** | All verification rules evaluated by Oracle |

## Verification Oracle Scorecard

| Invariant Rule | Status | Observed | Expected | Details |
| :--- | :---: | :--- | :--- | :--- |
| `baseline_healthy` | **PASS** | `1` | `>=1` | Target met baseline readiness (1 ready, expected >= 1). |
| `fault_injected` | **PASS** | `Injected` | `Injected` | Chaos Mesh confirmed fault injection (AllInjected / Apply Succeeded). |
| `target_impact` | **PASS** | `True` | `True` | Target pod disappearance confirmed (paymentservice-879f99cd5-h6ssh). |
| `service_recovery` | **PASS** | `1` | `>=1` | Service recovered 1 ready replica(s) (replacement: paymentservice-879f99cd5-fh5gt). |
| `recovery_within_timeout` | **PASS** | `23.41s` | `<=60.0s` | Service recovered in 23.41s (within limit of 60.0s). |
| `app_health_probe` | **PASS** | `SERVING` | `SERVING/200 OK` | Application health probe (GRPC) verified: status SERVING. |
| `app_functional_check` | **PASS** | `1f2b6958-acf5-466d-8fb1-0b383aaa2abb` | `CONFIRMED_OK` | Functional transaction verified successfully (Entity ID: 1f2b6958-acf5-466d-8fb1-0b383aaa2abb). |
| `app_logs_healthy` | **PASS** | `startup=True, errors=0` | `startup=True, errors=0` | Container logs confirmed clean startup (found 'PaymentService gRPC server started', 0 fatal errors). |
| `telemetry_traces_recorded` | **PASS** | `4` | `>0` | Distributed tracing active: 4 traces (4 spans) captured for 'paymentservice'. |
| `telemetry_error_spike_detected` | **NOT_APPLICABLE** | `N/A` | `N/A` | Error spike verification skipped (optional check not required in config). |
| `telemetry_latency_recovered` | **PASS** | `1.86ms` | `<=1000.0ms` | Latency SLA verified: p95 latency 1.86ms within threshold (<=1000.0ms). |

## Application-Level Correctness Verification

- **Health Probe (GRPC)**: `SERVING`
- **Functional Transaction Confirmation**: Processed successfully (`Entity ID: 1f2b6958-acf5-466d-8fb1-0b383aaa2abb`)
- **Container Startup Logs**: `Clean initialization detected` (0 fatal exceptions)

## Distributed Tracing & Telemetry (OpenTelemetry / Jaeger)

- **Target Service**: `paymentservice`
- **Traces Captured**: `4` (4 spans)
- **Error Spans**: `0` (0.0% error rate)
- **Latency Distribution**: min `0.1ms` | avg `0.66ms` | **p50** `0.2ms` | **p95** `1.86ms` | max `2.13ms`
- **Sample Trace IDs**: `12815de23ed7c7147c84aea1bc5cb1fc`, `e2ba80f09b368a10bff9db8c3259d82d`, `c5148168b222c246bd42e394a7455750`
- **Operations Monitored**: `grpc.grpc.health.v1.Health/Check`, `grpc.hipstershop.PaymentService/Charge`

## Kubernetes Cluster Audit Events

| Type | Reason | Object | Message |
| :---: | :--- | :--- | :--- |
| `Normal` | **Created** | `pod/paymentservice-879f99cd5-h6ssh` | Container created |
| `Normal` | **Started** | `pod/paymentservice-879f99cd5-h6ssh` | Container started |
| `Warning` | **Unhealthy** | `pod/paymentservice-879f99cd5-h6ssh` | Readiness probe failed: timeout: failed to connect service "10.244.0.32:50051" within 1s: context deadline exceeded |
| `Normal` | **Killing** | `pod/paymentservice-879f99cd5-h6ssh` | Stopping container server |
| `Normal` | **SuccessfulCreate** | `replicaset/paymentservice-879f99cd5` | Created pod: paymentservice-879f99cd5-h6ssh |
| `Normal` | **SuccessfulCreate** | `replicaset/paymentservice-879f99cd5` | Created pod: paymentservice-879f99cd5-fh5gt |
