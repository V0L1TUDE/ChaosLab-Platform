"""
Report generation
=================
One structured report per experiment, persisted as JSON (machine readable) and a self-contained
HTML page (human readable); Live runs additionally register the engine's native HTML report.
Simulated reports carry an unmissable SIMULATED banner and are never presented as real evidence.
"""

import html
import json
import os
from typing import Any, Dict, List, Optional

from . import catalog, config, db
from .execution import DATA_SOURCE_LIVE, DATA_SOURCE_SIMULATED

SIM_NOTICE = ("SIMULATED REPORT: this experiment was a demonstration run using predefined data. No fault "
              "was injected into Kubernetes and none of the metrics or invariant outcomes below are real observations.")


def experiment_ref(exp_id: int) -> str:
    return f"EXP-{exp_id:04d}"


def build_report(conn, row) -> Dict[str, Any]:
    result = json.loads(row["result_json"]) if row["result_json"] else None
    cfg = json.loads(row["config_json"] or "{}")
    app_name = conn.execute("SELECT name FROM applications WHERE id = ?", (row["application_id"],)).fetchone()[0]
    events = conn.execute(
        "SELECT ts, stage, level, message, simulated FROM experiment_events "
        "WHERE experiment_id = ? AND stage != 'metric' ORDER BY id", (row["id"],)
    ).fetchall()
    simulated = row["mode"] == "simulated"
    oracle = (result or {}).get("oracle")
    if row["status"] == "failed" or not oracle:
        overall, final = "NOT_EVALUATED", "FAILED (execution error; the Oracle did not run)"
    else:
        overall = oracle["verdict"]
        final = f"COMPLETED, Oracle verdict {overall}"
    fault = catalog.get_fault(row["fault_type"]) or {}
    metrics = dict((result or {}).get("metrics") or {})
    series = metrics.pop("series", [])
    return {
        "report_version": 1,
        "experiment_id": row["id"],
        "experiment_ref": experiment_ref(row["id"]),
        "generated_at": db.utcnow(),
        "application": {"id": row["application_id"], "name": app_name},
        "execution_mode": "Simulated" if simulated else "Live",
        "simulated": simulated,
        "data_source": DATA_SOURCE_SIMULATED if simulated else DATA_SOURCE_LIVE,
        "simulation_notice": SIM_NOTICE if simulated else None,
        "target_service": row["target_service"],
        "fault": {"id": row["fault_type"], "label": fault.get("label", row["fault_type"]),
                  "engine_type": fault.get("engine_type")},
        "configuration": {"service": row["target_service"], "fault": row["fault_type"],
                          "duration_seconds": row["duration_seconds"], "namespace": cfg.get("namespace"),
                          "engine_config": cfg.get("engine_config")},
        "status": row["status"],
        "final_status": final,
        "error": row["error"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "timeline": (result or {}).get("timeline", []),
        "event_log": [dict(e) for e in events],
        "health": (result or {}).get("health"),
        "recovery_metrics": metrics,
        "metric_samples": series,
        "oracle": oracle,
        "overall_verdict": overall,
        "notes": (result or {}).get("notes", []),
    }


# --------------------------------------------------------------------------- HTML rendering
_CSS = """
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:#0b0f19;color:#e5e9f0;margin:0;padding:32px;line-height:1.5}
.wrap{max-width:980px;margin:0 auto}h1{font-size:24px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px;color:#93c5fd;
border-bottom:1px solid #26334d;padding-bottom:6px}.muted{color:#94a3b8;font-size:13px}
.banner{background:#451a03;border:2px solid #f59e0b;color:#fde68a;padding:14px 18px;border-radius:10px;font-weight:600;margin:16px 0}
.live{background:#052e1b;border:1px solid #10b981;color:#a7f3d0;padding:10px 16px;border-radius:10px;margin:16px 0;font-size:13px}
table{border-collapse:collapse;width:100%;margin:6px 0 4px;font-size:13px}th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #1e293b;vertical-align:top}
th{color:#94a3b8;font-weight:600}.kv td:first-child{width:230px;color:#94a3b8}
.b{display:inline-block;padding:1px 9px;border-radius:99px;font-size:11px;font-weight:700;letter-spacing:.4px}
.PASS{background:#064e3b;color:#6ee7b7}.FAIL{background:#7f1d1d;color:#fca5a5}.SKIP{background:#78350f;color:#fcd34d}
.NA{background:#334155;color:#cbd5e1}.NOT_EVALUATED{background:#334155;color:#cbd5e1}.SIM{background:#f59e0b;color:#1c1917}
code,pre{font-family:Consolas,monospace;font-size:12px}pre{background:#111827;padding:12px;border-radius:8px;overflow:auto}
.verdict{font-size:20px;font-weight:800;margin:10px 0}
"""


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _badge(label: str, simulated: bool = False) -> str:
    cls = {"N/A": "NA"}.get(label, label)
    out = f'<span class="b {_e(cls)}">{_e(label)}</span>'
    if simulated:
        out += ' <span class="b SIM">SIMULATED</span>'
    return out


def _kv(rows: List[tuple]) -> str:
    return '<table class="kv">' + "".join(f"<tr><td>{_e(k)}</td><td>{v}</td></tr>" for k, v in rows) + "</table>"


def _chart_svg(samples: List[Dict[str, Any]], key: str, title: str, color: str) -> str:
    pts = [(s["t"], s[key]) for s in samples if s.get(key) is not None]
    if len(pts) < 2:
        return ""
    w, h, pad = 560, 120, 8
    tmax = max(p[0] for p in pts) or 1
    vmax = max(p[1] for p in pts) or 1
    coords = " ".join(f"{pad + (t / tmax) * (w - 2 * pad):.1f},{h - pad - (v / vmax) * (h - 2 * pad):.1f}" for t, v in pts)
    return (f'<div class="muted">{_e(title)} (max {vmax:g})</div><svg width="{w}" height="{h}" '
            f'style="background:#111827;border-radius:8px"><polyline fill="none" stroke="{color}" stroke-width="2" points="{coords}"/></svg>')


def render_html(r: Dict[str, Any]) -> str:
    sim = r["simulated"]
    parts: List[str] = []
    parts.append(f"<h1>ChaosLab Experiment Report {_e(r['experiment_ref'])}</h1>")
    parts.append(f'<div class="muted">Generated {_e(r["generated_at"])} &middot; Execution mode: <b>{_e(r["execution_mode"])}</b></div>')
    if sim:
        parts.append(f'<div class="banner">{_e(SIM_NOTICE)}</div>')
    else:
        parts.append(f'<div class="live">{_e(r["data_source"])}</div>')

    verdict = r["overall_verdict"]
    vlabel = "NOT_EVALUATED" if verdict == "NOT_EVALUATED" else verdict
    parts.append(f'<div class="verdict">Overall Oracle verdict: {_badge(vlabel, sim)}</div>')
    parts.append(f'<div class="muted">Final experiment status: {_e(r["final_status"])}</div>')
    if r.get("error"):
        parts.append(f'<pre>{_e(r["error"])}</pre>')

    cfg = r["configuration"]
    parts.append("<h2>Experiment</h2>" + _kv([
        ("Experiment ID", _e(r["experiment_ref"])),
        ("Application", _e(r["application"]["name"])),
        ("Target microservice", _e(r["target_service"])),
        ("Fault type", _e(r["fault"]["label"])),
        ("Duration (s)", _e(cfg["duration_seconds"])),
        ("Namespace", _e(cfg["namespace"])),
        ("Execution mode", _e(r["execution_mode"]) + (' <span class="b SIM">SIMULATED</span>' if sim else "")),
        ("Status", _e(r["status"])),
        ("Started", _e(r["started_at"])),
        ("Completed", _e(r["completed_at"])),
    ]))

    parts.append("<h2>Execution timeline</h2>")
    if r["timeline"]:
        parts.append("<table><tr><th>Stage</th><th>Time (UTC)</th></tr>" +
                     "".join(f"<tr><td>{_e(t['label'])}</td><td>{_e(t['at'])}</td></tr>" for t in r["timeline"]) + "</table>")
    else:
        parts.append('<div class="muted">No timeline recorded (the run ended before any stage completed).</div>')
    if r["event_log"]:
        parts.append("<table><tr><th>Time</th><th>Stage</th><th>Event</th></tr>" +
                     "".join(f"<tr><td>{_e(e['ts'])}</td><td>{_e(e['stage'])}</td><td>{_e(e['message'])}</td></tr>"
                             for e in r["event_log"]) + "</table>")

    oracle = r.get("oracle")
    titles = {"infrastructure": "Infrastructure verification", "application": "Application verification",
              "telemetry": "Telemetry verification"}
    if oracle:
        c = oracle["counts"]
        for group, title in titles.items():
            rules = oracle["groups"].get(group, [])
            parts.append(f"<h2>{title}</h2>")
            parts.append("<table><tr><th>Invariant</th><th>Status</th><th>Detail</th><th>Observed</th><th>Expected</th></tr>" +
                         "".join(
                             f"<tr><td><code>{_e(x['rule'])}</code></td><td>{_badge(x['status_label'])}</td>"
                             f"<td>{_e(x['message'])}</td><td>{_e(x['observed'])}</td><td>{_e(x['expected'])}</td></tr>"
                             for x in rules) + "</table>")
        parts.append(f'<div class="muted">{c["pass"]} PASS &middot; {c["fail"]} FAIL &middot; {c["skip"]} SKIP &middot; '
                     f'{c["na"]} N/A (of {c["total"]}). SKIP and N/A are not counted as PASS; only {c["executed"]} '
                     f'invariant(s) were actually executed.</div>')
    else:
        parts.append('<h2>Verification</h2><div class="muted">The Oracle did not run for this experiment.</div>')

    m = r["recovery_metrics"] or {}
    fmt = lambda v, u="": "n/a (not collected)" if v is None else f"{v}{u}"
    parts.append("<h2>Recovery metrics</h2>" + _kv([
        ("Recovery time", _e(fmt(m.get("recovery_seconds"), " s"))),
        ("Total recovery time", _e(fmt(m.get("total_recovery_seconds"), " s"))),
        ("Baseline p95 latency", _e(fmt(m.get("baseline_p95_ms"), " ms"))),
        ("Peak p95 latency", _e(fmt(m.get("peak_p95_ms"), " ms"))),
        ("Post-recovery p95 latency", _e(fmt(m.get("post_recovery_p95_ms"), " ms"))),
        ("Peak error rate", _e(fmt(m.get("peak_error_rate")))),
        ("Error spans", _e(fmt(m.get("error_spans")))),
        ("Traces", _e(fmt(m.get("traces_count")))),
    ]))
    samples = r.get("metric_samples") or []
    parts.append(_chart_svg(samples, "p95_ms", "p95 latency (ms)" + (" - SIMULATED" if sim else ""), "#60a5fa"))
    parts.append(_chart_svg(samples, "replicas_ready", "Ready replicas" + (" - SIMULATED" if sim else ""), "#34d399"))

    parts.append("<h2>Configuration</h2><pre>" + _e(json.dumps(cfg, indent=2)) + "</pre>")
    for n in r.get("notes", []):
        parts.append(f'<div class="muted">Note: {_e(n)}</div>')

    title = f"ChaosLab {_e(r['experiment_ref'])}" + (" (SIMULATED)" if sim else "")
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>{title}</title>'
            f'<style>{_CSS}</style></head><body><div class="wrap">' + "\n".join(parts) + "</div></body></html>")


