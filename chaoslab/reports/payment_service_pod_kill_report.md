# ChaosLab Experiment Report: `payment-service-pod-kill`

> **Verdict**: ✅ **PASS** | **Application**: `Google Online Boutique` | **Target**: `paymentservice` | **Fault**: `pod-kill` | **Recovery Time**: `12.17s`

## Executive Summary

| Property | Value |
| :--- | :--- |
| **Experiment** | `payment-service-pod-kill` |
| **Application Group** | **Google Online Boutique** |
| **Target Microservice** | `paymentservice` (namespace: `default`) |
| **Fault Injected** | `pod-kill` |
| **Baseline Pod** | `paymentservice-756497c7f8-xbfq2` (1/1) |
| **Replacement Pod** | `paymentservice-756497c7f8-tc9fz` |
| **Recovery Time** | `12.17s` (Threshold: `60s`) |
| **Final Verdict** | **PASS** (10/10 executed invariants satisfied) |

## Recovery Timeline Progression

| Offset | Time (UTC) | Milestone | Details |
| :---: | :---: | :--- | :--- |
| `+0.0s` | `20:32:59.645` | **Experiment Initialized** | Baseline established for paymentservice |
| `+0.23s` | `20:32:59.872` | **Fault Injected** | Chaos Mesh fault injected (pod-kill) |
| `+0.27s` | `20:32:59.920` | **Target Impact Detected** | Original pod paymentservice-756497c7f8-xbfq2 terminated/impacted |
| `+12.44s` | `20:33:12.085` | **Replacement Ready** | Service recovered (paymentservice-756497c7f8-tc9fz) |
| `+26.48s` | `20:33:26.129` | **Experiment Completed** | All verification rules evaluated by Oracle |

## Verification Oracle Scorecard

| Invariant Rule | Status | Observed | Expected | Details |
| :--- | :---: | :--- | :--- | :--- |
| `baseline_healthy` | **PASS** | `1` | `>=1` | Target met baseline readiness (1 ready, expected >= 1). |
| `fault_injected` | **PASS** | `Injected` | `Injected` | Chaos Mesh confirmed fault injection (AllInjected / Apply Succeeded). |
| `target_impact` | **PASS** | `True` | `True` | Target pod disappearance confirmed (paymentservice-756497c7f8-xbfq2). |
| `service_recovery` | **PASS** | `1` | `>=1` | Service recovered 1 ready replica(s) (replacement: paymentservice-756497c7f8-tc9fz). |
| `recovery_within_timeout` | **PASS** | `12.17s` | `<=60.0s` | Service recovered in 12.17s (within limit of 60.0s). |
| `app_health_probe` | **PASS** | `SERVING` | `SERVING/200 OK` | Application health probe (GRPC) verified: status SERVING. |
| `app_functional_check` | **PASS** | `8d403349-6f70-4b0e-94c3-ec3f8c232592` | `CONFIRMED_OK` | Functional transaction verified successfully (Entity ID: 8d403349-6f70-4b0e-94c3-ec3f8c232592). |
| `app_logs_healthy` | **PASS** | `startup=True, errors=0` | `startup=True, errors=0` | Container logs confirmed clean startup (found 'PaymentService gRPC server started', 0 fatal errors). |
| `telemetry_traces_recorded` | **PASS** | `4` | `>0` | Distributed tracing active: 4 traces (40 spans) captured for 'paymentservice'. |
| `telemetry_error_spike_detected` | **NOT_APPLICABLE** | `N/A` | `N/A` | Error spike verification skipped (optional check not required in config). |
| `telemetry_latency_recovered` | **PASS** | `154.81ms` | `<=1000.0ms` | Latency SLA verified: p95 latency 154.81ms within threshold (<=1000.0ms). |

## Application-Level Correctness Verification

- **Health Probe (GRPC)**: `SERVING`
- **Functional Transaction Confirmation**: Processed successfully (`Entity ID: 8d403349-6f70-4b0e-94c3-ec3f8c232592`)
- **Container Startup Logs**: `Clean initialization detected` (0 fatal exceptions)

## Distributed Tracing & Telemetry (OpenTelemetry / Jaeger)

- **Target Service**: `paymentservice`
- **Traces Captured**: `4` (40 spans)
- **Error Spans**: `0` (0.0% error rate)
- **Latency Distribution**: min `0.1ms` | avg `19.54ms` | **p50** `0.95ms` | **p95** `154.81ms` | max `176.04ms`
- **Sample Trace IDs**: `918ba4e975fd6a5ac92bf7050357c4fd`, `3d94463e082a72c658bd16da05579aab`, `e6ecd831acd619b7ad20cdcb151e1a3f`
- **Operations Monitored**: `POST`, `grpc.grpc.health.v1.Health/Check`, `grpc.hipstershop.PaymentService/Charge`, `hipstershop.CartService/EmptyCart`, `hipstershop.CartService/GetCart`

## Kubernetes Cluster Audit Events

| Type | Reason | Object | Message |
| :---: | :--- | :--- | :--- |
| `Normal` | **SuccessfulCreate** | `replicaset/paymentservice-756497c7f8` | Created pod: paymentservice-756497c7f8-tc9fz |
| `Normal` | **ScalingReplicaSet** | `deployment/paymentservice` | Scaled up replica set paymentservice-597bd87644 from 0 to 1 |
| `Normal` | **ScalingReplicaSet** | `deployment/paymentservice` | Scaled up replica set paymentservice-6bc57ffc7 from 0 to 1 |
| `Normal` | **ScalingReplicaSet** | `deployment/paymentservice` | Scaled down replica set paymentservice-597bd87644 from 1 to 0 |
| `Normal` | **ScalingReplicaSet** | `deployment/paymentservice` | Scaled up replica set paymentservice-756497c7f8 from 0 to 1 |
| `Normal` | **ScalingReplicaSet** | `deployment/paymentservice` | Scaled down replica set paymentservice-6bc57ffc7 from 1 to 0 |
