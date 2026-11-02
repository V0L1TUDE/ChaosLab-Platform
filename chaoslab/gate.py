"""
ChaosLab Automated Quality Gate & Release Policy Engine
======================================================
Evaluates test suite outcomes against organizational resilience SLAs,
latency policies, and stability invariants to produce an automated
release gate verdict (APPROVED / REJECTED) with deterministic exit codes.
"""

import argparse
import glob
import json
import os
import sys
from typing import Any, Dict, List, Tuple

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPORTS_DIR = os.path.join(BASE_DIR, "chaoslab", "reports")

class PolicyResult:
    """Represents the outcome of a single organizational quality gate policy."""

    def __init__(self, name: str, passed: bool, message: str, observed: Any, threshold: Any):
        self.name = name
        self.passed = passed
        self.message = message
        self.observed = observed
        self.threshold = threshold

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy": self.name,
            "passed": self.passed,
            "message": self.message,
            "observed": self.observed,
            "threshold": self.threshold
        }

class QualityGate:
    """Evaluates suite reports against resilience and stability policies."""

    def __init__(
        self,
        max_recovery_time_s: float = 45.0,
        max_p95_ms: float = 1000.0,
        reports_dir: str = REPORTS_DIR
    ):
        self.max_recovery_time_s = max_recovery_time_s
        self.max_p95_ms = max_p95_ms
        self.reports_dir = reports_dir

    def load_reports(self) -> List[Dict[str, Any]]:
        """Load all individual experiment reports from the reports directory."""
        reports = []
        if not os.path.exists(self.reports_dir):
            return []

        pattern = os.path.join(self.reports_dir, "*_report.json")
        for filepath in glob.glob(pattern):
            # Skip suite summary json itself
            if os.path.basename(filepath) == "suite_summary.json":
                continue
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.loads(f.read())
                    reports.append(data)
            except Exception as e:
                print(f"Warning: Failed to parse report {filepath}: {e}")

        reports.sort(key=lambda x: x.get("experiment", ""))
        return reports

    def evaluate(self) -> Tuple[bool, List[PolicyResult], Dict[str, Any]]:
        """Evaluate all 4 organizational release policies against latest test outcomes."""
        reports = self.load_reports()
        if not reports:
            res = PolicyResult(
                name="reports_available",
                passed=False,
                message="No experiment report files found to evaluate.",
                observed=0,
                threshold=">0"
            )
            return False, [res], {"total_reports": 0}

        policies: List[PolicyResult] = []

        # Policy 1: Zero Regression Policy (All Oracle Invariants across all experiments must pass)
        total_reports = len(reports)
        passed_reports = sum(1 for r in reports if r.get("verdict") == "PASS")
        all_passed = (passed_reports == total_reports)
        policies.append(PolicyResult(
            name="zero_regression",
            passed=all_passed,
            message=f"{passed_reports}/{total_reports} experiment(s) passed all Oracle invariants.",
            observed=f"{passed_reports}/{total_reports}",
            threshold=f"{total_reports}/{total_reports}"
        ))

        # Policy 2: Maximum Recovery Time SLA Policy
        recovery_times = [
            r.get("recovery_time_seconds") for r in reports
            if r.get("recovery_time_seconds") is not None
        ]
        max_observed_rec = max(recovery_times) if recovery_times else 0.0
        rec_passed = max_observed_rec <= self.max_recovery_time_s
        violating_services = [
            f"{r.get('target')} ({r.get('recovery_time_seconds')}s)"
            for r in reports
            if r.get("recovery_time_seconds") and r.get("recovery_time_seconds") > self.max_recovery_time_s
        ]
        rec_msg = (
            f"Max recovery latency {max_observed_rec:.2f}s satisfied SLA (<={self.max_recovery_time_s:.1f}s)."
            if rec_passed else
            f"Recovery latency SLA violation on: {', '.join(violating_services)} (threshold: <={self.max_recovery_time_s:.1f}s)."
        )
        policies.append(PolicyResult(
            name="max_recovery_sla",
            passed=rec_passed,
            message=rec_msg,
            observed=f"{max_observed_rec:.2f}s",
            threshold=f"<={self.max_recovery_time_s:.1f}s"
        ))

        # Policy 3: Telemetry Latency SLA Policy (Post-recovery p95 latency)
        p95_latencies = []
        violating_telemetry = []
        for r in reports:
            tel = r.get("telemetry", {})
            if tel.get("available") and tel.get("traces_count", 0) > 0:
                p95 = tel.get("latency_ms", {}).get("p95", 0.0)
                p95_latencies.append(p95)
                if p95 > self.max_p95_ms:
                    violating_telemetry.append(f"{r.get('target')} (p95: {p95}ms)")

        max_observed_p95 = max(p95_latencies) if p95_latencies else 0.0
        tel_passed = len(violating_telemetry) == 0
        tel_msg = (
            f"Max post-recovery p95 latency {max_observed_p95:.2f}ms satisfied SLA (<={self.max_p95_ms:.1f}ms)."
            if tel_passed else
            f"Telemetry latency SLA violation on: {', '.join(violating_telemetry)} (threshold: <={self.max_p95_ms:.1f}ms)."
        )
        policies.append(PolicyResult(
            name="telemetry_latency_sla",
            passed=tel_passed,
            message=tel_msg,
            observed=f"{max_observed_p95:.2f}ms" if p95_latencies else "N/A",
            threshold=f"<={self.max_p95_ms:.1f}ms"
        ))

        # Policy 4: Zero Fatal Panics & Crash Loops
        total_fatal_errors = sum(
            r.get("application", {}).get("log_error_count", 0) for r in reports
        )
        panics_passed = total_fatal_errors == 0
        panics_msg = (
            "Zero fatal panics or crash loops detected across all microservices."
            if panics_passed else
            f"Detected {total_fatal_errors} fatal container crash errors in application logs."
        )
        policies.append(PolicyResult(
            name="zero_fatal_panics",
            passed=panics_passed,
            message=panics_msg,
            observed=total_fatal_errors,
            threshold=0
        ))

        gate_passed = all(p.passed for p in policies)
        summary = {
            "total_reports": total_reports,
            "passed_reports": passed_reports,
            "max_recovery_seconds": max_observed_rec,
            "max_p95_ms": max_observed_p95,
            "gate_passed": gate_passed
        }

        return gate_passed, policies, summary

    def format_scorecard(self, policies: List[PolicyResult], gate_passed: bool) -> str:
        """Format terminal-friendly Quality Gate scorecard."""
        lines = [
            "====================================================================================================",
            "CHAOSLAB REGRESSION QUALITY GATE",
            "===================================================================================================="
        ]

        for p in policies:
            status = "[PASS]" if p.passed else "[FAIL]"
            lines.append(f"{status} {p.name}: {p.message}")

        lines.append("----------------------------------------------------------------------------------------------------")
        verdict = "APPROVED (All policies satisfied)" if gate_passed else "REJECTED (One or more policies violated)"
        lines.append(f"Quality Gate Verdict: {verdict}")
        lines.append("====================================================================================================")
        return "\n".join(lines)


def main():
    """CLI entrypoint for ChaosLab Quality Gate."""
    parser = argparse.ArgumentParser(description="ChaosLab Automated Quality Gate & Release Policy Engine")
    subparsers = parser.add_subparsers(dest="command")

    check_parser = subparsers.add_parser("check", help="Evaluate Quality Gate policies against latest reports")
    check_parser.add_argument("--max-recovery-time", type=float, default=45.0, help="Maximum allowed recovery time in seconds (default: 45.0)")
    check_parser.add_argument("--max-p95", type=float, default=1000.0, help="Maximum allowed p95 latency in ms (default: 1000.0)")
    check_parser.add_argument("--reports-dir", type=str, default=REPORTS_DIR, help="Path to reports directory")

    args = parser.parse_args()

    # Default to check if command omitted
    max_rec = getattr(args, "max_recovery_time", 45.0)
    max_p95 = getattr(args, "max_p95", 1000.0)
    rep_dir = getattr(args, "reports_dir", REPORTS_DIR)

    gate = QualityGate(max_recovery_time_s=max_rec, max_p95_ms=max_p95, reports_dir=rep_dir)
    passed, policies, summary = gate.evaluate()
    print(gate.format_scorecard(policies, passed))

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
