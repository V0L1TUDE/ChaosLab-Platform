"""
Built-in application catalog with cluster service discovery
===========================================================
The executable applications in the MVP are the three microservice systems already deployed in the
ChaosLab Kubernetes environment (Online Boutique, Learning Management, Project Management).

Services are DISCOVERED from the cluster (`kubectl get deployments`), not hard-coded. A static table
only supplies descriptive metadata (language, protocol, blurb) and is the fallback when the cluster
cannot be queried (e.g. a laptop with no cluster), in which case the UI says the list is static and
Live execution is refused. This module also turns a user's selection into a configuration for the
existing ChaosLab engine.
"""

import copy
import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

import yaml

from . import config, db, k8s_deploy

DISCOVERY_TTL_SECONDS = 10

# ----------------------------------------------------------------------------- applications
BUILTIN_APPS: List[Dict[str, Any]] = [
    {
        "slug": "online-boutique",
        "name": "Online Boutique (Demo)",
        "namespace": "default",
        "description": (
            "Google's Online Boutique e-commerce reference application: a polyglot, gRPC-based microservice "
            "system preconfigured in ChaosLab with Chaos Mesh fault injection, health probes and the "
            "Verification Oracle."),
        "static_services": [
            ("frontend", "Go", "HTTP", "Web UI and HTTP entry point."),
            ("cartservice", "C#", "gRPC", "Stores shopping carts in Redis."),
            ("productcatalogservice", "Go", "gRPC", "Product listing and search."),
            ("currencyservice", "Node.js", "gRPC", "Currency conversion rates."),
            ("paymentservice", "Node.js", "gRPC", "Mock credit-card charging."),
            ("shippingservice", "Go", "gRPC", "Shipping cost and tracking."),
            ("emailservice", "Python", "gRPC", "Order confirmation e-mails."),
            ("checkoutservice", "Go", "gRPC", "Orchestrates the checkout flow."),
            ("recommendationservice", "Python", "gRPC", "Product recommendations."),
            ("adservice", "Java", "gRPC", "Contextual text ads."),
            ("redis-cart", "Redis", "TCP", "Cart datastore."),
        ],
    },
    {
        "slug": "learning-platform",
        "name": "Learning Management System (Demo)",
        "namespace": "learning-platform",
        "description": (
            "A small LMS built for ChaosLab: course, student and enrollment services behind an nginx frontend. "
            "Its curated experiments include real HTTP health and functional probes plus tracing."),
        "static_services": [
            ("lms-frontend", "nginx", "HTTP", "Static web frontend."),
            ("course-service", "Python", "HTTP", "Course catalogue API."),
            ("student-service", "Python", "HTTP", "Student records API."),
            ("enrollment-service", "Python", "HTTP", "Enrollment API."),
        ],
    },
    {
        "slug": "project-platform",
        "name": "Project Management Platform (Demo)",
        "namespace": "project-platform",
        "description": (
            "A small project-management system built for ChaosLab: user, project and task services behind an "
            "nginx frontend. Its curated experiments include real HTTP probes plus tracing."),
        "static_services": [
            ("pm-frontend", "nginx", "HTTP", "Static web frontend."),
            ("user-service", "Python", "HTTP", "User accounts API."),
            ("project-service", "Python", "HTTP", "Projects API."),
            ("task-service", "Python", "HTTP", "Tasks API."),
        ],
    },
]
_APP_BY_SLUG = {a["slug"]: a for a in BUILTIN_APPS}

# Services that must not be fault-injected in the demo (data stores, load generators, tooling).
_NON_TARGETABLE = re.compile(r"(redis|postgres|mysql|mongo|memcached|jaeger|loadgenerator|prometheus|grafana)", re.I)

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
_NAME_SANITIZE = re.compile(r"[^a-z0-9-]+")


def get_fault(fault_id: str) -> Optional[Dict[str, Any]]:
    return _FAULT_BY_ID.get(fault_id)


def get_builtin(slug: Optional[str]) -> Optional[Dict[str, Any]]:
    return _APP_BY_SLUG.get(slug) if slug else None


