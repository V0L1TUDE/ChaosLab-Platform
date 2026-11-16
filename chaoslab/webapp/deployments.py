"""
Deployment lifecycle for uploaded applications
==============================================
Status flow:  validated -> deploying -> deployed -> (undeploy / TTL reaper) -> validated
                              `-> deploy_failed (auto-cleaned; can be retried)

Deploys run on a worker thread; undeploys are quick and synchronous. Limits: per-user and global caps,
a TTL after which the reaper tears the namespace down, and no teardown while an experiment is running.
"""

from datetime import datetime, timedelta, timezone
import json
import threading
import time
import traceback
from typing import Any, Dict, Optional

from . import config, db, k8s_deploy
from .live_backend import live_capability


class DeployRejected(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _parse(ts: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def expires_at(row) -> Optional[str]:
    start = _parse(row["deployed_at"])
    if not start or row["status"] != "deployed" or config.DEPLOY_TTL_MINUTES <= 0:
        return None
    return (start + timedelta(minutes=config.DEPLOY_TTL_MINUTES)).isoformat(timespec="seconds")


def deployment_info(row) -> Dict[str, Any]:
    validation = json.loads(row["validation_json"]) if row["validation_json"] else None
    return {
        "status": row["status"],
        "namespace": row["namespace"],
        "deployed_at": row["deployed_at"],
        "expires_at": expires_at(row),
        "error": row["deploy_error"],
        "validation": validation,
        "can_deploy": config.DEPLOY_ENABLED and row["status"] in ("validated", "deploy_failed"),
        "ttl_minutes": config.DEPLOY_TTL_MINUTES,
    }


def _active_experiments(conn, app_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM experiments WHERE application_id = ? AND status IN ('pending', 'running')",
                        (app_id,)).fetchone()[0]


def start_deploy(app_id: int, user_id: int) -> None:
    if not config.DEPLOY_ENABLED:
        raise DeployRejected("Deploying uploaded applications is disabled on this server (CHAOSLAB_DEPLOY_ENABLED=false).", 503)
    cap = live_capability()
    if not cap["available"]:
        raise DeployRejected(f"Cannot deploy: {cap['reason']}", 503)
    with db.session() as conn:
        row = conn.execute("SELECT * FROM applications WHERE id = ? AND user_id = ?", (app_id, user_id)).fetchone()
        if not row:
            raise DeployRejected("Application not found.", 404)
        if row["status"] not in ("validated", "deploy_failed") or not row["manifest_json"]:
            raise DeployRejected(f"Only validated Kubernetes manifests can be deployed (status: {row['status']}).", 409)
        mine = conn.execute("SELECT COUNT(*) FROM applications WHERE user_id = ? AND status IN ('deploying', 'deployed')",
                            (user_id,)).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM applications WHERE status IN ('deploying', 'deployed')").fetchone()[0]
        if mine >= config.MAX_DEPLOYED_PER_USER:
            raise DeployRejected(f"You can have at most {config.MAX_DEPLOYED_PER_USER} deployed applications. Undeploy one first.", 429)
        if total >= config.MAX_DEPLOYED_TOTAL:
            raise DeployRejected("The shared cluster has reached its limit of deployed applications. Try again later.", 429)
        # Conditional update guards against double-clicks / races.
        cur = conn.execute("UPDATE applications SET status = 'deploying', deploy_error = NULL, namespace = ? "
                           "WHERE id = ? AND status IN ('validated', 'deploy_failed')",
                           (k8s_deploy.namespace_for(app_id), app_id))
        if cur.rowcount != 1:
            raise DeployRejected("Deployment already in progress.", 409)
        resources = json.loads(row["manifest_json"])["resources"]
    threading.Thread(target=_deploy_worker, args=(app_id, user_id, resources), daemon=True, name=f"deploy-{app_id}").start()


def _deploy_worker(app_id: int, user_id: int, resources) -> None:
    try:
        ok, msg = k8s_deploy.deploy(app_id, user_id, resources, config.DEPLOY_TIMEOUT_SECONDS)
    except Exception as e:
        traceback.print_exc()
        ok, msg = False, f"Unexpected deployment error: {type(e).__name__}"
        try:
            k8s_deploy.undeploy(app_id)
        except Exception:
            pass
    with db.session() as conn:
        if ok:
            conn.execute("UPDATE applications SET status = 'deployed', deployed_at = ?, deploy_error = NULL WHERE id = ?",
                         (db.utcnow(), app_id))
        else:
            conn.execute("UPDATE applications SET status = 'deploy_failed', namespace = NULL, deploy_error = ? WHERE id = ?",
                         (msg[:1000], app_id))


def undeploy(app_id: int, user_id: Optional[int] = None, force: bool = False) -> None:
    """Tear down the sandbox namespace. Refuses while experiments run unless `force` (used on app delete)."""
    with db.session() as conn:
        q, args = "SELECT * FROM applications WHERE id = ?", [app_id]
        if user_id is not None:
            q, args = q + " AND user_id = ?", args + [user_id]
        row = conn.execute(q, args).fetchone()
        if not row:
            raise DeployRejected("Application not found.", 404)
        if row["status"] == "deploying":
            raise DeployRejected("Deployment is still in progress.", 409)
        if _active_experiments(conn, app_id):
            raise DeployRejected("An experiment is running on this application. Wait for it to finish.", 409)
    ok, msg = k8s_deploy.undeploy(app_id)
    if not ok:
        raise DeployRejected(msg, 502)
    with db.session() as conn:
        conn.execute("UPDATE applications SET status = CASE WHEN manifest_json IS NOT NULL THEN 'validated' ELSE status END, "
                     "namespace = NULL, deployed_at = NULL, deploy_error = NULL WHERE id = ? AND status != 'invalid'", (app_id,))


# ----------------------------------------------------------------------------- reaper / recovery
def reap_expired() -> int:
    """Undeploy applications older than the TTL. Returns how many were torn down."""
    if config.DEPLOY_TTL_MINUTES <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=config.DEPLOY_TTL_MINUTES)).isoformat(timespec="seconds")
    with db.session() as conn:
        rows = conn.execute("SELECT id FROM applications WHERE status = 'deployed' AND deployed_at < ?", (cutoff,)).fetchall()
    n = 0
    for r in rows:
        try:
            undeploy(r["id"])
            n += 1
            print(f"Auto-undeployed application {r['id']} (TTL {config.DEPLOY_TTL_MINUTES} min).")
        except DeployRejected:
            pass          # experiment running or cluster hiccup: try again next sweep
    return n


def recover_interrupted() -> int:
    """Deployments that were 'deploying' when the server stopped can never finish: clean up and mark failed."""
    with db.session() as conn:
        rows = conn.execute("SELECT id FROM applications WHERE status = 'deploying'").fetchall()
    for r in rows:
        try:
            k8s_deploy.undeploy(r["id"])
        except Exception:
            pass
        with db.session() as conn:
            conn.execute("UPDATE applications SET status = 'deploy_failed', namespace = NULL, "
                         "deploy_error = 'Server restarted during deployment.' WHERE id = ?", (r["id"],))
    return len(rows)


_reaper_started = False


def start_reaper(interval_seconds: int = 60) -> None:
    global _reaper_started
    if _reaper_started:
        return
    _reaper_started = True

    def loop():
        while True:
            time.sleep(interval_seconds)
            try:
                reap_expired()
            except Exception:
                traceback.print_exc()
    threading.Thread(target=loop, daemon=True, name="deploy-reaper").start()
