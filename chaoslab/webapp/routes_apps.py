"""Applications API: list, upload, detail, delete. Every query is scoped to the caller."""

from typing import Any, Dict, Optional

from . import catalog, db, uploads
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
        "executable": builtin,               # only the built-in demo app can run experiments in the MVP
        "experiments_count": experiments_count,
    }
    d["slug"] = row["slug"]
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

    try:
        with db.session() as conn:
            cur = conn.execute(
                "INSERT INTO applications (user_id, name, description, file_path, original_filename, file_size, "
                "upload_type, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'registered', ?)",
                (req.user["id"], name, description, meta["file_path"], meta["original_filename"],
                 meta["file_size"], meta["upload_type"], db.utcnow()),
            )
            row = conn.execute("SELECT * FROM applications WHERE id = ?", (cur.lastrowid,)).fetchone()
    except Exception:
        uploads.delete_stored_file(meta["file_path"])   # don't orphan the file if the DB insert fails
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
        data["note"] = ("Uploaded applications are registered and stored only. Automatic deployment and "
                        "experiment execution for uploaded applications is not part of this MVP; run experiments "
                        "against the built-in demo application.")
    return json_response({"application": data})


@router.route("DELETE", "/api/applications/<id>")
def delete_application(req):
    uid = req.user["id"]
    with db.session() as conn:
        row = get_accessible_app(conn, uid, _app_id(req))
        if row["user_id"] is None:
            raise HttpError(403, "The built-in demo application cannot be deleted.")
        path = row["file_path"]
        conn.execute("DELETE FROM applications WHERE id = ? AND user_id = ?", (row["id"], uid))
    uploads.delete_stored_file(path)
    return json_response({"ok": True})