def _resolve_cluster_app(row) -> Optional[Dict[str, Any]]:
    """An application living in a customer cluster connected through the ChaosLab agent."""
    from . import clusters
    with db.session() as conn:
        cluster = conn.execute("SELECT * FROM clusters WHERE id = ?", (row["cluster_id"],)).fetchone()
    if not cluster or cluster["status"] == "revoked":
        return None
    return {"slug": None, "id": row["id"], "name": row["name"], "description": row["description"],
            "namespace": row["namespace"], "static_services": [], "curated": False,
            "cluster_id": cluster["id"], "cluster_name": cluster["name"], "cluster_online": clusters.is_online(cluster),
            "cluster_paused": bool(cluster["paused"]), "cluster_seen": cluster["last_seen"],
            "agent_inventory": clusters.inventory_services(cluster, row["namespace"]),
            "probes": json.loads(row["probe_json"]) if row["probe_json"] else {},
            "inventory_at": cluster["inventory_at"]}


def resolve_app(row) -> Optional[Dict[str, Any]]:
    """
    Catalog entry for an applications row: a built-in app, or a user's validated manifest upload (whose
    namespace is its sandbox namespace and whose services come from the validated Deployments).
    None for apps that cannot run experiments (plain registrations, invalid manifests).
    """
    if row["slug"]:
        return get_builtin(row["slug"])
    if row["cluster_id"]:
        return _resolve_cluster_app(row)
    if not row["manifest_json"] or not row["validation_json"]:
        return None
    workloads = json.loads(row["validation_json"]).get("workloads", [])
    services = [(w.get("app") or w["name"], (w["images"][0].rsplit("/", 1)[-1] if w.get("images") else "container"),
                 "unknown", "Uploaded workload: " + ", ".join(w.get("images") or [])) for w in workloads]
    return {"slug": None, "id": row["id"], "name": row["name"], "description": row["description"],
            "namespace": k8s_deploy.namespace_for(row["id"]), "static_services": services,
            "uploaded": True, "deployed": row["status"] == "deployed", "status": row["status"], "curated": False}


# ----------------------------------------------------------------------------- discovery
_cache: Dict[str, Any] = {"at": 0.0, "data": None}
_cache_lock = threading.Lock()


def _query_cluster() -> Dict[str, List[Dict[str, Any]]]:
    """namespace -> deployments, from ONE kubectl call. Raises RuntimeError when the cluster is unusable."""
    if not shutil.which("kubectl"):
        raise RuntimeError("kubectl not found")
    try:
        res = subprocess.run(["kubectl", "get", "deployments", "-A", "-o", "json"],
                             capture_output=True, text=True, timeout=8)
    except Exception as e:
        raise RuntimeError(str(e))
    if res.returncode != 0:
        raise RuntimeError((res.stderr or "kubectl failed").strip()[:200])
    by_ns: Dict[str, List[Dict[str, Any]]] = {}
    for item in json.loads(res.stdout).get("items", []):
        meta, spec, status = item["metadata"], item.get("spec", {}), item.get("status", {})
        labels = (spec.get("selector", {}).get("matchLabels") or {})
        app_label = labels.get("app") or (spec.get("template", {}).get("metadata", {}).get("labels") or {}).get("app")
        by_ns.setdefault(meta["namespace"], []).append({
            "deployment": meta["name"], "app_label": app_label,
            "replicas": spec.get("replicas", 1), "ready": status.get("readyReplicas", 0) or 0,
        })
    return by_ns


def _cluster_snapshot(force: bool = False) -> Dict[str, Any]:
    with _cache_lock:
        if not force and _cache["data"] and time.time() - _cache["at"] < DISCOVERY_TTL_SECONDS:
            return _cache["data"]
        if not config.DISCOVERY_ENABLED:
            snap = {"ok": False, "error": "Cluster discovery disabled (CHAOSLAB_DISCOVERY_ENABLED=false).", "namespaces": {}}
        else:
            try:
                snap = {"ok": True, "error": None, "namespaces": _query_cluster()}
            except Exception as e:
                snap = {"ok": False, "error": str(e), "namespaces": {}}
        snap["at"] = db.utcnow()
        _cache.update(at=time.time(), data=snap)
        return snap


