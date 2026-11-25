"""Print a ChaosLab experiment report in the terminal (read-only).

    python3 -m webapp.report_cli                 # latest experiment
    python3 -m webapp.report_cli EXP-0025        # a specific one (or just 25)
    python3 -m webapp.report_cli --watch         # follow the newest run live, then print its report
    python3 -m webapp.report_cli --list          # recent experiments

Run it on the server (it opens the SQLite database read-only and never changes anything).
The report content is the same as the web report: it is built with reports.build_report().
"""

import argparse
import json
import os
import sqlite3
import sys
import time

from . import config, reports

_TTY = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
_COL = {"PASS": "32", "FAIL": "31", "SKIP": "33", "SKIPPED": "33", "N/A": "90", "NA": "90",
        "NOT_APPLICABLE": "90", "NOT_EVALUATED": "90", "warn": "33", "error": "31", "head": "1;36", "dim": "90"}


def c(text, key):
    code = _COL.get(str(key))
    return f"\033[{code}m{text}\033[0m" if _TTY and code else str(text)


def connect(path):
    if not os.path.exists(path):
        sys.exit(f"Database not found: {path} (pass --db)")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def parse_id(ref):
    s = ref.upper().removeprefix("EXP-")
    if not s.isdigit():
        sys.exit(f"Not an experiment id: {ref} (use 25 or EXP-0025)")
    return int(s)


def get_row(conn, exp_id=None):
    if exp_id is None:
        return conn.execute("SELECT * FROM experiments ORDER BY id DESC LIMIT 1").fetchone()
    return conn.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()


def short(v, n=60):
    s = "" if v is None else (v if isinstance(v, str) else json.dumps(v))
    return s if len(s) <= n else s[: n - 1] + "…"


def print_list(conn, n):
    rows = conn.execute(
        "SELECT e.id, e.target_service, e.fault_type, e.mode, e.status, e.oracle_verdict, e.created_at, a.name "
        "FROM experiments e JOIN applications a ON a.id = e.application_id ORDER BY e.id DESC LIMIT ?", (n,)).fetchall()
    for r in rows:
        v = r["oracle_verdict"] or ("-" if r["status"] != "failed" else "NOT_EVALUATED")
        print(f"{reports.experiment_ref(r['id'])}  {r['created_at']}  {r['mode']:<9} {r['status']:<9} "
              f"{c(f'{v:<5}', v)}  {r['name']} / {r['target_service']} / {r['fault_type']}")


