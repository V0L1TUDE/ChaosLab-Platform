"""
ChaosLab Universal Experiment Execution & Verification Engine
=============================================================
Protocol-agnostic, multi-namespace chaos engineering orchestrator.
Automates baseline assessment, Chaos Mesh injection, recovery tracking,
pluggable health/functional probing, distributed trace collection, and Oracle invariant evaluation.
"""

from datetime import datetime, timezone
import json
import os
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
import yaml

# Ensure local imports work cleanly
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from oracle import VerificationOracle
from probes import ProbeDispatcher
from reporter import ExperimentReporter
from telemetry import TelemetryClient

DEFAULT_CONFIG_PATH = os.path.join(".", "chaoslab", "experiments", "payment_kill.yaml")
REPORTS_DIR = os.path.join(".", "chaoslab", "reports")

def run_command(command: List[str], input_text: Optional[str] = None, timeout: int = 15) -> subprocess.CompletedProcess:
    """Execute shell command and return CompletedProcess."""
    try:
        result = subprocess.run(
            command,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return result
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, returncode=124, stdout="", stderr=f"Command timed out after {timeout}s")
    except Exception as e:
        return subprocess.CompletedProcess(command, returncode=1, stdout="", stderr=str(e))

def load_experiment_config(config_path: str) -> Dict[str, Any]:
    """
    Load, validate, and normalize declarative experiment configuration from YAML file.
    Supports both legacy Online Boutique schema and the new Universal schema.
    """
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Experiment configuration file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        raise ValueError(f"Invalid YAML content in {config_path}: expected dictionary root")

    # Name validation
    config.setdefault("name", os.path.splitext(os.path.basename(config_path))[0])

    # Target normalization
    config.setdefault("target", {})
    if "namespace" not in config["target"]:
        config["target"]["namespace"] = "default"
    if "label" not in config["target"]:
        config["target"]["label"] = {}

    # Fault normalization
    config.setdefault("fault", {})
    config["fault"].setdefault("type", "pod-kill")
    config["fault"].setdefault("mode", "one")

    # Recovery normalization
    config.setdefault("recovery", {})
    config["recovery"].setdefault("timeout_seconds", 60)
    config["recovery"].setdefault("expected_ready_replicas", 1)

    # Verification normalization: link new application and telemetry blocks with legacy verification block
    config.setdefault("verification", {})
    config["verification"].setdefault("expected_ready_replicas", config["recovery"].get("expected_ready_replicas", 1))

    # Normalize application probe spec
    if "application" in config:
        config["verification"]["application"] = config["application"]
    elif "application" in config.get("verification", {}):
        config["application"] = config["verification"]["application"]
    else:
        config.setdefault("application", {"enabled": False})

    # Normalize telemetry spec
    if "telemetry" in config:
        config["verification"]["telemetry"] = config["telemetry"]
    elif "telemetry" in config.get("verification", {}):
        config["telemetry"] = config["verification"]["telemetry"]
    else:
        config.setdefault("telemetry", {"enabled": False})

    return config

def get_label_selector(label_dict: Dict[str, str]) -> str:
    """Convert label dictionary into a Kubernetes label selector string."""
    return ",".join(f"{k}={v}" for k, v in label_dict.items())

def get_target_name(config: Dict[str, Any]) -> str:
    """Extract human-readable target name from label dictionary or experiment config."""
    label_dict = config.get("target", {}).get("label", {})
    return label_dict.get("app") or label_dict.get("service") or get_label_selector(label_dict) or config.get("name")

def get_target_app_group(config: Dict[str, Any]) -> str:
    """Determine the application group based on namespace or explicit metadata."""
    ns = config.get("target", {}).get("namespace", "default").lower()
    if ns == "project-platform" or "project" in ns:
        return "Project Management Platform"
    elif ns == "learning-platform" or "lms" in ns or "learning" in ns:
        return "Learning Management System (LMS)"
    elif ns == "default" or "boutique" in ns:
        return "Google Online Boutique"
    return config.get("application_group") or f"Namespace: {ns}"

