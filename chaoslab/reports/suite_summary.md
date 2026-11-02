# ChaosLab Automated Regression Suite Summary

> **Verdict**: ✅ **SUITE PASSED** &bull; **Passed**: `9/9` experiments &bull; **Time (UTC)**: `2026-11-02T03:55:59.057651+00:00`

## Experiment Scorecard

| Application | Experiment | Target | Fault | Recovery Time | p95 Latency | Invariants | Status |
| :--- | :--- | :--- | :--- | :---: | :---: | :---: | :---: |
| **Learning Management System (LMS)** | `lms-course-network-delay` | `course-service` | `network-delay` | `0.5s` | `1.18ms` | `8/8` | ✅ PASS |
| **Learning Management System (LMS)** | `lms-course-pod-kill` | `course-service` | `pod-kill` | `7.43s` | `11.45ms` | `10/10` | ✅ PASS |
| **Learning Management System (LMS)** | `lms-enrollment-pod-kill` | `enrollment-service` | `pod-kill` | `7.58s` | `12.63ms` | `10/10` | ✅ PASS |
| **Google Online Boutique** | `cart-service-pod-kill` | `cartservice` | `pod-kill` | `23.0s` | `N/A` | `7/7` | ✅ PASS |
| **Google Online Boutique** | `checkout-service-pod-kill` | `checkoutservice` | `pod-kill` | `2.83s` | `1.05ms` | `9/9` | ✅ PASS |
| **Google Online Boutique** | `payment-service-pod-kill` | `paymentservice` | `pod-kill` | `23.41s` | `1.86ms` | `10/10` | ✅ PASS |
| **Project Management Platform** | `project-service-pod-kill` | `project-service` | `pod-kill` | `6.26s` | `13.36ms` | `10/10` | ✅ PASS |
| **Project Management Platform** | `project-task-network-delay` | `task-service` | `network-delay` | `0.5s` | `706.55ms` | `8/8` | ✅ PASS |
| **Project Management Platform** | `project-task-pod-kill` | `task-service` | `pod-kill` | `7.42s` | `10.4ms` | `10/10` | ✅ PASS |

---
*Artifacts generated in `C:\users\arjan singh sawhney\microservices-demo\chaoslab\reports`. View individual HTML dashboards for visual recovery timelines.*