def print_report(conn, row):
    r = reports.build_report(conn, row)
    line = "=" * 78
    print(c(line, "head"))
    print(c(f" ChaosLab {r['experiment_ref']}  ({r['execution_mode']})", "head"))
    print(c(line, "head"))
    if r["simulated"]:
        print(c(" SIMULATED: no real cluster was touched; values are synthetic.", "warn"))
    cl = r.get("cluster")
    rows = [("Application", r["application"]["name"]), ("Target service", r["target_service"]),
            ("Fault", f"{r['fault']['label']} ({r['configuration']['duration_seconds']} s)"),
            ("Namespace", r["configuration"].get("namespace")),
            ("Cluster", f"{cl['name']} (#{cl['id']})" if cl else None),
            ("Data source", r["data_source"]), ("Started", r["started_at"]), ("Completed", r["completed_at"]),
            ("Status", r["final_status"])]
    for k, v in rows:
        if v:
            print(f" {k:<16} {v}")
    if r.get("error"):
        print(c(f" Error            {r['error']}", "error"))

    if r["timeline"]:
        print(c("\n Timeline", "head"))
        for t in r["timeline"]:
            print(f"   {t['at']}  {t['label']}")

    oracle = r.get("oracle")
    if oracle:
        titles = {"infrastructure": "Infrastructure", "application": "Application", "telemetry": "Telemetry"}
        for group, title in titles.items():
            items = oracle["groups"].get(group, [])
            if not items:
                continue
            print(c(f"\n {title} verification", "head"))
            for x in items:
                label = x["status_label"]
                print(f"   {c(f'{label:<5}', label)} {x['rule']:<30} {short(x['message'], 100)}")
                if label in ("FAIL",) or x.get("observed") is not None:
                    obs, exp = x.get("observed"), x.get("expected")
                    if obs is not None or exp is not None:
                        print(c(f"         observed {short(obs, 40)}   expected {short(exp, 40)}", "dim"))
        k = oracle["counts"]
        print(f"\n {k['pass']} PASS · {k['fail']} FAIL · {k['skip']} SKIP · {k['na']} N/A  (of {k['total']}; "
              f"{k['executed']} executed)")
    else:
        print(c("\n The Oracle did not run for this experiment.", "dim"))

    m = r["recovery_metrics"] or {}
    fmt = lambda v, u="": "n/a" if v is None else f"{v}{u}"
    print(c("\n Recovery metrics", "head"))
    for label, key, unit in [("Recovery time", "recovery_seconds", " s"),
                             ("Total recovery time", "total_recovery_seconds", " s"),
                             ("Baseline p95", "baseline_p95_ms", " ms"), ("Peak p95", "peak_p95_ms", " ms"),
                             ("Post-recovery p95", "post_recovery_p95_ms", " ms"),
                             ("Peak error rate", "peak_error_rate", ""), ("Error spans", "error_spans", ""),
                             ("Traces", "traces_count", "")]:
        print(f"   {label:<20} {fmt(m.get(key), unit)}")
    for n in r.get("notes", []):
        print(c(f" Note: {n}", "dim"))
    v = r["overall_verdict"]
    print(c(line, "head"))
    print(f" VERDICT: {c(v, v)}")
    print(c(line, "head"))


def watch(conn, exp_id):
    row = get_row(conn, exp_id)
    if not row:
        print("Waiting for an experiment to start…", flush=True)
        while not row:
            time.sleep(1)
            row = get_row(conn, exp_id)
    exp_id = row["id"]
    print(c(f"Following {reports.experiment_ref(exp_id)}: {row['target_service']} / {row['fault_type']} "
            f"({row['mode']})", "head"), flush=True)
    last = 0
    while True:
        for e in conn.execute("SELECT id, ts, stage, level, message FROM experiment_events "
                              "WHERE experiment_id = ? AND id > ? AND stage != 'metric' ORDER BY id",
                              (exp_id, last)).fetchall():
            last = e["id"]
            print(f"  {e['ts'][11:19]}  {e['stage']:<12} {c(e['message'], e['level'])}", flush=True)
        row = get_row(conn, exp_id)
        if row["status"] in ("completed", "failed"):
            break
        time.sleep(1)
    print()
    print_report(conn, row)


def main(argv=None):
    p = argparse.ArgumentParser(description="Show a ChaosLab experiment report in the terminal (read-only).")
    p.add_argument("experiment", nargs="?", help="experiment id, e.g. 25 or EXP-0025 (default: latest)")
    p.add_argument("--watch", action="store_true", help="follow the run live, then print the report")
    p.add_argument("--next", action="store_true", help="with --watch: wait for the NEXT experiment to start")
    p.add_argument("--list", nargs="?", const=15, type=int, metavar="N", help="list the N most recent experiments")
    p.add_argument("--db", default=config.DB_PATH, help=f"database path (default {config.DB_PATH})")
    a = p.parse_args(argv)
    conn = connect(a.db)
    if a.list:
        return print_list(conn, a.list)
    exp_id = parse_id(a.experiment) if a.experiment else None
    if a.watch:
        if a.next and exp_id is None:
            top = get_row(conn)
            nxt = (top["id"] + 1) if top else 1
            print("Waiting for the next experiment to start… (start it on the website)", flush=True)
            while not get_row(conn, nxt):
                time.sleep(1)
            exp_id = nxt
        return watch(conn, exp_id)
    row = get_row(conn, exp_id)
    if not row:
        sys.exit("No such experiment.")
    print_report(conn, row)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
