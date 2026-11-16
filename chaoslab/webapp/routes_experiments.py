"""Experiments API: capabilities, create/run, list, detail (polled by the monitor), Oracle catalog."""

from typing import Any, Dict

from . import catalog, config, db, engine_bridge, runner_service
from .execution import ORACLE_GROUPS, STAGES
from .http_core import HttpError, json_response, router
from .routes_apps import get_accessible_app

LIST_LIMIT = 200


def get_own_experiment(conn, user_id: int, raw_id: str):
    """Experiments are strictly per-user; anyone else's looks like 404."""
    try:
        exp_id = int(raw_id)
    except ValueError:
        raise HttpError(404, "Experiment not found.")
    row = conn.execute("SELECT * FROM experiments WHERE id = ? AND user_id = ?", (exp_id, user_id)).fetchone()
    if not row:
        raise HttpError(404, "Experiment not found.")
    return row


@router.route("GET", "/api/capabilities")
def capabilities(req):
    return json_response({"storage": config.STORAGE_BACKEND, "modes": runner_service.capabilities(), "stages": [{"key": k, "label": l} for k, l in STAGES]})


@router.route("GET", "/api/experiments")
def list_experiments(req):
    uid = req.user["id"]
    app_filter = req.qs("application_id")
    sql = ("SELECT e.*, a.name AS application_name FROM experiments e JOIN applications a ON a.id = e.application_id "
           "WHERE e.user_id = ?")
    args: list = [uid]
    if app_filter:
        if not app_filter.isdigit():
            raise HttpError(400, "application_id must be an integer.")
        sql += " AND e.application_id = ?"
        args.append(int(app_filter))
    sql += f" ORDER BY e.id DESC LIMIT {LIST_LIMIT}"
    with db.session() as conn:
        rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = runner_service.experiment_summary(r)
        d["application_name"] = r["application_name"]
        out.append(d)
    return json_response({"experiments": out})


@router.route("POST", "/api/experiments")
def create_experiment(req):
    body = req.json()
    app_id = body.get("application_id")
    if isinstance(app_id, bool) or not isinstance(app_id, int):
        raise HttpError(400, "application_id must be an integer.")
    with db.session() as conn:
        app_row = get_accessible_app(conn, req.user["id"], app_id)
    try:
        exp_id = runner_service.create_and_start(
            req.user, app_row, body.get("target_service"), body.get("fault_type"),
            body.get("duration_seconds"), body.get("mode"),
        )
    except runner_service.ExperimentRejected as e:
        raise HttpError(e.status, str(e))
    with db.session() as conn:
        row = conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
        return json_response({"experiment": runner_service.experiment_summary(row)}, 201)


@router.route("GET", "/api/experiments/<id>")
def experiment_detail(req):
    after = req.qs("after_event", "0")
    after_id = int(after) if after and after.isdigit() else 0
    with db.session() as conn:
        row = get_own_experiment(conn, req.user["id"], req.params["id"])
        detail = runner_service.experiment_detail(conn, row, after_id)
        report_rows = conn.execute("SELECT format FROM reports WHERE experiment_id = ? ORDER BY format", (row["id"],)).fetchall()
    detail["reports"] = [r["format"] for r in report_rows]
    return json_response({"experiment": detail})


@router.route("GET", "/api/oracle/invariants")
def oracle_invariants(req):
    """The Verification Oracle's invariant catalog (read from the real Oracle) with mode availability."""
    rules = {r.name: r.description for r in engine_bridge.oracle().VerificationOracle().rules}
    availability: Dict[str, Dict[str, str]] = {
        "infrastructure": {
            "live": "Evaluated from real Kubernetes / Chaos Mesh evidence.",
            "simulated": "Predefined simulated evidence, evaluated by the real Oracle.",
        },
        "application": {
            "live": "Only for experiments with application probes configured (curated experiments such as "
                    "paymentservice / cartservice / checkoutservice pod-kill); otherwise N/A.",
            "simulated": "Predefined simulated probe results.",
        },
        "telemetry": {
            "live": "Only when Jaeger telemetry is enabled for the experiment; otherwise N/A.",
            "simulated": "Predefined simulated trace data.",
        },
    }
    groups = [{"id": g, "title": g.capitalize(), "availability": availability[g],
               "invariants": [{"name": n, "description": rules.get(n, "")} for n in names]}
              for g, names in ORACLE_GROUPS.items()]
    return json_response({
        "groups": groups,
        "statuses": [
            {"status": "PASS", "meaning": "The invariant was evaluated and satisfied."},
            {"status": "FAIL", "meaning": "The invariant was evaluated and violated."},
            {"status": "SKIP", "meaning": "The check was configured but its evidence was unavailable. NOT a pass."},
            {"status": "N/A", "meaning": "The check does not apply to this experiment. NOT a pass."},
        ],
    })