# --------------------------------------------------------------------------- persistence
def _upsert(conn, exp_id: int, user_id: int, fmt: str, path: str, overall: str) -> None:
    conn.execute(
        "INSERT INTO reports (experiment_id, user_id, format, file_path, overall_result, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(experiment_id, format) DO UPDATE SET "
        "file_path = excluded.file_path, overall_result = excluded.overall_result, created_at = excluded.created_at",
        (exp_id, user_id, fmt, path, overall, db.utcnow()),
    )


def generate(exp_id: int) -> Dict[str, str]:
    """Build and persist the JSON + HTML reports (and register the engine-native HTML for Live runs)."""
    with db.session() as conn:
        row = conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
        if not row:
            raise ValueError("experiment not found")
        report = build_report(conn, row)
        out_dir = os.path.join(config.REPORT_DIR, f"user_{row['user_id']}")
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.join(out_dir, f"experiment_{exp_id}")
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        with open(base + ".html", "w", encoding="utf-8") as f:
            f.write(render_html(report))
        overall = report["overall_verdict"]
        _upsert(conn, exp_id, row["user_id"], "json", base + ".json", overall)
        _upsert(conn, exp_id, row["user_id"], "html", base + ".html", overall)
        paths = {"json": base + ".json", "html": base + ".html"}

        if row["mode"] == "live" and row["status"] == "completed":
            name = ((json.loads(row["config_json"]).get("engine_config") or {}).get("name") or "")
            native = os.path.join(config.REPORT_DIR, "engine", f"{name.lower().replace('-', '_')}_report.html")
            if name and os.path.isfile(native):
                _upsert(conn, exp_id, row["user_id"], "engine-html", native, overall)
                paths["engine-html"] = native
    return paths


def is_safe_report_path(path: str) -> bool:
    root = os.path.realpath(config.REPORT_DIR)
    real = os.path.realpath(path)
    return os.path.commonpath([root, real]) == root and os.path.isfile(real)
