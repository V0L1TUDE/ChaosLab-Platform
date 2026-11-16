"""
Built-in demo application catalog
=================================
The MVP's executable target is the Google Online Boutique microservice demo that already
runs in the ChaosLab Kubernetes environment (namespace `default`, pods labelled `app=<service>`).
This module is the single source of truth for its services, supported faults, and for turning a
user's selection into a configuration for the existing ChaosLab engine.
"""

import copy
import glob
import os
import re
from typing import Any, Dict, List, Optional

import yaml

from . import config

DEMO_SLUG = "online-boutique"
DEMO_NAMESPACE = "default"
DEMO_NAME = "Online Boutique (Demo)"
DEMO_DESCRIPTION = (
    "Google's Online Boutique e-commerce reference application: a polyglot, gRPC-based microservice "
    "system preconfigured in ChaosLab with Chaos Mesh fault injection, health probes and the "
    "Verification Oracle. Use it to try ChaosLab without deploying anything."
)

SERVICES: List[Dict[str, Any]] = [
    {"name": "frontend", "language": "Go", "protocol": "HTTP", "targetable": True, "description": "Web UI and HTTP entry point."},
    {"name": "cartservice", "language": "C#", "protocol": "gRPC", "targetable": True, "description": "Stores shopping carts in Redis."},
    {"name": "productcatalogservice", "language": "Go", "protocol": "gRPC", "targetable": True, "description": "Product listing and search."},
    {"name": "currencyservice", "language": "Node.js", "protocol": "gRPC", "targetable": True, "description": "Currency conversion rates."},
    {"name": "paymentservice", "language": "Node.js", "protocol": "gRPC", "targetable": True, "description": "Mock credit-card charging."},
    {"name": "shippingservice", "language": "Go", "protocol": "gRPC", "targetable": True, "description": "Shipping cost and tracking."},
    {"name": "emailservice", "language": "Python", "protocol": "gRPC", "targetable": True, "description": "Order confirmation e-mails."},
    {"name": "checkoutservice", "language": "Go", "protocol": "gRPC", "targetable": True, "description": "Orchestrates the checkout flow."},
    {"name": "recommendationservice", "language": "Python", "protocol": "gRPC", "targetable": True, "description": "Product recommendations."},
    {"name": "adservice", "language": "Java", "protocol": "gRPC", "targetable": True, "description": "Contextual text ads."},
    {"name": "redis-cart", "language": "Redis", "protocol": "TCP", "targetable": False, "description": "Cart datastore (not targetable in the demo to protect state)."},
]

# `engine_type` is the fault type understood by the existing engine / Chaos Mesh manifests.
FAULTS: List[Dict[str, Any]] = [
    {"id": "pod-kill", "label": "Pod Kill", "engine_type": "pod-kill", "chaos_kind": "PodChaos",
     "description": "Kills one pod of the target service; Kubernetes must reschedule it.",
     "duration_meaning": "Recovery timeout: how long the service has to become Ready again."},
    {"id": "network-delay", "label": "Network Delay", "engine_type": "network-delay", "chaos_kind": "NetworkChaos",
     "description": "Adds 200ms (+/-10ms) latency to traffic of the target service.",
     "duration_meaning": "How long the latency fault stays active."},
    {"id": "network-failure", "label": "Network Failure", "engine_type": "network-loss", "chaos_kind": "NetworkChaos",
     "description": "Drops 100% of network packets to the target service (full network failure).",
     "duration_meaning": "How long the network failure stays active."},
]

_FAULT_BY_ID = {f["id"]: f for f in FAULTS}
_SERVICE_BY_NAME = {s["name"]: s for s in SERVICES}
_NAME_SANITIZE = re.compile(r"[^a-z0-9-]+")


def get_fault(fault_id: str) -> Optional[Dict[str, Any]]:
    return _FAULT_BY_ID.get(fault_id)


def get_service(name: str) -> Optional[Dict[str, Any]]:
    return _SERVICE_BY_NAME.get(name)


def targetable_services() -> List[str]:
    return [s["name"] for s in SERVICES if s["targetable"]]


