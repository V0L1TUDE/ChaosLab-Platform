"""Clusters API (browser/user facing): connect a cluster, see its agent, register applications, emergency stop."""

from . import clusters, config, db
from .http_core import HttpError, json_response, router


def _server_url(req) -> str:
    if config.PUBLIC_URL:
        return config.PUBLIC_URL
    proto = req.headers.get("X-Forwarded-Proto") or "http"
    return f"{proto}://{req.headers.get('Host') or f'{config.HOST}:{config.PORT}'}"


def _wrap(fn):
    try:
        return fn()
    except clusters.ClusterError as e:
        raise HttpError(e.status, str(e))


def _enrollment_payload(req, token: str, row_id: int):
    with db.session() as conn:
        row = conn.execute("SELECT enroll_expires_at FROM clusters WHERE id = ?", (row_id,)).fetchone()
    return {"token": token, "expires_at": row["enroll_expires_at"], "server_url": _server_url(req),
            "note": "This token is shown once and works once. It is exchanged for a revocable agent credential."}


@router.route("GET", "/api/clusters")
def list_clusters(req):
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM clusters WHERE user_id = ? ORDER BY (status = 'revoked'), id DESC", (req.user["id"],)).fetchall()
        counts = {r["cluster_id"]: r["n"] for r in conn.execute(
            "SELECT cluster_id, COUNT(*) AS n FROM applications WHERE user_id = ? AND cluster_id IS NOT NULL GROUP BY cluster_id", (req.user["id"],))}
    out = []
    for r in rows:
        d = clusters.cluster_to_dict(r)
        d["applications_count"] = counts.get(r["id"], 0)
        out.append(d)
    return json_response({"clusters": out})


@router.route("POST", "/api/clusters")
def create_cluster(req):
    body = req.json()
    row, token = _wrap(lambda: clusters.create_cluster(req.user["id"], body.get("name"), req.ip))
    return json_response({"cluster": clusters.cluster_to_dict(row), "enrollment": _enrollment_payload(req, token, row["id"])}, 201)


@router.route("GET", "/api/clusters/<id>")
def cluster_detail(req):
    with db.session() as conn:
        row = _wrap(lambda: clusters.get_own(conn, req.user["id"], req.params["id"]))
        apps = conn.execute("SELECT id, name, namespace, status, created_at FROM applications WHERE cluster_id = ? AND user_id = ? ORDER BY id DESC",
                            (row["id"], req.user["id"])).fetchall()
        recent_jobs = conn.execute("SELECT id, experiment_id, status, created_at, finished_at, error FROM jobs WHERE cluster_id = ? "
                                   "ORDER BY id DESC LIMIT 10", (row["id"],)).fetchall()
        audit = conn.execute("SELECT ts, actor, action, detail, ip FROM audit_log WHERE cluster_id = ? ORDER BY id DESC LIMIT 50",
                             (row["id"],)).fetchall()
    d = clusters.cluster_to_dict(row, include_inventory=True)
    d["applications"] = [dict(a) for a in apps]
    d["jobs"] = [dict(j) for j in recent_jobs]
    d["audit"] = [dict(a) for a in audit]
    return json_response({"cluster": d})


@router.route("POST", "/api/clusters/<id>/enrollment")
def regenerate(req):
    token = _wrap(lambda: clusters.regenerate_enrollment(req.user["id"], req.params["id"], req.ip))
    return json_response({"enrollment": _enrollment_payload(req, token, int(req.params["id"]))}, 201)


@router.route("POST", "/api/clusters/<id>/emergency-stop")
def emergency_stop(req):
    n = _wrap(lambda: clusters.set_paused(req.user["id"], req.params["id"], True, req.ip))
    return json_response({"ok": True, "jobs_affected": n})


@router.route("POST", "/api/clusters/<id>/resume")
def resume(req):
    _wrap(lambda: clusters.set_paused(req.user["id"], req.params["id"], False, req.ip))
    return json_response({"ok": True})


@router.route("DELETE", "/api/clusters/<id>")
def remove_cluster(req):
    _wrap(lambda: clusters.revoke(req.user["id"], req.params["id"], req.ip))
    return json_response({"ok": True})


@router.route("POST", "/api/clusters/<id>/applications")
def register_application(req):
    body = req.json()
    app_id = _wrap(lambda: clusters.register_application(
        req.user["id"], req.params["id"], body.get("name"), body.get("namespace"), body.get("description", ""), req.ip))
    return json_response({"application_id": app_id}, 201)
