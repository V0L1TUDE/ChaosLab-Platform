"""Startup tasks: seed the built-in demo application, recover interrupted experiments."""

from . import catalog, db


def seed_demo_application() -> None:
    with db.session() as conn:
        row = conn.execute("SELECT id FROM applications WHERE slug = ?", (catalog.DEMO_SLUG,)).fetchone()
        if row:
            conn.execute("UPDATE applications SET name = ?, description = ? WHERE id = ?",
                         (catalog.DEMO_NAME, catalog.DEMO_DESCRIPTION, row["id"]))
        else:
            conn.execute(
                "INSERT INTO applications (user_id, name, description, upload_type, status, slug, created_at) "
                "VALUES (NULL, ?, ?, 'builtin', 'ready', ?, ?)",
                (catalog.DEMO_NAME, catalog.DEMO_DESCRIPTION, catalog.DEMO_SLUG, db.utcnow()),
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
    seed_demo_application()
    n = recover_interrupted_experiments()
    if n:
        print(f"Recovered {n} interrupted experiment(s) -> failed.")