def curated_experiments() -> List[Dict[str, Any]]:
    """Existing, hand-written ChaosLab YAML experiments for the demo namespace (deep verification)."""
    out = []
    for path in sorted(glob.glob(os.path.join(config.EXPERIMENTS_DIR, "*.yaml"))):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            target = data.get("target", {})
            if target.get("namespace", "default") != DEMO_NAMESPACE:
                continue
            svc = (target.get("label") or {}).get("app")
            if not svc:
                continue
            out.append({
                "name": data.get("name"),
                "service": svc,
                "fault": (data.get("fault") or {}).get("type", "pod-kill"),
                "file": os.path.relpath(path, config.REPO_ROOT).replace("\\", "/"),
                "application_checks": bool((data.get("application") or data.get("verification", {}).get("application") or {}).get("enabled")),
                "telemetry_checks": bool((data.get("telemetry") or data.get("verification", {}).get("telemetry") or {}).get("enabled")),
            })
        except Exception:
            continue
    return out


def describe_demo_app() -> Dict[str, Any]:
    return {
        "namespace": DEMO_NAMESPACE,
        "services": SERVICES,
        "services_count": len(SERVICES),
        "targetable_services": targetable_services(),
        "faults": FAULTS,
        "curated_experiments": curated_experiments(),
    }


def _find_curated_yaml(service: str, engine_fault: str) -> Optional[str]:
    for exp in curated_experiments():
        if exp["service"] == service and exp["fault"] == engine_fault:
            return os.path.join(config.REPO_ROOT, exp["file"])
    return None


def validate_selection(service: str, fault_id: str, duration: Any) -> int:
    """Validate a user's experiment choice; returns the normalized duration or raises ValueError."""
    svc = get_service(service) if isinstance(service, str) else None
    if not svc or not svc["targetable"]:
        raise ValueError(f"Unknown or non-targetable service. Choose one of: {', '.join(targetable_services())}.")
    if not isinstance(fault_id, str) or fault_id not in _FAULT_BY_ID:
        raise ValueError(f"Unknown fault type. Choose one of: {', '.join(_FAULT_BY_ID)}.")
    if isinstance(duration, bool) or not isinstance(duration, int):
        raise ValueError("duration_seconds must be an integer.")
    if not config.MIN_DURATION_SECONDS <= duration <= config.MAX_DURATION_SECONDS:
        raise ValueError(f"duration_seconds must be between {config.MIN_DURATION_SECONDS} and {config.MAX_DURATION_SECONDS}.")
    return duration


def build_engine_config(service: str, fault_id: str, duration: int, run_name: str, *, full_verification: bool = False) -> Dict[str, Any]:
    """
    Build a configuration in the existing engine schema.

    * If a curated YAML experiment exists for (service, fault) it is the base, so its deep
      application/telemetry probes are reused unchanged (Live mode).
    * Otherwise a minimal infrastructure-only configuration is synthesized.
    * `full_verification=True` (Simulated mode only) enables every invariant group so the whole
      Oracle can be demonstrated; it never changes what Live mode does.
    """
    fault = _FAULT_BY_ID[fault_id]
    engine_fault = fault["engine_type"]
    curated = _find_curated_yaml(service, engine_fault)

    cfg: Dict[str, Any]
    if curated and not full_verification:
        with open(curated, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {}

    cfg = copy.deepcopy(cfg)
    cfg["name"] = _NAME_SANITIZE.sub("-", run_name.lower()).strip("-")[:48] or "chaoslab-run"
    cfg.setdefault("target", {})
    cfg["target"]["namespace"] = DEMO_NAMESPACE
    cfg["target"]["label"] = {"app": service}
    cfg["fault"] = {**cfg.get("fault", {}), "type": engine_fault, "mode": "one"}
    cfg.setdefault("recovery", {})
    cfg["recovery"]["expected_ready_replicas"] = cfg["recovery"].get("expected_ready_replicas", 1)

    if engine_fault == "pod-kill":
        cfg["recovery"]["timeout_seconds"] = duration
        cfg["fault"].pop("duration", None)
    else:
        cfg["fault"]["duration"] = f"{duration}s"
        cfg["recovery"]["timeout_seconds"] = max(60, duration + 30)
        if engine_fault == "network-delay":
            cfg["fault"].setdefault("latency", "200ms")
            cfg["fault"].setdefault("jitter", "10ms")
        else:
            cfg["fault"]["loss_pct"] = "100"

    if full_verification:
        cfg["application"] = {
            "enabled": True,
            "health_probe": {"type": "grpc" if _SERVICE_BY_NAME[service]["protocol"] == "gRPC" else "http", "expected_status": "SERVING"},
            "functional_check": {"enabled": True},
            "verify_logs": True,
            "expected_log_pattern": "started",
        }
        cfg["telemetry"] = {"enabled": True, "service_name": service, "verify_error_spike": True, "max_allowed_p95_ms": 1000}
    else:
        cfg.setdefault("application", {"enabled": False})
        cfg.setdefault("telemetry", {"enabled": False})
    return cfg
