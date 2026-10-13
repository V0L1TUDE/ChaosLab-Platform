import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
import yaml

# Ensure verifier directory is in Python path for local module imports
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from oracle import VerificationOracle

DEFAULT_CONFIG_PATH = os.path.join(".", "chaoslab", "experiments", "payment_kill.yaml")
REPORTS_DIR = os.path.join(".", "chaoslab", "reports")

def run_command(command, input_text=None):
    """Execute shell command and return CompletedProcess."""
    result = subprocess.run(
        command,
        input=input_text,
        capture_output=True,
        text=True
    )
    return result

def load_experiment_config(config_path):
    """Load and validate declarative experiment configuration from YAML file."""
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Experiment configuration file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Apply defaults if fields are omitted
    config.setdefault("name", "unnamed-experiment")
    config.setdefault("target", {})
    config["target"].setdefault("namespace", "default")
    config["target"].setdefault("label", {})

    config.setdefault("fault", {})
    config["fault"].setdefault("type", "pod-kill")
    config["fault"].setdefault("mode", "one")

    config.setdefault("recovery", {})
    config["recovery"].setdefault("timeout_seconds", 60)

    config.setdefault("verification", {})
    config["verification"].setdefault("expected_ready_replicas", 1)

    return config

def get_label_selector(label_dict):
    """Convert label dictionary into a Kubernetes label selector string."""
    return ",".join(f"{k}={v}" for k, v in label_dict.items())

def get_target_name(config):
    """Extract human-readable target name from label dictionary or experiment config."""
    label_dict = config.get("target", {}).get("label", {})
    return label_dict.get("app") or get_label_selector(label_dict) or config.get("name")

def generate_chaos_manifest(config):
    """Generate Chaos Mesh PodChaos manifest dynamically from declarative configuration."""
    name = config.get("name", "experiment")
    clean_name = f"chaoslab-{name.lower().replace('_', '-')}"
    namespace = config.get("target", {}).get("namespace", "default")
    label_dict = config.get("target", {}).get("label", {})
    fault = config.get("fault", {})
    action = fault.get("type", "pod-kill")
    mode = fault.get("mode", "one")

    manifest = {
        "apiVersion": "chaos-mesh.org/v1alpha1",
        "kind": "PodChaos",
        "metadata": {
            "name": clean_name,
            "namespace": namespace
        },
        "spec": {
            "action": action,
            "mode": mode,
            "selector": {
                "namespaces": [namespace],
                "labelSelectors": label_dict
            }
        }
    }
    return clean_name, yaml.safe_dump(manifest, sort_keys=False)

def clean_chaos(chaos_name, namespace):
    """Clean up any existing Chaos Mesh experiment resource."""
    run_command([
        "kubectl",
        "delete",
        "podchaos",
        chaos_name,
        "-n",
        namespace,
        "--ignore-not-found=true"
    ])

def apply_chaos(config):
    """Dynamically inject chaos defined by declarative configuration."""
    chaos_name, manifest_yaml = generate_chaos_manifest(config)
    namespace = config.get("target", {}).get("namespace", "default")

    print()
    print(f"Injecting chaos ({chaos_name})...")
    clean_chaos(chaos_name, namespace)
    time.sleep(1)

    result = run_command(
        ["kubectl", "apply", "-f", "-"],
        input_text=manifest_yaml
    )

    if result.returncode != 0:
        print("Failed to inject chaos.")
        print(result.stderr)
        return False, chaos_name, result.stderr

    print(result.stdout.strip())
    return True, chaos_name, result.stdout.strip()

