"""
ChaosLab Universal Report Generator Engine
==========================================
Produces multi-format reports (JSON, Markdown, standalone HTML)
with visual recovery timelines, Oracle scorecard breakdowns,
application-level verification proofs, and OpenTelemetry tracing analytics.
Supports multi-application environments (Online Boutique, Project Platform, LMS).
"""

import json
import os
from datetime import datetime
from typing import Any, Dict, List

class ExperimentReporter:
    """Generates Markdown and HTML reports from experiment evidence and Oracle outcomes."""

    @staticmethod
    def _parse_iso(iso_str: str) -> datetime:
        """Parse ISO timestamp handling timezone offsets."""
        if not iso_str:
            return None
        try:
            return datetime.fromisoformat(iso_str)
        except Exception:
            return None

    @classmethod
    def _compute_timeline(cls, report_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Compute relative time offsets for key experiment lifecycle events."""
        timestamps = report_data.get("timestamps", {})
        start_dt = cls._parse_iso(timestamps.get("experiment_start"))
        if not start_dt:
            return []

        events = []

        def add_event(name: str, key: str, description: str):
            dt = cls._parse_iso(timestamps.get(key))
            if dt:
                offset = (dt - start_dt).total_seconds()
                events.append({
                    "name": name,
                    "offset": round(max(0.0, offset), 2),
                    "timestamp": dt.strftime("%H:%M:%S.%f")[:-3],
                    "description": description
                })

        events.append({
            "name": "Experiment Initialized",
            "offset": 0.0,
            "timestamp": start_dt.strftime("%H:%M:%S.%f")[:-3],
            "description": f"Baseline established for {report_data.get('target')}"
        })

        add_event("Fault Injected", "fault_injected", f"Chaos Mesh fault injected ({report_data.get('fault')})")
        add_event("Target Impact Detected", "original_pod_disappeared", f"Original pod {report_data.get('original_pod')} terminated/impacted")
        add_event("Replacement Ready", "replacement_ready", f"Service recovered ({report_data.get('replacement_pod', 'ready')})")
        add_event("Experiment Completed", "experiment_end", "All verification rules evaluated by Oracle")

        events.sort(key=lambda x: x["offset"])
        return events

    @classmethod
    def generate_markdown(cls, data: Dict[str, Any]) -> str:
        """Generate executive Markdown report."""
        verdict = data.get("verdict", "UNKNOWN")
        verdict_badge = "✅ **PASS**" if verdict == "PASS" else "❌ **FAIL**"
        oracle = data.get("oracle_evaluation", {})
        app = data.get("application", {})
        telemetry = data.get("telemetry", {})
        timeline = cls._compute_timeline(data)
        app_group = data.get("application_group") or "Microservices Demo"

        md = []
        md.append(f"# ChaosLab Experiment Report: `{data.get('experiment')}`\n")
        md.append(f"> **Verdict**: {verdict_badge} | **Application**: `{app_group}` | **Target**: `{data.get('target')}` | **Fault**: `{data.get('fault')}` | **Recovery Time**: `{data.get('recovery_time_seconds')}s`\n")

        # Executive Summary Table
        md.append("## Executive Summary\n")
        md.append("| Property | Value |")
        md.append("| :--- | :--- |")
        md.append(f"| **Experiment** | `{data.get('experiment')}` |")
        md.append(f"| **Application Group** | **{app_group}** |")
        md.append(f"| **Target Microservice** | `{data.get('target')}` (namespace: `{data.get('namespace')}`) |")
        md.append(f"| **Fault Injected** | `{data.get('fault')}` |")
        md.append(f"| **Baseline Pod** | `{data.get('baseline', {}).get('pod_name')}` ({data.get('baseline', {}).get('ready')}) |")
        md.append(f"| **Replacement Pod** | `{data.get('replacement_pod')}` |")
        md.append(f"| **Recovery Time** | `{data.get('recovery_time_seconds')}s` (Threshold: `{data.get('config', {}).get('recovery', {}).get('timeout_seconds')}s`) |")
        md.append(f"| **Final Verdict** | **{verdict}** ({oracle.get('passed_rules', 0)}/{oracle.get('executed_rules', oracle.get('total_rules', 0))} executed invariants satisfied) |\n")

        # Lifecycle Timeline
        md.append("## Recovery Timeline Progression\n")
        md.append("| Offset | Time (UTC) | Milestone | Details |")
        md.append("| :---: | :---: | :--- | :--- |")
        for ev in timeline:
            md.append(f"| `+{ev['offset']}s` | `{ev['timestamp']}` | **{ev['name']}** | {ev['description']} |")
        md.append("")

        # Oracle Invariant Scorecard
        md.append("## Verification Oracle Scorecard\n")
        md.append("| Invariant Rule | Status | Observed | Expected | Details |")
        md.append("| :--- | :---: | :--- | :--- | :--- |")
        for r in oracle.get("rule_evaluations", []):
            st = r.get("status", "PASS" if r.get("passed") else "FAIL")
            md.append(f"| `{r['rule']}` | **{st}** | `{r['observed']}` | `{r['expected']}` | {r['message']} |")
        md.append("")

        # Application Correctness Evidence
        if app.get("enabled"):
            probe_type = app.get("probe_type", "health").upper()
            md.append("## Application-Level Correctness Verification\n")
            md.append(f"- **Health Probe ({probe_type})**: `" + str(app.get("health_status")) + "`")
            if app.get("transaction_id"):
                md.append(f"- **Functional Transaction Confirmation**: Processed successfully (`Entity ID: {app.get('transaction_id')}`)")
            elif app.get("functional_test_passed"):
                md.append("- **Functional Transaction**: Processed successfully (`Status: OK`)")
            md.append("- **Container Startup Logs**: `" + ("Clean initialization detected" if app.get("startup_log_detected") else "Not detected") + f"` ({app.get('log_error_count', 0)} fatal exceptions)\n")

        # Distributed Tracing & Telemetry Evidence
        if telemetry.get("available") and telemetry.get("traces_count", 0) > 0:
            lat = telemetry.get("latency_ms", {})
            md.append("## Distributed Tracing & Telemetry (OpenTelemetry / Jaeger)\n")
            md.append(f"- **Target Service**: `{telemetry.get('service')}`")
            md.append(f"- **Traces Captured**: `{telemetry.get('traces_count')}` ({telemetry.get('spans_count')} spans)")
            md.append(f"- **Error Spans**: `{telemetry.get('error_spans_count')}` ({telemetry.get('error_rate_pct')}% error rate)")
            md.append(f"- **Latency Distribution**: min `{lat.get('min')}ms` | avg `{lat.get('avg')}ms` | **p50** `{lat.get('p50')}ms` | **p95** `{lat.get('p95')}ms` | max `{lat.get('max')}ms`")
            if telemetry.get("sample_trace_ids"):
                md.append(f"- **Sample Trace IDs**: `" + "`, `".join(telemetry.get("sample_trace_ids")[:3]) + "`")
            if telemetry.get("operations"):
                md.append(f"- **Operations Monitored**: `" + "`, `".join(telemetry.get("operations")[:5]) + "`")
            md.append("")

        # Kubernetes Events
        k8s_events = data.get("kubernetes_events", [])
        if k8s_events:
            md.append("## Kubernetes Cluster Audit Events\n")
            md.append("| Type | Reason | Object | Message |")
            md.append("| :---: | :--- | :--- | :--- |")
            for ev in k8s_events[-6:]:
                md.append(f"| `{ev.get('type')}` | **{ev.get('reason')}** | `{ev.get('object')}` | {ev.get('message')} |")
            md.append("")

        return "\n".join(md)

    @classmethod
    def generate_html(cls, data: Dict[str, Any]) -> str:
        """Generate standalone, offline-ready HTML dashboard with recovery timeline and scorecard."""
        verdict = data.get("verdict", "UNKNOWN")
        is_pass = (verdict == "PASS")
        verdict_color = "#10b981" if is_pass else "#ef4444"
        verdict_bg = "#ecfdf5" if is_pass else "#fef2f2"
        oracle = data.get("oracle_evaluation", {})
        app = data.get("application", {})
        telemetry = data.get("telemetry", {})
        timeline = cls._compute_timeline(data)
        k8s_events = data.get("kubernetes_events", [])
        app_group = data.get("application_group") or "Microservices Demo"

        # Timeline HTML
        timeline_html = []
        for ev in timeline:
            timeline_html.append(f"""
            <div class="timeline-item">
                <div class="timeline-dot"></div>
                <div class="timeline-content">
                    <span class="timeline-offset">+{ev['offset']}s</span>
                    <strong>{ev['name']}</strong> &mdash; <em>{ev['timestamp']}</em>
                    <p>{ev['description']}</p>
                </div>
            </div>
            """)

        # Rules Table HTML
        rules_html = []
        for r in oracle.get("rule_evaluations", []):
            st = r.get("status", "PASS" if r.get("passed") else "FAIL")
            if st == "PASS":
                st_color = "#10b981"
            elif st == "FAIL":
                st_color = "#ef4444"
            elif st == "SKIPPED":
                st_color = "#f59e0b"
            else:
                st_color = "#64748b"

            rules_html.append(f"""
            <tr>
                <td><code>{r['rule']}</code></td>
                <td><span class="badge" style="background:{st_color}1a; color:{st_color}; border:1px solid {st_color}4d;">{st}</span></td>
                <td><code>{r['observed']}</code></td>
                <td><code>{r['expected']}</code></td>
                <td>{r['message']}</td>
            </tr>
            """)

        # Events Table HTML
        events_html = []
        for ev in k8s_events[-8:]:
            events_html.append(f"""
            <tr>
                <td><span class="badge" style="background:#e0f2fe; color:#0369a1;">{ev.get('type')}</span></td>
                <td><strong>{ev.get('reason')}</strong></td>
                <td><code>{ev.get('object')}</code></td>
                <td>{ev.get('message')}</td>
            </tr>
            """)

        # Telemetry Section HTML
        telemetry_html = ""
        if telemetry.get("available") and telemetry.get("traces_count", 0) > 0:
            lat = telemetry.get("latency_ms", {})
            traces_cnt = telemetry.get("traces_count", 0)
            spans_cnt = telemetry.get("spans_count", 0)
            err_cnt = telemetry.get("error_spans_count", 0)
            err_pct = telemetry.get("error_rate_pct", 0.0)
            trace_id_badges = " ".join([f"<code>{tid}</code>" for tid in telemetry.get("sample_trace_ids", [])[:3]])

            telemetry_html = f"""
            <div class="section">
                <h2>Distributed Tracing & Telemetry (OpenTelemetry / Jaeger)</h2>
                <div class="grid" style="margin-bottom: 16px;">
                    <div class="card">
                        <h3>Traces Captured</h3>
                        <p>{traces_cnt} <span style="font-size:13px; font-weight:normal; color:var(--text-muted);">({spans_cnt} spans)</span></p>
                    </div>
                    <div class="card">
                        <h3>Error Spans</h3>
                        <p>{err_cnt} <span style="font-size:13px; font-weight:normal; color:var(--text-muted);">({err_pct}%)</span></p>
                    </div>
                    <div class="card">
                        <h3>p50 Latency</h3>
                        <p>{lat.get('p50', 0)}ms</p>
                    </div>
                    <div class="card">
                        <h3>p95 Latency</h3>
                        <p>{lat.get('p95', 0)}ms</p>
                    </div>
                </div>
                <div class="app-highlight" style="background:#f8fafc; border-color:var(--border);">
                    <p><strong>Sample Trace IDs:</strong> {trace_id_badges}</p>
                </div>
            </div>
            """

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ChaosLab Report: {data.get('experiment')}</title>
    <style>
        :root {{
            --primary: #2563eb;
            --bg: #f8fafc;
            --card-bg: #ffffff;
            --text: #0f172a;
            --text-muted: #64748b;
            --border: #e2e8f0;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 24px;
            line-height: 1.5;
        }}
        .container {{
            max-width: 1040px;
            margin: 0 auto;
        }}
        .header {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 24px 32px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        }}
        .header-title h1 {{
            margin: 0 0 6px 0;
            font-size: 24px;
        }}
        .header-title p {{
            margin: 0;
            color: var(--text-muted);
            font-size: 14px;
        }}
        .verdict-badge {{
            background: {verdict_bg};
            color: {verdict_color};
            border: 2px solid {verdict_color};
            padding: 10px 24px;
            border-radius: 9999px;
            font-weight: 700;
            font-size: 20px;
            letter-spacing: 0.5px;
        }}
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}
        .card {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 18px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.03);
        }}
        .card h3 {{
            margin: 0 0 4px 0;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-muted);
        }}
        .card p {{
            margin: 0;
            font-size: 18px;
            font-weight: 600;
        }}
        .section {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 24px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.03);
        }}
        .section h2 {{
            margin: 0 0 16px 0;
            font-size: 18px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 8px;
        }}
        .timeline {{
            position: relative;
            padding-left: 28px;
        }}
        .timeline::before {{
            content: '';
            position: absolute;
            left: 7px;
            top: 6px;
            bottom: 6px;
            width: 2px;
            background: var(--border);
        }}
        .timeline-item {{
            position: relative;
            margin-bottom: 16px;
        }}
        .timeline-item:last-child {{
            margin-bottom: 0;
        }}
        .timeline-dot {{
            position: absolute;
            left: -28px;
            top: 6px;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: var(--primary);
            border: 2px solid #ffffff;
            box-shadow: 0 0 0 2px var(--primary);
        }}
        .timeline-content {{
            font-size: 14px;
        }}
        .timeline-offset {{
            font-weight: 700;
            color: var(--primary);
            margin-right: 6px;
        }}
        .timeline-content p {{
            margin: 2px 0 0 0;
            color: var(--text-muted);
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }}
        th, td {{
            text-align: left;
            padding: 10px 14px;
            border-bottom: 1px solid var(--border);
        }}
        th {{
            background: #f8fafc;
            font-weight: 600;
            color: var(--text-muted);
        }}
        code {{
            background: #f1f5f9;
            padding: 2px 6px;
            border-radius: 4px;
            font-family: monospace;
            font-size: 12px;
        }}
        .badge {{
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-weight: 600;
            font-size: 11px;
        }}
        .app-highlight {{
            background: #f0fdf4;
            border: 1px solid #bbf7d0;
            border-radius: 8px;
            padding: 14px 18px;
            margin-top: 12px;
        }}
        .app-highlight p {{
            margin: 4px 0;
            font-size: 14px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <div class="header">
            <div class="header-title">
                <h1>ChaosLab Report: {data.get('experiment')}</h1>
                <p>Application: <strong>{app_group}</strong> &bull; Target: <strong>{data.get('target')}</strong> &bull; Namespace: <code>{data.get('namespace')}</code> &bull; Fault: <code>{data.get('fault')}</code></p>
            </div>
            <div class="verdict-badge">{verdict}</div>
        </div>

        <!-- Key Metrics Cards -->
        <div class="grid">
            <div class="card">
                <h3>Target Microservice</h3>
                <p>{data.get('target')}</p>
            </div>
            <div class="card">
                <h3>Recovery Time</h3>
                <p>{data.get('recovery_time_seconds')}s</p>
            </div>
            <div class="card">
                <h3>Invariants Passed</h3>
                <p>{oracle.get('passed_rules', 0)} / {oracle.get('executed_rules', oracle.get('total_rules', 0))} <span style="font-size:12px; font-weight:normal; color:var(--text-muted);">({oracle.get('skipped_rules', 0)} N/A)</span></p>
            </div>
            <div class="card">
                <h3>Chaos Fault Type</h3>
                <p>{data.get('fault')}</p>
            </div>
        </div>

        <!-- Timeline Section -->
        <div class="section">
            <h2>Lifecycle Timeline Progression</h2>
            <div class="timeline">
                {"".join(timeline_html)}
            </div>
        </div>

        <!-- App Details -->
        {f'''
        <div class="section">
            <h2>Application-Level Correctness Verification</h2>
            <div class="app-highlight">
                <p><strong>Health Probe:</strong> <code>{app.get('health_status', 'N/A')}</code> &mdash; Verified operational</p>
                {f"<p><strong>Functional Confirmation:</strong> Valid entity processed (<code>ID: {app.get('transaction_id')}</code>)</p>" if app.get('transaction_id') else ("<p><strong>Functional Transaction:</strong> Processed successfully (Status: <code>200 OK</code>)</p>" if app.get('functional_test_passed') else "")}
                <p><strong>Container Logs:</strong> {"Clean startup confirmed" if app.get('startup_log_detected') else "Startup pattern missing"} ({app.get('log_error_count', 0)} fatal errors)</p>
            </div>
        </div>
        ''' if app.get('enabled') else ''}

        <!-- Telemetry Section -->
        {telemetry_html}

        <!-- Oracle Scorecard -->
        <div class="section">
            <h2>Verification Oracle Invariant Scorecard</h2>
            <table>
                <thead>
                    <tr>
                        <th>Rule Name</th>
                        <th>Status</th>
                        <th>Observed</th>
                        <th>Expected</th>
                        <th>Evaluation Message</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(rules_html)}
                </tbody>
            </table>
        </div>

        <!-- K8s Events -->
        {f'''
        <div class="section">
            <h2>Kubernetes Lifecycle Events</h2>
            <table>
                <thead>
                    <tr>
                        <th>Type</th>
                        <th>Reason</th>
                        <th>Object</th>
                        <th>Message</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(events_html)}
                </tbody>
            </table>
        </div>
        ''' if k8s_events else ''}
    </div>
</body>
</html>
"""
        return html

    @classmethod
    def save_all(cls, experiment_name: str, report_data: Dict[str, Any], reports_dir: str) -> Dict[str, str]:
        """Save JSON, Markdown, and HTML reports to disk."""
        os.makedirs(reports_dir, exist_ok=True)
        clean_name = experiment_name.lower().replace("-", "_")

        json_path = os.path.join(reports_dir, f"{clean_name}_report.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2)

        md_path = os.path.join(reports_dir, f"{clean_name}_report.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(cls.generate_markdown(report_data))

        html_path = os.path.join(reports_dir, f"{clean_name}_report.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(cls.generate_html(report_data))

        return {"json": json_path, "markdown": md_path, "html": html_path}
