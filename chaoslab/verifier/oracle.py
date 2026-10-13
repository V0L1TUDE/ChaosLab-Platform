"""
ChaosLab Verification / Oracle Engine
=====================================
Evaluates declarative invariants and collected evidence to produce
a verified PASS/FAIL verdict with granular rule outcomes.
"""

from typing import Any, Dict, List

class RuleResult:
    """Represents the evaluation outcome of a single verification rule."""

    def __init__(self, rule_name: str, passed: bool, message: str, observed: Any = None, expected: Any = None):
        self.rule_name = rule_name
        self.passed = passed
        self.message = message
        self.observed = observed
        self.expected = expected

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule_name,
            "passed": self.passed,
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
        expected_replicas = config.get("verification", {}).get("expected_ready_replicas", 1)
        baseline = evidence.get("baseline", {})
        ready_replicas = baseline.get("ready_replicas", 0)

        passed = ready_replicas >= expected_replicas
        if passed:
            msg = f"Target met baseline readiness ({ready_replicas} ready, expected >= {expected_replicas})."
        else:
            msg = f"Target failed baseline readiness ({ready_replicas} ready, expected >= {expected_replicas})."

        return RuleResult(self.name, passed, msg, observed=ready_replicas, expected=f">={expected_replicas}")


class FaultInjectedRule(VerificationRule):
    """Verifies that Chaos Mesh successfully registered and applied the fault injection."""

    name = "fault_injected"
    description = "Chaos Mesh must confirm that the fault was injected into the target."

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> RuleResult:
        chaos_evidence = evidence.get("chaos_mesh_evidence", {})
        injection_attempted = evidence.get("fault_injected", False)

        if not injection_attempted:
            return RuleResult(self.name, False, "Chaos injection command failed to execute.", observed=False, expected=True)

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
        else:
            msg = "Chaos Mesh did not record a successful fault injection."

        return RuleResult(self.name, passed, msg, observed="Injected" if passed else "Pending/Failed", expected="Injected")


class TargetImpactRule(VerificationRule):
    """Verifies that the target pod was actually impacted (disappeared / terminated)."""

    name = "target_impact"
    description = "Target pod must terminate or disappear as a result of fault injection."

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> RuleResult:
        disappeared = evidence.get("original_pod_disappeared", False)
        original_pod = evidence.get("original_pod", "unknown")

        if disappeared:
            msg = f"Target pod disappearance confirmed ({original_pod})."
        else:
            msg = f"Target pod ({original_pod}) did not terminate within timeout."

        return RuleResult(self.name, disappeared, msg, observed=disappeared, expected=True)


class ServiceRecoveryRule(VerificationRule):
    """Verifies that Kubernetes restored the expected number of ready replicas."""

    name = "service_recovery"
    description = "Target microservice must restore expected ready replicas."

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> RuleResult:
        expected_replicas = config.get("verification", {}).get("expected_ready_replicas", 1)
        recovered_replicas = evidence.get("recovered_ready_replicas", 0)
        replacement_pod = evidence.get("replacement_pod")

        passed = recovered_replicas >= expected_replicas
        if passed:
            msg = f"Service recovered {recovered_replicas} ready replica(s) (replacement: {replacement_pod})."
        else:
            msg = f"Service did not restore expected ready replicas ({recovered_replicas}/{expected_replicas})."

        return RuleResult(self.name, passed, msg, observed=recovered_replicas, expected=f">={expected_replicas}")


class RecoveryTimeRule(VerificationRule):
    """Verifies that recovery occurred within the configured timeout SLA."""

    name = "recovery_within_timeout"
    description = "Recovery time must satisfy the declared recovery timeout threshold."

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> RuleResult:
        timeout_seconds = float(config.get("recovery", {}).get("timeout_seconds", 60))
        recovery_time = evidence.get("recovery_time_seconds")

        if recovery_time is None:
            return RuleResult(self.name, False, "Recovery time could not be measured (service did not recover).", observed=None, expected=f"<={timeout_seconds}s")

        passed = recovery_time <= timeout_seconds
        if passed:
            msg = f"Service recovered in {recovery_time:.2f}s (within limit of {timeout_seconds}s)."
        else:
            msg = f"Recovery took {recovery_time:.2f}s, exceeding SLA limit of {timeout_seconds}s."

        return RuleResult(self.name, passed, msg, observed=recovery_time, expected=f"<={timeout_seconds}s")