def get_pods(namespace, label_selector):
    """Retrieve current pods matching target label selector with structured metadata."""
    result = run_command([
        "kubectl",
        "get",
        "pods",
        "-n",
        namespace,
        "-l",
        label_selector,
        "-o",
        "json"
    ])

    if result.returncode != 0:
        return []

    try:
        data = json.loads(result.stdout)
    except Exception:
        return []

    pods = []
    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        status_info = item.get("status", {})
        phase = status_info.get("phase", "Unknown")

        # Check deletion timestamp (pod is terminating)
        is_terminating = metadata.get("deletionTimestamp") is not None
        if is_terminating:
            phase = "Terminating"

        container_statuses = status_info.get("containerStatuses", [])
        total_containers = len(container_statuses)
        ready_containers = sum(1 for c in container_statuses if c.get("ready", False))
        ready_str = f"{ready_containers}/{total_containers}" if total_containers > 0 else "0/0"
        is_ready = (total_containers > 0) and (ready_containers == total_containers) and (phase == "Running")

        restarts = 0
        if container_statuses:
            restarts = container_statuses[0].get("restartCount", 0)

        pods.append({
            "name": metadata.get("name", ""),
            "ready": ready_str,
            "is_ready": is_ready,
            "status": phase,
            "restarts": restarts,
            "pod_ip": status_info.get("podIP", ""),
            "node": item.get("spec", {}).get("nodeName", ""),
            "created_at": metadata.get("creationTimestamp", "")
        })

    return pods

def get_ready_pods(namespace, label_selector):
    """Return pods matching label selector that are fully ready and running."""
    pods = get_pods(namespace, label_selector)
    return [pod for pod in pods if pod["is_ready"]]

def get_chaos_mesh_evidence(chaos_name, namespace):
    """Query Chaos Mesh CRD status and container records."""
    result = run_command([
        "kubectl",
        "get",
        "podchaos",
        chaos_name,
        "-n",
        namespace,
        "-o",
        "json"
    ])

    if result.returncode != 0:
        return {"available": False, "error": result.stderr.strip()}

    try:
        data = json.loads(result.stdout)
        status = data.get("status", {})
        conditions = [
            {"type": c.get("type"), "status": c.get("status")}
            for c in status.get("conditions", [])
        ]
        container_records = status.get("experiment", {}).get("containerRecords", [])
        return {
            "available": True,
            "resource_name": chaos_name,
            "desired_phase": status.get("experiment", {}).get("desiredPhase", "Unknown"),
            "conditions": conditions,
            "container_records": container_records
        }
    except Exception as e:
        return {"available": False, "error": str(e)}

def get_kubernetes_events(namespace, target_pod_names, target_name):
    """Retrieve Kubernetes events related to target pods and microservice."""
    result = run_command([
        "kubectl",
        "get",
        "events",
        "-n",
        namespace,
        "--sort-by=.metadata.creationTimestamp",
        "-o",
        "json"
    ])

    if result.returncode != 0:
        return []

    try:
        data = json.loads(result.stdout)
        events = []
        for item in data.get("items", []):
            obj = item.get("involvedObject", {})
            obj_name = obj.get("name", "")
            obj_kind = obj.get("kind", "")

            # Filter events relevant to target microservice or pods
            if obj_name in target_pod_names or target_name in obj_name:
                events.append({
                    "reason": item.get("reason"),
                    "message": item.get("message"),
                    "object": f"{obj_kind.lower()}/{obj_name}",
                    "type": item.get("type"),
                    "timestamp": item.get("lastTimestamp") or item.get("eventTime") or item.get("metadata", {}).get("creationTimestamp")
                })
        # Keep the latest 10 relevant events
        return events[-10:] if len(events) > 10 else events
    except Exception:
        return []

