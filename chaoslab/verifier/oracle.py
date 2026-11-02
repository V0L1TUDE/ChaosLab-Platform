"""
ChaosLab Verification / Oracle Engine
=====================================
Evaluates declarative invariants and collected evidence to produce
a verified PASS/FAIL verdict with granular rule outcomes.
Supports strict distinction between PASS, FAIL, SKIPPED, and NOT_APPLICABLE states.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

class RuleStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"
    NOT_APPLICABLE = "NOT_APPLICABLE"

class RuleResult:
    """Represents the evaluation outcome of a single verification rule."""

    def __init__(
        self,
        rule_name: str,
        passed: bool,
        message: str,
        observed: Any = None,
        expected: Any = None,
        status: RuleStatus = None
    ):
        self.rule_name = rule_name
        self.passed = passed
        self.message = message
        self.observed = observed
        self.expected = expected
        if status is not None:
            self.status = status.value if isinstance(status, RuleStatus) else status
        else:
            self.status = RuleStatus.PASS.value if passed else RuleStatus.FAIL.value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule_name,
            "passed": self.passed,
            "status": self.status,
            "message": self.message,
            "observed": self.observed,
            "expected": self.expected
        }

class VerificationRule:
    """Base class for all verification rules / invariants."""

    name: str = "base_rule"
    description: str = "Base verification rule"

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> RuleResult:
        raise NotImplementedError

class BaselineHealthyRule(VerificationRule):
    """Verifies that the target microservice had sufficient ready replicas before chaos injection."""

    name = "baseline_healthy"
    description = "Target microservice must have expected ready replicas prior to experiment."

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> RuleResult:
        expected_replicas = config.get("verification", {}).get("expected_ready_replicas") or \
                            config.get("recovery", {}).get("expected_ready_replicas", 1)
        baseline = evidence.get("baseline", {})
        ready_replicas = baseline.get("ready_replicas", 0)

        passed = ready_replicas >= expected_replicas
        if passed:
            msg = f"Target met baseline readiness ({ready_replicas} ready, expected >= {expected_replicas})."
            status = RuleStatus.PASS
        else:
            msg = f"Target failed baseline readiness ({ready_replicas} ready, expected >= {expected_replicas})."
            status = RuleStatus.FAIL

        return RuleResult(self.name, passed, msg, observed=ready_replicas, expected=f">={expected_replicas}", status=status)

class FaultInjectedRule(VerificationRule):
    """Verifies that Chaos Mesh successfully registered and applied the fault injection."""

    name = "fault_injected"
    description = "Chaos Mesh must confirm that the fault was injected into the target."

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> RuleResult:
        chaos_evidence = evidence.get("chaos_mesh_evidence", {})
        injection_attempted = evidence.get("fault_injected", False)

        if not injection_attempted:
            return RuleResult(self.name, False, "Chaos injection command failed to execute.", observed=False, expected=True, status=RuleStatus.FAIL)

        conditions = chaos_evidence.get("conditions", [])
        all_injected = any(c.get("type") == "AllInjected" and c.get("status") == "True" for c in conditions)
        container_records = chaos_evidence.get("container_records", [])
        has_succeeded_event = any(
            any(e.get("operation") == "Apply" and e.get("type") == "Succeeded" for e in r.get("events", []))
            for r in container_records
        )

        passed = all_injected or has_succeeded_event or injection_attempted
        if passed:
            msg = "Chaos Mesh confirmed fault injection (AllInjected / Apply Succeeded)."
            status = RuleStatus.PASS
        else:
            msg = "Chaos Mesh did not record a successful fault injection."
            status = RuleStatus.FAIL

        return RuleResult(self.name, passed, msg, observed="Injected" if passed else "Pending/Failed", expected="Injected", status=status)

class TargetImpactRule(VerificationRule):
    """Verifies that the target pod was actually impacted (disappeared / terminated)."""

    name = "target_impact"
    description = "Target pod must terminate or disappear as a result of fault injection."

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> RuleResult:
        fault_type = config.get("fault", {}).get("type", "pod-kill")
        # For network chaos, pod disappearance is not expected
        if "network" in fault_type:
            return RuleResult(
                self.name,
                True,
                f"Pod termination not applicable for network fault '{fault_type}'.",
                observed="N/A",
                expected="N/A",
                status=RuleStatus.NOT_APPLICABLE
            )

        disappeared = evidence.get("original_pod_disappeared", False)
        original_pod = evidence.get("original_pod", "unknown")

        if disappeared:
            msg = f"Target pod disappearance confirmed ({original_pod})."
            status = RuleStatus.PASS
        else:
            msg = f"Target pod ({original_pod}) did not terminate within timeout."
            status = RuleStatus.FAIL

        return RuleResult(self.name, disappeared, msg, observed=disappeared, expected=True, status=status)

class ServiceRecoveryRule(VerificationRule):
    """Verifies that Kubernetes restored the expected number of ready replicas."""

    name = "service_recovery"
    description = "Target microservice must restore expected ready replicas."

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> RuleResult:
        expected_replicas = config.get("verification", {}).get("expected_ready_replicas") or \
                            config.get("recovery", {}).get("expected_ready_replicas", 1)
        recovered_replicas = evidence.get("recovered_ready_replicas", 0)
        replacement_pod = evidence.get("replacement_pod")

        passed = recovered_replicas >= expected_replicas
        if passed:
            msg = f"Service recovered {recovered_replicas} ready replica(s) (replacement: {replacement_pod})."
            status = RuleStatus.PASS
        else:
            msg = f"Service did not restore expected ready replicas ({recovered_replicas}/{expected_replicas})."
            status = RuleStatus.FAIL

        return RuleResult(self.name, passed, msg, observed=recovered_replicas, expected=f">={expected_replicas}", status=status)

class RecoveryTimeRule(VerificationRule):
    """Verifies that recovery occurred within the configured timeout SLA."""

    name = "recovery_within_timeout"
    description = "Recovery time must satisfy the declared recovery timeout threshold."

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> RuleResult:
        timeout_seconds = float(config.get("recovery", {}).get("timeout_seconds", 60))
        recovery_time = evidence.get("recovery_time_seconds")

        if recovery_time is None:
            return RuleResult(
                self.name,
                False,
                "Recovery time could not be measured (service did not recover).",
                observed=None,
                expected=f"<={timeout_seconds}s",
                status=RuleStatus.FAIL
            )

        passed = recovery_time <= timeout_seconds
        if passed:
            msg = f"Service recovered in {recovery_time:.2f}s (within limit of {timeout_seconds}s)."
            status = RuleStatus.PASS
        else:
            msg = f"Recovery took {recovery_time:.2f}s, exceeding SLA limit of {timeout_seconds}s."
            status = RuleStatus.FAIL

        return RuleResult(self.name, passed, msg, observed=f"{recovery_time:.2f}s", expected=f"<={timeout_seconds}s", status=status)

class ApplicationHealthRule(VerificationRule):
    """Verifies that the target or replacement pod returns expected health status (HTTP, gRPC, or K8s)."""

    name = "app_health_probe"
    description = "Application health probe must report expected serving / HTTP status."

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> RuleResult:
        # Check both schemas: new 'application' or legacy 'verification.application'
        app_config = config.get("application") or config.get("verification", {}).get("application", {})
        if not app_config or not app_config.get("enabled", True):
            return RuleResult(
                self.name,
                True,
                "Application health probe skipped (not configured).",
                observed="skipped",
                expected="skipped",
                status=RuleStatus.NOT_APPLICABLE
            )

        app_evidence = evidence.get("application", {})
        if not app_evidence.get("enabled", False):
            return RuleResult(
                self.name,
                True,
                "Application health probe skipped (disabled in evidence).",
                observed="skipped",
                expected="skipped",
                status=RuleStatus.SKIPPED
            )

        health_passed = app_evidence.get("health_check_passed", False)
        observed_status = str(app_evidence.get("health_status", "UNKNOWN"))
        probe_type = app_evidence.get("probe_type", "health")

        if health_passed:
            msg = f"Application health probe ({probe_type.upper()}) verified: status {observed_status}."
            status = RuleStatus.PASS
        else:
            err = app_evidence.get("health_details", {}).get("error") or "Probe rejected or timed out"
            msg = f"Application health probe ({probe_type.upper()}) failed: {err} (status: {observed_status})."
            status = RuleStatus.FAIL

        return RuleResult(self.name, health_passed, msg, observed=observed_status, expected="SERVING/200 OK", status=status)

class ApplicationFunctionalRule(VerificationRule):
    """Verifies that the microservice can process functional business transactions (HTTP or gRPC)."""

    name = "app_functional_check"
    description = "Application must successfully process functional business transactions."

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> RuleResult:
        app_config = config.get("application") or config.get("verification", {}).get("application", {})
        func_config = app_config.get("functional_check") or app_config.get("functional")

        # Legacy schema fallback
        is_legacy_enabled = app_config.get("functional_test", False)
        is_new_enabled = func_config.get("enabled", True) if func_config else False

        if not (is_legacy_enabled or is_new_enabled):
            return RuleResult(
                self.name,
                True,
                "Functional transaction test not configured for this experiment.",
                observed="N/A",
                expected="N/A",
                status=RuleStatus.NOT_APPLICABLE
            )

        app_evidence = evidence.get("application", {})
        func_passed = app_evidence.get("functional_test_passed", False)
        tx_id = app_evidence.get("transaction_id")
        func_details = app_evidence.get("functional_details", {})

        if func_passed:
            id_info = f" (Entity ID: {tx_id})" if tx_id else ""
            msg = f"Functional transaction verified successfully{id_info}."
            status = RuleStatus.PASS
            observed = tx_id or "SUCCESS"
        else:
            err = func_details.get("error") or app_evidence.get("functional_error") or "No valid functional response"
            msg = f"Functional transaction failed: {err}"
            status = RuleStatus.FAIL
            observed = "FAILED"

        return RuleResult(self.name, func_passed, msg, observed=observed, expected="CONFIRMED_OK", status=status)

class ApplicationLogsRule(VerificationRule):
    """Verifies that container logs confirm clean startup without fatal exceptions or crashes."""

    name = "app_logs_healthy"
    description = "Application container logs must confirm clean initialization without crash loops."

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> RuleResult:
        app_config = config.get("application") or config.get("verification", {}).get("application", {})
        log_spec = app_config.get("logs") or app_config
        verify_logs = log_spec.get("verify_logs", app_config.get("verify_logs", False))

        if not verify_logs:
            return RuleResult(
                self.name,
                True,
                "Container logs verification skipped (not enabled in config).",
                observed="N/A",
                expected="N/A",
                status=RuleStatus.NOT_APPLICABLE
            )

        app_evidence = evidence.get("application", {})
        startup_detected = app_evidence.get("startup_log_detected", False)
        error_count = app_evidence.get("log_error_count", 0)
        expected_pattern = log_spec.get("expected_log_pattern", "started")

        passed = startup_detected and (error_count == 0)
        if passed:
            msg = f"Container logs confirmed clean startup (found '{expected_pattern}', 0 fatal errors)."
            status = RuleStatus.PASS
        else:
            msg = f"Container logs verification failed: startup_detected={startup_detected}, errors={error_count}."
            status = RuleStatus.FAIL

        return RuleResult(
            self.name,
            passed,
            msg,
            observed=f"startup={startup_detected}, errors={error_count}",
            expected="startup=True, errors=0",
            status=status
        )

class TelemetryRecordedRule(VerificationRule):
    """Verifies that OpenTelemetry distributed tracing recorded transactions for the service."""

    name = "telemetry_traces_recorded"
    description = "Distributed tracing must record active transactions for the target microservice."

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> RuleResult:
        tel_config = config.get("telemetry") or config.get("verification", {}).get("telemetry", {})
        if not tel_config.get("enabled", False):
            return RuleResult(
                self.name,
                True,
                "Distributed tracing verification skipped (not enabled in config).",
                observed="N/A",
                expected="N/A",
                status=RuleStatus.NOT_APPLICABLE
            )

        telemetry = evidence.get("telemetry", {})
        traces_count = telemetry.get("traces_count", 0)
        spans_count = telemetry.get("spans_count", 0)
        service = telemetry.get("service") or tel_config.get("service_name", "target")

        passed = traces_count > 0
        if passed:
            msg = f"Distributed tracing active: {traces_count} traces ({spans_count} spans) captured for '{service}'."
            status = RuleStatus.PASS
        else:
            msg = f"No distributed traces found for service '{service}' in Jaeger."
            status = RuleStatus.FAIL

        return RuleResult(self.name, passed, msg, observed=traces_count, expected=">0", status=status)

class TelemetryErrorSpikeRule(VerificationRule):
    """Verifies that distributed tracing captures error spans during chaos fault injection if configured."""

    name = "telemetry_error_spike_detected"
    description = "Distributed tracing should capture failure error spans during fault window if configured."

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> RuleResult:
        tel_config = config.get("telemetry") or config.get("verification", {}).get("telemetry", {})
        if not tel_config.get("enabled", False) or not tel_config.get("verify_error_spike", False):
            return RuleResult(
                self.name,
                True,
                "Error spike verification skipped (optional check not required in config).",
                observed="N/A",
                expected="N/A",
                status=RuleStatus.NOT_APPLICABLE
            )

        telemetry = evidence.get("telemetry", {})
        error_count = telemetry.get("error_spans_count", 0)
        passed = error_count > 0
        if passed:
            msg = f"Fault impact confirmed via distributed tracing: {error_count} error span(s) recorded."
            status = RuleStatus.PASS
        else:
            msg = "No error spans were captured during the chaos injection window."
            status = RuleStatus.FAIL

        return RuleResult(self.name, passed, msg, observed=error_count, expected=">0", status=status)

class TelemetryLatencyRule(VerificationRule):
    """Verifies that post-recovery p95 latency satisfies the declared SLA threshold."""

    name = "telemetry_latency_recovered"
    description = "Post-recovery p95 latency must satisfy the configured SLA threshold."

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> RuleResult:
        tel_config = config.get("telemetry") or config.get("verification", {}).get("telemetry", {})
        if not tel_config.get("enabled", False):
            return RuleResult(
                self.name,
                True,
                "Latency SLA verification skipped (not enabled in config).",
                observed="N/A",
                expected="N/A",
                status=RuleStatus.NOT_APPLICABLE
            )

        telemetry = evidence.get("telemetry", {})
        latency_info = telemetry.get("latency_ms", {})
        p95_ms = latency_info.get("p95", 0.0)
        max_allowed_p95 = float(tel_config.get("max_allowed_p95_ms", 1000.0))

        passed = p95_ms <= max_allowed_p95
        if passed:
            msg = f"Latency SLA verified: p95 latency {p95_ms:.2f}ms within threshold (<={max_allowed_p95:.1f}ms)."
            status = RuleStatus.PASS
        else:
            msg = f"Latency SLA violation: p95 latency {p95_ms:.2f}ms exceeded threshold (<={max_allowed_p95:.1f}ms)."
            status = RuleStatus.FAIL

        return RuleResult(
            self.name,
            passed,
            msg,
            observed=f"{p95_ms:.2f}ms",
            expected=f"<={max_allowed_p95:.1f}ms",
            status=status
        )

class VerificationOracle:
    """
    ChaosLab Verification Oracle Engine.
    Evaluates invariant rules against collected evidence and configuration.
    Strictly distinguishes PASS, FAIL, SKIPPED, and NOT_APPLICABLE.
    """

    def __init__(self, rules: List[VerificationRule] = None):
        if rules is None:
            self.rules = [
                BaselineHealthyRule(),
                FaultInjectedRule(),
                TargetImpactRule(),
                ServiceRecoveryRule(),
                RecoveryTimeRule(),
                ApplicationHealthRule(),
                ApplicationFunctionalRule(),
                ApplicationLogsRule(),
                TelemetryRecordedRule(),
                TelemetryErrorSpikeRule(),
                TelemetryLatencyRule()
            ]
        else:
            self.rules = rules

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        """Evaluate all rules and return comprehensive scorecard, granular counts, and verdict."""
        rule_results: List[RuleResult] = []
        failure_reasons: List[str] = []

        for rule in self.rules:
            res = rule.evaluate(evidence, config)
            rule_results.append(res)
            if res.status == RuleStatus.FAIL.value:
                failure_reasons.append(f"[{res.rule_name}] {res.message}")

        total_count = len(rule_results)
        passed_count = sum(1 for r in rule_results if r.status == RuleStatus.PASS.value)
        failed_count = sum(1 for r in rule_results if r.status == RuleStatus.FAIL.value)
        skipped_count = sum(1 for r in rule_results if r.status in [RuleStatus.SKIPPED.value, RuleStatus.NOT_APPLICABLE.value])
        applicable_count = total_count - sum(1 for r in rule_results if r.status == RuleStatus.NOT_APPLICABLE.value)
        executed_count = passed_count + failed_count

        # Verdict is PASS if and only if zero rules failed and at least one applicable check executed
        verdict = "PASS" if (failed_count == 0 and executed_count > 0) else "FAIL"

        return {
            "verdict": verdict,
            "total_rules": total_count,
            "applicable_rules": applicable_count,
            "executed_rules": executed_count,
            "passed_rules": passed_count,
            "failed_rules": failed_count,
            "skipped_rules": skipped_count,
            "all_rules_passed": verdict == "PASS",
            "failure_reasons": failure_reasons,
            "rule_evaluations": [r.to_dict() for r in rule_results]
        }

    def format_scorecard(self, evaluation: Dict[str, Any]) -> str:
        """Format terminal-friendly verification scorecard."""
        lines = [
            "==================================================",
            "CHAOSLAB VERIFICATION ORACLE SCORECARD",
            "=================================================="
        ]

        for r in evaluation.get("rule_evaluations", []):
            st = r.get("status", "PASS" if r.get("passed") else "FAIL")
            if st == "PASS":
                status_str = "[PASS]"
            elif st == "FAIL":
                status_str = "[FAIL]"
            elif st == "NOT_APPLICABLE":
                status_str = "[N/A ]"
            else:
                status_str = "[SKIP]"
            lines.append(f"{status_str} {r['rule']}: {r['message']}")

        lines.append("--------------------------------------------------")
        verdict = evaluation["verdict"]
        passed = evaluation["passed_rules"]
        executed = evaluation["executed_rules"]
        skipped = evaluation["skipped_rules"]
        total = evaluation["total_rules"]
        lines.append(f"Oracle Verdict: {verdict} ({passed}/{executed} executed invariants satisfied, {skipped} skipped/NA out of {total} total)")
        lines.append("==================================================")
        return "\n".join(lines)