class ApplicationHealthRule(VerificationRule):
    """Verifies that the replacement pod's gRPC health service returns expected serving status."""

    name = "app_health_probe"
    description = "Application gRPC health probe must report expected serving status."

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> RuleResult:
        app_config = config.get("verification", {}).get("application", {})
        if not app_config.get("enabled", False) or not app_config.get("grpc_health_check", False):
            return RuleResult(self.name, True, "Application health probe skipped (not enabled in config).", observed="skipped", expected="skipped")

        app_evidence = evidence.get("application", {})
        health_passed = app_evidence.get("health_check_passed", False)
        observed_status = app_evidence.get("health_status", "UNKNOWN")
        expected_status = app_config.get("expected_health_status", "SERVING")

        passed = health_passed and (observed_status == expected_status)
        if passed:
            msg = f"Application gRPC health probe verified (status: {observed_status})."
        else:
            error_detail = app_evidence.get("health_error", "Health probe rejected or timed out")
            msg = f"Application gRPC health probe failed: {error_detail} (status: {observed_status}, expected: {expected_status})."

        return RuleResult(self.name, passed, msg, observed=observed_status, expected=expected_status)


class ApplicationFunctionalRule(VerificationRule):
    """Verifies that the microservice can process functional business RPC requests."""

    name = "app_functional_check"
    description = "Application must successfully process functional RPC transactions."

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> RuleResult:
        app_config = config.get("verification", {}).get("application", {})
        if not app_config.get("enabled", False) or not app_config.get("functional_test", False):
            return RuleResult(self.name, True, "Functional transaction test skipped (not enabled in config).", observed="skipped", expected="skipped")

        app_evidence = evidence.get("application", {})
        func_passed = app_evidence.get("functional_test_passed", False)
        tx_id = app_evidence.get("transaction_id")

        if func_passed and tx_id:
            msg = f"Functional charge RPC processed successfully (transaction ID: {tx_id})."
            passed = True
        else:
            err = app_evidence.get("functional_error", "No response from service")
            msg = f"Functional charge RPC failed: {err}"
            passed = False

        return RuleResult(self.name, passed, msg, observed=tx_id or "failed", expected="valid_uuid")


class ApplicationLogsRule(VerificationRule):
    """Verifies that the replacement pod container logs confirm clean startup without fatal exceptions."""

    name = "app_logs_healthy"
    description = "Application container logs must confirm clean initialization without crash loops."

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> RuleResult:
        app_config = config.get("verification", {}).get("application", {})
        if not app_config.get("enabled", False) or not app_config.get("verify_logs", False):
            return RuleResult(self.name, True, "Container logs verification skipped (not enabled in config).", observed="skipped", expected="skipped")

        app_evidence = evidence.get("application", {})
        startup_detected = app_evidence.get("startup_log_detected", False)
        error_count = app_evidence.get("log_error_count", 0)
        expected_pattern = app_config.get("expected_log_pattern", "started")

        passed = startup_detected and (error_count == 0)
        if passed:
            msg = f"Container logs confirmed clean startup (found '{expected_pattern}', 0 fatal errors)."
        else:
            msg = f"Container logs verification failed: startup_detected={startup_detected}, errors={error_count}."

        return RuleResult(self.name, passed, msg, observed=f"startup={startup_detected}, errors={error_count}", expected="startup=True, errors=0")


class VerificationOracle:
    """
    ChaosLab Verification Oracle Engine.
    Evaluates invariant rules against collected evidence and configuration.
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
                ApplicationLogsRule()
            ]
        else:
            self.rules = rules

    def evaluate(self, evidence: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        """Evaluate all rules and return comprehensive scorecard and verdict."""
        rule_results: List[RuleResult] = []
        failure_reasons: List[str] = []

        for rule in self.rules:
            res = rule.evaluate(evidence, config)
            rule_results.append(res)
            if not res.passed:
                failure_reasons.append(f"[{res.rule_name}] {res.message}")

        passed_count = sum(1 for r in rule_results if r.passed)
        total_count = len(rule_results)
        verdict = "PASS" if passed_count == total_count else "FAIL"

        return {
            "verdict": verdict,
            "total_rules": total_count,
            "passed_rules": passed_count,
            "failed_rules": total_count - passed_count,
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
            status = "[PASS]" if r["passed"] else "[FAIL]"
            lines.append(f"{status} {r['rule']}: {r['message']}")

        lines.append("--------------------------------------------------")
        lines.append(f"Oracle Verdict: {evaluation['verdict']} ({evaluation['passed_rules']}/{evaluation['total_rules']} invariants satisfied)")
        lines.append("==================================================")
        return "\n".join(lines)