def save_report(experiment_name, report_data):
    """Save structured report to disk as JSON."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    clean_name = experiment_name.lower().replace("-", "_")
    report_path = os.path.join(REPORTS_DIR, f"{clean_name}_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)

    # Save a timestamped copy for experiment history
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    history_path = os.path.join(REPORTS_DIR, f"{clean_name}_{timestamp_str}.json")
    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)

    return report_path

def probe_application(pod_name, namespace, config):
    """Perform application-level verification: gRPC health check, functional RPC, and logs."""
    app_config = config.get("verification", {}).get("application", {})
    if not app_config.get("enabled", False):
        return {"enabled": False}

    evidence = {
        "enabled": True,
        "probed_pod": pod_name,
        "health_check_passed": False,
        "health_status": "UNKNOWN",
        "functional_test_passed": False,
        "transaction_id": None,
        "logs_verified": False,
        "startup_log_detected": False,
        "log_error_count": 0
    }

    # 1. gRPC Health Check Probe
    if app_config.get("grpc_health_check", True):
        probe_cmd = (
            "const grpc = require('@grpc/grpc-js'); "
            "const protoLoader = require('@grpc/proto-loader'); "
            "const pkgDef = protoLoader.loadSync('/usr/src/app/proto/grpc/health/v1/health.proto'); "
            "const healthProto = grpc.loadPackageDefinition(pkgDef).grpc.health.v1; "
            "const client = new healthProto.Health('localhost:50051', grpc.credentials.createInsecure()); "
            "client.check({service: ''}, (err, res) => { "
            "  if (err) { console.error(err.message || err); process.exit(1); } "
            "  console.log(JSON.stringify(res)); "
            "});"
        )
        res = run_command([
            "kubectl", "exec", pod_name, "-n", namespace, "--", "node", "-e", probe_cmd
        ])
        if res.returncode == 0:
            try:
                data = json.loads(res.stdout.strip())
                status_code = data.get("status")
                # In grpc.health.v1, status 1 = SERVING
                if status_code == 1:
                    evidence["health_check_passed"] = True
                    evidence["health_status"] = "SERVING"
                else:
                    evidence["health_status"] = str(status_code)
            except Exception as e:
                evidence["health_error"] = str(e)
        else:
            evidence["health_error"] = res.stderr.strip()

    # 2. Functional Business RPC Test (Charge)
    if app_config.get("functional_test", True):
        func_cmd = (
            "const grpc = require('@grpc/grpc-js'); "
            "const protoLoader = require('@grpc/proto-loader'); "
            "const pkgDef = protoLoader.loadSync('/usr/src/app/proto/demo.proto', { keepCase: true }); "
            "const shop = grpc.loadPackageDefinition(pkgDef).hipstershop; "
            "const client = new shop.PaymentService('localhost:50051', grpc.credentials.createInsecure()); "
            "const req = { amount: { currency_code: 'USD', units: 50, nanos: 0 }, "
            "  credit_card: { credit_card_number: '4111-1111-1111-1111', credit_card_cvv: 123, "
            "    credit_card_expiration_year: 2030, credit_card_expiration_month: 12 } }; "
            "client.charge(req, (err, res) => { "
            "  if (err) { console.error(err.message || err); process.exit(1); } "
            "  console.log(JSON.stringify(res)); "
            "});"
        )
        res = run_command([
            "kubectl", "exec", pod_name, "-n", namespace, "--", "node", "-e", func_cmd
        ])
        if res.returncode == 0:
            try:
                data = json.loads(res.stdout.strip())
                tx_id = data.get("transaction_id")
                if tx_id:
                    evidence["functional_test_passed"] = True
                    evidence["transaction_id"] = tx_id
            except Exception as e:
                evidence["functional_error"] = str(e)
        else:
            evidence["functional_error"] = res.stderr.strip()

    # 3. Application Container Logs Inspection
    if app_config.get("verify_logs", True):
        expected_pattern = app_config.get("expected_log_pattern", "PaymentService gRPC server started")
        res = run_command([
            "kubectl", "logs", pod_name, "-n", namespace, "--tail=100"
        ])
        if res.returncode == 0:
            stdout = res.stdout
            has_startup = expected_pattern.lower() in stdout.lower()
            fatal_errors = [
                line.strip() for line in stdout.splitlines()
                if "uncaughtexception" in line.lower() or "fatal error" in line.lower()
            ]
            evidence["logs_verified"] = True
            evidence["startup_log_detected"] = has_startup
            evidence["log_error_count"] = len(fatal_errors)
        else:
            evidence["log_error"] = res.stderr.strip()

    return evidence

def run_experiment(config):
    """Execute declarative experiment: baseline -> inject -> observe -> verify -> report."""
    experiment_start_dt = datetime.now(timezone.utc)
    experiment_start_time = time.time()

    # Extract declarative parameters (no hardcoded values)
    experiment_name = config.get("name", "unnamed-experiment")
    namespace = config.get("target", {}).get("namespace", "default")
    label_dict = config.get("target", {}).get("label", {})
    label_selector = get_label_selector(label_dict)
    target_name = get_target_name(config)
    fault_type = config.get("fault", {}).get("type", "pod-kill")
    timeout = config.get("recovery", {}).get("timeout_seconds", 60)
    expected_replicas = config.get("verification", {}).get("expected_ready_replicas", 1)

    print("==================================================")
    print(f"CHAOSLAB EXPERIMENT: {experiment_name}")
    print("==================================================")
    print(f"Target: {target_name} (namespace: {namespace}, selector: {label_selector})")
    print(f"Fault: {fault_type}")
    print(f"Verification Spec: Expected Ready Replicas = {expected_replicas}, Timeout = {timeout}s")
    print("--------------------------------------------------")
    print("Checking baseline pods...")

    initial_pods = get_pods(namespace, label_selector)
    initial_ready_pods = get_ready_pods(namespace, label_selector)

    if len(initial_ready_pods) < expected_replicas:
        print()
        print("FAIL")
        print(f"Target does not meet baseline readiness: found {len(initial_ready_pods)} ready pods, expected {expected_replicas}.")
        return False

    baseline_pod = initial_pods[0]
    initial_names = {pod["name"] for pod in initial_pods}

    print(f"Baseline pod(s) [{len(initial_pods)}]:")
    for pod in initial_pods:
        print(f"- {pod['name']} ({pod['ready']}, {pod['status']})")

    # Inject Chaos
    chaos_success, chaos_name, chaos_msg = apply_chaos(config)
    fault_injected_dt = datetime.now(timezone.utc)
    fault_injected_time = time.time()

    if not chaos_success:
        print("FAIL: Chaos injection failed.")
        return False

    print()
    print("Waiting for the target pod to disappear...")

    original_pod_disappeared = False
    original_pod_disappeared_dt = None
    original_pod_disappeared_time = None
    replacement_pod = None
    replacement_ready = False
    replacement_ready_dt = None
    recovery_time = None
    total_recovery_time = None
    verdict = "FAIL"
    failure_reason = None

    start = time.time()
    while time.time() - start < timeout:
        current_pods = get_pods(namespace, label_selector)
        current_names = {pod["name"] for pod in current_pods}

        # Check if an original pod disappeared or is terminating
        active_original_pods = [
            p for p in current_pods
            if p["name"] in initial_names and p["status"] != "Terminating"
        ]

        if len(active_original_pods) < len(initial_pods):
            original_pod_disappeared = True
            original_pod_disappeared_dt = datetime.now(timezone.utc)
            original_pod_disappeared_time = time.time()
            print(f"Target pod disappeared: {initial_names - {p['name'] for p in active_original_pods}}")

            recovery_start = time.time()
            print(f"Waiting for replacement pod to satisfy expected ready replicas ({expected_replicas})...")

            while time.time() - recovery_start < timeout:
                ready_pods = get_ready_pods(namespace, label_selector)

                # Detect replacement pod
                current_all = get_pods(namespace, label_selector)
                new_pods = [p for p in current_all if p["name"] not in initial_names]
                if new_pods:
                    replacement_pod = new_pods[0]

                # Check if expected ready replicas condition is satisfied
                if len(ready_pods) >= expected_replicas:
                    ready_new = [p for p in ready_pods if p["name"] not in initial_names]
                    if ready_new or ready_pods:
                        replacement_pod = (ready_new or ready_pods)[0]
                        replacement_ready = True
                        replacement_ready_dt = datetime.now(timezone.utc)
                        replacement_ready_time = time.time()

                        recovery_time = replacement_ready_time - recovery_start
                        total_recovery_time = replacement_ready_time - fault_injected_time
                        verdict = "PASS"
                        break

                time.sleep(1)

            if not replacement_ready:
                failure_reason = f"Target pods did not reach {expected_replicas} ready replicas within timeout."
            break

        time.sleep(1)

    if not original_pod_disappeared:
        failure_reason = "Target pod was not terminated within timeout."

    # Give Chaos Mesh and Kubernetes events a brief moment to synchronize
    time.sleep(2)

    # Collect Evidence
    tracked_names = set(initial_names)
    if replacement_pod:
        tracked_names.add(replacement_pod["name"])

    chaos_mesh_evidence = get_chaos_mesh_evidence(chaos_name, namespace)
    k8s_events = get_kubernetes_events(namespace, tracked_names, target_name)
    current_ready_pods = get_ready_pods(namespace, label_selector)

    # Perform Application-Level Probing on replacement pod
    app_evidence = {"enabled": False}
    if replacement_ready and replacement_pod:
        print()
        print("Probing application correctness on replacement pod...")
        app_evidence = probe_application(replacement_pod["name"], namespace, config)
        if app_evidence.get("enabled"):
            print(f"- gRPC Health Status: {app_evidence.get('health_status', 'N/A')}")
            if app_evidence.get("transaction_id"):
                print(f"- Functional Charge RPC: Transaction ID {app_evidence.get('transaction_id')}")
            print(f"- Container Startup Logs: {'Verified' if app_evidence.get('startup_log_detected') else 'Missing'}")

    experiment_end_dt = datetime.now(timezone.utc)

    # Build Structured Evidence Dictionary
    evidence = {
        "experiment": experiment_name,
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
        "chaos_mesh_evidence": chaos_mesh_evidence,
        "kubernetes_events": k8s_events
    }

    # Evaluate Evidence through Verification Oracle
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

    # Save to disk
    report_file = save_report(experiment_name, report_data)

    # Print Oracle Scorecard and Summary
    print()
    print(oracle.format_scorecard(oracle_eval))
    print()
    print("==================================================")
    print("CHAOSLAB EXPERIMENT SUMMARY")
    print("==================================================")
    print(f"Experiment: {experiment_name}")
    print(f"Target: {target_name} ({namespace})")
    print(f"Fault: {fault_type}")
    print(f"Baseline: {baseline_pod['name']} ({baseline_pod['ready']})")
    if replacement_pod:
        print(f"Replacement: {replacement_pod['name']}")
    if app_evidence.get("enabled"):
        print(f"Application Health: {app_evidence.get('health_status', 'N/A')}")
        if app_evidence.get("transaction_id"):
            print(f"Transaction ID: {app_evidence.get('transaction_id')}")
    if recovery_time is not None:
        print(f"Recovery Time: {recovery_time:.2f} seconds")
    print(f"Final Oracle Verdict: {verdict}")
    print(f"Structured JSON Report: {report_file}")
    print("==================================================")

    return verdict == "PASS"

def verify_experiment(config_path=None):
    """Execute and verify experiment from a configuration path or default."""
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    print(f"Loading experiment configuration: {config_path}")
    config = load_experiment_config(config_path)
    return run_experiment(config)

def verify_recovery():
    """Backwards-compatible entrypoint."""
    return verify_experiment()

if __name__ == "__main__":
    config_arg = sys.argv[1] if len(sys.argv) > 1 else None
    verify_experiment(config_arg)