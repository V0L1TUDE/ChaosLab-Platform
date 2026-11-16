"""Startup tasks: seed the built-in demo application, recover interrupted experiments."""

from . import catalog, db


def seed_builtin_applications() -> None:
    with db.session() as conn:
        for app in catalog.BUILTIN_APPS:
            row = conn.execute("SELECT id FROM applications WHERE slug = ?", (app["slug"],)).fetchone()
            if row:
                conn.execute("UPDATE applications SET name = ?, description = ? WHERE id = ?",
                             (app["name"], app["description"], row["id"]))
            else:
                conn.execute(
                    "INSERT INTO applications (user_id, name, description, upload_type, status, slug, created_at) "
                    "VALUES (NULL, ?, ?, 'builtin', 'ready', ?, ?)",
                    (app["name"], app["description"], app["slug"], db.utcnow()),
                )


def recover_interrupted_experiments() -> int:
    """Experiments left pending/running by a previous process can never finish: mark them failed."""
    with db.session() as conn:
        cur = conn.execute(
            "UPDATE experiments SET status = 'failed', current_stage = 'failed', "
            "error = 'Server restarted while the experiment was running.', completed_at = ? "
            "WHERE status IN ('pending', 'running')",
            (db.utcnow(),),
        )
        return cur.rowcount


def on_startup() -> None:
    seed_builtin_applications()
    n = recover_interrupted_experiments()
    if n:
        print(f"Recovered {n} interrupted experiment(s) -> failed.")