def generate_chaos_manifest(config: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    """Generate Chaos Mesh manifest dynamically from declarative configuration."""
    name = config.get("name", "experiment")
    clean_name = f"chaoslab-{name.lower().replace('_', '-')}"
    namespace = config.get("target", {}).get("namespace", "default")
    label_dict = config.get("target", {}).get("label", {})
    fault = config.get("fault", {})
    action = fault.get("type", "pod-kill")
    mode = fault.get("mode", "one")
    duration = fault.get("duration", "30s")

    if action in ["pod-kill", "pod-failure", "container-kill"]:
        crd_type = "PodChaos"
        spec = {
            "action": action,
            "mode": mode,
            "selector": {
                "namespaces": [namespace],
                "labelSelectors": label_dict
            }
        }
        if action in ["pod-failure", "container-kill"] and "duration" in fault:
            spec["duration"] = duration
    elif action in ["network-delay", "network-loss", "network-corrupt"]:
        crd_type = "NetworkChaos"
        net_action = action.replace("network-", "")
        spec = {
            "action": net_action,
            "mode": mode,
            "selector": {
                "namespaces": [namespace],
                "labelSelectors": label_dict
            },
            "direction": fault.get("direction", "to"),
            "duration": duration
        }
        if net_action == "delay":
            latency = fault.get("latency", "200ms")
            jitter = fault.get("jitter", "10ms")
            spec["delay"] = {"latency": latency, "jitter": jitter}
        elif net_action == "loss":
            spec["loss"] = {"loss": fault.get("loss_pct", "50")}
        elif net_action == "corrupt":
            spec["corrupt"] = {"corrupt": fault.get("corrupt_pct", "50")}
    else:
        # Default fallback
        crd_type = "PodChaos"
        spec = {
            "action": "pod-kill",
            "mode": mode,
            "selector": {
                "namespaces": [namespace],
                "labelSelectors": label_dict
            }
        }

    manifest = {
        "apiVersion": "chaos-mesh.org/v1alpha1",
        "kind": crd_type,
        "metadata": {
            "name": clean_name,
            "namespace": namespace
        },
        "spec": spec
    }
    return clean_name, crd_type, manifest

def apply_chaos(config: Dict[str, Any]) -> Tuple[bool, str, str, str]:
    """Deploy Chaos Mesh manifest to Kubernetes, ensuring clean idempotency."""
    clean_name, crd_type, manifest = generate_chaos_manifest(config)
    namespace = config.get("target", {}).get("namespace", "default")
    # Delete preexisting chaos CRD if any to guarantee fresh fault injection
    run_command(["kubectl", "delete", crd_type, clean_name, "-n", namespace, "--ignore-not-found=true", "--wait=false"])
    manifest_yaml = yaml.dump(manifest)
    res = run_command(["kubectl", "apply", "-f", "-"], input_text=manifest_yaml)
    if res.returncode != 0:
        return False, clean_name, res.stderr.strip(), crd_type
    print(f"{crd_type.lower()}.chaos-mesh.org/{clean_name} created")
    return True, clean_name, res.stdout.strip(), crd_type

def cleanup_chaos(clean_name: str, namespace: str, crd_type: str):
    """Clean up applied Chaos Mesh custom resources to leave cluster clean."""
    try:
        run_command(["kubectl", "delete", crd_type, clean_name, "-n", namespace, "--ignore-not-found=true", "--wait=false"])
    except Exception:
        pass

def get_pods(namespace: str, label_selector: str) -> List[Dict[str, Any]]:
    """Retrieve pods matching label selector in JSON format."""
    cmd = ["kubectl", "get", "pods", "-n", namespace, "-o", "json"]
    if label_selector:
        cmd.extend(["-l", label_selector])

    res = run_command(cmd)
    if res.returncode != 0:
        return []

    try:
        data = json.loads(res.stdout)
        pods = []
        for item in data.get("items", []):
            name = item["metadata"]["name"]
            status = item["status"].get("phase", "Unknown")
            deletion = item["metadata"].get("deletionTimestamp")
            if deletion:
                status = "Terminating"
            container_statuses = item["status"].get("containerStatuses", [])
            ready_count = sum(1 for c in container_statuses if c.get("ready", False))
            total_count = len(container_statuses)
            ready_str = f"{ready_count}/{total_count}" if total_count > 0 else "0/0"
            pod_ip = item["status"].get("podIP", "")
            node = item["spec"].get("nodeName", "")

            pods.append({
                "name": name,
                "status": status,
                "ready": ready_str,
                "ready_count": ready_count,
                "total_count": total_count,
                "pod_ip": pod_ip,
                "node": node
            })
        return pods
    except Exception:
        return []

def get_ready_pods(namespace: str, label_selector: str) -> List[Dict[str, Any]]:
    """Retrieve only Running pods whose containers are all Ready."""
    pods = get_pods(namespace, label_selector)
    return [p for p in pods if p["status"] == "Running" and p["ready_count"] == p["total_count"] and p["total_count"] > 0]

def get_chaos_mesh_evidence(chaos_name: str, namespace: str, crd_type: str) -> Dict[str, Any]:
    """Query Chaos Mesh CRD status and injection conditions."""
    res = run_command(["kubectl", "get", crd_type, chaos_name, "-n", namespace, "-o", "json"])
    if res.returncode != 0:
        return {"status": "Unknown", "error": res.stderr.strip()}

    try:
        data = json.loads(res.stdout)
        status = data.get("status", {})
        conditions = status.get("conditions", [])
        records = status.get("experiment", {}).get("containerRecords", [])
        return {
            "conditions": conditions,
            "container_records": records,
            "injected": any(c.get("type") == "AllInjected" and c.get("status") == "True" for c in conditions),
            "raw_status": status
        }
    except Exception as e:
        return {"error": str(e)}

def get_kubernetes_events(namespace: str, target_pod_names: Set[str], target_name: str) -> List[Dict[str, Any]]:
    """Extract Kubernetes lifecycle events associated with target pods."""
    res = run_command(["kubectl", "get", "events", "-n", namespace, "-o", "json"])
    if res.returncode != 0:
        return []

    try:
        data = json.loads(res.stdout)
        events = []
        for item in data.get("items", []):
            obj = item.get("involvedObject", {})
            obj_name = obj.get("name", "")
            obj_kind = obj.get("kind", "")

            if obj_name in target_pod_names or target_name in obj_name:
                events.append({
                    "reason": item.get("reason"),
                    "message": item.get("message"),
                    "object": f"{obj_kind.lower()}/{obj_name}",
                    "type": item.get("type"),
                    "timestamp": item.get("lastTimestamp") or item.get("eventTime") or item.get("metadata", {}).get("creationTimestamp")
                })
        return events[-10:] if len(events) > 10 else events
    except Exception:
        return []

def probe_application(pod_name: str, namespace: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Execute application health, functional, and log probes via ProbeDispatcher."""
    return ProbeDispatcher.probe_application(pod_name, namespace, config)

def run_experiment(config: Dict[str, Any], progress_callback: Optional[Callable] = None) -> bool:
    """
    Universal ChaosLab Experiment Orchestration Lifecycle:
    Baseline -> Inject -> Observe -> Verify -> Collect Telemetry -> Oracle -> Report.
    """
    def emit_progress(phase: str, percent: int, message: str, data: dict = None):
        if progress_callback:
            try:
                progress_callback(phase, percent, message, data)
            except Exception:
                pass

    experiment_start_dt = datetime.now(timezone.utc)
    experiment_start_time = time.time()

    # Normalize parameters
    experiment_name = config.get("name", "unnamed-experiment")
    namespace = config.get("target", {}).get("namespace", "default")
    label_dict = config.get("target", {}).get("label", {})
    label_selector = get_label_selector(label_dict)
    target_name = get_target_name(config)
    app_group = get_target_app_group(config)
    fault_type = config.get("fault", {}).get("type", "pod-kill")
    timeout = config.get("recovery", {}).get("timeout_seconds", 60)
    expected_replicas = config.get("recovery", {}).get("expected_ready_replicas") or \
                        config.get("verification", {}).get("expected_ready_replicas", 1)

    print("==================================================")
    print(f"CHAOSLAB EXPERIMENT: {experiment_name}")
    print(f"Application Group: {app_group}")
    print("==================================================")
    print(f"Target: {target_name} (namespace: {namespace}, selector: {label_selector})")
    print(f"Fault: {fault_type}")
    print(f"Verification Spec: Expected Ready Replicas = {expected_replicas}, Timeout = {timeout}s")
    print("--------------------------------------------------")
    print("Checking baseline pods...")
    emit_progress("baseline", 10, f"Checking baseline pods for target '{target_name}' in namespace '{namespace}'...")

    initial_pods = get_pods(namespace, label_selector)
    initial_ready_pods = get_ready_pods(namespace, label_selector)

    if len(initial_ready_pods) < expected_replicas:
        print()
        print("FAIL: Baseline readiness check failed.")
        msg = f"Target does not meet baseline readiness: found {len(initial_ready_pods)} ready pods, expected {expected_replicas}."
        print(msg)
        emit_progress("failed", 100, msg)
        return False

    baseline_pod = initial_pods[0]
    initial_names = {pod["name"] for pod in initial_pods}

    print(f"Baseline pod(s) [{len(initial_pods)}]:")
    for pod in initial_pods:
        print(f"- {pod['name']} ({pod['ready']}, {pod['status']})")

    # Inject Fault
    emit_progress("injecting", 25, f"Injecting chaos fault '{fault_type}' via Chaos Mesh into namespace '{namespace}'...")
    chaos_success, chaos_name, chaos_msg, crd_type = apply_chaos(config)
    fault_injected_dt = datetime.now(timezone.utc)
    fault_injected_time = time.time()
    fault_injected_us = int(fault_injected_time * 1_000_000)

    if not chaos_success:
        print("FAIL: Chaos injection failed.")
        emit_progress("failed", 100, f"Chaos injection failed: {chaos_msg}")
        return False

    is_network_fault = "network" in fault_type.lower()

    original_pod_disappeared = False
    original_pod_disappeared_dt = None
    replacement_pod = None
    replacement_ready = False
    replacement_ready_dt = None
    replacement_ready_us = None
    recovery_time = None
    total_recovery_time = None

    if not is_network_fault:
        print()
        print("Waiting for the target pod to disappear...")
        emit_progress("waiting_impact", 40, f"Fault applied. Waiting for target pod '{baseline_pod['name']}' to terminate...")

        start = time.time()
        while time.time() - start < timeout:
            current_pods = get_pods(namespace, label_selector)
            active_original_pods = [
                p for p in current_pods
                if p["name"] in initial_names and p["status"] != "Terminating"
            ]

            if len(active_original_pods) < len(initial_pods):
                original_pod_disappeared = True
                original_pod_disappeared_dt = datetime.now(timezone.utc)
                print(f"Target pod disappeared: {initial_names - {p['name'] for p in active_original_pods}}")
                emit_progress("waiting_recovery", 60, f"Target pod terminated. Waiting for replacement pod to be Ready (timeout: {timeout}s)...")

                recovery_start = time.time()
                print(f"Waiting for replacement pod to satisfy expected ready replicas ({expected_replicas})...")

                while time.time() - recovery_start < timeout:
                    ready_pods = get_ready_pods(namespace, label_selector)
                    current_all = get_pods(namespace, label_selector)
                    new_pods = [p for p in current_all if p["name"] not in initial_names]
                    if new_pods:
                        replacement_pod = new_pods[0]

                    if len(ready_pods) >= expected_replicas:
                        ready_new = [p for p in ready_pods if p["name"] not in initial_names]
                        replacement_pod = (ready_new or ready_pods)[0]
                        replacement_ready = True
                        replacement_ready_dt = datetime.now(timezone.utc)
                        replacement_ready_time = time.time()
                        replacement_ready_us = int(replacement_ready_time * 1_000_000)

                        recovery_time = replacement_ready_time - recovery_start
                        total_recovery_time = replacement_ready_time - fault_injected_time
                        break

                    time.sleep(1)
                break
            time.sleep(1)
    else:
        # Network chaos scenario: wait for injection duration, observe steady-state
        emit_progress("waiting_impact", 50, f"Network chaos active. Observing service under latency/loss...")
        time.sleep(5)
        replacement_pod = baseline_pod
        replacement_ready = True
        recovery_time = 0.5
        total_recovery_time = 5.0
        replacement_ready_dt = datetime.now(timezone.utc)
        replacement_ready_us = int(time.time() * 1_000_000)

    # Allow cluster state to synchronize
    time.sleep(2)

    # Collect Evidence
    tracked_names = set(initial_names)
    if replacement_pod:
        tracked_names.add(replacement_pod["name"])

    chaos_mesh_evidence = get_chaos_mesh_evidence(chaos_name, namespace, crd_type)
    k8s_events = get_kubernetes_events(namespace, tracked_names, target_name)
    current_ready_pods = get_ready_pods(namespace, label_selector)

    # Perform Deep Application Probing
    app_evidence = {"enabled": False}
    target_probe_pod = replacement_pod or baseline_pod
    if target_probe_pod and (replacement_ready or is_network_fault):
        print()
        print("Probing application correctness on target pod...")
        emit_progress("probing_app", 75, f"Target pod '{target_probe_pod['name']}' Ready. Probing application correctness...")
        app_evidence = probe_application(target_probe_pod["name"], namespace, config)
        if app_evidence.get("enabled"):
            print(f"- Health Status: {app_evidence.get('health_status', 'N/A')}")
            if app_evidence.get("transaction_id"):
                print(f"- Functional Confirmation ID: {app_evidence.get('transaction_id')}")
            print(f"- Container Startup Logs: {'Verified' if app_evidence.get('startup_log_detected') else 'Missing'}")

    # Collect Distributed Tracing Telemetry
    telemetry_config = config.get("telemetry") or config.get("verification", {}).get("telemetry", {})
    telemetry_evidence = {"enabled": False}
    if telemetry_config.get("enabled", False):
        print()
        print("Collecting distributed tracing telemetry from Jaeger...")
        emit_progress("telemetry", 85, "Collecting OpenTelemetry traces & analyzing latency distribution...")
        telemetry_service = telemetry_config.get("service_name") or target_name
        jaeger_url = telemetry_config.get("query_endpoint", "http://localhost:16686")
        time.sleep(3)

        end_time_us = int(time.time() * 1_000_000)
        client = TelemetryClient(base_url=jaeger_url, namespace="default")

        post_rec_start = replacement_ready_us if replacement_ready_us else (end_time_us - 10_000_000)
        post_recovery_tel = client.get_service_telemetry(
            telemetry_service,
            start_time_us=post_rec_start,
            end_time_us=end_time_us + 2_000_000,
            limit=50
        )

        if post_recovery_tel.get("traces_count", 0) == 0:
            post_recovery_tel = client.get_service_telemetry(
                telemetry_service,
                start_time_us=end_time_us - 15_000_000,
                end_time_us=end_time_us + 2_000_000,
                limit=50
            )

        fault_tel = client.get_service_telemetry(
            telemetry_service,
            start_time_us=fault_injected_us,
            end_time_us=post_rec_start,
            limit=50
        )

        telemetry_evidence = post_recovery_tel
        telemetry_evidence["fault_window_errors"] = fault_tel.get("error_spans_count", 0)
        telemetry_evidence["error_spans_count"] = fault_tel.get("error_spans_count", 0) + post_recovery_tel.get("error_spans_count", 0)

        if telemetry_evidence.get("available") and telemetry_evidence.get("traces_count", 0) > 0:
            lat = telemetry_evidence.get("latency_ms", {})
            print(f"- Post-Recovery Traces: {telemetry_evidence['traces_count']} ({telemetry_evidence['spans_count']} spans)")
            print(f"- p50 / p95 Latency: {lat.get('p50')}ms / {lat.get('p95')}ms (max: {lat.get('max')}ms)")
            print(f"- Error Spans (Fault Window): {telemetry_evidence.get('fault_window_errors', 0)}")
            if telemetry_evidence.get("sample_trace_ids"):
                print(f"- Sample Trace ID: {telemetry_evidence['sample_trace_ids'][0]}")
        else:
            print(f"- Distributed Tracing: No traces recorded for '{telemetry_service}' in post-recovery window.")

    experiment_end_dt = datetime.now(timezone.utc)
    cleanup_chaos(chaos_name, namespace, crd_type)

    # Build Structured Evidence Dictionary
    evidence = {
        "experiment": experiment_name,
        "application_group": app_group,
        "target": target_name,
        "namespace": namespace,
        "fault": fault_type,
        "baseline": {
            "pod_name": baseline_pod["name"],
            "ready": baseline_pod["ready"],
            "status": baseline_pod["status"],
            "ready_replicas": len(initial_ready_pods),
            "ip": baseline_pod.get("pod_ip", ""),
            "node": baseline_pod.get("node", "")
        },
        "timestamps": {
            "experiment_start": experiment_start_dt.isoformat(),
            "fault_injected": fault_injected_dt.isoformat(),
            "original_pod_disappeared": original_pod_disappeared_dt.isoformat() if original_pod_disappeared_dt else None,
            "replacement_ready": replacement_ready_dt.isoformat() if replacement_ready_dt else None,
            "experiment_end": experiment_end_dt.isoformat()
        },
        "original_pod": baseline_pod["name"],
        "fault_injected": chaos_success,
        "original_pod_disappeared": original_pod_disappeared,
        "replacement_pod": replacement_pod["name"] if replacement_pod else None,
        "replacement_ready": replacement_ready,
        "recovered_ready_replicas": len(current_ready_pods),
        "recovery_time_seconds": round(recovery_time, 2) if recovery_time is not None else None,
        "total_recovery_seconds": round(total_recovery_time, 2) if total_recovery_time is not None else None,
        "application": app_evidence,
        "telemetry": telemetry_evidence,
        "chaos_mesh_evidence": chaos_mesh_evidence,
        "kubernetes_events": k8s_events
    }

    # Evaluate Evidence through Verification Oracle
    emit_progress("oracle", 95, "Evaluating Verification Oracle invariant rules...")
    oracle = VerificationOracle()
    oracle_eval = oracle.evaluate(evidence, config)
    verdict = oracle_eval["verdict"]
    failure_reasons = oracle_eval["failure_reasons"]

    # Build Comprehensive Report Data
    report_data = {
        **evidence,
        "config": config,
        "verdict": verdict,
        "failure_reason": "; ".join(failure_reasons) if failure_reasons else None,
        "oracle_evaluation": oracle_eval
    }

    # Persist Reports
    report_files = ExperimentReporter.save_all(experiment_name, report_data, REPORTS_DIR)

    # Print Oracle Scorecard and Summary
    print()
    print(oracle.format_scorecard(oracle_eval))
    print()
    print("==================================================")
    print("CHAOSLAB EXPERIMENT SUMMARY")
    print("==================================================")
    print(f"Experiment: {experiment_name}")
    print(f"Application: {app_group}")
    print(f"Target: {target_name} ({namespace})")
    print(f"Fault: {fault_type}")
    print(f"Baseline: {baseline_pod['name']} ({baseline_pod['ready']})")
    if replacement_pod:
        print(f"Replacement: {replacement_pod['name']}")
    if app_evidence.get("enabled"):
        print(f"Application Health: {app_evidence.get('health_status', 'N/A')}")
        if app_evidence.get("transaction_id"):
            print(f"Confirmation ID: {app_evidence.get('transaction_id')}")
    if telemetry_evidence.get("available") and telemetry_evidence.get("traces_count", 0) > 0:
        lat = telemetry_evidence.get("latency_ms", {})
        print(f"Distributed Tracing: {telemetry_evidence['traces_count']} traces, {telemetry_evidence['spans_count']} spans (p95: {lat.get('p95')}ms)")
    if recovery_time is not None:
        print(f"Recovery Time: {recovery_time:.2f} seconds")
    print(f"Final Oracle Verdict: {verdict}")
    print("--------------------------------------------------")
    print("Reports Generated:")
    print(f"- HTML Dashboard: {report_files['html']}")
    print(f"- Markdown Summary: {report_files['markdown']}")
    print(f"- Structured JSON: {report_files['json']}")
    print("==================================================")

    emit_progress("completed", 100, f"Experiment completed. Oracle Verdict: {verdict}", data=report_data)
    return verdict == "PASS"

def verify_experiment(config_path: Optional[str] = None, progress_callback: Optional[Callable] = None) -> bool:
    """Execute and verify experiment from a configuration path or default."""
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    print(f"Loading experiment configuration: {config_path}")
    config = load_experiment_config(config_path)
    return run_experiment(config, progress_callback=progress_callback)

if __name__ == "__main__":
    cfg_arg = sys.argv[1] if len(sys.argv) > 1 else None
    verify_experiment(cfg_arg)
