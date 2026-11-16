"""Reports API: list history, view/download a report, (re)generate."""

import json
from typing import Any, Dict

from . import db, reports
from .http_core import HttpError, Response, json_response, router
from .routes_experiments import get_own_experiment

_MIME = {"json": "application/json; charset=utf-8", "html": "text/html; charset=utf-8",
         "engine-html": "text/html; charset=utf-8"}


@router.route("GET", "/api/reports")
def list_reports(req):
    uid = req.user["id"]
    with db.session() as conn:
        rows = conn.execute(
            "SELECT e.id, e.target_service, e.fault_type, e.mode, e.status, e.created_at, e.completed_at, "
            "a.name AS application_name, "
            "(SELECT group_concat(format) FROM reports r WHERE r.experiment_id = e.id) AS formats, "
            "(SELECT overall_result FROM reports r WHERE r.experiment_id = e.id LIMIT 1) AS overall_result, "
            "(SELECT MAX(created_at) FROM reports r WHERE r.experiment_id = e.id) AS report_created_at "
            "FROM experiments e JOIN applications a ON a.id = e.application_id "
            "WHERE e.user_id = ? AND EXISTS (SELECT 1 FROM reports r WHERE r.experiment_id = e.id) "
            "ORDER BY e.id DESC LIMIT 200", (uid,)).fetchall()
    return json_response({"reports": [{
        "experiment_id": r["id"], "experiment_ref": reports.experiment_ref(r["id"]),
        "application_name": r["application_name"], "target_service": r["target_service"],
        "fault_type": r["fault_type"], "mode": r["mode"], "simulated": r["mode"] == "simulated",
        "status": r["status"], "overall_result": r["overall_result"],
        "created_at": r["report_created_at"], "formats": sorted((r["formats"] or "").split(",")),
    } for r in rows]})


@router.route("GET", "/api/experiments/<id>/report")
def get_report(req):
    fmt = req.qs("format", "html")
    if fmt not in _MIME:
        raise HttpError(400, "format must be one of: json, html, engine-html.")
    with db.session() as conn:
        exp = get_own_experiment(conn, req.user["id"], req.params["id"])
        # user_id is re-checked in the query: a report row can never be served to a non-owner.
        row = conn.execute("SELECT * FROM reports WHERE experiment_id = ? AND format = ? AND user_id = ?",
                           (exp["id"], fmt, req.user["id"])).fetchone()
    if not row or not reports.is_safe_report_path(row["file_path"]):
        raise HttpError(404, "Report not available. It is generated when an experiment finishes.")
    with open(row["file_path"], "rb") as f:
        body = f.read()
    ext = "json" if fmt == "json" else "html"
    suffix = "-SIMULATED" if exp["mode"] == "simulated" else ""
    headers = [("X-Report", "1")]
    if req.qs("download") == "1":
        headers.append(("Content-Disposition",
                        f'attachment; filename="chaoslab-report-{reports.experiment_ref(exp["id"])}{suffix}.{ext}"'))
    return Response(200, body, _MIME[fmt], headers)


@router.route("POST", "/api/experiments/<id>/report")
def regenerate_report(req):
    with db.session() as conn:
        exp = get_own_experiment(conn, req.user["id"], req.params["id"])
    if exp["status"] not in ("completed", "failed"):
        raise HttpError(409, "The experiment is still running; its report is generated when it finishes.")
    paths = reports.generate(exp["id"])
    return json_response({"formats": sorted(paths)}, 201)
