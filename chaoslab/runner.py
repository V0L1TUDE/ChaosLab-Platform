"""
ChaosLab Unified Experiment Runner & CLI
========================================
Automated experiment discovery, lifecycle orchestration, and test suite execution.
Supports multi-application environments (Online Boutique, Project Platform, LMS),
CI/CD quality gate integration, step summaries, fail-fast policies,
and an interactive Web Dashboard & Control Center.
"""

import argparse
from datetime import datetime, timezone
import glob
import json
import os
import sys
from typing import Any, Dict, List, Optional
import yaml

# Resolve paths
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CHAOSLAB_DIR = os.path.join(BASE_DIR, "chaoslab")
EXPERIMENTS_DIR = os.path.join(CHAOSLAB_DIR, "experiments")
VERIFIER_DIR = os.path.join(CHAOSLAB_DIR, "verifier")
REPORTS_DIR = os.path.join(CHAOSLAB_DIR, "reports")

# Ensure verifier and chaoslab directories are in Python path
for p in [CHAOSLAB_DIR, VERIFIER_DIR, BASE_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from engine import verify_experiment

def discover_experiments() -> List[Dict[str, Any]]:
    """Scan experiments directory for declarative YAML experiment files."""
    experiments = []
    if not os.path.exists(EXPERIMENTS_DIR):
        return []

    yaml_files = glob.glob(os.path.join(EXPERIMENTS_DIR, "*.yaml")) + glob.glob(os.path.join(EXPERIMENTS_DIR, "*.yml"))
    for file_path in yaml_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict) and "name" in data:
                target_dict = data.get("target", {})
                label_dict = target_dict.get("label", {})
                target_str = label_dict.get("app") or label_dict.get("service") or ",".join(f"{k}={v}" for k, v in label_dict.items()) or "unknown"
                ns = target_dict.get("namespace", "default")
                fault_dict = data.get("fault", {})
                recovery_dict = data.get("recovery", {})

                # Support both schemas for application probe and telemetry
                app_dict = data.get("application") or data.get("verification", {}).get("application", {})
                app_check = app_dict.get("enabled", True) if app_dict else False

                tel_dict = data.get("telemetry") or data.get("verification", {}).get("telemetry", {})
                tel_check = tel_dict.get("enabled", False) if tel_dict else False

                # Application group identification
                ns_lower = ns.lower()
                if "project" in ns_lower:
                    app_group = "Project Platform"
                elif "learning" in ns_lower or "lms" in ns_lower:
                    app_group = "LMS Platform"
                elif ns_lower in ["default", "boutique"]:
                    app_group = "Online Boutique"
                else:
                    app_group = data.get("application_group") or ns

                experiments.append({
                    "name": data.get("name"),
                    "app_group": app_group,
                    "target": target_str,
                    "namespace": ns,
                    "fault": fault_dict.get("type", "pod-kill"),
                    "timeout": recovery_dict.get("timeout_seconds", 60),
                    "app_check": "Enabled" if app_check else "Disabled",
                    "telemetry": "Enabled" if tel_check else "Disabled",
                    "file_path": file_path,
                    "rel_path": os.path.relpath(file_path, BASE_DIR)
                })
        except Exception as e:
            print(f"Warning: Failed to load {file_path}: {e}")

    experiments.sort(key=lambda x: (x["app_group"], x["name"]))
    return experiments

def list_experiments():
    """Print formatted table of registered experiments grouped by application."""
    exps = discover_experiments()
    print("========================================================================================================================")
    print("CHAOSLAB REGISTERED EXPERIMENTS (MULTI-APPLICATION TEST SUITE)")
    print("========================================================================================================================")
    if not exps:
        print(f"No experiment files found in {EXPERIMENTS_DIR}")
        print("========================================================================================================================")
        return

    # Header
    print(f"{'Application':<22} {'Experiment Name':<28} {'Target':<18} {'Namespace':<18} {'Fault':<12} {'Timeout':<8} {'App':<7} {'Traces'}")
    print("-" * 120)
    for exp in exps:
        print(f"{exp['app_group']:<22} {exp['name']:<28} {exp['target']:<18} {exp['namespace']:<18} {exp['fault']:<12} {str(exp['timeout']) + 's':<8} {exp['app_check']:<7} {exp['telemetry']}")
    print("========================================================================================================================")
    print(f"Total: {len(exps)} registered experiment(s) across applications")
    print()

def find_experiment_path(name_or_path: str) -> Optional[str]:
    """Resolve an experiment path from a name, partial name, or file path."""
    if os.path.exists(name_or_path):
        return os.path.abspath(name_or_path)

    exps = discover_experiments()
    for exp in exps:
        if exp["name"] == name_or_path or os.path.basename(exp["file_path"]) == name_or_path:
            return exp["file_path"]

    for exp in exps:
        if name_or_path.lower() in exp["name"].lower():
            return exp["file_path"]

    return None

