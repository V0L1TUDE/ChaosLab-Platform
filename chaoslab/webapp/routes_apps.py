"""Applications API: list, upload, detail, delete. Every query is scoped to the caller."""

import json
from typing import Any, Dict, Optional

from . import catalog, db, deployments, manifests, probe_spec, uploads
from .http_core import HttpError, json_response, router


def app_to_dict(row, experiments_count: int = 0) -> Dict[str, Any]:
    builtin = row["user_id"] is None
    d = {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "upload_type": row["upload_type"],
        "status": row["status"],
        "created_at": row["created_at"],
        "is_builtin": builtin,
        "original_filename": row["original_filename"],
        "file_size": row["file_size"],
        "executable": builtin or row["status"] in ("validated", "deployed") or bool(row["cluster_id"]),   # can run experiments
        "deployable": (not builtin) and row["status"] in ("validated", "deploy_failed"),
        "experiments_count": experiments_count,
    }
    d["slug"] = row["slug"]
    d["cluster_id"] = row["cluster_id"]
    if row["cluster_id"]:
        d["namespace"] = row["namespace"]
        with db.session() as c2:
            cl = c2.execute("SELECT name, status FROM clusters WHERE id = ?", (row["cluster_id"],)).fetchone()
        d["cluster_name"] = cl["name"] if cl else None
        d["executable"] = bool(cl and cl["status"] != "revoked")
    if not builtin and row["validation_json"]:
        v = json.loads(row["validation_json"])
        d["validation"] = {"valid": v["valid"], "errors": v["errors"], "warnings": v["warnings"]}
        d["namespace"] = row["namespace"]
    if builtin:
        app = catalog.get_builtin(row["slug"])
        d["services_count"] = len(catalog.discover_services(app)["services"]) if app else 0
    return d


def get_accessible_app(conn, user_id: int, app_id: int):
    """Own uploads + built-in apps only. Other users' apps look like 404 (no existence leak)."""
    row = conn.execute(
        "SELECT * FROM applications WHERE id = ? AND (user_id = ? OR user_id IS NULL)", (app_id, user_id)
    ).fetchone()
    if not row:
        raise HttpError(404, "Application not found.")
    return row


def _app_id(req) -> int:
    try:
        return int(req.params["id"])
    except ValueError:
        raise HttpError(404, "Application not found.")


@router.route("GET", "/api/applications")
def list_applications(req):
    uid = req.user["id"]
    with db.session() as conn:
        rows = conn.execute(
            "SELECT a.*, (SELECT COUNT(*) FROM experiments e WHERE e.application_id = a.id AND e.user_id = ?) AS n "
            "FROM applications a WHERE a.user_id = ? OR a.user_id IS NULL "
            "ORDER BY (a.user_id IS NULL) DESC, CASE WHEN a.user_id IS NULL THEN a.id END ASC, a.id DESC",
            (uid, uid),
        ).fetchall()
    return json_response({"applications": [app_to_dict(r, r["n"]) for r in rows]})


@router.route("POST", "/api/applications")
def upload_application(req):
    fields, files = req.multipart()
    name = (fields.get("name") or "").strip()
    description = (fields.get("description") or "").strip()
    if not 1 <= len(name) <= 80:
        raise HttpError(400, "Application name is required (max 80 characters).")
    if len(description) > 500:
        raise HttpError(400, "Description must be at most 500 characters.")
    f = files.get("file")
    if not f or not f["filename"]:
        raise HttpError(400, "An application file is required.")

    try:
        meta = uploads.validate_and_store(req.user["id"], f["filename"], f["data"])
    except uploads.UploadError as e:
        raise HttpError(e.status, str(e))

    # Kubernetes manifest uploads (.yaml/.yml/.zip with yaml) are validated in memory against the strict sandbox.
    status, manifest_json, validation_json = "registered", None, None
    if manifests.has_manifest_candidates(meta["upload_type"], f["data"]):
        result = manifests.analyze(meta["upload_type"], f["data"])
        status = "validated" if result["valid"] else "invalid"
        manifest_json = json.dumps({"resources": result["resources"]}) if result["valid"] else None
        validation_json = json.dumps({k: result[k] for k in ("valid", "errors", "warnings", "workloads")})

    try:
        with db.session() as conn:
            cur = conn.execute(
                "INSERT INTO applications (user_id, name, description, file_path, storage_backend, original_filename, "
                "file_size, upload_type, status, manifest_json, validation_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (req.user["id"], name, description, meta["file_path"], meta["storage_backend"], meta["original_filename"],
                 meta["file_size"], meta["upload_type"], status, manifest_json, validation_json, db.utcnow()),
            )
            row = conn.execute("SELECT * FROM applications WHERE id = ?", (cur.lastrowid,)).fetchone()
    except Exception:
        try:   # don't orphan the stored object if the DB insert fails
            uploads.delete_stored_file(meta["file_path"], meta["storage_backend"])
        except uploads.UploadError:
            pass
        raise
    return json_response({"application": app_to_dict(row)}, 201)


