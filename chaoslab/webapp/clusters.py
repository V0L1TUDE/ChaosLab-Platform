"""
Connected customer clusters (Option A: bring-your-own-cluster via the ChaosLab agent)
=====================================================================================
A user registers a cluster, gets a ONE-TIME enrollment token, and starts the agent inside/next to their
cluster. The agent exchanges the token for a long-lived credential (stored hashed) and from then on only
makes OUTBOUND calls: it polls for approved jobs and posts evidence back.

Trust model
  * enrollment token: single use, expires, only shown once, stored hashed
  * agent credential: hashed at rest, revocable, only allows poll (never job data of other clusters)
  * per-job token: one-time, scoped to one job, valid only until the job's lease expires
  * cluster identity (kube-system namespace UID) can be claimed by one cluster record at a time, and
    a credential used from a different cluster is rejected and audited
  * targets are explicit: applications are registered per (cluster, namespace) and only for namespaces the
    agent itself says it is allowed to touch (the agent enforces its own allow-list again locally)
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

from . import config, db

_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_MAX_INVENTORY_BYTES = 256 * 1024


class ClusterError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(ts: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def audit(user_id: Optional[int], cluster_id: Optional[int], actor: str, action: str, detail: str = "",
          ip: Optional[str] = None) -> None:
    """Append-only audit trail (who did what, to which cluster)."""
    with db.session() as conn:
        conn.execute("INSERT INTO audit_log (ts, user_id, cluster_id, actor, action, detail, ip) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (db.utcnow(), user_id, cluster_id, actor, action, detail[:500], ip))


# ----------------------------------------------------------------------------- state helpers
def is_online(row) -> bool:
    seen = _parse(row["last_seen"])
    return bool(row["status"] == "connected" and seen and (_now() - seen).total_seconds() <= config.AGENT_ONLINE_SECONDS)


def cluster_to_dict(row, include_inventory: bool = False) -> Dict[str, Any]:
    online = is_online(row)
    if row["status"] == "revoked":
        state = "revoked"
    elif row["status"] == "pending":
        state = "pending"
    elif row["paused"]:
        state = "paused"
    else:
        state = "online" if online else "offline"
    d = {
        "id": row["id"], "name": row["name"], "state": state, "status": row["status"], "online": online,
        "paused": bool(row["paused"]), "last_seen": row["last_seen"], "agent_version": row["agent_version"],
        "kube_version": row["kube_version"], "created_at": row["created_at"],
        "allowed_namespaces": json.loads(row["allowed_namespaces"]) if row["allowed_namespaces"] else [],
        "inventory_at": row["inventory_at"],
        "enroll_expires_at": row["enroll_expires_at"] if row["status"] == "pending" else None,
    }
    if include_inventory:
        d["inventory"] = json.loads(row["inventory_json"]) if row["inventory_json"] else {}
    return d


def get_own(conn, user_id: int, cluster_id: Any):
    try:
        cid = int(cluster_id)
    except (TypeError, ValueError):
        raise ClusterError("Cluster not found.", 404)
    row = conn.execute("SELECT * FROM clusters WHERE id = ? AND user_id = ?", (cid, user_id)).fetchone()
    if not row:
        raise ClusterError("Cluster not found.", 404)
    return row


# ----------------------------------------------------------------------------- user-facing lifecycle
def _new_enrollment(conn, cluster_id: int) -> str:
    token = "cle_" + secrets.token_urlsafe(24)
    expires = _iso(_now() + timedelta(minutes=config.ENROLL_TTL_MINUTES))
    conn.execute("UPDATE clusters SET enroll_token_hash = ?, enroll_expires_at = ? WHERE id = ?",
                 (_hash(token), expires, cluster_id))
    return token


def create_cluster(user_id: int, name: Any, ip: Optional[str] = None):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
        raise ClusterError("Cluster name is required (max 60 characters).")
    with db.session() as conn:
        n = conn.execute("SELECT COUNT(*) FROM clusters WHERE user_id = ? AND status != 'revoked'", (user_id,)).fetchone()[0]
        if n >= config.MAX_CLUSTERS_PER_USER:
            raise ClusterError(f"You can connect at most {config.MAX_CLUSTERS_PER_USER} clusters.", 429)
        cur = conn.execute("INSERT INTO clusters (user_id, name, status, created_at) VALUES (?, ?, 'pending', ?)",
                           (user_id, name.strip(), db.utcnow()))
        token = _new_enrollment(conn, cur.lastrowid)
        row = conn.execute("SELECT * FROM clusters WHERE id = ?", (cur.lastrowid,)).fetchone()
    audit(user_id, row["id"], "user", "cluster_created", name.strip(), ip)
    return row, token


def regenerate_enrollment(user_id: int, cluster_id: Any, ip: Optional[str] = None) -> str:
    """New one-time token. Invalidates the current agent credential: the agent must re-enroll."""
    with db.session() as conn:
        row = get_own(conn, user_id, cluster_id)
        if row["status"] == "revoked":
            raise ClusterError("This cluster was removed.", 409)
        conn.execute("UPDATE clusters SET status = 'pending', credential_hash = NULL WHERE id = ?", (row["id"],))
        token = _new_enrollment(conn, row["id"])
    from . import jobs
    jobs.cancel_all(row["id"], "Agent credential was reset.")
    audit(user_id, row["id"], "user", "enrollment_regenerated", "agent credential revoked", ip)
    return token


def revoke(user_id: int, cluster_id: Any, ip: Optional[str] = None) -> None:
    with db.session() as conn:
        row = get_own(conn, user_id, cluster_id)
        conn.execute("UPDATE clusters SET status = 'revoked', credential_hash = NULL, enroll_token_hash = NULL, "
                     "cluster_uid = NULL, paused = 0 WHERE id = ?", (row["id"],))
    from . import jobs
    jobs.cancel_all(row["id"], "Cluster was removed.")
    audit(user_id, row["id"], "user", "cluster_revoked", row["name"], ip)


def set_paused(user_id: int, cluster_id: Any, paused: bool, ip: Optional[str] = None) -> int:
    """Emergency stop / resume. Stopping cancels queued jobs and tells the agent to abort running ones."""
    with db.session() as conn:
        row = get_own(conn, user_id, cluster_id)
        if row["status"] == "revoked":
            raise ClusterError("This cluster was removed.", 409)
        conn.execute("UPDATE clusters SET paused = ? WHERE id = ?", (int(paused), row["id"]))
    n = 0
    if paused:
        from . import jobs
        n = jobs.cancel_all(row["id"], "Emergency stop")
    audit(user_id, row["id"], "user", "emergency_stop" if paused else "resume", f"{n} job(s) affected" if paused else "", ip)
    return n


# ----------------------------------------------------------------------------- agent-facing
def enroll(token: Any, info: Dict[str, Any], ip: Optional[str]) -> Dict[str, Any]:
    if not isinstance(token, str) or not token.startswith("cle_") or len(token) > 100:
        raise ClusterError("Invalid enrollment token.", 401)
    uid = info.get("cluster_uid")
    if not isinstance(uid, str) or not 8 <= len(uid) <= 100:
        raise ClusterError("cluster_uid is required.", 400)
    namespaces = info.get("allowed_namespaces")
    if (not isinstance(namespaces, list) or not namespaces or len(namespaces) > 50
            or not all(isinstance(n, str) and _DNS_LABEL.match(n) for n in namespaces)):
        raise ClusterError("allowed_namespaces must list the namespaces the agent may test (explicit allow-list).", 400)
    with db.session() as conn:
        row = conn.execute("SELECT * FROM clusters WHERE enroll_token_hash = ? AND status = 'pending'", (_hash(token),)).fetchone()
        exp = _parse(row["enroll_expires_at"]) if row else None
        if not row or not exp or exp < _now():
            audit(None, row["id"] if row else None, "agent", "enroll_rejected", "invalid or expired token", ip)
            raise ClusterError("Invalid or expired enrollment token.", 401)
        clash = conn.execute("SELECT id FROM clusters WHERE cluster_uid = ? AND id != ?", (uid, row["id"])).fetchone()
        if clash:
            audit(row["user_id"], row["id"], "agent", "enroll_rejected", "cluster already registered", ip)
            raise ClusterError("This Kubernetes cluster is already connected to a ChaosLab account.", 409)
        credential = "cla_" + secrets.token_urlsafe(32)
        conn.execute(
            "UPDATE clusters SET status = 'connected', credential_hash = ?, enroll_token_hash = NULL, enroll_expires_at = NULL, "
            "cluster_uid = ?, kube_version = ?, agent_version = ?, allowed_namespaces = ?, last_seen = ? WHERE id = ?",
            (_hash(credential), uid, str(info.get("kube_version") or "")[:40], str(info.get("agent_version") or "")[:40],
             json.dumps(sorted(set(namespaces))), db.utcnow(), row["id"]))
    audit(row["user_id"], row["id"], "agent", "agent_enrolled",
          f"namespaces={','.join(namespaces)} agent={info.get('agent_version')}", ip)
    return {"cluster_id": row["id"], "credential": credential, "name": row["name"]}


def authenticate_agent(authorization: Optional[str]):
    """Cluster row for a valid, non-revoked agent credential, else None."""
    if not authorization or not authorization.startswith("Bearer cla_") or len(authorization) > 120:
        return None
    with db.session() as conn:
        return conn.execute("SELECT * FROM clusters WHERE credential_hash = ? AND status = 'connected'",
                            (_hash(authorization[len("Bearer "):]),)).fetchone()


def record_heartbeat(row, body: Dict[str, Any], ip: Optional[str]) -> None:
    """Update liveness + the agent-reported inventory. Rejects a credential used from a different cluster."""
    uid = body.get("cluster_uid")
    if uid != row["cluster_uid"]:
        audit(row["user_id"], row["id"], "agent", "cluster_uid_mismatch", "credential used from a different cluster", ip)
        raise ClusterError("This credential belongs to a different cluster.", 403)
    sets, args = ["last_seen = ?"], [db.utcnow()]
    if isinstance(body.get("agent_version"), str):
        sets.append("agent_version = ?"); args.append(body["agent_version"][:40])
    namespaces = body.get("allowed_namespaces")
    if isinstance(namespaces, list) and namespaces and all(isinstance(n, str) and _DNS_LABEL.match(n) for n in namespaces[:50]):
        sets.append("allowed_namespaces = ?"); args.append(json.dumps(sorted(set(namespaces[:50]))))
    inv = body.get("inventory")
    if isinstance(inv, dict):
        clean: Dict[str, List[Dict[str, Any]]] = {}
        allowed = set(namespaces or json.loads(row["allowed_namespaces"] or "[]"))
        for ns, items in inv.items():
            if ns in allowed and isinstance(items, list):
                clean[ns] = [{"deployment": str(i.get("deployment"))[:63], "app_label": (str(i["app_label"])[:63] if i.get("app_label") else None),
                              "replicas": int(i.get("replicas") or 0), "ready": int(i.get("ready") or 0)}
                             for i in items[:200] if isinstance(i, dict)]
        blob = json.dumps(clean)
        if len(blob) <= _MAX_INVENTORY_BYTES:
            sets += ["inventory_json = ?", "inventory_at = ?"]; args += [blob, db.utcnow()]
    with db.session() as conn:
        conn.execute(f"UPDATE clusters SET {', '.join(sets)} WHERE id = ?", (*args, row["id"]))


# ----------------------------------------------------------------------------- registered applications
def inventory_services(cluster_row, namespace: str) -> List[Dict[str, Any]]:
    inv = json.loads(cluster_row["inventory_json"]) if cluster_row["inventory_json"] else {}
    return inv.get(namespace, [])


def register_application(user_id: int, cluster_id: Any, name: Any, namespace: Any, description: Any = "",
                         ip: Optional[str] = None) -> int:
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ClusterError("Application name is required (max 80 characters).")
    if not isinstance(namespace, str) or not _DNS_LABEL.match(namespace):
        raise ClusterError("namespace must be a valid Kubernetes namespace name.")
    description = (description or "").strip() if isinstance(description, str) else ""
    if len(description) > 500:
        raise ClusterError("Description must be at most 500 characters.")
    with db.session() as conn:
        row = get_own(conn, user_id, cluster_id)
        if row["status"] != "connected":
            raise ClusterError("Connect the agent first: this cluster is not enrolled.", 409)
        allowed = json.loads(row["allowed_namespaces"] or "[]")
        if namespace not in allowed:
            raise ClusterError(f"The agent is not allowed to test namespace '{namespace}'. Allowed: {', '.join(allowed)}.", 403)
        if conn.execute("SELECT 1 FROM applications WHERE cluster_id = ? AND namespace = ?", (row["id"], namespace)).fetchone():
            raise ClusterError("An application is already registered for this namespace.", 409)
        cur = conn.execute(
            "INSERT INTO applications (user_id, name, description, upload_type, status, cluster_id, namespace, created_at) "
            "VALUES (?, ?, ?, 'cluster', 'ready', ?, ?, ?)",
            (user_id, name.strip(), description, row["id"], namespace, db.utcnow()))
        app_id = cur.lastrowid
    audit(user_id, row["id"], "user", "application_registered", f"{name.strip()} -> {namespace}", ip)
    return app_id


# ----------------------------------------------------------------------------- enrollment throttle
_attempts: Dict[str, List[float]] = {}
_lock = threading.Lock()


def throttled(ip: str, record: bool = False, limit: int = 12, window: int = 600) -> bool:
    now = time.time()
    with _lock:
        recent = [t for t in _attempts.get(ip, []) if now - t < window]
        if record:
            recent.append(now)
        _attempts[ip] = recent
        return len(recent) > limit
