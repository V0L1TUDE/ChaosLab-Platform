"""Small SQLite store for the single-replica local project demo."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import uuid


class ProjectStore:
    def __init__(self, database_path, seeds=None):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            for key, record in (seeds or {}).items():
                db.execute('INSERT OR IGNORE INTO projects(id,data) VALUES(?,?)', (key, json.dumps(record)))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def list(self):
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT data FROM projects ORDER BY rowid')]

    def get(self, project_id):
        with self.connection() as db:
            row = db.execute('SELECT data FROM projects WHERE id=?', (project_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def create(self, record):
        item = {**record, 'id': 'proj-' + uuid.uuid4().hex}
        with self.connection() as db:
            db.execute('INSERT INTO projects(id,data) VALUES(?,?)', (item['id'], json.dumps(item)))
        return item