@router.route("GET", "/api/applications/<id>")
def application_detail(req):
    uid = req.user["id"]
    with db.session() as conn:
        row = get_accessible_app(conn, uid, _app_id(req))
        exps = conn.execute(
            "SELECT id, target_service, fault_type, mode, status, oracle_verdict, created_at, completed_at "
            "FROM experiments WHERE application_id = ? AND user_id = ? ORDER BY id DESC LIMIT 50",
            (row["id"], uid),
        ).fetchall()
    data = app_to_dict(row, len(exps))
    data["experiments"] = [dict(e) for e in exps]
    if row["user_id"] is None:
        data["catalog"] = catalog.describe_app(catalog.get_builtin(row["slug"]), force=req.qs("refresh") == "1")
    else:
        resolved = catalog.resolve_app(row)
        if row["cluster_id"]:
            data["note"] = ("This application runs in your own cluster. Experiments are executed by the ChaosLab agent "
                            "inside that cluster; nothing is deployed or changed by this server.")
            if resolved:
                data["probes"] = resolved["probes"]
                data["catalog"] = catalog.describe_app(resolved)
                data["cluster"] = {"id": resolved["cluster_id"], "name": resolved["cluster_name"],
                                   "online": resolved["cluster_online"], "paused": resolved["cluster_paused"]}
            else:
                data["note"] = "The cluster for this application was removed. Past experiments and reports are kept."
        elif resolved:
            data["catalog"] = catalog.describe_app(resolved, force=req.qs("refresh") == "1")
            data["deployment"] = deployments.deployment_info(row)
        elif row["status"] == "invalid":
            data["deployment"] = deployments.deployment_info(row)
            data["note"] = "This upload is not a valid Kubernetes manifest bundle for the sandbox. See the errors below, fix them and upload again."
        else:
            data["note"] = ("This file is registered and stored only. To run experiments on your own application, upload "
                            "Kubernetes manifests (.yaml, or a .zip containing .yaml files).")
    return json_response({"application": data})


@router.route("DELETE", "/api/applications/<id>")
def delete_application(req):
    uid = req.user["id"]
    with db.session() as conn:
        row = get_accessible_app(conn, uid, _app_id(req))
        if row["user_id"] is None:
            raise HttpError(403, "The built-in demo application cannot be deleted.")
        if row["status"] == "deploying":
            raise HttpError(409, "Deployment is still in progress. Try again in a moment.")
        if row["status"] in ("deployed", "deploy_failed") or row["namespace"]:
            try:                                  # never leave a sandbox namespace running without an owner
                deployments.undeploy(row["id"], uid)
            except deployments.DeployRejected as e:
                raise HttpError(e.status, str(e))
        ref, backend = row["file_path"], row["storage_backend"]
        # Remove the stored object first: if the backend fails the record is kept so the delete can be retried.
        try:
            uploads.delete_stored_file(ref, backend)
        except uploads.UploadError as e:
            raise HttpError(e.status, str(e))
        conn.execute("DELETE FROM applications WHERE id = ? AND user_id = ?", (row["id"], uid))
    return json_response({"ok": True})


@router.route("POST", "/api/applications/<id>/deploy")
def deploy_application(req):
    with db.session() as conn:
        row = get_accessible_app(conn, req.user["id"], _app_id(req))
    try:
        deployments.start_deploy(row["id"], req.user["id"])
    except deployments.DeployRejected as e:
        raise HttpError(e.status, str(e))
    with db.session() as conn:
        row = conn.execute("SELECT * FROM applications WHERE id = ?", (row["id"],)).fetchone()
    return json_response({"deployment": deployments.deployment_info(row)}, 202)


@router.route("POST", "/api/applications/<id>/undeploy")
def undeploy_application(req):
    with db.session() as conn:
        row = get_accessible_app(conn, req.user["id"], _app_id(req))
    if row["user_id"] is None:
        raise HttpError(403, "Built-in applications are managed by the platform.")
    try:
        deployments.undeploy(row["id"], req.user["id"])
    except deployments.DeployRejected as e:
        raise HttpError(e.status, str(e))
    with db.session() as conn:
        row = conn.execute("SELECT * FROM applications WHERE id = ?", (row["id"],)).fetchone()
    return json_response({"deployment": deployments.deployment_info(row)})


@router.route("POST", "/api/applications/<id>/probes")
def set_probes(req):
    """Set (or clear, with probes=null) the declarative verification probes for one service of a cluster app."""
    body = req.json()
    service = body.get("service")
    with db.session() as conn:
        row = conn.execute("SELECT * FROM applications WHERE id = ? AND user_id = ?", (_app_id(req), req.user["id"])).fetchone()
        if not row:
            raise HttpError(404, "Application not found.")
        if not row["cluster_id"]:
            raise HttpError(409, "Verification probes are available for applications in a connected cluster.")
        resolved = catalog.resolve_app(row)
        if not resolved:
            raise HttpError(409, "The cluster for this application was removed.")
        names = catalog.targetable_services(resolved)
        if not isinstance(service, str) or service not in names:
            raise HttpError(400, f"Unknown service. Choose one of: {', '.join(names) or '(none reported yet)'}.")
        probes = json.loads(row["probe_json"]) if row["probe_json"] else {}
        if body.get("probes") is None:
            probes.pop(service, None)
        else:
            clean, err = probe_spec.validate(body["probes"])
            if err:
                raise HttpError(400, err)
            probes[service] = clean
        conn.execute("UPDATE applications SET probe_json = ? WHERE id = ?", (json.dumps(probes) if probes else None, row["id"]))
    return json_response({"probes": probes})