def save_suite_summary(results: List[Dict[str, Any]]) -> Dict[str, str]:
    """Generate and persist aggregated suite summary in JSON and Markdown formats."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    timestamp_str = datetime.now(timezone.utc).isoformat()
    passed_count = sum(1 for r in results if r.get("passed"))
    total_count = len(results)
    all_passed = (passed_count == total_count) and total_count > 0

    suite_data = {
        "timestamp": timestamp_str,
        "total_experiments": total_count,
        "passed_count": passed_count,
        "failed_count": total_count - passed_count,
        "all_passed": all_passed,
        "results": results
    }

    # 1. Save JSON summary
    json_path = os.path.join(REPORTS_DIR, "suite_summary.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(suite_data, f, indent=2)

    # 2. Generate Markdown summary
    verdict_badge = "✅ **SUITE PASSED**" if all_passed else "❌ **SUITE FAILED**"
    md_lines = [
        "# ChaosLab Automated Regression Suite Summary",
        "",
        f"> **Verdict**: {verdict_badge} &bull; **Passed**: `{passed_count}/{total_count}` experiments &bull; **Time (UTC)**: `{timestamp_str}`",
        "",
        "## Experiment Scorecard",
        "",
        "| Application | Experiment | Target | Fault | Recovery Time | p95 Latency | Invariants | Status |",
        "| :--- | :--- | :--- | :--- | :---: | :---: | :---: | :---: |"
    ]

    for r in results:
        status_badge = "✅ PASS" if r.get("passed") else "❌ FAIL"
        rec_time = f"{r.get('recovery_time_seconds')}s" if r.get('recovery_time_seconds') is not None else "N/A"
        tel = r.get("telemetry", {})
        p95 = f"{tel.get('latency_ms', {}).get('p95')}ms" if tel.get("available") and tel.get("traces_count", 0) > 0 else "N/A"
        oracle = r.get("oracle_evaluation", {})
        rules_stat = f"{oracle.get('passed_rules', 0)}/{oracle.get('executed_rules', oracle.get('total_rules', 0))}" if oracle else "N/A"
        app_grp = r.get("application_group", "Microservices")
        md_lines.append(
            f"| **{app_grp}** | `{r.get('name')}` | `{r.get('target')}` | `{r.get('fault', 'pod-kill')}` | `{rec_time}` | `{p95}` | `{rules_stat}` | {status_badge} |"
        )

    md_lines.extend([
        "",
        "---",
        f"*Artifacts generated in `{REPORTS_DIR}`. View individual HTML dashboards for visual recovery timelines.*"
    ])
    md_content = "\n".join(md_lines)

    md_path = os.path.join(REPORTS_DIR, "suite_summary.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    # If running in GitHub Actions, append directly to GITHUB_STEP_SUMMARY
    github_step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if github_step_summary:
        try:
            with open(github_step_summary, "a", encoding="utf-8") as f:
                f.write("\n" + md_content + "\n")
            print("Successfully posted summary to GitHub Actions Job Summary ($GITHUB_STEP_SUMMARY).")
        except Exception as e:
            print(f"Notice: Could not write to GITHUB_STEP_SUMMARY: {e}")

    return {"json": json_path, "markdown": md_path}

def run_single(name_or_path: str = None) -> bool:
    """Execute a single experiment by path or name."""
    if not name_or_path:
        default_file = os.path.join(EXPERIMENTS_DIR, "payment_kill.yaml")
        if os.path.exists(default_file):
            return verify_experiment(default_file)
        exps = discover_experiments()
        if exps:
            return verify_experiment(exps[0]["file_path"])
        print("Error: No experiments available to run.")
        return False

    exp_path = find_experiment_path(name_or_path)
    if not exp_path:
        print(f"Error: Experiment '{name_or_path}' not found.")
        print()
        list_experiments()
        return False

    return verify_experiment(exp_path)

def run_all(fail_fast: bool = False, is_ci: bool = False) -> bool:
    """Execute all discovered experiments sequentially, record suite metrics, and output scorecard."""
    exps = discover_experiments()
    if not exps:
        print("Error: No experiments found to run.")
        return False

    print("====================================================================================================")
    print(f"CHAOSLAB SUITE RUNNER: Executing {len(exps)} experiment(s) across applications (fail-fast: {fail_fast}, CI: {is_ci})...")
    print("====================================================================================================")

    results = []
    for idx, exp in enumerate(exps, 1):
        print()
        print(f">>> Running Experiment [{idx}/{len(exps)}]: {exp['name']} ({exp['app_group']})")
        passed = verify_experiment(exp["file_path"])

        # Attempt to load generated report JSON for rich aggregated metrics
        clean_name = exp["name"].lower().replace("-", "_")
        report_file = os.path.join(REPORTS_DIR, f"{clean_name}_report.json")
        report_details = {}
        if os.path.exists(report_file):
            try:
                with open(report_file, "r", encoding="utf-8") as f:
                    report_details = json.load(f)
            except Exception:
                pass

        item_result = {
            "name": exp["name"],
            "application_group": report_details.get("application_group", exp["app_group"]),
            "target": exp["target"],
            "namespace": exp["namespace"],
            "fault": exp["fault"],
            "passed": passed,
            "recovery_time_seconds": report_details.get("recovery_time_seconds"),
            "telemetry": report_details.get("telemetry", {}),
            "oracle_evaluation": report_details.get("oracle_evaluation", {})
        }
        results.append(item_result)

        if fail_fast and not passed:
            print()
            print(f"FAIL-FAST TRIGGERED: Experiment '{exp['name']}' failed. Aborting remaining suite execution.")
            break

    # Summary
    passed_count = sum(1 for r in results if r["passed"])
    total_count = len(results)
    all_passed = (passed_count == len(exps)) and (len(exps) > 0)

    # Save suite summary JSON & Markdown
    summary_files = save_suite_summary(results)

    print()
    print("====================================================================================================")
    print("CHAOSLAB TEST SUITE SUMMARY")
    print("====================================================================================================")
    for r in results:
        status = "[PASS]" if r["passed"] else "[FAIL]"
        rec_time = f" ({r.get('recovery_time_seconds')}s)" if r.get('recovery_time_seconds') is not None else ""
        print(f"{status} [{r.get('application_group', 'General')}] {r['name']} ({r['target']} in {r.get('namespace', 'default')}){rec_time}")
    print("-" * 100)
    suite_status = "PASS" if all_passed else "FAIL"
    print(f"Suite Verdict: {suite_status} ({passed_count}/{len(exps)} experiments passed)")
    print(f"Suite Summary Reports Generated:")
    print(f"- Markdown Summary: {summary_files['markdown']}")
    print(f"- Structured JSON: {summary_files['json']}")
    print("====================================================================================================")

    return all_passed

def main():
    """Main CLI entrypoint for ChaosLab runner."""
    parser = argparse.ArgumentParser(description="ChaosLab Unified Experiment Runner & CLI")
    subparsers = parser.add_subparsers(dest="command")

    # list
    subparsers.add_parser("list", help="List all registered experiments")

    # run-all
    run_all_parser = subparsers.add_parser("run-all", help="Execute full experiment test suite")
    run_all_parser.add_argument("--fail-fast", action="store_true", help="Abort suite immediately on first failure")
    run_all_parser.add_argument("--ci", action="store_true", help="Run in CI mode with strict exit code and summary exports")

    # run
    run_parser = subparsers.add_parser("run", help="Run a specific experiment by name or partial match")
    run_parser.add_argument("name", nargs="?", default=None, help="Experiment name or path")

    # dashboard
    dash_parser = subparsers.add_parser("dashboard", help="Start the interactive ChaosLab Web Dashboard & Control Center")
    dash_parser.add_argument("--port", type=int, default=8080, help="Port to bind dashboard server (default: 8080)")
    dash_parser.add_argument("--no-browser", action="store_true", help="Do not open web browser automatically")

    args, unknown = parser.parse_known_args()

    command = args.command if args.command else (args.name if hasattr(args, "name") and args.name else None)

    if not command:
        if unknown and unknown[0] in ["all", "run-all"]:
            fail_fast = "--fail-fast" in unknown
            is_ci = "--ci" in unknown
            success = run_all(fail_fast=fail_fast, is_ci=is_ci)
            sys.exit(0 if success else 1)
        if unknown and unknown[0] in ["dashboard", "ui", "web"]:
            port = 8080
            for u in unknown:
                if u.isdigit():
                    port = int(u)
            from dashboard.server import run_server
            import threading
            import webbrowser
            threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{port}")).start()
            run_server(port=port)
            sys.exit(0)
        # Default action: run payment_kill
        success = run_single()
        sys.exit(0 if success else 1)

    if command in ["list", "ls"]:
        list_experiments()
    elif command in ["run-all", "all"]:
        fail_fast = getattr(args, "fail_fast", False) or "--fail-fast" in unknown
        is_ci = getattr(args, "ci", False) or "--ci" in unknown
        success = run_all(fail_fast=fail_fast, is_ci=is_ci)
        sys.exit(0 if success else 1)
    elif command == "dashboard":
        port = getattr(args, "port", 8080)
        no_browser = getattr(args, "no_browser", False)
        from dashboard.server import run_server
        import threading
        import webbrowser
        if not no_browser:
            threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{port}")).start()
        run_server(port=port)
        sys.exit(0)
    elif command == "run":
        target = args.name
        success = run_single(target)
        sys.exit(0 if success else 1)
    else:
        # Treat argument as an experiment name directly
        success = run_single(command)
        sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
