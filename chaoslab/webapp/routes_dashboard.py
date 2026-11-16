"""Dashboard aggregates, computed from the caller's own records only."""

from . import db, runner_service
from .http_core import json_response, router
from .routes_apps import app_to_dict


@router.route("GET", "/api/dashboard")
def dashboard(req):
    uid = req.user["id"]
    with db.session() as conn:
        apps = conn.execute(
            "SELECT a.*, (SELECT COUNT(*) FROM experiments e WHERE e.application_id = a.id AND e.user_id = ?) AS n "
            "FROM applications a WHERE a.user_id = ? OR a.user_id IS NULL "
            "ORDER BY (a.user_id IS NULL) DESC, a.id DESC", (uid, uid)).fetchall()
        stats = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(status = 'completed' AND oracle_verdict = 'PASS') AS passed, "
            "SUM(status = 'failed' OR (status = 'completed' AND oracle_verdict = 'FAIL')) AS failed, "
            "SUM(status IN ('pending', 'running')) AS active, "
            "SUM(mode = 'live') AS live, SUM(mode = 'simulated') AS simulated "
            "FROM experiments WHERE user_id = ?", (uid,)).fetchone()
        recent = conn.execute(
            "SELECT e.*, a.name AS application_name FROM experiments e JOIN applications a ON a.id = e.application_id "
            "WHERE e.user_id = ? ORDER BY e.id DESC LIMIT 8", (uid,)).fetchall()
    recent_out = []
    for r in recent:
        d = runner_service.experiment_summary(r)
        d["application_name"] = r["application_name"]
        recent_out.append(d)
    return json_response({
        "totals": {
            "applications": len(apps),
            "experiments": stats["total"] or 0,
            "successful": stats["passed"] or 0,
            "failed": stats["failed"] or 0,
            "active": stats["active"] or 0,
            "live": stats["live"] or 0,
            "simulated": stats["simulated"] or 0,
        },
        "applications": [app_to_dict(a, a["n"]) for a in apps],
        "recent": recent_out,
    })