def _static_service(app: Dict[str, Any], t) -> Dict[str, Any]:
    name, lang, proto, desc = t
    return {"name": name, "language": lang, "protocol": proto, "description": desc,
            "targetable": not _NON_TARGETABLE.search(name), "replicas": None, "ready": None}


def discover_services(app: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
    """
    Services of a built-in app. source='cluster' when read from Kubernetes, else 'static' (fallback,
    with the reason). Cluster services missing from the static table still appear (metadata unknown).
    """
    meta = {t[0]: t for t in app["static_services"]}
    if app.get("cluster_id"):
        services = []
        for d in sorted(app["agent_inventory"], key=lambda x: x["deployment"]):
            if not d["app_label"]:
                continue          # the engine targets pods by the `app` label
            name = d["app_label"]
            services.append({"name": name, "language": "unknown", "protocol": "unknown",
                             "description": f"Deployment {d['deployment']} in your cluster.",
                             "targetable": not _NON_TARGETABLE.search(name), "replicas": d["replicas"], "ready": d["ready"]})
        online = app["cluster_online"] and not app["cluster_paused"]
        err = None if online else ("Emergency stop is active for this cluster." if app["cluster_paused"]
                                   else "The ChaosLab agent is offline; showing the last reported services.")
        return {"source": "agent" if online else "static", "discovered_at": app["inventory_at"] or db.utcnow(),
                "error": err, "services": services}
    if app.get("uploaded") and not app.get("deployed"):
        return {"source": "static", "discovered_at": db.utcnow(), "error": "Application is not deployed to the cluster.",
                "services": [_static_service(app, t) for t in app["static_services"]]}
    snap = _cluster_snapshot(force)
    deployments = snap["namespaces"].get(app["namespace"]) if snap["ok"] else None
    if deployments:
        services = []
        for d in sorted(deployments, key=lambda x: x["deployment"]):
            if not d["app_label"]:
                continue          # the engine selects pods by the `app` label; without it nothing can be targeted
            name = d["app_label"]
            base = _static_service(app, meta.get(name, (name, "unknown", "unknown", "Discovered from the cluster.")))
            base.update(replicas=d["replicas"], ready=d["ready"])
            services.append(base)
        return {"source": "cluster", "discovered_at": snap["at"], "error": None, "services": services}
    reason = snap["error"] or f"No deployments found in namespace '{app['namespace']}'."
    return {"source": "static", "discovered_at": snap["at"], "error": reason,
            "services": [_static_service(app, t) for t in app["static_services"]]}


def targetable_services(app: Dict[str, Any]) -> List[str]:
    return [s["name"] for s in discover_services(app)["services"] if s["targetable"]]


def service_info(app: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    return next((s for s in discover_services(app)["services"] if s["name"] == name), None)


# ----------------------------------------------------------------------------- curated experiments
def curated_experiments(namespace: str) -> List[Dict[str, Any]]:
    """Existing hand-written ChaosLab YAML experiments for a namespace (deep application/telemetry checks)."""
    out = []
    for path in sorted(glob.glob(os.path.join(config.EXPERIMENTS_DIR, "*.yaml"))):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            target = data.get("target", {})
            if target.get("namespace", "default") != namespace:
                continue
            svc = (target.get("label") or {}).get("app")
            if not svc:
                continue
            app_cfg = data.get("application") or data.get("verification", {}).get("application") or {}
            tel_cfg = data.get("telemetry") or data.get("verification", {}).get("telemetry") or {}
            out.append({
                "name": data.get("name"), "service": svc,
                "fault": (data.get("fault") or {}).get("type", "pod-kill"),
                "file": os.path.relpath(path, config.REPO_ROOT).replace("\\", "/"),
                "application_checks": bool(app_cfg.get("enabled")),
                "telemetry_checks": bool(tel_cfg.get("enabled")),
            })
        except Exception:
            continue
    return out


def describe_app(app: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
    disc = discover_services(app, force)
    return {
        "slug": app.get("slug"), "namespace": app["namespace"],
        "services": disc["services"], "services_count": len(disc["services"]),
        "targetable_services": [s["name"] for s in disc["services"] if s["targetable"]],
        "discovery": {"source": disc["source"], "discovered_at": disc["discovered_at"], "error": disc["error"]},
        "faults": FAULTS,
        # curated YAMLs only apply to the built-in demo apps (user apps get probes they configure themselves)
        "curated_experiments": curated_experiments(app["namespace"]) if app.get("curated", True) else [],
    }


def _find_curated_yaml(namespace: str, service: str, engine_fault: str) -> Optional[str]:
    for exp in curated_experiments(namespace):
        if exp["service"] == service and exp["fault"] == engine_fault:
            return os.path.join(config.REPO_ROOT, exp["file"])
    return None


# ----------------------------------------------------------------------------- selection -> engine config
def validate_selection(app: Dict[str, Any], service: Any, fault_id: Any, duration: Any) -> int:
    """Validate a user's experiment choice; returns the normalized duration or raises ValueError."""
    names = targetable_services(app)
    if not isinstance(service, str) or service not in names:
        if not names:
            raise ValueError(f"No targetable services were found for {app['name']} in namespace '{app['namespace']}'. "
                             "Check that the namespace name is right (kubectl get deployments -n <namespace>), that the "
                             "agent's --allowed-namespaces matches it, and that the Deployments carry an 'app' label.")
        raise ValueError(f"Unknown or non-targetable service for {app['name']}. Choose one of: {', '.join(names)}.")
    if not isinstance(fault_id, str) or fault_id not in _FAULT_BY_ID:
        raise ValueError(f"Unknown fault type. Choose one of: {', '.join(_FAULT_BY_ID)}.")
    if isinstance(duration, bool) or not isinstance(duration, int):
        raise ValueError("duration_seconds must be an integer.")
    if not config.MIN_DURATION_SECONDS <= duration <= config.MAX_DURATION_SECONDS:
        raise ValueError(f"duration_seconds must be between {config.MIN_DURATION_SECONDS} and {config.MAX_DURATION_SECONDS}.")
    return duration


def build_engine_config(app: Dict[str, Any], service: str, fault_id: str, duration: int, run_name: str, *,
                        full_verification: bool = False) -> Dict[str, Any]:
    """
    Build a configuration in the existing engine schema.

    * If a curated YAML exists for (namespace, service, fault) it is the base, so its deep
      application/telemetry probes are reused unchanged (Live mode).
    * Otherwise a minimal infrastructure-only configuration is synthesized.
    * `full_verification=True` (Simulated mode only) enables every invariant group so the whole Oracle
      can be demonstrated; it never changes what Live mode does.
    """
    namespace = app["namespace"]
    fault = _FAULT_BY_ID[fault_id]
    engine_fault = fault["engine_type"]
    curated = _find_curated_yaml(namespace, service, engine_fault) if app.get("curated", True) else None

    cfg: Dict[str, Any] = {}
    if curated and not full_verification:
        with open(curated, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    cfg = copy.deepcopy(cfg)
    cfg["name"] = _NAME_SANITIZE.sub("-", run_name.lower()).strip("-")[:48] or "chaoslab-run"
    cfg.setdefault("target", {})
    cfg["target"]["namespace"] = namespace
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
        info = next((s for s in discover_services(app)["services"] if s["name"] == service), None) or {}
        grpc = info.get("protocol") == "gRPC"
        cfg["application"] = {
            "enabled": True,
            "health_probe": {"type": "grpc" if grpc else "http", "expected_status": "SERVING" if grpc else 200},
            "functional_check": {"enabled": True},
            "verify_logs": True,
            "expected_log_pattern": "started",
        }
        cfg["telemetry"] = {"enabled": True, "service_name": service, "verify_error_spike": True, "max_allowed_p95_ms": 1000}
    elif (app.get("probes") or {}).get(service):
        # Declarative probes the user configured for this service of a connected cluster (executed by the agent).
        from . import probe_spec
        cfg["application"] = probe_spec.to_engine_application(app["probes"][service])
        cfg.setdefault("telemetry", {"enabled": False})
    else:
        # Curated YAMLs may nest these under `verification` (legacy schema); only default when absent in both.
        verification = cfg.get("verification") or {}
        if "application" not in cfg and "application" not in verification:
            cfg["application"] = {"enabled": False}
        if "telemetry" not in cfg and "telemetry" not in verification:
            cfg["telemetry"] = {"enabled": False}
    return cfg
