"""
ChaosLab Generic Probing Framework
==================================
Protocol-agnostic, declarative health and functional probers for Kubernetes microservices.
Supports HTTP REST, gRPC health, container exec, kubelet condition probes, and log analysis.
"""

import json
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.request
import urllib.error

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

class HttpProber:
    """Generic HTTP Prober supporting both pod-internal exec and direct cluster probes."""

    @staticmethod
    def probe_health(
        pod_name: str,
        namespace: str,
        probe_spec: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute an HTTP health check against the target pod.
        probe_spec schema:
            type: "http"
            path: "/health" (or endpoint: "http://...")
            port: 8000
            method: "GET"
            expected_status: 200
            timeout: 5
            expected_body_pattern: "OK" (optional)
            expected_json: {"status": "healthy"} (optional)
        """
        path = probe_spec.get("path") or probe_spec.get("endpoint") or "/health"
        port = probe_spec.get("port", 8000)
        method = (probe_spec.get("method") or "GET").upper()
        expected_status = int(probe_spec.get("expected_status", 200))
        timeout = int(probe_spec.get("timeout", 5))
        expected_pattern = probe_spec.get("expected_body_pattern")
        expected_json = probe_spec.get("expected_json")

        # Normalize URL path
        if not path.startswith("http://") and not path.startswith("https://"):
            if not path.startswith("/"):
                path = "/" + path
            target_url = f"http://localhost:{port}{path}"
        else:
            target_url = path

        # Probe via in-container python / curl inside the pod
        # Python probe script executed inside the container
        python_script = (
            "import urllib.request, json, sys; "
            f"req = urllib.request.Request('{target_url}', method='{method}'); "
            "try:\n"
            f"    with urllib.request.urlopen(req, timeout={timeout}) as resp:\n"
            "        body = resp.read().decode('utf-8', errors='ignore'); "
            "        print(json.dumps({'status': resp.status, 'body': body})); sys.exit(0)\n"
            "except urllib.error.HTTPError as e:\n"
            "    body = e.read().decode('utf-8', errors='ignore'); "
            "    print(json.dumps({'status': e.code, 'body': body})); sys.exit(0)\n"
            "except Exception as e:\n"
            "    print(json.dumps({'error': str(e)})); sys.exit(1)"
        )

        res = run_command([
            "kubectl", "exec", pod_name, "-n", namespace, "--", "python", "-c", python_script
        ], timeout=timeout + 5)

        # Fallback to curl if python is not in container
        if res.returncode != 0 and "error" not in res.stdout:
            curl_cmd = [
                "kubectl", "exec", pod_name, "-n", namespace, "--",
                "curl", "-s", "-w", "\n{\"status\": %{http_code}}", "-X", method, target_url
            ]
            curl_res = run_command(curl_cmd, timeout=timeout + 5)
            if curl_res.returncode == 0:
                lines = curl_res.stdout.strip().splitlines()
                status_line = lines[-1] if lines else "{}"
                body_content = "\n".join(lines[:-1]) if len(lines) > 1 else ""
                try:
                    status_data = json.loads(status_line)
                    res = subprocess.CompletedProcess(
                        curl_cmd, 0,
                        stdout=json.dumps({"status": status_data.get("status", 0), "body": body_content}),
                        stderr=""
                    )
                except Exception:
                    pass

        if res.returncode == 0:
            try:
                data = json.loads(res.stdout.strip().splitlines()[-1])
                status = data.get("status")
                body = data.get("body", "")
                passed = (status == expected_status)

                # Optional body pattern validation
                if passed and expected_pattern:
                    passed = expected_pattern.lower() in body.lower()

                # Optional JSON validation
                if passed and expected_json and isinstance(expected_json, dict):
                    try:
                        parsed_body = json.loads(body)
                        for k, v in expected_json.items():
                            if parsed_body.get(k) != v:
                                passed = False
                                break
                    except Exception:
                        passed = False

                health_status_str = f"{status} OK" if passed else f"HTTP {status}"
                return {
                    "passed": passed,
                    "health_status": health_status_str,
                    "status_code": status,
                    "body": body[:500],
                    "details": f"HTTP {method} {target_url} returned {status}"
                }
            except Exception as e:
                return {
                    "passed": False,
                    "health_status": "PARSE_ERROR",
                    "error": f"Failed to parse response: {e}, stdout: {res.stdout.strip()}"
                }

        error_msg = res.stderr.strip() or res.stdout.strip() or "Connection failed"
        return {
            "passed": False,
            "health_status": "UNREACHABLE",
            "error": error_msg
        }

    @staticmethod
    def execute_functional_test(
        pod_name: str,
        namespace: str,
        func_spec: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute an HTTP functional business transaction against the pod.
        func_spec schema:
            type: "http"
            path: "/tasks" (or endpoint: "http://...")
            port: 8000
            method: "POST" (or "GET")
            headers: {"Content-Type": "application/json"}
            body: {"title": "Test Task", ...}
            expected_status: 200 (or 201)
            expected_json_key: "task_id" (or "id", "order_id")
            timeout: 5
        """
        path = func_spec.get("path") or func_spec.get("endpoint") or "/"
        port = func_spec.get("port", 8000)
        method = (func_spec.get("method") or "GET").upper()
        headers = func_spec.get("headers", {})
        payload = func_spec.get("body")
        expected_status = int(func_spec.get("expected_status", 200))
        expected_key = func_spec.get("expected_json_key") or func_spec.get("expected_key")
        timeout = int(func_spec.get("timeout", 5))

        if not path.startswith("http://") and not path.startswith("https://"):
            if not path.startswith("/"):
                path = "/" + path
            target_url = f"http://localhost:{port}{path}"
        else:
            target_url = path

        payload_json = json.dumps(payload) if payload is not None else None

        # Build in-container Python test script
        python_script = (
            "import urllib.request, json, sys\n"
            f"headers = {repr(headers)}\n"
            f"payload_data = {repr(payload_json)}\n"
            f"data_bytes = payload_data.encode('utf-8') if payload_data else None\n"
            f"req = urllib.request.Request('{target_url}', data=data_bytes, headers=headers, method='{method}')\n"
            "try:\n"
            f"    with urllib.request.urlopen(req, timeout={timeout}) as resp:\n"
            "        body_str = resp.read().decode('utf-8', errors='ignore')\n"
            "        print(json.dumps({'status': resp.status, 'body': body_str}))\n"
            "        sys.exit(0)\n"
            "except urllib.error.HTTPError as e:\n"
            "    body_str = e.read().decode('utf-8', errors='ignore')\n"
            "    print(json.dumps({'status': e.code, 'body': body_str}))\n"
            "    sys.exit(0)\n"
            "except Exception as e:\n"
            "    print(json.dumps({'error': str(e)}))\n"
            "    sys.exit(1)\n"
        )

        res = run_command([
            "kubectl", "exec", pod_name, "-n", namespace, "--", "python", "-c", python_script
        ], timeout=timeout + 5)

        if res.returncode == 0:
            try:
                data = json.loads(res.stdout.strip().splitlines()[-1])
                status = data.get("status")
                body = data.get("body", "")
                passed = (status == expected_status)
                found_id = None
                parsed_json = None

                if body:
                    try:
                        parsed_json = json.loads(body)
                        target_dict = None
                        if isinstance(parsed_json, dict):
                            target_dict = parsed_json
                        elif isinstance(parsed_json, list) and len(parsed_json) > 0 and isinstance(parsed_json[0], dict):
                            target_dict = parsed_json[0]

                        if target_dict:
                            # Try to extract the requested key or common identity keys
                            if expected_key and expected_key in target_dict:
                                found_id = str(target_dict[expected_key])
                            elif "id" in target_dict:
                                found_id = str(target_dict["id"])
                            elif "task_id" in target_dict:
                                found_id = str(target_dict["task_id"])
                            elif "project_id" in target_dict:
                                found_id = str(target_dict["project_id"])
                            elif "course_id" in target_dict:
                                found_id = str(target_dict["course_id"])
                            elif "enrollment_id" in target_dict:
                                found_id = str(target_dict["enrollment_id"])
                            elif "transaction_id" in target_dict:
                                found_id = str(target_dict["transaction_id"])
                    except Exception:
                        pass

                if expected_key and not found_id:
                    passed = False

                return {
                    "passed": passed,
                    "status_code": status,
                    "transaction_id": found_id,
                    "response_body": body[:500],
                    "parsed_json": parsed_json,
                    "details": f"Functional {method} {target_url} returned {status} (ID: {found_id})"
                }
            except Exception as e:
                return {
                    "passed": False,
                    "error": f"Failed to parse functional response: {e}"
                }

        return {
            "passed": False,
            "error": res.stderr.strip() or res.stdout.strip() or "Functional request execution failed"
        }

class GrpcProber:
    """Generic gRPC Health and Functional Prober."""

    @staticmethod
    def probe_health(
        pod_name: str,
        namespace: str,
        probe_spec: Dict[str, Any],
        target_name: str = ""
    ) -> Dict[str, Any]:
        """
        Execute gRPC health probe against target pod.
        Supports standard grpc.health.v1 or legacy Online Boutique Node.js scripts.
        """
        # Legacy Boutique Node.js adapter
        if "payment" in target_name.lower() or probe_spec.get("adapter") == "boutique-payment":
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
                    data = json.loads(res.stdout.strip().splitlines()[-1])
                    status_code = data.get("status")
                    if status_code == 1:
                        return {"passed": True, "health_status": "SERVING", "details": "gRPC health: SERVING"}
                    return {"passed": False, "health_status": str(status_code), "details": f"Status code {status_code}"}
                except Exception as e:
                    return {"passed": False, "health_status": "UNKNOWN", "error": str(e)}
            return {"passed": False, "health_status": "UNKNOWN", "error": res.stderr.strip()}

        # Generic microservice probe: check pod readiness condition confirmed by kubelet
        return K8sProber.probe_readiness(pod_name, namespace)

    @staticmethod
    def execute_functional_test(
        pod_name: str,
        namespace: str,
        func_spec: Dict[str, Any],
        target_name: str = ""
    ) -> Dict[str, Any]:
        """
        Execute functional gRPC transaction.
        Supports legacy Online Boutique PaymentService/Charge.
        """
        if "payment" in target_name.lower() or func_spec.get("adapter") == "boutique-payment":
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
                    data = json.loads(res.stdout.strip().splitlines()[-1])
                    tx_id = data.get("transaction_id")
                    if tx_id:
                        return {
                            "passed": True,
                            "transaction_id": tx_id,
                            "details": f"Payment charge RPC confirmed transaction ID {tx_id}"
                        }
                except Exception as e:
                    return {"passed": False, "error": str(e)}
            return {"passed": False, "error": res.stderr.strip()}

        return {"passed": False, "error": "gRPC functional test adapter not configured"}

class K8sProber:
    """Kubernetes infrastructure and pod readiness prober."""

    @staticmethod
    def probe_readiness(pod_name: str, namespace: str) -> Dict[str, Any]:
        """Check pod readiness condition via kubelet API."""
        res = run_command([
            "kubectl", "get", "pod", pod_name, "-n", namespace,
            "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].status}"
        ])
        if res.returncode == 0 and res.stdout.strip() == "True":
            return {"passed": True, "health_status": "SERVING", "details": "Kubelet Ready condition confirmed"}
        return {"passed": False, "health_status": "NOT_SERVING", "details": "Kubelet Ready condition False"}

class LogProber:
    """Application container logs verifier."""

    @staticmethod
    def verify_logs(
        pod_name: str,
        namespace: str,
        log_spec: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Verify that container startup logs contain expected pattern and zero fatal errors.
        log_spec schema:
            expected_log_pattern: "Application started" (or "Uvicorn running", etc.)
            prohibited_patterns: ["panic:", "fatal error", "uncaughtException"]
        """
        expected_pattern = log_spec.get("expected_log_pattern", "started")
        prohibited_patterns = log_spec.get("prohibited_patterns", [
            "uncaughtexception", "fatal error", "panic:"
        ])

        res = run_command([
            "kubectl", "logs", pod_name, "-n", namespace, "--tail=300"
        ])
        if res.returncode != 0:
            return {
                "logs_verified": False,
                "startup_log_detected": False,
                "log_error_count": 1,
                "error": res.stderr.strip()
            }

        stdout = res.stdout
        has_startup = expected_pattern.lower() in stdout.lower() if expected_pattern else True

        fatal_errors = []
        for line in stdout.splitlines():
            line_lower = line.lower()
            for prohibited in prohibited_patterns:
                if prohibited.lower() in line_lower:
                    fatal_errors.append(line.strip())
                    break

        return {
            "logs_verified": True,
            "startup_log_detected": has_startup,
            "log_error_count": len(fatal_errors),
            "fatal_errors": fatal_errors[:5],
            "details": f"Startup pattern detected: {has_startup}, fatal error count: {len(fatal_errors)}"
        }

class ProbeDispatcher:
    """Unified dispatcher for health, functional, and log verification."""

    @classmethod
    def probe_application(
        cls,
        pod_name: str,
        namespace: str,
        config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Inspect declarative configuration (supporting both new and legacy schemas)
        and execute appropriate health, functional, and log probes.
        """
        evidence = {
            "enabled": True,
            "probed_pod": pod_name,
            "probe_type": "none",
            "health_check_passed": False,
            "health_status": "UNKNOWN",
            "functional_test_passed": False,
            "transaction_id": None,
            "logs_verified": False,
            "startup_log_detected": False,
            "log_error_count": 0
        }

        # Determine target name
        target_name = config.get("target", {}).get("label", {}).get("app") or config.get("name", "")

        # Extract application configuration (supports new 'application' or legacy 'verification.application')
        app_config = config.get("application") or config.get("verification", {}).get("application", {})
        if not app_config or not app_config.get("enabled", True):
            evidence["enabled"] = False
            return evidence

        # ---------------------------------------------------------
        # 1. Health Probe
        # ---------------------------------------------------------
        health_spec = app_config.get("health_probe") or app_config.get("probe")

        # Fallback to legacy gRPC schema if health_spec omitted
        if not health_spec:
            if app_config.get("grpc_health_check", False):
                health_spec = {"type": "grpc", "expected_status": app_config.get("expected_health_status", "SERVING")}
            else:
                health_spec = {"type": "k8s"}

        probe_type = health_spec.get("type", "k8s").lower()
        evidence["probe_type"] = probe_type

        if probe_type == "http":
            h_res = HttpProber.probe_health(pod_name, namespace, health_spec)
            evidence["health_check_passed"] = h_res.get("passed", False)
            evidence["health_status"] = h_res.get("health_status", "UNKNOWN")
            evidence["health_details"] = h_res
        elif probe_type == "grpc":
            g_res = GrpcProber.probe_health(pod_name, namespace, health_spec, target_name)
            evidence["health_check_passed"] = g_res.get("passed", False)
            evidence["health_status"] = g_res.get("health_status", "UNKNOWN")
            evidence["health_details"] = g_res
        else: # k8s condition probe
            k_res = K8sProber.probe_readiness(pod_name, namespace)
            evidence["health_check_passed"] = k_res.get("passed", False)
            evidence["health_status"] = k_res.get("health_status", "UNKNOWN")
            evidence["health_details"] = k_res

        # ---------------------------------------------------------
        # 2. Functional Test
        # ---------------------------------------------------------
        func_spec = app_config.get("functional_check") or app_config.get("functional")
        if not func_spec and app_config.get("functional_test", False):
            func_spec = {"enabled": True, "type": "grpc", "adapter": "boutique-payment"}

        if func_spec and func_spec.get("enabled", True):
            func_type = func_spec.get("type", "http").lower()
            if func_type == "http":
                f_res = HttpProber.execute_functional_test(pod_name, namespace, func_spec)
                evidence["functional_test_passed"] = f_res.get("passed", False)
                evidence["transaction_id"] = f_res.get("transaction_id")
                evidence["functional_details"] = f_res
            elif func_type == "grpc":
                f_res = GrpcProber.execute_functional_test(pod_name, namespace, func_spec, target_name)
                evidence["functional_test_passed"] = f_res.get("passed", False)
                evidence["transaction_id"] = f_res.get("transaction_id")
                evidence["functional_details"] = f_res
        else:
            evidence["functional_test_skipped"] = True

        # ---------------------------------------------------------
        # 3. Log Verification
        # ---------------------------------------------------------
        log_spec = app_config.get("logs") or app_config
        verify_logs = log_spec.get("verify_logs", app_config.get("verify_logs", False))
        if verify_logs:
            l_res = LogProber.verify_logs(pod_name, namespace, log_spec)
            evidence["logs_verified"] = l_res.get("logs_verified", False)
            evidence["startup_log_detected"] = l_res.get("startup_log_detected", False)
            evidence["log_error_count"] = l_res.get("log_error_count", 0)
            evidence["log_details"] = l_res

        return evidence
